"""Issue #164 — ``openstudio_operator.events.EventEmitter`` contract.

The class lives at ``openstudio_operator/events.py`` (single source of
truth, moved out of the ``analysis_sla`` accidental hub). These tests
pin its public surface: dry-run gating + suppressed counter, and a
clean import path. The other handler-level tests (analysis_sla,
datapoint_watchdog, worker_recycler, web_background_monitor) cover the
class's integration with the timer wrappers — they pass plain
callable closures and rely on the ``__call__`` shim, which is the
backwards-compatible path.
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest
from kubernetes.client import ApiException

import openstudio_operator.events as events_module
from openstudio_operator.events import EventEmitter
from openstudio_operator.metrics import (
    EVENTS_EMIT_FAILURES_TOTAL,
    EVENTS_EMITTED_TOTAL,
)

#: Issue #255 / #299 — the OSCM warning-event reason vocabulary that the
#: failure counter (and the dry-run / emitted companion counters) share.
#: Same seven values as ``tests/test_metrics_endpoint.py::test_events_*_per_reason``
#: — pinning the cardinality here too so a drift between this module and
#: the metric-family tests fails CI loudly.
_REASON_VOCABULARY = (
    "AnalysisSoftStopped",
    "AnalysisEscalated",
    "DatapointRequeued",
    "DatapointRequeueExhausted",
    "WorkerRecycled",
    "WebBackgroundRestarted",
    "ResqueKeyLayoutUnknown",
)


@pytest.fixture
def body() -> dict:
    """Minimal OSCM body shape — kopf passes this to every timer/event callback.

    Only the parts kopf.event reads (the embedded ``metadata`` used to
    attach the Event to the right CR) matter for these tests.
    """
    return {
        "metadata": {"namespace": "openstudio-server", "name": "test-oscm"},
        "spec": {},
    }


def test_emit_emits_warning_event_when_dry_run_false(body):
    """Issue #164 acceptance: ``dryRun=False`` → ``kopf.event`` is called.

    With ``dry_run=False`` the class is a transparent wrapper around
    :func:`kopf.event` — every ``emit(type, reason, message)`` call
    translates into one ``kopf.event(body, type=..., reason=...,
    message=...)`` call with the bound body. The suppressed counter
    stays at zero (no dry-run gate tripped).
    """
    emitter = EventEmitter(body=body, dry_run=False)

    with patch.object(events_module, "kopf") as mock_kopf:
        emitter.emit("Warning", "Reason", "message")

    mock_kopf.event.assert_called_once_with(
        body, type="Warning", reason="Reason", message="message"
    )
    assert emitter.suppressed_count == 0
    assert emitter.dry_run is False


def test_emit_no_event_when_dry_run_true_but_counter_increments(body, caplog):
    """Issue #164 acceptance: ``dryRun=True`` → no Event, counter increments.

    The class's whole point: the dry-run gate (D11) lives at the
    :func:`kopf.event` chokepoint, so callers can fire ``emit`` exactly
    as they would in a real run and never accidentally post Events in
    dry-run mode. Suppressions are observable via
    :attr:`EventEmitter.suppressed_count` (no log scraping required),
    and a single INFO log line names the type/reason/message for
    debugging.
    """
    emitter = EventEmitter(body=body, dry_run=True)

    with patch.object(events_module, "kopf") as mock_kopf, caplog.at_level("INFO"):
        emitter.emit("Warning", "Reason", "first message")
        emitter.emit("Warning", "Reason", "second message")

    mock_kopf.event.assert_not_called()
    assert emitter.suppressed_count == 2
    assert emitter.dry_run is True

    # Two INFO records, one per suppressed emit, carrying the
    # type/reason/message so log-only observability still works.
    suppressed_logs = [
        record for record in caplog.records
        if "dry-run suppressed" in record.getMessage()
    ]
    assert len(suppressed_logs) == 2
    assert "Reason" in suppressed_logs[0].getMessage()
    assert "first message" in suppressed_logs[0].getMessage()
    assert "second message" in suppressed_logs[1].getMessage()


def test_event_emitter_imports_clean():
    """Issue #164 acceptance: ``EventEmitter`` is importable from the new module.

    Single source of truth — the public surface lives at
    ``openstudio_operator.events``, not at
    ``openstudio_operator.handlers.analysis_sla``. Pinning the import
    path here so a future rename gets caught before the wave merges.
    """
    mod = importlib.import_module("openstudio_operator.events")
    assert hasattr(mod, "EventEmitter")
    assert mod.EventEmitter is EventEmitter
    # ``__call__`` shim must exist so the ``emit("Warning", REASON,
    # message)`` syntax in handler call sites keeps working.
    assert callable(EventEmitter.__call__)
    assert callable(EventEmitter.emit)


@pytest.mark.parametrize("reason", _REASON_VOCABULARY)
def test_emit_kopf_event_failure_increments_failure_counter_and_reraises(reason, body):
    """Issue #299 (tracks #255) — ``kopf.event`` failure path is observable.

    The dedicated ``EVENTS_EMIT_FAILURES_TOTAL{reason}`` counter must
    increment by exactly 1 when ``kopf.event`` raises an ``ApiException``,
    AND the same ``ApiException`` must propagate unchanged to the caller
    (so the handler wrapper's ``error_type`` label can still classify it
    via ``handler_tick_failures_total`` — see the comment block in
    ``src/openstudio_operator/events.py:139-155``).

    Why this matters vs. project goals: AGENTS.md cites the canonical
    16-counter invariant; without an integration test pinning this
    contract, a future refactor that catches the exception but forgets
    to ``.inc()`` the failure counter would ship silently (the bare
    ``.inc()`` call at ``tests/test_metrics_endpoint.py:524-547`` only
    exercises the metric-family machinery, not ``EventEmitter.emit``).

    Pinning ``EVENTS_EMITTED_TOTAL is unchanged`` enforces the metric's
    documented semantic ("successful kopf.event calls") — a failed post
    never produced an Event, so the emitted counter must stay flat. A
    regression that re-introduces a pre-try ``.inc()`` would double-count
    (emitted AND failure incremented for the same failed post), breaking
    the ``emitted`` vs ``failures`` rate pair #237 documents as the
    headline observability surface.

    Parametrising over the seven-reason vocabulary pins the per-reason
    label cardinality of ``EVENTS_EMIT_FAILURES_TOTAL`` from the
    ``EventEmitter`` integration end — complementing the metric-family
    tests' exposition shape probes at the metric module surface.
    """
    failures_before = EVENTS_EMIT_FAILURES_TOTAL.labels(
        namespace="openstudio-server", name="test-oscm", reason=reason
    )._value.get()
    # Sanity baseline: a reason that this test will NOT touch. The
    # failure path must only bump the specific ``reason`` series, not
    # every label series of the counter.
    failures_other_before = EVENTS_EMIT_FAILURES_TOTAL.labels(
        namespace="openstudio-server", name="test-oscm", reason="__sanity_other__"
    )._value.get()
    # EVENTS_EMITTED_TOTAL must be untouched by the failure code path
    # (the metric documents "successful kopf.event calls" — a failure
    # never produced a posted Event). Pinning this prevents a future
    # refactor from double-counting by bumping both counters.
    emitted_same_before = EVENTS_EMITTED_TOTAL.labels(
        namespace="openstudio-server", name="test-oscm", reason=reason
    )._value.get()
    emitted_other_before = EVENTS_EMITTED_TOTAL.labels(
        namespace="openstudio-server", name="test-oscm", reason="__sanity_other__"
    )._value.get()

    emitter = EventEmitter(body=body, dry_run=False)
    with patch.object(events_module, "kopf") as mock_kopf:
        mock_kopf.event.side_effect = ApiException(
            status=503, reason="apiserver down"
        )
        with pytest.raises(ApiException) as exc_info:
            emitter.emit("Warning", reason, "message")

    failures_after = EVENTS_EMIT_FAILURES_TOTAL.labels(
        namespace="openstudio-server", name="test-oscm", reason=reason
    )._value.get()
    assert failures_after - failures_before == 1.0

    # Same ApiException propagates unchanged — status and reason
    # attributes survive the re-raise so the caller can still introspect
    # (the handler wrapper's ``error_type=ApiException`` branch uses the
    # same exception object via ``exc_info``).
    assert exc_info.value.status == 503
    assert exc_info.value.reason == "apiserver down"

    # Cardinality pin: only the targeted ``reason`` series moves.
    failures_other_after = EVENTS_EMIT_FAILURES_TOTAL.labels(
        namespace="openstudio-server", name="test-oscm", reason="__sanity_other__"
    )._value.get()
    assert failures_other_after == failures_other_before

    # EVENTS_EMITTED_TOTAL is unchanged: a failed ``kopf.event`` post
    # never produced an Event, so the emitted counter must not move.
    # Probed for both the exercised reason (must stay flat) and a
    # sentinel reason (must also stay flat — guards against any
    # spurious series bumps from the failure-handling code path).
    emitted_same_after = EVENTS_EMITTED_TOTAL.labels(
        namespace="openstudio-server", name="test-oscm", reason=reason
    )._value.get()
    emitted_other_after = EVENTS_EMITTED_TOTAL.labels(
        namespace="openstudio-server", name="test-oscm", reason="__sanity_other__"
    )._value.get()
    assert emitted_same_after == emitted_same_before
    assert emitted_other_after == emitted_other_before

    # And the suppressed counter stays at zero on the failure path
    # (the dry-run gate never fired — ``dry_run=False``).
    assert emitter.suppressed_count == 0
    assert emitter.dry_run is False