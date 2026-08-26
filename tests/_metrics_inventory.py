"""Canonical EXPECTED_*_FAMILIES inventory (issue #406).

Single shared source of truth for the three metrics-family tuples the
drift-invariant tests compare against: ``test_metrics_endpoint.py``
(module walk) and ``test_walk_metrics_registry.py`` (REGISTRY round-trip
walk) both import from here. Issue #287 kept a deliberately-duplicated
local mirror in the registry-walk file to avoid an ``import requests``
side effect — but the tuples' previous home only touched ``requests``
inside function bodies, so a side-effect-free shared module (this file)
is strictly better: one tuple to update, one failure message.

This module MUST stay import-cheap: no ``requests``, no ``subprocess``,
no metrics-server startup — plain string tuples only. The leading
underscore keeps pytest from collecting it as a test file, so the
``ls tests/test_*.py | wc -l`` file-count claim in AGENTS.md is
unaffected.
"""

EXPECTED_COUNTER_FAMILIES = (
    "openstudio_operator_soft_stops_total",
    "openstudio_operator_stops_total",  # issue #707
    "openstudio_operator_datapoints_requeued_total",
    "openstudio_operator_datapoints_requeue_exhausted_total",
    "openstudio_operator_workers_recycled_total",
    "openstudio_operator_worker_pods_evicted_total",
    "openstudio_operator_web_background_restarts_total",
    "openstudio_operator_analyses_archived_total",
    "openstudio_operator_archival_jobs_failed_total",  # issue #782
    "openstudio_operator_analyses_deleted_total",
    # Issue #119 — status-store 409 retry observability surface.
    "openstudio_operator_status_conflicts_total",
    "openstudio_operator_status_conflict_retries_exhausted_total",
    # Issue #117 — handler tick failures (per module + error_type).
    "openstudio_operator_handler_tick_failures_total",
    # Issue #783 — consecutive-failure streak crossing counter.
    "openstudio_operator_handler_consecutive_failure_streak_total",
    # Issue #171 — defensive cap evictions on the CR .status maps.
    "openstudio_operator_status_map_caps_total",
    # Issue #237 — EventEmitter (#164) dry-run gate Prometheus surface
    # (per-reason labelled). Mirrors the warning-event reason vocabulary
    # so dashboards can tell WHICH handler path the dry-run gate
    # intercepted. Emitted companion below lets the dry-run ratio be
    # derived via Prometheus arithmetic without log scraping.
    "openstudio_operator_events_dry_run_suppressed_total",
    "openstudio_operator_events_emitted_total",
    # Issue #239 — singleton-guard election outcomes (per outcome).
    "openstudio_operator_singleton_election_total",
    # Issue #403 — per-tick singleton-guard loser suppressions (per
    # (module, namespace, name) tuple). The change-gated election
    # counter above is silent for a stable multi-CR namespace; this one
    # fires on EVERY suppressed loser tick so a sustained multi-CR
    # configuration is visible as rate(...) > 0. Cardinality is bounded
    # by the one-winner-per-namespace invariant (D05).
    "openstudio_operator_singleton_loser_skips_total",
    # Issue #255 — kopf.event emission failures (per reason).
    "openstudio_operator_events_emit_failures_total",
    # Issue #306 — storage-prune CronJob skip-tick failures (per branch
    # reason). The three bump sites in prune_entrypoint.main() fire this
    # counter (cr_list_failure, runtime_failure, and the #392 exit-3
    # redis_url_empty guard); the CronJob pod
    # exposes the same /metrics endpoint on port 9090 as the operator,
    # gated by the parallel ``openstudio-storage-pruner-metrics-ingress``
    # NetworkPolicy. Mirrors the bounded-cardinality convention #117
    # established for the handler timer wrappers.
    "openstudio_operator_prune_tick_failures_total",
    # Issue #310 — QueuedKopfEventSink backpressure drop counter. The
    # sink rejects defer_to_next_tick calls once the queue hits
    # MAX_DEFERRED_WARNING_EVENTS (1000); the Counter increments on each
    # rejection with reason="queue_full" so the loss is observable on
    # /metrics (no longer silent). Initial vocabulary is one reason; the
    # label leaves room for a future per-reason-cap branch without a
    # Counter rename.
    "openstudio_operator_warnings_deferred_dropped_total",
    # Issue #471 — REST retry-attempt counter. Incremented once per
    # RE-attempt inside OpenStudioClient._request's GET-only retry loop
    # (before the backoff sleep), so a retry storm is separable from a
    # slow success — the dimension the #308 duration histogram (which
    # observes only the terminal outcome) structurally cannot carry.
    # Labelled by ``method`` only (no CR-identity labels — the client
    # is CR-agnostic, matching the sibling duration histogram).
    "openstudio_operator_rest_retries_total",
    # Issue #649 — ineffective-restart circuit-breaker counter: increments
    # each time a web_background restart is PROVEN ineffective (a later
    # restart fired while the predecessor anchor existed — the stall
    # re-sustained a full window past its cooldown). From the 3rd
    # consecutive one the operator emits WebBackgroundRestartIneffective
    # and backs the restart action off (total wait 4/6/8 stall windows —
    # the natural cooldown + re-sustain cadence is already 2). Alert on
    # increase(...[30m]) > 0 (shipped as
    # OpenStudioOperatorWebBackgroundRestartIneffective).
    "openstudio_operator_web_background_restarts_ineffective_total",
)

