"""Prometheus metrics for the operator (plan Phase 4, issue #17).

Counters are module-level singletons on prometheus_client's default REGISTRY.
The /metrics endpoint is served by ``start_metrics_server()`` via
``prometheus_client.start_http_server`` — a plain WSGI server on a daemon
thread. Mechanism choice (D10): no web framework, no extra dependencies, and
the thread dies with the process. It is started at operator startup from the
handlers package import (``kopf run --module openstudio_operator.handlers``
imports that package exactly once), which is the operator's de-facto
entrypoint — see ``openstudio_operator/handlers/__init__.py``.

Port choice (issue #165): the conventional Prometheus port lives in
:mod:`openstudio_operator._constants` as :data:`METRICS_PORT` — the single
source of truth shared with ``deploy/operator-deployment.yaml`` ``containerPort``
and the ``openstudio-operator-metrics-ingress`` NetworkPolicy (issue #166).
"""

import logging
import threading

import prometheus_client
from prometheus_client import Counter, Gauge, Histogram

from openstudio_operator._constants import METRICS_PORT

# Default bucket set for handler tick-duration Histograms (issue #308).
# Spread matches the operator's poll cadence: 50 ms–30 s covers both fast
# idle ticks (~0.1 s) and degraded stalls (REST 5xx storms, GC pauses, kopf
# bus contention — D12 says handlers "skip the tick and retry naturally on
# the next poll"; without this histogram a slow tick that masks the retry
# window is invisible). A new SRE-facing latency-budget SLO lives or dies
# on this set: too tight and the right tail gets clipped, too loose and
# the dashboard loses resolution on the healthy-band the operator cares
# about. See the issue body for the canonical choice.
_HANDLER_TICK_BUCKETS = (0.05, 0.1, 0.5, 1, 2, 5, 10, 30)

# Default bucket set for the REST request-duration Histogram (issue #308).
# Tighter than the handler set because the v3.11.0 REST endpoints are
# fast (< 1 s on a healthy cluster; #242 timing data); a 5 s cap keeps
# resolution at the healthy band where a degrade is detectable.
_REST_REQUEST_BUCKETS = (0.05, 0.1, 0.5, 1, 2, 5)

logger = logging.getLogger(__name__)

SOFT_STOPS_TOTAL = Counter(
    "openstudio_operator_soft_stops_total",
    "Analyses soft-stopped by the SLA monitor. Labelled by ``outcome`` "
    "(issue #309) so the dashboard can distinguish real soft-stops "
    "(``issued``) from dry-run-marked suppressions (``dry-run``). "
    "Incremented at the soft-stop branch in ``run_sla_tick``.",
    labelnames=["outcome"],
)

DATAPOINTS_REQUEUED_TOTAL = Counter(
    "openstudio_operator_datapoints_requeued_total",
    "Zombie datapoints automatically requeued",
)

DATAPOINTS_REQUEUE_EXHAUSTED_TOTAL = Counter(
    "openstudio_operator_datapoints_requeue_exhausted_total",
    "Zombie datapoints abandoned after exceeding maxRequeues "
    "(incremented by the datapoint watchdog, #10)",
)

WORKERS_RECYCLED_TOTAL = Counter(
    "openstudio_operator_workers_recycled_total",
    "Worker deployment recycles performed. Labelled by ``trigger`` "
    "(issue #309) so an SRE investigating a recycle spike can tell "
    "whether the interval-elapsed sweep fired (``interval-elapsed``) "
    "or an analysis-completed edge fired (``analysis-completed``). "
    "Incremented at the recycle site in ``run_recycler_tick``; the "
    "trigger string is the value returned by ``_armed_trigger``.",
    labelnames=["trigger"],
)

WORKER_PODS_EVICTED_TOTAL = Counter(
    "openstudio_operator_worker_pods_evicted_total",
    "Worker pods surgically evicted by the analysis-SLA escalation "
    "(incremented by the SLA monitor, #9; counts decisions — dry-run ticks "
    "increment too, matching SOFT_STOPS_TOTAL). Labelled by ``outcome`` "
    "(issue #309) so the dashboard can distinguish escalation outcomes "
    "(``evicted`` | ``evicted-partial`` | ``no-matching-pods`` | ``dry-run``) "
    "without log scraping. The outcome is decided once per analysis at the "
    "end of the per-pod loop in ``_escalate_analysis`` and applied to the "
    "evicted count via ``inc(evicted_count)`` — a per-pod label would race "
    "with the post-loop ``failed_count`` aggregation.",
    labelnames=["outcome"],
)

WEB_BACKGROUND_RESTARTS_TOTAL = Counter(
    "openstudio_operator_web_background_restarts_total",
    "web_background deployment restarts issued after sustained queue stalls "
    "(incremented by the web_background monitor, #13)",
)

ANALYSES_ARCHIVED_TOTAL = Counter(
    "openstudio_operator_analyses_archived_total",
    "Analyses whose archival Job passed rclone verification "
    "(incremented by the storage pruner, #16; counts observed Job completions, "
    "adopted completions included)",
)

