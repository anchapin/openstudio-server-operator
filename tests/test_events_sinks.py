"""Issue #234 — consolidated ``QueuedKopfEventSink`` contract.

The handlers package used to carry three near-identical queue/drain
mechanisms side by side (the redis-URL guard #116, the redis-key-layout
guard #163, and the status-map-cap guard #171). This test pins the
acceptance criterion: one queue, one drain handler, one
``set_event_sink`` hook for the status_store cap path, AND the three
original Warning Event reasons (``RedisUrlEmpty``,
``RedisKeyLayoutDrift``, ``StatusMapCapped``) are still produced by the
handlers package.

Layers covered here:

1. Class contract — :class:`openstudio_operator.events_sinks.QueuedKopfEventSink`
   is a real class with a clean ``defer_to_next_tick`` / ``flush_for`` /
   ``queued`` / ``clear`` surface (the seam any future hardening will
   hang off).
2. Module-level singleton — ``get_default_sink()`` returns the
   process-wide instance the handlers package installs its single
   ``@kopf.on.event`` drain handler against.
3. Public Warning Event reasons — every reason introduced by the three
   pre-#234 hardening waves still appears in the operator's surfaced
   Warning Event inventory after the collapse. This is the CI gate the
   acceptance criterion names: "CI asserts the three Warning Event
   reasons (``RedisUrlEmpty``, ``RedisKeyLayoutDrift``,
   ``StatusMapCapped``) are still produced".
4. Drain handler registration — the single ``@kopf.on.event`` wrapper
   that drains the queue is registered in the kopf WatchingRegistry
   after the handlers package is imported. A maintainer who deletes the
   wrapper or accidentally re-introduces a duplicate drain must fail
   this gate at CI, not silently in production.

The per-reason behavior (e.g. that the URL guard defers one entry per
empty-redisUrl CR, that the cap-eviction path routes through
``status_store.set_event_sink``) is covered by the existing handler /
status_store tests; this file is the cross-cutting gate that all three
reasons still flow through the same plumbing.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

import kopf
import pytest

import openstudio_operator.events_sinks as sinks_module
from openstudio_operator import handlers as handlers_pkg
from openstudio_operator import metrics as metrics_module
from openstudio_operator import status_store
from openstudio_operator.events_sinks import QueuedKopfEventSink, get_default_sink
from openstudio_operator.handlers import redis_layout_check

# --- 1. Class contract ------------------------------------------------------


def test_queued_kopf_event_sink_class_is_exported() -> None:
    """Issue #234 acceptance: a ``QueuedKopfEventSink`` class is the public surface.

    ``openstudio_operator.events_sinks`` is the new home for the
    queue/drain mechanism — the class is importable, the helper
    function ``get_default_sink`` is the module-level singleton seam,
    and ``defer_to_next_tick`` / ``flush_for`` / ``queued`` / ``clear``
    are the four public methods any future hardening will hang off.
    """
    assert hasattr(sinks_module, "QueuedKopfEventSink")
    assert callable(QueuedKopfEventSink)
    sink = QueuedKopfEventSink()
    assert hasattr(sink, "defer_to_next_tick")
    assert hasattr(sink, "flush_for")
    assert hasattr(sink, "queued")
    assert hasattr(sink, "clear")
    assert sink.queued == []
    # Constructor starts empty; ``defer_to_next_tick`` enqueues;
    # ``queued`` is a snapshot (mutating it does not affect the sink).
    sink.defer_to_next_tick(
        namespace="ns", name="nm", reason="R", message="m",
    )
    snapshot = sink.queued
    assert snapshot == [("ns", "nm", "R", "m")]
    snapshot.clear()
    assert sink.queued == [("ns", "nm", "R", "m")], (
        "queued must be a snapshot, not a live view — otherwise test "
        "isolation breaks (one test's clear() would empty another's queue)."
    )
    sink.clear()
    assert sink.queued == []


def test_get_default_sink_returns_same_instance() -> None:
    """``get_default_sink()`` is the module-level singleton — one per process.

    The handlers package installs its single ``@kopf.on.event`` drain
    handler against the instance returned by this helper. Two callers
    MUST receive the same object — otherwise ``defer_to_next_tick`` from
    one site and ``flush_for`` from another would target different
    queues and the user-facing Warning Event would be silently dropped.
    """
    a = get_default_sink()
    b = get_default_sink()
    assert a is b, (
        "get_default_sink() must return the same instance on every call "
        "— the drain handler and the defer call sites target the same "
        "queue. See issue #234 — a future bug here would silently drop "
        "Warning Events from the wrong queue."
    )
    assert isinstance(a, QueuedKopfEventSink)


def test_defer_then_flush_emits_one_kopf_event_per_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sink's flush turns every queued entry into one ``kopf.event`` call.

    End-to-end contract: defer three entries for the same CR, flush for
    that CR, observe exactly three ``kopf.event`` calls with the right
    ``type``/``reason``/``message``/body. An empty queue returns ``0``
    from ``flush_for`` and emits nothing. This is the regression fence
    for the actual user-facing Warning Event emission.
    """
    sink = QueuedKopfEventSink()
    sink.defer_to_next_tick(
        namespace="ns-a", name="osc-a", reason="R1", message="m1",
    )
    sink.defer_to_next_tick(
        namespace="ns-a", name="osc-a", reason="R2", message="m2",
    )
    sink.defer_to_next_tick(
        namespace="ns-b", name="osc-b", reason="R3", message="m3",
    )

    emitted: list[dict[str, Any]] = []

    def _fake_event(*args: Any, **kwargs: Any) -> None:
        emitted.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(sinks_module.kopf, "event", _fake_event)

    # Flush for ns-a/osc-a — drains entries 1 and 2 only; entry 3
    # (ns-b/osc-b) stays queued for a later flush. The CR-scoped drain
    # is load-bearing: flushing every CR on every tick would emit
    # cross-CR Warnings to the wrong object.
    flushed = sink.flush_for(namespace="ns-a", name="osc-a")
    assert flushed == 2, (
        f"Expected 2 flushed entries for ns-a/osc-a; got {flushed}. See "
        f"issue #234 — the CR-scoped drain is the regression fence."
    )
    assert len(emitted) == 2
    for entry in emitted:
        assert entry["kwargs"]["type"] == "Warning"
        body = entry["args"][0]
        assert body == {"metadata": {"namespace": "ns-a", "name": "osc-a"}}
    reasons = sorted(e["kwargs"]["reason"] for e in emitted)
    assert reasons == ["R1", "R2"]
    messages = sorted(e["kwargs"]["message"] for e in emitted)
    assert messages == ["m1", "m2"]

    # The entry for ns-b/osc-b is still queued.
    assert sink.queued == [("ns-b", "osc-b", "R3", "m3")], (
        f"Cross-CR entry leaked across the drain boundary: "
        f"{sink.queued!r}. See issue #234 — CR scoping is the point."
    )

    # Empty flush is a no-op (returns 0, no events emitted).
    flushed = sink.flush_for(namespace="ns-a", name="osc-a")
    assert flushed == 0
    assert len(emitted) == 2


