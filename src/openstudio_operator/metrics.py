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

logger = logging.getLogger(__name__)

SOFT_STOPS_TOTAL = Counter(
    "openstudio_operator_soft_stops_total",
    "Analyses soft-stopped by the SLA monitor",
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
    "Worker deployment recycles performed",
)

WORKER_PODS_EVICTED_TOTAL = Counter(
    "openstudio_operator_worker_pods_evicted_total",
    "Worker pods surgically evicted by the analysis-SLA escalation "
    "(incremented by the SLA monitor, #9; counts decisions — dry-run ticks "
    "increment too, matching SOFT_STOPS_TOTAL)",
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
    "delete and the increment)",
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
# the only signal is a SLOW tick.
STATUS_CONFLICTS_TOTAL = Counter(
    "openstudio_operator_status_conflicts_total",
    "Per-attempt 409 responses from the Kubernetes API Server during "
    "status-store RMW cycles (incremented inside _mutate's except branch "
    "for each 409 before the backoff sleep; #119)",
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
# ``map_name`` label values: softStops | requeues | startedSince |
# archivedAnalyses. The companion Warning Event (``StatusMapCapped``) is
# emitted from the same code path so the on-call has both a log/Event and
# a Prometheus signal to correlate.
STATUS_MAP_CAPS_TOTAL = Counter(
    "openstudio_operator_status_map_caps_total",
    "Defensive cap evictions issued by status_store when a CR .status map "
    "hits STATUS_MAP_MAX_ENTRIES (incremented once per actual cap hit, after "
    "the successful RMW — retry-stable, not per 409 attempt; #171). "
    "Labelled by map_name (softStops | requeues | startedSince | "
    "archivedAnalyses).",
    labelnames=["map_name"],
)

STATUS_CONFLICT_RETRIES_EXHAUSTED_TOTAL = Counter(
    "openstudio_operator_status_conflict_retries_exhausted_total",
    "Status-store RMW cycles that exhausted the 409 retry budget and "
    "raised StatusStoreConflictError; the tick that hit this counter was "
    "skipped (the failure surfaces as a WARNING log + no .status write). "
    "Issued by status_store._mutate just before raising; #119.",
)

# Issue #117 — observability surface for handler tick failures.
# Each handler timer wrapper (analysis_sla, datapoint_watchdog,
# worker_recycler, web_background_monitor) catches its respective
# exception tuples and silently skips the tick. Without a counter, a
# sustained degraded window (REST API down, Redis unreachable, k8s
# API unavailable) is invisible at the /metrics endpoint, and an SRE
# alerting on tick failure rate cannot tell which module is degraded.
# Per-issue #117: one increment per observation (i.e., per tick that
# the wrapper caught), labelled by module name and exception class.
HANDLER_TICK_FAILURES_TOTAL = Counter(
    "openstudio_operator_handler_tick_failures_total",
    "Handler tick failures caught by the timer wrappers (issue #117). "
    "Labelled by module (analysis_sla | datapoint_watchdog | "
    "worker_recycler | web_background_monitor) and error_type "
    "(OpenStudioApiError | StatusStoreError | ApiException | "
    "RedisClientError). Increment-by-1 per tick the wrapper suppresses; "
    "the WARNING log line in the wrapper records the same event for log "
    "forwarding.",
    labelnames=["module", "error_type"],
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
    "EventEmitter.emit when dry_run=True (issue #237). Labelled by reason "
    "(the same warning-event reasons used by the four handler modules).",
    labelnames=["reason"],
)

EVENTS_EMITTED_TOTAL = Counter(
    "openstudio_operator_events_emitted_total",
    "Kubernetes Events posted to the kube-apiserver via EventEmitter (#164). "
    "Companion to events_dry_run_suppressed_total: emitted-vs-suppressed "
    "rate is the headline SLO for an audit-only install (issue #237). "
    "Labelled by reason (the same warning-event reasons used by the four "
    "handler modules).",
    labelnames=["reason"],
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