ANALYSES_DELETED_TOTAL = Counter(
    "openstudio_operator_analyses_deleted_total",
    "Analyses deleted by the retention pipeline after verified archival "
    "(incremented by the storage pruner, #16; spec.dryRun suppresses both the "
    "delete and the increment). Labelled by ``outcome`` (issue #309) — the "
    "current code path only exercises ``deleted`` (the only branch that "
    "performs the REST cascade), but the label is pinned so a future "
    "expansion (e.g. partial-failure splits) is a one-line change.",
    labelnames=["outcome"],
)

# Issue #238 — per-queue Resque depth Gauge. The operator reads
# ``LLEN resque:queue:simulations`` and ``LLEN resque:queue:requeued`` on every
# web_background_monitor tick (cheapest leg-A signal in
# ``_stall_condition_holds``) but never exposed the readings as a metric —
# the only view was KEDA's external metrics API, and the operator's own
# readings are the authoritative cross-check for a centralized-constants /
# live v3.11.0 layout drift (#44/#66/#67): if the operator's depth reads
# disagree with KEDA's, the Resque key constants are wrong. Labelled by
# ``queue`` so a Grafana panel can render both series on the same axis;
# cardinality is bounded (one series per managed queue — 2 total). Set in
# ``web_background_monitor._stall_condition_holds`` immediately after the
# ``queue_depths()`` read so the gauge advances on every tick, even the
# ones that bail before the stall-condition evaluation runs (cheap path).
RESQUE_QUEUE_DEPTH = Gauge(
    "openstudio_operator_resque_queue_depth",
    "Resque queue depth observed by web_background_monitor (LLEN "
    "``resque:queue:simulations`` / ``resque:queue:requeued``) on every "
    "sensing tick (issue #238). Labelled by queue name; cardinality is "
    "bounded to the two managed queues — surfaces the operator's "
    "authoritative reading as a cross-check against KEDA's external "
    "metrics view and a debuggable signal when the centralized Resque key "
    "constants drift from the live v3.11.0 layout.",
    labelnames=["queue"],
)

# Issue #312 — ``RESQUE_QUEUE_DEPTH`` (above) advances only on a successful
# Redis LLEN; on the exception path (Redis unreachable, ApiException, etc.)
# the gauge holds its prior tick's value and masquerades as a live reading
# while the operator has in fact lost visibility. A paired freshness
# timestamp gauge set to ``time.time()`` immediately after each successful
# read lets dashboards compute staleness (``time() - fresh``) and alert
# on the gap — the same idiom as the rest of the Prometheus ecosystem
# (``_created`` series). Set at the same site in
# ``web_background_monitor._stall_condition_holds`` that advances
# :data:`RESQUE_QUEUE_DEPTH`, BEFORE any leg evaluation runs, so the
# freshness stamp is recorded on every sensing tick — matching the
# unconditional-advance pattern of issue #87. Unlabelled: the freshness is
# process-wide (the operator reads Resque from one place); bounded
# cardinality (one series). Same docstring pointer convention as
# :data:`STALL_WINDOW_FRESH` below.
RESQUE_QUEUE_DEPTH_FRESH = Gauge(
    "openstudio_operator_resque_queue_depth_fresh",
    "Unix-epoch seconds of the most recent successful "
    "``RESQUE_QUEUE_DEPTH`` read (issue #312). Set to ``time.time()`` "
    "after every successful ``ReadOnlyRedisClient.queue_depths()`` call "
    "in ``web_background_monitor._stall_condition_holds``. The "
    "``RESQUE_QUEUE_DEPTH`` Gauge advances on success but is NOT "
    "touched on the exception path (Redis unreachable, ApiException, "
    "etc.) — so a stale value can masquerade as a live reading while the "
    "operator has in fact lost visibility. Dashboards should compute "
    "staleness as ``time() - openstudio_operator_resque_queue_depth_"
    "fresh`` and alert on a sustained gap, the same idiom as the rest of "
    "the Prometheus ecosystem's ``_created`` series. Set on EVERY "
    "sensing tick that successfully reads Redis (the unconditional-"
    "advance pattern from issue #87).",
)

# Issue #239 — singleton-guard election outcomes. The guard (D05, issue #14)
# emits Warning ``SingletonConflict`` on losers and Normal
# ``SingletonActive`` on the winner when > 1 CRs exist, plus a single info
# log on transition to zero CRs ("operator idle"). When the guard is
# silently bypassed — the kopf registry internals change shape so
# ``install_singleton_guard`` returns 0 and the AST coverage test does not
# fail — the operator ticks against multiple CRs without any
# ``SingletonConflict`` Warning Events: the corruption is silent on the
# dashboard. A Counter on the three enforcement outcomes (idle | active |
# conflict) lets an SRE alert on ``rate(openstudio_operator_singleton_
# election_total{outcome="conflict"}[5m]) > 0`` and notice the silent
# bypass even if no Warning Event fires. Incremented at the three
# post-decode branches in ``SingletonGuard.enforce`` (issue #239
# explicitly allows ``the equivalent post-decode site`` as an alternative
# to ``singleton_guard._check``); only fires on STATE CHANGES (matches
# the existing change-gated log/Event noise channel — steady state is
# silent both for Events and for this counter).
SINGLETON_ELECTION_TOTAL = Counter(
    "openstudio_operator_singleton_election_total",
    "Singleton-guard election outcomes (issue #239). Incremented at the "
    "three branches in SingletonGuard.enforce: outcome=idle when no "
    "OSCM CRs exist in the namespace; outcome=active when exactly one "
    "CR exists and is served; outcome=conflict when >1 CRs exist and the "
    "oldest is served (the others get ``SingletonConflict`` Warning "
    "Events). Only fires on state changes (mirrors the existing log/Event "
    "noise gate) so steady state is silent. Alert on sustained nonzero "
    "rate on outcome=conflict — a multi-CR namespace is a singleton-guard "
    "violation (D05).",
    labelnames=["outcome"],
)