def test_flush_is_idempotent() -> None:
    """Two consecutive flushes do NOT double-emit the same Warning Event.

    The drain handler is invoked once per OSCM watch event — it must
    flush the queue and leave it empty. A second invocation that found
    the same queued entries and emitted them again would produce
    duplicate Kubernetes Events and confuse the user-facing signal.
    """
    sink = QueuedKopfEventSink()
    sink.defer_to_next_tick(
        namespace="ns", name="osc", reason="R", message="m",
    )
    # No kopf patch — ``flush_for`` calls ``kopf.event`` directly. We
    # accept the (test-time) side effect because the assertion is about
    # the queue state, not the event emission (covered above).
    import unittest.mock
    with unittest.mock.patch.object(sinks_module.kopf, "event"):
        sink.flush_for(namespace="ns", name="osc")
        assert sink.queued == []
        sink.flush_for(namespace="ns", name="osc")  # must be a no-op
        assert sink.queued == []


# --- 2. Handlers-package integration ----------------------------------------


def _ensure_metrics_stubbed() -> None:
    """Mirror of the same helper in test_singleton_registry_coverage.

    The handlers package's import-time side effects include
    ``start_metrics_server()`` — that opens a network socket on port
    9090 and is undesirable in CI. Stub it here so we can import the
    package without flakiness. Idempotent.
    """
    import openstudio_operator.metrics as metrics_mod

    if getattr(metrics_mod.start_metrics_server, "__wrapped_for_test__", False):
        return

    def _stub(*args: object, **kwargs: object) -> int:
        return 9090

    _stub.__wrapped_for_test__ = True  # type: ignore[attr-defined]
    metrics_mod.start_metrics_server = _stub  # type: ignore[assignment]


@pytest.fixture
def ensure_handlers_loaded():
    """Import the handlers package once per test session (idempotent).

    Importing :mod:`openstudio_operator.handlers` runs its module-level
    side effects: ``start_metrics_server()`` (stubbed by
    :func:`_ensure_metrics_stubbed`),
    :func:`openstudio_operator.status_store.set_event_sink` (installs
    the cap-eviction hook), and
    :func:`openstudio_operator.singleton.install_singleton_guard`
    (registers the spawning-handler wrapper). All three are idempotent,
    so re-importing in a second test is safe.
    """
    _ensure_metrics_stubbed()
    if "openstudio_operator.handlers" not in sys.modules:
        importlib.import_module("openstudio_operator.handlers")
    yield


def test_handlers_installs_shared_sink_on_module_load(
    ensure_handlers_loaded: None,
) -> None:
    """The handlers package installs ONE shared sink — ``handlers._sink``.

    After the import side effects, ``openstudio_operator.handlers._sink``
    must be the same instance as :func:`get_default_sink` — otherwise
    ``singleton.py`` (which defers to ``_emit_redis_warning_event``)
    and the status_store cap path would target different queues and
    the Warning Event would never reach kube-apiserver. This is the
    "one queue" half of the acceptance criterion.
    """
    assert handlers_pkg._sink is get_default_sink(), (
        "handlers._sink must be the same instance as get_default_sink() "
        "— otherwise the singleton URL guard, the key-layout check, and "
        "the status_store cap-eviction path defer into different queues "
        "and the Warning Event is silently dropped. See issue #234."
    )


def test_handlers_emit_redis_url_empty_via_singleton_path(
    ensure_handlers_loaded: None,
) -> None:
    """The redis-URL guard helper still defers a ``RedisUrlEmpty`` event.

    Pre-#234 this lived in ``_NOTIFY_QUEUE``; after #234 it routes
    through the shared sink. The test invokes the public helper
    (``handlers_pkg._emit_redis_warning_event``) and asserts the
    reason string is unchanged AND the entry lives in the shared
    queue — both invariants are load-bearing for the user-facing
    Kubernetes signal.
    """
    handlers_pkg._sink.clear()
    handlers_pkg._emit_redis_warning_event(
        namespace="ns", name="osc",
        message="spec.redisUrl is empty (issue #116).",
    )
    queued = handlers_pkg._sink.queued
    assert len(queued) == 1
    ns, nm, reason, message = queued[0]
    assert ns == "ns" and nm == "osc"
    assert reason == "RedisUrlEmpty", (
        f"Expected reason='RedisUrlEmpty' (preserved verbatim from #116); "
        f"got {reason!r}. See issue #234 — the CI gate for unchanged "
        f"Warning Event reasons."
    )
    assert "issue #116" in message


