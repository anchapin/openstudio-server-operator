"""Kopf handlers, one module per operator component in the plan doc.

Storage pruning/archival is NOT here by design (#78): the retention pipeline
runs as native Job primitives via ``deploy/storage-cronjob.yaml`` +
``openstudio_operator.prune_entrypoint`` (library code in
``openstudio_operator.retention``) — the operator core keeps no storage
polling loop.

Issue #234 — three near-identical queue/drain mechanisms (#116 URL guard,
#163 key-layout guard, #171 status-map-cap guard) collapsed into a single
:class:`openstudio_operator.events_sinks.QueuedKopfEventSink`. The
Warning Event reasons (``RedisUrlEmpty``, ``RedisKeyLayoutDrift``,
``StatusMapCapped``) are preserved verbatim.

Issue #584 — the key-layout check itself (the ``@kopf.on.event``
registration + the per-CR validator) moved OUT of this package init into
:mod:`openstudio_operator.handlers.redis_layout_check`, so
``web_background_monitor``'s #490 revalidation rider imports it at module
top level instead of reaching back into the package via a function-local
import (the deferred-import cycle this file used to force). The sink
wiring below is unchanged: the check defers through the SAME shared sink
this init binds, and this init's drain handler still flushes it.
"""

import logging

import kopf

from openstudio_operator import singleton, status_store
from openstudio_operator._constants import CRD_SPEC
from openstudio_operator.events_sinks import get_default_sink
from openstudio_operator.handlers import (  # noqa: F401
    analysis_sla,
    datapoint_watchdog,
    dry_run_audit,
    redis_layout_check,
    web_background_monitor,
    worker_recycler,
)
from openstudio_operator.logging_setup import install_json_logging
from openstudio_operator.metrics import start_metrics_server

logger = logging.getLogger(__name__)

# Issue #256 — install the structured JSON log formatter on the root
# logger BEFORE any handler module emits its first record. Idempotent:
# ``install_json_logging`` is guarded by a sentinel attribute on the root
# logger so repeated ``import openstudio_operator.handlers`` (collection +
# execution in pytest, or kopf's own startup re-import) does not double
# the log output. The kopf structured logger (``kopf.objects`` / ``kopf``)
# is NOT replaced — its records flow through the same root chain and are
# formatted as JSON by the handler we install here.
install_json_logging()

# Issue #234 — the consolidated Warning-Event queue/drain mechanism.
# Replaces the three module-level queues + enqueue functions + drain
# handlers from issues #116, #163, and #171 with one in-process queue
# flushed by a single ``@kopf.on.event`` handler. Every Warning Event
# reason (``RedisUrlEmpty``, ``RedisKeyLayoutDrift``, ``StatusMapCapped``)
# defers into the same sink; the next OSCM watch tick drains every
# queued entry for that CR. See ``openstudio_operator.events_sinks`` for
# the class contract.
#
# Rationale (preserved from the original three blocks):
# kopf 1.44+ requires a populated ``settings_var`` ContextVar to enqueue
# events (the posting engine reads it to know whether posting is enabled),
# which is only set inside an active kopf handler — queueing + deferring
# sidesteps that constraint for the singleton-guard URL check, the
# boot-time key-layout check, and the status_store RMW path, none of
# which run inside a kopf callback.
_sink = get_default_sink()


def _emit_redis_warning_event(*, namespace: str, name: str, message: str) -> None:
    """Defer a ``RedisUrlEmpty`` Warning Event to the next OSCM watch tick.

    Counterpart to :func:`openstudio_operator.singleton._emit_redis_url_guard_events`,
    which fires from boot / on a non-OSCM-object watchdog event. The
    actual ``kopf.event`` emit happens on the next OSCM watch tick via
    :func:`_drain_queued_warning_events` — kopf only permits emits from
    within an active callback, and the singleton guard runs from
    non-event contexts.
    """
    _sink.defer_to_next_tick(
        namespace=namespace, name=name, reason="RedisUrlEmpty", message=message,
    )