# Issue #253 — Redis key-layout validation status as a Gauge. The boot-time
# validator (#163) returns one of ``ok | degraded | unreachable | error |
# skipped`` and emits a structured log line per CR; the Warning Event on the
# ``degraded`` branch is the only third-party signal today. A Gauge lets the
# on-call tell ``validator has not run yet`` from ``validator found a
# layout drift`` from a /metrics scrape: the gauge reads ``1.0`` while the
# most recent validation succeeded, ``0.0`` for every other terminal status
# (degraded | unreachable | error | skipped). The
# ``resque_workers_seen_max`` gauge is the related diagnostic but only
# asserts worker presence in a candidate key prefix; this gauge asserts the
# validator itself returned ok. Set at every call to
# ``handlers/_check_redis_key_layout_for_cr`` so the metric reflects the
# latest operator boot observation per CR — without labels to keep
# cardinality bounded (one series for the cluster-wide validator state,
# not per-CR).
REDIS_KEY_LAYOUT_STATUS = Gauge(
    "openstudio_operator_redis_key_layout_status",
    "Redis key-layout validation status (issue #253). 1.0 when the most "
    "recent ``validate_key_layout()`` call returned ``ok``; 0.0 for every "
    "other terminal status (``degraded`` | ``unreachable`` | ``error`` | "
    "``skipped``). Surfaces the post-#44 failure mode — a v3.11.0 layout "
    "drift takes ``resque_workers_seen_max`` silent and the stall "
    "condition fires vacuously — as a Prometheus signal so an SRE can "
    "alert on ``openstudio_operator_redis_key_layout_status == 0`` "
    "instead of correlating logs. Set in "
    "``handlers/_check_redis_key_layout_for_cr`` for every CR on every "
    "OSCM watch event (cluster-wide latest observation, not per-CR — "
    "the validator outcome is process-wide).",
)

# Issue #254 — sustained-window elapsed seconds for the web_background
# stall. ``StallWindowTracker`` records the first tick the stall condition
# was observed and counts elapsed seconds against
# ``stallWindowMinutes``. The action is gated on reaching the window, but
# the only metric that fires today is
# ``web_background_restarts_total`` AFTER the window expires — an on-call
# has no early-warning signal that the stall is accumulating. Three
# Redis/K8s-leg ticks could be quietly accumulating toward a restart with
# nothing on the dashboard between them. A Gauge of elapsed seconds (0
# when the condition breaks, ``(now - first_observed).total_seconds()``
# when it holds) gives SREs a heads-up display: rate > 0 means the window
# is accumulating, exact value shows how close to action. Set in
# ``run_stall_tick`` immediately after ``tracker.observe()`` — the same
# site that already issues the action, so the gauge tracks the tracker
# state exactly.
STALL_WINDOW_ELAPSED_SECONDS = Gauge(
    "openstudio_operator_stall_window_elapsed_seconds",
    "Sustained-window elapsed seconds for the web_background stall "
    "(issue #254). Set in ``run_stall_tick`` after ``tracker.observe()`` "
    "to the elapsed seconds when the stall condition held this tick, or "
    "0 when it broke (the tracker resets). Rate > 0 means the window is "
    "accumulating toward a ``web_background_restarts_total`` increment; "
    "exact value shows how close to action (the action fires at "
    "``stallWindowMinutes``). Gives SREs a heads-up display between the "
    "first sustained observation and the eventual restart — without this "
    "gauge, three or more Redis/K8s-leg ticks can accumulate toward a "
    "restart with nothing on the dashboard until the gate trips.",
)

# Issue #312 — ``STALL_WINDOW_ELAPSED_SECONDS`` (above) is set on the
# holding/broken paths in ``run_stall_tick`` but NOT on the exception path
# (``RedisClientError`` | ``ApiException`` from ``_stall_condition_holds``
# → blind gap → tracker.reset() → re-raise). A successful prior tick's
# value (some nonzero accumulated window) therefore masquerades as a
# continuing stall window while the operator has in fact lost visibility.
# A paired freshness timestamp gauge set to ``time.time()`` immediately
# AFTER the ``STALL_WINDOW_ELAPSED_SECONDS.set(...)`` sequence on both
# the holding and broken paths lets dashboards compute staleness
# (``time() - fresh``) and alert on the gap — the same idiom as the rest
# of the Prometheus ecosystem's ``_created`` series. Set at the SAME site
# that already sets :data:`STALL_WINDOW_ELAPSED_SECONDS` in
# ``run_stall_tick`` so the gauge tracks the gauge exactly. Unlabelled:
# one series per process (the stall window is per-CR, but the gauge is
# process-wide as in #254; the freshness follows the same scope).
STALL_WINDOW_FRESH = Gauge(
    "openstudio_operator_stall_window_fresh",
    "Unix-epoch seconds of the most recent "
    "``STALL_WINDOW_ELAPSED_SECONDS`` update (issue #312). Set to "
    "``time.time()`` after every successful ``STALL_WINDOW_ELAPSED_"
    "SECONDS.set(...)`` in ``web_background_monitor.run_stall_tick`` "
    "(both the holding path at elapsed-seconds assignment and the "
    "broken/restart-reset path). The ``STALL_WINDOW_ELAPSED_SECONDS`` "
    "Gauge advances on the holding/broken paths but is NOT updated on "
    "the exception path (``RedisClientError`` | ``ApiException`` "
    "raised from ``_stall_condition_holds``) — so a prior tick's value "
    "can masquerade as a continuing stall window while the operator has "
    "in fact lost visibility. Dashboards should compute staleness as "
    "``time() - openstudio_operator_stall_window_fresh`` and alert on "
    "a sustained gap (the same idiom as the rest of the Prometheus "
    "ecosystem's ``_created`` series).",
)