def test_handlers_emit_redis_key_layout_drift_via_helper(
    ensure_handlers_loaded: None,
) -> None:
    """The key-layout helper still defers a ``RedisKeyLayoutDrift`` event.

    Invokes :func:`openstudio_operator.handlers.redis_layout_check._emit_redis_key_layout_event`
    directly (the helper ``_check_redis_key_layout_for_cr`` calls into
    on the degraded branch — the layout-drift logic itself is
    exhaustively tested in ``tests/test_redis_client.py``; the helper
    moved to its own module in #584). Asserts the deferred event lands
    in the SAME shared sink the package drains (the new module binds
    the same ``get_default_sink()`` singleton as ``handlers_pkg._sink``).
    Pre-#234 this lived in ``_REDIS_KEY_LAYOUT_QUEUE``; after #234 it
    routes through the shared sink. The CI gate for unchanged reasons.
    """
    handlers_pkg._sink.clear()
    redis_layout_check._emit_redis_key_layout_event(
        "ns", "osc",
        "RedisKeyLayoutDrift",
        "synthetic layout drift (issue #234)",
    )
    queued = handlers_pkg._sink.queued
    assert len(queued) == 1
    ns, nm, reason, message = queued[0]
    assert ns == "ns" and nm == "osc"
    assert reason == "RedisKeyLayoutDrift", (
        f"Expected reason='RedisKeyLayoutDrift' (preserved verbatim from "
        f"#163); got {reason!r}. See issue #234 — the CI gate for "
        f"unchanged Warning Event reasons."
    )
    assert "issue #234" in message


def test_status_store_set_event_sink_routes_to_shared_sink(
    ensure_handlers_loaded: None,
) -> None:
    """``status_store.set_event_sink`` routes cap evictions through the shared sink.

    The handlers package installs a single sink with
    :func:`openstudio_operator.status_store.set_event_sink`; the cap
    eviction path inside ``StatusStore._set_map_entry`` calls that
    sink with ``reason="StatusMapCapped"``. Pre-#234 the sink was a
    standalone function that appended to ``_STATUS_MAP_CAP_QUEUE``;
    after #234 the sink is the shared queue and the ``StatusMapCapped``
    reason survives the round trip. This is the "one
    ``set_event_sink()`` for the status_store cap path" half of the
    acceptance criterion.
    """
    handlers_pkg._sink.clear()
    # Simulate the production cap-eviction emit by calling the
    # installed sink directly (the test in test_status_store.py asserts
    # the StatusStore calls it; here we assert the handler-installed
    # sink routes into the shared queue and preserves the reason).
    status_store._emit_status_map_event(  # type: ignore[attr-defined]
        "ns", "osc", status_store.STATUS_MAP_CAPPED_EVENT,
        "synthetic cap-eviction (issue #234)",
    )
    queued = handlers_pkg._sink.queued
    assert len(queued) == 1
    ns, nm, reason, message = queued[0]
    assert ns == "ns" and nm == "osc"
    assert reason == "StatusMapCapped", (
        f"Expected reason='StatusMapCapped' (preserved verbatim from "
        f"#171); got {reason!r}. See issue #234 — the CI gate for "
        f"unchanged Warning Event reasons."
    )
    assert "issue #234" in message


# --- 3. Consolidated drain handler registration ----------------------------


def test_consolidated_drain_handler_is_registered(
    ensure_handlers_loaded: None,
) -> None:
    """The single ``_drain_queued_warning_events`` handler is in kopf's registry.

    Pre-#234 the handlers package registered three separate
    ``@kopf.on.event`` drain handlers (``_drain_redis_warning_queue``,
    ``_drain_redis_key_layout_queue``,
    ``_drain_status_map_cap_queue``). After the refactor there is one:
    ``_drain_queued_warning_events``. A maintainer who deletes it (or
    accidentally re-introduces a duplicate drain) must fail this CI
    gate loudly — the user-facing Warning Event is silently dropped
    otherwise. Symmetric to the OSCM-spawning gate in
    ``tests/test_singleton_registry_coverage``.
    """
    registry = kopf.get_default_registry()
    watching = getattr(registry, "_watching", None)
    if watching is None:  # pragma: no cover — boundary check
        raise TypeError(
            "kopf internal structure changed: OperatorRegistry no longer "
            "has ``_watching``. The consolidated drain handler (#234) "
            "cannot be located without it."
        )
    handlers = getattr(watching, "_handlers", None)
    if not isinstance(handlers, list):  # pragma: no cover — boundary check
        raise TypeError(
            "kopf internal structure changed: WatchingRegistry._handlers "
            "is not a list. The consolidated drain handler (#234) cannot "
            "be located without it."
        )

    registered_ids = {getattr(h, "id", "?") for h in handlers}
    assert "_drain_queued_warning_events" in registered_ids, (
        f"Consolidated drain handler '_drain_queued_warning_events' is "
        f"NOT registered in the kopf watching registry. Registered: "
        f"{sorted(registered_ids)}. The Warning Event drain is missing — "
        f"the user-facing Kubernetes signal is silently dropped. See "
        f"issue #234 — the 'one drain handler' half of the acceptance "
        f"criterion is broken."
    )

    # Guard against accidental duplication: pre-#234 produced three
    # drain handlers. Post-#234 there is exactly one. Any extra
    # handler whose id matches the pre-#234 set must fail this gate
    # — that is the regression fence against a maintainer who adds a
    # fourth duplicate sink instead of routing through the shared one.
    legacy_ids = {
        "_drain_redis_warning_queue",
        "_drain_redis_key_layout_queue",
        "_drain_status_map_cap_queue",
    }
    leaked = legacy_ids & registered_ids
    assert not leaked, (
        f"Legacy drain handler(s) still registered after #234: "
        f"{sorted(leaked)}. Issue #234 collapsed the three queue/drain "
        f"mechanisms into one shared sink — any legacy handler still in "
        f"the registry means the collapse is incomplete and the same "
        f"Warning Event could be emitted twice on the same tick."
    )


