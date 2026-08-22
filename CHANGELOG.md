# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Auto-improvement-loop sessions post-0.2.0 (2026-08-20 → 2026-08-22):
four full audits opened `#387`–`#417`, `#462`–`#507`, and follow-ups
(`#531`, `#544`); waves closed the security/reliability/observability/
architecture headliners — credential fences (`#462` sentinel Secrets,
`#463` Redis secretRef, `#479` runtime-only image lockfile), scheduler
observability (`#469` heartbeat gauge, `#471` REST retries counter,
`#468` PrometheusRule + Grafana, `#492` config-posture gauges,
`#489` status-map lead-time gauge, `#504` build_info, `#491`
wrap-count gauge), prune-failure surfacing (`#470`), transport +
egress hardening (`#476` rediss:// TLS, `#477` IMDS/CGNAT egress
blocks), the `#473` tick-runner extraction ending four-copy wrapper
drift (completed by `#493` wiring-failure skip-tick), the `#475`
exception-hierarchy fix, architecture consolidation (`#494` K8s
factory helper, `#496` events alias unification, `#497` per-CR
cache convention), the test-infrastructure waves (`#474`+`#531`
shared fakes, `#482` config parsing, `#483` rolling-restart paths,
`#485` deploy-inventory gate, `#501` EventEmitter coverage), and
the documentation set (`#484` audit-policy index, `#487` ADR index,
`#486` snapshot-cruft prune, `#503` CONTRIBUTING setup) — alongside
the earlier iteration's
NetworkPolicy/RBAC/JSON-logging/metrics-expansion themes (`#224`–`#257`).