# Issue #255 — ``kopf.event`` emission failure counter. ``EventEmitter``
# routes every Event through ``kopf.event`` (issue #164). When the
# apiserver is unreachable, kopf's event posting raises; the exception
# propagates up through the handler wrapper and is caught by
# ``handler_tick_failures_total{module,error_type}`` (issue #117). Today
# the on-call cannot distinguish "REST API down" from "Event posting
# down" — ``error_type`` captures ``ApiException`` for both, with no
# granularity for the kopf event path. This Counter is incremented inside
# the try/except that wraps the ``kopf.event`` call in ``EventEmitter.emit``,
# BEFORE re-raising, so a sustained ``ApiException`` storm from the event
# posting path is a distinct /metrics signal.
#
# Issue #311 — ``(namespace, name)`` per-CR labels on the four
# status-store / conflict counters + the four action counters. The
# motivation is multi-CR observability: an SRE cannot tell which CR
# is generating conflicts when the operator process serves multiple
# CRs (legal until the singleton guard picks a winner — but during the
# pick window, in a multi-namespace setup, or in the test harness where
# the singleton guard is short-circuited). The existing labelled
# counters (``status_map_caps_total{map_name}``,
# ``handler_tick_failures_total{module,error_type}``,
# ``events_*_total{reason}``) carry dimension-specific labels but NOT
# CR identity, so the per-CR dimension is added across the board. CR
# cardinality is bounded by the singleton guard (D05, one CR per
# namespace), so the multiplied series count stays small. EVENTS_EMIT_*
# counters inherit ``namespace`` + ``name`` for symmetry with the
# ``events_emitted_total`` family.
EVENTS_EMIT_FAILURES_TOTAL = Counter(
    "openstudio_operator_events_emit_failures_total",
    "``kopf.event`` emission failures caught by EventEmitter.emit "
    "(issue #255). Incremented inside the try/except wrapping the "
    "``kopf.event`` call BEFORE re-raising — a sustained nonzero rate "
    "means the operator cannot post Kubernetes Events to the apiserver "
    "(independent of the REST/Redis/K8s API signals that surface via "
    "``handler_tick_failures_total``). Labelled by ``namespace`` + "
    "``name`` (CR identity, issue #311) + ``reason`` (the warning-event "
    "reason the call site was attempting to post) so a dashboard can "
    "tell WHICH handler path's Event emission failed AND which CR the "
    "failure happened on (same ``reason`` vocabulary as "
    "``events_emitted_total`` so the two can be rate-correlated).",
    labelnames=["namespace", "name", "reason"],
)

# Issue #310 — Queue-depth observability + backpressure cap for
# ``QueuedKopfEventSink`` (``openstudio_operator.events_sinks``). The sink
# owns a single FIFO queue of deferred Warning Events
# (``(namespace, name, reason, message)`` tuples) that grows on every
# :meth:`QueuedKopfEventSink.defer_to_next_tick` and drains only on the next
# OSCM watch tick via :meth:`QueuedKopfEventSink.flush_for`. If the
# apiserver watch stream stalls the queue grows unbounded — three hardened
# call sites (the redis-URL guard #116, the redis-key-layout guard #163,
# the status-store cap #171) all assume the drain fires on the next watch
# event and will keep enqueueing otherwise. The same observability-gap
# family motivated #44 (Resque worker set) and #66 (key-layout), so this
# Gauge is the symmetric signal for the deferred-event queue: a sustained
# nonzero value means the watch stream is unhealthy and the deferred
# events are piling up; a value above ``MAX_DEFERRED_WARNING_EVENTS`` would
# have been impossible before the cap existed. Set in
# :meth:`QueuedKopfEventSink.defer_to_next_tick` (to ``len(self._queue)``
# after the append-or-drop) and in :meth:`QueuedKopfEventSink.flush_for`
# (to the post-drain length — typically 0 when the queue was solely this
# CR's entries, but the residual is reported faithfully so multi-CR
# backlogs are visible). Unlabelled — the queue is process-wide, not
# per-CR; one series for the whole sink keeps cardinality bounded.
WARNINGS_DEFERRED_QUEUE_DEPTH = Gauge(
    "openstudio_operator_warnings_deferred_queue_depth",
    "Depth of the in-process QueuedKopfEventSink queue (issue #310). Set "
    "after every defer_to_next_tick and flush_for. Sustained nonzero "
    "values mean the apiserver watch stream is stalled and Warning "
    "Events are piling up; the cap at MAX_DEFERRED_WARNING_EVENTS "
    "(1000) prevents unbounded growth, but the backpressure cap is the "
    "last line — a healthy cluster reads 0 on every drain.",
)