def test_legacy_module_level_queues_are_removed() -> None:
    """Pre-#234 module-level queues are no longer exported on handlers.

    Pre-#234 the handlers package carried three module-level
    ``_NOTIFY_QUEUE`` / ``_REDIS_KEY_LAYOUT_QUEUE`` /
    ``_STATUS_MAP_CAP_QUEUE`` lists. After #234 the queues live on the
    ``QueuedKopfEventSink`` instance (``handlers_pkg._sink._queue``)
    and the module-level names are gone. This test pins the surface —
    if a maintainer re-introduces a module-level queue (likely as a
    parallel mechanism to bypass the new sink), CI fails at the
    import boundary, not at production runtime.
    """
    for legacy_name in (
        "_NOTIFY_QUEUE",
        "_REDIS_KEY_LAYOUT_QUEUE",
        "_STATUS_MAP_CAP_QUEUE",
    ):
        assert not hasattr(handlers_pkg, legacy_name), (
            f"Legacy module-level queue {legacy_name!r} is still exported "
            f"on openstudio_operator.handlers. Issue #234 collapsed the "
            f"three queue/drain mechanisms into one shared "
            f"QueuedKopfEventSink — a re-introduced module-level queue "
            f"means the collapse is incomplete and the deferred Warning "
            f"Events may bypass the consolidated drain."
        )


def test_legacy_drain_handlers_are_removed() -> None:
    """Pre-#234 drain-handler function names are no longer exported.

    Companion to :func:`test_legacy_module_level_queues_are_removed`:
    the old drain handlers (``_drain_redis_warning_queue`` etc.) must
    not exist as exported names on the handlers package either — a
    future maintainer who copy-pastes the old code would otherwise
    silently register a duplicate ``@kopf.on.event`` drain.
    """
    for legacy_name in (
        "_drain_redis_warning_queue",
        "_drain_redis_key_layout_queue",
        "_drain_status_map_cap_queue",
    ):
        assert not hasattr(handlers_pkg, legacy_name), (
            f"Legacy drain handler {legacy_name!r} is still exported on "
            f"openstudio_operator.handlers. Issue #234 collapsed the "
            f"three drain handlers into one shared "
            f"_drain_queued_warning_events — a re-introduced legacy "
            f"drain means the same Warning Event could be emitted twice."
        )


# --- 4. Issue #310 — backpressure cap + observability surface ----------------


def test_max_deferred_warning_events_constant_is_1000() -> None:
    """The :data:`MAX_DEFERRED_WARNING_EVENTS` constant is the published cap.

    Issue #310 acceptance criterion: the queue is capped at 1000
    entries. The constant must be module-level (not hidden in the
    class) so the test, the documentation, and any future
    reconfiguration surface import the same value the production code
    enforces — a typo in a magic number would silently weaken the cap
    without a CI signal.
    """
    assert sinks_module.MAX_DEFERRED_WARNING_EVENTS == 1000
    assert isinstance(sinks_module.MAX_DEFERRED_WARNING_EVENTS, int)


def test_defer_to_next_tick_sets_queue_depth_gauge() -> None:
    """Every :meth:`defer_to_next_tick` advances ``WARNINGS_DEFERRED_QUEUE_DEPTH``.

    The Gauge is the headline observability signal for #310 — an SRE
    alerting on its sustained value catches a stalled apiserver
    watch stream (the queue is growing because the drain handler is
    not firing). Driving the defer directly + reading the Gauge value
    is sufficient to verify the wiring; the actual production call
    sites are exhaustively tested in the handler-level tests.
    """
    sink = QueuedKopfEventSink()
    sink.clear()
    # Reset the Gauge so the assertion is local to this test (the
    # default REGISTRY is process-wide and other tests may have set it).
    metrics_module.WARNINGS_DEFERRED_QUEUE_DEPTH.set(0)
    for i in range(1, 6):
        sink.defer_to_next_tick(
            namespace="ns", name="osc", reason="R", message=f"m{i}",
        )
        assert metrics_module.WARNINGS_DEFERRED_QUEUE_DEPTH._value.get() == float(i)


def test_flush_for_resets_queue_depth_gauge_to_zero() -> None:
    """A successful :meth:`flush_for` resets ``WARNINGS_DEFERRED_QUEUE_DEPTH``.

    Acceptance criterion: "reset to 0 on every ``flush_for``". In the
    single-CR case (every queued entry belongs to the flushed CR)
    the post-drain queue is empty so the Gauge reads 0 — the literal
    "reset to 0" the issue calls for. Multi-CR backlogs are reported
    faithfully in the companion test below.
    """
    sink = QueuedKopfEventSink()
    sink.clear()
    metrics_module.WARNINGS_DEFERRED_QUEUE_DEPTH.set(0)
    for i in range(3):
        sink.defer_to_next_tick(
            namespace="ns", name="osc", reason="R", message=f"m{i}",
        )
    # Stub kopf.event so the flush emits without a real apiserver.
    import unittest.mock
    with unittest.mock.patch.object(sinks_module.kopf, "event"):
        flushed = sink.flush_for(namespace="ns", name="osc")
    assert flushed == 3
    assert sink.queued == []
    assert metrics_module.WARNINGS_DEFERRED_QUEUE_DEPTH._value.get() == 0.0


