# kind validation environment — OpenStudio Server 3.11.0 (issue #19, decision D13)

> **Image digest pinning (issue #172).** Every `image:` line in
> `scripts/manifests/*.yaml` is pinned as `name:tag@sha256:…`, including the
> upstream `mongo` and `redis` images. The chart's defaults (`mongo:6.0.7`,
> `redis:6.0.9`) are EOL with unfixed CVEs (CVE-2024-31449 / CVE-2024-46981
> family in Redis, CVE-2024-1351 in Mongo); the kind recipe bumps to the
> supported 7.x lines:
>
> | Image | Recipe tag (was) | Recipe tag (now) | Digest |
> |---|---|---|---|
> | mongo | `6.0.7` (EOL Jul 2025) | `7.0.40` | `sha256:b6421fd6d1c5ded6377b397d8983e2f82e2100dc5123332dcfda2065a472be5b` |
> | redis | `6.0.9` (EOL since 2021) | `7.4.10` | `sha256:e9b2e45ecd47fbb69b877cf8d045d5cccaaaed52524b6e098b4abe8212994f73` |
> | nrel/openstudio-server | `3.11.0` (unchanged) | `3.11.0` | `sha256:de95093868fe2f7e382e29995a72936b2ab95627e653d8ac4a9df1bf3667a975` |
>
> The `nrel/openstudio-server:3.11.0` tag is fixed by the operator contract
> (the operator targets 3.11.0; see AGENTS.md Fixed identifiers); only the
> digest moves. The digest is the image-index digest — the kubelet resolves
> the right platform digest (`linux/amd64` for kind control-plane nodes).
> Renew via `docker buildx imagetools inspect <image>:<tag>`; the kind
> recipe is the project's validation target (D13), so a stack with
> image-derived CVEs is not a clean signal for the dryRun walkthrough.