# Issue #310 — backpressure drop counter. When the queue hits
# ``MAX_DEFERRED_WARNING_EVENTS`` (1000) the next
# :meth:`QueuedKopfEventSink.defer_to_next_tick` call is DROPPED
# (not appended) and this counter is incremented. Labelled by ``reason``
# so a future second drop reason (e.g. a per-reason cap) can share the
# series without losing the original semantics. The initial vocabulary
# is ``queue_full`` — the deferral was rejected because the queue was
# at the cap. Without this counter a sustained drop storm is silent
# (the user-facing Warning Event is lost AND the only signal is the
# gauge plateauing at the cap, which is ambiguous with a healthy cap-
# saturated system). Alert on
# ``rate(openstudio_operator_warnings_deferred_dropped_total{reason=
# "queue_full"}[5m]) > 0`` — a nonzero drop rate means Warning Events
# were silently dropped and the operator's at-least-once deferred-event
# contract (issue #234) is broken.
WARNINGS_DEFERRED_DROPPED_TOTAL = Counter(
    "openstudio_operator_warnings_deferred_dropped_total",
    "Deferred Warning Events dropped by QueuedKopfEventSink before "
    "appending (issue #310). Incremented inside defer_to_next_tick "
    "when ``len(self._queue) >= MAX_DEFERRED_WARNING_EVENTS`` — the "
    "deferral is rejected, not queued, so the user-facing Warning Event "
    "is lost. Labelled by ``reason`` (initial vocabulary: ``queue_full``) "
    "so a future second drop reason can share the series without losing "
    "the original semantics. The companion "
    "``openstudio_operator_warnings_deferred_queue_depth`` Gauge hits "
    "the cap when this counter increments.",
    labelnames=["reason"],
)

#: Issue #44 — Resque key-layout leg-2 non-vacuity safeguard; issue #87 —
#: unconditional emission. Monotonic max of distinct Resque worker ids the
#: operator has EVER observed in process lifetime (web_background_monitor,
#: #13). Since #87 the worker set is read on EVERY sensing tick regardless
#: of queue depth (SMEMBERS ``resque:workers`` cardinality), so a healthy
#: idle fleet — empty queues, workers heartbeating — populates the gauge
#: within one poll. ``0`` with a reachable Redis therefore unambiguously
#: means no workers are registered: either the fleet is really gone or the
#: centralized Resque key constants do not match the live v3.11.0 layout —
#: the empty-registry branch of the stall-condition leg 2 is then
#: vacuously true, and the operator will periodic-restart web_background
#: while everything looks healthy. Surface this as a Prometheus signal so
#: an SRE can alert on ``openstudio_operator_resque_workers_seen_max == 0``.
RESQUE_WORKERS_SEEN_MAX = Gauge(
    "openstudio_operator_resque_workers_seen_max",
    "Resque workers seen in the worker set, emitted every poll regardless "
    "of queue depth (0 with a reachable Redis = no workers registered); "
    "monotonic max — survives worker disappearance within process lifetime",
)

# Issue #119 — observability surface for the status-store 409 retry loop.
# The retention pipeline + the prune CronJob both write to the same
# `.status.archivedAnalyses` map (see retention.py:517); their RMW cycle is
# only safe because status_store._mutate retries on 409 up to
# `MAX_CONFLICT_RETRIES` times. Without these counters a sustained conflict
# storm is invisible: the operator keeps responding healthy on /metrics and
# the only signal is a SLOW tick. Labelled by ``namespace`` + ``name``
# (CR identity, issue #311) so an SRE can attribute a sustained 409 burst
# to the specific CR generating the conflicts — the singleton guard
# (D05) bounds CR cardinality per namespace.
STATUS_CONFLICTS_TOTAL = Counter(
    "openstudio_operator_status_conflicts_total",
    "Per-attempt 409 responses from the Kubernetes API Server during "
    "status-store RMW cycles (incremented inside _mutate's except branch "
    "for each 409 before the backoff sleep; #119). Labelled by "
    "``namespace`` + ``name`` (issue #311) so a multi-CR operator process "
    "can attribute a conflict burst to the specific CR.",
    labelnames=["namespace", "name"],
)

