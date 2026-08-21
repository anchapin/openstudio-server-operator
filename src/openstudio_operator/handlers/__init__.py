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
"""

import logging

import kopf

from openstudio_operator import singleton, status_store
from openstudio_operator.client_factory import get_read_only_redis_client
from openstudio_operator.events_sinks import get_default_sink
from openstudio_operator.handlers import (  # noqa: F401
    analysis_sla,
    datapoint_watchdog,
    dry_run_audit,
    web_background_monitor,
    worker_recycler,
)
from openstudio_operator.logging_setup import install_json_logging
from openstudio_operator.metrics import REDIS_KEY_LAYOUT_STATUS, start_metrics_server
from openstudio_operator.redis_client import (
    OperatorConfigError,
    RedisClientError,
)

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


# Issue #163 — boot-time Redis key-layout validation (D05/D13 invariant).
# ``validate_key_layout()`` is documented in AGENTS.md as the startup-time
# assertion that the Redis Service ``queue`` exposes the Resque keyspace the
# operator reads (``resque:worker:*``, ``resque:queue:simulations``, etc.). A
# quiet drift (helm chart upgrade to a Resque-2.x-with-different-prefix
# layout, or a different queue backend entirely) would otherwise only be
# noticed downstream when the worker-registry gauges go silent — too late for
# the boot-time identity the singleton guard polices. The fix is to call
# ``validate_key_layout()`` once per CR at operator boot (the kopf watch's
# initial listing IS the boot path for any non-zero namespace), emit a
# structured log line ``redis_key_layout=ok|degraded|unreachable``, and queue
# a Warning Event on the degraded branch. The whole call is wrapped in
# try/except so a Redis connectivity failure degrades gracefully (operator
# continues to boot, retries on the next CR tick) — never crashes the
# process. The drain path is the consolidated :func:`_drain_queued_warning_events`.


def _emit_redis_key_layout_event(
    namespace: str, name: str, reason: str, message: str
) -> None:
    """Defer a redis-key-layout Warning Event to the next OSCM watch tick.

    Counterpart to :func:`_check_redis_key_layout_for_cr`'s degraded
    branch. Fires from the per-CR check; the actual ``kopf.event``
    emit happens on the next OSCM watch tick (same queue/defer pattern
    as :func:`_emit_redis_warning_event` for issue #116, both routed
    through the consolidated :data:`_sink` after #234).
    """
    _sink.defer_to_next_tick(
        namespace=namespace, name=name, reason=reason, message=message,
    )


def _check_redis_key_layout_for_cr(
    item: object, *, logger: logging.Logger
) -> str:
    """Run ``validate_key_layout()`` for one OSCM CR; return a status string.

    Returns one of ``"ok"``, ``"degraded"``, ``"unreachable"``, ``"error"``,
    or ``"skipped"`` (empty redis_url, nameless item, etc.). The handler
    controls the structured log line based on the return value; the test
    suite asserts the line is emitted (see
    ``tests/test_redis_client.py::test_redis_key_layout_check_emits_*``).

    Wrapped in try/except so a Redis connectivity failure (network down,
    DNS failure, refused connection, timeout) does NOT crash the boot —
    the operator continues in degraded mode and retries on the next tick,
    per the issue's "silent-misbehavior risk" counter-spec.

    Issue #253 — every return path updates the cluster-wide
    ``openstudio_operator_redis_key_layout_status`` Gauge: ``1.0`` on
    ``ok`` (the most recent validator run succeeded) and ``0.0`` for
    every other terminal status (``degraded`` | ``unreachable`` |
    ``error`` | ``skipped``). The gauge is a cluster-wide latest-observation
    signal — no per-CR labels, so cardinality stays bounded regardless of
    CR count.
    """
    if not isinstance(item, dict):
        REDIS_KEY_LAYOUT_STATUS.set(0.0)
        return "skipped"
    meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else item
    if not isinstance(meta, dict):
        REDIS_KEY_LAYOUT_STATUS.set(0.0)
        return "skipped"
    ns = str(meta.get("namespace") or "")
    nm = str(meta.get("name") or "")
    if not ns or not nm:
        REDIS_KEY_LAYOUT_STATUS.set(0.0)
        return "skipped"
    spec = item.get("spec") or {}
    redis_url = str(spec.get("redisUrl") or "")
    if not redis_url:
        # Empty URL is a separate concern (#116) — don't fail layout
        # validation on the operator's intentional refusal to default.
        logger.debug(
            "redis_key_layout skip: OSCM %s/%s has empty spec.redisUrl (#116)",
            ns,
            nm,
        )
        REDIS_KEY_LAYOUT_STATUS.set(0.0)
        return "skipped"
    try:
        get_read_only_redis_client(redis_url).validate_key_layout()
    except OperatorConfigError as exc:
        logger.warning(
            "redis_key_layout=degraded namespace=%s name=%s reason=%s: %s",
            ns,
            nm,
            "layout_drift",
            exc,
        )
        _emit_redis_key_layout_event(
            ns,
            nm,
            "RedisKeyLayoutDrift",
            (
                "Redis key layout validation failed (issue #163): "
                f"{exc}. Modules 3/5 (worker recycler / web_background "
                "stall) may produce noisy signals or stay silent — the "
                "centralized Resque key constants in "
                "src/openstudio_operator/redis_client.py do not match the "
                "live Redis layout. Verify with "
                f"`redis-cli -u <redis_url> KEYS 'resque:*'` and update "
                "the constants (see issue #44 / docs/kind-validation.md)."
            ),
        )
        REDIS_KEY_LAYOUT_STATUS.set(0.0)
        return "degraded"
    except (RedisClientError, OSError) as exc:
        # Redis connectivity failure (refused, DNS, timeout) — wire-level,
        # not a layout drift. Loud warning, no event (we don't know the
        # layout drifted; we just couldn't reach the server). Operator
        # MUST continue to boot — the issue's hard requirement.
        logger.warning(
            "redis_key_layout=unreachable namespace=%s name=%s: %s",
            ns,
            nm,
            exc,
        )
        REDIS_KEY_LAYOUT_STATUS.set(0.0)
        return "unreachable"
    except Exception as exc:  # noqa: BLE001 — defensive last-resort (see web_background_monitor.py)
        logger.warning(
            "redis_key_layout=error namespace=%s name=%s: %s: %s",
            ns,
            nm,
            type(exc).__name__,
            exc,
        )
        REDIS_KEY_LAYOUT_STATUS.set(0.0)
        return "error"
    logger.info(
        "redis_key_layout=ok namespace=%s name=%s",
        ns,
        nm,
    )
    REDIS_KEY_LAYOUT_STATUS.set(1.0)
    return "ok"


@kopf.on.event("energy.nrel.gov", "v1alpha1", "openstudioclustermanagers")
def _redis_key_layout_check(
    name: str, namespace: str, body: kopf.Body, **_kwargs: object
) -> None:
    """Run ``validate_key_layout()`` per CR at boot (initial listing) and on every change.

    The kopf watch's initial listing fires this for every existing CR — that
    IS the boot path. Idempotent: ``validate_key_layout()`` is reentrant and
    capped at ``VALIDATE_SCAN_KEY_BUDGET`` keys. A queued Warning Event is
    drained on the next tick by :func:`_drain_queued_warning_events`.
    """
    _check_redis_key_layout_for_cr(body, logger=logger)


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


@kopf.on.event("energy.nrel.gov", "v1alpha1", "openstudioclustermanagers")
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