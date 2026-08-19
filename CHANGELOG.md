# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- (no entries yet — all changes since 0.1.0 are in [0.2.0])

### Changed
- (no entries yet — all changes since 0.1.0 are in [0.2.0])

### Removed
- (no entries yet — all changes since 0.1.0 are in [0.2.0])

## [0.2.0] - 2026-08-19

Auto-improvement-wave 10 consolidation: 32 issues closed in the recent
post-0.1.0 sweep across operator hardening, observability, CI gating,
RBAC, security, and developer ergonomics. This entry curates the
behaviour-changing deltas; the GitHub auto-generated release body is
otherwise a raw commit-list dump with no narrative of intent. The
detailed v3.11.0 contract work and architectural pivots (KEDA,
CronJob extraction, SLA re-source) remain anchored on 0.1.0; this
release is the cleanup / docs / observability / CI pass that landed
on the same day.

### Added

- **`#180`** — prune `CronJob` inherits the `#116` `redisUrl`-empty
  guard: emits a `Warning` Event and exits with code `3` when
  `spec.redisUrl` arrives empty at the prune actor. Closes the
  parity gap between operator and pruner redisUrl handling.
- **`#183`** — `/metrics` surface enumeration in `README.md` and
  `AGENTS.md` (11 counters + 1 gauge); test invariant updated in
  `tests/test_metrics_endpoint.py` to fail loudly on drift.
- **`#178`** — first-time-contributor onboarding doc
  (`docs/onboarding.md`): setup, single-test / full-suite commands,
  venv-drift guard (`#71`), the 5-step "add a new OSCM timer
  handler" pattern, Working-rules-that-bite, audit-doc update
  rules, branch / PR conventions, good-first-PR candidates.
- **`#182`** — `docs/kind-validation.md` test / counter count
  refresh (343 → 427).
