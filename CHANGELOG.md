# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Auto-improvement-loop iteration 2 (post-0.2.0): 34 new issues opened
(`#224`–`#257`), 38 total closed (4 pre-existing + 34 new) across 14
priority-ordered waves with dependency-aware merging. Major themes:
critical `NetworkPolicy` gaps (`#224` `#225`), least-privilege
`verbs:[*]` violation on the OSCM CR (`#228`), Mongo credential rotation
(`#219`), `OpenStudioClient` hardening (`#227` `#226` `#242`), structured
JSON log emission (`#256`), `/metrics` expansion 12+1+1 → 16+4+1
(`#237` `#238` `#239` `#253` `#254` `#255`), operator-side `events_sinks.py`
collapse + Redis factory centralization + handler registry + k8s client
factories + public `list_datapoints()` (`#234` `#235` `#250` `#251`
`#252`), per-handler exception coverage + live-fixture drift + Hypothesis
edge cases (`#246` `#247` `#248` `#249`).

### Added
- **`#256`** — structured JSON logs from the operator process (and the
  prune `CronJob`). `:func:`openstudio_operator.logging_setup.install_json_logging``
  installs a stdlib `logging.Formatter` subclass that emits one JSON
  object per log line (`timestamp` ISO-8601 UTC, `level`, `logger`,
  `message`, `module`, `funcName`, `lineno`, plus kopf-injected
  `namespace` / `name` when present). Invoked from `handlers/__init__.py`
  (operator process) and `prune_entrypoint.py::main` (CronJob). Documented
  in `README.md#logs`.
- **`#237`** — Prometheus surface for the dry-run gate on
  `EventEmitter` (`#164`). `openstudio_operator_events_dry_run_suppressed_total{reason}`
  + companion `openstudio_operator_events_emitted_total{reason}` (both
  labelled by the warning-event vocabulary). Headline SLO for an
  audit-only install: emitted-vs-suppressed rate ratio.
- **`#238`** — `openstudio_operator_resque_queue_depth{queue}` Gauge
  surfaces the operator's authoritative `LLEN` reads on every sensing
  tick. Cross-check against KEDA's external metrics view; the operator's
  depths disagreeing with KEDA's is the centralized-constants drift
  signal.
- **`#239`** — `openstudio_operator_singleton_election_total{outcome}`
  Counter at the three `SingletonGuard.enforce` post-decode branches
  (`idle` | `active` | `conflict`). Surfaces the silent-bypass failure
  mode when the kopf registry internals change shape and
  `install_singleton_guard` returns 0 without the AST coverage test
  catching it.
- **`#253`** — `openstudio_operator_redis_key_layout_status` Gauge
  (1.0=`ok`, 0.0=any-other). Surfaces the boot-time validator outcome
  (`#163`) as a cluster-wide latest-observation signal so an SRE can
  alert on `== 0` without log scraping. The post-`#44` failure mode
  (v3.11.0 layout drift takes `resque_workers_seen_max` silent, the
  stall condition fires vacuously) is observable here.
- **`#254`** — `openstudio_operator_stall_window_elapsed_seconds` Gauge
  tracks the `StallWindowTracker` state between the first sustained
  observation and the eventual `web_background_restarts_total` increment.
  Heads-up display that gives SREs time to react before the gate trips.
- **`#255`** — `openstudio_operator_events_emit_failures_total{reason}`
  Counter inside `EventEmitter.emit`'s try/except wrapper (BEFORE
  re-raising). Distinguishes "Event posting down" from "REST API down"
  — `handler_tick_failures_total{error_type=ApiException}` previously
  captured both under one label.
- **`#250`** — Python-level OSCM handler registry in
  `src/openstudio_operator/_oscm_handlers.py`. New handler modules MUST
  call `:func:`openstudio_operator._oscm_handlers.register`` at module
  import time; the singleton guard (`#14`) cross-checks the Python
  registry against the kopf registry at gate time. The four pre-existing
  handlers (`analysis_sla`, `datapoint_watchdog`, `web_background_monitor`,
  `worker_recycler`) remain registered with kopf directly; their IDs are
  listed in `KNOWN_LEGACY_OSCM_HANDLER_IDS` so the cross-check passes
  for them without retrofitting a `register()` call.
- **`#251`** — `:func:`openstudio_operator._k8s.deployment_label_selector``
  + `:class:`openstudio_operator._k8s.DeploymentReader`` shared K8s
  helpers. The previously cross-imported `deployment_label_selector`
  from `analysis_sla` is the canonical home now (`#236`); the singleton
  guard's `AppsV1Api` slice is rebuilt here too. Tests live in
  `tests/test_k8s_clients.py`.
