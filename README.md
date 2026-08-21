# OpenStudio Server Operator

A Kubernetes operator that automates day-2 operations for [OpenStudio Server](https://github.com/NREL/OpenStudio-server) deployments (Ruby/Rails `web` + `web_background` + MongoDB + `worker` pods + NFS-shared volumes). It runs **alongside** the existing [`openstudio-server-helm`](https://github.com/NREL/openstudio-server-helm) chart — it manages that stack; it does not replace it.

**Status: implementation complete (Phases 1–4, issues #2–#21); live kind-cluster validation done for modules 2/3/5/singleton and the full-module `dryRun: true` walkthrough (#66/#67/#84); Phase-4 autoscaling is now driven by a standard KEDA ScaledObject (#77, replacing the custom HPA-floor adjuster #18); D1/D2 contract drift resolved by #96 (Resque-worker identity path — see Module 1 row).** The verified API ground truth is [`docs/contracts/openstudio-server-v3.11.0-rest.md`](./docs/contracts/openstudio-server-v3.11.0-rest.md). Cross-cutting audit: [`docs/audit-dryrun-idempotency.md`](./docs/audit-dryrun-idempotency.md). Cluster validation runbook: [`docs/validation.md`](./docs/validation.md). First-time contributors and AI agents: start at [`docs/onboarding.md`](./docs/onboarding.md) (issue #178). Framework: **Python + [Kopf](https://kopf.readthedocs.io/)**.

Changelog: [CHANGELOG.md](./CHANGELOG.md) — curated per-release notes (Keep a Changelog format; see issue #177). Contributing: [CONTRIBUTING.md](./CONTRIBUTING.md) — branch, PR-body, and merge-subject conventions (issue #302).

## What it will automate

| Module | Plan phase | Purpose |
|---|---|---|
| Analysis SLA / soft-stop | 1 | Soft-stop analyses exceeding `maxDurationMinutes`; after `gracefulStopTimeoutMinutes`, surgically evict the worker pods currently processing the analysis (resolved via the Resque worker registry → pod name, since v3.11.0 datapoint `ip_address` is always null — issue #83 D2). Gated by `analysisPolicy.forceDeleteOnEscalation`. |
| Zombie datapoint watchdog | 2 | Auto-requeue datapoints stalled past `maxDatapointRuntimeMinutes` (bounded by `maxAutoRequeues`) |
| Worker recycler | 2 | Rolling-restart the worker Deployment after analyses / on interval |
| web_background watchdog | 2 | Detect Resque queue stalls; restart the `web_background` Deployment |
| Storage archiver & NFS pruner | 3 | Ephemeral Jobs on the NFS PV archive results to S3/GCS, then prune |
| KEDA ScaledObject + `/metrics` | 4 | Standard KEDA ScaledObject scales the `worker` Deployment from 0..N on the Resque `simulations` + `requeued` queue depths; operator exposes Prometheus metrics on `:9090` |

### Autoscaling approach (KEDA, post-#77)

Worker autoscaling is a **standard KEDA ScaledObject**
(`deploy/keda-scaledobject.yaml`) — NOT an in-operator reconciliation loop.
The custom HPA-floor adjuster (#18) was removed in #77 because (a) it
patched only the chart's CPU HPA `minReplicas`, which meant the chart
HPA still controlled scaling above the floor and the operator was
fighting itself, and (b) KEDA + a Redis-list trigger on the same two
queues is the standard answer for "Resque-backed worker pool, scale by
queue depth". The acceptance criterion — *Worker deployment scales from
0 to N based on pending Redis queue items* — is verified end-to-end on
the kind cluster in [docs/kind-validation.md#live-capture-evidence-2026-08-19-issue-77-keda-migration](./docs/kind-validation.md).

**Cluster prerequisites (cluster-admin, outside the operator process):**

1. KEDA installed (helm `kedacore/keda` ≥ 2.20, or
   `scripts/install-keda.sh` which uses KEDA's raw manifests as fallback)
   in namespace `keda`.
2. The helm chart's `worker-hpa` HPA **disabled** (values override or
   `kubectl delete hpa worker-hpa --ignore-not-found`); two
   autoscalers on the same Deployment oscillate. The kind recipe's
   `scripts/manifests/06-worker.yaml` no longer includes the chart HPA,
   so a fresh `scripts/deploy-openstudio-stack.sh` does not apply it.
3. The Redis password Secret for KEDA's TriggerAuthentication (operator
   never sees it). The committed manifest ships only the unusable sentinel
   `CHANGE_ME_RUN_ROTATE_SCRIPT` (#462) — install a real per-cluster
   password with `scripts/rotate_redis_password.sh` first; plain
   `kubectl apply -f deploy/redis-credentials-secret.yaml` installs a
   non-working credential (see
   [docs/onboarding.md](./docs/onboarding.md) §"Redis password is not a
   fixed literal (#150)"). Same rule for Mongo:
   `scripts/rotate_mongo_password.sh` before any
   `kubectl apply -f deploy/mongo-credentials-secret.yaml`.
4. `kubectl apply -f deploy/keda-scaledobject.yaml`.

The operator's Role has no `horizontalpodautoscalers` verbs; autoscaling
is owned by KEDA's own ServiceAccount. Same RBAC-shrink pattern as #78
(storage moved to a prune CronJob SA).

## Metrics

The operator exposes a Prometheus scrape endpoint at `:9090/metrics` (plain
HTTP, no auth by default — see `src/openstudio_operator/metrics.py`). `/metrics` is the
**only** direct observability window into operator health: every Kopf timer
wrapper catches its failure tuples and silently skips the tick (D04 idempotency
survives), so a sustained degraded window — REST API down, Redis unreachable,
k8s API unavailable — only surfaces as one of these counters, never as a pod
restart. The scrape target is the operator Pod on port 9090 (matches the
`containerPort` in `deploy/operator-deployment.yaml`).

**Shipped alerting & dashboard artifacts (issue #468):** the canonical
alert expressions in the table below are transcribed into
`deploy/prometheustrule.yaml` — a `monitoring.coreos.com/v1` PrometheusRule
applied by the cluster admin (the operator's Role deliberately holds no
`prometheusrules` verbs); it ships the `release: prometheus` label for
kube-prometheus-stack default pickup — rename the label if your release
differs. `deploy/grafana-dashboard.json` renders the four OSCM timers'
action counters, the tick-duration histograms, and the Resque queue gauges
+ freshness (datasource templated as `${DS_PROMETHEUS}`).
`tests/test_monitoring_artifacts.py` fails CI if either artifact
references a metric family missing from `tests/_metrics_inventory.py`.

**Source of truth:** the family names below mirror
`tests/_metrics_inventory.py` — the canonical `EXPECTED_COUNTER_FAMILIES`,
`EXPECTED_GAUGE_FAMILIES`, and `EXPECTED_HISTOGRAM_FAMILIES` tuples shared by
`tests/test_metrics_endpoint.py` and `tests/test_walk_metrics_registry.py`
(#406) — exactly; the tests assert `declared == expected` on every CI run, so
adding a counter, gauge, or histogram here without adding it there (or vice
versa) fails CI loudly. **Current shape: 20 counters + 8 gauges + 3 histograms
(post-#171 status-map defensive cap; post-#179 datapoint-budget distribution;
post-#237 EventEmitter dry-run gate Prometheus surface; post-#238 Resque queue
depth gauges; post-#239 singleton-guard election outcome counter; post-#253
Redis key-layout validation status gauge; post-#254 sustained-window elapsed
seconds gauge; post-#255 kopf.event emission failure counter; post-#306
storage-prune CronJob skip-tick failure counter; post-#308 handler tick and
REST round-trip duration histograms; post-#310 QueuedKopfEventSink
backpressure drop counter + Queue depth gauge; post-#312 paired freshness
timestamp gauges for `resque_queue_depth` and `stall_window_elapsed_seconds`;
post-#403 singleton-guard loser per-tick skip counter; post-#393 metrics-server
bind-outcome gauge; post-#471 REST retry-attempt counter).**

**Optional bearer-token authN (issue #401):** by default the endpoint is open
plaintext behind the `openstudio-operator-metrics-ingress` NetworkPolicy
(#166). To add protocol-layer authN, set the `OPENSTUDIO_METRICS_TOKEN_FILE`
env var in `deploy/operator-deployment.yaml` to a mounted token-file path and
uncomment the commented-out `metrics-token` Secret volume + volumeMount
blocks there (Secret `openstudio-operator-metrics-token`, key `token`). The
server then requires `Authorization: Bearer <token>` on `/metrics` and
returns 401 on a missing or invalid token; a separate unauthenticated
`/healthz` path serves a bare 200 for the kubelet probes — when the token is
enabled, point both probe paths at `/healthz` (the kubelet cannot present a
Bearer header). The token file is re-read on every request, so Secret
rotation takes effect without an operator restart, and a missing or empty
file fails closed (every request 401s). No TLS is added — that is a separate
concern requiring cert management.

The metric families added since the 16+4+1 claim are signed off below
for the on-call's reference; the drift-integrity invariant that fails CI is
the `N counters + M gauges + K histograms` count itself, not any one
specific family:

- `openstudio_operator_prune_tick_failures_total` (counter, #306) — prune
  CronJob skip-tick failures (per `reason` label).
- `openstudio_operator_warnings_deferred_dropped_total` (counter, #310) —
  QueuedKopfEventSink backpressure rejections (per `reason` label).
- `openstudio_operator_resque_queue_depth_fresh` (gauge, #312) — last
  successful `queue_depths()` read timestamp; staleness pairs with the
  `resque_queue_depth` data gauge.
- `openstudio_operator_stall_window_fresh` (gauge, #312) — last successful
  `run_stall_tick` read timestamp; staleness pairs with the
  `stall_window_elapsed_seconds` data gauge.
- `openstudio_operator_warnings_deferred_queue_depth` (gauge, #310) —
  current depth of the in-process QueuedKopfEventSink queue.
- `openstudio_operator_handler_tick_duration_seconds` (histogram, #308) —
  per-`module` wall-clock duration of the four @kopf.timer wrappers.
- `openstudio_operator_rest_request_duration_seconds` (histogram, #308) —
  per-`(method, outcome)` wall-clock duration of OpenStudioClient REST calls.
- `openstudio_operator_singleton_loser_skips_total` (counter, #403) — per-tick
  singleton-guard loser suppressions (per `(module, namespace, name)`).
- `openstudio_operator_metrics_server_bound` (gauge, #393) — outcome of the
  /metrics server's first bind attempt (per `(addr, port)`); `== 0` is the
  canonical "Prometheus scrape is down because of US" signal.

| Family | Type | Module / issue origin | Meaning for an on-call |
|---|---|---|---|
| `openstudio_operator_soft_stops_total{outcome}` | counter (labelled) | `analysis_sla` (Module 1) · #8, #309 | Analyses soft-stopped in this process lifetime. Rising during normal SLA enforcement is expected. A long stall with rising analysis runtimes means the SLA clock anchor isn't being written (D04 / #83 D1 regression). Labelled by `outcome` (`issued` \| `dry-run`) so the dashboard can distinguish real soft-stops from dry-run-marked suppressions without log scraping. |
| `openstudio_operator_worker_pods_evicted_total{outcome}` | counter (labelled) | `analysis_sla` (Module 1) · #9, #83 D2, #309 | Surgically evicted worker pods during SLA escalation (counts past `gracefulStopTimeoutMinutes`). Dry-run ticks increment too. Non-zero means SLA escalations are firing — cross-check with `lastWebBackgroundRestart` and `.status.softStops`. Labelled by `outcome` (`evicted` \| `evicted-partial` \| `no-matching-pods` \| `dry-run`) so an SRE can tell whether escalations are succeeding or partially failing without log scraping. |
| `openstudio_operator_datapoints_requeued_total` | counter | `datapoint_watchdog` (Module 2) · #10 | Zombie datapoints auto-requeued. A burst signals a worker fleet problem or a new job class that doesn't update liveness. Bounded by `spec.datapointPolicy.maxAutoRequeues`. |
| `openstudio_operator_datapoints_requeue_exhausted_total` | counter | `datapoint_watchdog` (Module 2) · #10 | Datapoints abandoned after exceeding `maxAutoRequeues`. Should be near-zero in steady state; any nonzero rate means real jobs are dying past the auto-requeue budget. |
| `openstudio_operator_workers_recycled_total{trigger}` | counter (labelled) | `worker_recycler` (Module 3) · #11, #309 | Worker Deployment rolling-restarts issued by the recycler. Spikes imply the worker fleet is misbehaving (CRASHLOOP, OOM); a steady cadence is normal. Labelled by `trigger` (`analysis-completed` \| `interval-elapsed`) so an SRE investigating a recycle spike can tell whether the interval-elapsed sweep or the analysis-completed edge fired. |
| `openstudio_operator_web_background_restarts_total` | counter | `web_background_monitor` (Module 4) · #13 | `web_background` Deployment restarts after sustained queue stalls. Alert on any growth — Resque plumbing is broken or v3.11.0 key layout drifted (#44 / #87). |
| `openstudio_operator_analyses_archived_total` | counter | `retention` (storage pruner) · #16, #394 | Analyses whose archival Job passed rclone verification (including adopted completions). Steady growth = healthy storage archival. Adopted = the operator saw a Job completion it didn't start, counted anyway. **Wedged-pipeline alert (#394):** `rate(...) == 0` AND an in-flight archival Job's `status.active > age_seconds / ARCHIVAL_JOB_ACTIVE_DEADLINE_SECONDS` — `ARCHIVAL_JOB_ACTIVE_DEADLINE_SECONDS` is the upper bound the operator pins on every archival Job (6× the chart's worker `terminationGracePeriodSeconds`, currently `31200`s in `src/openstudio_operator/archival.py`); the kubelet hard-kills any rclone pod past it so the gate cannot be a hung-gate. |
| `openstudio_operator_analyses_deleted_total{outcome}` | counter (labelled) | `retention` (storage pruner) · #16, #309 | Analyses deleted after verified archival. In healthy operation, this should track `analyses_archived_total` minus the in-progress backlog. `spec.dryRun` suppresses both the delete and the increment. Labelled by `outcome` (`deleted` is the only currently-exercised value; the label is pinned so future outcome splits are a one-line change). |
| `openstudio_operator_status_conflicts_total` | counter | `status_store` (RMW helper) · #119 | Per-attempt 409 responses from the Kubernetes API Server during CR `.status` RMW cycles (incremented inside `_mutate` for each 409 before the backoff sleep). Sustained nonzero rate means multiple operators are racing; investigate the singleton guard (#14). |
| `openstudio_operator_status_conflict_retries_exhausted_total` | counter | `status_store` (RMW helper) · #119 | RMW cycles that exhausted the 409 retry budget and raised `StatusStoreConflictError` — the tick that hit this counter was skipped (WARNING log line, no `.status` write). Alert: a CR status write was lost. |
| `openstudio_operator_handler_tick_failures_total{module,error_type}` | counter (labelled) | all four timer wrappers (`analysis_sla` / `datapoint_watchdog` / `worker_recycler` / `web_background_monitor`) · #117 | Per-tick failures caught by the timer wrappers. Increment-by-1 per tick suppressed. Labelled by `module` and `error_type` (`OpenStudioApiError` \| `StatusStoreError` \| `ApiException` \| `RedisClientError`). Sustained nonzero per `(module, error_type)` tells you which downstream — REST, Redis, k8s API — is degraded. |
| `openstudio_operator_status_map_caps_total{map_name}` | counter (labelled) | `status_store` (`_set_map_entry` cap path) · #171 | Evictions triggered by the per-map defensive cap (drop-oldest when a map reaches `STATUS_MAP_MAX_ENTRIES = 10_000`). Labelled by `map_name` ∈ {`softStops`, `requeues`, `startedSince`, `archivedAnalyses`}. Sustained nonzero means something is filling the maps faster than they drain (e.g. an admin batch-`create` of 10k+ draft analyses) — investigate the upstream cause, don't raise the cap. |
| `openstudio_operator_events_dry_run_suppressed_total{reason}` | counter (labelled) | `events` (`EventEmitter.emit` dry-run branch) · #237 | Kubernetes Events suppressed by the dry-run gate (D11) — incremented at the same site as `EventEmitter.suppressed_count`, inside `EventEmitter.emit` when `dry_run=True`. Labelled by `reason` mirroring the warning-event vocabulary (`AnalysisSoftStopped` \| `AnalysisEscalated` \| `DatapointRequeued` \| `DatapointRequeueExhausted` \| `WorkerRecycled` \| `WebBackgroundRestarted` \| `ResqueKeyLayoutUnknown`) so a dashboard can tell WHICH handler path the dry-run gate intercepted. A cluster running dry-run mode (canary staging, audit-only installs) was previously invisible at `/metrics` — only log scraping for the `dry-run suppressed` INFO line worked. Sustained nonzero rate confirms the dry-run gate is firing. |
| `openstudio_operator_events_emitted_total{reason}` | counter (labelled) | `events` (`EventEmitter.emit` non-dry-run branch) · #237 | Companion to `events_dry_run_suppressed_total` — every successful `kopf.event` call from `EventEmitter` (`dry_run=False`). Same `reason` label vocabulary. `rate(events_emitted_total) / rate(events_dry_run_suppressed_total)` is the headline SLO for an audit-only install: a non-trivial suppressed rate with zero emitted rate is the intended steady state; the inverse drift (suppressed > emitted during a non-dry-run deploy) is the alert signal. |
| `openstudio_operator_singleton_election_total{outcome}` | counter (labelled) | `singleton` (`SingletonGuard.enforce` post-decode branches) · #239 | Singleton-guard election outcomes, incremented at the three branches in `SingletonGuard.enforce` (D05). `outcome=idle` when no OSCM CRs exist; `outcome=active` when exactly one CR exists and is served; `outcome=conflict` when >1 CRs exist and the oldest is served (the others get `SingletonConflict` Warning Events). Only fires on state changes — mirrors the existing change-gated log/Event noise channel so steady state is silent. Alert on sustained nonzero rate on `outcome=conflict` (a multi-CR namespace is a singleton-guard violation); also a critical signal when the guard is silently bypassed (the kopf registry internals change shape and `install_singleton_guard` returns 0 without the AST coverage test catching it) — the corruption is silent on the dashboard without this counter. |
| `openstudio_operator_singleton_loser_skips_total{module,namespace,name}` | counter (labelled) | `singleton` (`_gated` wrapper `if not active:` branch) · #403 | Per-tick singleton-guard loser suppressions — incremented on EVERY tick whose CR is not the oldest in the namespace (D05), before any side effect. The change-gated `singleton_election_total{outcome="conflict"}` is silent for a stable multi-CR namespace; this counter is the per-tick twin that makes the sustained loser load visible. Labelled by `module` (the wrapped handler's name — same vocabulary as `handler_tick_failures_total`), `namespace`, and `name` (the LOSER CR whose tick was suppressed); cardinality is bounded by the one-winner-per-namespace invariant (D05). **Alert: `rate(singleton_loser_skips_total[5m]) > 0` surfaces a sustained multi-CR configuration** — delete the loser CR (its Warning Event names the winner). |
| `openstudio_operator_events_emit_failures_total{reason}` | counter (labelled) | `events` (`EventEmitter.emit` try/except wrapper) · #255 | `kopf.event` posting failures caught by `EventEmitter.emit`'s try/except wrapper BEFORE re-raising. Labelled by `reason` — the warning-event reason the call site was attempting to post — so a dashboard can tell WHICH handler path's Event emission failed (same vocabulary as `events_emitted_total`). Sustained nonzero rate means the operator cannot post Kubernetes Events to the apiserver — distinct from the REST/Redis/K8s API signals that surface via `handler_tick_failures_total`. Alert: API server is unreachable for Events (vs REST/Redis/K8s). |
| `openstudio_operator_prune_tick_failures_total{reason}` | counter (labelled) | `prune_entrypoint` (storage-prune CronJob) · #306 | Skip-tick failures inside the storage-prune CronJob (`prune_entrypoint.main()`). Labelled by `reason` ∈ {`cr_list_failure`, `runtime_failure`, `redis_url_empty`} — the three bump sites: the K8s API CR list failure, the caught-exception branch, and the exit-3 empty-`spec.redisUrl` guard (#392 — the loud Failed-pod signal, wired to the same counter so a sustained redisUrl wedge — e.g. a chart upgrade dropping the redis-secret KeyRef — is visible at `/metrics`). The CronJob pod exposes the same `/metrics` endpoint on port 9090 as the operator, gated by the parallel `openstudio-storage-pruner-metrics-ingress` NetworkPolicy. Counter family mirrors the bounded-cardinality convention from #117. **Alert: `rate(prune_tick_failures_total[5m]) > 0` (#306 SLO) covers sustained wedging on the redisUrl guard too** — investigate the upstream cause (RBAC, apiserver, NFS, redis-secret KeyRef) before the storage-archival backlog grows. |
| `openstudio_operator_warnings_deferred_dropped_total{reason}` | counter (labelled) | `events_sinks` (`QueuedKopfEventSink.defer_to_next_tick` cap path) · #310 | `defer_to_next_tick` calls rejected by the sink's cap (MAX_DEFERRED_WARNING_EVENTS = 1000). Labelled by `reason` — initial vocabulary is `queue_full` (the only drop path today); the label leaves room for a future per-reason-cap branch without a Counter rename. Sustained nonzero rate means Warning Events are being silently lost — the apiserver watch stream is stalled and the queue hit the cap. Pair with `warnings_deferred_queue_depth` to see how close the queue is to the cap on subsequent ticks. |
| `openstudio_operator_rest_retries_total{method}` | counter (labelled) | `openstudio_client` (`_request` GET-only retry loop) · #471 | REST retry attempts, incremented once per RE-attempt (before the jittered backoff sleep) — a GET that fails twice with 5xx and succeeds on attempt 3 records exactly 2. Separates a retry storm from a slow success: the `rest_request_duration_seconds` histogram observes only the terminal outcome, so a retry-heavy degrade used to look like mildly slower `outcome="200"`s while the operator multiplied its own load up to 4x per poll. Labelled by `method` (`GET` \| `POST` \| `DELETE` — same vocabulary as the duration histogram; no CR-identity labels, the client is CR-agnostic). **Alert: `rate(openstudio_operator_rest_retries_total[5m]) > 0` is the early-degrade companion to `outcome="exception"`** — it fires while the degrade is still retry-recoverable, before retries exhaust into the exception rate. |
| `openstudio_operator_analysis_datapoint_count` | histogram | `analysis_sla` (Module 1) + `datapoint_watchdog` (Module 2) · #179, relabelled #472 | Per-tick counts observed by the SLA tick (analyses returned by the `/analyses.json` poll, governs soft-stop timing) and the watchdog tick (started datapoints from the light `/data_points/status` view, governs zombie requeue timing). **Labelled by `view`** (#472): `view="analyses_per_tick"` at the SLA site, `view="started_datapoints_per_tick"` at the watchdog site — the two populations have different units (analyses are typically an order of magnitude fewer than in-flight datapoints), so the pre-#472 unlabelled merge produced meaningless percentiles and silently reweighted on any cadence change. Dashboard queries MUST pin the `view` label; two series total, cardinality still bounded (per-observation, not per-CR). Buckets `[5, 10, 50, 100, 500, 1000, 5000]` — still surfacing the "we just started getting 5000-point analyses" shift. Lets an on-call correlate "why are SLA stops spiking?" with a shift in analysis-size distribution. |
| `openstudio_operator_handler_tick_duration_seconds` | histogram (labelled, `module`) | all four timer wrappers (`analysis_sla` / `datapoint_watchdog` / `worker_recycler` / `web_background_monitor`) · #308 | Per-`module` wall-clock duration of the four `@kopf.timer` wrappers, observed regardless of success or caught-exception outcome. Sustained degradation (REST 5xx storm, GC pause, kopf bus contention, NFS stall) is visible to Prometheus BEFORE it crosses the failure threshold captured by `handler_tick_failures_total`. Labelled by `module` (same vocabulary as the failure counter) so a dashboard can correlate latency with failure rate on the same dimension. Buckets `[0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60]` seconds — covers the healthy band (sub-second typical) through the action threshold (the timer wrappers run at cadence 30-60s; an observation > 60s means the tick crossed the next-cadence boundary). |
| `openstudio_operator_rest_request_duration_seconds` | histogram (labelled, `method`, `outcome`) | `openstudio_client` (`_request` retry envelope) · #308 | Per-`(method, outcome)` wall-clock duration of OpenStudioClient REST calls — includes the GET-only 3× retry envelope. `outcome` ∈ {`"200"`, `"exception"`} — `"200"` covers any successful 2xx/3xx (the success branch returns early); `"exception"` covers any raised `OpenStudioApiError`. Sustained non-zero rate on `outcome="exception"` is the canonical REST-degraded alert (REST 5xx, network, cluster down). Labelled by `method` ∈ {`GET`, `POST`, `DELETE`} (the verbs the operator actually uses). Buckets `(0.05, 0.1, 0.5, 1, 2, 5)` — the canonical set from #308; pinned so a future refactor that broadens or narrows the resolution at the healthy band is caught at CI. |
| `openstudio_operator_resque_workers_seen_max` | gauge | `web_background_monitor` (Module 4) · #44 / #87 | Monotonic max of distinct Resque worker ids ever observed in process lifetime (SMEMBERS `resque:workers` cardinality, read on **every** sensing tick since #87 regardless of queue depth). **`== 0` with reachable Redis means no workers are registered** — the leg-2 non-vacuity safeguard is then vacuously true and the operator will periodic-restart `web_background` while everything looks healthy. Alert on `== 0`. |
| `openstudio_operator_resque_queue_depth{queue}` | gauge (labelled) | `web_background_monitor` (`_stall_condition_holds` leg-A read) · #238 | LLEN of the two managed Resque queues (`resque:queue:simulations` and `resque:queue:requeued`) on **every** sensing tick (issue #87-style unconditional emission — the same path the stall-condition leg-A reads, no separate cost). Labelled by `queue` (cardinality bounded to the two managed queues — 2 total). Surfaces the operator's authoritative reading as a cross-check against KEDA's external metrics view — a centralized-constants / live v3.11.0 layout drift (#44/#66/#67) shows up as the operator's depths disagreeing with KEDA's. Alert on `simulations` > 0 sustained while `resque_workers_seen_max == 0` (the dangerous silent misbehavior signature). |
| `openstudio_operator_redis_key_layout_status` | gauge | `handlers` (`_check_redis_key_layout_for_cr` per-CR check) · #253 | Cluster-wide latest observation of the boot-time Redis key-layout validator (#163). `1.0` when the most recent `validate_key_layout()` call returned `ok`; `0.0` for every other terminal status (`degraded` \| `unreachable` \| `error` \| `skipped`). One series for the cluster-wide validator state (no per-CR labels — cardinality stays bounded regardless of CR count). Alert on `== 0` — the post-#44 failure mode (a v3.11.0 layout drift takes `resque_workers_seen_max` silent, the stall condition fires vacuously, and the operator periodic-restarts `web_background` while everything looks healthy) is observable here without log scraping. |
| `openstudio_operator_stall_window_elapsed_seconds` | gauge | `web_background_monitor` (`run_stall_tick` post-`tracker.observe()`) · #254 | Sustained-window elapsed seconds for the web_background stall. Set after `tracker.observe()` to the elapsed seconds when the stall condition held this tick, or `0` when it broke (the tracker resets). Rate > 0 means the window is accumulating toward a `web_background_restarts_total` increment; exact value shows how close to action (the action fires at `stallWindowMinutes`). Gives SREs a heads-up display between the first sustained observation and the eventual restart — without this gauge, three or more Redis/K8s-leg ticks can accumulate toward a restart with nothing on the dashboard until the gate trips. Blind-gap reset (Redis/K8s read failure) also clears the gauge to 0 so a stale value cannot survive across a degraded tick. |
| `openstudio_operator_resque_queue_depth_fresh` | gauge | `web_background_monitor` (`_stall_condition_holds` post-`queue_depths()`) · #312 | Last-successful-update Unix timestamp for the `resque_queue_depth` data gauge. Set to `time.time()` immediately after every successful `queue_depths()` Redis call — NOT touched on the exception path (Redis unreachable, ApiException, etc.). The data gauge advances on success but is a static stale value on failure; without this freshness pair, a prior tick's value masquerades as a live reading while the operator has in fact lost visibility. Dashboard query: `time() - openstudio_operator_resque_queue_depth_fresh` — alert on a sustained gap (e.g. > 5× the sensing tick cadence). |
| `openstudio_operator_stall_window_fresh` | gauge | `web_background_monitor` (`run_stall_tick` post-`tracker.observe()`) · #312 | Last-successful-update Unix timestamp for the `stall_window_elapsed_seconds` data gauge. Set to `time.time()` immediately after the `STALL_WINDOW_ELAPSED_SECONDS.set(...)` sequence on both the holding and broken paths. Unlabelled — one series (the reading site is unique, process-wide). Mirrors the `resque_queue_depth_fresh` round-trip pattern; the dashboard staleness computation `time() - fresh` works identically. Resetting only the freshness gauge (simulating "we lost visibility") leaves the data gauge holding its prior value — the exact failure mode #312 fixes. |
| `openstudio_operator_warnings_deferred_queue_depth` | gauge | `events_sinks` (`QueuedKopfEventSink.defer_to_next_tick` / `flush`) · #310 | Current depth of the in-process QueuedKopfEventSink queue. Unlabelled (the queue is process-wide, not per-CR) — cardinality stays bounded regardless of CR count. Set on every `defer` / `flush` call. Sustained nonzero values mean the apiserver watch stream is stalled and Warning Events are piling up — a companion to `warnings_deferred_dropped_total` which fires when the cap (MAX_DEFERRED_WARNING_EVENTS = 1000) is exceeded. Alert when the depth approaches the cap (e.g. > 80% of 1000) so the drop path can be diagnosed before silent loss starts. |
| `openstudio_operator_metrics_server_bound{addr,port}` | gauge (labelled) | `metrics` (`start_metrics_server` first bind attempt) · #393 | Outcome of the /metrics server's FIRST bind attempt: `1.0` on a successful bind, `0.0` on `OSError` (port already in use, unbindable address); never re-touched after the first attempt. Labelled by `addr` + `port` (the configured bind target — `0.0.0.0:9090` in the stock deployment, the same surface the `containerPort`, NetworkPolicy, and Prometheus scrape config reference). Covers the bind attempt in BOTH authN modes (open plaintext and the #401 bearer-token server share the single `except OSError` branch). **Alert on `== 0`: the canonical "Prometheus scrape is down because of US" signal** — it distinguishes "the metrics endpoint never bound" from "operator wedged / wrong scrape config" without log scraping for the `Cannot serve /metrics` WARNING. Self-referential edge: when the bind failed, this pod's `/metrics` is dead, so the `0.0` cannot be scraped from the pod itself — pair the alert with blackbox-exporter `up == 0` (the gauge is the durable record for post-mortems and confirms the operator-side cause). |

The labelled counters emit one series per label combo; only the observed
combos appear in the exposition (prometheus_client behaviour for labelled
counters without observations). `handler_tick_failures_total` is labelled by
`(module, error_type)` (4 modules × 4 error types = sixteen possible
series), `status_map_caps_total` by `map_name` (4), the two
EventEmitter counters by `reason` (7), `singleton_election_total` by
`outcome` (3 — `idle` | `active` | `conflict`), `events_emit_failures_total`
by `reason` (same 7 vocabulary as `events_emitted_total`),
`prune_tick_failures_total` by `reason` (3 — `cr_list_failure` |
`runtime_failure` | `redis_url_empty`), `warnings_deferred_dropped_total` by `reason` (1 today —
`queue_full`; the label is reserved for future drop reasons),
`singleton_loser_skips_total` by `(module, namespace, name)` (the module
vocabulary is the four handler names, same as `handler_tick_failures_total`;
the namespace × name cross-product is bounded by the singleton guard's
one-winner-per-namespace invariant, D05), and
`resque_queue_depth` by `queue` (2 — the two managed queues). The one
labelled gauge, `metrics_server_bound`, is labelled by `(addr, port)`
(1 series — the single first bind attempt; cardinality is fixed by design,
not bounded by an invariant). The labelled
histograms (`handler_tick_duration_seconds`, `rest_request_duration_seconds`)
follow the same convention — one labelled series per label combo. See each
row for the vocabulary.

**Quick triage commands:**

```bash
kubectl port-forward -n openstudio-server deploy/openstudio-operator 9090:9090 &
curl -s localhost:9090/metrics | grep -E '^openstudio_operator_'
curl -s localhost:9090/metrics | grep '^openstudio_operator_handler_tick_failures_total{' # labelled series
```

## Logs

The operator and the prune CronJob emit **one JSON object per log line** to
stderr (issue #256). The format is a stdlib `logging.Formatter` subclass
installed at process startup — `:func:`openstudio_operator.logging_setup.install_json_logging``
called from `handlers/__init__.py` (operator process) and from
`prune_entrypoint.py::main` (CronJob). kopf's own `kopf.objects` /
`kopf` loggers are NOT replaced; their records flow through the root
logger chain and are formatted as JSON by the same handler, so the
stream is uniformly machine-parseable.

**Canonical fields** (top-level keys in every record):

| Field | Type | Source | Notes |
|---|---|---|---|
| `timestamp` | string (ISO-8601 UTC) | `record.created` | tz-aware, e.g. `2026-08-19T14:23:45.123456+00:00`. Use this for log ordering, NOT container timestamps. |
| `level` | string | `record.levelname` | `INFO` / `WARNING` / `ERROR` / `DEBUG`. |
| `logger` | string | `record.name` | Dotted name; `openstudio_operator.handlers` for handler logs, `kopf.objects` for per-CR kopf adapter logs, `openstudio_operator.prune_entrypoint` for the CronJob. |
| `message` | string | `record.getMessage()` | The formatted message (args interpolated). |
| `module` | string | `record.module` | Filename without `.py` — useful for grepping back to a handler. |
| `funcName` | string | `record.funcName` | Function or method that emitted the record. |
| `lineno` | integer | `record.lineno` | Source line number. |

**kopf-injected fields** (present iff the record was emitted from
inside a kopf handler with the `ObjectLogger` adapter — true for every
CR-scoped log line):

| Field | Type | Notes |
|---|---|---|
| `namespace` | string | Flattened from `k8s_ref['namespace']`. Queryable in Loki as `{namespace="openstudio-server"}`. |
| `name` | string | Flattened from `k8s_ref['name']`. Queryable in Loki as `{name="osc-prod"}`. |

**Forward-compat:** any other non-reserved `extra=` field on the
underlying `logging.LogRecord` is emitted as a top-level key, so a
future handler that adds `extra={'analysis_id': '...'}` gets a free
`"analysis_id": "..."` field in the JSON line without a formatter
change.

**Example line** (from the boot-time Redis key-layout validator, #163):

```json
{"timestamp": "2026-08-19T14:23:45.123456+00:00", "level": "INFO", "logger": "openstudio_operator.handlers", "message": "redis_key_layout=ok namespace=openstudio-server name=osc-prod", "module": "handlers", "funcName": "_check_redis_key_layout_for_cr", "lineno": 202, "namespace": "openstudio-server", "name": "osc-prod"}
```

**Quick triage commands:**

```bash
# All handler log lines for a specific CR, in Loki syntax:
kubectl logs -n openstudio-server deploy/openstudio-operator \
  | jq -c 'select(.logger=="openstudio_operator.handlers" and .name=="osc-prod")'

# WARNING/ERROR only, with namespace context:
kubectl logs -n openstudio-server deploy/openstudio-operator \
  | jq -c 'select(.level=="WARNING" or .level=="ERROR") | {ts:.timestamp, level, namespace, name, msg:.message}'

# Prune-CronJob failure triage (logs ship from a completed Job):
kubectl logs -n openstudio-server job/openstudio-prune-<timestamp> \
  | jq -c 'select(.level!="INFO") | {ts:.timestamp, level, msg:.message}'
```

## Repository layout

```
.
├── .github/workflows/          # ci.yml (lint+test+branch guard), release.yml (GHCR + releases)
├── deploy/                     # CRD, RBAC, operator Deployment, KEDA, credential Secrets, CronJob, policies, alerting
│   ├── crd.yaml                # OpenStudioClusterManager CRD
│   ├── rbac.yaml               # operator Role (no HPA verbs, no batch verbs post-#77/#78)
│   ├── operator-deployment.yaml  # single-replica operator Deployment (strategy: Recreate)
│   ├── keda-scaledobject.yaml  # KEDA ScaledObject + TriggerAuthentication (#77)
│   ├── redis-credentials-secret.yaml  # Redis password Secret for KEDA (#77; committed value is the unusable sentinel, #462 — rotate before use)
│   ├── mongo-credentials-secret.yaml  # Mongo credentials Secret for the web/db auth boundary (#219; committed value is the unusable sentinel, #462 — rotate before use)
│   ├── storage-cronjob.yaml    # prune CronJob (#78)
│   ├── network-policy.yaml     # NetworkPolicy for the operator surface + /metrics ingress allow (#112, #166)
│   ├── pod-delete-admission-policy.yaml  # cluster-scoped ValidatingAdmissionPolicy narrowing pods/delete (#293)
│   ├── priority-class.yaml     # PriorityClass for operator Deployment + prune CronJob (#414)
│   ├── resource-quota.yaml     # ResourceQuota + LimitRange for the openstudio-server namespace (#400)
│   ├── prometheustrule.yaml    # PrometheusRule alert definitions for the /metrics surface (#468)
│   └── grafana-dashboard.json  # Grafana dashboard JSON — action counters, tick histograms, Resque gauges (#468)
├── docs/                       # audit-dryrun-idempotency.md, validation.md, kind-validation.md, contracts/
├── scripts/                    # kind cluster recipe + fixture capture + drift checker
├── src/openstudio_operator/
│   ├── _constants.py           # Operator-behavior constants (polling cadences, metrics port, Resque-key-layout grace); single source of truth — policy values do NOT live here (#165)
│   ├── _time.py                # tz-aware UTC parser (`parse_utc`); None-safe; replaces three byte-equivalent duplicates (#174)
│   ├── _k8s.py                 # Shared Kubernetes API helpers (`DeploymentReader`, `deployment_label_selector`); neutral home for cross-handler K8s surface (#236, #250)
│   ├── _oscm_handlers.py       # Python-level OSCM handler registry; new handlers call `register_fn(fn)` at import (id = fn.__name__, #407); singleton guard cross-checks against the kopf registry at gate time (#250)
│   ├── config.py               # CRD spec → typed settings (defaults mirror deploy/crd.yaml)
│   ├── openstudio_client.py    # OpenStudio REST client (verified against v3.11.0)
│   ├── client_factory.py       # `lru_cache`-keyed factories for `OpenStudioClient` + `ReadOnlyRedisClient`; a mutated `spec.serverUrl` / `spec.redisUrl` invalidates by a different key (#168, #235)
│   ├── status_store.py         # CR .status RMW helper (D04 durable store, 409-safe)
│   ├── redis_client.py         # read-only Redis client (queue depths + Resque liveness)
│   ├── archival.py             # rclone archival Job manifest generator (backend-agnostic)
│   ├── retention.py            # prune pipeline (invoked by storage-cronjob.yaml; #78)
│   ├── prune_entrypoint.py     # CronJob entrypoint for prune (entry_points = prune_entrypoint:run)
│   ├── singleton.py            # passive oldest-CR-per-namespace guard (D05)
│   ├── metrics.py              # Prometheus counters + gauges + histograms + /metrics endpoint (20+8+3)
│   ├── logging_setup.py        # JSON `logging.Formatter` + idempotent installer (#256); called from `handlers/__init__.py` (operator) and `prune_entrypoint.py::main` (CronJob)
│   ├── events.py               # `EventEmitter` class (one instance per tick); the dry-run gate (D11) + suppressed-event counter live here, not at call sites (#164)
│   ├── events_sinks.py         # `QueuedKopfEventSink` — collapses the three near-identical queue/drain mechanisms from `handlers/__init__.py` (#234)
│   └── handlers/               # Kopf handlers, one file per plan module
│       ├── analysis_sla.py
│       ├── datapoint_watchdog.py
│       ├── worker_recycler.py
│       └── web_background_monitor.py
│       # (storage_pruner moved to the prune CronJob in #78;
│       #  hpa_floor removed in #77 — autoscaling is a KEDA ScaledObject.)
├── tests/                      # unit tests for every handler + client + status store + fixtures
│   ├── fixtures/               # contract-shapes.json + samples/ (synthetic) + live/ (captured)
│   └── golden/                 # snapshot tests for generated rclone Job manifests
├── Dockerfile
└── pyproject.toml
```

## Branching model

- `develop` — **default branch**; all work lands here (direct pushes allowed).
- `main` — **release branch**; protected by the repository ruleset `protect-main`:
  - direct pushes and force pushes are rejected (for everyone, including admins),
  - deletions are blocked,
  - updates require a pull request, and the required `guard-branch-pairing` CI check
    fails any PR into `main` whose source branch is not `develop`.

  Release flow: `develop` → open PR to `main` → CI guard passes → merge.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

ruff check .     # lint
pytest           # tests
kopf run --module openstudio_operator.handlers --namespace openstudio-server   # run (needs cluster + CRD)

```

Kubernetes manifests (CRD, least-privilege RBAC, operator Deployment) live in `deploy/`.

## CI

- **ci.yml** — `ruff` + `pytest` on pushes to `develop` and PRs into `develop`/`main`; the `guard-branch-pairing` job enforces the `main` ← `develop` policy.
- **release.yml** — publishes `ghcr.io/anchapin/openstudio-server-operator:dev` on pushes to `develop`; pushes of `v*` tags publish a versioned image and create a GitHub Release. The workflow currently targets `linux/amd64`; arm64 is a follow-up.