# Issue #171 — defensive cap on the four CR .status maps (``softStops``,
# ``requeues``, ``startedSince``, ``archivedAnalyses``). The CRD schema
# accepts unbounded maps (preserved-unknown-fields), so a CR with
# ``update`` on the status subresource can grow any of them to etcd's 1.5
# MB object-size limit; the operator then reads + JSON-parses + merge-patches
# the full map on every timer tick. status_store._set_map_entry enforces a
# per-map cap (STATUS_MAP_MAX_ENTRIES = 10000) by dropping the oldest entries
# (sorted by key — the operator's keys are UUIDs, so the sort order is
# deterministic but not age-aware) before adding a new entry. This counter
# is incremented once per actual eviction (post-RMW, retry-stable), not per
# 409 attempt, so an alert on ``rate(...[5m]) > 0`` fires once per cap hit.
# Labelled by ``namespace`` + ``name`` (CR identity, issue #311) +
# ``map_name`` (softStops | requeues | startedSince | archivedAnalyses).
# The companion Warning Event (``StatusMapCapped``) is emitted from the
# same code path so the on-call has both a log/Event and a Prometheus
# signal to correlate.
STATUS_MAP_CAPS_TOTAL = Counter(
    "openstudio_operator_status_map_caps_total",
    "Defensive cap evictions issued by status_store when a CR .status map "
    "hits STATUS_MAP_MAX_ENTRIES (incremented once per actual cap hit, after "
    "the successful RMW — retry-stable, not per 409 attempt; #171). "
    "Labelled by ``namespace`` + ``name`` (CR identity, issue #311) + "
    "``map_name`` (softStops | requeues | startedSince | archivedAnalyses).",
    labelnames=["namespace", "name", "map_name"],
)

STATUS_CONFLICT_RETRIES_EXHAUSTED_TOTAL = Counter(
    "openstudio_operator_status_conflict_retries_exhausted_total",
    "Status-store RMW cycles that exhausted the 409 retry budget and "
    "raised StatusStoreConflictError; the tick that hit this counter was "
    "skipped (the failure surfaces as a WARNING log + no .status write). "
    "Issued by status_store._mutate just before raising; #119. "
    "Labelled by ``namespace`` + ``name`` (CR identity, issue #311) so a "
    "multi-CR operator process can distinguish which CR is exhausting "
    "its retry budget.",
    labelnames=["namespace", "name"],
)

# Issue #117 — observability surface for handler tick failures.
# Each handler timer wrapper (analysis_sla, datapoint_watchdog,
# worker_recycler, web_background_monitor) catches its respective
# exception tuples and silently skips the tick. Without a counter, a
# sustained degraded window (REST API down, Redis unreachable, k8s
# API unavailable) is invisible at the /metrics endpoint, and an SRE
# alerting on tick failure rate cannot tell which module is degraded.
# Labelled by ``namespace`` + ``name`` (CR identity, issue #311) +
# ``module`` (analysis_sla | datapoint_watchdog | worker_recycler |
# web_background_monitor) + ``error_type`` (OpenStudioApiError |
# StatusStoreError | ApiException | RedisClientError). Increment-by-1
# per tick the wrapper suppresses; the WARNING log line in the wrapper
# records the same event for log forwarding.
HANDLER_TICK_FAILURES_TOTAL = Counter(
    "openstudio_operator_handler_tick_failures_total",
    "Handler tick failures caught by the timer wrappers (issue #117). "
    "Labelled by ``namespace`` + ``name`` (CR identity, issue #311) + "
    "``module`` (analysis_sla | datapoint_watchdog | worker_recycler | "
    "web_background_monitor) + ``error_type`` (OpenStudioApiError | "
    "StatusStoreError | ApiException | RedisClientError). Increment-by-1 "
    "per tick the wrapper suppresses; the WARNING log line in the wrapper "
    "records the same event for log forwarding.",
    labelnames=["namespace", "name", "module", "error_type"],
)

# Issue #306 — observability surface for the storage-prune CronJob's
# skip-tick branches. The CronJob runs as a separate process from the
# operator (deploy/storage-cronjob.yaml invoking
# ``openstudio_operator.prune_entrypoint.main()``); the two skip-tick
# branches — ``prune_entrypoint.py:213-222`` (CR list failure) and
# ``:280-288`` (D12 exception tuple caught around ``run_retention_tick``)
# — log at WARNING level and return exit code 0. Without a counter, a
# sustained degraded window in the retention pipeline (Redis unreachable,
# REST API 5xx storm, ``StatusStoreConflictError`` thundering herd) is
# invisible at ``/metrics`` — the only signal is a Loki/CloudWatch log
# alert. Each OSCM timer wrapper has the analogue
# ``HANDLER_TICK_FAILURES_TOTAL{module, error_type}`` (#117); the prune
# actor now mirrors that pattern with a process-local counter. The
# CronJob pod exposes ``/metrics`` on port 9090 (the same port as the
# operator Deployment, gated by the parallel
# ``openstudio-storage-pruner-metrics-ingress`` NetworkPolicy), so a
# Prometheus job can scrape both processes out of the same target list.
# Labels mirror the two branch sites: ``cr_list_failure`` for the kube
# API list path, ``runtime_failure`` for the D12 exception tuple. The
# exception class name is already in the WARNING log line via
# ``type(exc).__name__`` — keeping the label vocabulary bounded to the
# two branch names keeps cardinality trivial (2 series max) and matches
# the regex of "one increment per tick the wrapper suppressed" that
# #117 established for the handler wrappers.
PRUNE_TICK_FAILURES_TOTAL = Counter(
    "openstudio_operator_prune_tick_failures_total",
    "Storage-prune CronJob tick failures caught by the skip-tick branches "
    "in ``prune_entrypoint.main()`` (issue #306). The CronJob runs as a "
    "separate process from the operator; the two skip-tick branches "
    "(CR list failure + D12 exception tuple caught around "
    "``run_retention_tick``) log at WARNING and return exit code 0, so "
    "without this counter a sustained degraded window (Redis unreachable, "
    "REST 5xx storm, StatusStoreConflictError) is invisible at "
    "``/metrics``. Labelled by ``reason`` (cr_list_failure | "
    "runtime_failure) — mirror of the two branch sites, bounded "
    "cardinality (2 series total). Increment-by-1 per tick the entrypoint "
    "suppresses; the WARNING log line records the same event for log "
    "forwarding with the exception class name. Scraped from the "
    "CronJob pod's plaintext ``/metrics`` on port 9090 (gated by the "
    "parallel ``openstudio-storage-pruner-metrics-ingress`` "
    "NetworkPolicy).",
    labelnames=["reason"],
)

