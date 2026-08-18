# AGENTS.md

Guidance for AI coding agents working in this repository.

## Repository status

- **Scaffold stage.** Framework decision: **Python + Kopf** (decided 2026-08-18). Handler stubs map 1:1 to plan modules and are marked `TODO(phase N)`; every `OpenStudioClient` method raises `NotImplementedError`. A full design review (2026-08-18 "grill session") locked decisions D01–D13 (`.agents/results/plan-grill.json`) and exported the work as GitHub issues **#2–#21** — check the issue tracker before starting work.
- **API ground truth is `.agents/skills/_shared/api-contracts/openstudio-server-v3.11.0-rest.md`**, verified against NREL/OpenStudio-server tag `v3.11.0` and the NatLabRockies helm chart `develop` branch. The plan doc predates verification; where it conflicts with the contract file, **the contract file wins** (see its "Non-existent / legacy" section for the traps: no `PUT action`, no `kill`/`hard_stop`, no `/cluster.json`). The plan filename still contains spaces and `&` — always quote it in shell commands.
- The operator image (`ghcr.io/anchapin/openstudio-server-operator:dev`) is unpublished; `.github/workflows/release.yml` is a placeholder. Tests are mocked — CI green means nothing against a real cluster. Validation strategy (D13): kind + real `3.11.0` images on a dev machine (#19), then a work-cluster dry-run runbook (#20).

## What this project is

A Kubernetes operator for [OpenStudio Server](https://github.com/NREL/OpenStudio-server) (Ruby/Rails `web` + `web_background` + MongoDB + `worker` pods + NFS-shared volumes). It runs *alongside* the existing `openstudio-server-helm` deployment (NatLabRockies helm chart, `develop` branch, namespace `openstudio-server`, server images pinned to `3.11.0` — a user override, not the chart default) and automates: analysis timeout soft-stops, zombie datapoint requeues, worker pod recycling, NFS pruning with S3/GCS/Azure archival, and `web_background` queue-stall detection. It manages that stack; it does not replace it.

## Branching model

- `develop` — default branch; commit directly there.
- `main` — release branch, protected by ruleset `protect-main`: direct pushes, force pushes, and deletions are rejected for everyone (no bypass, including the owner); it only changes via a PR **from `develop`**. The required `guard-branch-pairing` CI check fails any PR into `main` whose source branch is not `develop`.
- Never attempt to push `main` directly — it is rejected by design (verified).

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

ruff check .        # lint (CI enforces; line-length 100 per pyproject)
pytest              # tests (CI enforces)
kopf run --module openstudio_operator.handlers --namespace openstudio-server   # run (needs cluster + CRD; #3 fixes manifests that still say `openstudio`)
```

- Mock the OpenStudio REST API in tests with the `responses` library (in the `dev` extra for exactly this) — don't add another HTTP-mocking dependency. The Redis client (#12) is mocked with `fakeredis` for the same reason.

## Layout

- `src/openstudio_operator/handlers/` — one file per plan module: `analysis_sla`, `datapoint_watchdog`, `worker_recycler`, `storage_pruner`, `web_background_monitor`
- `src/openstudio_operator/config.py` — CRD spec → typed settings (defaults must stay in sync with `deploy/crd.yaml`)
- `src/openstudio_operator/openstudio_client.py` — OpenStudio REST client
- `src/openstudio_operator/metrics.py` — Prometheus counters declared up front for Phase 4
- `deploy/crd.yaml` · `deploy/rbac.yaml` · `deploy/operator-deployment.yaml` — CRD, least-privilege RBAC (namespaced **Role**, never a ClusterRole; verbs enumerated per the plan), operator Deployment
- `.github/workflows/ci.yml` — lint + test + `guard-branch-pairing`; keep that job name stable — it is a **required status check on `main`**

## Fixed identifiers (from the CRD/RBAC spec — exact spelling matters)

- CRD: `openstudioclustermanagers.energy.nrel.gov` · group `energy.nrel.gov` · version `v1alpha1` · kind `OpenStudioClusterManager` · kubectl shortName `oscm`
- Namespace: `openstudio-server` · ServiceAccount: `openstudio-operator-sa` · Role: `openstudio-operator-role` (manifests still say `openstudio` until #3 lands — don't propagate the old name)
- Managed objects (helm `develop` defaults — see contract file for the full topology table): worker Deployment `worker` (with unconditional HPA `worker-hpa`) · web_background Deployment `web-background` · web Service `web` :80 · Redis Service `queue` :6379 · NFS PVC `nfs-pvc` (mounted only by `web` at `/mnt/openstudio`; worker scratch is emptyDir)

## OpenStudio REST API contract used by the operator

Verified against NREL/OpenStudio-server **v3.11.0** — the full contract with payload details lives in `.agents/skills/_shared/api-contracts/openstudio-server-v3.11.0-rest.md`.

- **Reads:** `GET /analyses.json` (poll ~30s; raw docs, no derived fields) · `GET /analyses/{id}/page_data.json` (**the SLA clock anchor** — derived `start_time`; never anchor on `created_at`) · `GET /data_points/status?status=1&jobs=started` (light watchdog poll — the `jobs` param does the filtering, a Rails quirk; returns no timestamps) · `GET /data_points.json` (full docs, query params ignored; escalation-only, for datapoint `ip_address`)
- **Actions:** `GET /analyses/{id}/soft_stop` (cooperative stop, does not wait) · `POST /analyses/{id}/action` with body param `analysis_action` ∈ `start|stop` (`stop` waits for in-flight runs) · `POST /data_points/{id}/requeue` (204; re-enqueues onto the `requeued` Resque queue) · `DELETE /analyses/{id}` (server-side cascade rm-rf's the NFS asset dirs — this IS the NFS cleanup)
- **No `kill`/`hard_stop` exists anywhere.** Escalation is Kubernetes-side: delete worker pods whose pod IP matches a started datapoint's `ip_address` (#9), gated by `analysisPolicy.forceDeleteOnEscalation` semantics.
- Analysis states: `na → init → queued → started → post-processing → completed` — no `stopping`/`failed`; grace-period waits anchor on CR `.status` timestamps, never server state.
- `/cluster.json` doesn't exist and `/compute_nodes.json` is unpopulated on K8s. The queue fabric is Redis (Service `queue`, Resque queues `simulations` + `requeued`) — the operator reads it directly, **read-only** (#12).

## Build order

Follow the plan's phase sequence — later phases depend on earlier ones (issue numbers from the grill-session task graph):

1. (#2–#9) Docs/contract truth + manifest fixes + CRD/config extensions + client rewrite + CR-status store + analysis SLA monitor with soft-stop caller and surgical pod-eviction escalation (`AnalysisSoftStopped` K8s Events)
2. (#10–#14) Zombie datapoint watchdog (light poll + operator-tracked `startedSince` clock) · gated worker recycler (`restartedAt` patch — temp-file clearing dropped, worker scratch is emptyDir) · read-only Redis client · `web_background` stall detector · singleton guard (`WorkerRecycled` events)
3. (#15–#16) rclone archival Jobs mounted to the NFS PV (backend-agnostic `s3|gcs|azure`, credentials via `envFrom` secretRef — never read secrets in-operator) + `retentionDays` gate (0 = immediate) + verified-upload-then-`DELETE /analyses/{id}` pipeline
4. (#17, #18) Prometheus `/metrics` (counters already declared in `src/openstudio_operator/metrics.py`) + **HPA-floor adjuster**: patch the chart's existing `worker-hpa` `minReplicas` from Redis backlog — **not KEDA** (the chart's HPA is unconditional; two autoscalers would fight). KEDA is a documented future migration path only.

## Working rules

- Policy values (timeouts, requeue limits, recycle intervals, archive settings) belong in the CRD `spec` / `config.py` — configuration, not hardcoded constants.
- **Every mutating action is gated by `spec.dryRun`** (default `false`; when true, actions are suppressed and emitted as dry-run-marked Events) — D11.
- **Operator memory lives in the CR `.status` subresource only** (maps `softStops`/`requeues`/`startedSince`/`archivedAnalyses` + scalars `lastRecycleAt`/`lastWebBackgroundRestart`), pruned when analyses/datapoints complete; in-memory state is cache, never source of truth — D04.
- Parse all API timestamps to tz-aware UTC at the client boundary; retry REST calls 3× with jittered backoff inside the client, then raise — handlers skip the tick and retry naturally on the next poll (idempotency comes from the status anchors) — D12.
- Exactly one OSCM CR per namespace — enforced passively (oldest wins, loud log + Warning Event); zero CRs means idle — D05.
- The CRD `spec.serverUrl` is the authoritative server URL (`config.py` reads it); the `OPENSTUDIO_SERVER_URL` env var in `deploy/operator-deployment.yaml` is an unreconciled placeholder — reconcile it when wiring Phase 1, don't add a second config path.
- `deploy/operator-deployment.yaml` is single-replica with `strategy: Recreate` on purpose — one active poller, no leader election. Don't scale replicas or switch to RollingUpdate without adding leader election.
- Keep this file accurate: update when facts change, delete what tooling now answers.