def test_flush_for_reports_residual_queue_depth_for_other_crs() -> None:
    """A flush that drains one CR still updates the Gauge to the residual depth.

    The queue is process-wide and shared across all CRs — when a
    flush drains CR-A the queue may still hold entries for CR-B. The
    Gauge must report the residual faithfully (the cross-CR deferred
    load is observable, not hidden by a "reset to 0" that would be
    a lie). This is the same code path as the single-CR case; the
    test pins the multi-CR behavior so a future maintainer who
    hard-codes ``.set(0)`` after every flush is caught at CI.
    """
    sink = QueuedKopfEventSink()
    sink.clear()
    metrics_module.WARNINGS_DEFERRED_QUEUE_DEPTH.set(0)
    for i in range(2):
        sink.defer_to_next_tick(
            namespace="ns", name="osc-a", reason="R", message=f"a{i}",
        )
    for i in range(3):
        sink.defer_to_next_tick(
            namespace="ns", name="osc-b", reason="R", message=f"b{i}",
        )
    # Gauge reads 5 (the combined queue) before the flush.
    assert metrics_module.WARNINGS_DEFERRED_QUEUE_DEPTH._value.get() == 5.0
    import unittest.mock
    with unittest.mock.patch.object(sinks_module.kopf, "event"):
        flushed = sink.flush_for(namespace="ns", name="osc-a")
    assert flushed == 2
    # Residual queue still has the 3 osc-b entries — Gauge must read 3,
    # NOT 0 (a "reset to 0" would mask the cross-CR backlog).
    assert sink.queued == [
        ("ns", "osc-b", "R", "b0"),
        ("ns", "osc-b", "R", "b1"),
        ("ns", "osc-b", "R", "b2"),
    ]
    assert metrics_module.WARNINGS_DEFERRED_QUEUE_DEPTH._value.get() == 3.0


def test_queue_cap_drops_excess_deferrals_and_increments_drop_counter() -> None:
    """Issue #310 acceptance criterion (b): the 1001st deferral is dropped.

    Drives 1001 deferrals against a fresh sink. Asserts:

    * The 1000th deferral succeeds (the queue holds exactly 1000).
    * The 1001st deferral is REJECTED — the queue stays at 1000, the
      user-facing Warning Event is silently lost (the only signal is
      the /metrics Counter).
    * ``WARNINGS_DEFERRED_DROPPED_TOTAL{reason="queue_full"}`` has
      incremented by exactly 1.
    * ``WARNINGS_DEFERRED_QUEUE_DEPTH`` reads 1000 (saturated at the
      cap) — the drop does NOT advance the Gauge (the queue length
      is unchanged; the Gauge is a state indicator, not a delta of
      this call).

    This is the regression fence for the silent-drop failure mode
    the issue calls out: without the cap + counter the queue would
    grow unbounded on a stalled apiserver watch stream and the only
    operator-visible signal would be OOM.
    """
    sink = QueuedKopfEventSink()
    sink.clear()
    metrics_module.WARNINGS_DEFERRED_QUEUE_DEPTH.set(0)

    counter = metrics_module.WARNINGS_DEFERRED_DROPPED_TOTAL

    def _total_for_reason(reason: str) -> float:
        # ``counter._metrics`` is a dict keyed by a tuple of label values
        # in the order declared by ``counter._labelnames`` — for our
        # single-label counter ``('reason',)`` the first element is the
        # reason string. ``prometheus_client`` 0.26.x exposes
        # ``_value.get()`` on each per-series sample.
        per_series = getattr(counter, "_metrics", {})
        label_names = getattr(counter, "_labelnames", ())
        if "reason" not in label_names:
            return 0.0
        reason_idx = label_names.index("reason")
        total = 0.0
        for label_values, snapshot in per_series.items():
            if reason_idx < len(label_values) and label_values[reason_idx] == reason:
                value_obj = getattr(snapshot, "_value", None)
                if value_obj is not None and hasattr(value_obj, "get"):
                    total += float(value_obj.get())
        return total

    baseline = _total_for_reason(sinks_module.DROP_REASON_QUEUE_FULL)

    # Drive 1000 deferrals — every one is accepted, queue saturates at 1000.
    for i in range(sinks_module.MAX_DEFERRED_WARNING_EVENTS):
        sink.defer_to_next_tick(
            namespace="ns", name="osc", reason="R", message=f"m{i}",
        )
    assert len(sink.queued) == sinks_module.MAX_DEFERRED_WARNING_EVENTS
    assert (
        metrics_module.WARNINGS_DEFERRED_QUEUE_DEPTH._value.get()
        == float(sinks_module.MAX_DEFERRED_WARNING_EVENTS)
    )
    # No drops yet — the 1000 deferrals all fit.
    assert _total_for_reason(sinks_module.DROP_REASON_QUEUE_FULL) == baseline

    # The 1001st deferral must be REJECTED.
    sink.defer_to_next_tick(
        namespace="ns", name="osc", reason="R", message="m-overflow",
    )
    # Queue length is unchanged (still 1000), the rejected entry is
    # silently dropped — the user-facing Warning Event is lost.
    assert len(sink.queued) == sinks_module.MAX_DEFERRED_WARNING_EVENTS
    assert sink.queued[-1] == ("ns", "osc", "R", "m999"), (
        "The 1001st deferral was appended instead of dropped. The "
        "backpressure cap (issue #310) is not enforced. See the "
        "silent-drop failure mode the issue calls out."
    )
    # Gauge plateaus at the cap — the drop does NOT advance it.
    assert (
        metrics_module.WARNINGS_DEFERRED_QUEUE_DEPTH._value.get()
        == float(sinks_module.MAX_DEFERRED_WARNING_EVENTS)
    )
    # The drop Counter increments by exactly 1 — the loss is observable.
    assert (
        _total_for_reason(sinks_module.DROP_REASON_QUEUE_FULL)
        == baseline + 1
    )