# Issue #237 — observability surface for the dry-run gate on
# :class:`openstudio_operator.events.EventEmitter` (#164). When
# ``spec.dryRun=true`` the EventEmitter suppresses every Event that would
# have been posted to the kube-apiserver and only bumps a Python attribute
# (:attr:`EventEmitter.suppressed_count`). That attribute is per-process and
# per-tick — there was no /metrics signal, so a cluster running dry-run mode
# (canary staging, audit-only installs) was invisible to Prometheus: an SRE
# could not verify via a metrics scrape that the operator was actually doing
# the work, only a log scrape for the ``dry-run suppressed`` INFO line.
# Increment happens inside :meth:`EventEmitter.emit` at the same site that
# increments :attr:`EventEmitter.suppressed_count`. Labelled by ``reason``
# mirroring the warning-event reason vocabulary
# (``AnalysisSoftStopped`` | ``AnalysisEscalated`` | ``DatapointRequeued`` |
# ``DatapointRequeueExhausted`` | ``WorkerRecycled`` |
# ``WebBackgroundRestarted`` | ``ResqueKeyLayoutUnknown``) so a dashboard can
# tell WHICH action the dry-run gate intercepted, not just that it did.
# Companion emitted counter below lets ``rate(emitted) / rate(suppressed)``
# be derived without log parsing — the dry-run ratio is the headline SLO for
# an audit-only install.
EVENTS_DRY_RUN_SUPPRESSED_TOTAL = Counter(
    "openstudio_operator_events_dry_run_suppressed_total",
    "Kubernetes Events suppressed by the dry-run gate on EventEmitter (#164). "
    "Incremented at the same site as EventEmitter.suppressed_count, inside "
    "EventEmitter.emit when dry_run=True (issue #237). Labelled by "
    "``namespace`` + ``name`` (CR identity, issue #311) + ``reason`` "
    "(the same warning-event reasons used by the four handler modules).",
    labelnames=["namespace", "name", "reason"],
)

EVENTS_EMITTED_TOTAL = Counter(
    "openstudio_operator_events_emitted_total",
    "Kubernetes Events posted to the kube-apiserver via EventEmitter (#164). "
    "Companion to events_dry_run_suppressed_total: emitted-vs-suppressed "
    "rate is the headline SLO for an audit-only install (issue #237). "
    "Labelled by ``namespace`` + ``name`` (CR identity, issue #311) + "
    "``reason`` (the same warning-event reasons used by the four handler "
    "modules).",
    labelnames=["namespace", "name", "reason"],
)

# Issue #179 — per-CR datapoint-budget Histogram. The SLA monitor and the
# datapoint watchdog each iterate a count off the OpenStudio REST analysis
# payload (or the equivalent summary endpoint) — the number of analyses per
# tick for the SLA, the number of started datapoints per tick for the
# watchdog — to decide whether to soft-stop, requeue, or escalate. The value
# is a Counter-shaped integer that was never recorded, so an on-call SRE
# investigating "why are SLA stops spiking?" had no way to correlate the
# spike with a shift in analysis-size distribution. The Histogram is
# bucket-capped at `[5, 10, 50, 100, 500, 1000, 5000]` so the
# per-(analysis | datapoint) cardinality is bounded while still surfacing
# the "we just started getting 5000-point analyses" shift (Goal 10, OSS
# hardening). No labels: per-observation (one .observe() per observed count),
# not per-CR — relabelling per analysis would multiply the series count by
# the analysis count and defeat the bounded-cardinality design.
ANALYSIS_DATAPOINT_COUNT = Histogram(
    "openstudio_operator_analysis_datapoint_count",
    "Datapoints per analysis observed by SLA / watchdog modules",
    buckets=[5, 10, 50, 100, 500, 1000, 5000],
)