### Changed
- **`#497`** — one per-CR cache keying + reset convention for the
  handler modules' in-memory layer (the D04 "cache, never source of
  truth" tier), documented with a full census in the new
  `openstudio_operator/_cr_cache.py`: every module-level per-CR cache
  is keyed by `(namespace, name)` and UID-VALIDATED (a lookup under a
  different `metadata.uid` is the #364 delete+recreate signature and
  starts fresh — closes the leak at lookup time, no deletion hook
  needed), and every cache-bearing module exposes a uniform
  `reset_per_cr_caches(namespace=None, name=None)` seam. Census
  outcomes: `datapoint_watchdog._EXHAUSTED_WARNED` was an UN-keyed
  `set[str]` (one-CR assumption — a recreated CR inherited the deleted
  CR's exhaustion-dedup entries) → re-keyed + uid-validated + seam;
  `web_background_monitor._tracker_cache` was already
  `(namespace, name)`-keyed (#167) but never invalidated → uid
  validation + seam (the leak could satisfy a sustained stall window
  with the deleted CR's 9 accumulated minutes and restart
  web_background early — not conservative); `analysis_sla` and
  `worker_recycler` hold NO per-CR module cache (memory is CR
  `.status`, D04) and stay seam-free; the #44 leg-2 safeguard globals
  are deliberately un-keyed process-lifetime diagnostics (Redis-layout
  property, not CR property) — documented, unchanged;
  `events_sinks` queues carry per-entry `(namespace, name)` tuples
  drained on the DELETED watch event too, and the #402 persisted
  mirror dies with the CR — documented, unchanged. `singleton` /
  `client_factory` out of scope (#494/#491 territory; their seams
  already exist). Seams wired into the autouse conftest reset;
  delete+recreate leak proofs at the seam AND through the real
  `run_stall_tick` / `run_watchdog_tick` (a recreated CR earns a fresh
  stall window and re-earns its one-shot exhaustion Warning).
  Follow-up gap: no `@kopf.on.delete` handler exists to call the seams
  on CR deletion — the uid validation makes that wiring optional, not
  blocking.
- **`#309`** — outcome / trigger labels on the four unlabelled action
  counters so the dashboard can distinguish distinct outcomes without
  log scraping.
  - `openstudio_operator_soft_stops_total{outcome}` (`issued` | `dry-run`)
  - `openstudio_operator_workers_recycled_total{trigger}` (`analysis-completed` | `interval-elapsed`)
  - `openstudio_operator_worker_pods_evicted_total{outcome}` (`evicted` | `evicted-partial` | `no-matching-pods` | `dry-run`)
  - `openstudio_operator_analyses_deleted_total{outcome}` (`deleted`)
  CI gate tests pin all four label vocabularies so a future refactor
  that drops or renames a label is caught at CI rather than at
  the on-call's Grafana board.
- **`#473`** — `_oscm_handlers.run_oscm_tick(...)` + `SKIP_TICK_EXCEPTIONS`
  now own the entire kopf timer-wrapper tail: config parse, empty-
  serverUrl idle check, StatusStore/EventEmitter construction, the
  `handler_tick_failures_total` increment, and the single
  "tick skipped, retrying next poll" log line (one site in `src/`).
  The four `@kopf.timer` wrappers (still `register_fn`-registered —
  singleton-guard AST gate green) delegate via `wire` + `tick` closures.
  The canonical skip tuple is the UNION of the four historical per-module
  tuples — `(OpenStudioApiError, StatusStoreError, ApiException,
  RedisClientError)` — so no handler silently lost a catch (the copies
  had already diverged: `analysis_sla` alone caught `RedisClientError`,
  `datapoint_watchdog` alone omitted `ApiException`). The onboarding
  "add a new OSCM timer handler" 5-step pattern shrinks accordingly.
  The remaining half of `#395`: timing + rolling-restart went first;
  this extracts the wrapper wiring itself.
- **`#479`** — the production image installs from a runtime-only,
  hash-pinned `requirements.txt` (30 packages vs 40 in the dev
  `requirements.lock` — the dropped 10 are exactly the dev closure);
  `requirements.lock` stays byte-identical for CI. release.yml asserts
  dev-tool absence by running the BUILT image (`importlib.find_spec`
  blacklist on pytest/hypothesis/responses/fakeredis/ruff) — in
  publish-dev between build and the digest-pin commit so a leak can
  never be pinned into `deploy/` — and ci.yml verifies the runtime
  lockfile installs hash-clean in a fresh venv on every PR.
  `tests/test_dependency_drift.py` gates both lockfiles against
  `pyproject.toml`; both are refreshed together by the documented
  pip-compile pair.
- **`#494`** — one `_cached_k8s_api(cache_attr, build, *, label, strict)`
  helper owns the lazy-global caching, `load_operator_kube_config()`,
  strict-raise vs placeholder-on-`ConfigException` semantics, and the
  once-stated rationale docstring for the four K8s client factories;
  the public names (`operator_custom_objects_api` etc.) stay as thin
  delegates. Per-class module globals preserved deliberately (tests
  monkeypatch them; `reset_operator_k8s_client()` assigns them), and
  the construction-thunk shape keeps the bare `XApi()` call
  AST-visible so the #158/#251/#305 gates are green unmodified.
  singleton.py 835→790 lines.
- **`#496`** — `events.py` is the canonical home for the emitter
  aliases and the kopf.event wrapper: `TickEmitter` (3-arg),
  `EventSink` (4-arg object-attached), and the shared
  `emit_kopf_event()`. retention.py's colliding
  `EventEmitter = Callable[...]` alias (same public name as the
  events.py class, incompatible signature — a live trap) is gone;
  singleton.py and dry_run_audit.py drop their duplicate
  `EventSink`/`_emit_kopf_event` definitions.
  `status_store.EmitStatusEvent` stays local with a justification
  comment (zero external importers; `events_sinks.StatusEventSink` is
  the public seam for that shape).
- **`#531`** — one shared `FakeAppsV1Api` in `tests/_fakes.py` for the
  deployment-patch surface: records every patch attempt (byte-identical
  shape to the old copies) AND applies real RFC 7386 merge semantics
  via `_fakes._merge_patch`, with `fail_with` injection and the helm
  worker selector as the default `match_labels`. All four consumers
  (test_analysis_sla, test_web_background_monitor, test_worker_recycler,
  test_k8s_rolling_restart) import it; the #483 variant folds in; no
  subclasses needed; zero assertion rewrites. Completes the #474
  census.

### Added
- **`#491`** — `openstudio_operator_singleton_wrapped_handlers`: boot-time
  gauge recording how many OSCM spawning (timer/daemon) handlers
  `install_singleton_guard` actually wrapped (set at the end of the
  install — the count of registry entries whose fn carries the gate
  marker, so an idempotent re-install keeps reporting the gated
  population; `0` on the registry-internals-mismatch branch). The kopf
  pin exists because the gate walks kopf's private
  `registry._spawning._handlers`; a kopf upgrade that moves that
  internal wraps NOTHING — silently disabling D05 enforcement while the
  operator appears healthy. The registry-coverage CI test fences the
  layout at build time; this gauge is the runtime fence, scrapeable:
  `== 0` on a booted operator that expects timers is the silent-unwrap
  failure mode (shipped as the `OpenStudioOperatorSingletonGuardUnwrapped`
  alert in `deploy/prometheustrule.yaml`, `for: 5m` — the complement of
  the #469 heartbeat: heartbeat proves scheduling, wrap count proves
  guarding). Unlabelled, one series; registry now 20 counters + 16
  gauges + 3 histograms.
- **`#492`** — config-state posture gauges: `openstudio_operator_dry_run_active`,
  `openstudio_operator_server_url_set`, `openstudio_operator_redis_url_set`,
  `openstudio_operator_auto_soft_stop_enabled`, each 1/0 labelled by
  `(namespace, name)` (CR identity, #311; bounded by D05). The operator's
  behavior is steered by CR spec fields but none were represented at
  /metrics: an audit-only install (`dryRun=true` left on after canary
  staging) was invisible in an idle cluster because
  `events_dry_run_suppressed_total` (#237) only increments when an action
  is attempted — a quiet dry-run operator and a quiet live operator
  produced identical scrapes. Two stamp sites by design: the shared
  tick-runner `run_oscm_tick` stamps all four immediately after
  `OperatorConfig.from_spec` succeeds (before the idle check and the
  guarded try — posture survives idle and wiring-failing ticks, #493),
  and the `dry_run_audit` watch handler (#397 wiring point) flips
  `dry_run_active` immediately on real `spec.dryRun` transitions (no
  next-tick latency; transition-gated like the DryRunToggled Event,
  D11-exempt, loser CRs covered). `redis_url_set` counts the #463
  `secretRef` as a URL source alongside inline `spec.redisUrl`. Shipped
  with the `OpenStudioOperatorDryRunActive` alert (`== 1` for 1h, the
  migration window; deploy/prometheustrule.yaml) and a "Config posture"
  stat panel (deploy/grafana-dashboard.json, 14 panels). Registry now
  20 counters + 13 gauges + 3 histograms.
- **`#474`** — `tests/_fakes.py` is the single definition site for the
  byte-identical test fakes: `FakeCustomObjectsApi` (union semantics;
  the 409 injector folded in as `patch_conflicts=`; prune's list+get
  shape via `items=`), RFC 7386 `_merge_patch`, `make_emit`,
  parameterized `make_cr` (`default_spec=`), `calls_to`,
  `tick_failures_total`. 10 consumer files converted, net −226 lines,
  zero assertion-logic changes, collected count unchanged. Genuinely
  divergent variants stay local and documented (singleton's
  read-only-proof fake, landed-only counting subclasses, 3 divergent
  `make_cr` locals).
- **`#476`** — `rediss://` TLS end-to-end: CRD `redisUrl` pattern + CEL
  rule accept both schemes with the `#463` no-embedded-userinfo fence
  intact; the client connects via `redis.Redis.from_url` (redis-py
  selects `SSLConnection` natively — cert required, hostname check,
  system CAs); `REDIS_TLS_CA_BUNDLE` overrides the CA path, validated
  like `#296` (`OperatorConfigError` from `config.py`, the `#475`
  hierarchy pinned by test). The `#463` secretRef path passes
  `rediss://` values through the same fence checks.
- **`#482`** — `tests/test_config.py` (11 tests) pins
  `OperatorConfig.from_spec`'s actual contract: the full camelCase →
  snake_case mapping walk, empty-spec defaults, missing policy
  sub-dicts, string-int pass-through (`maxDurationMinutes: "180"` —
  CRD admission is the real type gate), invalid storage backend
  (validated at `archival.build_archival_job` + CRD enum),
  `maxAutoRequeues: 0` warn-only semantics, `#463` redisCredentials
  null/absent/malformed handling.
- **`#483`** — `tests/test_k8s_rolling_restart.py` (8 tests) pins the
  shared `#395` helper directly: merge-patch body carries ONLY
  `kubectl.kubernetes.io/restartedAt` (sibling annotations survive via
  a fake applying real RFC 7386 semantics), tz-aware UTC timestamp
  (`parse_iso_utc` round-trip), `ApiException` 500/409 both propagate
  with no retry (caller-side D12), and `load_operator_kube_config`
  falls back to kubeconfig only on `ConfigException` (non-Config
  raises straight out; both-fail propagates the second).
- **`#485`** — CI drift gate: `assert_deploy_inventory_matches` asserts
  `set(glob deploy/*)` equals the documented inventory in BOTH AGENTS.md
  (the Layout bullet) and README (the `deploy/` tree comment), both
  directions, globbed at test time — a manifest added without doc
  updates (or a doc naming a phantom) fails CI naming the file + doc.
- **`#469`** — `openstudio_operator_handler_last_tick_timestamp{module}`:
  the scheduler-heartbeat gauge. Every other runtime signal is
  event-driven (tick-failure counters increment only when a tick runs and
  fails; the duration histograms observe only when a tick executes;
  `singleton_election_total` fires only when `enforce()` runs), so if
  ticks stop being scheduled entirely — kopf registry internals shift so
  `install_singleton_guard` returns 0 and the timers are silently
  unwrapped (the documented kopf-pin failure mode), the CR is deleted, or
  the scheduling loop wedges — every series goes flat and every dashboard
  reads green while the operator does nothing. The gauge generalizes the
  #312 freshness-pair idiom to the scheduler itself: set to `time.time()`
  in a `finally` at the END of every `run_oscm_tick` invocation (the
  single shared wrapper since #473) on every terminal path — success,
  caught skip-tuple failure, idle return, even propagating exceptions.
  Labelled by `module` (the four-timer vocabulary; 4 series); the
  event-driven `dry_run_audit` watch handler is consciously excluded (no
  cadence). Staleness alert
  `time() - openstudio_operator_handler_last_tick_timestamp{module=...} > 3 * <interval>`
  shipped as `OpenStudioOperatorHandlerHeartbeatStale`
  (deploy/prometheusrule.yaml; per-module thresholds 90/180/900/180 s from
  `_constants.py`) and a "Handler heartbeat lag" panel joined
  deploy/grafana-dashboard.json (13 panels). Follow-up from #470 in the
  same manifest: the mathematically-meaningless
  `rate(openstudio_operator_prune_tick_failures_total[5m])` alert
  (per-pod-lifetime counter, unscrapeable) was rekeyed onto
  kube-state-metrics `kube_job_status_failed` as
  `OpenStudioOperatorPruneJobFailed`. Registry now 20 counters + 9 gauges
  + 3 histograms.
- **`#463`** — `spec.redisCredentials.secretRef` (`{name, key}`): Redis
  credentials move out of the CR spec into a Secret holding the FULL
  `redis://:password@queue:6379` URL. `config.py` parses it,
  `client_factory.get_read_only_redis_client` resolves it via one
  namespaced `CoreV1` Secret get (the bounded "never reads Secrets"
  exception, `get`-only grant in `deploy/rbac.yaml`) and PREFERs it over
  an inline `spec.redisUrl` when both are present; the `lru_cache` key
  grows to `(redisUrl, secretRef, namespace)`. The CRD pattern for
  `spec.redisUrl` now REJECTS embedded credentials (`@` userinfo) at
  apply time (credential-free inline URLs stay valid; stored CRs are
  grandfathered), the secretRef name is pattern-locked to
  `^openstudio-redis[a-z0-9-]*$` (#240-style fence), the resolved URL is
  fence-checked against the in-cluster pattern (#390 SSRF fence preserved
  on the Secret path), and the #116 `RedisUrlEmpty` guard stays silent
  when a secretRef is set. Resolution failures raise
  `RedisCredentialResolutionError` (subclass of `RedisClientError`) and
  are not cached — a Secret created later is picked up on the next tick.
- **`#471`** — `openstudio_operator_rest_retries_total{method}`: REST
  retry-attempt counter, incremented once per RE-attempt inside
  `OpenStudioClient._request`'s GET-only retry loop (before the jittered
  backoff sleep) — a GET that fails twice with 5xx and succeeds on
  attempt 3 records exactly 2. Separates a retry storm from a slow
  success: the #308 duration histogram observes only the terminal
  outcome (`outcome="200"` with the backoff sleeps silently inflating
  the bucket), so the operator's retry amplification during a v3.11.0
  degrade was invisible at `/metrics`. README documents the alert
  `rate(openstudio_operator_rest_retries_total[5m]) > 0` as the
  early-degrade companion to `outcome="exception"`. Registry now 20
  counters + 8 gauges + 3 histograms.
- **`#468`** — `deploy/prometheustrule.yaml` (14 alerts in 5 groups:
  per-module tick-failure rate, REST exception rate, singleton
  conflict, `metrics_server_bound == 0`, deferred-dropped rate,
  `redis_key_layout_status == 0`, stall-window accumulation, prune
  tick failures, both #312 freshness staleness expressions; exact
  `openstudio_operator_*` family names; `release: prometheus` pickup
  label documented in the manifest header) + `deploy/grafana-dashboard.json`
  (12 panels: action counters with outcome/trigger breakouts, tick-duration
  histograms, Resque queue gauges + freshness; `${DS_PROMETHEUS}`
  templated). New `tests/test_monitoring_artifacts.py` (8 tests) is the
  drift gate: every family referenced by the rules must exist in
  `tests/_metrics_inventory.py`, the dashboard must parse and reference
  the action counters, and RBAC must hold no `prometheusrules` verbs.
- **`#472`** — `analysis_datapoint_count` gains a `view` label
  (`analyses_per_tick` at the SLA observe site vs
  `started_datapoints_per_tick` at the watchdog site). The histogram
  previously merged two different units with no distinguishing label —
  p99 of the mixture answered neither "how big are our analyses" nor
  "how many datapoints are in flight", and cadence shifts reweighted
  the mixture without any workload change (#179's correlation goal
  defeated). Two pin tests assert each tick never touches the other
  series. Family name unchanged (inventory tuples untouched).
- **`#383`** — `scripts/check_stale_worktrees.sh`: stale-worktree
  pre-flight visibility check for wave orchestration. Lists every
  `issue-*` directory under the worktrees dir (default
  `../worktrees` resolved from the MAIN working tree) whose derived
  branch name (`fix/`/`feat/`/`docs/`/`chore/` + dirname) matches no
  local branch and which is not registered in `git worktree list` —
  the leftover-directories-from-prior-sessions hazard `git worktree
  prune` cannot see. Prints one `STALE worktree:` line per hit plus a
  summary; a `WARNING` line when the count exceeds `--threshold`
  (default 5) tells the orchestrator to surface a Warning event and
  confirm with the user BEFORE creating new worktrees. List mode
  always exits `0` and never deletes anything (visibility, not a CI
  gate); deletion is opt-in via `--prune-stale-worktrees --yes
  --min-age-days N` (default 30). Regression tests in
  `tests/test_stale_worktree_check.py`; runbook section in
  `docs/onboarding.md#stale-worktree-pre-flight-check-383`.
- **`#393`** — `openstudio_operator_metrics_server_bound{addr,port}`:
  metrics-server bind-outcome gauge. `start_metrics_server()` catches
  `OSError` and only logged a WARNING — the operator continued with
  `/metrics` dead and no /metrics-side signal distinguished "operator
  wedged" from "metrics endpoint never bound". The gauge records the
  FIRST bind attempt (`1.0` bound / `0.0` OSError, labelled by the
  configured bind target) and is never re-touched; it covers both authN
  modes (the plain and #401 bearer-token servers share the single
  `except OSError` branch). README documents the alert
  `metrics_server_bound == 0` as the canonical "Prometheus scrape is
  down because of US" signal (pair with blackbox `up == 0` — a dead bind
  is unscrapeable from the pod itself). Registry now 19 counters +
  8 gauges + 3 histograms.
- **`#403`** — `openstudio_operator_singleton_loser_skips_total{module,namespace,name}`:
  per-tick singleton-guard loser suppression counter, bumped inside the
  `_gated` wrapper's `if not active:` branch on every suppressed tick. The
  change-gated `singleton_election_total{outcome="conflict"}` (#239) is
  silent for a stable multi-CR namespace; this is the per-tick twin.
  Alert `rate(singleton_loser_skips_total[5m]) > 0` surfaces a sustained
  multi-CR configuration. Cardinality bounded by the one-winner-per-
  namespace invariant (D05) — one series per `(module, namespace, name)`
  tuple, same shape as `handler_tick_failures_total`.
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
  `worker_recycler`) were grandfathered via `KNOWN_LEGACY_OSCM_HANDLER_IDS`
  so the cross-check passed for them without retrofitting a `register()`
  call. (`#285` retires that whitelist — every OSCM handler now calls
  `register()` explicitly.)
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
- **`#489`** — `openstudio_operator_status_map_entries{namespace,name,
  map_name}` (softStops/requeues/startedSince/archivedAnalyses) stamped
  from map length in `StatusStore._read_status` — the fresh GET every
  RMW cycle and typed getter lands on. SREs get a capacity panel and
  the `OpenStudioOperatorStatusMapNearCap` alert (>8000 for 30m, the
  0.8 × STATUS_MAP_MAX_ENTRIES threshold) with days of runway, instead
  of learning about D04 anchor loss from duplicate-action anomalies
  after `STATUS_MAP_CAPS_TOTAL` already fired.
- **`#504`** — `openstudio_operator_build_info{version, python_version}`
  fleet-identity gauge, set once at metrics import from
  `importlib.metadata` (`PackageNotFoundError` → `"unknown"` fallback).
  One series; makes every other series interpretable against a
  release — the single-replica Recreate rolling-window scrape mix
  becomes readable from the scrape itself.
- **`#501`** — 15 tests pinning the D11 chokepoint's actual contract:
  `__call__` delegation to `emit` (identical positional shape, same
  counter labels, None return), mixed shim/emit suppression sharing
  one counter series, ten metadata-missing body shapes pinning the
  `<unknown>` fallbacks, and the kopf-1.4x MappingView behavior
  (emits fine; extraction pinned as `<unknown>` — the production
  label-degradation gap this surfaced is tracked as `#544`).
- **`#298`** — CI drift gate for `pyproject.toml` dev deps vs
  `requirements.lock`. `tests/test_dependency_drift.py` parses
  `[project.optional-dependencies].dev` and asserts every entry has a
  matching `--hash=sha256:...` line in `requirements.lock`; the failure
  message names the missing dep and prints the canonical `pip-compile`
  remediation command. The lockfile was regenerated with `--extra=dev` so
  `ruff`, `responses`, `fakeredis`, `hypothesis` (and their transitive
  deps `packaging`, `pluggy`, `pygments`, `iniconfig`, `sortedcontainers`)
  are now hash-pinned alongside the production deps.

### Changed
- **`#234`** — `handlers/__init__.py` no longer carries three
  divergent queue/drain sites: removed the `_NOTIFY_QUEUE`,
  `_REDIS_KEY_LAYOUT_QUEUE`, `_STATUS_MAP_CAP_QUEUE` module-level
  lists and the three corresponding `@kopf.on.event` drain
  handlers. The `#116` redis-URL guard, the `#163` Resque key-layout
  guard, and the `#171` status-map-cap guard now share one
  `QueuedKopfEventSink` (`src/openstudio_operator/events_sinks.py`)
  queue and one drain handler installed by `handlers/__init__.py`.
  See Added `#234` above for the new class.
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
- **`#251`** — `prune_entrypoint.py`, `analysis_sla.py`,
  `web_background_monitor.py`, `worker_recycler.py` no longer
  construct `AppsV1Api()` / `BatchV1Api()` / `CoreV1Api()` inline; the
  four sites now call `singleton.operator_*_api()` factories (the
  `CustomObjectsApi` precedent was `#158`, in `[0.2.0]` Added). The
  previously cross-imported `deployment_label_selector` from
  `analysis_sla` moves to its canonical home in
  `src/openstudio_operator/_k8s.py` alongside the new
  `DeploymentReader` Protocol (see Added `#251` above).
- **`#252`** — `retention.py` no longer reaches into the private
  `OpenStudioClient._request_json` to fetch the data-point list; the
  fetch goes through the new public
  `:func:`openstudio_operator.openstudio_client.list_datapoints``
  (see Added `#252` above). No behaviour change to the client
  itself; the reach-in removal closes the only public-side caller
  of the private method.
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
- **`#285`** — `KNOWN_LEGACY_OSCM_HANDLER_IDS` retired. The four
  pre-`#250` OSCM timers (`analysis_sla`, `datapoint_watchdog`,
  `web_background_monitor`, `worker_recycler`) now call
  `register(handler_id, fn)` at module import time, so the singleton
  guard's cross-check has no exemptions and the Python-level registry
  is the SOLE source of truth for OSCM handler ids known to the gate.
  New CI gate
  `tests/test_singleton_registry_coverage.py::test_no_oscm_handler_orphan_or_legacy_whitelist`
  pins both halves of the structural invariant (no orphan + no
  whitelist); re-introducing either fails the test loudly.
- **`#244`** — `README.md` + `AGENTS.md` Repository layout sections
  list the four new shared-utility modules (`events_sinks.py`, `_k8s.py`,
  `_oscm_handlers.py`, `logging_setup.py`).
- **`#305`** — kubeconfig loader collapsed to the SINGLE public loader
  `openstudio_operator._k8s.load_operator_kube_config()`; the
  `singleton._load_k8s_config` and `prune_entrypoint._load_kube_config`
  wrappers are now thin delegations to it. Future loader changes
  (kubeconfig Secret reference, network-proxy client, custom CA bundle)
  apply at the single site. New AST CI gate
  `tests/test_singleton_registry_coverage.py::test_only_one_kubeconfig_loader_call_site`
  rejects any inline `load_incluster_config(` / `load_kube_config(`
  call outside `_k8s.py` (catches both bare-name and
  attribute-shape calls). Tests: 529 → 531.
- **`#293`** — `ValidatingAdmissionPolicy` + `ValidatingAdmissionPolicyBinding`
  (`openstudio-operator-pod-delete-scope`) added to
  `deploy/pod-delete-admission-policy.yaml` constrains the operator
  SA's `pods/delete` verb to pods that carry `app=worker` (matches the
  worker-pool label the eviction path targets). The CEL `||` keeps
  humans-via-kubectl and the prune CronJob SA unrestricted — only the
  operator SA is narrowed. RBAC's lack of label-selector support is
  the same reason this is VAP rather than `resourceNames`. Requires
  K8s 1.30+.
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

### Fixed
- **`#391`** — operator Deployment gained `livenessProbe` + `readinessProbe`
  against the named `metrics` port (containerPort 9090). Liveness
  (initialDelay 60s, period 30s, timeout 5s, failureThreshold 3) restarts a
  wedged operator within ~90s — previously a silent stall (pod Running,
  kubelet never restarts, the four `@kopf.timer` handlers stop firing) had
  no recovery path under the single-replica + `Recreate` posture. Readiness
  (initialDelay 5s, period 10s) gates Service endpoints during boot. Two
  regression tests pin the probe shape and the 3×30≥90s restart budget.
- **`#423`** — the test-count CI guard now covers all four claim locations.
  The `#151`/`#220` steps consolidated into one
  `Verify test-count claims in all four docs` step looping over AGENTS.md,
  onboarding.md, audit-dryrun-idempotency.md, and kind-validation.md with a
  report-all FAIL accumulator (mirroring the #387 metrics gate). The two
  secondary docs had drifted three times in one day because only the first
  two were guarded. Also revives a dead missing-claim branch previously
  swallowed under `set -o pipefail`.
- **`#378`** (skill, `docs/skill-snapshot/`) — wave-state cross-repo
  isolation: new `scripts/wave-state-helpers.sh` derives a repo slug from
  the git remote, namespaces the state file
  (`wave-state.<repo-slug>.json`), writes atomically via
  temp-file-then-rename, and falls back to the legacy unprefixed path with
  a deprecation warning. Two concurrent orchestrators on different repos
  in the same `../worktrees/` directory no longer collide or corrupt each
  other's state.
- **`#380`** (skill, `docs/skill-snapshot/`) — closing-PR title convention:
  sub-agent templates now emit `fix: resolve #N — …` subjects + `Closes #N`
  bodies for closing PRs and `refs #N` + `Refs #N` for keep-open PRs, with
  the merge command always passing an explicit `--subject` override per
  CONTRIBUTING.md #88 (auto-generated squash subjects previously preserved
  the keep-open keyword on closing PRs, defeating git-log archaeology).
- **`#385`** — closed via documentation comment (stale-sweep): both
  acceptance items (scope-guard heading detection in
  `scripts/check_pr_body_scope.sh` Check 2b + `.github/PULL_REQUEST_TEMPLATE.md`)
  shipped in PR #386 / commit `f9e480f`, but the keep-open `refs` keyword
  left the issue open. No code change.
- **`#387`** — metrics-family prose claim drift. The four prose
  locations (`README.md` lines 67 and 205, the audit `Appendix D`
  introduction, `docs/kind-validation.md` lines 1474 and 1542) all
  claimed **16 counters + 4 gauges + 1 histograms** while the actual
  registry — asserted by `tests/test_metrics_endpoint.py`'s
  `EXPECTED_COUNTER_FAMILIES` / `EXPECTED_GAUGE_FAMILIES` /
  `EXPECTED_HISTOGRAM_FAMILIES` — is **18 counters + 7 gauges + 3
  histograms**. The seven families added since the 16+4+1 claim:
  - `openstudio_operator_prune_tick_failures_total` (counter, #306)
  - `openstudio_operator_warnings_deferred_dropped_total` (counter, #310)
  - `openstudio_operator_resque_queue_depth_fresh` (gauge, #312)
  - `openstudio_operator_stall_window_fresh` (gauge, #312)
  - `openstudio_operator_warnings_deferred_queue_depth` (gauge, #310)
  - `openstudio_operator_handler_tick_duration_seconds` (histogram, #308)
  - `openstudio_operator_rest_request_duration_seconds` (histogram, #308)
  Prose claims updated to **18 counters + 7 gauges + 3 histograms**; the
  README metrics table now lists every family the registry serves. New
  CI gate `tests/test_metrics_endpoint.py` mirrored to parse the
  `N counters + M gauges + K histograms` claim out of `README.md` and
  `docs/audit-dryrun-idempotency.md` and fail the build when it
  disagrees with `len(EXPECTED_*_FAMILIES)` — parallels the
  `AGENTS.md` test-count guard from #151 / #220. Companion repo-text
  fix landed in same PR as the gate; this fix is the only entry that
  documents the prose→tuple convergence.
- **`#389`** — supply-chain hardening: every `uses:` ref in
  `.github/workflows/release.yml` and `ci.yml` (17 lines across 7
  third-party actions: `actions/checkout`, `docker/setup-buildx-action`,
  `docker/login-action`, `docker/build-push-action`,
  `sigstore/cosign-installer`, `softprops/action-gh-release`,
  `actions/setup-python`) now resolves to a 40-char commit SHA with the
  resolved tag as a trailing `# vX.Y.Z` comment. A new CI lint step
  `Enforce commit-SHA action pinning (#389)` in the `lint` job rejects
  any `uses: <name>@v<N>` shape that regresses. The release workflow
  holds `packages: write` + `id-token: write` + `attestations: write`
  permissions — a compromised mutable tag would have gained push access
  to `ghcr.io/anchapin/openstudio-server-operator` with a valid
  Sigstore signature.
- **`#390`** — `spec.redisUrl` hardened with a DNS-1035-style `pattern:`
  constraint + CEL `x-kubernetes-validations` rule (mirrors the existing
  `spec.serverUrl` rule from #160). A CR-write user can no longer pivot
  the operator's Redis probe to an external host (SSRF / hostile-Redis
  exfil). The empty-default escape hatch (#116) is preserved via a
  `|^$` branch in the regex. The corrected pattern accepts the
  helm-recipe `:password@queue:6379` form; the original issue body
  pattern was internally inconsistent (it required `.svc` literally,
  which would have rejected the issue's own acceptance-criterion good
  URL). 5 new tests in `tests/test_crd_schema.py` pin the three cases
  (rejects off-cluster host, accepts in-cluster forms incl. bare
  Service + FQDN + auth, accepts empty default).

### Removed
- (none — the legacy sites retired by `#234` / `#235` / `#251` /
  `#252` are described inline within their Changed bullets above)

### Fixed
- **`#402`** — the deferred Warning-Event queue survives operator
  restarts. `QueuedKopfEventSink`'s queue was purely in-process, so a
  crash while it held entries (exactly the stalled-watch-stream mode
  `#310` designed for) silently dropped every queued Warning with no
  `queue_full` drop to observe. Every ACCEPTED deferral now mirrors
  into `status.deferredEvents` (new typed `StatusStore` accessors:
  append / get / clear, 409-safe RMW per D04, `max_entries` backstop
  at `MAX_DEFERRED_WARNING_EVENTS`), and `flush_for` drains the
  persisted list FIRST, deduplicating in-memory twins — exactly-once
  in the common path, at-least-once across restarts. Persistence is
  armed from a `@kopf.on.startup` handler (not import time), so
  test/library imports never touch a cluster. CRD `status.deferredEvents`
  (typed array) added. `WARNINGS_DEFERRED_DROPPED_TOTAL{reason="queue_full"}`
  semantics and the `WARNINGS_DEFERRED_QUEUE_DEPTH` Gauge are unchanged.

### Fixed
- **`#462`** — the committed Redis/Mongo credential Secret manifests no
  longer ship a usable password. `deploy/redis-credentials-secret.yaml` +
  `deploy/mongo-credentials-secret.yaml` carried `openstudio-rotated`, a
  publicly-known (git-committed) credential that plain `kubectl apply`
  installs as a working password — the #150/#219 leak class renamed. Both
  manifests now ship the unusable sentinel `CHANGE_ME_RUN_ROTATE_SCRIPT`;
  the CI guards (`check_redis_password_unique.sh` /
  `check_mongo_password_unique.sh`) were extended to fail the build if
  either manifest's committed password is anything other than the sentinel
  (legacy-literal rejection intact); the rotation scripts substitute both
  tokens (sentinel in deploy Secrets, legacy in kind manifests) and reject
  the sentinel as a user-chosen password. README prereqs + `install-keda.sh`
  next-steps no longer offer the plain-apply path for those Secrets.
  Regression test:
  `test_credential_secret_manifests_ship_only_sentinel_placeholder`.

### Added
- **`#466`** — tick-level error-path coverage for `datapoint_watchdog`
  (was 14 tests, zero error paths): sustained-503 through the real client
  → retry exhaustion → 4-label `HANDLER_TICK_FAILURES_TOTAL` bump + clean
  skip; single-409 on the requeue write → bounded retry resolves; sustained
  409 → `StatusStoreConflictError` (pins the re-attempt-next-poll shape);
  raw non-409 `ApiException` escapes the wrapper's 2-tuple uncounted
  (pinned; `#493` owns the tuple standardization).
- **`#467`** — tick-level error-path coverage for `worker_recycler`
  (was 20 tests, zero error paths), mirroring the #466 pattern: sustained
  503 on `/analyses.json` → counter + clean skip; 409-then-success on
  `set_last_recycle_at`; `ApiException` from the deployment patch caught
  by the 3-tuple (the inverse asymmetry vs `#466`, pinned for `#493`);
  sustained 409 after a fired restart → unanchored restart + exactly one
  re-fire on recovery (the documented benign delete-then-anchor race,
  D12). Test count 797 → 806 across the four claim locations.

### Docs
- **`#465`** — AGENTS.md `deploy/` inventory lists all 11 manifests
  (`priority-class.yaml` #414, `resource-quota.yaml` #400 were missing).
- **`#484`** — `docs/audit-policy.md` (the `#399` kube-apiserver
  audit-policy recipe) is indexed where readers look: the AGENTS.md
  key-references block and README's intro doc links; README's `docs/`
  tree comment now names all six top-level docs so it matches the
  directory.
- **`#487`** — `docs/adr/` with an index + five records in
  context/decision/consequences format (kopf pin, single-replica
  Recreate, KEDA-only, VAPs, redisUrl fence) — each stands alone
  offline, with issue numbers as pointers only. Linked from the
  AGENTS.md key-references list; README's tree comment gains `adr/`.
- **`#503`** — CONTRIBUTING.md gains the local development setup
  section: venv + editable install + ruff + venv-pytest commands
  verbatim from AGENTS.md, the docs/onboarding.md#quick-start link,
  the lockfile-vs-editable note (#173), and the one-line venv-drift
  warning (#71) at the conventional human entry point.
- **`#486`** — the wave-orchestration surface is documented, not
  mysterious: the two skill-snapshot cruft files
  (`wave-planner.js.bak`, `_injection-test.js`) are deleted (zero
  references); AGENTS.md's scripts/ bullet names the four orchestration
  tools as the deliberate load-bearing exception to the
  validation-toolkit framing; a docs bullet covers the
  frozen-legacy + wave-numbered snapshot lineage (#379) with the
  `~/.config/opencode/` canonical pointer; README's layout tree
  mentions both.

### Fixed
- **`#470`** — the storage-prune CronJob no longer masks failures behind
  exit 0. Exit-code table (documented in `prune_entrypoint.py`'s
  docstring): `0` ok · `3` empty `spec.redisUrl` (#392) · `4` CR-list
  failure · `5` D12 runtime-failure tuple. Failed exits mark the Job
  FAILED so `failedJobsHistoryLimit` (3) + Job monitoring alert — the
  loud transport for a sustained retention-pipeline failure, since
  `prune_tick_failures_total` is a per-pod-lifetime counter that a
  30–60 s scrape effectively never samples (`rate()` is mathematically
  meaningless). README triage documents the `kubectl get jobs` check;
  `OpenStudioOperatorPruneJobFailed` (see `#469`'s entry) keys on
  `kube_job_status_failed` instead of the stale rate expression.
- **`#475`** — `OperatorConfigError` moves to `config.py` as a direct
  `Exception` subclass (canonical home; `redis_client` keeps an
  identity-verified compat re-export, the `#305` pattern). The REST
  client no longer imports any symbol from `redis_client` — a TLS
  misconfiguration raises an error whose name no longer says Redis.
  The hazard was catch-tuple conflation: `except RedisClientError`
  blocks accidentally swallowed REST TLS-config failures while other
  handlers treated the identical failure as a crash.
  `SKIP_TICK_EXCEPTIONS` carries `OperatorConfigError` explicitly — the
  pre-refactor wrappers caught it at runtime via Redis parentage, so
  explicit membership restores the exact historical runtime set (D12:
  wiring/config failure → counter-bumped skip + retry next poll, where
  a fixed Secret, CR spec, or re-mounted CA bundle is picked up live).
- **`#477`** — the storage egress NetworkPolicy's `0.0.0.0/0` except
  list now also excludes `169.254.0.0/16` (link-local — hosts the
  AWS/GCP/Azure instance-metadata service; archival pods hold live
  object-store credentials via envFrom, so this closes the
  node-credential pivot AWS's own guidance recommends blocking) and
  `100.64.0.0/10` (CGNAT), alongside the existing RFC1918 entries.
  Dual-stack caveat documented in-manifest (`0.0.0.0/0` ipBlock does
  not constrain IPv6 egress on dual-stack CNIs). Regression test
  asserts all five CIDRs.
- **`#493`** — wiring failures get the D12 skip-tick treatment:
  `run_oscm_tick` now invokes `custom_objects_api()`/`StatusStore`/
  `EventEmitter`/`wire()` INSIDE the guarded region, and
  `SKIP_TICK_EXCEPTIONS` gains the construction-failure pair —
  `kubernetes.config.ConfigException` +
  `urllib3.exceptions.LocationValueError` (the issue's hinted
  `kube_config` import path is dead in modern kubernetes; the urllib3
  home is stable 1.26→2.x). A bad kubeconfig or bare URL now bumps
  `HANDLER_TICK_FAILURES_TOTAL`, logs the single skip line, and
  returns cleanly — while the `#469` heartbeat still stamps via the
  finally (wiring-failing-but-scheduled reads ALIVE + climbing, the
  signature SREs need). Non-tuple wiring errors (e.g. `RuntimeError`)
  still propagate fail-closed.

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