def test_queue_cap_drops_subsequent_deferrals_until_drained() -> None:
    """After a drop, further deferrals stay dropped until the queue drains.

    The cap is enforced continuously — once the queue is saturated,
    every subsequent deferral is dropped (and the Counter increments
    on each one). Only a :meth:`flush_for` that empties the queue
    re-opens the door. Pinning this here so a future maintainer who
    treats the cap as a one-shot trigger (e.g. only the first drop
    is counted) is caught at CI rather than silently dropping
    every other event after the initial hit.
    """
    sink = QueuedKopfEventSink()
    sink.clear()
    metrics_module.WARNINGS_DEFERRED_QUEUE_DEPTH.set(0)
    counter = metrics_module.WARNINGS_DEFERRED_DROPPED_TOTAL

    def _total_for_reason(reason: str) -> float:
        # See the identical helper in
        # ``test_queue_cap_drops_excess_deferrals_and_increments_drop_counter``
        # for the rationale — ``_metrics`` keys are tuples of label
        # values in ``_labelnames`` order.
        per_series = getattr(counter, "_metrics", {})
        label_names = getattr(counter, "_labelnames", ())
        if "reason" not in label_names:
            return 0.0
        reason_idx = label_names.index("reason")
        total = 0.0
        for label_values, snapshot in per_series.items():
            if reason_idx < len(label_values) and label_values[reason_idx] == reason:
                value_obj = getattr(snapshot, "_value", None)
                if value_obj is not None and hasattr(value_obj, "get"):
                    total += float(value_obj.get())
        return total

    baseline = _total_for_reason(sinks_module.DROP_REASON_QUEUE_FULL)
    cap = sinks_module.MAX_DEFERRED_WARNING_EVENTS

    # Saturate + drop 5 more.
    for i in range(cap + 5):
        sink.defer_to_next_tick(
            namespace="ns", name="osc", reason="R", message=f"m{i}",
        )
    assert len(sink.queued) == cap
    # 5 deferrals were dropped (i in 1000..1004 inclusive; the 1000th
    # is accepted, the next 5 are dropped).
    assert (
        _total_for_reason(sinks_module.DROP_REASON_QUEUE_FULL)
        == baseline + 5
    )

    # Drain via a stubbed kopf.event (the sink uses kopf directly).
    import unittest.mock
    with unittest.mock.patch.object(sinks_module.kopf, "event"):
        flushed = sink.flush_for(namespace="ns", name="osc")
    assert flushed == cap
    assert sink.queued == []

    # Re-fill to the cap; further deferrals drop again.
    for i in range(cap + 2):
        sink.defer_to_next_tick(
            namespace="ns", name="osc", reason="R", message=f"n{i}",
        )
    assert len(sink.queued) == cap
    assert (
        _total_for_reason(sinks_module.DROP_REASON_QUEUE_FULL)
        == baseline + 5 + 2
    )


# --- 5. Issue #402 — deferred-queue persistence across restarts ----------------