#: Issue #44 — Resque key-layout leg-2 non-vacuity safeguard. Since #87
#: the gauge is emitted every poll regardless of queue depth, so
#: `resque_workers_seen_max == 0` with a reachable Redis means no workers
#: are registered; under load that signature additionally marks a
#: centralized-constants / live v3.11.0 layout mismatch (the original #44
#: alert combined it with `AND queue depth > 0`).
#:
#: Issue #238 — ``resque_queue_depth{queue="..."}`` exposes the operator's
#: authoritative LLEN reads on every sensing tick (the same value KEDA's
#: external metrics API exposes — the operator's view is the cross-check
#: that surfaces a centralized-constants drift).
#:
#: Issue #253 — ``redis_key_layout_status`` Gauge (1.0=ok, 0.0=any-other)
#: surfaces the boot-time validator outcome as a cluster-wide latest-
#: observation signal so an SRE can alert on `== 0` without log scraping.
#:
#: Issue #490 — ``redis_key_layout_status_fresh`` is the freshness pair
#: for that gauge: set to ``time.time()`` in lockstep with the status
#: value at EVERY ``_check_redis_key_layout_for_cr`` invocation (all
#: terminal paths — fresh means recently validated; the status gauge
#: carries the result), and kept cadenced by the periodic revalidation
#: riding the web_background stall tick
#: (``REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL``, 5 min). Alert on
#: ``time() - redis_key_layout_status_fresh > 600`` (2× the interval).
#:
#: Issue #254 — ``stall_window_elapsed_seconds`` Gauge tracks the
#: ``StallWindowTracker`` state between the first sustained observation
#: and the eventual ``web_background_restarts_total`` increment — a heads-
#: up display that gives SREs time to react before the gate trips.
#:
#: Issue #312 — paired freshness timestamp gauges for ``resque_queue_
#: depth`` and ``stall_window_elapsed_seconds``. Set to ``time.time()``
#: on every successful read/update so dashboards can compute staleness
#: (``time() - fresh``) and alert on a sustained gap. The data gauges
#: advance on success but are NOT touched on the exception path (Redis
#: unreachable, ApiException from ``_stall_condition_holds``, etc.) —
#: without the freshness pair a prior tick's value masquerades as a
#: live reading while the operator has in fact lost visibility.
#:
#: Issue #310 — ``warnings_deferred_queue_depth`` Gauge surfaces the
#: in-process QueuedKopfEventSink queue depth on every defer / flush
#: call. Unlabelled (the queue is process-wide, not per-CR), so
#: cardinality stays bounded regardless of CR count. Sustained nonzero
#: values mean the apiserver watch stream is stalled and Warning
#: Events are piling up — a companion to the
#: ``warnings_deferred_dropped_total`` Counter which fires when the
#: cap (MAX_DEFERRED_WARNING_EVENTS = 1000) is exceeded.
#:
#: Issue #393 — ``metrics_server_bound`` Gauge records the /metrics
#: server's FIRST bind attempt outcome (1.0 bound / 0.0 OSError),
#: labelled by the configured ``(addr, port)``. ``== 0`` is the
#: canonical "Prometheus scrape is down because of US" signal.
#:
#: Issue #469 — ``handler_last_tick_timestamp{module}`` Gauge is the
#: scheduler heartbeat: Unix-epoch seconds of the most recent completed
#: ``run_oscm_tick`` invocation, stamped in a ``finally`` on EVERY
#: terminal path (success, idle return, caught skip-tuple failure,
#: propagation). Generalizes the #312 freshness-pair idiom to the
#: scheduler itself — every other registry signal is event-driven and
#: reads green while ticks are silently unscheduled. The event-driven
#: ``dry_run_audit`` watch handler is consciously excluded (no cadence).
#: Alert: ``time() - handler_last_tick_timestamp{module=...} > 3 *
#: <interval>`` (per-module intervals in ``_constants.py``).
#:
#: Issue #492 — the four config-state posture gauges, each labelled by
#: ``namespace`` + ``name`` (CR identity, the #311 convention; bounded
#: by the singleton guard's one-CR-per-namespace invariant, D05):
#: ``dry_run_active`` (1/0 — the D11 gate posture; flipped immediately
#: on spec.dryRun transitions by ``dry_run_audit`` in addition to the
#: per-tick stamp), ``server_url_set`` / ``redis_url_set`` (1/0 —
#: whether the CR carried the respective URL source at config-parse
#: time; redis counts the #463 secretRef as a source), and
#: ``auto_soft_stop_enabled`` (1/0 — whether the SLA monitor is armed).
#: Turn operator policy posture into a scrapeable fact: a quiet dry-run
#: operator was previously indistinguishable from a quiet live one.
#: Alert: ``dry_run_active == 1`` sustained beyond a migration window.
#:
#: Issue #489 — ``status_map_entries{namespace,name,map_name}`` Gauge is
#: the LEAD-TIME companion to the #171 cap counter: set to ``len(map)``
#: inside ``status_store.StatusStore._read_status`` (the single read site
#: every RMW cycle and typed getter lands on), so all four .status maps
#: are stamped on every read. The cap counter + ``StatusMapCapped``
#: Warning Event fire only after anchors are already being dropped;
#: the gauge gives an SRE a capacity panel and a ``> 8000`` (0.8 ×
#: STATUS_MAP_MAX_ENTRIES) alert with days of runway. Cardinality is
#: bounded by the same invariant as the cap counter (one series per
#: CR-map pair; singleton guard bounds CRs, D05).
#:
#: Issue #504 — ``build_info{version, python_version}`` Gauge is the
#: fleet-identity row: constant ``1`` set once at metrics import time
#: from the installed distribution metadata (``unknown`` fallback when
#: the distribution is absent). During an upgrade the operator is a
#: single-replica Recreate Deployment, so a rolling-window scrape after
#: redeploy mixes old/new pod series with identical labels — this series
#: makes the emitting release a scrapeable fact. One series, fixed
#: cardinality.
#:
#: Issue #491 — ``singleton_wrapped_handlers`` Gauge is the RUNTIME half
#: of the kopf-pin fence: the count of OSCM spawning handlers whose fn
#: carries the singleton-gate marker after ``install_singleton_guard``
#: ran at boot. ``0`` on a booted operator that expects timers is the
#: silent-unwrap failure mode (a kopf upgrade moved the private
#: ``registry._spawning._handlers`` layout; D05 enforcement silently
#: disabled while every timer still fires ungated) — the CI-time fence
#: is ``tests/test_singleton_registry_coverage.py``, this gauge is what
#: Prometheus can alert on. Complementary to the #469 heartbeat: the
#: heartbeat proves scheduling, the wrap count proves guarding.
#: Unlabelled; one series per process; set once at boot (D11-exempt —
#: before any dry-run-gated action could exist).
EXPECTED_GAUGE_FAMILIES = (
    "openstudio_operator_resque_workers_seen_max",
    "openstudio_operator_resque_queue_depth",
    "openstudio_operator_redis_key_layout_status",
    # Issue #490 — freshness pair for redis_key_layout_status: stamped in
    # lockstep at every _check_redis_key_layout_for_cr run; cadenced by the
    # 5-minute revalidation riding the web_background stall tick.
    "openstudio_operator_redis_key_layout_status_fresh",
    "openstudio_operator_stall_window_elapsed_seconds",
    "openstudio_operator_resque_queue_depth_fresh",
    "openstudio_operator_stall_window_fresh",
    "openstudio_operator_warnings_deferred_queue_depth",
    "openstudio_operator_metrics_server_bound",
    "openstudio_operator_handler_last_tick_timestamp",
    # Issue #492 — config-state posture gauges (per-CR labelled).
    "openstudio_operator_dry_run_active",
    "openstudio_operator_server_url_set",
    "openstudio_operator_redis_url_set",
    "openstudio_operator_auto_soft_stop_enabled",
    # Issue #489 — per-map CR .status size gauge (lead time before the
    # 10k cap evicts D04 anchors).
    "openstudio_operator_status_map_entries",
    # Issue #504 — build/version identity gauge (the fleet-identity
    # row; constant 1 set at metrics import time from the installed
    # distribution metadata, ``unknown`` fallback when absent).
    "openstudio_operator_build_info",
    # Issue #491 — boot-time singleton-guard wrap count (0 on a booted
    # operator with timers expected = the silent-unwrap failure mode).
    "openstudio_operator_singleton_wrapped_handlers",
    # Issue #570 — boot-time singleton-guard EXPECTED count, the
    # denominator of the #491 wrap gauge. Sized at install time from the
    # OSCM spawning-handler population kopf actually reports (the same
    # scan the coverage test performs; the Python-level registry
    # population stands in on the internals-mismatch branch). A PARTIAL
    # unwrap — one handler skipped for missing #250 registration or a
    # dataclasses.replace TypeError — reads wrapped < expected while the
    # historical == 0 alert stayed silent; the strict-< alert
    # (rekeyed OpenStudioOperatorSingletonGuardUnwrapped) subsumes the
    # old == 0 clause.
    "openstudio_operator_singleton_expected_handlers",
)