# Issue #171 — production kopf-backed Warning-Event sink for the
# status-store defensive cap. The status_store is a library module
# that does NOT import kopf directly (it lives below the handler
# layer); the handlers package is the entrypoint and installs the sink
# at module load time via the preserved
# :func:`openstudio_operator.status_store.set_event_sink` seam. The
# underlying queue is the consolidated :data:`_sink` after #234.
def _emit_status_map_cap_event(
    namespace: str, name: str, reason: str, message: str
) -> None:
    """Defer a cap-eviction Warning Event to the next OSCM watch tick.

    Counterpart to :func:`openstudio_operator.status_store._emit_status_map_event`
    (the module-level sink hook). The status_store calls this from
    inside its RMW cycle; the actual ``kopf.event`` emit happens on
    the next OSCM watch tick via the consolidated drain handler — same
    architecture as the redis-URL guard and the key-layout guard after
    #234 collapsed the three queue/drain mechanisms.
    """
    _sink.defer_to_next_tick(
        namespace=namespace, name=name, reason=reason, message=message,
    )


status_store.set_event_sink(_emit_status_map_cap_event)


@kopf.on.event(**CRD_SPEC)
def _drain_queued_warning_events(
    name: str, namespace: str, **_kwargs: object
) -> None:
    """Drain every queued Warning Event for THIS CR; the consolidated drain.

    Replaces the three module-level drain handlers from #116, #163, and
    #171 (one per queue) with a single ``@kopf.on.event`` wrapper that
    delegates to :meth:`openstudio_operator.events_sinks.QueuedKopfEventSink.flush_for`.
    Fires once per OSCM watch event; an empty queue is a no-op. The
    Warning Event reasons preserved verbatim: ``RedisUrlEmpty`` (#116),
    ``RedisKeyLayoutDrift`` (#163), ``StatusMapCapped`` (#171).

    Issue #402 — the drain is two-phase: ``status.deferredEvents``
    first (the crash-surviving mirror), then the in-memory queue. On a
    fresh operator start the in-memory queue is empty but the watch's
    initial listing fires this handler, so persisted entries re-emit on
    the very first tick after a restart.
    """
    _sink.flush_for(namespace=namespace, name=name)


# Issue #402 — deferred-Warning-queue persistence across operator restarts.
# ``QueuedKopfEventSink``'s queue is process-local; a crash while it held
# entries silently dropped every queued Warning (no ``queue_full`` drop —
# the loss was invisible to /metrics). The fix mirrors every ACCEPTED
# deferral into ``status.deferredEvents`` via the :class:`StatusStore` RMW
# (D04 — the single durable-state surface) and makes ``flush_for`` drain
# the persisted list before the in-memory queue. The store factory is
# armed from a ``@kopf.on.startup`` handler — NOT at import time — so
# importing this package in tests / library contexts never builds a
# ``CustomObjectsApi`` against whatever kubeconfig the host carries;
# persistence only exists once a real operator run begins.
def _deferred_event_store(namespace: str, name: str) -> status_store.StatusStore:
    """Build the per-CR ``.status`` persistence adapter for the shared sink (#402)."""
    return status_store.StatusStore(
        namespace, name, singleton.operator_custom_objects_api()
    )


@kopf.on.startup()
def _install_deferred_event_persistence(logger: kopf.Logger, **_kwargs: object) -> None:
    """Arm ``status.deferredEvents`` persistence on the shared sink (#402).

    Runs once per operator start, before the watch streams (and therefore
    before any ``_drain_queued_warning_events`` invocation) — the restart
    recovery path is armed by the time the first OSCM watch tick fires.
    """
    _sink.set_store_factory(_deferred_event_store)
    logger.info(
        "deferred-event persistence armed (issue #402): accepted deferrals "
        "mirror to status.deferredEvents and drains run persisted-first"
    )


# Operator startup wiring: ``kopf run --module openstudio_operator.handlers``
# imports this package exactly once, making this import path the operator's
# entrypoint — so the Prometheus /metrics server is started here (issue #17).
# Idempotent and failure-tolerant; see openstudio_operator/metrics.py.
start_metrics_server()

# Singleton gate (issue #14, D05): exactly one OSCM CR per namespace is served
# — the oldest. ``singleton``'s own @kopf.on.event / @kopf.on.startup handlers
# police conflicts (Warning Events + loud logs), and this call centrally wraps
# every OSCM @kopf.timer registered above so all handler modules are gated
# without editing their files. MUST run after the handler modules are imported
# (it is); future handler modules: add them to the import block above and the
# gate picks them up here automatically. Idempotent.
singleton.install_singleton_guard()