class _FakeDeferredEventStore:
    """In-memory :class:`DeferredEventStore` (the Protocol #402 persistence uses).

    Mirrors the ``StatusStore`` surface the sink calls — one shared list
    per CR stands in for ``status.deferredEvents``. Failure injection:
    ``fail_appends`` / ``fail_clears`` / ``fail_gets`` raise from the
    corresponding method so the degradation paths (best-effort
    persistence, at-least-once re-drain) are testable without a fake
    apiserver.
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, str]] = []
        self.append_calls: list[dict[str, str]] = []
        self.clear_calls = 0
        self.fail_appends = False
        self.fail_clears = False
        self.fail_gets = False

    def get_deferred_events(self) -> list[dict[str, str]]:
        if self.fail_gets:
            raise RuntimeError("synthetic get failure (issue #402 test)")
        return [dict(e) for e in self.entries]

    def append_deferred_event(
        self, entry: dict[str, str], *, max_entries: int | None = None
    ) -> None:
        self.append_calls.append(dict(entry))
        if self.fail_appends:
            raise RuntimeError("synthetic append failure (issue #402 test)")
        if max_entries is not None and len(self.entries) >= max_entries:
            return
        self.entries.append(dict(entry))

    def clear_deferred_events(self) -> None:
        self.clear_calls += 1
        if self.fail_clears:
            raise RuntimeError("synthetic clear failure (issue #402 test)")
        self.entries.clear()


_ENTRY = {
    "namespace": "ns",
    "name": "osc",
    "reason": "StatusMapCapped",
    "message": "status.softStops size capped at 10000 (issue #171)",
}


def test_defer_persists_accepted_entry_to_status() -> None:
    """Every ACCEPTED deferral is mirrored into the persistence store (#402).

    The persisted copy is what survives a crash: without the mirror, a
    process death between defer and the next tick silently dropped the
    Warning with no ``queue_full`` drop to observe. The append carries
    ``max_entries=MAX_DEFERRED_WARNING_EVENTS`` — the persisted twin of
    the in-memory cap — so the ``.status`` array can never outgrow the
    queue's own bound.
    """
    store = _FakeDeferredEventStore()
    sink = QueuedKopfEventSink(store_factory=lambda ns, nm: store)
    sink.defer_to_next_tick(
        namespace="ns", name="osc", reason="StatusMapCapped",
        message="status.softStops size capped at 10000 (issue #171)",
    )
    assert store.entries == [_ENTRY]
    assert len(store.append_calls) == 1
    # The in-memory queue is untouched by the persistence layer — the
    # Gauge semantics (in-memory depth) are unchanged by #402.
    assert sink.queued == [("ns", "osc", "StatusMapCapped", _ENTRY["message"])]


def test_flush_drains_persisted_first_and_dedupes_in_memory_twin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dual-write common path emits each Warning EXACTLY once (#402).

    A defer that succeeded in BOTH stores (memory + ``.status``) must not
    double-emit on flush: the persisted phase runs first and its emitted
    tuples suppress their in-memory twins. The persisted list is cleared
    after the successful drain.
    """
    store = _FakeDeferredEventStore()
    sink = QueuedKopfEventSink(store_factory=lambda ns, nm: store)
    sink.defer_to_next_tick(
        namespace="ns", name="osc", reason="StatusMapCapped",
        message=_ENTRY["message"],
    )
    assert store.entries and sink.queued  # dual-write precondition

    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sinks_module.kopf, "event",
        lambda *a, **k: emitted.append({"args": a, "kwargs": k}),
    )

    flushed = sink.flush_for(namespace="ns", name="osc")
    assert flushed == 1, (
        f"Dual-written entry emitted {flushed} times; expected exactly 1 "
        f"(persisted-first drain + in-memory dedup). See issue #402."
    )
    assert len(emitted) == 1
    assert emitted[0]["kwargs"]["reason"] == "StatusMapCapped"
    assert emitted[0]["kwargs"]["type"] == "Warning"
    assert emitted[0]["args"][0] == {"metadata": {"namespace": "ns", "name": "osc"}}
    assert store.entries == [], "persisted list must be cleared after the drain"
    assert store.clear_calls == 1
    assert sink.queued == []


def test_restart_re_emits_persisted_entries_on_fresh_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #402 acceptance criterion: entries queued before a crash re-emit.

    "Restart" is simulated the unit-test-friendly way: sink A defers
    (dual-write to the shared persisted store), then the process dies —
    modeled by discarding sink A entirely (its in-memory queue is gone;
    only ``status.deferredEvents`` survives). Sink B is a FRESH sink
    (empty in-memory queue) wired to the same persisted store, exactly
    what a relaunched operator process constructs. ``flush_for`` on sink
    B — fired by the first OSCM watch tick after restart — must re-emit
    the persisted Warning and clear it.
    """
    store = _FakeDeferredEventStore()
    sink_a = QueuedKopfEventSink(store_factory=lambda ns, nm: store)
    sink_a.defer_to_next_tick(
        namespace="ns", name="osc", reason="RedisUrlEmpty",
        message="spec.redisUrl is empty (issue #116).",
    )
    # ... operator process crashes here; in-memory queue is lost ...
    sink_b = QueuedKopfEventSink(store_factory=lambda ns, nm: store)
    assert sink_b.queued == [], "fresh process starts with an empty in-memory queue"
    assert store.entries, "the persisted mirror survived the crash"

    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sinks_module.kopf, "event",
        lambda *a, **k: emitted.append({"args": a, "kwargs": k}),
    )

    flushed = sink_b.flush_for(namespace="ns", name="osc")
    assert flushed == 1, (
        f"Restart recovery flushed {flushed} entries; expected 1. The "
        f"at-least-once contract (#234/#310/#402) is broken — a crashed "
        f"operator silently dropped the queued Warning."
    )
    assert len(emitted) == 1
    assert emitted[0]["kwargs"]["reason"] == "RedisUrlEmpty"
    assert emitted[0]["kwargs"]["message"] == "spec.redisUrl is empty (issue #116)."
    assert emitted[0]["kwargs"]["type"] == "Warning"
    assert emitted[0]["args"][0] == {"metadata": {"namespace": "ns", "name": "osc"}}
    assert store.entries == [], "recovery drain must clear the persisted list"
    assert sink_b.queued == []

    # Second flush is a no-op — the recovery is not a re-emit loop.
    assert sink_b.flush_for(namespace="ns", name="osc") == 0
    assert len(emitted) == 1


def test_persist_append_failure_degrades_to_in_memory_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing persistence write must not break the deferral itself (#402).

    Persistence is best-effort by contract: on an append failure (apiserver
    unreachable, 409 budget exhausted) the entry still lives in the
    in-memory queue and flushes normally — the pre-#402 behavior. The
    only lost guarantee is crash-survivability for that one entry.
    """
    store = _FakeDeferredEventStore()
    store.fail_appends = True
    sink = QueuedKopfEventSink(store_factory=lambda ns, nm: store)
    sink.defer_to_next_tick(
        namespace="ns", name="osc", reason="R", message="m",
    )
    assert sink.queued == [("ns", "osc", "R", "m")]
    assert store.entries == []

    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sinks_module.kopf, "event",
        lambda *a, **k: emitted.append({"args": a, "kwargs": k}),
    )
    assert sink.flush_for(namespace="ns", name="osc") == 1
    assert len(emitted) == 1
    assert sink.queued == []


