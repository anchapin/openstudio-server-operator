# AGENTS.md

Guidance for AI coding agents working in this repository.

## Repository status

- **Scaffold stage.** Framework decision: **Python + Kopf** (decided 2026-08-18). Handler stubs map 1:1 to plan modules and are marked `TODO(phase N)`; the authoritative spec remains `OpenStudio Server Operator Architecture & Implementation Plan.md` — read it before implementing.
- The plan filename contains spaces and `&`. Always quote it in shell commands: `cat "OpenStudio Server Operator Architecture & Implementation Plan.md"`.

## What this project is

A Kubernetes operator for [OpenStudio Server](https://github.com/NREL/OpenStudio-server) (Ruby/Rails `web` + `web_background` + MongoDB + `worker` pods + NFS-shared volumes). It runs *alongside* the existing `openstudio-server-helm` deployment and automates: analysis timeout soft-stops, zombie datapoint requeues, worker pod recycling, NFS pruning with S3/GCS archival, and `web_background` queue-stall detection. It manages that stack; it does not replace it.

## Branching model

- `develop` — default branch; commit directly there.
- `main` — release branch, protected by ruleset `protect-main`: direct pushes, force pushes, and deletions are rejected for everyone (no bypass, including the owner); it only changes via a PR **from `develop`**. The required `guard-branch-pairing` CI check fails any PR into `main` whose source branch is not `develop`.
- Never attempt to push `main` directly — it is rejected by design (verified).

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

ruff check .        # lint (CI enforces)
pytest              # tests (CI enforces)
kopf run --module openstudio_operator.handlers --namespace openstudio   # run (needs cluster + CRD)
```

## Layout

- `src/openstudio_operator/handlers/` — one file per plan module: `analysis_sla`, `datapoint_watchdog`, `worker_recycler`, `storage_pruner`, `web_background_monitor`
- `src/openstudio_operator/config.py` — CRD spec → typed settings (defaults must stay in sync with `deploy/crd.yaml`)
- `src/openstudio_operator/openstudio_client.py` — OpenStudio REST client
- `deploy/` — CRD, least-privilege RBAC (namespaced **Role**, never a ClusterRole; verbs enumerated per the plan), operator Deployment
- `.github/workflows/ci.yml` — lint + test + `guard-branch-pairing`; keep that job name stable — it is a **required status check on `main`**

## Fixed identifiers (from the CRD/RBAC spec — exact spelling matters)

- CRD: `openstudioclustermanagers.energy.nrel.gov` · group `energy.nrel.gov` · version `v1alpha1` · kind `OpenStudioClusterManager`
- Namespace: `openstudio` · ServiceAccount: `openstudio-operator-sa`

## OpenStudio REST API contract used by the operator

- `GET /analyses.json` (poll ~30s), `GET /data_points.json?status=started`, `GET /cluster.json`
- `PUT /analyses/{id}/action` with `{"action": "soft_stop"}`; escalation actions are `kill` / `hard_stop`
- `POST /data_points/{id}/requeue` · `DELETE /analyses/{id}`

## Build order

Follow the plan's phase sequence — later phases depend on earlier ones:

1. CRD + analysis SLA monitor + soft-stop caller + K8s Event emission (`AnalysisSoftStopped`, `WorkerRecycled`)
2. Zombie datapoint watchdog + auto-requeue + post-analysis worker rolling restart + `web_background` stall detector
3. S3/GCS archival (ephemeral K8s Jobs mounted to the NFS PV) + NFS cleanup + API object deletion
4. KEDA autoscaling (MongoDB queue depth / custom metrics) + Prometheus `/metrics` (counters already declared in `src/openstudio_operator/metrics.py`) + RBAC tightening

## Working rules

- Policy values (timeouts, requeue limits, recycle intervals, archive settings) belong in the CRD `spec` / `config.py` — configuration, not hardcoded constants.
- Keep this file accurate: update when facts change, delete what tooling now answers.