# Issue #308 — handler tick-duration Histogram. The four ``@kopf.timer``
# wrappers (analysis_sla, datapoint_watchdog, worker_recycler,
# web_background_monitor) catch their respective exception tuples and
# silently skip the tick on transient failure — D12 says "handlers skip
# the tick and retry naturally on the next poll". A slow tick that masks
# the retry window (REST 5xx storm, GC pause, kopf bus contention, Mongo
# write amplification, NFS stall) was previously invisible: the only
# /metrics signal was ``handler_tick_failures_total`` AFTER the tick
# crossed the failure threshold, and an SRE alerting on tick failure
# rate had no early warning that a tick was trending slow. This Histogram
# observes the wall-clock duration of every wrapper invocation —
# regardless of success or caught-exception outcome — so a sustained
# degradation between the healthy-band baseline and the eventual
# ``handler_tick_failures_total`` increment is visible. ``module``
# label vocabulary matches the failure counter (analysis_sla |
# datapoint_watchdog | worker_recycler | web_background_monitor) so a
# dashboard can correlate latency with failure rate on the same module
# dimension. Buckets cover 50 ms (a fast idle tick) through 30 s
# (a degraded poll that masks the retry window); the issue body is the
# canonical source of the bucket choice.
HANDLER_TICK_DURATION_SECONDS = Histogram(
    "openstudio_operator_handler_tick_duration_seconds",
    "Wall-clock duration of the four @kopf.timer wrappers (issue #308). "
    "Observed at the END of each wrapper invocation, regardless of "
    "success or caught-exception outcome, so a sustained degradation "
    "(REST 5xx storm, GC pause, kopf bus contention, NFS stall) is "
    "visible to Prometheus before it crosses the failure threshold "
    "captured by ``handler_tick_failures_total``. Labelled by "
    "``module`` (analysis_sla | datapoint_watchdog | worker_recycler | "
    "web_background_monitor) — same vocabulary as the failure counter, "
    "so a dashboard can correlate latency with failure rate on the same "
    "module dimension.",
    labelnames=["module"],
    buckets=_HANDLER_TICK_BUCKETS,
)

# Issue #308 — REST round-trip duration Histogram. ``OpenStudioClient._request``
# runs the 3-attempt retry loop (GET-only) with jittered exponential
# backoff; each attempt is a real HTTP round-trip, but only the final
# outcome (a 200 response, or an exhausted-budget ``OpenStudioApiError``)
# is surfaced to the caller — a single slow attempt that succeeds on
# retry is invisible at /metrics, and so is a 5xx storm that succeeds
# on the third try. This Histogram observes the wall-clock duration of
# every ``_request`` call, labelled by HTTP ``method`` (the same vocab
# the operator uses — GET | POST | DELETE) and ``outcome``. The
# ``outcome`` label is the terminal result the caller would see:
# ``"200"`` for any 2xx/3xx that returned, ``"exception"`` for any
# raised ``OpenStudioApiError`` (4xx immediately, 5xx retries exhausted,
# non-GET 5xx per issue #226). The bucket set is tighter than the
# handler set because v3.11.0 endpoints are fast on a healthy cluster;
# a 5 s cap keeps resolution at the healthy band where a degrade is
# detectable. A sustained non-zero rate on ``outcome="exception"`` is
# the canonical "REST API degraded" alert, and the per-method split
# tells the on-call which verb is responsible.
REST_REQUEST_DURATION_SECONDS = Histogram(
    "openstudio_operator_rest_request_duration_seconds",
    "Wall-clock duration of every ``OpenStudioClient._request`` call "
    "(issue #308), including the GET-only 3x retry loop. Observed at "
    "the END of ``_request`` so the time spent inside the retry envelope "
    "is part of the observation. Labelled by ``method`` (GET | POST | "
    "DELETE — the verbs the operator actually uses) and ``outcome`` "
    "(``\"200\"`` for any successful 2xx/3xx response, ``\"exception\"`` "
    "for any raised ``OpenStudioApiError``: 4xx immediately, 5xx retries "
    "exhausted, or non-GET 5xx per issue #226). A sustained non-zero "
    "rate on ``outcome=\"exception\"`` is the canonical REST-degraded "
    "alert; the per-method split tells the on-call which verb is "
    "responsible.",
    labelnames=["method", "outcome"],
    buckets=_REST_REQUEST_BUCKETS,
)

#: Re-exported alias for back-compat with the historical ``DEFAULT_METRICS_PORT``
#: identifier and any external callers that import it from this module (issue #165
#: consolidated the port into :data:`openstudio_operator._constants.METRICS_PORT`).
DEFAULT_METRICS_PORT = METRICS_PORT

_start_lock = threading.Lock()
_started = False
_active_port: int | None = None


def start_metrics_server(port: int | None = None, addr: str = "0.0.0.0") -> int | None:
    """Serve /metrics from a daemon thread (idempotent).

    Returns the port /metrics is actively served on. If the server is
    already running in this process, returns the already-active port — a
    second server is never started (the handlers package import starts it
    at operator startup, so later calls are reads, not restarts). Returns
    ``None`` only when the port could not be bound (logged as a warning —
    losing metrics must never take the operator down).
    """
    global _started, _active_port
    with _start_lock:
        if _started:
            return _active_port
        bound_port = DEFAULT_METRICS_PORT if port is None else port
        try:
            server, thread = prometheus_client.start_http_server(bound_port, addr=addr)
        except OSError as exc:
            logger.warning("Cannot serve /metrics on %s:%s: %s", addr, bound_port, exc)
            return None
        _started = True
        _active_port = server.server_address[1]
        logger.info(
            "Serving /metrics on %s:%s (daemon thread: %s)", addr, bound_port, thread.daemon
        )
        return _active_port


def is_metrics_server_started() -> bool:
    """Whether :func:`start_metrics_server` has successfully run in this process."""
    return _started