#: Issue #179 — per-tick count Histogram. The SLA monitor and the
#: datapoint watchdog each observe a count off the OpenStudio REST payload
#: once per tick: the SLA records ``len(analyses)`` from
#: ``/analyses.json``; the watchdog records ``len(started_ids)`` from
#: ``/data_points/status?status=1&jobs=started``. Issue #472 labels the
#: family by ``view`` (``analyses_per_tick`` at the SLA site,
#: ``started_datapoints_per_tick`` at the watchdog site) because the two
#: populations have different units — the pre-#472 unlabelled merge made
#: the percentiles meaningless. Two series total; bounded-cardinality at
#: the histogram level rather than per analysis.
#:
#: Issue #308 — handler tick-duration Histogram. The four ``@kopf.timer``
#: wrappers (analysis_sla, datapoint_watchdog, worker_recycler,
#: web_background_monitor) observe their wall-clock duration regardless of
#: success or caught-exception outcome, so a sustained degradation
#: (REST 5xx storm, GC pause, kopf bus contention, NFS stall) is visible
#: to Prometheus before it crosses the failure threshold captured by
#: ``handler_tick_failures_total``. ``module`` label vocabulary matches the
#: failure counter.
#:
#: Issue #308 — REST round-trip duration Histogram. ``OpenStudioClient
#: ._request`` observes its wall-clock duration including the GET-only 3x
#: retry envelope, labelled by ``method`` and ``outcome`` (``"200"`` |
#: ``"exception"``). A sustained non-zero rate on ``outcome="exception"``
#: is the canonical REST-degraded alert.
#:
#: Issue #488 — Redis request-duration Histogram. Every
#: ``ReadOnlyRedisClient`` read method (queue_depths LLENs,
#: worker_heartbeats SMEMBERS + the stale_workers delegation,
#: validate_key_layout EXISTS probes + diagnostic scans (#688), the
#: #83 D2 workers_for_analysis read) observes its wall-clock duration on
#: BOTH success and failure, labelled by ``operation``
#: (llen | smembers | scan | exists — exists is the #688 layout-verdict
#: probe; multi-command methods timed once under their dominant
#: label). Companion to the REST histogram above: attributes an inflated
#: tick-duration bucket to Redis vs REST vs kube.
#:
#: Issue #488 — Kubernetes API request-duration Histogram. The kube
#: client chokepoints (status_store RMW get/patch, the rolling-restart
#: Deployment patch, pod list/delete in the SLA escalation, the
#: Deployment reads behind deployment_label_selector) observe their
#: wall-clock duration on BOTH success and failure, labelled by ``verb``
#: (get | patch | delete | list). A slow-but-SUCCESSFUL apiserver is the
#: blind spot the #119 409 counters leave open. Same #308 bucket set.
EXPECTED_HISTOGRAM_FAMILIES = (
    "openstudio_operator_analysis_datapoint_count",
    "openstudio_operator_handler_tick_duration_seconds",
    "openstudio_operator_rest_request_duration_seconds",
    # Issue #488 — per-dependency latency attribution histograms.
    "openstudio_operator_redis_request_duration_seconds",
    "openstudio_operator_kube_api_request_duration_seconds",
)
