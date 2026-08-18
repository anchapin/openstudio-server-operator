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

The constants live in `src/openstudio_operator/redis_client.py`:

```
WORKER_REGISTRY_KEY = "resque:workers"      # SET of registered worker ids
def _heartbeat_key(worker_id: str) -> str:
    return f"{WORKER_REGISTRY_KEY}:{worker_id}"   # heartbeat string, epoch float
```

If the live keys differ, edit those two constants — no other code needs to
change. The validator + safeguard are wired to use the constants directly.

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

```bash
scripts/teardown-kind-env.sh   # deletes the whole cluster (mongo/redis/NFS stand-in data die with it)
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
