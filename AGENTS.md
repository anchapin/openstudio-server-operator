# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this project is

A Kubernetes operator for [OpenStudio Server](https://github.com/NREL/OpenStudio-server) (Ruby/Rails `web` + `web_background` + MongoDB + `worker` pods + NFS-shared volumes). It runs **alongside** the existing [`openstudio-server-helm`](https://github.com/NREL/openstudio-server-helm) deployment (namespace `openstudio-server`, server images pinned to `3.11.0`) and automates analysis timeout soft-stops, zombie datapoint requeues, worker pod recycling, NFS pruning with S3/GCS/Azure archival, and `web_background` queue-stall detection. It manages that stack; it does not replace it. Framework: **Python + [Kopf](https://kopf.readthedocs.io/)**.

Full context: [`README.md`](./README.md). Plan: [`docs/architecture-plan.md`](./docs/architecture-plan.md). Verified v3.11.0 REST contract: [`docs/contracts/openstudio-server-v3.11.0-rest.md`](./docs/contracts/openstudio-server-v3.11.0-rest.md). Cross-cutting audit: [`docs/audit-dryrun-idempotency.md`](./docs/audit-dryrun-idempotency.md). Live-cluster runbooks: [`docs/validation.md`](./docs/validation.md), [`docs/kind-validation.md`](./docs/kind-validation.md).

## Branching model

- `develop` — default branch; all work lands here (direct pushes allowed for the owner).
- `main` — release branch; protected by ruleset `protect-main`. Direct pushes and force pushes are rejected for everyone (no bypass). Updates require a PR **from `develop`**; the required `guard-branch-pairing` CI check fails any PR into `main` whose source branch is not `develop`. **Never attempt to push `main` directly** — it is rejected by design.
- Ruleset `require-ci-on-develop-prs` requires `lint` + `test` on PR merges into `develop`. The owner is a bypass actor, so direct pushes to `develop` remain allowed — intentional.
- **Outage bypass:** when GitHub Actions is unavailable, the owner may merge/push via admin bypass. Mitigations: every change locally verified on the branch (ruff + pytest green; `docker build` when Release/Docker files are touched) and a retroactive CI run on `develop` HEAD once runners recover.
- **Merge-subject hygiene (#88):** `develop` is the default branch — GitHub closes an issue when any commit with closing keywords (`Closes #N`, `resolve #N`, `fixes #N`) lands on `develop`, **including auto-generated squash-merge subjects** (independent of the PR body). For keep-open PRs, keep closing keywords out of the PR **title** AND branch **commit subjects** (use `Refs #N` / `for #N` / `touches #N`) AND merge with explicit overrides: `gh pr merge N --squash --subject "…" --body "Refs #N"` — never rely on the auto-generated subject. When the issue SHOULD close on merge, the closing keyword IS desired in both subject (`fix: resolve #N — …`) and PR body (`Closes #N`).

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

ruff check .     # lint (line-length 100 per pyproject.toml; CI pins ruff>=0.16)
.venv/bin/pytest # 427 tests across 23 files — use the venv pytest (system pytest won't resolve `openstudio_operator`)
kopf run --module openstudio_operator.handlers --namespace openstudio-server   # run (needs cluster + CRD)
```

- **Venv drift guard (#71):** in wave-orchestration worktrees the shared venv can resolve `openstudio_operator` against a different checkout. Before running tests/lint against a new checkout, re-run `pip install -e '.[dev]'` from that checkout, or verify with `scripts/check_editable_install.sh`.
- **Mocking deps are pinned in `pyproject.toml`**: `responses` for the OpenStudio REST API, `fakeredis` for the read-only Redis client. Don't add another HTTP/Redis mocking library.
- `scripts/` is the **validation toolkit**, not build glue. See "Cluster validation" below.

## Layout

- `src/openstudio_operator/handlers/` — Kopf handlers, one file per plan module: `analysis_sla`, `datapoint_watchdog`, `worker_recycler`, `web_background_monitor`. `handlers/__init__.py` is the operator entrypoint: it starts the Prometheus `/metrics` server (`#17`) and installs the singleton guard (`#14`); add new handler modules to its import block and the gate picks them up automatically.
- `src/openstudio_operator/config.py` — CRD `spec` → typed `OperatorConfig` (defaults **must** stay in sync with `deploy/crd.yaml`).
- `src/openstudio_operator/openstudio_client.py` — REST client (verified against v3.11.0, 3× retry/backoff, tz-aware UTC parsing).
- `src/openstudio_operator/status_store.py` — typed 409-safe RMW helper over the CR `.status` subresource.
- `src/openstudio_operator/redis_client.py` — read-only Redis client (queue depths + Resque worker liveness).
- `src/openstudio_operator/singleton.py` — passive oldest-CR-per-namespace guard + central gating of every OSCM timer handler.
- `src/openstudio_operator/archival.py` — pure rclone archival Job manifest generator (backend-agnostic `s3|gcs|azure`, `envFrom`-only creds).
- `src/openstudio_operator/retention.py` + `src/openstudio_operator/prune_entrypoint.py` — retention pipeline + Job entrypoint invoked by `deploy/storage-cronjob.yaml` (`#78`); the operator process owns no storage polling loop.
- `src/openstudio_operator/metrics.py` — Prometheus counters + `/metrics` HTTP server (port 9090). The full family inventory is enumerated in [README.md#metrics](./README.md#metrics); the test invariant that catches drift is `tests/test_metrics_endpoint.py::EXPECTED_COUNTER_FAMILIES` / `EXPECTED_GAUGE_FAMILIES` (12 counters + 1 gauge today post-#171, #183).
- `deploy/crd.yaml` · `deploy/rbac.yaml` · `deploy/operator-deployment.yaml` — CRD, namespaced **Role** only (verbs enumerated; no `horizontalpodautoscalers`, no `batch` post-`#77`/`#78`), operator Deployment (single-replica `Recreate`, hardened `securityContext` post-`#115`).
- `deploy/keda-scaledobject.yaml` · `deploy/redis-credentials-secret.yaml` — KEDA ScaledObject + TriggerAuthentication + Redis password Secret (cluster-admin install; operator-managed? no — see Working rules).
- `deploy/storage-cronjob.yaml` — prune CronJob (`#78`; uses `openstudio_operator.prune_entrypoint`).
- `deploy/network-policy.yaml` — default-deny egress + per-actor allow-lists for the operator surface (`#112`), plus the `/metrics` ingress allow (`#166`): deny-all + DNS allow + operator-only egress + storage-egress (HTTPS-only, RFC1918 excepted) + metrics-ingress (port 9090 restricted to `prometheus` namespace + same-namespace peer; cluster-admin opt-in for other scraper namespaces).
- `tests/fixtures/` — `contract-shapes.json` + `samples/` (synthetic) + `live/` (captured from a real v3.11.0 cluster).
- `tests/golden/` — snapshot tests for generated rclone Job manifests.
- `docs/onboarding.md` — first-time-contributor + AI-agent walkthrough (issue #178): setup, single-test/full-suite commands, venv drift guard (#71), the 5-step "add a new OSCM timer handler" pattern, Working-rules-that-bite, audit-doc update rules, branch/PR conventions, good-first-PR candidates.
- `.github/workflows/ci.yml` — `lint` + `test` + `guard-branch-pairing`; keep that job name stable — it is a **required status check on `main`**.
- `.github/workflows/release.yml` — publishes `ghcr.io/anchapin/openstudio-server-operator:dev` on every push to `develop`; `v*` tags publish a versioned image and create a GitHub Release (`linux/amd64` only). Both jobs enable SLSA provenance (`mode=max`) + SPDX SBOM as OCI attestations and cosign-keyless sign the published digest (`#113`); CI guards on `cosign verify-attestation --type slsaprovenance`. `Dockerfile` pins `python:3.12-slim` by digest (`#113`).

## Fixed identifiers (exact spelling matters)

- CRD: `openstudioclustermanagers.energy.nrel.gov` · group `energy.nrel.gov` · version `v1alpha1` · kind `OpenStudioClusterManager` · kubectl shortName `oscm`.
- Namespace: `openstudio-server` · ServiceAccount: `openstudio-operator-sa` · Role: `openstudio-operator-role`. **Running with any other namespace yields a no-op operator** (CRD/RBAC are namespaced there and the singleton guard needs it to detect boot).
- Managed objects (helm `develop` defaults — see contract file for the full topology table): worker Deployment `worker` · `web_background` Deployment `web-background` · web Service `web` :80 · Redis Service `queue` :6379 · NFS PVC `nfs-pvc` (mounted only by `web` at `/mnt/openstudio`; worker scratch is `emptyDir`).

## Cluster prerequisites (cluster-admin, outside the operator process)

**KEDA is a prerequisite for autoscaling (#77).** Install KEDA ≥ 2.20 in namespace `keda` (helm `kedacore/keda` or `scripts/install-keda.sh` raw-manifest path), disable the chart's `worker-hpa` HPA, then apply `deploy/redis-credentials-secret.yaml` + `deploy/keda-scaledobject.yaml`. Two autoscalers on the same Deployment oscillate. The operator owns no autoscaling surface — KEDA does — and the operator's Role has no `horizontalpodautoscalers` verbs. The kind recipe (`scripts/deploy-openstudio-stack.sh` + `scripts/manifests/06-worker.yaml`) already omits the chart HPA. Runbook: `docs/validation.md#keda-cluster-prerequisite-issue-77`.

## Cluster validation (kind recipe)

`scripts/create-kind-cluster.sh` + `scripts/deploy-openstudio-stack.sh` stand up a single control-plane kind node and the full OpenStudio stack via a helm-chart overlay (`scripts/manifests/`). Designed for one node on purpose — the whole stack runs there, and images are pulled from Docker Hub by the kubelet, so no `kind load` is needed. `scripts/capture_fixtures.sh` captures live v3.11.0 fixtures into `tests/fixtures/live/`; `scripts/check_fixture_drift.py` diffs live vs. synthetic samples. Teardown: `scripts/teardown-kind-env.sh`. Live evidence in `docs/kind-validation.md`.

## Working rules (these bite if violated)

- **Every mutating action is gated by `spec.dryRun`** (default `false`; when `true`, actions are suppressed and emitted as dry-run-marked Events) — D11.
- **Operator memory lives in the CR `.status` subresource only** (maps `softStops`/`requeues`/`startedSince`/`archivedAnalyses` + scalars `lastRecycleAt`/`lastWebBackgroundRestart`); in-memory state is cache, never source of truth — D04. The status store handles 409 retries internally.
- **Exactly one OSCM CR per namespace** — enforced passively via the singleton guard (oldest wins, Warning Event + loud log); zero CRs means idle — D05.
- **Parse all API timestamps to tz-aware UTC at the client boundary**; retry REST calls 3× with jittered backoff inside the client, then raise — handlers skip the tick and retry naturally on the next poll (idempotency comes from the status anchors) — D12.
- **Policy values belong in the CRD `spec` / `config.py`** — configuration, not hardcoded constants in handlers.
- **`kopf` is pinned `>=1.37,<1.45` on purpose.** `singleton.install_singleton_guard` reaches into kopf's private `registry._spawning._handlers` (1.37+). A kopf upgrade can rename or move that internal without crashing, and the failure mode is silently unwrapped handlers (D05 enforcement quietly disabled). `tests/test_singleton_registry_coverage.py` is the CI gate that fails loudly when the internals change shape, but the upper bound requires a deliberate bump.
- **The CRD `spec.serverUrl` is the authoritative server URL** (`config.py` reads it); the `OPENSTUDIO_SERVER_URL` env var is gone — single config path enforced (`#3`).
- **`spec.redisUrl` defaults to empty by design (`#116`).** The historical default baked the kind-recipe password `openstudio` into every CRD; the operator now refuses to operate and emits a per-CR `Warning` event when the field is empty. Helm-chart users must set it explicitly (or template it from the Redis Secret). Don't "fix" the default — it's the regression fence.
- **Redis password is no longer a fixed literal (`#150`).** The kind recipe's `scripts/manifests/02-redis.yaml` + `deploy/redis-credentials-secret.yaml` ship the placeholder `openstudio-rotated` (was the publicly-known `openstudio` literal until #150). The matching `REDIS_URL` env vars in `04-web.yaml` / `05-web-background.yaml` / `06-worker.yaml` were rotated in lockstep. Fresh installs MUST run `scripts/rotate_redis_password.sh` first — it generates a per-cluster 32-char random password, substitutes it into the manifests at apply time, and updates the live `openstudio-redis` Secret. The `scripts/check_redis_password_unique.sh` CI guard fails the build if the legacy `openstudio` literal re-appears as a Redis password in any of those five files.
- **`deploy/operator-deployment.yaml` is single-replica with `strategy: Recreate` on purpose** — one active poller, no leader election. Don't scale replicas or switch to `RollingUpdate` without adding leader election.
- **The operator never reads secrets.** rclone archival jobs receive credentials via `envFrom` `secretRef` only.
- **No custom autoscaling code.** All autoscaling lives in `deploy/keda-scaledobject.yaml`. The `hpa_floor` handler module is deleted; the `openstudio_operator_hpa_floor_adjustments_total` counter is gone.
- **REST contract trapdoors** (from the verified v3.11.0 contract — full list in `docs/contracts/openstudio-server-v3.11.0-rest.md`): no `PUT action`, no `kill`/`hard_stop`, no `/cluster.json`; unknown ids never 404; raw docs omit nil fields; ids are UUIDs; `DELETE /analyses/{id}` 302s without `Accept: application/json`; `requeue` 500s on jobless datapoints. Analysis states: `na → init → queued → started → post-processing → completed` — no `stopping`/`failed`; grace-period waits anchor on CR `.status` timestamps, never server state. The queue fabric is Redis (Service `queue`, Resque queues `simulations` + `requeued`) — operator reads it directly, **read-only** (`#12`).
- **`/metrics` ingress is restricted to a scraper-namespace allow (`#166`).** `deploy/network-policy.yaml` ships with `openstudio-operator-metrics-ingress` (policyTypes: [Ingress]) that allows TCP/9090 to the operator pod only from (a) a namespace labeled `kubernetes.io/metadata.name: prometheus` and (b) any same-namespace peer (sidecar). The plaintext Prometheus endpoint (`metrics.py:204` `prometheus_client.start_http_server(port, addr="0.0.0.0")` + `deploy/operator-deployment.yaml:43` `containerPort: 9090`) has no authN/authZ/TLS, so a cluster whose Prometheus runs in a differently-named namespace (`monitoring`, `kube-prometheus-stack`, `observability`, etc.) MUST edit the `namespaceSelector` label match in that policy before applying — otherwise the operator's metrics (queue depths, status-conflict retries, `resque_workers_seen_max`) silently become unreadable. `tests/test_deploy_manifests.py::test_network_policy_metrics_ingress_has_prometheus_and_peer_allow` enforces both peers are present.

## Update protocol

Keep this file accurate: update when facts change, delete what tooling now answers. Cross-check with `README.md` and the audit doc; the contract file (`docs/contracts/openstudio-server-v3.11.0-rest.md`) is the ground truth for REST surface details. When bumping things that affect the test count, the layout bullets, or the Working rules (e.g. adding a new handler module, changing a default), re-verify rather than re-assert — drift here is what CI history #106/#144 has already flagged.