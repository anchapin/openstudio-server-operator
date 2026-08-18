# OpenStudio Server Operator

A Kubernetes operator that automates day-2 operations for [OpenStudio Server](https://github.com/NREL/OpenStudio-server) deployments (Ruby/Rails `web` + `web_background` + MongoDB + `worker` pods + NFS-shared volumes). It runs **alongside** the existing [`openstudio-server-helm`](https://github.com/NREL/openstudio-server-helm) chart — it manages that stack; it does not replace it.

**Status: pre-implementation scaffold.** The authoritative spec is [`OpenStudio Server Operator Architecture & Implementation Plan.md`](./OpenStudio%20Server%20Operator%20Architecture%20&%20Implementation%20Plan.md). Code modules are placeholders mapped to plan phases. Framework: **Python + [Kopf](https://kopf.readthedocs.io/)**.

## What it will automate

| Module | Plan phase | Purpose |
|---|---|---|
| Analysis SLA / soft-stop | 1 | Soft-stop analyses exceeding `maxDurationMinutes`; escalate to `kill`/`hard_stop` after grace period |
| Zombie datapoint watchdog | 2 | Auto-requeue datapoints stalled past `maxDatapointRuntimeMinutes` (bounded by `maxAutoRequeues`) |
| Worker recycler | 2 | Rolling-restart the worker Deployment after analyses / on interval |
| web_background watchdog | 2 | Detect Resque queue stalls; restart the `web_background` Deployment |
| Storage archiver & NFS pruner | 3 | Ephemeral Jobs on the NFS PV archive results to S3/GCS, then prune |
| KEDA autoscaling + `/metrics` | 4 | Scale workers on queue depth; expose operator Prometheus metrics |

## Repository layout

```
.
├── .github/workflows/          # ci.yml (lint+test+branch guard), release.yml (placeholder)
├── deploy/                     # CRD, RBAC (namespaced Role only), operator Deployment
├── src/openstudio_operator/
│   ├── config.py               # CRD spec → typed settings
│   ├── openstudio_client.py    # OpenStudio REST client (action spellings matter)
│   ├── metrics.py              # Prometheus counters (Phase 4)
│   └── handlers/               # Kopf handlers, one file per plan module
├── tests/                      # structural smoke tests
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
kopf run --module openstudio_operator.handlers --namespace openstudio   # run (needs cluster + CRD)
```

Kubernetes manifests (CRD, least-privilege RBAC, operator Deployment) live in `deploy/`.

## CI

- **ci.yml** — `ruff` + `pytest` on pushes to `develop` and PRs into `develop`/`main`; the `guard-branch-pairing` job enforces the `main` ← `develop` policy.
- **release.yml** — placeholder for image build/publish on `main` updates.