- **`#234`** — `QueuedKopfEventSink` in `src/openstudio_operator/events_sinks.py`
  collapses the three near-identical queue/drain mechanisms from
  `handlers/__init__.py` (the `#116` redis-URL guard, the `#163`
  Resque key-layout guard, and the `#171` status-map-cap guard) into a
  single class. Each emitted Warning Event is queued in-process and
  flushed on the next OSCM watch event by a single `@kopf.on.event`
  drain handler installed by `handlers/__init__.py`.
- **`#252`** — `:func:`openstudio_operator.openstudio_client.list_datapoints``
  is the new public surface that `retention.py` uses; replaces the
  private `OpenStudioClient._request_json` reach-in. No behaviour change
  to the client itself.
- **`#242`** — `OpenStudioClient` session gets explicit TLS verification
  (default `True`; `verify=True` configurable via `tls_verify=False` in
  the `OpenStudioClient` constructor for self-signed cert scenarios).
- **`#227`** — `OpenStudioClient` mutating endpoints now send
  `Accept: application/json` (mirrors the GET surfaces). Without the
  header, mutating responses return `text/html` and the JSON parse path
  raises.
- **`#228`** — operator `Role` no longer grants `verbs: [*]` on the OSCM
  CR. The least-privilege verb set is enumerated explicitly:
  `get,list,watch,update,patch` on `openstudioclustermanagers` (the
  `update`/`patch` are the singleton guard's reconcile path on
  `.metadata.labels`, NOT status mutations — those go through the
  status subresource and remain `get,update,patch` on
  `openstudioclustermanagers/status`). AST CI gate in
  `tests/test_deploy_manifests.py` fails the build on any future
  `verbs: ['*']` reintroduction.
- **`#241`** — archival Jobs no longer inherit the ServiceAccount token
  (`automountServiceAccountToken: false` on the Job pod template); the
  archival pipeline never needs K8s API access, and the token would be
  a lateral-movement surface if the rclone Job were compromised.
- **`#240`** — `storagePolicy.secretRef` constrained by CEL validation
  to a name-matching allowlist (the operator-managed `rclone-credentials`
  Secret). Prevents a tainted `storagePolicy.bucket` from pointing at
  an arbitrary user Secret in the namespace.
- **`#219`** — kind-recipe Mongo credentials rotated from the
  publicly-known `openstudio` literal to `mongo-rotated-<random>` in
  `scripts/manifests/{01-mongo,04-web,05-web-background,06-worker}.yaml`.
  Fresh installs MUST run `scripts/rotate_mongo_credentials.sh` first;
  the `scripts/check_mongo_credentials_unique.sh` CI guard fails the
  build if the legacy literal re-appears.
- **`#225`** — allow-egress `NetworkPolicy` no longer mislabels the API
  server as `kube-dns`. The operator-to-apiserver egress now targets
  the apiserver's actual Service label, not the placeholder.
- **`#224`** — operator pod template gains
  `app.kubernetes.io/managed-by: openstudio-operator` so every
  `NetworkPolicy` selector in `deploy/network-policy.yaml` matches the
  actual pod. (The prune `CronJob` was already labelled correctly.)
- **`#246`** — Hypothesis property-based tests for
  `:func:`openstudio_operator._time.parse_utc`` edge cases
  (`tests/test_time_parsing.py`).
- **`#247`** — `tests/conftest.py` autouse fixture resets
  `singleton._process_guard` and the event-queue globals between tests
  so handler-boundary tests can run in any order without leaking state.
- **`#248`** — `tests/test_live_fixture_drift.py` value-level diff
  between the captured live v3.11.0 fixtures (`tests/fixtures/live/`)
  and the synthetic samples (`tests/fixtures/samples/`). Catches the
  drift mode where live and synthetic still parse to the same shape but
  diverge on enum values / status strings.
- **`#249`** — `tests/test_timer_wrapper_failures.py` direct tests for
  the four `@kopf.timer` wrappers (`analysis_sla`, `datapoint_watchdog`,
  `worker_recycler`, `web_background_monitor`) — per-handler exception
  tuple coverage of `handler_tick_failures_total{module, error_type}`
  rather than relying on `run_*_tick` end-to-end paths.
- **`#231`** — direct tests for the four `@kopf.timer` wrappers (not
  just `run_*_tick`). Complements `#249`'s exception-tuple coverage.
- **`#232`** — `tests/test_handler_boundaries.py` exercises the kopf 1.4x
  `MappingView` body end-to-end through `EventEmitter` + `run_*_tick` —
  the change in kopf 1.4x where handler `body` parameters became
  `MappingView` rather than `dict` is now a CI gate, not a "look the
  other way" assumption.