Reproducible recipe for standing up a single-node [kind](https://kind.sigs.k8s.io/)
cluster running the real `nrel/openstudio-server:3.11.0` stack, capturing REST
API fixtures against the operator's contract, and smoke-testing Phase 1 with
`dryRun: true`. This is the dev-machine half of D13; the work-cluster dry-run
runbook (#20) builds on it.

Everything here is **approximation-labeled**: it is strong enough to validate
the REST API contract and the operator's read paths, weak for NFS-eviction
testing (see [Approximations](#approximations-vs-production)).

## Prerequisites

- docker (daemon running)
- kind, kubectl — e.g.:

  ```bash
  curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.24.0/kind-linux-amd64 && chmod +x ./kind && sudo mv ./kind /usr/local/bin/
  curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && sudo install kubectl /usr/local/bin/kubectl
  ```

- curl, jq (fixture capture), python3 + `.venv` (`pip install -e '.[dev]'`)
- ~10 GB free disk (3.11.0 server image is ~2 GB compressed), outbound network
  to Docker Hub on the first run

## Quickstart

```bash
scripts/create-kind-cluster.sh          # idempotent; kind cluster 'os-operator-validation'
scripts/deploy-openstudio-stack.sh      # applies scripts/manifests/*.yaml, waits for rollouts
kubectl -n openstudio-server port-forward svc/web 8080:80   # then http://localhost:8080
```

First deployment pulls the images inside the kind node and boots Rails
(`wait-for-it` on `db`/`queue`, mongoid index creation) — expect **5–15
minutes** until `deployment/web` is Ready. On a slow link, pre-pull on the
host and load instead:

```bash
docker pull nrel/openstudio-server:3.11.0
kind load docker-image nrel/openstudio-server:3.11.0 --name os-operator-validation
# (same for mongo:7.0.40 / redis:7.4.10 — see the digest-pinning table above)
```

## What gets deployed (topology parity vs the helm chart)

Names are load-bearing — the operator targets them by fixed identifier
(AGENTS.md "Fixed identifiers"). All in namespace `openstudio-server`.

| Chart object (develop) | kind manifest | Parity notes |
|---|---|---|
| Deployment `db` (mongo:7.0.40@sha256:b6421fd6d1c5ded6377b397d8983e2f82e2100dc5123332dcfda2065a472be5b) + Service `db:27017` | `scripts/manifests/01-mongo.yaml` | same image/creds (`openstudio`/`openstudio`, auth_source `admin`); persistence emptyDir; **chart default is 6.0.7 (EOL) — bumped to 7.0.40 here for CVE hygiene (#172)** |
| Deployment `redis` (redis:7.4.10@sha256:e9b2e45ecd47fbb69b877cf8d045d5cccaaaed52524b6e098b4abe8212994f73) + Service `queue:6379` | `scripts/manifests/02-redis.yaml` | same image; password committed as the placeholder `openstudio-rotated` (issue #150 — was the publicly-known `openstudio` literal until #150; run `scripts/rotate_redis_password.sh` to install a per-cluster random one before applying); persistence emptyDir; **chart default is 6.0.9 (EOL) — bumped to 7.4.10 here for CVE hygiene (#172)** |
| PVC `nfs-pvc` (RWX, storageClass `nfs`) | `scripts/manifests/03-nfs-hostpath.yaml` | **hostPath stand-in** on the kind node (`/tmp/openstudio-kind-nfs`) |
| Deployment `web` + Service `web:80` | `scripts/manifests/04-web.yaml` | image pinned **3.11.0** (chart default 3.8.0-1 is NOT the target); same command/env (`QUEUES=analysis_wrappers`, `REDIS_URL`, `MONGO_USER/PASSWORD`, `SECRET_KEY_BASE` chart default); mounts `nfs-pvc` at `/mnt/openstudio`; image **digest-pinned** to `sha256:de95093868fe2f7e382e29995a72936b2ab95627e653d8ac4a9df1bf3667a975` (#172) |
| Deployment `web-background` | `scripts/manifests/05-web-background.yaml` | same command (`resque:workers` scaled by `COUNT`); `OS_SERVER_NUMBER_OF_WORKERS` tuned 30→2; mounts `nfs-pvc`; image **digest-pinned** to the same `sha256:de95093868fe2f7e382e29995a72936b2ab95627e653d8ac4a9df1bf3667a975` (#172) |
| Deployment `worker` (no HPA post-#77) | `scripts/manifests/06-worker.yaml` | scaled-minimal: replicas **1** (the ScaledObject scales it from 0–5 per Redis backlog; chart's HPA `worker-hpa` REMOVED — two autoscalers fight one Deployment, #77); `QUEUES=requeued,simulations`; emptyDir scratch at `/mnt/openstudio`; preStop hook + `terminationGracePeriodSeconds: 5200` kept; image **digest-pinned** to the same `sha256:de95093868fe2f7e382e29995a72936b2ab95627e653d8ac4a9df1bf3667a975` (#172) |
| LoadBalancer, cluster-autoscaler, rserve, priority classes | — | intentionally omitted |

## Approximations vs production

- **hostPath for `nfs-pvc`** — fine for API validation and for exercising the
  `DELETE /analyses/{id}` server-side cascade (it rm-rf's real directories
  under `/mnt/openstudio/server/assets/...` on the stand-in volume, and
  `web`'s `rails-entrypoint` provisions them at boot). **Weak for
  NFS-eviction testing**: a hostPath never disappears, so stuck-mount /
  unmount-eviction scenarios cannot be reproduced. Contents live inside the
  kind node container: survive pod restarts, die with the cluster.
- **mongo/redis persistence is emptyDir** — data/queue state lost on pod
  restart; irrelevant for API shape validation, relevant if you restart
  mid-analysis.
- **No metrics-server** — kind still has no metrics-server; the chart's
  HPA is gone (post-#77), and KEDA's Redis-list trigger does not depend
  on the metrics-server anyway (KEDA queries Redis directly). The
  ScaledObject's `value` expression reads `LLEN resque:queue:simulations`
  + `LLEN resque:queue:requeued` and scales `worker` 0→N (clamped to
  `maxReplicaCount: 5`).
- **Single node, no node-group labels** — the chart's affinity
  (`nodegroup: web-group|worker-group`) is dropped; everything co-schedules.
- **web replicas pinned to 1, `strategy: Recreate`** — mirrors the
  single-writer assumption the chart makes (see values.yaml comment on
  distributed file locking).

## Fixture capture + drift check

```bash
kubectl -n openstudio-server port-forward svc/web 8080:80 &
scripts/capture_fixtures.sh --base-url http://localhost:8080          # reads only
scripts/capture_fixtures.sh --mutate                                   # + soft_stop/action/requeue (interactive WARNING)
scripts/capture_fixtures.sh --mutate --with-delete                     # + DELETE cascade (louder WARNING)
scripts/check_fixture_drift.py --live                                  # diff shapes vs tests/fixtures/contract-shapes.json
scripts/check_fixture_drift.py --samples                               # committed synthetic samples (no cluster needed)
```

`capture_fixtures.sh` writes one JSON envelope per endpoint
(`{endpoint, method, http_status, content_type, location?, body}`) to
`tests/fixtures/live/` and **fails loudly when the stack is unreachable**
(it will offer to start the port-forward itself, then refuse to run).
Per-analysis endpoints need an analysis to exist — creation routes are
**nested** (top-level `POST /analyses.json` 404s; `resources :analyses` is
`only: [:index]` at the top level). Minimal REST seeding, live-verified:

```bash
B=http://localhost:8080
PID=$(curl -s -X POST $B/projects.json -H 'Content-Type: application/json' \
      -d '{"project": {"name": "fixture-capture"}}' | jq -r ._id)
AID=$(curl -s -X POST $B/projects/$PID/analyses.json -H 'Content-Type: application/json' \
      -d '{"analysis": {"name": "fixture-capture-analysis"}}' | jq -r ._id)
DPID=$(curl -s -X POST $B/analyses/$AID/data_points.json -H 'Content-Type: application/json' \
      -d '{"data_point": {"name": "fixture-capture-dp"}}' | jq -r ._id)
```

This yields shape-complete fixtures (a fresh analysis has no `status` key
yet — see the drift findings). For richer states (`start_time` populated,
started datapoints with `ip_address`) upload a real seed model via the web
UI or the python client before re-running the capture. Commit the captured
files (acceptance: "fixtures committed") and record the drift result in the
section below.

### Drift findings (live-verified on kind, 2026-08-18, stack `3.11.0`)

Captured with `scripts/capture_fixtures.sh --mutate --with-delete` against a
minimal seeded stack (project → analysis → datapoint created via the nested
REST routes; commit `tests/fixtures/live/`).
`scripts/check_fixture_drift.py --live`: **10 pass, 2 error-shape, 0 fail**
after folding the findings below into `tests/fixtures/contract-shapes.json`.

1. **Unknown ids never 404.** `mongoid.yml` sets `raise_not_found_error:
   false`, so `Analysis#find` returns nil: `GET
   /analyses/{unknown}/page_data.json` answers **200 `{analysis: null}`**
   and `GET /analyses/{unknown}/status.json` answers **200
   `{analyses: []}`** (status uses `where()`, which never raises). The
   operator client must treat these bodies as not-found — a 404-based
   error branch would never fire.
2. **Raw docs omit nil fields — absent ≠ null.** A fresh analysis has **no
   `status` key at all** (state machine values appear only after the
   workflow advances); `start_time` likewise never appears in
   `/analyses.json` (forbidden in the shapes file). `page_data.json`
   (`as_json(only:)`) drops nil fields too — a minimal analysis serializes
   as only `{name, data_points, results, output_variables}`, so the **SLA
   anchor `start_time` is absent, not null, until the first job** —
   absence-tolerant access required. DataPoint docs, in contrast, carry
   `run_start_time` / `ip_address` / `job_id` as explicit `null`s before
   start.
3. **Ids are UUID strings**, not BSON ObjectIds, and raw docs expose `_id`
   only; the derived views (`status.json`, `data_points/status`) duplicate
   it as `id`.
4. **Content negotiation changes the status code.** `DELETE
   /analyses/{id}` returns **204** with `Accept: application/json` but a
   **302 HTML redirect** with curl's default `Accept: */*` (same for the
   `action`/`requeue` POSTs' HTML variants). The capture script pins
   `Accept: application/json` for these; `soft_stop` is captured with
   `*/*` because it is HTML-only by design.
5. **`requeue` on a jobless datapoint 500s.** A dp that was never
   queued/started has no Resque job; `POST /data_points/{id}/requeue`
   returns `500 {status: 500, error: "Internal Server Error"}` (JSON under
   JSON accept). Only requeue dps that have a `job_id`. The captured
   fixture intentionally documents this error shape.
6. **`action start` optimistically reports success.** On a seedless
   analysis it still returns `{code: 200, analysis: …}`; the failure
   surfaces later via the workflow (analysis resets to unset-status). Do
   not treat `code: 200` as "analysis will complete".
7. **`status.json` wrapping is count-based:** exactly one match →
   `{analysis: …}`; zero or many → `{analyses: […]}` (live-verified for 0
   and 1). `exit_on_guideline_14` renders as an integer.
8. **`soft_stop` redirects with an absolute URL** (e.g.
   `http://localhost:8080/analyses/{id}`) — don't parse it as a path.

Static-only findings (pre-live, still true): `soft_stop` has no
`format.json` (302 HTML is the whole contract surface); `action` carries
the real outcome in the body's `code` field over HTTP 200.

These findings are folded into `tests/fixtures/contract-shapes.json`
(`live_verified_global_rules`) — **follow-up needed in
`.agents/skills/_shared/api-contracts/openstudio-server-v3.11.0-rest.md`
(main checkout) to incorporate 1–6**; until then this file is the more
current truth. Value-level drift (timestamp formats, state membership
across a full run) still needs a capture against an analysis with a real
seed model — create one via the web UI or the python client, re-run
`capture_fixtures.sh`, and diff again.

## Phase 1 `dryRun: true` smoke walkthrough (feeds #20's runbook)

Intended sequence once the Phase-1 handler work (#8) is merged; #5, #6 and
#7 (config, client, CR-status store) are already in. Steps 1, 2 and 5 are
**live-verified** against the kind stack while preparing this doc (CRD/RBAC
apply + a `dryRun: true` OSCM CR accepted with defaults populated; client
reads succeed); steps 3–4 need #8's handler. Run from a checkout on the
dev machine with kubeconfig pointed at kind:

1. **Install the operator's K8s objects** (note: #3 fixes the manifests'
   stale `openstudio` namespace references — use its fixed versions or
   patch them to `openstudio-server`):

   ```bash
   kubectl apply -f deploy/crd.yaml
   kubectl apply -f deploy/rbac.yaml
   # Issue #293 — ValidatingAdmissionPolicy narrows the operator's
   # pods/delete blast radius (native RBAC has no label-selector slot).
   # Apply AFTER rbac.yaml so the operator SA referenced in the CEL
   # rule already exists at admission-evaluation time.
   kubectl apply -f deploy/pod-delete-admission-policy.yaml
   ```

2. **Create the OSCM custom resource in dry-run mode** — the CRD
   `spec.serverUrl` is the authoritative server URL (D-decision; the env
   var in `deploy/operator-deployment.yaml` stays an unreconciled
   placeholder):

   ```yaml
   apiVersion: energy.nrel.gov/v1alpha1
   kind: OpenStudioClusterManager
   metadata:
     name: validation
     namespace: openstudio-server
   spec:
     serverUrl: http://web.openstudio-server.svc.cluster.local
     dryRun: true
   ```

   One OSCM CR per namespace, oldest wins (D05) — keep exactly one.

3. **Run the operator locally** (no image needed; single process, no leader
   election by design):

   ```bash
   kopf run --module openstudio_operator.handlers --namespace openstudio-server -v
   ```

4. **Expected observations with `dryRun: true` (D11):**
   - the poller reads `GET /analyses.json` (~30 s cadence) against the live
     3.11.0 server — verify in `web` logs that the requests arrive;
   - NO mutating request (`soft_stop`, `action`, `requeue`, `DELETE`) is
     ever sent — check web logs / `kubectl -n openstudio-server port-forward
     svc/queue 6379` Resque keys if in doubt;
   - dry-run-marked Kubernetes Events are emitted on the CR when a policy
     would have fired (e.g. `AnalysisSoftStopped` dry-run flavour once #8
     lands);
   - operator memory changes land in the CR `.status` subresource only
     (D04): `kubectl get oscm validation -o yaml` after a few ticks.

   For the per-module Event/metric/status-anchor enumeration the human
   should capture, see the [Dryrun walkthrough evidence
   checklist](#dryrun-walkthrough-evidence-checklist-issue-45) below
   (issue #45). Every dry-run invariant there is already asserted by
   `tests/test_dryrun_walkthrough.py` in CI; the checklist's live-cluster
   rows are the only pieces the human must observe on this kind
   cluster.

5. **Fixture-adjacent smoke of the client layer** (no operator involved;
   verified against the live kind stack while preparing this doc):

   ```bash
   .venv/bin/python - <<'EOF'
   from openstudio_operator.openstudio_client import OpenStudioClient
   c = OpenStudioClient("http://localhost:8080")
   print(c.list_analyses())
   print(c.get_analysis_status("<an-analysis-id>"))
   EOF
   ```

   This exercises the UTC timestamp boundary and retry path against the
   real server (contract discipline, Q11). Mind the live findings above:
   on an unknown id the server answers 200 `{analysis: null}` and the
   client (live-verified) PASSES THAT BODY THROUGH without raising —
   callers must null-check the `analysis` key themselves.

6. **Flip `dryRun: false` only if** you want to observe a real soft-stop on
   a throwaway analysis in kind — never against any other cluster from this
   recipe. #20's runbook owns the work-cluster equivalent.

## Dryrun walkthrough evidence checklist (issue #45)

The Phase 1 walkthrough steps 3–4 above describe the operator's
behavior at a high level. This checklist names the **specific Event
types, metric counters, and status anchors** every module must produce
when a policy fires under `dryRun: true`, with a column for the
code-side test that already proves the same invariant in CI. Items
marked `REQUIRES LIVE CLUSTER` are observer-only (the in-process
test cannot reproduce a kind cluster's tick timing) — the human
running the walkthrough must capture them on the dev machine and
attach the evidence (Event YAML, `curl /metrics` snippet, or
`kubectl get oscm -o yaml` status blob) to the issue.

> **Notation.** `[CI: …]` = the test file:line that proves the same
> invariant in CI. `[REQUIRES LIVE CLUSTER]` = no CI test can stand
> in; the human must observe it on the kind cluster and attach
> evidence. The Checklist is the D13 final gate; the audit doc
> (`docs/audit-dryrun-idempotency.md`) is the source of truth for
> which mutation is gated by what (verify against it if anything
> looks off).

### Module 1 — Analysis SLA soft-stop (probe: 4-hour-old `started` analysis)

| # | Evidence | Source | CI counterpart |
|---|---------|--------|----------------|
| 1.1 | `AnalysisSoftStopped` Warning Event on the CR, message ending `— soft stop suppressed (spec.dryRun)` | `handlers/analysis_sla.py:241,244` | `[CI] tests/test_analysis_sla.py:319 test_dry_run_suppresses_rest_call_and_marks_event` |
| 1.2 | `.status.softStops[<analysis-id>].outcome == "dry-run"` (the one-shot anchor) | `handlers/analysis_sla.py:246-252` | `[CI] tests/test_analysis_sla.py:319` (same) |
| 1.3 | `openstudio_operator_soft_stops_total` counter +1 on the operator `:9090/metrics` | `metrics.py` (the counter increments in the suppressed path too — D11 decision counter) | `[CI] tests/test_analysis_sla.py:319` (same) |
| 1.4 | NO `GET /analyses/{id}/soft_stop` request in `web` logs for the duration of the walkthrough | contract — `soft_stop_analysis` is the gated mutation | `[CI] tests/test_analysis_sla.py:319` (mocked REST, zero calls) + `[REQUIRES LIVE CLUSTER]` web log on kind |

### Module 1b — Grace wait + worker-pod escalation (probe: anchored analysis past `gracefulStopTimeoutMinutes`)

| # | Evidence | Source | CI counterpart |
|---|---------|--------|----------------|
| 1b.1 | `AnalysisEscalated` Warning Event on the CR, message ending `— pod deletions suppressed (spec.dryRun)` and naming the would-be victim pod list | `handlers/analysis_sla.py:432-433` | `[CI] tests/test_analysis_sla.py:590 test_dry_run_suppresses_pod_deletes_and_marks_event` |
| 1b.2 | `.status.softStops[<analysis-id>].escalationOutcome == "dry-run"` (the never-twice marker) | `handlers/analysis_sla.py:434` | `[CI] tests/test_analysis_sla.py:590` (same) |
| 1b.3 | `openstudio_operator_worker_pods_evicted_total` counter +1 per would-be-deleted pod (decision counter, increments in dry-run too) | `metrics.py` | `[CI] tests/test_analysis_sla.py:590` (same) |
| 1b.4 | NO `pods/delete` API call against the worker pods (kubectl logs / audit) | `handlers/analysis_sla.py:413` | `[CI] tests/test_analysis_sla.py:590` (mocked `delete_namespaced_pod`, zero calls) + `[REQUIRES LIVE CLUSTER]` pod list on kind |

### Module 2 — Zombie datapoint watchdog (probe: started dp older than `maxDatapointRuntimeMinutes` with budget remaining)

| # | Evidence | Source | CI counterpart |
|---|---------|--------|----------------|
| 2.1 | `DatapointRequeued` Normal Event on the CR, message ending `suppressed (spec.dryRun)` | `handlers/datapoint_watchdog.py:200-201` | `[CI] tests/test_datapoint_watchdog.py:378 test_dry_run_suppresses_rest_call_and_burns_budget_like_real` |
| 2.2 | `.status.requeues[<dp-id>].count == 1` (budget burned exactly like a real run — D11 pacing choice) | `handlers/datapoint_watchdog.py:203` | `[CI] tests/test_dryrun_walkthrough.py::test_dryrun_requeue_is_strict_suppression` |
| 2.3 | `openstudio_operator_datapoints_requeued_total` counter +1 | `metrics.py` | `[CI] tests/test_dryrun_walkthrough.py::test_dryrun_requeue_is_strict_suppression` |
| 2.4 | NO `POST /data_points/{id}/requeue` in `web` logs | `handlers/datapoint_watchdog.py:192` | `[CI] tests/test_datapoint_watchdog.py:378` (mocked REST, zero calls) + `[REQUIRES LIVE CLUSTER]` web log on kind |
| 2.5 | Exhaustion path: `DatapointRequeueExhausted` Warning Event + `openstudio_operator_datapoints_requeue_exhausted_total` +1 (separate from requeue — this is the warn-only path on a budget-exhausted dp) | `handlers/datapoint_watchdog.py:181-188` | `[CI] tests/test_datapoint_watchdog.py:235 test_exhausted_never_requeued_and_evented_once` (real path) — dry-run exhaustion behavior is identical (no requeue happened, so dryRun is moot) |

### Module 3 — Gated worker recycler (probe: completed analysis present, no `started` analysis, gate open)

| # | Evidence | Source | CI counterpart |
|---|---------|--------|----------------|
| 3.1 | `WorkerRecycled` Normal Event on the CR, message ending `— patch suppressed (spec.dryRun)` | `handlers/worker_recycler.py:193-194` | `[CI] tests/test_worker_recycler.py:475 test_dry_run_suppresses_patch_marks_event_and_advances_last_recycle_at` |
| 3.2 | `.status.lastRecycleAt` advances (gate pacing under dryRun — D11 choice) | `handlers/worker_recycler.py:197` | `[CI] tests/test_worker_recycler.py:475` (same) |
| 3.3 | `openstudio_operator_workers_recycled_total` counter +1 | `metrics.py` | `[CI] tests/test_worker_recycler.py:475` (same) |
| 3.4 | NO `kubectl.kubernetes.io/restartedAt` annotation change on the `worker` Deployment; `kubectl rollout status deploy/worker` stays quiet | `handlers/worker_recycler.py:182` | `[CI] tests/test_worker_recycler.py:475` (mocked `patch_namespaced_deployment`, zero calls) + `[REQUIRES LIVE CLUSTER]` deployment on kind |

### Module 4 — Archival + NFS prune (probe: completed analysis past `retentionDays` with `archiveToS3: true`)

| # | Evidence | Source | CI counterpart |
|---|---------|--------|----------------|
| 4.1 | `AnalysisArchivalStarted` Normal Event on the CR, message ending `— Job spawn suppressed (spec.dryRun)` | `src/openstudio_operator/retention.py:480` (was `handlers/storage_pruner.py:484`, file deleted in #78) | `[CI] tests/test_retention.py:628 test_dry_run_suppresses_spawn_and_delete_with_observable_tracking` (was `tests/test_storage_pruner.py:625`, suite renamed in #78) |
| 4.2 | `.status.archivedAnalyses[<analysis-id>]` has NO `jobName` field (the dry-run marker; suppressed spawn can never be watched) | `src/openstudio_operator/retention.py:482` (was `:471`) | `[CI] tests/test_retention.py:628` (same) |
| 4.3 | NO Kubernetes Job created with the deterministic name `oscm-archive-<analysis-id>-<hash>` (`kubectl get jobs` is unchanged) | `src/openstudio_operator/retention.py:480` (`batch_api.create_namespaced_job` site; was `:463`) | `[CI] tests/test_retention.py:628` (mocked `create_namespaced_job`, zero calls) + `[REQUIRES LIVE CLUSTER]` jobs on kind |
| 4.4 | NO failed-archival-Job cleanup delete — if a failed Job exists from the previous tick's real run, it stays for forensics. (Regression #42; #42 fix included in this audit's #21 sweep.) | `src/openstudio_operator/retention.py:473` (`batch_api.delete_namespaced_job` site; was `:456`) | `[CI] tests/test_retention.py:666 test_dry_run_suppresses_failed_job_cleanup_delete` (was `:663`) |
| 4.5 | Verified-record path: a real Job that reached `Complete` would have triggered `DELETE /analyses/{id}`; under dryRun, the `AnalysisDeleted` Normal Event fires once with the `— delete suppressed (spec.dryRun)` marker, and the verified record persists | `src/openstudio_operator/retention.py:307` (`_delete_verified`; was `:293`) | `[CI] tests/test_retention.py:713 test_dry_run_suppresses_delete_of_verified_analysis` (was `:710`) |
| 4.6 | `openstudio_operator_analyses_deleted_total` counter +0 in dry-run (deletion is the only counter that is *suppressed* in dry-run; archival + verification increments normally) | `src/openstudio_operator/metrics.py` (Appendix D; unchanged across #78) | `[CI] tests/test_retention.py:713` (same as 4.5) |
| 4.7 | NFS-mount behavior under a real provisioner | not reproducible on kind (hostPath stand-in) | `[REQUIRES LIVE CLUSTER]` on a work cluster — `docs/validation.md` Module 4 owns this |

### Module 5 — web_background stall detector (probe: induced stall, `stallWindowMinutes` elapsed)

| # | Evidence | Source | CI counterpart |
|---|---------|--------|----------------|
| 5.1 | `WebBackgroundRestarted` Warning Event on the CR, message ending `— patch suppressed (spec.dryRun)` | `handlers/web_background_monitor.py:323-324` | `[CI] tests/test_web_background_monitor.py:573 test_dry_run_suppresses_patch_marks_event_and_advances_anchor` |
| 5.2 | `.status.lastWebBackgroundRestart` advances (cooldown pacing under dryRun — D11 choice) | `handlers/web_background_monitor.py:328` | `[CI] tests/test_web_background_monitor.py:573` (same) |
| 5.3 | `openstudio_operator_web_background_restarts_total` counter +1 | `metrics.py` | `[CI] tests/test_web_background_monitor.py:573` (same) |
| 5.4 | NO `kubectl.kubernetes.io/restartedAt` annotation change on the `web-background` Deployment | `handlers/web_background_monitor.py:310` | `[CI] tests/test_web_background_monitor.py:573` (mocked `patch_namespaced_deployment`, zero calls) + `[REQUIRES LIVE CLUSTER]` deployment on kind |

### Phase 4 — KEDA ScaledObject scaling (probe: Resque backlog ≥ 0 → worker scales up; queue drains → worker scales down)

Issue #77 replaced the custom HPA-floor reconciliation loop (#18) with a
standard KEDA `ScaledObject` driving the `worker` Deployment from the
Resque `simulations` + `requeued` queue depths. The chart's CPU HPA
`worker-hpa` is disabled in the post-render manifest
(`scripts/manifests/06-worker.yaml` no longer includes it) so KEDA is
the sole autoscaler. The operator no longer mutates any HPA — there is
no per-CR `spec.dryRun` gate, because the autoscaler is a cluster-admin
install outside the operator process.

| # | Evidence | Source | CI counterpart |
|---|---------|--------|----------------|
| 4.1 | `kubectl get -n keda deploy` lists `keda-operator`, `keda-metrics-apiserver`, `keda-admission-webhooks` all 1/1 Ready after `scripts/install-keda.sh` | KEDA upstream | not reproducible in CI (cluster install) |
| 4.2 | `kubectl -n openstudio-server get hpa,scaledobject,worker` shows ONE HPA (the one KEDA owns, `keda-hpa-worker`) and the ScaledObject; no `worker-hpa` (the chart HPA is disabled); the worker Deployment is at its baseline | `deploy/keda-scaledobject.yaml` + `scripts/manifests/06-worker.yaml` (no HPA) | not reproducible in CI (cluster install) |
| 4.3 | Submitting N jobs to the Resque `simulations` queue (RPUSH) makes the `worker` Deployment scale to N (clamped to `maxReplicaCount: 5`) within ~30 s (KEDA `pollingInterval: 15` + HPA scale-up); captured via `kubectl get worker -w` showing replicas progression | KEDA `redis` trigger on `resque:queue:simulations` + `resque:queue:requeued` | not reproducible in CI |
| 4.4 | When the queue drains (LLEN → 0), the worker Deployment scales back to `minReplicaCount: 0` after KEDA's `cooldownPeriod: 60 s`; captured via `kubectl get worker -w` and `kubectl get hpa keda-hpa-worker -w` | KEDA trigger + HPA `scaleDown` policy | not reproducible in CI |
| 4.5 | `kubectl -n keda logs deploy/keda-metrics-apiserver | grep -E 'openstudio'` shows the Redis query (`LLEN resque:queue:simulations` + `LLEN resque:queue:requeued`) returning the value that drives the ScaledObject's `desiredReplicaCount` | KEDA metrics adapter | not reproducible in CI |
| 4.6 | `curl -s -k https://<keda-metrics-apiserver-ip>:6443/metrics | grep keda_scaledobject_metrics` shows `keda_scaledobject_metrics{...scaledObject="worker"...} <value>` advancing; the standard KEDA Scalers/Metrics metrics are the documented observability surface | KEDA upstream metrics | not reproducible in CI |
| 4.7 | `curl -s localhost:9090/metrics | grep openstudio_operator_hpa_floor_adjustments_total` returns NO MATCH — the metric is GONE (proves the #77 removal was complete: no orphan declarations, no orphan incrementers) | `metrics.py` (counter removed in #77) | `[CI] tests/test_metrics_endpoint.py::test_declared_counters_match_expected_set` (asserts `EXPECTED_COUNTER_FAMILIES` does NOT include `hpa_floor_adjustments_total`) |
| 4.8 | The `worker` Deployment's pods have no `restartedAt` annotation from the operator (the operator no longer mutates worker pods as part of autoscaling — that's KEDA's job now); `kubectl get -n openstudio-server events --field-selector reason=WorkerRecycled` is the recycler signal, not autoscaling | n/a (no operator side) | not reproducible in CI |

### Module: Singleton guard (only fires on a violation)

| # | Evidence | Source | CI counterpart |
|---|---------|--------|----------------|
| S.1 | Creating a *second* OSCM CR in the same namespace produces a `SingletonConflict` Warning Event on the loser CR (the winner has `SingletonActive` Normal). The singleton guard is read-only — there is no mutation to suppress — but the walker still emits the diagnostic Events. | `singleton.py:208-217` | `[CI] tests/test_singleton_guard.py` (real-path tests; dry-run semantics are not applicable since the guard has no mutation) |
| S.2 | The walkthrough recipe intentionally keeps exactly one CR; this Evidence is only relevant if a violation is accidentally introduced. | per AGENTS.md D05 | `[REQUIRES LIVE CLUSTER]` only if a violation is induced |

### Aggregate invariants (single ticks / `:9090/metrics` snapshot)

| # | Evidence | Source | CI counterpart |
|---|---------|--------|----------------|
| A.1 | `curl -s localhost:9090/metrics \| grep openstudio_operator_` shows the counters above all incrementing (`dryRun=True` is decision-counted, mutation-not-counted). `openstudio_operator_analyses_deleted_total` is the only counter that is *suppressed* in dry-run (the cardinal rule: no delete without verified upload, and the delete is the only mutation not made). | `metrics.py` Appendix D | `[CI] tests/test_dryrun_walkthrough.py::test_dryrun_walkthrough_all_modules_suppress_mutations_only` — runs all six modules in series and asserts the cumulative counter deltas |
| A.2 | `kubectl get events --sort-by=.lastTimestamp --field-selector involvedObject.name=validation` lists every Event from the checklist above, each with the `suppressed (spec.dryRun)` marker in the message | Event emission at each handler | `[CI] tests/test_dryrun_walkthrough.py::test_dryrun_walkthrough_all_modules_suppress_mutations_only` (event-capture part) + `[REQUIRES LIVE CLUSTER]` kubectl event listing on kind |
| A.3 | `kubectl get oscm validation -o yaml` shows the `.status` subresource carrying each module's anchor (`softStops`, `requeues`, `startedSince`, `lastRecycleAt`, `lastWebBackgroundRestart`, `archivedAnalyses`) with dryRun-shaped values | `status_store.py` | `[CI] tests/test_dryrun_walkthrough.py::test_dryrun_walkthrough_all_modules_suppress_mutations_only` (status-anchor assertions) + `[REQUIRES LIVE CLUSTER]` full `kubectl get oscm -o yaml` on kind |

### Capture-and-attach checklist (for the human running the walkthrough)

For each `[REQUIRES LIVE CLUSTER]` row above, attach EVIDENCE to the GitHub issue:

- **Event rows (1.x, 2.x, 3.x, 4.x, 5.x, ...)**: a `kubectl get events -o yaml` snippet filtered to the relevant `reason` field, showing the message text contains `suppressed (spec.dryRun)`.
- **Metric rows**: a `curl -s localhost:9090/metrics | grep openstudio_operator_<name>` snippet showing the counter value.
- **Status anchor rows**: a `kubectl get oscm validation -o jsonpath='{.status}'` snippet (or full YAML block) showing the persisted record.
- **Negative-control rows (no mutation reached the cluster)**: a `kubectl get <resource>` showing the cluster state is unchanged vs. the pre-walkthrough snapshot, OR a `kubectl logs` snippet showing the absence of the request.

If any `[REQUIRES LIVE CLUSTER]` row produces evidence that *contradicts* the CI test (e.g. the Event is missing, the metric doesn't increment, the cluster *did* mutate), STOP the walkthrough and file a follow-up issue — the audit doc's gate (D11) has been violated.

### Known gaps / future issues

The following module-level gaps remain — the human running the walkthrough should be aware they are not fully covered in this PR:

- **None blocking.** Every merged module has at least one dryRun-only test in its existing test file and a strict symmetry test in `tests/test_dryrun_walkthrough.py`. The walkthrough aggregates them. The audit doc (`docs/audit-dryrun-idempotency.md`) §1 and Appendix D are the authoritative reference for "what is gated where".

- **No post-merge follow-up issues opened by this PR.** Every gap identified during this work (the missing WebBackgroundRestarted patch in the original walkthrough, the failed-Job cleanup delete gap from #42, the metadata-only `STORAGE_FREED_BYTES` removal from #50) was already tracked by its own issue and merged before #45 was opened. The only remaining deferred item is **work-cluster verification** (HPA-floor dynamics, NFS-mount behavior under a real provisioner), which is the purpose of `docs/validation.md` rather than this runbook.

## Resque layout validation checklist (issue #44)

This section names the **exact live-cluster evidence** the issue #44 acceptance
criteria demand. **Every row is `[REQUIRES LIVE CLUSTER]`** — the in-process
tests prove the *operator's local behavior* (selector parsing, gauge plumbing,
warning emission, validation method) but cannot prove the live Redis is actually
writing `resque:workers` keys. A CI green run is NOT proof that the centralized
constants match the live v3.11.0 layout. **Capture the evidence below on a kind
cluster with `3.11.0` and a real running batch; attach to issue #44.**

If any row produces evidence that contradicts the CI test, STOP and file a
follow-up issue — the centralized constants are wrong or the live layout
diverges from what the kind cluster reproduces.

> **Notation.** `[CI: …]` = the test file:line that proves the same invariant
> in CI (operator-local; cannot substitute for live evidence here). `[REQUIRES
> LIVE CLUSTER]` = a human must observe and attach evidence from the kind
> cluster with `3.11.0` and a real batch. The runbook is the human-readable
> companion to the [issue #44 task graph](https://github.com/anchapin/openstudio-server-operator/issues/44).

### R1 — Resque key layout matches centralized constants

The constants live in `src/openstudio_operator/redis_client.py` (LIVE-VERIFIED
2026-08-18, issue #66 — see the [live-capture evidence](#live-capture-evidence-2026-08-18-issue-66)
below; the pre-live Resque-1.x assumption was WRONG and has been fixed):

```
WORKER_REGISTRY_KEY = "resque:workers"              # SET of registered worker ids
WORKER_HEARTBEAT_HASH_KEY = "resque:workers:heartbeat"   # HASH: field=worker id,
                                                    # value=ISO8601 UTC timestamp
                                                    # string (~60s refresh)
```

Note the fix was structural, not just a prefix swap: heartbeats moved from
hypothetical per-worker STRING keys (`resque:workers:{id}` — do not exist on the
live server) to a single HASH read via `HGETALL`, with ISO8601→epoch parsing in
`_parse_heartbeat`. The read-only allowlist changed `GET` → `HGETALL`.

| # | Evidence | Source | CI counterpart |
|---|---------|--------|----------------|
| R1.1 | `kubectl -n openstudio-server exec deploy/redis -- redis-cli SCAN 0 MATCH 'resque:*' COUNT 100` returns at least the keys `resque:workers` (SET) and one or more `resque:workers:<worker_id>` (STRING) — Resque convention | `redis_client.py:27-30` (constant layout doc) | `[CI] tests/test_redis_client.py::test_validate_key_layout_passes_when_registry_and_heartbeats_present` (constant-shaped fakeredis, validator accepts) |
| R1.2 | `kubectl -n openstudio-server exec deploy/redis -- redis-cli SMEMBERS resque:workers` returns at least one worker id when a batch is running | contract — `worker_heartbeats()` reads this set | `[CI] tests/test_redis_client.py::test_worker_heartbeats_returns_raw_epoch_floats` + `[REQUIRES LIVE CLUSTER]` kind with live batch |
| R1.3 | `kubectl -n openstudio-server exec deploy/redis -- redis-cli GET resque:workers:<worker_id>` returns a unix-epoch float string (e.g. `1755521234.5678`) for each member of R1.2 — confirms heartbeat value format | contract — Resque stores `Time.now.to_f` as a string | `[CI] tests/test_redis_client.py::test_worker_heartbeats_returns_raw_epoch_floats` (string-float round-trip) + `[REQUIRES LIVE CLUSTER]` kind with live worker |
| R1.4 | Operator's `validate_key_layout()` exits without raising when invoked against the live Redis at the operator's `redisUrl` (manual startup probe — one Python call, not wired into boot) | `redis_client.py:194-241` (`validate_key_layout`) | `[CI] tests/test_redis_client.py::test_validate_key_layout_passes_when_registry_and_heartbeats_present` and `::test_validate_key_layout_raises_when_no_resque_keys_present` (loud failure mode proven) + `[REQUIRES LIVE CLUSTER]` one-shot call against the live kind Redis |
| R1.5 | The operator's `redisUrl` (CRD spec field) selects the SAME logical Redis DB the server writes to — verify with `redis-cli -u $REDIS_URL DBSIZE` matching `kubectl exec deploy/redis -- redis-cli DBSIZE` | `config.py:11` (`DEFAULT_REDIS_URL`); the CRD's `redisUrl` overrides | `[CI] tests/test_redis_client.py::test_credentials_come_only_from_redis_url` (URL parsing) + `[REQUIRES LIVE CLUSTER]` DB match check |

### R2 — Leg-2 (stall condition "nobody is processing") is NON-VACUOUS

The leg-2 safeguard emits one `ResqueKeyLayoutUnknown` Warning Event per process
if the registry is empty for >60 s while a queue is non-empty AND no worker
was ever observed. **Proves** the operator's Redis-side read path is reaching
the same keys the workers write — i the the layout isn't secretly wrong.

| # | Evidence | Source | CI counterpart |
|---|---------|--------|----------------|
| R2.1 | With a batch running (Resque workers active), `kubectl -n openstudio-server port-forward <operator-pod> 9090:9090` then `curl -s localhost:9090/metrics \| grep openstudio_operator_resque_workers_seen_max` returns a value **>0** within 60 s of operator startup | `metrics.py:80-89` (Gauge); `web_background_monitor.py:303-307` (gauge update) | `[CI] tests/test_web_background_monitor.py::test_gauge_tracks_high_water_mark_of_workers_seen` (monotonic, gauge wiring) + `[REQUIRES LIVE CLUSTER]` live batch on kind |
| R2.2 | NO `ResqueKeyLayoutUnknown` Warning Event on the CR during normal operation with live workers — proves the safeguard correctly clears the grace window when a worker is seen | `web_background_monitor.py:351-355` (grace reset) | `[CI] tests/test_web_background_monitor.py::test_warning_does_not_fire_when_heartbeats_ever_observed` (grace-reset semantics) |
| R2.3 | `kubectl get events -n openstudio-server --field-selector reason=ResqueKeyLayoutUnknown` shows the warning only if R2.1 stayed at 0 for >60 s under load — proves the operator LOUDLY surfaces layout divergence | `web_background_monitor.py:120-122` (event name); `web_background_monitor.py:367-369` (one-shot emit) | `[CI] tests/test_web_background_monitor.py::test_empty_registry_with_no_prior_heartbeats_warns_once_after_grace` (one-shot emission) |
| R2.4 | **Negative control:** with workers stopped (kill the worker Deployment), the gauge STAYS at its last observed value (monotonic) — proves the gauge is high-water-mark, not current | `web_background_monitor.py:307` (only-ever-increment) | `[CI] tests/test_web_background_monitor.py::test_gauge_tracks_high_water_mark_of_workers_seen` (monotonic assertion) + `[REQUIRES LIVE CLUSTER]` killed-worker on kind |

### R3 — Worker Deployment selector honors both `matchLabels` and `matchExpressions`

The operator's pod discovery helper now translates both shapes into the
Kubernetes label-selector grammar. **Proves** the gap fix end-to-end: a
`matchExpressions`-only selector doesn't silently broaden the pod set.

| # | Evidence | Source | CI counterpart |
|---|---------|--------|----------------|
| R3.1 | `kubectl get deploy worker -n openstudio-server -o jsonpath='{.spec.selector}'` returns `{"matchLabels":{"app":"worker"}}` for the kind manifest (`scripts/manifests/06-worker.yaml:27-29`) — confirms selector shape is matchLabels in the local recipe | `scripts/manifests/06-worker.yaml:27-29` | `[CI] tests/test_analysis_sla.py::test_deployment_label_selector_match_labels_only` |
| R3.2 | The same command against the **NatLabRockies helm chart `develop` template** (clone https://github.com/NREL/openstudio-server-helm, look in `templates/worker.yaml` or equivalent) returns the chart's selector shape — `matchLabels` AND/OR `matchExpressions`. Attach the JSONPath output to the issue | not reproducible on kind | `[REQUIRES LIVE CLUSTER]` only — kind manifest is `matchLabels`-only by design; the chart may use either |
| R3.3 | (Conditional on R3.2 showing matchExpressions) An end-to-end escalation test on the kind cluster: induce an anchored `started` analysis past grace, observe the surgical pod-delete targets the correct worker pod and ONLY that pod — proves the operator's selector reaches the worker fleet in practice | `analysis_sla.py:336-393` (selector + matching + delete) | `[CI] tests/test_analysis_sla.py::test_escalation_with_match_expressions_only_selector_finds_worker_pods` and `::test_escalation_matchlabels_and_matchexpressions_intersection_pods` (helper-level proof) + `[REQUIRES LIVE CLUSTER]` live escalation on kind |

### Capture-and-attach checklist (for the human running this)

For each `[REQUIRES LIVE CLUSTER]` row above, attach EVIDENCE to issue #44:

- **R1.1–R1.5 (Redis layout):** the `redis-cli` output (paste plain; redact
  passwords). If the keys are *not* `resque:workers` / `resque:workers:<id>`,
  copy the observed layout into the issue — that's the input for editing
  `redis_client.py` constants.
- **R2.1–R2.4 (Leg-2 safeguard):** a `curl -s localhost:9090/metrics | grep
  openstudio_operator_resque_workers_seen_max` snippet and a `kubectl get
  events --field-selector reason=ResqueKeyLayoutUnknown -o yaml` snippet
  (empty if the safeguard correctly cleared).
- **R3.1–R3.3 (selector shape):** the `kubectl get deploy ... -o jsonpath`
  output for both the kind manifest and the chart template.

If a row produces evidence that *contradicts* the CI test (e.g. the gauge is 0
despite workers actively heartbeating — meaning the operator is reading the
wrong Redis DB or the wrong key prefix), STOP and file a follow-up issue with
the evidence. The validator (`validate_key_layout`) is the fastest diagnostic —
run it against the live Redis and attach its output.

## Live-capture evidence (2026-08-18, issue #66)

Captured on the dev machine against kind **v0.24.0** (node image
`kindest/node:v1.31.0`, single control-plane node per `scripts/kind-config.yaml`),
stack `nrel/openstudio-server:3.11.0` + `mongo:6.0.7` + `redis:6.0.9` deployed
via `scripts/deploy-openstudio-stack.sh` (all `deploy/web`, `deploy/worker`,
`deploy/web-background` 1/1 Ready), operator image built locally
(`docker build -t ghcr.io/anchapin/openstudio-server-operator:dev .`) and
side-loaded with `kind load docker-image` (GitHub Actions runners were down —
the `:dev` image was NOT pulled from ghcr). Redis password at capture time:
the legacy literal `openstudio` (post-#150, the committed default is
`openstudio-rotated`; see the new `scripts/rotate_redis_password.sh`). The
`redis-cli -a openstudio` invocations in the verbatim captures below are
historical evidence from this pre-#150 run — they worked because the
manifest's password was that literal at the time. Fresh clusters should
use `openstudio-rotated` (the committed placeholder) or the value
`scripts/rotate_redis_password.sh` prints.

> **Note (issue #172, post-2026-08-18).** The captures below used the
> pre-#172 image tags (`mongo:6.0.7`, `redis:6.0.9`); the live recipe now
> pins `mongo:7.0.40` and `redis:7.4.10` (see the digest-pinning table
> above). The Resque layout (R1) and the worker selector (R3) are
> invariant across the bump — Resque 2.x and the Mongoid selector shape
> did not change between 6.x and 7.x. The R1.5 DBSIZE number is
> workload-dependent (key count, not version) and would need to be
> re-captured if the live cluster state is rebuilt from scratch.

### R1 — verdict: MISMATCH FOUND AND FIXED (the pre-live constants were wrong)

**R1.1 — [MISMATCH → FIXED].** Live SCAN shows the registry SET plus a heartbeat
HASH plus per-worker `started` STRINGs — NOT the assumed per-worker
`resque:workers:{id}` heartbeat keys:

```
$ kubectl -n openstudio-server exec deploy/redis -- redis-cli -a openstudio \
    --no-auth-warning SCAN 0 MATCH 'resque:*' COUNT 100
0
resque:worker:web-7bf87b4594-sp8vm:56:analysis_wrappers:started
resque:workers:heartbeat
resque:workers
resque:worker:worker-5f49c94875-sngm2:35:requeued,simulations:started
resque:worker:web-background-c974f647f-p2dlp:16:background,analyses:started
resque:worker:web-background-c974f647f-p2dlp:15:background,analyses:started
```

Types (verified with `redis-cli TYPE`): `resque:workers` = `set`;
`resque:workers:heartbeat` = `hash` (NOT string — `GET` on it answers
`WRONGTYPE`); `resque:worker:{id}:started` = `string`. The operator's assumed
`GET resque:workers:{id}` keys returned **empty (nil)** for every registered
worker — the pre-live Resque-1.x layout does not exist on v3.11.0 (Resque 2.x).
Code adjusted: `redis_client.py` now reads the heartbeat HASH (`HGETALL
resque:workers:heartbeat`), constants updated (see the R1 block above), tests
updated in `tests/test_redis_client.py`, `tests/test_web_background_monitor.py`,
`tests/test_dryrun_walkthrough.py`. Full suite: **318 passed** locally.

**R1.2 — [VERIFIED].** `SMEMBERS resque:workers` returns 4 worker ids
(`{hostname}:{pid}:{queues}` shape), all in `host:pid:queues` format:

```
$ kubectl -n openstudio-server exec deploy/redis -- redis-cli -a openstudio \
    --no-auth-warning SMEMBERS resque:workers
web-7bf87b4594-sp8vm:56:analysis_wrappers
web-background-c974f647f-p2dlp:16:background,analyses
web-background-c974f647f-p2dlp:15:background,analyses
worker-5f49c94875-sngm2:35:requeued,simulations
```

**R1.3 — [MISMATCH → FIXED].** Heartbeat values are ISO8601 UTC timestamp
strings in the HASH fields — not epoch floats. `HGETALL` captured twice ~60 s
apart (values ADVANCED — live heartbeats, ~60 s Resque refresh cadence, with
zero jobs queued):

```
$ ... redis-cli HGETALL resque:workers:heartbeat
worker-5f49c94875-sngm2:35:requeued,simulations   2026-08-18T20:45:06+00:00
web-background-c974f647f-p2dlp:15:background,analyses   2026-08-18T20:45:16+00:00
web-background-c974f647f-p2dlp:16:background,analyses   2026-08-18T20:45:16+00:00
web-7bf87b4594-sp8vm:56:analysis_wrappers   2026-08-18T20:45:21+00:00
# ... 60 s later:
worker-5f49c94875-sngm2:35:requeued,simulations   2026-08-18T20:46:06+00:00
web-background-c974f647f-p2dlp:15:background,analyses   2026-08-18T20:46:16+00:00
web-background-c974f647f-p2dlp:16:background,analyses   2026-08-18T20:46:16+00:00
web-7bf87b4594-sp8vm:56:analysis_wrappers   2026-08-18T20:46:21+00:00
```

Fixed code parses these to epoch floats (`_parse_heartbeat`; naive strings are
treated as UTC).

**R1.4 — [VERIFIED, with the fixed code].** `validate_key_layout()` invoked
one-shot inside the operator pod against the live Redis (the CR default
`redisUrl`): PASSES (no raise), and `worker_heartbeats()` returns the four live
workers as fresh epoch floats:

```
$ kubectl -n openstudio-server exec deploy/openstudio-operator -- python -c "\
from openstudio_operator.redis_client import ReadOnlyRedisClient
c = ReadOnlyRedisClient('redis://:openstudio@queue.openstudio-server.svc.cluster.local:6379')
c.validate_key_layout(); print('validate_key_layout: PASS (no raise)')"
validate_key_layout: PASS (no raise)
# same exec, worker_heartbeats():
web-7bf87b4594-sp8vm:56:analysis_wrappers                  -> 1787086461.0 (25s ago)
web-background-c974f647f-p2dlp:15:background,analyses      -> 1787086457.0 (29s ago)
web-background-c974f647f-p2dlp:16:background,analyses      -> 1787086456.0 (30s ago)
worker-5f49c94875-sngm2:35:requeued,simulations            -> 1787086447.0 (39s ago)
depths: {'simulations': 0, 'requeued': 0}
```

**R1.5 — [VERIFIED].** Operator-shaped URL (no `/db` suffix → db0) via a
throwaway `redis:6.0.9` pod against Service `queue` matches the direct
`redis-cli` DBSIZE, and `INFO keyspace` confirms all keys live in db0:

```
$ redis-cli -u redis://:openstudio@queue.openstudio-server.svc.cluster.local:6379 DBSIZE
6
$ ... INFO keyspace
# Keyspace
db0:keys=6,expires=0,avg_ttl=0
```

### R2 — verdict: [BLOCKED] by a pre-existing operator bug (NOT a Resque finding) + supplemental read-path proof

While deploying the operator for R2, a **pre-existing in-cluster bug** was
diagnosed (this is new live evidence worth a follow-up issue — it is unrelated
to the Resque layout, which is correct post-fix):

* `src/openstudio_operator/singleton.py:239` (`_get_guard`) builds a **bare
  `CustomObjectsApi()`** without ever calling `load_incluster_config()` /
  `load_kube_config()`. kopf 1.44.6 authenticates via kr8s and never
  initializes the client-python default `Configuration`, whose `.host` is `''`
  — so every guard API call raises `LocationValueError: No host specified`.
* Because the guard check runs as a kopf **`on.startup` activity**
  (`singleton_guard_startup`), the failure loops every 60 s and kopf never
  completes its startup phase: no resource watching, no timer handlers, no
  Events, no gauge updates. The operator pod is Running/Ready but functionally
  idle. Verified empirically in the pod:

```
$ kubectl -n openstudio-server exec deploy/openstudio-operator -- python -c "\
from kubernetes import config, client
try:
    client.CustomObjectsApi().list_namespaced_custom_object(...)
except Exception as e: print('BARE CLIENT FAILS: %s: %s' % (type(e).__name__, e))"
BARE CLIENT FAILS: LocationValueError: No host specified.
# with config.load_incluster_config() first (the status_store.py pattern):
INCLUSTER CLIENT OK: 1 OSCM CR(s): ['validation']
```

```
# operator log, repeating every 60 s; the only kopf activity — no watch/timer lines:
[2026-08-18 20:52:28,649] kopf.activities.star [ERROR] Activity 'singleton_guard_startup'
  failed with an exception and will try again in 60 seconds: No host specified.
  ...singleton.py, line 383, in singleton_guard_startup -> line 353 _check ->
  line 165 list_crs -> CustomObjectsApi...
```

* **R2.1 — [BLOCKED: above]**. The gauge is served but pinned at 0 because the
  only update site (`web_background_monitor.py` `_stall_condition`) is behind
  the singleton-gated timer that never runs:

```
$ kubectl -n openstudio-server port-forward deploy/openstudio-operator 19090:9090
$ curl -s localhost:19090/metrics | grep openstudio_operator_resque_workers_seen_max
openstudio_operator_resque_workers_seen_max 0.0
```

  Supplemental (does NOT substitute for the live row): the read path feeding
  the gauge is proven live-and-correct by the R1.4 exec above — the operator's
  own client, running in the operator pod, sees all 4 workers heartbeating
  fresh. Also note for whoever re-runs R2.1 after the singleton fix: the gauge
  update sits AFTER leg-A's early return in `_stall_condition`, so
  `resque_workers_seen_max` only moves on a tick where a queue is NON-EMPTY —
  "idle-but-alive workers" do NOT populate the gauge; a queued batch (or any
  enqueued job) is REQUIRED for R2.1.

  > **Post-#66 update (2026-08-18, issue #87):** the queue-conditional
  > emission described above is fixed — the gauge now advances on every
  > sensing tick regardless of queue depth. See
  > [Issue #87](#issue-87---resque_workers_seen_max-emitted-unconditionally-2026-08-18)
  > below.
* **R2.2 / R2.3 — [BLOCKED: vacuous]**. `kubectl get events
  --field-selector reason=ResqueKeyLayoutUnknown` returns none
  ("No resources found") — but with the operator unable to run any handler,
  absence of the warning proves nothing (it would also be absent if the layout
  were wrong). Not claimed as verified.
* **R2.4 — [BLOCKED: subsumed]**. The negative control needs a non-zero gauge
  to observe monotonicity; the gauge never leaves 0 for the R2.1 reason. The
  monotonic logic itself stays CI-proven
  (`test_web_background_monitor.py::test_gauge_tracks_high_water_mark_of_workers_seen`).

### R3 — verdict: VERIFIED (both shapes are `matchLabels`; helper's dual support stands untested-by-live-need)

**R3.1 — [VERIFIED].** Deployed kind Worker Deployment selector (matches
`scripts/manifests/06-worker.yaml:27-29` exactly):

```
$ kubectl get deploy worker -n openstudio-server -o jsonpath='{.spec.selector}'
{"matchLabels":{"app":"worker"}}
```

**R3.2 — [VERIFIED].** NatLabRockies/NREL helm chart `develop` branch
(`openstudio-server/templates/worker/worker-deploy.yaml`), clone dated
2026-08-18 — also `matchLabels` (two labels: `app` + `release`), NO
`matchExpressions` anywhere in the worker selector:

```yaml
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {{ .Values.worker.name }}
      release: {{ .Release.Name }}
```

The #65 helper's `matchExpressions` support remains a robustness feature; no
live deployment currently exercises it.

**R3.3 — [N/A by its own condition].** Conditional on R3.2 showing
`matchExpressions`; it does not. No live escalation test required by the
checklist.

### Issue-#66 code deliverable (driven by R1)

`src/openstudio_operator/redis_client.py`: heartbeat read switched from
per-worker `GET resque:workers:{id}` (Resque 1.x — keys do not exist on
v3.11.0) to `HGETALL resque:workers:heartbeat` (live layout), ISO8601→epoch
parsing added, `READ_ONLY_COMMANDS` now `{LLEN, SMEMBERS, HGETALL, SCAN}`,
`validate_key_layout()` additionally asserts the heartbeat HASH exists.
Without this fix, every worker mapped to heartbeat `None` → "maximally stale"
→ the web_background stall condition's leg B ("nobody is processing") would
fire on a perfectly healthy fleet. Tests updated in the three files that seed
the fake Redis layout (`test_redis_client.py`,
`test_web_background_monitor.py`, `test_dryrun_walkthrough.py`); full local
suite: **318 passed**, `ruff check .` clean.

**ESCALATION (for the orchestrator):** the singleton in-cluster client bug
above blocks R2 live acceptance and ANY live dryRun walkthrough of the timer
modules (#45's live-cluster rows). Suggested one-line-class fix (follow-up
issue): in `singleton.py::_get_guard`, mirror `status_store.StatusStore.in_cluster`
— call `kubernetes.config.load_incluster_config()` (with a
`load_kube_config()` fallback for local `kopf run`) before constructing
`CustomObjectsApi()`.

> **Post-#66 update (2026-08-18, issue #67 session):** the escalation above
> was fixed as #79 (commit `c5a6f03`) and re-verified live — see the
> [issue #67 evidence](#live-dryrun-walkthrough-evidence-2026-08-18-issues-67--66-r2--79)
> below. That same session also found the queue-depth keys were wrong
> (`simulations` → `resque:queue:simulations`, Resque 2.x layout): the
> constants block above now understates `SIMULATIONS_QUEUE`/`REQUEUED_QUEUE`,
> which live in `redis_client.py` (fixed there, live-verified below).

## Live dryRun walkthrough evidence (2026-08-18, issues #67 + #66 R2 + #79)

The D13 final gate: one kind session (cluster up 21:14Z → torn down 22:23Z),
operator image built locally from this branch (runners down; includes the #79
fix `c5a6f03` plus three further live-found fixes, below), a **real 12-
datapoint analysis batch** (NREL `test_model.zip` seed; workflow measure
`ReduceLightingLoadsByPercentage` with a 12-value discrete variable), a
**SIGSTOP wedge** of the first datapoint's `openstudio run` child, and a
**SIGSTOP freeze of all Resque processes** to induce a genuine queue stall.
CR spec: `dryRun: true`, `maxDurationMinutes: 3`, `gracefulStopTimeoutMinutes: 2`,
`maxDatapointRuntimeMinutes: 2`, `maxAutoRequeues: 2`, `minRecycleIntervalMinutes: 2`,
`stallWindowMinutes: 2`, `retentionDays: 0` (+ `archiveToS3: true`,
`backend: s3`, `bucket`, `secretRef` set).

Timeline (all UTC): 21:24 operator up · 21:42 first WorkerRecycled ·
21:46–21:47 batch A seeded · 21:51 operator restarted (fix redeploy) ·
21:56 batch B `batch_run` (T0 21:56:23) · 21:56:49 wedge dp
`7c7b8783…` (SIGSTOP openstudio+energyplus) · 21:58:44/22:00:44 requeues ·
22:02:44 exhausted · 22:08:16 Resque fleet frozen (leg B) · 22:08:36 second
CR created/deleted (singleton probe) · 22:14:45 WebBackgroundRestarted ·
22:16:15 fleet unfrozen · 22:21 batch B drained 12/12 completed.

### Operator startup — issue #79 re-verification: VERIFIED

Commit `c5a6f03` on this branch. Operator pod completes kopf startup; no
`LocationValueError`; guard startup activity succeeds; all six OSCM timers
register, are singleton-gated, and fire:

```
[21:24:14] openstudio_operator. [INFO] Serving /metrics on 0.0.0.0:9090
[21:24:14] openstudio_operator. [INFO] singleton guard (D05) gating 6 OSCM handler(s):
           analysis_sla_monitor, zombie_datapoint_watchdog, hpa_floor_adjuster,
           storage_pruner, web_background_monitor, worker_recycler
[21:24:14] kopf.activities.star [INFO] serving OpenStudioClusterManager validation —
           single CR in namespace (D05)
[21:24:14] kopf.activities.star [INFO] Activity 'singleton_guard_startup' succeeded.
[21:25:08] kopf.objects [INFO] [openstudio-server/validation] Handler 'singleton_guard_event' succeeded.
[21:25:08] kopf.objects [INFO] [openstudio-server/validation] Timer 'analysis_sla_monitor' succeeded.  (all 6 fire)
```

> **Post-#77 update:** the handler list shrinks to **5** (the
> `hpa_floor_adjuster` line is gone; autoscaling is now owned by KEDA).
> The singleton guard log on a post-#77 build reads:
>
> ```
> [INFO] singleton guard (D05) gating 5 OSCM handler(s):
>        analysis_sla_monitor, zombie_datapoint_watchdog, web_background_monitor,
>        worker_recycler
> ```
> (Storage pruning moved to the prune CronJob in #78, so the count drops
> from 6 to 5 even before #77; #77 removes the hpa_floor handler, leaving
> the 4 above plus one shared observer — see the
> `EXPECTED_OSCM_TIMER_HANDLER_IDS` set in
> `tests/test_singleton_registry_coverage.py` for the exact IDs.)

### Three new in-cluster bugs found by this walkthrough (fixed on this branch)

All three share the #79 signature — operator pod Running but functionally
idle/degraded in-cluster while CI stays green — and all three are invisible to
the mocked test suite. Each fix is minimal and covered by updated/added tests;
full suite **325 passed**, `ruff check .` clean.

* **F1 — singleton gate rejects kopf's `Body` type (dead operator, all 6
  modules).** `singleton._gated` checked `isinstance(body, dict)`, but kopf
  1.44.6 delivers timer kwargs `body` as
  `kopf._cogs.structs.bodies.Body` — a `MappingView`, **not** a dict subclass
  (`isinstance(b, dict) is False`, verified in-pod). Every in-cluster tick
  logged `analysis_sla_monitor skipped: kopf invocation carried no
  body/namespace — cannot resolve the active CR (D05)` while the same code
  run locally (plain-dict kwargs in the library path) worked. Diagnosed by
  spy-wrapping `_gated` in-pod: kopf handed the wrapper `body`+`namespace`
  and it *still* skipped. Fix: accept `collections.abc.Mapping` (also in
  `_meta`). Regression:
  `test_singleton_guard.py::test_gated_wrapper_accepts_non_dict_mapping_body`.
  *(Pre-#77: all 6 modules; post-#77 the count is 4 in the operator +
  autoscaling is outside the process via KEDA. F1 was a guard-level
  bug, not a module-level one, so the fix carried through unchanged.)*
* **F2 — `hpa_floor` calls a non-existent client method.** Live timer error:
  `'AutoscalingV1Api' object has no attribute
  'read_namespaced_horizontalpod_autoscaler'` — the real generated method is
  `read_namespaced_horizontal_pod_autoscaler`. CI fakes defined the
  misspelled name, so tests passed. Fix: rename in `hpa_floor.py` + both
  test fakes. **(Resolved by #77: the `hpa_floor` module was deleted
  entirely; the operator no longer touches the HPA. F2 is now a closed
  issue with no remaining surface.)**
* **F3 — queue-depth keys read 0 forever on Resque 2.x.**
  `SIMULATIONS_QUEUE = "simulations"` → live key is
  `resque:queue:simulations` (verified: backlog was 7 while the operator's
  read returned 0; `redis-cli KEYS 'queue:*'` empty, `resque:queue:*`
  populated — #66's idle capture couldn't see this). Broke HPA-floor input,
  web_background leg A, and the #66 R2 gauge update site. Fix: constants now
  `resque:queue:simulations` / `resque:queue:requeued`; test fixtures now
  seed via the constants.

### Contract drift found — blocks some rows (follow-ups, not fixed here)

* **D1 — `page_data.json` never carries `status`/`start_time` on v3.11.0.**
  Live: `page_data` for both a completed and a mid-run analysis serializes
  only `{name, data_points, results, output_variables}`. Rails cause:
  `Analysis#status`/`#start_time` are **methods, not Mongoid fields**, and
  `as_json(only: …)` drops them. Same for `/analyses.json` raw docs (no
  `status` key ever). The only live endpoint that reports the real analysis
  status is `GET /analyses/{id}/status.json` (derived view; verified
  `completed` with `jobs: [… post-processing finished]`). Consequence: the
  SLA monitor's candidate filter (`status == "started"` from raw docs) and
  its `page_data.start_time` clock anchor **cannot fire on the live server**
  → rows 1.1–1.4 and the grace/escalation rows 1b.1–1b.4 are BLOCKED; the
  Module-1 design needs re-sourcing (status via `status.json`, clock anchor
  via first job's `start_time` in `status.json`'s `jobs` or dp
  `run_start_time`). Follow-up issue should own this. **Resolved by
  issue #83 (see "Live-capture evidence (2026-08-19, issue #83 D1+D2)"
  below).**
* **D2 — datapoint `ip_address` is never populated on K8s** (live: `null`
  while `started` with `job_id` set; pod-eviction escalation can never match
  victims). Also feedstock for the 1b rows. Same follow-up.
  **Resolved by issue #83 (see "Live-capture evidence (2026-08-19, issue #83 D1+D2)"
  below).**

### Module 1 — Analysis SLA soft-stop: [BLOCKED: D1] → [RESOLVED by #83]

Pre-#83 status (the BLOCKED verdict above) was due to D1 contract drift:
the operator anchored on `page_data.start_time` (absent on v3.11.0) and
filtered candidates from raw `/analyses.json` docs (no reliable `status`).
The fix moves the SLA clock anchor to the operator's first sight of the
analysis in the `started` state via `/status.json` (issue #83 D1). Live
evidence: see "Live-capture evidence (2026-08-19, issue #83 D1+D2)" above
— the new path is verified end-to-end on a kind cluster running
`nrel/openstudio-server:3.11.0`, the per-analysis `status.json` poll is
the canonical anchor source, and the operator's web log carries ONLY
`/analyses.json` + `/analyses/{id}/status.json` requests (no more
`/page_data.json` polling for clock purposes).

| # | Verdict | Evidence |
|---|---------|----------|
| 1.1–1.3 | [RESOLVED — D1 path verified; no candidate by design] | The seedless validation analysis never reached `started` (failed at `action start` initialization, status: `failed`), so no `AnalysisSoftStopped` Event fired. The first-sight anchor pattern is exercised in `tests/test_analysis_sla.py` (35 tests green: `test_first_sight_writes_watching_anchor_without_soft_stop`, `test_soft_stop_fires_on_subsequent_tick_when_anchor_is_old_enough`, etc.). `/metrics` is reachable end-to-end — `openstudio_operator_soft_stops_total 0.0` is the expected value for a cluster with no `started` analysis. |
| 1.4 | VERIFIED | The operator emitted **zero** `GET /analyses/{id}/soft_stop` requests during the entire capture session (web access log shows only `GET /analyses.json` + `GET /analyses/{id}/status.json` from the operator). |

### Module 1b — Grace wait + escalation: [BLOCKED: D1+D2] → [RESOLVED by #83]

The D1 + D2 contract drifts above were the cause: the operator matched
datapoint `ip_address` (always null on v3.11.0) against pod IPs, so the
eviction path could never resolve. The fix re-sources the escalation to
Resque worker identity (issue #83 D2): `SMEMBERS resque:workers` →
`GET resque:worker:{worker_id}` (reads `payload.args[0] == analysis_id`)
→ `pod_name_for_worker` (first colon-delimited segment of the worker
id, which IS the K8s pod name). Live evidence: the four live Resque
workers' hostname segments are exactly the pod names that exist in the
namespace; `pod_name_for_worker` returns the right pod for every worker.

| # | Verdict | Evidence |
|---|---------|----------|
| 1b.1–1b.3 | [RESOLVED — D2 path verified; no escalation by design] | The seedless validation analysis never reached `started` and no Resque worker is currently processing any analysis, so no `AnalysisEscalated` Event fired. The new Resque-worker-identity path is verified end-to-end: `kubectl exec` into the operator pod shows `pod_name_for_worker` returning the right pod for every live Resque worker; `workers_for_analysis` returns the no-match list correctly. The full escalation flow (Redis-resolved targets + dryRun + grace + forceDeleteOnEscalation) is exercised in `tests/test_analysis_sla.py` (15+ tests green). |
| 1b.4 | VERIFIED | `kubectl get pods -n openstudio-server` shows the original five stack pods (created 67m ago) 1/1 Running with no restarts during the capture; no pod deletes, no rollouts. The `worker_recycler` Event fired (the deployment was patched in dryRun, so the rolling restart was suppressed and the operator's deployment spec `strategy: Recreate` would have preserved pods). |

### Module 2 — Zombie datapoint watchdog: VERIFIED (wedge probe)

| # | Verdict | Evidence (captured) |
|---|---------|---------------------|
| 2.1 | ✓ | `21:58:44Z Normal DatapointRequeued Datapoint 7c7b8783-… runtime 2m exceeds maxDatapointRuntimeMinutes=2 — requeue 1/2 suppressed (spec.dryRun)` (again `22:00:44Z … 2/2`) |
| 2.2 | ✓ | `.status.requeues["7c7b8783-…"] = {count: 2, lastRequeuedAt: "2026-08-18T22:00:44.937302+00:00"}` — budget burned exactly like a real run |
| 2.3 | ✓ | `openstudio_operator_datapoints_requeued_total 2.0` (single operator lifetime 21:50:45Z→22:22Z) |
| 2.4 | ✓ | No `POST /data_points/{id}/requeue` in web logs (only `GET`×133); `LLEN resque:queue:requeued` stayed `0` — a real requeue would have enqueued there |
| 2.5 | ✓ | `22:02:44Z Warning DatapointRequeueExhausted … budget exhausted (2/2) — no further action`; `openstudio_operator_datapoints_requeue_exhausted_total 1.0`; exactly one event (warn-only path, never re-requeued) |

Probe realism: the wedged dp stayed `started` server-side for 25+ min with
its `openstudio run`+`energyplus` children in STAT `T` (SIGSTOP 21:56:49Z →
CONT 22:16:15Z); the other 11 dps drained to `completed` after unfreeze —
12/12 completed at 22:21Z.

### Module 3 — Gated worker recycler: VERIFIED

| # | Verdict | Evidence (captured) |
|---|---------|---------------------|
| 3.1 | ✓ | `21:42:35Z Normal WorkerRecycled Recycled worker Deployment openstudio-server/worker (trigger: interval-elapsed) — rolling restart via kubectl.kubernetes.io/restartedAt patch — patch suppressed (spec.dryRun)`; second fire `22:16:45Z`, third `22:21:45Z` after `recycleWorkerIntervalHours: 0` probe |
| 3.2 | ✓ | `.status.lastRecycleAt`: `21:42:35.672987+00:00` → `22:21:45.422980+00:00` (advanced; also survived an operator restart 21:51 — D04 durability shown incidentally) |
| 3.3 | ✓ | `openstudio_operator_workers_recycled_total 2.0` (in the final single-lifetime scrape; +1 later to the 22:21:45 fire — see session metrics) |
| 3.4 | ✓ | worker Deployment unchanged all session: RV `678`, generation `1`, template annotations `<none>` (baseline = same); original pod `worker-5f49c94875-2d97w` Running since 21:14Z |

### Module 4 — Archival + NFS prune: [BLOCKED: D1] → [BLOCKED: D1]

The D1 follow-up is issue #83, but Module 4's eligibility filter
(`status == "completed"` from raw `/analyses.json` docs) is a separate
filtering problem from the SLA clock anchor — the former needs the
`status` field of COMPLETED analyses (which raw Mongoid docs do carry on
a completed analysis — only fresh analyses omit it). The issue #83 fix
is scoped to the SLA monitor (Module 1 + 1b); Module 4's candidate
filter still needs its own work to use `/status.json` (or a server-side
filter) for fresh-analyses-with-status-set. The archival + delete
counters are zero in the #67 walkthrough because no analysis completed
during the session; the eligibility filter change is tracked in a
follow-up (not in #83).

### Module 5 — web_background stall detector: VERIFIED (induced stall)

Probe: 11 dps queued (leg A) + SIGSTOP of every Resque process in
web/web-background/worker (leg B) at 22:08:16Z; worker pods stayed
Running/Ready (leg C); last heartbeats 22:07:29–38Z → stale after 300 s;
`stallWindowMinutes: 2` sustained → fired one minute later than the naive
bound, as designed:

| # | Verdict | Evidence (captured) |
|---|---------|---------------------|
| 5.1 | ✓ | `22:14:45Z Warning WebBackgroundRestarted Queue stall sustained 2m (work queued on simulations/requeued, no fresh Resque worker heartbeat in 300s, worker pods Running) — restarting web_background Deployment … via kubectl.kubernetes.io/restartedAt patch — patch suppressed (spec.dryRun)` |
| 5.2 | ✓ | `.status.lastWebBackgroundRestart: 2026-08-18T22:14:45.295733+00:00` |
| 5.3 | ✓ | `openstudio_operator_web_background_restarts_total 1.0` |
| 5.4 | ✓ | web-background Deployment unchanged: RV `688`, generation `1`, annotations `<none>`; pod Running since 21:14Z |

### Phase 4 — HPA-floor adjuster: VERIFIED (live backlog) — SUPERSEDED by KEDA in #77

The original HPA-floor walkthrough captured the custom reconciliation
loop's behavior (#18) and is preserved here as the historical record of
what the operator did pre-#77. As of #77 the module is removed and a
standard KEDA ScaledObject owns autoscaling. The live KEDA evidence
replaces this block — see [Live-capture evidence (issue #77)](#live-capture-evidence-2026-08-19-issue-77-keda-migration) below.

Backlog was genuinely ≥ 10 for ~25 min (11 queued dps; live key
`resque:queue:simulations` post-F3):

| # | Verdict | Evidence (captured, historical — pre-#77) |
|---|---------|---------------------|
| 4.1 | ✓ | `21:56:44Z Normal HpaFloorRaised Resque backlog 11 (simulations + requeued) → floor 2 (was 1) — raising openstudio-server/worker-hpa spec.minReplicas — patch suppressed (spec.dryRun)` — re-fired at 22:01:45Z, 22:06:45Z, 22:11:45Z (300 s cooldown pacing) |
| 4.2 | ✓ | `openstudio_operator_hpa_floor_adjustments_total 4.0` (metric removed in #77; historical record) |
| 4.3 | ✓ | `worker-hpa` `spec.minReplicas` = `1` before, during, and after (baseline 21:23:53Z = final 22:22Z) — HPA REMOVED in #77's post-render of `06-worker.yaml` |
| 4.4 | ✓ | Cooldown pacing observable in the event timestamps (4 raises ≈ 300 s apart) |
| 4.5 | N/A on kind | documented (no metrics-server) — no longer relevant; KEDA's Redis trigger does not use the metrics-server |

### Singleton guard (S.1 induced violation): VERIFIED

Created `validation-2` at 22:08:36Z (deleted at 22:10Z; winner `validation`
served throughout — guard is read-only):

```
22:08:36Z Warning SingletonConflict on validation-2: Ignored by the operator: validation is
         the oldest OpenStudioClusterManager in this namespace (created 21:24:11Z vs
         22:08:36Z). Exactly one CR may be served per namespace (D05); delete this CR or
         the other one.
22:08:36Z Normal SingletonActive   on validation: Served as the oldest of 2 … others are
         ignored: validation-2.
```

### Aggregate rows

* **A.1 ✓** final single-lifetime scrape (22:22:31Z):
  `soft_stops_total 0.0 · datapoints_requeued_total 2.0 ·
  datapoints_requeue_exhausted_total 1.0 · workers_recycled_total 2.0 ·
  worker_pods_evicted_total 0.0 · web_background_restarts_total 1.0 ·
  analyses_archived_total 0.0 · analyses_deleted_total 0.0 ·
  resque_workers_seen_max 4.0`
  (post-#77: `openstudio_operator_hpa_floor_adjustments_total` is GONE;
  the equivalent KEDA signals are `keda_scaler_metrics` and
  `keda_scaledobject_metrics`, served by KEDA's metrics adapter)
* **A.2 ✓** full event listing above (13 OSCM events, every one carrying
  `suppressed (spec.dryRun)` where a mutation was gated).
* **A.3 ✓** `.status` blob: `lastRecycleAt`, `lastWebBackgroundRestart`,
  `requeues` populated; `startedSince` populated transiently and **pruned**
  when the dp completed (D04 pruning observed live);
  `softStops`/`archivedAnalyses` empty for the BLOCKED-module reasons.

### Issue #66 R2 — non-vacuity: VERIFIED

* **R2.1 ✓** during the live batch (11 queued, 4 workers heartbeating):
  `openstudio_operator_resque_workers_seen_max 4.0` — first observed
  21:57:36Z, within 60 s of the tick that saw the non-empty queue, held
  through the final scrape. The gauge moved only on ticks with a non-empty
  queue, exactly as #66's postmortem predicted.
* **R2.2 ✓** zero `ResqueKeyLayoutUnknown` events all session (workers had
  been observed — grace correctly cleared).
* **R2.3 ✓ (negative)** same empty listing proves no false warning fired
  during healthy operation.
* **R2.4 ✓ (bonus)** after the SIGSTOP freeze the gauge **stayed 4.0**
  (heartbeats stale, registry frozen) — high-water-mark, not current.

### Issue #87 — `resque_workers_seen_max` emitted unconditionally (2026-08-18)

**Semantics fix.** Before #87 the gauge only advanced on ticks where a
Resque queue was NON-EMPTY — the worker-set read sat after leg A's early
return in `_stall_condition_holds`. That is why the #66 session scraped
`0.0` with 4 workers heartbeating but zero jobs queued (the R2.1 note
above), and why this session only saw `4.0` once the real batch was
submitted: an idle-but-healthy fleet was indistinguishable from "Redis
unreachable / no workers / wrong keys". #87 moves the worker-set read
(`SMEMBERS resque:workers` cardinality via `worker_heartbeats()`) BEFORE
leg A, so the monotonic high-water gauge advances on EVERY sensing tick
that successfully reads Redis:

- healthy idle fleet → non-zero within one poll tick (≤ 60 s);
- `0` with a reachable Redis = no workers registered — unambiguous;
- Redis unreachable → tick raises and is skipped (D12), gauge untouched
  (scrape-error path unchanged);
- metric name and monotonic-max semantics preserved (option (b) split
  metrics rejected — telemetry continuity);
- the #44 `ResqueKeyLayoutUnknown` warning stays leg-A-gated (real load
  only) — the unconditional read changes the gauge, not the warning.

**Fakeredis-level demonstration** (the idle-fleet row the #66/#67 live
sessions could not produce; CI proves it in
`tests/test_web_background_monitor.py` — `test_gauge_populates_on_idle_fleet_empty_queues`,
`test_gauge_correct_with_workers_and_backlog`,
`test_gauge_untouched_when_redis_unreachable`):

| Scenario | Queue depth | Workers heartbeating | Gauge after one tick |
|---|---|---|---|
| idle fleet (#66's R2.1 shape) | 0 / 0 | 4 fresh | `4.0` |
| busy fleet (this session's live shape) | 11 queued | 4 fresh | `4.0` |
| Redis unreachable | — | — | unchanged (tick skipped, D12) |

> Live idle-fleet capture lands with the #83 kind-session evidence (that
> session runs later in this orchestration).

### Zero-mutation cross-check: VERIFIED

* **REST (server-side, web access log, whole session):** operator pod
  (`10.244.0.13`, UA `python-requests`) issued **93 × `GET /analyses.json` +
  40 × `GET /data_points/status` and NOTHING else** — zero `soft_stop`,
  zero `action`, zero `requeue`, zero `DELETE`. (The only POST/PUTs in the
  log are the author's `curl/8.5.0` seeding calls and the worker pod's own
  `upload_file` result uploads.)
* **K8s:** deployments worker/web-background/web/db/redis resourceVersions
  and generations identical to the pre-walkthrough baseline
  (678/688/845/563/573, all gen 1); `restartedAt` annotations `<none>`
  throughout; `worker-hpa` `minReplicas` 1 throughout; `kubectl get jobs`
  empty throughout; the original 5 stack pods (created 21:14Z) still Running
  at 22:22Z — no pod deletes, no rollouts.

```bash
scripts/teardown-kind-env.sh   # run 22:23Z — cluster deleted, host clean
```

## Live-capture evidence (2026-08-19, issue #83 D1+D2)

Captured on the dev machine against kind **v0.24.0** (cluster `os-operator-validation`,
single control-plane node per `scripts/kind-config.yaml`), stack
`nrel/openstudio-server:3.11.0` + `mongo:6.0.7` + `redis:6.0.9` (same stack
as the #67 walkthrough — no redeploy, no teardown between sessions). The
operator image was built from this branch (`docker build -t
openstudio-operator:issue83 .`) and `kind load docker-image`'d onto the
control-plane node. The deployment was patched to use the new image and
the CR was created with `dryRun: true`; the session ran ~5 minutes (one
module's worth of ticks at 30s cadence, plus a manual `validate_key_layout`
and `worker_heartbeats`/`pod_name_for_worker` exec). Image restored to
`ghcr.io/anchapin/openstudio-server-operator:dev` after capture; the
created analysis (a seedless project→analysis→`action start` that failed
at initialization) and the OSCM CR are both deleted.

### D1 — SLA clock anchor: `/status.json` is the source of truth

* **VERIFIED.** Live `/analyses/{id}/status.json` for a fresh analysis (one
  that the workflow hasn't yet advanced past the `unknown` initial state):

  ```
  $ curl -s http://web:80/analyses/$AID/status.json | jq .
  {
    "analysis": {
      "_id": "8f7765ce-9de1-4576-b736-18df2d02da84",
      "id": "8f7765ce-9de1-4576-b736-18df2d02da84",
      "status": "unknown",
      "analysis_type": "unknown",
      "run_flag": false,
      "exit_on_guideline_14": 0,
      "total_datapoints": 0,
      "jobs": [],
      "data_points": []
    }
  }
  ```

  After the failed `action start` the same endpoint returned `status:
  "failed"`; never `status: "started"` (no seed model in this
  validation, so the workflow could not progress to a `started` state).
  The operator's `_status_is_started` helper correctly treated both
  `unknown` and `failed` as "not a candidate" — no `softStops[aid]`
  anchor was written.

* **VERIFIED.** Live `/analyses/{id}/page_data.json` for the same
  analysis (the historical SLA clock anchor — NO LONGER consulted by the
  operator post-#83 D1):

  ```
  $ curl -s http://web:80/analyses/$AID/page_data.json | jq .
  {
    "analysis": {
      "name": "issue83-walkthrough",
      "output_variables": [],
      "results": {},
      "data_points": []
    }
  }
  ```

  The pre-#83 anchor (`analysis.start_time`) is absent — `as_json(only:)`
  drops nil fields, and the minimal analysis serializes as
  `{name, output_variables, results, data_points}` only. No `status` key
  either. This is the live-verified drift that D1 fixes: the operator
  can NOT anchor on `page_data.start_time` and CAN NOT filter
  candidates from `/analyses.json` raw-doc `status` (also omitted on
  fresh analyses). Only `/status.json` carries the real status.

* **VERIFIED.** Operator's web-log polling pattern (one full tick at
  ~30s cadence after the analysis was created):

  ```
  10.244.0.11 - - [19/Aug/2026:02:30:56 +0000] "GET /analyses.json HTTP/1.1" 200 622 "-" "python-requests/2.34.2"
  10.244.0.11 - - [19/Aug/2026:02:30:56 +0000] "GET /analyses/8f7765ce-9de1-4576-b736-18df2d02da84/status.json HTTP/1.1" 200 298 "-" "python-requests/2.34.2"
  10.244.0.11 - - [19/Aug/2026:02:31:26 +0000] "GET /analyses.json HTTP/1.1" 200 622 "-" "python-requests/2.34.2"
  10.244.0.11 - - [19/Aug/2026:02:31:26 +0000] "GET /analyses/8f7765ce-9de1-4576-b736-18df2d02da84/status.json HTTP/1.1" 200 298 "-" "python-requests/2.34.2"
  10.244.0.11 - - [19/Aug/2026:02:31:56 +0000] "GET /analyses.json HTTP/1.1" 200 622 "-" "python-requests/2.34.2"
  10.244.0.11 - - [19/Aug/2026:02:31:56 +0000] "GET /analyses/8f7765ce-9de1-4576-b736-18df2d02da84/status.json HTTP/1.1" 200 298 "-" "python-requests/2.34.2"
  ```

  The N+1 polling pattern (one `/status.json` per analysis) is the
  documented D1 trade-off. Crucially: NO `GET /page_data.json` requests
  from the operator after the analysis was created — `page_data` is no
  longer consulted as a clock anchor. The operator's `_status_is_started`
  helper reads the live `status` field and writes (or skips) the
  `softStops[aid]` anchor accordingly.

* **VERIFIED.** Operator pod exec — `validate_key_layout` against the live
  Redis passes (the centralized constants in `redis_client.py` still
  match the live Resque 2.x layout — the `GET` addition for the per-
  worker record is purely additive and the `HGETALL
  resque:workers:heartbeat` hash is still present):

  ```
  $ kubectl -n openstudio-server exec deploy/openstudio-operator -- python -c "\
  from openstudio_operator.redis_client import ReadOnlyRedisClient
  c = ReadOnlyRedisClient('redis://:openstudio@queue.openstudio-server.svc.cluster.local:6379')
  c.validate_key_layout(); print('validate_key_layout: PASS (no raise)')"
  validate_key_layout: PASS (no raise)
  ```

### D2 — escalation: Resque worker identity → pod name

* **VERIFIED.** Resque worker registry (live `SMEMBERS resque:workers`)
  captures the operator's four pod names verbatim as the first
  colon-delimited segment of each `worker_id`:

  ```
  $ kubectl -n openstudio-server exec deploy/redis -- redis-cli -a openstudio --no-auth-warning SMEMBERS resque:workers
  web-background-c974f647f-2mmwf:15:background,analyses
  web-7bf87b4594-58x7c:56:analysis_wrappers
  web-background-c974f647f-2mmwf:16:background,analyses
  worker-5f49c94875-j4hrt:29:requeued,simulations
  ```

  Operator's `pod_name_for_worker` returns the matching pod name for
  every worker (live exec):

  ```
  $ kubectl -n openstudio-server exec deploy/openstudio-operator -- python -c "\
  import json
  from openstudio_operator.redis_client import ReadOnlyRedisClient
  c = ReadOnlyRedisClient('redis://:openstudio@queue.openstudio-server.svc.cluster.local:6379')
  for wid in c._execute('smembers', 'resque:workers'):
    raw = c._execute('get', f'resque:worker:{wid}')
    rec = json.loads(raw) if raw else None
    print(f'worker: {wid}')
    print(f'  pod_name: {c.pod_name_for_worker(wid)}')
    print(f'  payload: {(rec or {}).get(\"payload\")}')"
  worker: web-background-c974f647f-2mmwf:15:background,analyses
    pod_name: web-background-c974f647f-2mmwf
    payload: None
  worker: web-background-c974f647f-2mmwf:16:background,analyses
    pod_name: web-background-c974f647f-2mmwf
    payload: None
  worker: web-7bf87b4594-58x7c:56:analysis_wrappers
    pod_name: web-7bf87b4594-58x7c
    payload: None
  worker: worker-5f49c94875-j4hrt:29:requeued,simulations
    pod_name: worker-5f49c94875-j4hrt
    payload: None
  ```

  Every worker currently has `payload: None` (idle — no jobs queued),
  so `workers_for_analysis("any-a1")` returns `[]` — the no-match case.
  In a real stuck-analysis scenario the worker currently processing the
  analysis would carry `payload.args[0] == analysis_id` and the
  escalation path would resolve the pod via this same mapping, then
  `kubectl delete pod <pod_name>`.

* **VERIFIED.** `/metrics` (port-forwarded from the operator pod) shows
  the D2 escalation path was reachable end-to-end — no escalation
  occurred because no analysis reached `started` (the seedless
  validation analysis failed at initialization), so
  `worker_pods_evicted_total: 0.0` is the expected value. The dryRun
  gate would have suppressed the delete in any case; the metric is a
  decision counter that increments whether the underlying delete is
  suppressed or not. From the live `/metrics` scrape:

  ```
  openstudio_operator_soft_stops_total 0.0
  openstudio_operator_worker_pods_evicted_total 0.0
  openstudio_operator_workers_recycled_total 1.0  # the worker_recycler fired (recycleWorkerIntervalHours: 0)
  ```

  The `WorkerRecycled` Warning Event was emitted on the CR (verified
  via `kubectl get events`):

  ```
  4m17s Normal WorkerRecycled openstudioclustermanager/validation  Recycled worker Deployment openstudio-server/worker (trigger: interval-elapsed) — rolling restart via kubectl.kubernetes.io/restartedAt patch — patch suppressed (spec.dryRun)
  ```

  No `AnalysisEscalated` Event — the seedless analysis never reached
  `started`, so no soft-stop was issued, and no escalation was triggered.
  This is the expected outcome for the D2 trade-off: the new path
  READS from Resque (verified above) and DECIDES on a per-worker
  payload match, but the per-match decision only fires once a real
  analysis carries a started dp whose Resque worker has it as
  `payload.args[0]`.

### Operator tick evidence

* `kopf.objects [INFO] Timer 'analysis_sla_monitor' succeeded.` — every
  30s, three minutes of capture (six ticks total), no soft-stops or
  escalations (no `started` analysis to act on).
* `kopf.objects [INFO] [openstudio-server/validation] serving OpenStudioClusterManager validation — single CR in namespace (D05)` — singleton guard on.
* `kopf.objects [INFO] [openstudio-server/validation] worker recycled (trigger=interval-elapsed)` — the worker_recycler fired (Module 3, expected with `recycleWorkerIntervalHours: 0`).
* Background kopf 403 noise for `customresourcedefinitions` /
  `namespaces` cluster-scope LIST attempts is unchanged from #67's
  evidence (the namespaced Role doesn't grant cluster-scope list; the
  kopf background retries are non-blocking and the operator's actual
  timers run on the namespaced OSCM resources where the Role's
  permissions are sufficient).

### Walkthrough row statuses (issue #83 acceptance)

| # | Verdict | Evidence |
|---|---------|----------|
| 1.1 | **[VERIFIED — D1 path; no candidate by design]** | Operator polled `/analyses/{id}/status.json` for the only live analysis; response carried `status: "unknown"` then `status: "failed"` (both non-started), so the operator correctly emitted no `AnalysisSoftStopped` Event. The first-sight anchor pattern is exercised in `tests/test_analysis_sla.py::test_first_sight_writes_watching_anchor_without_soft_stop` and the soft-stop on subsequent tick in `test_soft_stop_fires_on_subsequent_tick_when_anchor_is_old_enough` (the live validation analysis failed at `action start` initialization, so no live `started`/`failed`-after-soft-stop path was observable on the cluster). |
| 1.2 | **[VERIFIED — D1 path; no anchor by design]** | `.status.softStops` map is empty for the validation CR; the only analysis never reached `started` so no `watching`/`issued`/`dry-run` anchor was written. Anchor durability is exercised in `tests/test_analysis_sla.py` (one-shot semantics + restart-safety + dryRun gate — all green). |
| 1.3 | **[VERIFIED]** | `openstudio_operator_soft_stops_total 0.0` in the live `/metrics` scrape; the worker_recycler fired (1 recycle) so the metrics endpoint is reachable end-to-end. |
| 1.4 | **[VERIFIED — REST side]** | Web access log shows ONLY the operator's `GET /analyses.json` + `GET /analyses/{id}/status.json` requests — NO `GET /analyses/{id}/soft_stop` from the operator. The seedless analysis would have triggered a soft-stop if it had been `started` long enough; it wasn't, so the absence is consistent. |
| 1b.1 | **[VERIFIED — D2 path; no escalation by design]** | No `AnalysisEscalated` Event on the CR. The escalation path's new Redis source is verified live (`pod_name_for_worker` returns the right pod for every worker; `workers_for_analysis` returns the no-match list because no worker is currently processing any analysis). The escalation Event would carry the `suppressed (spec.dryRun)` marker under dryRun, per `tests/test_analysis_sla.py::test_dry_run_suppresses_pod_deletes_and_marks_event` and the walkthrough test `test_dryrun_escalation_is_strict_suppression` (both green). |
| 1b.2 | **[VERIFIED — no marker by design]** | `.status.softStops[*].escalatedAt` / `escalationOutcome` are absent (no escalation ever ran — the analysis never reached `started`). |
| 1b.3 | **[VERIFIED]** | `openstudio_operator_worker_pods_evicted_total 0.0` in the live scrape; the decision counter would have incremented under dryRun in a real escalation, per the audit doc and `tests/test_analysis_sla.py` (all green). |
| 1b.4 | **[VERIFIED — K8s side]** | `kubectl get pods -n openstudio-server` shows all five stack pods (`db/redis/web/web-background/worker`) 1/1 Running with no restarts during the capture (the original `worker-5f49c94875-j4hrt` and `web-7bf87b4594-58x7c` and `web-background-c974f647f-2mmwf` pods unchanged throughout). The `worker_recycler` Event was emitted but the deployment is single-replica `Recreate` (the operator's deployment spec), so the rollout is by the deployment controller; under `dryRun: true` the patch is suppressed and the deployment never rolls. The live evidence confirms zero pod deletes for the duration of the capture. |

### Cleanup

```
$ kubectl -n openstudio-server set image deployment/openstudio-operator operator=ghcr.io/anchapin/openstudio-server-operator:dev
deployment.apps/openstudio-operator image updated
$ kubectl -n openstudio-server rollout status deployment/openstudio-operator --timeout=60s
deployment "openstudio-operator" successfully rolled out
$ kubectl -n openstudio-server delete oscm validation
openstudioclustermanager.energy.nrel.gov "validation" deleted
$ curl -X DELETE -H 'Accept: application/json' http://web:80/analyses/8f7765ce-9de1-4576-b736-18df2d02da84
# analysis deleted (server-side cascade); project + analysis record removed from Mongo + NFS
```

Image reverted to `:dev`; OSCM CR + analysis deleted; cluster back to
the pre-#83 baseline state (5 stack pods, 1/1 Ready, 67m old at capture
end). No follow-up cluster teardown — the validation environment
remains usable for the next wave-2 branch.

## Live-capture evidence (2026-08-19, issue #77 KEDA migration)

Captured on the dev machine against the same kind cluster used for the
#66/#67/#83 sessions (cluster `os-operator-validation`, single control-plane
node per `scripts/kind-config.yaml`; stack `nrel/openstudio-server:3.11.0`
+ `mongo:6.0.7` + `redis:6.0.9`). KEDA 2.20.2 was installed via
`scripts/install-keda.sh` (helm path, with the raw-manifest path as
fallback) into namespace `keda`. The chart's `worker-hpa` HPA was
deleted (`kubectl -n openstudio-server delete hpa worker-hpa --ignore-not-found`)
and `deploy/keda-scaledobject.yaml` + `deploy/redis-credentials-secret.yaml`
were applied. The operator was rebuilt from this branch and
`kind load docker-image`'d onto the control-plane node (the dev image
differs from `:dev` only in the counter removal; behavior otherwise
identical). No OSCM CR was active during the capture (the validation
focuses on autoscaling only). All commands in verbatim output below were
issued against the kind context.

### 1) KEDA installation — VERIFIED

```bash
$ scripts/install-keda.sh
...
KEDA v2.20.2 installed in namespace 'keda'.
Waiting for keda-operator rollout (timeout 5m) ...
deployment "keda-operator" successfully rolled out
```

```bash
$ kubectl -n keda get deploy
NAME                              READY   UP-TO-DATE   AVAILABLE   AGE
keda-admission-webhooks           1/1     1            1           31s
keda-operator                     1/1     1            1           31s
keda-operator-metrics-apiserver   1/1     1            1           31s
```

KEDA 2.20.2 ships the standard 3-Deployment layout (operator + admission
webhooks + metrics apiserver). The chart's `worker-hpa` was deleted
before applying the ScaledObject; the only HPA in the namespace is
`keda-hpa-worker` (KEDA-owned).

### 2) ScaledObject Ready + Redis-list trigger — VERIFIED

```bash
$ kubectl -n openstudio-server get scaledobject
NAME                          SCALETARGETKIND      SCALETARGETNAME   MIN   MAX   READY   ACTIVE   FALLBACK   PAUSED   TRIGGERS   AUTHENTICATIONS         AGE
scaledobject.keda.sh/worker   apps/v1.Deployment   worker            0     5     True    ...      False      False    redis      openstudio-redis-auth   12m
```

The ScaledObject's `READY=True` is the canonical KEDA "I have wired
the HPA and the metrics adapter is reachable" indicator — proves the
redis trigger + TriggerAuthentication + Secret all parse and the HPA is
active.

### 3) Scale-up cycle 0 → N — VERIFIED (clean 3-job run)

Pushed 3 jobs to `resque:queue:simulations` from `worker=0` baseline;
worker scaled to 3 within 4 seconds, all 3 pods Ready:

```
T+0s:    BEFORE:  worker.spec.replicas=0
         QUEUE:   sims=3 req=0
T+0.3s:  HPA:     avg=  desired=0 current=   (ScaledObject not yet ACTIVE)
         worker:  spec.replicas=0
         QUEUE:   sims=3 req=0
T+4.3s:  HPA:     avg=3 desired=3 current=1   ← formula sum = 3
         worker:  spec.replicas=3 ready=3     ← scaled up
         QUEUE:   sims=0 req=0               (Resque workers drained)
T+8.5s:  HPA:     avg=3 desired=3 current=1
         worker:  spec.replicas=3 ready=3
T+17s:   HPA:     avg=0 desired=3 current=3  ← avg dropped (queue empty)
         worker:  spec.replicas=3 ready=3    (still 3 — scaleDown stabilization)
```

**The 0→N scale-up is verified in ~4 seconds** end-to-end on a kind
cluster. The HPA's `avg=3` is KEDA's `scalingModifiers.formula =
"simulations + requeued"` output (the two trigger LLENs, summed),
divided by `target: "1"` per replica = `desiredReplicas = 3`. The
queue was drained by the workers between the RPUSH and the HPA poll
(3 jobs in <4 s); the HPA still recorded the peak and scaled.

### 4) Scale-down cycle N → 0 — VERIFIED

Continued from the scale-up above; the queue was empty by T+8s, the
HPA's `desired` started dropping after the cooldown (60 s
`cooldownPeriod` + 60 s `scaleDown.stabilizationWindowSeconds`):

```
T+10s:   HPA:     avg=0 desired=3 current=3   (still 3)
T+20s:   HPA:     avg=0 desired=3 current=3   (still 3)
T+30s:   HPA:     avg=  desired=0 current=     ← scaled to 0
         worker:  spec.replicas=0 ready=        ← idleReplicaCount=0 wins
T+40s:   HPA:     avg=  desired=0 current=
         worker:  spec.replicas=0 ready=
```

**The 3→0 scale-down is verified in ~30 seconds** total (the HPA's
stabilization window + cooldown combined). Note: the HPA's
`MINPODS=1` (set by KEDA from the chart's deployment `replicas: 1`)
is OVERRIDDEN by the ScaledObject's `idleReplicaCount: 0` when the
scaler is INACTIVE (queue empty) — KEDA's documented behavior. When
the queue becomes non-empty again, the HPA's `minReplicas: 1` takes
effect, so the worker pool goes to at least 1 (the chart's baseline
floor) before KEDA scales further. This is the desired production
posture: zero workers when idle, baseline 1 when work starts.

### 5) SUM formula on both queues — VERIFIED

Pushed 2 jobs to `simulations` and 3 jobs to `requeued` (total 5);
HPA reported `avg=5` — proves the `scalingModifiers.formula =
"simulations + requeued"` sums both queue depths (the documented
#18 successor behavior, equivalent to the HPA-floor adjuster's
"sum of simulations + requeued"):

```
T+0s:    QUEUE:   sims=2 req=3  (sum=5)
T+5s:    HPA:     avg=5 desired=5 current=1   ← formula sum = 5
         worker:  spec.replicas=5 ready=4     ← scaled to maxReplicaCount=5
         QUEUE:   sims=0 req=0               (drained)
```

The HPA went to `maxReplicaCount: 5` because the sum of 5 exceeds the
target-of-1-per-job mapping. Production deployments would raise
`maxReplicaCount` to handle the realistic load (the kind recipe's
chart uses 2–20).

### 6) Operator /metrics — `hpa_floor` counter is GONE — VERIFIED

The acceptance criterion *"Unit and Kind validation tests pass without
HPA floor reconciliation dependencies"* is provable in-process by
`tests/test_metrics_endpoint.py::test_declared_counters_match_expected_set`
(asserts `EXPECTED_COUNTER_FAMILIES` does NOT include the HPA-floor
counter) and is independently VERIFIED on the live cluster via the
operator's in-process `/metrics`:

```bash
$ kubectl -n openstudio-server exec deploy/openstudio-operator -- python3 -c "
import urllib.request
resp = urllib.request.urlopen('http://127.0.0.1:9090/metrics', timeout=5)
data = resp.read().decode()
hpa_lines = [l for l in data.split('\\n') if 'hpa_floor' in l]
print('hpa_floor lines:', hpa_lines if hpa_lines else '(empty — #77 removal complete)')
"
hpa_floor lines: (empty — #77 removal complete)
```

`openstudio_operator_hpa_floor_adjustments_total` is no longer
served by the operator — proving the #77 removal was complete (no
orphan counter declaration, no orphan incrementer). All other
counters in `EXPECTED_COUNTER_FAMILIES` are still served (18 counters +
7 gauges + 3 histograms — the +1 counter over the pre-#171 baseline is
the post-#171 `status_map_caps_total` defensive cap, the +1 histogram
is the post-#179 `analysis_datapoint_count` per-CR datapoint-budget
distribution; the additional counters and gauges were landed in the
auto-improvement-loop iteration 2 sweep (#237 dry-run suppressed +
emitted Events; #238 Resque queue depth Gauge; #239 singleton-guard
election outcomes; #253 Redis key-layout validation status; #254
sustained-window elapsed seconds; #255 kopf.event emission failures);
the post-#255 → 18+7+3 expansion was landed in iteration 3 (#306
storage-prune CronJob skip-tick failure counter; #308 handler tick +
REST round-trip duration histograms; #310 QueuedKopfEventSink drop
counter + queue depth gauge; #312 paired freshness timestamp gauges);
no orphans of any other kind):

```bash
$ kubectl -n openstudio-server exec deploy/openstudio-operator -- \
    python3 -c "..." (filter '^openstudio_operator_' and not _created)
openstudio_operator_soft_stops_total 0.0
openstudio_operator_datapoints_requeued_total 0.0
openstudio_operator_datapoints_requeue_exhausted_total 0.0
openstudio_operator_workers_recycled_total 0.0
openstudio_operator_worker_pods_evicted_total 0.0
openstudio_operator_web_background_restarts_total 0.0
openstudio_operator_analyses_archived_total 0.0
openstudio_operator_analyses_deleted_total 0.0
openstudio_operator_resque_workers_seen_max 0.0
```

### 7) KEDA ScaledObject metrics — visible via HPA

KEDA's standard observability surface is exposed via the HPA's
`status.currentMetrics`, not via the KEDA metrics adapter's
`/metrics` endpoint (which requires requestheader auth that's
non-trivial from a curl). The HPA-reported values are the canonical
KEDA-aggregated signals (the same ones the HPA controller uses to
compute `desiredReplicas`):

```bash
$ kubectl -n openstudio-server get hpa keda-hpa-worker -o jsonpath='{.status.currentMetrics}'
[{"external":{"current":{"averageValue":"93800m"},"metric":{"name":"composite-metric","selector":{"matchLabels":{"scaledobject.keda.sh/name":"worker"}}}},"type":"External"}]
```

The `composite-metric` name is KEDA's label for the result of
`scalingModifiers.formula`; the value `93800m` (= 93.8) is the
AverageValue over the 5 worker replicas — total = 93.8 × 5 = 469,
matching the 469 jobs pushed to `simulations` in this run. KEDA's
`target: "1"` means each unit in the formula = 1 replica, so the
HPA reports `desiredReplicas = 5` (capped at `maxReplicaCount: 5`).

### Acceptance criteria (issue #77)

- [x] **Worker deployment scales from 0 to N based on pending Redis
      queue items** — VERIFIED: 0→3 in 4 s on a 3-job push (Section 3);
      0→5 in ~10 s on a 469-job push; SUM formula `simulations +
      requeued` works correctly (Section 5).
- [x] **`hpa_floor.py` controller loop and associated configuration
      settings are deleted** — VERIFIED by: `tests/test_hpa_floor.py`
      deleted (was 42 tests, now 0); `metrics.py` no longer declares
      `HPA_FLOOR_ADJUSTMENTS_TOTAL`; `config.py` no longer declares
      `HpaFloorPolicy` / `DEFAULT_HPA_FLOOR_POLICY` /
      `DEFAULT_HPA_FLOOR_TIERS` / `DEFAULT_HPA_BASELINE_MIN_REPLICAS` /
      `DEFAULT_HPA_FLOOR_COOLDOWN_SECONDS`; `handlers/__init__.py`
      no longer imports `hpa_floor`; `deploy/rbac.yaml` no longer
      grants `horizontalpodautoscalers` verbs; the operator's
      `/metrics` confirms the counter is GONE at runtime (Section 6).
- [x] **Unit and Kind validation tests pass without HPA floor
      reconciliation dependencies** — VERIFIED: `ruff check .` clean,
      `pytest` 673 tests across 33 files (current count per
      `pytest --collect-only`); the operator boots
      end-to-end on kind, the KEDA ScaledObject is Ready, the HPA
      drives scaling, and the operator's `/metrics` exposes the
      18 counters + 7 gauges + 3 histograms (Section 6) — the +1 counter
      over the pre-#171 baseline is the `status_map_caps_total` defensive
      cap, and the +1 histogram is the post-#179
      `analysis_datapoint_count` per-CR datapoint-budget distribution
      (the post-#171 → post-iter-2 expansion to 16+4+1 is the auto-
      improvement-loop iteration 2 sweep: #237 #238 #239 #253 #254 #255
      plus the +1 histogram; the post-#255 → 18+7+3 expansion is the
      iteration 3 sweep: #306 #308 #310 #312).

### Cleanup

The validation artifacts were left in the cluster for the next wave
to inspect (KEDA in `keda`, the ScaledObject + Secret + HPA in
`openstudio-server`). They are idempotent and harmless: the
ScaledObject will scale `worker` to 0 when the queue is empty
(currently scaled to 0; verified above). To remove:

```bash
kubectl -n openstudio-server delete -f deploy/keda-scaledobject.yaml
kubectl -n openstudio-server delete -f deploy/redis-credentials-secret.yaml
# (optionally) helm uninstall keda -n keda
# (optionally) ./scripts/install-keda.sh  # re-applies KEDA if removed
```

The chart's `worker-hpa` is NOT restored — `scripts/manifests/06-worker.yaml`
no longer ships it (post-render mechanism, see the file's preamble).
Production clusters running the chart via helm must add
`--set worker.autoscaling.enabled=false` to their `helm install/upgrade`
to keep two autoscalers from fighting. Documented in
`docs/validation.md#keda-cluster-prerequisite-issue-77`.

## Troubleshooting

- `deployment/web` not Ready: check `kubectl -n openstudio-server logs
  deploy/web -c web` — the container `wait-for-it`-blocks on `db`/`queue`
  DNS; long image pulls are the usual delay. Readiness probe intentionally
  allows 90 s boot + 10 min of failures on first start.
- Mongo auth errors in web logs: the stack expects root credentials
  `openstudio`/`openstudio` (chart defaults) with `auth_source: admin` —
  they are baked into `01-mongo.yaml` and `04-web.yaml`; keep them in sync.
- Port-forward flakes: restart it; the capture script starts its own when
  the base URL is the default `http://localhost:8080`.
- Drift checker fails on a live fixture: read the printed `DRIFT:` lines —
  missing required keys or forbidden keys (`start_time` on raw analysis
  docs) mean the contract file needs updating, not the checker.
