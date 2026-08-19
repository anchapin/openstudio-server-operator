# OpenStudio Server Operator

A Kubernetes operator that automates day-2 operations for [OpenStudio Server](https://github.com/NREL/OpenStudio-server) deployments (Ruby/Rails `web` + `web_background` + MongoDB + `worker` pods + NFS-shared volumes). It runs **alongside** the existing [`openstudio-server-helm`](https://github.com/NREL/openstudio-server-helm) chart — it manages that stack; it does not replace it.

**Status: implementation complete (Phases 1–4, issues #2–#21); live kind-cluster validation done for modules 2/3/5/singleton and the full-module `dryRun: true` walkthrough (#66/#67/#84); Phase-4 autoscaling is now driven by a standard KEDA ScaledObject (#77, replacing the custom HPA-floor adjuster #18); D1/D2 contract drift resolved by #96 (Resque-worker identity path — see Module 1 row).** The verified API ground truth is [`docs/contracts/openstudio-server-v3.11.0-rest.md`](./docs/contracts/openstudio-server-v3.11.0-rest.md). Cross-cutting audit: [`docs/audit-dryrun-idempotency.md`](./docs/audit-dryrun-idempotency.md). Cluster validation runbook: [`docs/validation.md`](./docs/validation.md). First-time contributors and AI agents: start at [`docs/onboarding.md`](./docs/onboarding.md) (issue #178). Framework: **Python + [Kopf](https://kopf.readthedocs.io/)**.

Changelog: [CHANGELOG.md](./CHANGELOG.md) — curated per-release notes (Keep a Changelog format; see issue #177).

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
3. `kubectl apply -f deploy/redis-credentials-secret.yaml` (Redis
   password for KEDA's TriggerAuthentication — operator never sees it).
4. `kubectl apply -f deploy/keda-scaledobject.yaml`.

The operator's Role has no `horizontalpodautoscalers` verbs; autoscaling
is owned by KEDA's own ServiceAccount. Same RBAC-shrink pattern as #78
(storage moved to a prune CronJob SA).

## Metrics

The operator exposes a Prometheus scrape endpoint at `:9090/metrics` (plain
HTTP, no auth — see `src/openstudio_operator/metrics.py`). `/metrics` is the
**only** direct observability window into operator health: every Kopf timer
wrapper catches its failure tuples and silently skips the tick (D04 idempotency
survives), so a sustained degraded window — REST API down, Redis unreachable,
k8s API unavailable — only surfaces as one of these counters, never as a pod
restart. The scrape target is the operator Pod on port 9090 (matches the
`containerPort` in `deploy/operator-deployment.yaml`).

**Source of truth:** the family names below mirror
`tests/test_metrics_endpoint.py::EXPECTED_COUNTER_FAMILIES`,
`EXPECTED_GAUGE_FAMILIES`, and `EXPECTED_HISTOGRAM_FAMILIES` exactly —
the test asserts `declared == expected` on every CI run, so adding a counter,
gauge, or histogram here without adding it there (or vice versa) fails CI
loudly. **Current shape: 12 counters + 1 gauge + 1 histogram (post-#171
status-map defensive cap; post-#179 datapoint-budget distribution).**

| Family | Type | Module / issue origin | Meaning for an on-call |
|---|---|---|---|
| `openstudio_operator_soft_stops_total` | counter | `analysis_sla` (Module 1) · #8 | Analyses soft-stopped in this process lifetime. Rising during normal SLA enforcement is expected. A long stall with rising analysis runtimes means the SLA clock anchor isn't being written (D04 / #83 D1 regression). |
| `openstudio_operator_worker_pods_evicted_total` | counter | `analysis_sla` (Module 1) · #9 | Surgically evicted worker pods during SLA escalation (counts past `gracefulStopTimeoutMinutes`). Dry-run ticks increment too. Non-zero means SLA escalations are firing — cross-check with `lastWebBackgroundRestart` and `.status.softStops`. |
| `openstudio_operator_datapoints_requeued_total` | counter | `datapoint_watchdog` (Module 2) · #10 | Zombie datapoints auto-requeued. A burst signals a worker fleet problem or a new job class that doesn't update liveness. Bounded by `spec.datapointPolicy.maxAutoRequeues`. |
| `openstudio_operator_datapoints_requeue_exhausted_total` | counter | `datapoint_watchdog` (Module 2) · #10 | Datapoints abandoned after exceeding `maxAutoRequeues`. Should be near-zero in steady state; any nonzero rate means real jobs are dying past the auto-requeue budget. |
| `openstudio_operator_workers_recycled_total` | counter | `worker_recycler` (Module 3) · #11 | Worker Deployment rolling-restarts issued by the recycler. Spikes imply the worker fleet is misbehaving (CRASHLOOP, OOM); a steady cadence is normal. |
| `openstudio_operator_web_background_restarts_total` | counter | `web_background_monitor` (Module 4) · #13 | `web_background` Deployment restarts after sustained queue stalls. Alert on any growth — Resque plumbing is broken or v3.11.0 key layout drifted (#44 / #87). |
| `openstudio_operator_analyses_archived_total` | counter | `retention` (storage pruner) · #16 | Analyses whose archival Job passed rclone verification (including adopted completions). Steady growth = healthy storage archival. Adopted = the operator saw a Job completion it didn't start, counted anyway. |
| `openstudio_operator_analyses_deleted_total` | counter | `retention` (storage pruner) · #16 | Analyses deleted after verified archival. In healthy operation, this should track `analyses_archived_total` minus the in-progress backlog. `spec.dryRun` suppresses both the delete and the increment. |
| `openstudio_operator_status_conflicts_total` | counter | `status_store` (RMW helper) · #119 | Per-attempt 409 responses from the Kubernetes API Server during CR `.status` RMW cycles (incremented inside `_mutate` for each 409 before the backoff sleep). Sustained nonzero rate means multiple operators are racing; investigate the singleton guard (#14). |
| `openstudio_operator_status_conflict_retries_exhausted_total` | counter | `status_store` (RMW helper) · #119 | RMW cycles that exhausted the 409 retry budget and raised `StatusStoreConflictError` — the tick that hit this counter was skipped (WARNING log line, no `.status` write). Alert: a CR status write was lost. |
| `openstudio_operator_handler_tick_failures_total{module,error_type}` | counter (labelled) | all four timer wrappers (`analysis_sla` / `datapoint_watchdog` / `worker_recycler` / `web_background_monitor`) · #117 | Per-tick failures caught by the timer wrappers. Increment-by-1 per tick suppressed. Labelled by `module` and `error_type` (`OpenStudioApiError` \| `StatusStoreError` \| `ApiException` \| `RedisClientError`). Sustained nonzero per `(module, error_type)` tells you which downstream — REST, Redis, k8s API — is degraded. |
| `openstudio_operator_status_map_caps_total{map_name}` | counter (labelled) | `status_store` (`_set_map_entry` cap path) · #171 | Evictions triggered by the per-map defensive cap (drop-oldest when a map reaches `STATUS_MAP_MAX_ENTRIES = 10_000`). Labelled by `map_name` ∈ {`softStops`, `requeues`, `startedSince`, `archivedAnalyses`}. Sustained nonzero means something is filling the maps faster than they drain (e.g. an admin batch-`create` of 10k+ draft analyses) — investigate the upstream cause, don't raise the cap. |
| `openstudio_operator_analysis_datapoint_count` | histogram | `analysis_sla` (Module 1) + `datapoint_watchdog` (Module 2) · #179 | Datapoints-per-analysis distribution observed by the SLA tick (`/analyses/{id}` page count, governs soft-stop timing) and the watchdog tick (`/data_points.json?analysis_id=...` started-datapoint count, governs zombie requeue timing). Buckets `[5, 10, 50, 100, 500, 1000, 5000]` — bounded cardinality while still surfacing the "we just started getting 5000-point analyses" shift. **No labels:** per-observation by design (one `.observe()` per observed count, not per-CR) — relabelling per analysis would multiply series cardinality by the analysis count and defeat the bounded-cardinality design, so the `an` and `dp` source-module identity is intentionally collapsed. Lets an on-call correlate "why are SLA stops spiking?" with a shift in analysis-size distribution. |
| `openstudio_operator_resque_workers_seen_max` | gauge | `web_background_monitor` (Module 4) · #44 / #87 | Monotonic max of distinct Resque worker ids ever observed in process lifetime (SMEMBERS `resque:workers` cardinality, read on **every** sensing tick since #87 regardless of queue depth). **`== 0` with reachable Redis means no workers are registered** — the leg-2 non-vacuity safeguard is then vacuously true and the operator will periodic-restart `web_background` while everything looks healthy. Alert on `== 0`. |

The one labelled counter (`handler_tick_failures_total`) emits one series per
`(module, error_type)` pair; the four handler modules × four error types =
sixteen possible series, of which only the observed ones appear in the
exposition (prometheus_client behaviour for labelled counters without
observations).

**Quick triage commands:**

```bash
kubectl port-forward -n openstudio-server deploy/openstudio-operator 9090:9090 &
curl -s localhost:9090/metrics | grep -E '^openstudio_operator_'
curl -s localhost:9090/metrics | grep '^openstudio_operator_handler_tick_failures_total{' # labelled series
```

## Repository layout

```
.
├── .github/workflows/          # ci.yml (lint+test+branch guard), release.yml (GHCR + releases)
├── deploy/                     # CRD, RBAC (namespaced Role only), operator Deployment, KEDA, CronJob
│   ├── crd.yaml                # OpenStudioClusterManager CRD
│   ├── rbac.yaml               # operator Role (no HPA verbs, no batch verbs post-#77/#78)
│   ├── operator-deployment.yaml
│   ├── keda-scaledobject.yaml  # KEDA ScaledObject + TriggerAuthentication (#77)
│   ├── redis-credentials-secret.yaml  # Redis password for KEDA (#77)
│   └── storage-cronjob.yaml    # prune CronJob (#78)
├── docs/                       # audit-dryrun-idempotency.md, validation.md, kind-validation.md, contracts/
├── scripts/                    # kind cluster recipe + fixture capture + drift checker
├── src/openstudio_operator/
│   ├── config.py               # CRD spec → typed settings (defaults mirror deploy/crd.yaml)
│   ├── openstudio_client.py    # OpenStudio REST client (verified against v3.11.0)
│   ├── status_store.py         # CR .status RMW helper (D04 durable store, 409-safe)
│   ├── redis_client.py         # read-only Redis client (queue depths + Resque liveness)
│   ├── archival.py             # rclone archival Job manifest generator (backend-agnostic)
│   ├── retention.py            # prune pipeline (invoked by storage-cronjob.yaml; #78)
│   ├── prune_entrypoint.py     # CronJob entrypoint for prune (entry_points = prune_entrypoint:run)
│   ├── singleton.py            # passive oldest-CR-per-namespace guard (D05)
│   ├── metrics.py              # Prometheus counters + /metrics endpoint
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
