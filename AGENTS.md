# AGENTS.md

Guidance for AI coding agents working in this repository.

## Repository status

- **Pre-implementation.** There is no code yet. The sole file — `OpenStudio Server Operator Architecture & Implementation Plan.md` — is the authoritative spec. Read it before writing anything.
- Not a git repository yet (no history, no branch/PR conventions to follow).
- The plan filename contains spaces and `&`. Always quote it in shell commands: `cat "OpenStudio Server Operator Architecture & Implementation Plan.md"`.

## What this project is

A Kubernetes operator for [OpenStudio Server](https://github.com/NREL/OpenStudio-server) (Ruby/Rails `web` + `web_background` + MongoDB + `worker` pods + NFS-shared volumes). It runs *alongside* the existing `openstudio-server-helm` deployment and automates: analysis timeout soft-stops, zombie datapoint requeues, worker pod recycling, NFS pruning with S3/GCS archival, and `web_background` queue-stall detection. It manages that stack; it does not replace it.

## Open decision — confirm before scaffolding

- Framework is **undecided in the plan**: Python **Kopf** or Go **Kubebuilder**. Ask the user which one before generating any framework scaffolding. Do not pick silently.

## Fixed identifiers (from the CRD/RBAC spec — exact spelling matters)

- CRD: `openstudioclustermanagers.energy.nrel.gov` · group `energy.nrel.gov` · version `v1alpha1` · kind `OpenStudioClusterManager`
- Namespace: `openstudio` · ServiceAccount: `openstudio-operator-sa`
- RBAC is least-privilege by design: a namespaced **Role**, never a ClusterRole; verbs are enumerated in `operator_rbac.yaml` in the plan. Keep it that way when code lands.

## OpenStudio REST API contract used by the operator

- `GET /analyses.json` (poll ~30s), `GET /data_points.json?status=started`, `GET /cluster.json`
- `PUT /analyses/{id}/action` with `{"action": "soft_stop"}`; escalation actions are `kill` / `hard_stop`
- `POST /data_points/{id}/requeue` · `DELETE /analyses/{id}`

## Build order

Follow the plan's phase sequence — later phases depend on earlier ones:

1. CRD + analysis SLA monitor + soft-stop caller + K8s Event emission (`AnalysisSoftStopped`, `WorkerRecycled`)
2. Zombie datapoint watchdog + auto-requeue + post-analysis worker rolling restart + `web_background` stall detector
3. S3/GCS archival (ephemeral K8s Jobs mounted to the NFS PV) + NFS cleanup + API object deletion
4. KEDA autoscaling (MongoDB queue depth / custom metrics) + Prometheus `/metrics` (soft-stops, requeues, storage freed) + RBAC tightening

## When code lands

- Add real build/test/run commands here as soon as they exist; until then, do not invent or guess them.
- Policy values (timeouts, requeue limits, recycle intervals, archive settings) belong in the CRD `spec` as defined there — configuration, not hardcoded constants.
- This file should shrink as tooling matures: keep only what an agent would otherwise get wrong.