def test_persisted_clear_failure_re_emits_next_tick_at_least_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed persisted-clear leaves the entries for the NEXT tick (#402).

    If the emit succeeded but the ``.status`` clear failed (409 budget,
    transient apiserver error), the persisted entries stay put and
    re-emit on the next flush — at-least-once semantics: a possible
    duplicate Kubernetes Event, never a silent loss. The in-memory twins
    were deduped and drained, so the next tick's emission comes from the
    persisted list alone.
    """
    store = _FakeDeferredEventStore()
    store.fail_clears = True
    sink = QueuedKopfEventSink(store_factory=lambda ns, nm: store)
    sink.defer_to_next_tick(
        namespace="ns", name="osc", reason="R", message="m",
    )

    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sinks_module.kopf, "event",
        lambda *a, **k: emitted.append({"args": a, "kwargs": k}),
    )

    assert sink.flush_for(namespace="ns", name="osc") == 1
    assert sink.queued == []
    assert store.entries, "clear failed — the persisted entries must survive"
    # Next tick: the persisted entry re-emits (at-least-once duplicate).
    assert sink.flush_for(namespace="ns", name="osc") == 1
    assert len(emitted) == 2


def test_persisted_read_failure_drains_in_memory_anyway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing persistence READ skips phase 1 and still drains phase 2 (#402).

    The drain must never die because the persisted surface is
    unreadable — the in-memory queue drains as if persistence were not
    installed (pre-#402 behavior), and the persisted entries retry on
    the next tick.
    """
    store = _FakeDeferredEventStore()
    store.fail_gets = True
    sink = QueuedKopfEventSink(store_factory=lambda ns, nm: store)
    sink.defer_to_next_tick(
        namespace="ns", name="osc", reason="R", message="m",
    )

    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sinks_module.kopf, "event",
        lambda *a, **k: emitted.append({"args": a, "kwargs": k}),
    )
    assert sink.flush_for(namespace="ns", name="osc") == 1
    assert len(emitted) == 1
    assert sink.queued == []


def test_store_factory_failure_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A factory that RAISES maps to in-memory-only, never a crash (#402).

    The factory runs inside hardening call sites (boot checks, the
    status_store RMW path); a raising factory must degrade to the
    pre-#402 behavior, not break the Warning deferral itself.
    """
    def _broken_factory(ns: str, nm: str) -> object:
        raise RuntimeError("synthetic factory failure (issue #402 test)")

    sink = QueuedKopfEventSink(store_factory=_broken_factory)
    sink.defer_to_next_tick(
        namespace="ns", name="osc", reason="R", message="m",
    )
    assert sink.queued == [("ns", "osc", "R", "m")]

    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sinks_module.kopf, "event",
        lambda *a, **k: emitted.append({"args": a, "kwargs": k}),
    )
    assert sink.flush_for(namespace="ns", name="osc") == 1
    assert len(emitted) == 1


def test_backpressure_drop_never_reaches_the_persisted_surface() -> None:
    """A ``queue_full`` drop is NOT mirrored into ``.status`` (#402 scope guard).

    Issue #310's semantics are frozen: ``WARNINGS_DEFERRED_DROPPED_TOTAL``
    is the account of record for backpressure losses. If the drop branch
    also appended to the persisted list, the ``.status`` array would
    disagree with the in-memory queue (and the counter) after every
    drop — the persisted surface tracks ACCEPTED deferrals only.
    """
    store = _FakeDeferredEventStore()
    sink = QueuedKopfEventSink(store_factory=lambda ns, nm: store)
    cap = sinks_module.MAX_DEFERRED_WARNING_EVENTS
    for i in range(cap):
        sink.defer_to_next_tick(
            namespace="ns", name="osc", reason="R", message=f"m{i}",
        )
    assert len(store.entries) == cap, "every ACCEPTED deferral is mirrored"
    # The 1001st deferral is dropped — the persisted list must not grow.
    sink.defer_to_next_tick(
        namespace="ns", name="osc", reason="R", message="m-overflow",
    )
    assert len(sink.queued) == cap
    assert len(store.entries) == cap, (
        "A queue_full drop was mirrored into the persisted surface — "
        "violates the #402 scope guard (drop semantics frozen by #310)."
    )
    assert len(store.append_calls) == cap


def test_handlers_install_deferred_event_persistence_at_startup(
    ensure_handlers_loaded: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The startup handler arms persistence on the shared sink (#402).

    The factory is installed from ``@kopf.on.startup`` — NOT at module
    import — so test/library imports of the handlers package never
    build a ``CustomObjectsApi`` against the host's kubeconfig. The test
    invokes the startup handler directly, asserts the shared sink now
    carries the factory (and that the factory builds a ``StatusStore``
    over the operator's single api-construction point), then DISARMS it
    again — leaving the default sink in-memory-only for the rest of the
    suite, exactly as a plain import leaves it.
    """
    import openstudio_operator.singleton as singleton_mod

    built_over: list[object] = []

    def _fake_operator_api() -> object:
        sentinel = object()
        built_over.append(sentinel)
        return sentinel

    monkeypatch.setattr(singleton_mod, "operator_custom_objects_api", _fake_operator_api)

    # Precondition: a plain import leaves the default sink in-memory-only.
    assert get_default_sink().store_factory is None

    try:
        import logging as _logging

        handlers_pkg._install_deferred_event_persistence(_logging.getLogger("test"))
        assert get_default_sink().store_factory is handlers_pkg._deferred_event_store, (
            "The startup handler must install the StatusStore-backed "
            "factory on the SHARED default sink (issue #402)."
        )
        store = handlers_pkg._deferred_event_store("ns", "osc")
        assert isinstance(store, status_store.StatusStore)
        assert built_over, (
            "The factory must source its client from the single "
            "CustomObjectsApi construction point (issue #158)."
        )
    finally:
        get_default_sink().set_store_factory(None)