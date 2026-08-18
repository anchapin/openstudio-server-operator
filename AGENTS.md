# AGENTS.md

Guidance for AI coding agents working in this repository.

## Repository status

- **All four implementation phases complete (issues #2–#21), plus the post-#21 wave (#54–#64).** Framework: **Python + Kopf** (decided 2026-08-18). The 2026-08-18 "grill session" locked decisions D01–D13 (`.agents/results/plan-grill.json`) and exported the work as GitHub issues **#2–#21** — every one closed. Post-#21: cleanup sweep (#54), namespace-drift sweep (#55), plan-doc rename to `docs/architecture-plan.md` (#56), real release pipeline (#57), status rewrite (#58), singleton kopf bound + registry-coverage test (#59), `stop_analysis` reservation (#60), `STORAGE_FREED_BYTES` counter removal (#61), HPA-floor chart-derived baseline (#62), vendored v3.11.0 REST contract (#48), redis + pod-discovery validation (#44), and the dryrun code-coverage walkthrough (#45). Cross-cutting audit: `docs/audit-dryrun-idempotency.md` (#21, final gate).
- **Modules in tree** (all live; every mutating call gated by `spec.dryRun` per the audit):
  - handlers — `analysis_sla`, `datapoint_watchdog`, `worker_recycler`, `web_background_monitor`, `storage_pruner`, `hpa_floor`
  - core — `openstudio_client`, `status_store`, `redis_client`, `singleton`, `archival`, `metrics`; plus `config.py` for CRD-spec → typed settings
- **API ground truth is `docs/contracts/openstudio-server-v3.11.0-rest.md`**, vendored in-repo by issue **#48** (originally lived at `.agents/skills/_shared/api-contracts/openstudio-server-v3.11.0-rest.md`, gitignored — clones never saw it). Verified against NREL/OpenStudio-server tag `v3.11.0` and the NatLabRockies helm chart `develop` branch, with the five live-drift findings from **#19's** kind capture folded into both the contract text and `tests/fixtures/contract-shapes.json` (`live_verified_global_rules`). The plan doc (`docs/architecture-plan.md`) predates verification; where they conflict, **the contract file wins** (see its "Non-existent / legacy" + "Live-verified server quirks" sections for the traps: no `PUT action`, no `kill`/`hard_stop`, no `/cluster.json`; unknown ids never 404; raw docs omit nil fields; ids are UUIDs; DELETE 302s without `Accept: application/json`; requeue 500s on jobless dps). **One remaining drift:** `README.md` still links to the old gitignored contract path — fix the link, do not reintroduce the file at `.agents/skills/_shared/`.
- **Operator image + release:** `ghcr.io/anchapin/openstudio-server-operator:dev` is published automatically on every push to `develop`; `v*` tags publish a versioned image and create a GitHub Release (`.github/workflows/release.yml`, #57, `linux/amd64` only). CI green does not prove behavior against a real cluster — every Kubernetes-coupled capability is mocked.
- **Validation status (D13):** code-side validation complete — kind cluster recipe + fixture capture (#19), work-cluster dry-run runbook (`docs/validation.md`, #20), Resque Redis layout + worker-pod discovery code-prep (#44), and `dryRun` test coverage (#45, evidence in `tests/test_dryrun_walkthrough.py`) are all closed. Live-cluster gates remain open: #66 (T28: live Resque layout + worker-pod discovery) and #67 (T29: full-module `dryRun: true` walkthrough steps 3–4, depends on #66) — these are the remaining D13 work before work-cluster exposure.

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
pytest              # tests (CI enforces; 272 cases across 16 test files)
kopf run --module openstudio_operator.handlers --namespace openstudio-server   # run (needs cluster + CRD)
```

- **Namespace is `openstudio-server`, not `openstudio`.** The deployed operator (`deploy/operator-deployment.yaml`) and all RBAC bindings use `openstudio-server`. Running with the wrong namespace yields a no-op operator: the CRD/RBAC are namespaced there and the singleton guard needs it to detect boot.
- **`kopf` is pinned `>=1.37,<1.45` on purpose.** `singleton.install_singleton_guard` reaches into kopf's private `registry._spawning._handlers` (1.37+) — a kopf upgrade can rename or move that internal without crashing, and the failure mode is silently unwrapped handlers (D05 enforcement quietly disabled). `tests/test_singleton_registry_coverage.py` is the CI gate that fails loudly when the internals change shape, but the upper bound requires a deliberate bump.
- Mock the OpenStudio REST API in tests with the `responses` library (in the `dev` extra for exactly this) — don't add another HTTP-mocking dependency. The Redis client is mocked with `fakeredis` for the same reason.
- `scripts/` is the validation toolkit, not build glue: `capture_fixtures.sh` (capture live v3.11.0 fixtures into `tests/fixtures/live/`), `check_fixture_drift.py` (diff live vs. synthetic samples; CI-friendly exit code), `create-kind-cluster.sh` / `teardown-kind-env.sh` / `kind-config.yaml` (local 3-node kind), `deploy-openstudio-stack.sh` + `scripts/manifests/` (helm chart overlay for kind). `docs/kind-validation.md` and `docs/validation.md` are the runbooks.

## Layout

- `src/openstudio_operator/handlers/` — one file per plan module: `analysis_sla`, `datapoint_watchdog`, `worker_recycler`, `storage_pruner`, `web_background_monitor`, `hpa_floor`
- `src/openstudio_operator/config.py` — CRD spec → typed settings (defaults must stay in sync with `deploy/crd.yaml`)
- `src/openstudio_operator/openstudio_client.py` — OpenStudio REST client
- `src/openstudio_operator/status_store.py` — typed 409-safe RMW helper over the CR `.status` subresource (the D04 durable store)
- `src/openstudio_operator/redis_client.py` — read-only Redis client (queue depths + Resque worker liveness)
- `src/openstudio_operator/singleton.py` — passive oldest-CR-per-namespace guard (D05) + central gating of every OSCM timer handler
- `src/openstudio_operator/archival.py` — pure rclone archival Job manifest generator (backend-agnostic s3|gcs|azure, envFrom-only creds, verified-upload gate via `rclone check`; orchestration lives in `storage_pruner`)
- `src/openstudio_operator/metrics.py` — Prometheus counters + `/metrics` endpoint served in the operator process
- `deploy/crd.yaml` · `deploy/rbac.yaml` · `deploy/operator-deployment.yaml` — CRD, least-privilege RBAC (namespaced **Role**, never a ClusterRole; verbs enumerated per the plan), operator Deployment
- `.github/workflows/ci.yml` — lint + test + `guard-branch-pairing`; keep that job name stable — it is a **required status check on `main`**

## Fixed identifiers (from the CRD/RBAC spec — exact spelling matters)

- CRD: `openstudioclustermanagers.energy.nrel.gov` · group `energy.nrel.gov` · version `v1alpha1` · kind `OpenStudioClusterManager` · kubectl shortName `oscm`
- Namespace: `openstudio-server` · ServiceAccount: `openstudio-operator-sa` · Role: `openstudio-operator-role`
- Managed objects (helm `develop` defaults — see contract file for the full topology table): worker Deployment `worker` (with unconditional HPA `worker-hpa`) · web_background Deployment `web-background` · web Service `web` :80 · Redis Service `queue` :6379 · NFS PVC `nfs-pvc` (mounted only by `web` at `/mnt/openstudio`; worker scratch is emptyDir)

## OpenStudio REST API contract used by the operator

Verified against NREL/OpenStudio-server **v3.11.0** — the full contract with payload details lives in `docs/contracts/openstudio-server-v3.11.0-rest.md`.

- **Reads:** `GET /analyses.json` (poll ~30s; raw docs, no derived fields) · `GET /analyses/{id}/page_data.json` (**the SLA clock anchor** — derived `start_time`; never anchor on `created_at`) · `GET /data_points/status?status=1&jobs=started` (light watchdog poll — the `jobs` param does the filtering, a Rails quirk; returns no timestamps) · `GET /data_points.json` (full docs, query params ignored; escalation-only, for datapoint `ip_address`)
- **Actions:** `GET /analyses/{id}/soft_stop` (cooperative stop, does not wait) · `POST /analyses/{id}/action` with body param `analysis_action` ∈ `start|stop` (`stop` waits for in-flight runs) · `POST /data_points/{id}/requeue` (204; re-enqueues onto the `requeued` Resque queue) · `DELETE /analyses/{id}` (server-side cascade rm-rf's the NFS asset dirs — this IS the NFS cleanup)
- **No `kill`/`hard_stop` exists anywhere.** Escalation is Kubernetes-side: delete worker pods whose pod IP matches a started datapoint's `ip_address` (#9), gated by `analysisPolicy.forceDeleteOnEscalation` semantics.
- Analysis states: `na → init → queued → started → post-processing → completed` — no `stopping`/`failed`; grace-period waits anchor on CR `.status` timestamps, never server state.
- `/cluster.json` doesn't exist and `/compute_nodes.json` is unpopulated on K8s. The queue fabric is Redis (Service `queue`, Resque queues `simulations` + `requeued`) — the operator reads it directly, **read-only** (#12).

## Build order (completed — Phases 1–4, issues #2–#21)

Issue numbers from the grill-session task graph. Every commit on `develop` resolves one issue; the cross-cutting audit (#21) is the final gate.

1. **Phase 1 — Docs/contract + foundation.** #2 plan-doc/contract alignment · #3 manifest fixes (namespace, RBAC, OPENSTUDIO_SERVER_URL) · #4 CRD spec/status schema · #5 `config.py` parity · #6 OpenStudioClient rewrite (verified v3.11.0, UTC boundary, 3× retry/backoff) · #7 CR-status store helper (409-safe RMW, pruning) · #8 analysis SLA monitor (page_data clock, one-shot soft-stop, dryRun-gated) · #9 grace wait + surgical pod-eviction escalation (`AnalysisSoftStopped` K8s Events)
2. **Phase 2 — Worker + queue hygiene.** #10 zombie datapoint watchdog (operator-tracked `startedSince` clock, bounded requeue, exhaustion events) · #11 gated worker recycler (`restartedAt` patch, `minRecycleIntervalMinutes` cooldown — temp-file clearing dropped, worker scratch is emptyDir) · #12 read-only Redis client (queue depths + Resque worker liveness, fakeredis-mocked) · #13 `web_background` stall detector (sustained-window + cooldown restart) · #14 passive singleton guard (oldest CR wins, Warning Events, idle on zero)
3. **Phase 3 — Storage archival.** #15 ephemeral rclone archival Job generator (backend-agnostic `s3|gcs|azure`, `envFrom` secretRef — never read secrets in-operator) · #16 retention pipeline (eligibility, verified-upload gate, cascade-delete)
4. **Phase 4 — Observability + autoscaling.** #17 Prometheus `/metrics` endpoint · #18 **HPA-floor adjuster**: patch the chart's existing `worker-hpa` `minReplicas` from Redis backlog — **not KEDA** (the chart's HPA is unconditional; two autoscalers would fight). KEDA is a documented future migration path only.

Validation: #19 kind cluster recipe + fixture capture · #20 work-cluster dry-run runbook (`docs/validation.md`).

Cross-cutting audit: #21 dryRun completeness + idempotency anchors + config purity (`docs/audit-dryrun-idempotency.md`) — one D11 violation found (failed-archival-Job cleanup delete, #42) and fixed in the same audit PR.

## Working rules

- Policy values (timeouts, requeue limits, recycle intervals, archive settings) belong in the CRD `spec` / `config.py` — configuration, not hardcoded constants.
- **Every mutating action is gated by `spec.dryRun`** (default `false`; when true, actions are suppressed and emitted as dry-run-marked Events) — D11.
- **Operator memory lives in the CR `.status` subresource only** (maps `softStops`/`requeues`/`startedSince`/`archivedAnalyses` + scalars `lastRecycleAt`/`lastWebBackgroundRestart`), pruned when analyses/datapoints complete; in-memory state is cache, never source of truth — D04.
- Parse all API timestamps to tz-aware UTC at the client boundary; retry REST calls 3× with jittered backoff inside the client, then raise — handlers skip the tick and retry naturally on the next poll (idempotency comes from the status anchors) — D12.
- Exactly one OSCM CR per namespace — enforced passively (oldest wins, loud log + Warning Event); zero CRs means idle — D05.
- The CRD `spec.serverUrl` is the authoritative server URL (`config.py` reads it); the `OPENSTUDIO_SERVER_URL` env var in `deploy/operator-deployment.yaml` is gone — single config path enforced (#3).
- `deploy/operator-deployment.yaml` is single-replica with `strategy: Recreate` on purpose — one active poller, no leader election. Don't scale replicas or switch to RollingUpdate without adding leader election.
- Keep this file accurate: update when facts change, delete what tooling now answers.