- **`#233`** — `tests/test_redis_client.py` covers the `redis_key_layout`
  degraded / unreachable / error / skipped branches (the validator path
  the boot-time check fires on `#163`).
- **`#257`** — `pytest` `slow` marker + duration budget to catch
  test-suite regressions before they land.

### Changed
- **`#235`** — `ReadOnlyRedisClient` construction is now centralized in
  `client_factory.get_read_only_redis_client(redis_url)`
  (`lru_cache(maxsize=8)`, keyed by URL), mirroring what `#168` did for
  `OpenStudioClient`. Retires three divergent sites:
  `web_background_monitor._get_redis_client` (module-level dict cache),
  `analysis_sla._default_redis_client` (fresh client — and a fresh
  connection pool — on every SLA tick), and the inline
  `ReadOnlyRedisClient(redis_url)` in the `#163` boot-time Resque
  key-layout check. The boot probe and every handler tick now share one
  connection pool per Redis URL. No behaviour change to the client
  itself (read-only allowlist, queue-depth reads, Resque worker
  liveness). New AST CI gate
  `tests/test_client_factory.py::test_only_one_read_only_redis_client_construction_point`
  fails on any inline `ReadOnlyRedisClient(` outside the factory.
- **`#226`** — `OpenStudioClient` mutating POSTs no longer retry on 5xx
  by default (retries on 5xx without idempotency keys risk duplicate
  state mutations); the retry policy is now keyed on the method
  (GET → retry on 5xx, POST/PATCH/DELETE → no retry without an explicit
  idempotency key). The behaviour is configurable per-instance for
  callers that can guarantee idempotency.
- **`#183`** (refresh) — `/metrics` surface enumeration in `README.md`
  and `AGENTS.md` updated to **16 counters + 4 gauges + 1 histogram**
  (was 11+1+0 / 12+1+1 at the 0.2.0 release). The drift that was
  caught in iteration 2 — every `metric.py` increment had landed in
  the registry but the docs/README/AGENTS still cited pre-`#171`
  numbers — is the canonical "drift in three places at once" failure
  mode and is now CI-gated by `tests/test_metrics_endpoint.py`.
- **`#181`** (refresh) — audit `Appendix D` counter / gauge / histogram
  table refreshed to match the 16+4+1 registry
  (`EVENTS_DRY_RUN_SUPPRESSED_TOTAL`, `EVENTS_EMITTED_TOTAL`,
  `SINGLETON_ELECTION_TOTAL`, `EVENTS_EMIT_FAILURES_TOTAL`,
  `RESQUE_QUEUE_DEPTH`, `REDIS_KEY_LAYOUT_STATUS`,
  `STALL_WINDOW_ELAPSED_SECONDS`, plus `STATUS_MAP_CAPS_TOTAL`).
- **`#220` / `#243`** — drift closeout: `AGENTS.md` + `docs/onboarding.md`
  test count claim aligned to `529 tests across 28 files` (was 342/17
  pre-`#178`); `docs/kind-validation.md` acceptance criteria refreshed
  to the current 16+4+1 metric surface and the current test count.
- **`#244`** — `README.md` + `AGENTS.md` Repository layout sections
  list the four new shared-utility modules (`events_sinks.py`, `_k8s.py`,
  `_oscm_handlers.py`, `logging_setup.py`).
- **`#294`** — `ValidatingAdmissionPolicy` + `ValidatingAdmissionPolicyBinding`
  (`openstudio-prune-job-scope`) added to `deploy/storage-cronjob.yaml`
  constrains the prune SA's `batch/jobs` `create|update|delete` verbs to
  Jobs that carry the archival labels (`app.kubernetes.io/managed-by=
  openstudio-operator` AND `app.kubernetes.io/component=archival`). RBAC
  `PolicyRule` has no `labelSelector` and `resourceNames` only accepts
  exact strings (no globs), so the constraint is enforced at admission
  rather than RBAC. `failurePolicy: Fail` + `namespaceSelector` scoped
  to `openstudio-server` make this the third defence-in-depth layer
  after the namespaced `#78` Role split and the `#78` deterministic
  Job name. Requires K8s 1.30+ (ValidatingAdmissionPolicy v1 GA).

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
  `AGENTS.md` (11 counters + 1 gauge as-of #183; the registry is
  12 counters + 1 gauge + 1 histogram at this release after
  #171 / #179); test invariant updated in
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
  `EXPECTED_GAUGE_FAMILIES` (CI drift guard); 11 counters + 1 gauge
  as-of this release (`#183`) — the registry is 12 counters + 1 gauge
  + 1 histogram at HEAD post-#171 / #179.
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