# OpenStudio Server Operator

A Kubernetes operator that automates day-2 operations for [OpenStudio Server](https://github.com/NREL/OpenStudio-server) deployments (Ruby/Rails `web` + `web_background` + MongoDB + `worker` pods + NFS-shared volumes). It runs **alongside** the existing [`openstudio-server-helm`](https://github.com/NREL/openstudio-server-helm) chart — it manages that stack; it does not replace it.

**Status: implementation complete (Phases 1–4, issues #2–#21); live kind-cluster validation done for modules 2/3/5/HPA-floor/singleton and the full-module `dryRun: true` walkthrough (#66/#67/#84); D1/D2 contract drift follow-up tracked in #83.** The verified API ground truth is [`docs/contracts/openstudio-server-v3.11.0-rest.md`](./docs/contracts/openstudio-server-v3.11.0-rest.md). Cross-cutting audit: [`docs/audit-dryrun-idempotency.md`](./docs/audit-dryrun-idempotency.md). Cluster validation runbook: [`docs/validation.md`](./docs/validation.md). Framework: **Python + [Kopf](https://kopf.readthedocs.io/)**.

## What it will automate

| Module | Plan phase | Purpose |
|---|---|---|
| Analysis SLA / soft-stop | 1 | Soft-stop analyses exceeding `maxDurationMinutes`; after `gracefulStopTimeoutMinutes`, surgically evict worker pods whose IP matches a started datapoint's `ip_address` (gated by `analysisPolicy.forceDeleteOnEscalation`) |
| Zombie datapoint watchdog | 2 | Auto-requeue datapoints stalled past `maxDatapointRuntimeMinutes` (bounded by `maxAutoRequeues`) |
| Worker recycler | 2 | Rolling-restart the worker Deployment after analyses / on interval |
| web_background watchdog | 2 | Detect Resque queue stalls; restart the `web_background` Deployment |
| Storage archiver & NFS pruner | 3 | Ephemeral Jobs on the NFS PV archive results to S3/GCS, then prune |
| HPA-floor adjuster + `/metrics` | 4 | Raise the chart `worker-hpa` `minReplicas` floor from Redis backlog; expose operator Prometheus metrics |

### Autoscaling approach (D10)

The operator deliberately does **not** deploy KEDA (or any second autoscaler): the helm chart ships an unconditional CPU-based HPA (`worker-hpa`), and two autoscalers fighting over one Deployment oscillate. Instead, the Phase-4 HPA-floor adjuster reads the Resque backlog (`simulations` + `requeued` depths, summed, read-only via Redis), maps it through a tiered policy table in `config.py`, and patches **only** `worker-hpa` `spec.minReplicas` — raising the floor when the backlog is deep, decaying to the baseline floor when it clears — behind a cooldown that prevents flapping and under `spec.dryRun` gating. KEDA remains the documented **future migration path**: a `ScaledObject` driving the `worker` Deployment from the same Redis list-length trigger would replace this adjuster entirely and is the preferred design if the chart ever makes its HPA conditional.

## Repository layout

```
.
├── .github/workflows/          # ci.yml (lint+test+branch guard), release.yml (GHCR + releases)
├── deploy/                     # CRD, RBAC (namespaced Role only), operator Deployment
├── docs/                       # audit-dryrun-idempotency.md, validation.md, kind-validation.md
├── scripts/                    # kind cluster recipe + fixture capture + drift checker
├── src/openstudio_operator/
│   ├── config.py               # CRD spec → typed settings (defaults mirror deploy/crd.yaml)
│   ├── openstudio_client.py    # OpenStudio REST client (verified against v3.11.0)
│   ├── status_store.py         # CR .status RMW helper (D04 durable store, 409-safe)
│   ├── redis_client.py         # read-only Redis client (queue depths + Resque liveness)
│   ├── archival.py             # rclone archival Job manifest generator (backend-agnostic)
│   ├── singleton.py            # passive oldest-CR-per-namespace guard (D05)
│   ├── metrics.py              # Prometheus counters + /metrics endpoint
│   └── handlers/               # Kopf handlers, one file per plan module
│       ├── analysis_sla.py
│       ├── datapoint_watchdog.py
│       ├── worker_recycler.py
│       ├── storage_pruner.py
│       ├── web_background_monitor.py
│       └── hpa_floor.py
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
