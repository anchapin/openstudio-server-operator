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
    "openstudio_operator_datapoints_requeued_total",
    "openstudio_operator_datapoints_requeue_exhausted_total",
    "openstudio_operator_workers_recycled_total",
    "openstudio_operator_worker_pods_evicted_total",
    "openstudio_operator_web_background_restarts_total",
    "openstudio_operator_analyses_archived_total",
    "openstudio_operator_analyses_deleted_total",
    # Issue #119 — status-store 409 retry observability surface.
    "openstudio_operator_status_conflicts_total",
    "openstudio_operator_status_conflict_retries_exhausted_total",
    # Issue #117 — handler tick failures (per module + error_type).
    "openstudio_operator_handler_tick_failures_total",
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
    # reason). The two skip-tick sites in prune_entrypoint.main() bump
    # this counter (cr_list_failure, runtime_failure); the CronJob pod
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
EXPECTED_GAUGE_FAMILIES = (
    "openstudio_operator_resque_workers_seen_max",
    "openstudio_operator_resque_queue_depth",
    "openstudio_operator_redis_key_layout_status",
    "openstudio_operator_stall_window_elapsed_seconds",
    "openstudio_operator_resque_queue_depth_fresh",
    "openstudio_operator_stall_window_fresh",
    "openstudio_operator_warnings_deferred_queue_depth",
)

#: Issue #179 — per-CR datapoint-budget Histogram. The SLA monitor and the
#: datapoint watchdog each observe the count off the OpenStudio REST analysis
#: payload (or the equivalent summary endpoint) once per tick: the SLA records
#: ``len(analyses)`` from ``/analyses.json``; the watchdog records
#: ``len(started_ids)`` from ``/data_points/status?status=1&jobs=
#: started``. No labels — one observation per tick, bounded-cardinality
#: at the histogram level rather than per analysis.
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
EXPECTED_HISTOGRAM_FAMILIES = (
    "openstudio_operator_analysis_datapoint_count",
    "openstudio_operator_handler_tick_duration_seconds",
    "openstudio_operator_rest_request_duration_seconds",
)
