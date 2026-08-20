"""Queued Kopf-event sink (#234).

Collapses the three near-identical queue/drain mechanisms from
``handlers/__init__.py`` — the redis-URL guard (#116), the redis-key-layout
guard (#163), and the status-map-cap guard (#171) — into a single class.
Each emitted Warning Event is queued in-process and flushed on the next
OSCM watch event by a single ``@kopf.on.event`` drain handler installed
by ``handlers/__init__.py``.

The kopf built-in :func:`kopf.event` is only callable from inside an active
kopf callback (the posting engine reads the ``settings_var`` ContextVar to
know whether posting is enabled). The three hardened call sites run from
non-callback contexts — the singleton guard's URL check fires at boot
/ on a watchdog event for a non-OSCM object, the boot-time key-layout
check runs in the kopf watch's initial listing, and the ``status_store``
RMW path is a plain Python method call (not a kopf callback). Each of
those paths needs the same workaround: enqueue the ``(ns, name, reason,
message)`` tuple, then drain on the next OSCM watch tick when a real
callback is running.

This module is the single home for that pattern. Future hardening
(a fourth Warning, a metric for deferred-event count, a backpressure cap,
a test seam) lives here — not as a fourth copy of the queue/defer/drain
mechanism in ``handlers/__init__.py``.

Issue #402 — the queue is also PERSISTED. Every accepted deferral is
mirrored into the CR's ``status.deferredEvents`` array through a
:class:`DeferredEventStore` (the :class:`StatusStore` RMW in production,
wired by ``handlers/__init__.py`` from a ``@kopf.on.startup`` handler),
and :meth:`QueuedKopfEventSink.flush_for` drains that persisted list
FIRST, before the in-memory queue. A crashed operator therefore
re-emits every queued Warning on its first OSCM watch tick after
restart — the at-least-once contract #234/#310 promise, which the
purely in-process queue broke (entries were silently lost, with no
``queue_full`` drop to observe). Persistence failures (apiserver
unreachable, 409 budget exhausted) degrade to the pre-#402 in-memory
behavior: logged, never fatal to the deferral path.

Scope guard:
    * Issue #164's :class:`openstudio_operator.events.EventEmitter` is a
      separate surface — it gates ``kopf.event`` per-tick on ``dry_run``
      (D11) and lives inside the handler invocation. This module is the
      *pre-tick deferral* mechanism for call sites that don't have a
      callback to begin with.
    * The :func:`openstudio_operator.status_store.set_event_sink` seam
      is preserved verbatim — it remains the contract between the
      library (status_store) and the operator entrypoint
      (handlers/__init__.py). What changes is what gets installed there.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Final, Protocol

import kopf

from openstudio_operator import metrics as metrics_module

logger = logging.getLogger(__name__)

#: Backpressure cap (issue #310). The deferred-event queue holds Warning
#: Events that need a kopf watch tick to emit. If the apiserver watch
#: stream stalls, the queue grows unbounded — the three hardened call
#: sites (redis-URL guard #116, redis-key-layout guard #163,
#: status-store cap #171) all keep enqueueing regardless. ``1000`` is the
#: floor: it is two orders of magnitude above any realistic single-CR
#: backlog (the three callers dedupe to ~1 entry per CR) and three
#: orders of magnitude above any healthy-tick backlog, so a cap hit
#: always means the watch stream is unhealthy. Exceeding the cap drops
#: the deferred event AND increments
#: ``WARNINGS_DEFERRED_DROPPED_TOTAL{reason="queue_full"}`` so the loss
#: is observable on /metrics — the silent-drop failure mode that this
#: constant prevents is exactly the failure mode that motivated the
#: Gauge / Counter pair.
MAX_DEFERRED_WARNING_EVENTS: Final = 1000

#: Drop-reason label vocabulary for :data:`WARNINGS_DEFERRED_DROPPED_TOTAL`.
#: The initial reason is ``queue_full`` (deferral rejected because the
#: queue was at ``MAX_DEFERRED_WARNING_EVENTS``). Exposed as a module
#: constant so the test (and any future cap-eviction branch) can import
#: the same string the production code uses — a typo in the test would
#: silently miss the alert.
DROP_REASON_QUEUE_FULL: Final = "queue_full"


class DeferredEventStore(Protocol):
    """Structural interface for the ``status.deferredEvents`` surface (#402).

    :class:`openstudio_operator.status_store.StatusStore` satisfies this
    structurally (``get_deferred_events`` / ``append_deferred_event`` /
    ``clear_deferred_events``); tests satisfy it with in-memory fakes. The
    sink deliberately does NOT import the concrete store — the production
    wiring happens in ``handlers/__init__.py`` (a ``@kopf.on.startup``
    handler installs a factory that builds a ``StatusStore`` over
    :func:`openstudio_operator.singleton.operator_custom_objects_api`), so
    the dependency direction stays handlers → (events_sinks, status_store)
    and importing this module never constructs a Kubernetes client.

    ``max_entries`` on :meth:`append_deferred_event` is the persisted twin
    of :data:`MAX_DEFERRED_WARNING_EVENTS` — the caller passes the same
    cap so the ``.status`` array can never outgrow the in-memory queue's
    own bound (defensive backstop against a clear-failure streak, not a
    second backpressure gate — drops are still counted ONLY at
    :meth:`QueuedKopfEventSink.defer_to_next_tick`).
    """

    def get_deferred_events(self) -> list[dict[str, str]]: ...

    def append_deferred_event(
        self, entry: Mapping[str, str], *, max_entries: int | None = None
    ) -> None: ...

    def clear_deferred_events(self) -> None: ...


#: Builds the per-CR persistence adapter for a ``(namespace, name)`` pair.
#: Installed on the default sink by the handlers entrypoint at operator
#: startup (issue #402); ``None`` (the default) means in-memory-only —
#: the exact pre-#402 behavior.
DeferredEventStoreFactory = Callable[[str, str], DeferredEventStore]


class QueuedKopfEventSink:
    """In-process queue of pending Warning Events, flushed by one drain handler.

    The class owns a single FIFO queue of ``(namespace, name, reason,
    message)`` tuples plus the flush logic that turns them into
    :func:`kopf.event` calls. The handlers package registers exactly one
    ``@kopf.on.event`` wrapper that calls :meth:`flush_for` — every
    deferred Warning for the matching CR is emitted on that tick.

    The class is constructed once at operator startup (via the
    module-level :data:`_default_sink`) and shared by every call site:

    * The redis-URL guard helper in ``handlers/__init__.py`` calls
      :meth:`defer_to_next_tick` with ``reason="RedisUrlEmpty"``.
    * The redis-key-layout check (also in ``handlers/__init__.py``)
      calls it with ``reason="RedisKeyLayoutDrift"``.
    * The :mod:`openstudio_operator.status_store` cap-eviction path is
      routed here through the existing
      :func:`openstudio_operator.status_store.set_event_sink` seam.

    Construction is cheap (one empty list); the class holds no
    kopf-side state, so test isolation is a one-line ``clear()`` between
    tests against the module-level instance, or a fresh ``QueuedKopfEventSink()``
    for full isolation.

    Issue #402 — an optional ``store_factory`` arms persistence: every
    accepted deferral is mirrored into ``status.deferredEvents`` for the
    CR, and :meth:`flush_for` drains the persisted list before the
    in-memory queue. A factory of ``None`` (the default) keeps the sink
    purely in-process — the pre-#402 behavior, and the mode every
    pre-existing test of this class runs in.
    """

    __slots__ = ("_queue", "_store_factory")

    def __init__(self, store_factory: DeferredEventStoreFactory | None = None) -> None:
        """Initialize an empty queue. Idempotent and cheap; safe to re-create per test.

        ``store_factory`` (issue #402) optionally wires the CR
        ``.status`` persistence surface; ``None`` disables persistence.
        """
        self._queue: list[tuple[str, str, str, str]] = []
        self._store_factory: DeferredEventStoreFactory | None = store_factory

    def set_store_factory(self, factory: DeferredEventStoreFactory | None) -> None:
        """Install/replace/disable the ``.status`` persistence factory (#402).

        The handlers entrypoint calls this from a ``@kopf.on.startup``
        handler (NOT at import time) so importing the handlers package in
        tests or library contexts never arms persistence against whatever
        kubeconfig the host happens to carry. ``None`` restores the
        in-memory-only behavior.
        """
        self._store_factory = factory

    @property
    def store_factory(self) -> DeferredEventStoreFactory | None:
        """The installed persistence factory, or ``None`` (test seam)."""
        return self._store_factory

    def _store_for(self, *, namespace: str, name: str) -> DeferredEventStore | None:
        """Build the per-CR persistence adapter; ``None`` when unavailable.

        Every failure mode (no factory installed, factory raising) maps to
        ``None`` + a warning log — the deferral path runs from hardening
        call sites (boot checks, the status_store RMW) that must never
        crash because the persistence mirror is unavailable.
        """
        factory = self._store_factory
        if factory is None:
            return None
        try:
            return factory(namespace, name)
        except Exception:
            logger.warning(
                "deferred-event store construction failed for %s/%s; "
                "deferring in-memory only (issue #402 degradation to the "
                "#234 behavior)",
                namespace,
                name,
                exc_info=True,
            )
            return None

    def _persist(
        self, *, namespace: str, name: str, reason: str, message: str
    ) -> None:
        """Mirror one ACCEPTED deferral into ``status.deferredEvents`` (#402).

        Best-effort: a failure (apiserver unreachable, 409 budget
        exhausted, cap backstop) is logged and swallowed — the in-memory
        queue still holds the entry, so the only lost guarantee is
        crash-survivability for THIS entry, which is exactly the pre-#402
        behavior. Never called on the drop branch: a backpressure drop
        (``queue_full``) must not reach the persisted surface, or the
        ``WARNINGS_DEFERRED_DROPPED_TOTAL`` accounting and the persisted
        list would disagree.
        """
        store = self._store_for(namespace=namespace, name=name)
        if store is None:
            return
        try:
            store.append_deferred_event(
                {"namespace": namespace, "name": name, "reason": reason, "message": message},
                max_entries=MAX_DEFERRED_WARNING_EVENTS,
            )
        except Exception:
            logger.warning(
                "deferred-event persistence failed for %s/%s (reason=%s); "
                "the in-memory queue still holds the entry — it is only "
                "lost if the process dies before the next tick (issue #402 "
                "degradation to the #234 behavior)",
                namespace,
                name,
                reason,
                exc_info=True,
            )

    def defer_to_next_tick(
        self,
        *,
        namespace: str,
        name: str,
        reason: str,
        message: str,
    ) -> None:
        """Queue a Warning Event for the next OSCM watch tick on this CR.

        Counterpart to :meth:`flush_for`. Fires from non-callback
        contexts (boot, watchdog events on non-OSCM objects, the
        status_store RMW path). The actual ``kopf.event`` emit happens
        on the next OSCM watch event for this CR — kopf only permits
        emits from within an active callback, and the deferred context
        is not a callback.

        The three hardened call sites pass their own ``reason`` and
        ``message``; this method does not enforce which reasons are
        valid, so the public surface stays open to future hardening
        waves (the cap-eviction reason
        :data:`openstudio_operator.status_store.STATUS_MAP_CAPPED_EVENT`
        is the third user of this queue).

        Backpressure (issue #310): when
        ``len(self._queue) >= MAX_DEFERRED_WARNING_EVENTS`` this call is
        REJECTED — the deferred event is silently dropped
        (from the user's perspective) and
        ``WARNINGS_DEFERRED_DROPPED_TOTAL{reason="queue_full"}`` is
        incremented. The user-facing Warning Event is lost in that
        case; the /metrics counter is the only signal that the loss
        happened, so an SRE alerting on its rate catches the
        contract violation. The :data:`WARNINGS_DEFERRED_QUEUE_DEPTH`
        Gauge is updated to ``len(self._queue)`` on every call —
        whether the event was accepted or rejected — so a saturated
        gauge pairs with a nonzero drop counter.
        """
        if len(self._queue) >= MAX_DEFERRED_WARNING_EVENTS:
            metrics_module.WARNINGS_DEFERRED_DROPPED_TOTAL.labels(
                reason=DROP_REASON_QUEUE_FULL,
            ).inc()
        else:
            self._queue.append((namespace, name, reason, message))
            # Issue #402 — mirror the accepted entry into the CR
            # ``.status`` so a crash before the next tick does not
            # silently lose the Warning. Persisted only on the accept
            # branch, deliberately: a ``queue_full`` drop never touches
            # the persisted surface (the drop counter is the account of
            # record for backpressure losses, and its semantics are
            # frozen by #310's scope guard).
            self._persist(namespace=namespace, name=name, reason=reason, message=message)
        # Always reflect the post-call queue depth in the Gauge — the
        # Gauge is a state indicator (queue is at this depth right now),
        # not a delta of this call. On a drop the depth is unchanged
        # (still at the cap); on an accept it advances by 1.
        metrics_module.WARNINGS_DEFERRED_QUEUE_DEPTH.set(len(self._queue))

    def flush_for(self, *, namespace: str, name: str) -> int:
        """Drain every queued Warning Event for THIS CR; return count flushed.

        Fires once per OSCM watch event; idempotent because
        :meth:`defer_to_next_tick` only enqueues once per
        ``(namespace, name)`` in practice (the singleton guard's URL
        cache, the boot-time layout check's idempotent assertion, and
        the status_store's retry-stable post-eviction emit all dedupe).
        Returns ``0`` when nothing is queued for this CR — the drain
        is a no-op in that case.

        Issue #402 — the drain runs in two phases: the PERSISTED queue
        (``status.deferredEvents`` for this CR) first, then the
        in-memory queue. The persisted list is what survives an operator
        crash: a restarted process starts with an empty ``_queue`` but
        re-emits every persisted entry on its first watch tick. Entries
        emitted from the persisted phase deduplicate their in-memory
        twins (exact ``(namespace, name, reason, message)`` match), so
        the common dual-write path emits each Warning exactly once while
        the restart path re-emits it at least once. A failure to CLEAR
        the persisted list leaves it in place — the next tick re-emits
        it (at-least-once; a duplicate Kubernetes Event, never a silent
        loss).

        Each flushed entry becomes one :func:`kopf.event` call with
        ``type="Warning"`` and the recorded ``reason``/``message``.
        The handler module is responsible for installing the
        ``@kopf.on.event`` wrapper that invokes this method.

        Observability (issue #310): the
        :data:`WARNINGS_DEFERRED_QUEUE_DEPTH` Gauge is updated to the
        post-drain queue length on every flush — including no-op
        flushes, so the Gauge tracks the queue state continuously
        rather than oscillating between enqueue/defer ticks. When the
        queue holds entries ONLY for this CR (the typical case) the
        post-drain length is 0, which is the literal "reset to 0" the
        acceptance criterion calls for; multi-CR backlogs are reported
        faithfully (the residual length is the cross-CR deferred load).
        The Gauge's semantics are unchanged by #402: it still reports
        the in-memory queue depth only (the persisted mirror is crash
        insurance, not a second gauge source).
        """
        pending = [
            msg for msg in self._queue
            if msg[0] == namespace and msg[1] == name
        ]
        # Phase 1 — the persisted queue (issue #402). A fresh store is
        # built once and reused for the clear at the end of the phase.
        persisted: list[dict[str, str]] = []
        store = self._store_for(namespace=namespace, name=name)
        if store is not None:
            try:
                persisted = store.get_deferred_events()
            except Exception:
                logger.warning(
                    "deferred-event persistence read failed for %s/%s; "
                    "draining the in-memory queue only (issue #402 "
                    "degradation — persisted entries retry next tick)",
                    namespace,
                    name,
                    exc_info=True,
                )
                persisted = []
        if not pending and not persisted:
            # No-op flush still updates the Gauge — the queue state is
            # unchanged but the call site fired, so we want the gauge
            # to reflect the most recent observation regardless.
            metrics_module.WARNINGS_DEFERRED_QUEUE_DEPTH.set(len(self._queue))
            return 0
        emitted_keys: set[tuple[str, str, str, str]] = set()
        flushed = 0
        if persisted:
            for entry in persisted:
                reason = str(entry.get("reason") or "")
                message = str(entry.get("message") or "")
                kopf.event(
                    {"metadata": {"namespace": namespace, "name": name}},
                    type="Warning",
                    reason=reason,
                    message=message,
                )
                emitted_keys.add((namespace, name, reason, message))
                flushed += 1
            if store is not None:
                try:
                    store.clear_deferred_events()
                except Exception:
                    logger.warning(
                        "deferred-event persistence clear failed for %s/%s; "
                        "the persisted entries will re-emit on the next "
                        "tick (at-least-once, issue #402)",
                        namespace,
                        name,
                        exc_info=True,
                    )
        # Phase 2 — the in-memory queue, skipping tuples already emitted
        # from the persisted phase (the dual-write dedup: exactly-once in
        # the common path, at-least-once across restarts).
        fresh = [msg for msg in pending if msg not in emitted_keys]
        for _ns, _nm, reason, message in fresh:
            kopf.event(
                {"metadata": {"namespace": namespace, "name": name}},
                type="Warning",
                reason=reason,
                message=message,
            )
            flushed += 1
        self._queue[:] = [
            msg for msg in self._queue
            if not (msg[0] == namespace and msg[1] == name)
        ]
        # Update Gauge to the post-drain length. Single-CR queues read
        # 0 here (the literal "reset to 0" the issue calls for);
        # multi-CR backlogs report the residual so the on-call can see
        # the cross-CR load via a sustained nonzero value.
        metrics_module.WARNINGS_DEFERRED_QUEUE_DEPTH.set(len(self._queue))
        return flushed

    @property
    def queued(self) -> list[tuple[str, str, str, str]]:
        """Snapshot of the internal queue (read-only).

        Test seam: lets a test assert ``sink.queued`` directly without
        poking at the module-level state. Production code should use
        :meth:`defer_to_next_tick` and :meth:`flush_for`, never this
        property.
        """
        return list(self._queue)

    def clear(self) -> None:
        """Empty the queue (test seam).

        Production code MUST NOT call this — the queue is the
        mechanism that defers non-callback emits across tick boundaries,
        and a mid-flight clear would silently drop a Warning Event.
        """
        self._queue.clear()


# Module-level singleton — the handlers package installs one
# ``@kopf.on.event`` drain handler that calls ``_default_sink.flush_for``
# on every OSCM watch event, and every ``defer_to_next_tick`` call
# targets this same instance. Tests that need isolation can construct a
# fresh ``QueuedKopfEventSink()`` or call ``_default_sink.clear()``
# between cases.
_default_sink: Final[QueuedKopfEventSink] = QueuedKopfEventSink()


def get_default_sink() -> QueuedKopfEventSink:
    """Return the process-wide default sink.

    This is the function :mod:`openstudio_operator.handlers` calls at
    module load to register its single drain handler. Callers wanting
    their own queue (a future multi-sink architecture, a test harness)
    can construct a ``QueuedKopfEventSink()`` directly — the class
    is the contract, this helper is just the default instance.
    """
    return _default_sink


#: Public type alias for the ``status_store.set_event_sink`` seam. The
#: function signature is preserved verbatim so the call site in
#: ``handlers/__init__.py`` keeps working without changes to
#: ``status_store.py``. The sink's :meth:`defer_to_next_tick` method
#: matches this shape exactly (excluding the ``keyword-only`` ``reason``
#: / ``message`` kwargs — see :meth:`QueuedKopfEventSink.defer_to_next_tick`).
StatusEventSink = Callable[[str, str, str, str], None]