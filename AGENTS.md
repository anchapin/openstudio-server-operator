# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this project is

A Kubernetes operator for [OpenStudio Server](https://github.com/NREL/OpenStudio-server) (Ruby/Rails `web` + `web_background` + MongoDB + `worker` pods + NFS-shared volumes). It runs **alongside** the existing [`openstudio-server-helm`](https://github.com/NREL/openstudio-server-helm) deployment (namespace `openstudio-server`, server images pinned to `3.11.0`) and automates analysis timeout soft-stops, zombie datapoint requeues, worker pod recycling, NFS pruning with S3/GCS/Azure archival, and `web_background` queue-stall detection. It manages that stack; it does not replace it. Framework: **Python + [Kopf](https://kopf.readthedocs.io/)**.

Codebase structure and key references:

- `README.md` — module table, metrics table, JSON log schema, quick triage commands.
- `docs/onboarding.md` — first-time contributor + AI-agent walkthrough (issue #178): setup, single-test/full-suite commands, venv drift guard, the 5-step "add a new OSCM timer handler" pattern, Working-rules-that-bite, audit-doc update rules, branch/PR conventions, good-first-PR candidates. **Read this before adding a new handler.**
- `docs/architecture-plan.md` — design plan that named the modules.
- `docs/contracts/openstudio-server-v3.11.0-rest.md` — verified REST contract (ground truth for the upstream surface).
- `docs/audit-dryrun-idempotency.md` — cross-cutting idempotency / dry-run audit (D04 / D05 / D11 / D12).
- `docs/validation.md`, `docs/kind-validation.md` — cluster validation runbooks + live evidence.
- `CHANGELOG.md` — curated Keep-a-Changelog notes per release (issue #177).

OpenCode-specific: the `codebase-memory-mcp` server is wired up for this repo (see `~/.config/opencode/AGENTS.md`). Prefer `search_graph` / `trace_path` / `get_code_snippet` over grep/glob for structural questions (handler wiring, callers, call chain). Use grep/glob only for string literals and non-code files.

## Branching model

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the canonical branch,
PR-body, and merge-subject conventions (the source of truth for issue
#88). Summary:

- `develop` — default branch; all work lands here (direct pushes allowed for the owner).
- `main` — release branch; protected by ruleset `protect-main`. Direct/force pushes rejected for everyone. Updates require a PR **from `develop`**; the required `guard-branch-pairing` CI check fails any PR into `main` whose source branch is not `develop`. **Never push `main` directly** — rejected by design.
- Ruleset `require-ci-on-develop-prs` requires `lint` + `test` on PR merges into `develop`. The owner is a bypass actor — direct pushes to `develop` remain allowed.
- **Merge-subject hygiene (issue #88):** see [`docs/onboarding.md#merge-subject-hygiene-88`](docs/onboarding.md#merge-subject-hygiene-88) for the full rule and the canonical command examples (`docs/onboarding.md` is the source of truth; this line is a quick-reference pointer). The shape: `develop` closes an issue whenever any commit with `Closes #N` / `resolve #N` / `fixes #N` lands there — including auto-generated squash subjects. Keep-open PRs use `Refs #N` / `for #N` / `touches #N` everywhere (PR title, squash subject, body); closing PRs use `Closes #N` in both the squash subject AND the body. Always merge with an explicit `--subject` override; never trust the auto-generated subject.
- **PR-body scope guard (issue #301):** every PR body MUST include a `Scope guard:` line that names the issues this PR touches and declares what is intentionally NOT being changed (and which follow-up issue owns that area). See [`docs/onboarding.md#scope-guard-issue-301`](docs/onboarding.md#scope-guard-issue-301) for the full rule + canonical example; `scripts/check_pr_body_scope.sh` enforces it on every pull request via the `lint` CI job.
- **Heading-vs-line gotcha (#385, caught in PR #384):** the script's regex anchors on the line start (`^[[:space:]]*[-*+]?[[:space:]]*Scope guard:`) — a markdown heading `## Scope guard:` does NOT match because `##` is neither whitespace nor a bullet. Always write a single `Scope guard:` line (optionally bulleted), never a section header.
- **Outage bypass:** when GitHub Actions is unavailable, the owner may merge/push via admin bypass. Mitigations: every change locally verified on the branch (ruff + pytest green; `docker build` when Release/Docker files are touched) and a retroactive CI run on `develop` HEAD once runners recover.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

ruff check .     # lint (line-length 100; CI pins ruff>=0.16, pyproject floor matches)
.venv/bin/pytest # 789 tests across 39 files — use the venv pytest (system pytest won't resolve `openstudio_operator`)
kopf run --module openstudio_operator.handlers --namespace openstudio-server   # run (needs cluster + CRD)
```

- **Venv drift guard (issue #71):** in wave-orchestration worktrees the shared venv can resolve `openstudio_operator` against a different checkout. Before running tests/lint against a new checkout, re-run `pip install -e '.[dev]'` from that checkout, or verify with `scripts/check_editable_install.sh`.
- **CI uses a hash-pinned lockfile** (`requirements.lock`, issue #173) — `pip install --require-hashes -r requirements.lock`. Local `pip install -e '.[dev]'` is fine because it's editable; the lockfile exists so CI installs are reproducible across runs.
- **Mocking deps are pinned in `pyproject.toml`**: `responses` for the OpenStudio REST API, `fakeredis` for the read-only Redis client, `hypothesis` for the D12 timestamp boundary (issue #246). Don't add another HTTP/Redis mocking library.
- **Single-test focus:** `pytest tests/test_metrics_endpoint.py::test_declared_counters_match_expected_set` — pytest's `-k` and path syntax work; node IDs are `<file>::<test>`.
- **`scripts/` is the validation toolkit** (kind cluster lifecycle, password rotation, fixture capture/drift, KEDA install). It is **not** build glue. The `check_*_unique.sh` scripts are CI gates; never delete them.

## Layout

- `src/openstudio_operator/handlers/` — Kopf handlers, one file per plan module: `analysis_sla`, `datapoint_watchdog`, `worker_recycler`, `web_background_monitor` (the four OSCM timers), plus `dry_run_audit` (a pure `@kopf.on.event` watch handler — NOT singleton-gated, NOT in the `_oscm_handlers` spawning registry — that emits the `DryRunToggled` audit Event on `spec.dryRun` transitions, #397). `handlers/__init__.py` is the operator entrypoint: it starts the Prometheus `/metrics` server, installs the singleton guard, registers the warning-event sinks (Redis-URL guard, Resque key-layout guard, status-store cap), and boots the JSON-logging handler. **Add new handler modules to its import block** — see `docs/onboarding.md` for the 5-step pattern.
- Shared utility modules (single source of truth for the cross-handler surface):
  - `_constants.py` — operator-behavior constants (polling cadences, metrics port, Resque-key-layout grace). Policy values do NOT belong here — cluster policy lives in the CRD `spec` / `config.py`.
  - `_time.py` — `parse_utc(...)`; tz-aware UTC, `None`-safe. Use this for any new timestamp parse — don't roll your own.
  - `_k8s.py` — `DeploymentReader` Protocol + `deployment_label_selector()`. Neutral home for cross-handler K8s surface.
  - `_oscm_handlers.py` — Python-level OSCM handler registry. New OSCM timer handlers MUST register at import time — call `register_fn(fn)` (the `__name__`-introspecting convenience, #407) at module bottom, or `register(id, fn)` when the id must differ; the singleton guard cross-checks against the kopf registry at gate time so a forgotten registration is caught by `tests/test_singleton_registry_coverage.py::test_python_registry_includes_all_oscm_spawning_handlers`.
  - `events.py` — `EventEmitter` (one instance per tick); dry-run gate (D11) and suppressed-event counter live here, not at call sites.
  - `events_sinks.py` — `QueuedKopfEventSink` — collapses the three near-identical queue/drain mechanisms (Redis-URL guard, Resque key-layout guard, status-map cap) into one class.
  - `logging_setup.py` — JSON `logging.Formatter` + idempotent `install_json_logging()` called from `handlers/__init__.py` (operator) and `prune_entrypoint.py::main` (CronJob).
  - `client_factory.py` — `lru_cache`-keyed factories for `OpenStudioClient` and `ReadOnlyRedisClient`. A mutated `spec.serverUrl` / `spec.redisUrl` invalidates by a different cache key — no manual eviction needed. **"Exactly one construction site" invariants are AST-gated** by `tests/test_singleton_registry_coverage.py::test_only_one_custom_objects_api_construction_point` and `tests/test_client_factory.py::test_only_one_read_only_redis_client_construction_point`.
- Domain modules:
  - `config.py` — CRD `spec` → typed `OperatorConfig` (defaults must stay in sync with `deploy/crd.yaml`; covered by `tests/test_smoke.py::test_config_defaults_parity_with_crd_yaml`).
  - `openstudio_client.py` — REST client (verified against v3.11.0, 3× retry/backoff, tz-aware UTC parsing).
  - `status_store.py` — typed 409-safe RMW helper over the CR `.status` subresource.
  - `redis_client.py` — read-only Redis client (queue depths + Resque worker liveness).
  - `singleton.py` — passive oldest-CR-per-namespace guard + central gating of every OSCM timer handler.
  - `archival.py` — pure rclone archival Job manifest generator (backend-agnostic `s3|gcs|azure`, `envFrom`-only creds).
  - `retention.py` + `prune_entrypoint.py` — retention pipeline + Job entrypoint invoked by `deploy/storage-cronjob.yaml`. The operator process owns no storage polling loop.
  - `metrics.py` — Prometheus counters + `/metrics` HTTP server (port 9090). Full family inventory in `README.md#metrics`; the drift-invariant is `tests/_metrics_inventory.py` (`EXPECTED_COUNTER_FAMILIES` / `EXPECTED_GAUGE_FAMILIES` / `EXPECTED_HISTOGRAM_FAMILIES` — the shared canonical source both `test_metrics_endpoint.py` and `test_walk_metrics_registry.py` import from, issue #406). **Adding a counter/gauge/histogram without adding it there (or vice versa) fails CI loudly.**
- `deploy/` — `crd.yaml` · `rbac.yaml` · `operator-deployment.yaml` (single-replica, `Recreate`) · `keda-scaledobject.yaml` · `redis-credentials-secret.yaml` · `mongo-credentials-secret.yaml` · `storage-cronjob.yaml` · `network-policy.yaml` · `pod-delete-admission-policy.yaml`. RBAC is namespaced **Role** only (verbs enumerated; no `horizontalpodautoscalers`, no `batch`). `pod-delete-admission-policy.yaml` (#293) is a cluster-scoped `ValidatingAdmissionPolicy` + `ValidatingAdmissionPolicyBinding` — see K8s 1.30+ rule below.
- `tests/fixtures/` — `contract-shapes.json` + `samples/` (synthetic) + `live/` (captured from a real v3.11.0 cluster; live captures go through `scripts/capture_fixtures.sh` + `scripts/check_fixture_drift.py`) + `wave-orchestrator/` (`sample-wave.json` — canonical 3-issue wave replayed by `tests/test_wave_orchestrator_e2e.py`, issue #381).
- `tests/golden/` — snapshot tests for generated rclone Job manifests.

## Fixed identifiers (exact spelling matters)

- CRD: `openstudioclustermanagers.energy.nrel.gov` · group `energy.nrel.gov` · version `v1alpha1` · kind `OpenStudioClusterManager` · kubectl shortName `oscm`.
- Namespace: `openstudio-server` · ServiceAccount: `openstudio-operator-sa` · Role: `openstudio-operator-role`. **Running with any other namespace yields a no-op operator** — CRD/RBAC are namespaced there and the singleton guard needs it to detect boot.
- Managed objects (helm `develop` defaults): worker Deployment `worker` · `web_background` Deployment `web-background` · web Service `web` :80 · Redis Service `queue` :6379 · NFS PVC `nfs-pvc` (mounted only by `web` at `/mnt/openstudio`; worker scratch is `emptyDir`).

## Working rules (these bite if violated)

- **Every mutating action is gated by `spec.dryRun`** (default `false`; when `true`, actions are suppressed and emitted as dry-run-marked Events) — D11.
- **Operator memory lives in the CR `.status` subresource only** (maps `softStops`/`requeues`/`startedSince`/`archivedAnalyses` + scalars `lastRecycleAt`/`lastWebBackgroundRestart` + the `deferredEvents` array — the crash-surviving mirror of the deferred-Warning queue, #402); in-memory state is cache, never source of truth — D04. The status store handles 409 retries internally.
- **Exactly one OSCM CR per namespace** — enforced passively via the singleton guard (oldest wins, Warning Event + loud log); zero CRs means idle — D05.
- **Parse all API timestamps to tz-aware UTC at the client boundary** (`_time.parse_utc`); retry REST calls 3× with jittered backoff inside the client, then raise — handlers skip the tick and retry naturally on the next poll (idempotency comes from the status anchors) — D12.
- **Policy values belong in the CRD `spec` / `config.py`** — configuration, not hardcoded constants in handlers.
- **`kopf` is pinned `>=1.37,<1.45` on purpose.** `singleton.install_singleton_guard` reaches into kopf's private `registry._spawning._handlers` (1.37+). A kopf upgrade can rename or move that internal without crashing, and the failure mode is silently unwrapped handlers (D05 enforcement quietly disabled). `tests/test_singleton_registry_coverage.py` is the CI gate that fails loudly when the internals change shape; the upper bound still requires a deliberate bump.
- **The CRD `spec.serverUrl` is the authoritative server URL** (`config.py` reads it); the `OPENSTUDIO_SERVER_URL` env var is gone — single config path enforced (issue #3).
- **`spec.redisUrl` defaults to empty by design (issue #116).** The historical default baked the kind-recipe password `openstudio` into every CRD; the operator now refuses to operate and emits a per-CR `Warning` event when the field is empty. Helm-chart users must set it explicitly (or template it from the Redis Secret). Don't "fix" the default — it's the regression fence.
- **Redis + Mongo passwords are no longer fixed literals (issues #150, #219).** Fresh installs MUST run `scripts/rotate_redis_password.sh` and `scripts/rotate_mongo_password.sh` first — they generate per-cluster random passwords, substitute them into the manifests at apply time, and update the live Secrets. CI guards `scripts/check_redis_password_unique.sh` + `scripts/check_mongo_password_unique.sh` fail the build if the legacy literal re-appears as a password in source.
- **`deploy/operator-deployment.yaml` is single-replica with `strategy: Recreate` on purpose** — one active poller, no leader election. Don't scale replicas or switch to `RollingUpdate` without adding leader election.
- **ValidatingAdmissionPolicies narrow the operator / prune SAs further than RBAC allows (issues #293, #294, #398).** RBAC `PolicyRule` has no `labelSelector`, so `verbs:[delete]` on `pods` cannot be constrained to a subset of pods by label, and `verbs:[create|update|delete]` on `batch/jobs` cannot be constrained to the archival Job subset. The admission layer fills the gap: `deploy/pod-delete-admission-policy.yaml` constrains the operator SA's `pods/delete` to pods carrying `app=worker` (matches the eviction path); `deploy/storage-cronjob.yaml` embeds a second VAP that constrains the prune SA's `batch/jobs` verbs to Jobs carrying both `app.kubernetes.io/managed-by=openstudio-operator` AND `app.kubernetes.io/component=archival` AND named `oscm-archive-*` (labels alone are spoofable by the very SA the policy constrains — the name pattern from `archival.py::archival_job_name` is the second factor, #398). Humans via `kubectl` and the prune CronJob SA are left unrestricted — only the operator / prune SAs are narrowed. **Requires K8s 1.30+** (`admissionregistration.k8s.io/v1` GA); pre-1.30 clusters must skip both VAPs.
- **The operator never reads secrets.** rclone archival jobs receive credentials via `envFrom` `secretRef` only.
- **No custom autoscaling code.** All autoscaling lives in `deploy/keda-scaledobject.yaml`. The `hpa_floor` handler module is deleted; the `openstudio_operator_hpa_floor_adjustments_total` counter is gone. KEDA is a cluster-admin prerequisite (helm `kedacore/keda` ≥ 2.20 or `scripts/install-keda.sh`) and the chart's `worker-hpa` HPA must be disabled — two autoscalers on the same Deployment oscillate. Full runbook in `docs/validation.md#keda-cluster-prerequisite-issue-77`.
- **REST contract trapdoors** (from the verified v3.11.0 contract): no `PUT action`, no `kill`/`hard_stop`, no `/cluster.json`; unknown ids never 404; raw docs omit nil fields; ids are UUIDs; `DELETE /analyses/{id}` 302s without `Accept: application/json`; `requeue` 500s on jobless datapoints. Analysis states: `na → init → queued → started → post-processing → completed` — no `stopping`/`failed`; grace-period waits anchor on CR `.status` timestamps, never server state. The queue fabric is Redis (Service `queue`, Resque queues `simulations` + `requeued`) — operator reads it directly, **read-only** (issue #12).
- **`/metrics` ingress is restricted to a scraper-namespace allow (issue #166).** `deploy/network-policy.yaml` ships with `openstudio-operator-metrics-ingress` (policyTypes: [Ingress]) that allows TCP/9090 to the operator pod only from (a) a namespace labeled `kubernetes.io/metadata.name: prometheus` and (b) a same-namespace peer carrying the label `app.kubernetes.io/component: metrics-scraper` (issue #295). The plaintext Prometheus endpoint (`metrics.py` `prometheus_client.start_http_server(bound_port, addr="0.0.0.0")` + `deploy/operator-deployment.yaml` `containerPort: 9090`) has no authN/authZ/TLS, so a cluster whose Prometheus runs in a differently-named namespace (`monitoring`, `kube-prometheus-stack`, `observability`, etc.) MUST edit the `namespaceSelector` label match in that policy before applying — otherwise the operator's metrics silently become unreadable. `tests/test_deploy_manifests.py::test_network_policy_metrics_ingress_has_prometheus_and_peer_allow` enforces both peers are present and that the same-namespace peer selector is label-scoped (NOT `podSelector: {}`, which would silently let every helm-chart pod — `web`, `web-background`, `worker`, `db` (Mongo), `redis`, `queue`, NFS — scrape the plaintext endpoint).
- **`metrics-scraper` label convention (issue #295).** The `app.kubernetes.io/component: metrics-scraper` label is the project's opt-in marker for pods that legitimately need to scrape the operator's `/metrics` endpoint from inside the `openstudio-server` namespace (e.g. a Prometheus sidecar, an in-cluster debug scraper). No helm-chart pod in this stack carries that value: `web` / `web-background` / `worker` / `db` (Mongo) / `redis` / `queue` / NFS all set their own `app.kubernetes.io/component`. When adding a new in-cluster scraper to `openstudio-server`, set `app.kubernetes.io/component: metrics-scraper` on its pod template — that single label is the contract the network policy trusts. Don't reintroduce `podSelector: {}` (or any broader selector) — that's the regression `tests/test_deploy_manifests.py::test_network_policy_metrics_ingress_has_prometheus_and_peer_allow` is wired to fail loudly.
- **Single public kubeconfig loader (issue #305).** `openstudio_operator._k8s.load_operator_kube_config()` is the SOLE call site for `load_incluster_config` / `load_kube_config`. The legacy wrappers `singleton._load_k8s_config` and `prune_entrypoint._load_kube_config` were removed (issue #405) — the `singleton.operator_*_api` factories and `prune_entrypoint.main()` call the public loader directly. The AST CI gate `tests/test_singleton_registry_coverage.py::test_only_one_kubeconfig_loader_call_site` rejects any inline `load_incluster_config(` / `load_kube_config(` call outside `_k8s.py` (catches both bare-name and attribute-shape calls). If you find yourself reaching for `load_kube_config()`, stop — call the public loader.

## Cluster validation (kind recipe)

`scripts/create-kind-cluster.sh` + `scripts/deploy-openstudio-stack.sh` stand up a single control-plane kind node and the full OpenStudio stack via a helm-chart overlay (`scripts/manifests/`). Designed for one node on purpose — the whole stack runs there, and images are pulled from Docker Hub by the kubelet, so no `kind load` is needed. `scripts/capture_fixtures.sh` captures live v3.11.0 fixtures into `tests/fixtures/live/`; `scripts/check_fixture_drift.py` diffs live vs. synthetic samples. Teardown: `scripts/teardown-kind-env.sh`. Live evidence in `docs/kind-validation.md`.

## Doc-drift guards (CI fails the build if you forget)

- **Issue #151** — `AGENTS.md` must contain the literal `# N tests across M files` claim matching `pytest --collect-only` + `ls tests/test_*.py | wc -l`. When you add tests, update this line in the same commit.
- **Issue #220** — same guard for `docs/onboarding.md` (plain prose form, no `# ` prefix).
- **Issue #152** — no surviving `TODO(phase N)` markers in `deploy/` or `scripts/` (initial-scaffold placeholders that should be deleted once the acceptance criterion ships).
- **Issues #150 / #219** — see the password literal rule above.
- **Metrics family drift** — `tests/_metrics_inventory.py::EXPECTED_*_FAMILIES` is the source of truth (shared canonical module, issue #406); adding a counter/gauge/histogram anywhere without updating the tuple fails CI.
- **Issue #301** — every PR body must carry a `Scope guard:` block that names the issues this PR touches and declares what is intentionally NOT being changed (with the follow-up issue that owns that area); `scripts/check_pr_body_scope.sh` is the CI gate that fails the `lint` job when the block is missing, lacks an issue reference, or has no rationale phrase.
