# kind validation environment — OpenStudio Server 3.11.0 (issue #19, decision D13)

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
# (same for mongo:6.0.7 / redis:6.0.9)
```

## What gets deployed (topology parity vs the helm chart)

Names are load-bearing — the operator targets them by fixed identifier
(AGENTS.md "Fixed identifiers"). All in namespace `openstudio-server`.

| Chart object (develop) | kind manifest | Parity notes |
|---|---|---|
| Deployment `db` (mongo:6.0.7) + Service `db:27017` | `scripts/manifests/01-mongo.yaml` | same image/creds (`openstudio`/`openstudio`, auth_source `admin`); persistence emptyDir |
| Deployment `redis` (redis:6.0.9) + Service `queue:6379` | `scripts/manifests/02-redis.yaml` | same image/password (`--requirepass openstudio`); persistence emptyDir |
| PVC `nfs-pvc` (RWX, storageClass `nfs`) | `scripts/manifests/03-nfs-hostpath.yaml` | **hostPath stand-in** on the kind node (`/tmp/openstudio-kind-nfs`) |
| Deployment `web` + Service `web:80` | `scripts/manifests/04-web.yaml` | image pinned **3.11.0** (chart default 3.8.0-1 is NOT the target); same command/env (`QUEUES=analysis_wrappers`, `REDIS_URL`, `MONGO_USER/PASSWORD`, `SECRET_KEY_BASE` chart default); mounts `nfs-pvc` at `/mnt/openstudio` |
| Deployment `web-background` | `scripts/manifests/05-web-background.yaml` | same command (`resque:workers` scaled by `COUNT`); `OS_SERVER_NUMBER_OF_WORKERS` tuned 30→2; mounts `nfs-pvc` |
| Deployment `worker` + HPA `worker-hpa` | `scripts/manifests/06-worker.yaml` | scaled-minimal: replicas **1**, HPA 1–2; `QUEUES=requeued,simulations`; emptyDir scratch at `/mnt/openstudio`; preStop hook + `terminationGracePeriodSeconds: 5200` kept |
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
- **No metrics-server** — `worker-hpa` never scales on CPU (stays at
  `minReplicas`). That is deliberate: the HPA exists for topology parity as
  the Phase-4 patch target (#18 `minReplicas` adjuster), not for load tests.
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
   print(c.get_analysis_page_data("<an-analysis-id>"))
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
| 4.1 | `AnalysisArchivalStarted` Normal Event on the CR, message ending `— Job spawn suppressed (spec.dryRun)` | `handlers/storage_pruner.py:484` | `[CI] tests/test_storage_pruner.py:625 test_dry_run_suppresses_spawn_and_delete_with_observable_tracking` |
| 4.2 | `.status.archivedAnalyses[<analysis-id>]` has NO `jobName` field (the dry-run marker; suppressed spawn can never be watched) | `handlers/storage_pruner.py:471` | `[CI] tests/test_storage_pruner.py:625` (same) |
| 4.3 | NO Kubernetes Job created with the deterministic name `oscm-archive-<analysis-id>-<hash>` (`kubectl get jobs` is unchanged) | `handlers/storage_pruner.py:463` | `[CI] tests/test_storage_pruner.py:625` (mocked `create_namespaced_job`, zero calls) + `[REQUIRES LIVE CLUSTER]` jobs on kind |
| 4.4 | NO failed-archival-Job cleanup delete — if a failed Job exists from the previous tick's real run, it stays for forensics. (Regression #42; #42 fix included in this audit's #21 sweep.) | `handlers/storage_pruner.py:456` | `[CI] tests/test_storage_pruner.py:663 test_dry_run_suppresses_failed_job_cleanup_delete` |
| 4.5 | Verified-record path: a real Job that reached `Complete` would have triggered `DELETE /analyses/{id}`; under dryRun, the `AnalysisDeleted` Normal Event fires once with the `— delete suppressed (spec.dryRun)` marker, and the verified record persists | `handlers/storage_pruner.py:293` | `[CI] tests/test_storage_pruner.py:710 test_dry_run_suppresses_delete_of_verified_analysis` |
| 4.6 | `openstudio_operator_analyses_deleted_total` counter +0 in dry-run (deletion is the only counter that is *suppressed* in dry-run; archival + verification increments normally) | `metrics.py` (Appendix D) | `[CI] tests/test_storage_pruner.py:710` (same) |
| 4.7 | NFS-mount behavior under a real provisioner | not reproducible on kind (hostPath stand-in) | `[REQUIRES LIVE CLUSTER]` on a work cluster — `docs/validation.md` Module 4 owns this |

### Module 5 — web_background stall detector (probe: induced stall, `stallWindowMinutes` elapsed)

| # | Evidence | Source | CI counterpart |
|---|---------|--------|----------------|
| 5.1 | `WebBackgroundRestarted` Warning Event on the CR, message ending `— patch suppressed (spec.dryRun)` | `handlers/web_background_monitor.py:323-324` | `[CI] tests/test_web_background_monitor.py:573 test_dry_run_suppresses_patch_marks_event_and_advances_anchor` |
| 5.2 | `.status.lastWebBackgroundRestart` advances (cooldown pacing under dryRun — D11 choice) | `handlers/web_background_monitor.py:328` | `[CI] tests/test_web_background_monitor.py:573` (same) |
| 5.3 | `openstudio_operator_web_background_restarts_total` counter +1 | `metrics.py` | `[CI] tests/test_web_background_monitor.py:573` (same) |
| 5.4 | NO `kubectl.kubernetes.io/restartedAt` annotation change on the `web-background` Deployment | `handlers/web_background_monitor.py:310` | `[CI] tests/test_web_background_monitor.py:573` (mocked `patch_namespaced_deployment`, zero calls) + `[REQUIRES LIVE CLUSTER]` deployment on kind |

### Phase 4 — HPA-floor adjuster (probe: Resque backlog ≥ first tier threshold)

| # | Evidence | Source | CI counterpart |
|---|---------|--------|----------------|
| 4.1 | `HpaFloorRaised` Normal Event on the CR (raising the floor is information-level), or `HpaFloorDecayed` Warning Event on decay (removing floor capacity is the direction worth human eyes). Both end `— patch suppressed (spec.dryRun)` | `handlers/hpa_floor.py:329-332` | `[CI] tests/test_hpa_floor.py:337 test_dry_run_suppresses_patch_but_paces_like_real` |
| 4.2 | `openstudio_operator_hpa_floor_adjustments_total` counter +1 | `metrics.py` | `[CI] tests/test_hpa_floor.py:337` (same) |
| 4.3 | NO patch on `worker-hpa` (`kubectl get hpa worker-hpa -o jsonpath='{.spec.minReplicas}'` unchanged) | `handlers/hpa_floor.py:319` | `[CI] tests/test_hpa_floor.py:337` (mocked `patch_namespaced_horizontalpodautoscaler`, zero calls) + `[REQUIRES LIVE CLUSTER]` HPA on kind |
| 4.4 | In-memory `HpaFloorState` cooldown advances (D11 pacing choice; documented D04 deviation) | `handlers/hpa_floor.py:338` | `[CI] tests/test_hpa_floor.py:337` (same) |
| 4.5 | Real HPA dynamics are unobservable on kind (no metrics-server; `worker-hpa` stays pinned at `minReplicas`) | `Approximations` above | `[REQUIRES LIVE CLUSTER]` on a work cluster |

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
the `:dev` image was NOT pulled from ghcr). Redis password per the manifest:
`openstudio`. All `redis-cli` output below is verbatim.

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
* **F2 — `hpa_floor` calls a non-existent client method.** Live timer error:
  `'AutoscalingV1Api' object has no attribute
  'read_namespaced_horizontalpodautoscaler'` — the real generated method is
  `read_namespaced_horizontal_pod_autoscaler`. CI fakes defined the
  misspelled name, so tests passed. Fix: rename in `hpa_floor.py` + both
  test fakes.
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
  `run_start_time`). Follow-up issue should own this.
* **D2 — datapoint `ip_address` is never populated on K8s** (live: `null`
  while `started` with `job_id` set; pod-eviction escalation can never match
  victims). Also feedstock for the 1b rows. Same follow-up.

### Module 1 — Analysis SLA soft-stop: [BLOCKED: D1]

| # | Verdict | Evidence |
|---|---------|----------|
| 1.1–1.3 | [BLOCKED: D1] | SLA monitor never sees a `started` analysis (no `status` in raw docs; no `start_time` in page_data) — nothing to soft-stop. Timer itself ran clean all session (`Timer 'analysis_sla_monitor' succeeded`, 30 s cadence). |
| 1.4 | VERIFIED (vacuously + by access log) | See zero-mutation cross-check: **zero** `GET …/soft_stop` from the operator all session (its only requests were `GET /analyses.json` ×93, `GET /data_points/status` ×40). |

### Module 1b — Grace wait + escalation: [BLOCKED: D1+D2]

Depends on Module 1's anchor; additionally `ip_address` is `null` on started
datapoints (D2) so victim matching could never resolve.
`openstudio_operator_worker_pods_evicted_total` observed `0.0` (consistent).

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

### Module 4 — Archival + NFS prune: [BLOCKED: D1]

Eligibility requires `status == "completed"` in `/analyses.json` raw docs —
never present live (D1), so no `AnalysisArchivalStarted` could fire despite
`retentionDays: 0` + complete archival spec. Consistent negatives captured:
`openstudio_operator_analyses_archived_total 0.0`,
`analyses_deleted_total 0.0` (4.6 holds — deletion suppressed/increment-0),
`kubectl get jobs` empty all session (4.3), no `DELETE /analyses/{id}` in
web logs (4.5's mutation never reached the server). Row 4.7 remains
work-cluster-only per the checklist. The eligibility fix belongs to the D1
follow-up.

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

### Phase 4 — HPA-floor adjuster: VERIFIED (live backlog)

Backlog was genuinely ≥ 10 for ~25 min (11 queued dps; live key
`resque:queue:simulations` post-F3):

| # | Verdict | Evidence (captured) |
|---|---------|---------------------|
| 4.1 | ✓ | `21:56:44Z Normal HpaFloorRaised Resque backlog 11 (simulations + requeued) → floor 2 (was 1) — raising openstudio-server/worker-hpa spec.minReplicas — patch suppressed (spec.dryRun)` — re-fired at 22:01:45Z, 22:06:45Z, 22:11:45Z (300 s cooldown pacing) |
| 4.2 | ✓ | `openstudio_operator_hpa_floor_adjustments_total 4.0` |
| 4.3 | ✓ | `worker-hpa` `spec.minReplicas` = `1` before, during, and after (baseline 21:23:53Z = final 22:22Z) |
| 4.4 | ✓ | Cooldown pacing observable in the event timestamps (4 raises ≈ 300 s apart) |
| 4.5 | N/A on kind | documented (no metrics-server) |

Note: no `HpaFloorDecayed` post-drain — coherent under dryRun: the suppressed
patch means the live floor never moved to 2, so each tick still read
`was 1` and "raised" again (decision-counted, mutation-suppressed). Decay
semantics remain CI-proven.

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
  hpa_floor_adjustments_total 4.0 · resque_workers_seen_max 4.0`
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