- **`#177`** — `CHANGELOG.md` (this entry's parent).
- **`#176`** — `docs/architecture-plan.md` §7 RBAC example aligned
  with `deploy/rbac.yaml`.
- **`#181`** — audit Appendix D counter / gauge table refreshed
  (8 → 11+1) to match the post-`#119` / post-`#117` metric families.
- **`#175`** — audit doc references rewritten
  (`storage_pruner` → `retention`; `STORAGE_FREED_BYTES` removed in
  `#50`).
- **`#174`** — unify tz-aware UTC parsing in
  `src/openstudio_operator/_time.py` (D12 boundary).
- **`#179`** — `openstudio_operator_analysis_datapoint_count`
  Histogram (per-observation distribution).
- **`#173`** — `requirements.lock` with `--hash=sha256:` pinning
  + CI verify.
- **`#172`** — kind-recipe upstream images pinned by SHA256 digest
  + EOL bumps (mongo `6.0` → `7.0`, redis `6.0` → `7.4`).
- **`#171`** — `status_map` defensive cap (drop oldest + `Warning`
  Event + counter).
- **`#169`** — cosign verify-attestation identity pinned to
  `release.yml@refs/(heads/develop|tags/v*)`.
- **`#168`** — centralize `OpenStudioClient` factory in
  `client_factory.py`.
- **`#170`** — CRD minimum constraints + CEL validation for
  numeric policy fields.
- **`#167`** — `StallWindowTracker` documents the singleton-guard
  key invariant.
- **`#166`** — `NetworkPolicy` restricting `/metrics` ingress to
  the `prometheus` namespace (and same-namespace peer). Plaintext
  Prometheus endpoint has no authN/authZ/TLS so a cluster whose
  scraper namespace is differently-named must edit the
  `namespaceSelector` label match before applying — otherwise the
  operator's metrics silently become unreadable.
- **`#165`** — centralize operator-behavior constants in
  `_constants.py`.
- **`#164`** — `EventEmitter` moved to
  `openstudio_operator/events.py`.
- **`#163`** — `validate_key_layout()` wired into operator boot
  (D05 / D13 invariant).
- **`#162`** — storage `CronJob` container + pod `securityContext`
  (`runAsNonRoot`, `runAsUser`, `seccompProfile`, `fsGroup`).
- **`#160`** — CRD `target*Deployment` + `serverUrl` pattern + CEL
  validation.
- **`#159`** — CI cosign verify-attestation gate for the `:dev`
  image provenance.
- **`#161`** — pod-level `securityContext` on operator Deployment
  + archival Jobs.
- **`#158`** — centralize K8s client construction in
  `singleton.operator_custom_objects_api()`.
- **`#155`** — `release.yml` generates the SLSA predicate inline
  (no missing-file reference; addressed the `#157` root cause).
- **`#154`** — narrow deny-egress `NetworkPolicy` selector (was
  matching every pod, breaking helm-chart egress).
- **`#151`** — CI guard for `AGENTS.md` test-count claim vs
  `pytest --collect-only`.
- **`#152`** — CI guard for `deploy/` + `scripts/`
  `TODO(phase N)` markers.
- **`#150`** — rotate kind-recipe Redis password out of public
  manifests. Fresh installs MUST run
  `scripts/rotate_redis_password.sh` first; the
  `scripts/check_redis_password_unique.sh` CI guard fails the
  build if the legacy `openstudio` literal re-appears as a
  Redis password in any of the five affected manifest files.
- **`#149`** — `release.yml` pins the operator image by SHA256
  digest at build time.

### Changed

- **KEDA migration** (`#77`) — replaces the custom Redis
  HPA-floor adjuster (`handlers/hpa_floor.py`) with a standard
  KEDA `ScaledObject`. KEDA ≥ 2.20 in namespace `keda` is a
  cluster prerequisite; two autoscalers on the same Deployment
  oscillate. The operator's Role carries no
  `horizontalpodautoscalers` verbs.
- **Storage / archival pipeline extracted to native primitives**
  (`#78`) — NFS pruning becomes a Kubernetes `CronJob`
  (`deploy/storage-cronjob.yaml`) invoking
  `openstudio_operator.prune_entrypoint`. Operator process owns
  **no** storage polling loop. Archival remains the same
  backend-agnostic rclone Job manifest generator
  (`archival.py` — `s3|gcs|azure`, `envFrom`-only creds,
  verified-upload gate). RBAC shrunk: no `batch` verbs on the
  operator's Role (the `CronJob` has its own ServiceAccount).
- **SLA clock + escalation re-sourced against verified v3.11.0**
  (`#83`, `#105`, `#104`) — Module 1 SLA clock anchors on
  operator-observed state transitions (first sight of `started`
  via `/analyses/{id}/status.json` plus CR `.status` timestamps);
  Module 4 escalation matches Resque worker identity → pod name
  instead of dp `ip_address` → pod IP. Dead `OpenStudioClient`
  methods (`get_analysis_page_data`, `get_datapoints_full`)
  trimmed.
- **RBAC narrowed** — operator's Role is namespaced with
  enumerated verbs: no `horizontalpodautoscalers` post-`#77`, no
  `batch` post-`#78`. Autoscaling lives entirely in KEDA's
  ServiceAccount; storage lives entirely in the prune-`CronJob`
  ServiceAccount. `deploy/rbac.yaml` is the source of truth
  (`#111` aligned the plan doc's §7 RBAC snippet to match).

### Removed

- **`src/openstudio_operator/handlers/hpa_floor.py`** (`#77`,
  `#50`) and its tests (`tests/test_hpa_floor.py`),
  `horizontalpodautoscalers` verbs in `deploy/rbac.yaml`, the
  `openstudio_operator_hpa_floor_adjustments_total` counter, and
  `DEFAULT_HPA_FLOOR_COOLDOWN_SECONDS` from `config.py` /
  `deploy/crd.yaml`.
- **`openstudio_operator_storage_freed_bytes` counter** (`#50`)
  — zero-information; the storage-pipeline move to native
  `CronJob`s (`#78`) further removes its source.

### Fixed

- **Singleton guard building bare `CustomObjectsApi`** (`#79`) —
  guard now uses in-cluster config; the operator was functionally
  dead in-cluster prior to the fix.
- **Release workflow cosign predicate missing** (`#157`) —
  `release.yml` referenced `release-slsa.json` that did not exist;
  the cosign-signed SLSA attestation is now attached to the
  published digest.
- **Storage pruner `redisUrl` parity** (`#180`) — extends the
  `#116` redisUrl-empty guard into `prune_entrypoint.py` so the
  prune actor also emits a `Warning` Event on a misconfigured
  `spec.redisUrl`.
- **Venv drift in wave-orchestration worktrees** (`#71`,
  `#147`) — `scripts/check_editable_install.sh` checks that
  `openstudio_operator` resolves against the active checkout
  before running tests; the script's test-file path list covers
  the six-plus new test files added across waves.
- **Stale `TODO(phase 1)` in `deploy/operator-deployment.yaml:2`**
  (`#146`) — removed; replaced with manifest comments
  referencing the validation and hardening work.
- **`scripts/kind-config.yaml` is single-node on purpose**
  (`#86`) — the prior "local 3-node kind" claim was misleading;
  AGENTS.md / scripts clarified the deliberate single-node
  choice (one node runs the whole stack, image pull is direct
  from Docker Hub).
- **`README.md` Status sentence** (`#85`, `#145`, `#100`) — live
  kind-cluster validation evidence replaces the "tracked in
  #83" placeholder.

### Security

- See `Added` → cosign verify-attestation gate (`#159`, `#169`),
  pod-level `securityContext` (`#161`, `#162`), `NetworkPolicy`
  `/metrics` ingress restriction (`#166`), digest pinning
  (`#149`, `#172`), rotation of the kind-recipe Redis password
  (`#150`).

## [0.1.0] - 2026-08-19

First tagged release of the OpenStudio Server Kubernetes operator. The
project is at 0.x development; this entry consolidates all
Goal-affecting work landed before the first release cut — the GitHub
auto-generated release body is otherwise a raw commit-list dump with
no curation of behaviour changes, RBAC shrinks, or storage/archival
pipeline moves.

### Added

- **NetworkPolicy** (`#112`) — apply a default-deny egress
  `NetworkPolicy` plus per-actor allow-lists for the operator surface
  (operator-only egress to API server + Redis + OpenStudio web; storage
  egress HTTPS-only to RFC1918 excepted; DNS allow). Closes the
  archival-Jobs-egress-anywhere gap that let a tainted
  `storagePolicy.bucket` or a compromised secret env var steer `rclone`
  toward any host on the internet. Bug-fix follow-up (`#156`) narrowed
  the deny-policy `podSelector` to operator-managed pods so the helm
  chart's `web` / `web-background` / `worker` / `queue` / `db` / NFS
  pods retain their existing egress.
- **SLSA provenance + SPDX SBOM + cosign keyless signing** (`#113`,
  `#124`, `#157`) — every released image (`:dev` and `:v*`) is now
  built with `provenance: mode=max` and `sbom: true` (SLSA-level
  provenance + SPDX SBOM written into the image as OCI attestations),
  then cosign-keyless-signed against GitHub Actions OIDC. CI guard
  (`cosign verify-attestation --type slsaprovenance`) fails the release
  job if the published digest carries no provenance. Operator, rclone,
  and `python:3.12-slim` base images are all pinned by SHA256 digest.
- **Operator Deployment hardening** (`#115`) — `securityContext` matching
  the sibling CronJob's: `runAsNonRoot: true`, `runAsUser: 1000`,
  `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`,
  `capabilities.drop: [ALL]`, restricted `seccompProfile`. CI test
  asserts the manifest. Rclone archival Job manifests hardened in
  parallel (`#114`).
- **`openstudio_operator_handler_tick_failures_total{module, error_type}`**
  (`#117`) — labelled Prometheus counter incremented inside each of the
  four timer wrappers' except branches (`analysis_sla` /
  `datapoint_watchdog` / `worker_recycler` / `web_background_monitor`).
  Closed the sustained-degraded-window observability gap: prior to
  this, a Redis or REST API outage only surfaced as log lines, and an
  SRE alerting on `openstudio_operator_*` saw zero signal.
- **`openstudio_operator_status_conflicts_total` and
  `openstudio_operator_status_conflict_retries_exhausted_total`**
  (`#119`) — counters around the `status_store._mutate` 409 retry
  budget. The first increments per 409 before the backoff sleep; the
  second increments immediately before raising
  `StatusStoreConflictError` at exhaustion. Distinguishes "REST API
  down" from "concurrent writes racing on the same `.status` map" and
  surfaces the ~30 s worst-case jittered-backoff cost of a conflict
  storm.
- **SLA escalation Event re-emission bound** (`#118`) — caps the
  per-failure Warning Event re-emission when the worker-pod-delete
  escalation persistently fails, so a degraded cluster doesn't drown
  the Event stream.
- **Resque-worker gauge semantics fix** (`#87`) — the
  `openstudio_operator_resque_workers_seen_max` gauge now reads worker
  cardinality on every tick regardless of queue depth. Idle-but-healthy
  fleets no longer scrape `0.0`; `== 0` with reachable Redis is again
  the unambiguous "no workers registered" alert.
- **`redisUrl`-empty guard** (`#116`, `#180`) — CRD default `spec.redisUrl`
  is now an empty string (no more baked-in `openstudio` password), and
  both `openstudio_operator` and `prune_entrypoint` emit a per-CR
  `Warning` Event and refuse to operate when the field arrives at the
  empty default. Kind-recipe Redis password rotated to a non-default
  value so the empty-default path is provably broken on a fresh
  cluster.
- **Operator-process single-replica `Recreate`** — the operator
  Deployment is single-replica with `strategy: Recreate`. One active
  poller, no leader election. Part of the singleton-guard
  implementation (`#14`, `#79`).
- **Prometheus `/metrics` scrape endpoint** (`:9090`) — operator-only
  observability window. Family inventory mirrored in
  `tests/test_metrics_endpoint.py::EXPECTED_COUNTER_FAMILIES` /
  `EXPECTED_GAUGE_FAMILIES` (CI drift guard); 11 counters + 1 gauge at
  this release (`#183`).
- **First-time-contributor onboarding doc** (`docs/onboarding.md`,
  `#178`) — setup, single-test / full-suite commands, venv-drift guard
  (`#71`), the 5-step "add a new OSCM timer handler" pattern,
  Working-rules-that-bite, audit-doc update rules, branch / PR
  conventions, good-first-PR candidates.

### Changed

- **KEDA replaces the custom Redis HPA-floor adjuster** (`#77`) — the
  Phase-4 custom reconciliation loop in `handlers/hpa_floor.py` is
  deleted. Worker autoscaling is now a standard KEDA `ScaledObject`
  with a Redis-list trigger against the Resque `simulations` +
  `requeued` queue depths. KEDA is a cluster prerequisite (helm
  `kedacore/keda` ≥ 2.20 in namespace `keda`); the helm chart's
  `worker-hpa` HPA must be disabled — two autoscalers on the same
  Deployment oscillate. The operator's Role carries no
  `horizontalpodautoscalers` verbs. The
  `openstudio_operator_hpa_floor_adjustments_total` counter is removed
  (`#50`).
- **Storage / archival pipeline extracted to native primitives**
  (`#78`) — NFS pruning is now a Kubernetes `CronJob`
  (`deploy/storage-cronjob.yaml`) invoking
  `openstudio_operator.prune_entrypoint`. Operator process owns **no**
  storage polling loop; background NFS-watermark polling is gone.
  Archival remains the same backend-agnostic rclone Job manifest
  generator (`archival.py` — `s3|gcs|azure`, `envFrom`-only creds,
  verified-upload gate). RBAC shrunk: no `batch` verbs on the
  operator's Role (CronJob has its own ServiceAccount). Retention
  pipeline anchored on `retention.eligibility → archival.verified →
  DELETE /analyses/{id}`; the server-side cascade rm-rf's the NFS asset
  dirs.
- **SLA clock + escalation re-sourced against verified v3.11.0**
  (`#83`, `#105`, `#104`) — D1/D2 contract drift resolved. The
  v3.11.0 REST contract verified live in kind confirmed that
  `GET /analyses/{id}/page_data.json` and `GET /analyses.json` never
  carry `status` or `start_time` fields, and that datapoint
  `ip_address` is always null. Module 1 SLA clock now anchors on
  operator-observed state transitions (first sight of `started` via
  `/analyses/{id}/status.json` plus CR `.status` timestamps) and
  Module 4 escalation matches Resque worker identity → pod name
  (label/annotation-based) instead of dp `ip_address` → pod IP.
  Pre-`#83` LEGACY escalation seams removed from `analysis_sla.py`;
  dead `OpenStudioClient` methods (`get_analysis_page_data`,
  `get_datapoints_full`) trimmed.
- **RBAC narrowed** — the operator's Role is namespaced with
  enumerated verbs: no `horizontalpodautoscalers` post-`#77`, no
  `batch` post-`#78`. Autoscaling lives entirely in KEDA's
  ServiceAccount; storage lives entirely in the prune-CronJob
  ServiceAccount. `deploy/rbac.yaml` is the source of truth
  (`#111` aligned the plan doc's §7 RBAC snippet to match).
- **Image digests pinned** (`#124`) — operator Dockerfile base, rclone
  archival image, and Python base all pinned by SHA256 digest.
- **`docs/contracts/openstudio-server-v3.11.0-rest.md` is ground
  truth** — the verified v3.11.0 REST contract is vendored in-repo.
  `scripts/capture_fixtures.sh` repointed to the new contract path
  (`#109`); worker-Deployment row rewritten post-`#77`
  (`#126`).
- **`README.md` Status section** rewritten post-`#77`/`#78`/`#100` to
  describe the actual pipeline (no more "tracked in #83" placeholder,
  `#96` is the resolution path). Repository layout collapsed
  (`#108`) to remove the duplicate `deploy/` block.
- **`AGENTS.md`** — re-verify-don't-re-assert update protocol; test
  count claim drifted to 342/17 (`#106` → `#144` → `#148`); redisUrl
  guard rule (`#116`); merge-subject hygiene (`#88`); final-convergence
  drift closeout (`#148`).
- **CI gating policy** (`#89`) — `lint` + `test` + `guard-branch-pairing`
  required on PR merges into `develop` and into `main`. Outage bypass
  via admin merge is allowed when GitHub Actions is unavailable, with
  retroactive CI run once runners recover.

### Removed

- **`src/openstudio_operator/handlers/hpa_floor.py`** (`#77`) and its
  tests (`tests/test_hpa_floor.py`), `horizontalpodautoscalers`
  verbs in `deploy/rbac.yaml`, the
  `openstudio_operator_hpa_floor_adjustments_total` counter (`#50`),
  and `DEFAULT_HPA_FLOOR_COOLDOWN_SECONDS` from `config.py` /
  `deploy/crd.yaml`.
- **`openstudio_operator_storage_freed_bytes` counter** (`#50`) —
  zero-information; the storage-pipeline move to native CronJobs
  (`#78`) further removes its source.
- **LEGACY pre-`#83` escalation seams** (`#105`) — dead branches in
  `analysis_sla.py` that assumed `page_data.json` carried
  `status` / `start_time`.
- **`OpenStudioClient.get_analysis_page_data` /
  `get_datapoints_full`** (`#104`) — dead after `#105`; the SLA clock
  anchor moved to `/status.json`.

### Fixed

- **Singleton guard building bare `CustomObjectsApi`** (`#79`) — the
  guard now uses in-cluster config; the operator was functionally dead
  in-cluster prior to the fix.
- **Release workflow cosign predicate missing** (`#157`) — `release.yml`
  referenced `release-slsa.json` that did not exist in the repo; the
  cosign-signed SLSA attestation is now attached to the published
  digest.
- **Storage pruner `redisUrl` parity** (`#180`) — extends the
  `#116` redisUrl-empty guard into `prune_entrypoint.py` so the prune
  actor also emits a Warning Event on a misconfigured `spec.redisUrl`.
- **Venv drift in wave-orchestration worktrees** (`#71`, `#147`) —
  `scripts/check_editable_install.sh` checks that
  `openstudio_operator` resolves against the active checkout before
  running tests; the script's test-file path list covers the
  six-plus new test files added across waves.
- **Stale `TODO(phase 1)` in `deploy/operator-deployment.yaml:2`**
  (`#146`) — removed; replaced with manifest comments referencing the
  validation and hardening work.
- **`scripts/kind-config.yaml` is single-node on purpose** (`#86`) —
  the prior "local 3-node kind" claim was misleading; AGENTS.md /
  scripts clarified the deliberate single-node choice (one node
  runs the whole stack, image pull is direct from Docker Hub).
- **`README.md` Status sentence** (`#85`, `#145`, `#100`) — live
  kind-cluster validation evidence replaces the "tracked in #83"
  placeholder.

### Security

- See `Added` → SLSA / SBOM / cosign (`#113`), operator hardening
  (`#115`), archival Job hardening (`#114`), NetworkPolicy (`#112`,
  `#156`), and redisUrl-guard (`#116`, `#180`).

[Unreleased]: https://github.com/anchapin/openstudio-server-operator/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/anchapin/openstudio-server-operator/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/anchapin/openstudio-server-operator/releases/tag/v0.1.0