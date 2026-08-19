# OpenStudio Server Operator

A Kubernetes operator that automates day-2 operations for [OpenStudio Server](https://github.com/NREL/OpenStudio-server) deployments (Ruby/Rails `web` + `web_background` + MongoDB + `worker` pods + NFS-shared volumes). It runs **alongside** the existing [`openstudio-server-helm`](https://github.com/NREL/openstudio-server-helm) chart — it manages that stack; it does not replace it.

**Status: implementation complete (Phases 1–4, issues #2–#21); live kind-cluster validation done for modules 2/3/5/singleton and the full-module `dryRun: true` walkthrough (#66/#67/#84); Phase-4 autoscaling is now driven by a standard KEDA ScaledObject (#77, replacing the custom HPA-floor adjuster #18); D1/D2 contract drift follow-up tracked in #83.** The verified API ground truth is [`docs/contracts/openstudio-server-v3.11.0-rest.md`](./docs/contracts/openstudio-server-v3.11.0-rest.md). Cross-cutting audit: [`docs/audit-dryrun-idempotency.md`](./docs/audit-dryrun-idempotency.md). Cluster validation runbook: [`docs/validation.md`](./docs/validation.md). Framework: **Python + [Kopf](https://kopf.readthedocs.io/)**.

## What it will automate

| Module | Plan phase | Purpose |
|---|---|---|
| Analysis SLA / soft-stop | 1 | Soft-stop analyses exceeding `maxDurationMinutes`; after `gracefulStopTimeoutMinutes`, surgically evict worker pods whose IP matches a started datapoint's `ip_address` (gated by `analysisPolicy.forceDeleteOnEscalation`) |
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
