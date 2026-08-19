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
from collections.abc import Callable
from typing import Final

import kopf

logger = logging.getLogger(__name__)


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
    """

    __slots__ = ("_queue",)

    def __init__(self) -> None:
        """Initialize an empty queue. Idempotent and cheap; safe to re-create per test."""
        self._queue: list[tuple[str, str, str, str]] = []

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
        """
        self._queue.append((namespace, name, reason, message))

    def flush_for(self, *, namespace: str, name: str) -> int:
        """Drain every queued Warning Event for THIS CR; return count flushed.

        Fires once per OSCM watch event; idempotent because
        :meth:`defer_to_next_tick` only enqueues once per
        ``(namespace, name)`` in practice (the singleton guard's URL
        cache, the boot-time layout check's idempotent assertion, and
        the status_store's retry-stable post-eviction emit all dedupe).
        Returns ``0`` when nothing is queued for this CR — the drain
        is a no-op in that case.

        Each flushed entry becomes one :func:`kopf.event` call with
        ``type="Warning"`` and the recorded ``reason``/``message``.
        The handler module is responsible for installing the
        ``@kopf.on.event`` wrapper that invokes this method.
        """
        pending = [
            msg for msg in self._queue
            if msg[0] == namespace and msg[1] == name
        ]
        if not pending:
            return 0
        for _ns, _nm, reason, message in pending:
            kopf.event(
                {"metadata": {"namespace": namespace, "name": name}},
                type="Warning",
                reason=reason,
                message=message,
            )
        self._queue[:] = [
            msg for msg in self._queue
            if not (msg[0] == namespace and msg[1] == name)
        ]
        return len(pending)

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