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

import openstudio_operator.events as events_module
from openstudio_operator.events import EventEmitter


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