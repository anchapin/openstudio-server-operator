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
import types
from unittest.mock import patch

import pytest
from kubernetes.client import ApiException

import openstudio_operator.events as events_module
from openstudio_operator.events import EventEmitter
from openstudio_operator.metrics import (
    EVENTS_DRY_RUN_SUPPRESSED_TOTAL,
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


# --- Issue #501: __call__ shim, metadata-missing bodies, extraction edges -----


def test_call_shim_delegates_to_emit_with_identical_positional_arguments(body):
    """Issue #501 — the ``__call__`` shim is pure positional delegation.

    ``EventEmitter.__call__(event_type, reason, message)`` must invoke
    ``self.emit(event_type, reason, message)`` with the identical
    positional argument order (the pre-#164 handler idiom
    ``emit("Warning", REASON, message)`` depends on it). Spying on the
    class-level ``emit`` (the instance has ``__slots__``, so the method
    cannot be patched per-instance) and asserting the exact call — self
    included — pins the delegation shape: any future signature drift in
    ``emit`` that breaks the shim fails here first.

    Also pins that ``__call__`` returns ``None`` — exactly what
    :meth:`emit` returns. The handler wrappers treat the emitter as a
    ``Callable[[str, str, str], None]`` (``TickEmitter``), so both paths
    must yield the same (``None``) value.
    """
    emitter = EventEmitter(body=body, dry_run=False)

    with patch.object(EventEmitter, "emit", autospec=True) as mock_emit:
        result = emitter("Warning", "Reason", "message")

    mock_emit.assert_called_once_with(emitter, "Warning", "Reason", "message")
    assert result is None


def test_call_shim_dry_run_false_posts_kopf_event_same_shape_as_emit(body):
    """Issue #501 — ``__call__`` under ``dry_run=False`` is ``emit``.

    Calling the instance must reach ``kopf.event`` with the exact same
    call shape the direct :meth:`emit` path produces (pinned by
    ``test_emit_emits_warning_event_when_dry_run_false``): the bound
    body plus keyword ``type=``/``reason=``/``message=``. The emitted
    counter is bumped with the CR's extracted (namespace, name) labels
    (#311), and the suppressed counter stays at zero — no gate tripped.
    """
    emitted_before = EVENTS_EMITTED_TOTAL.labels(
        namespace="openstudio-server", name="test-oscm", reason="Reason"
    )._value.get()

    emitter = EventEmitter(body=body, dry_run=False)
    with patch.object(events_module, "kopf") as mock_kopf:
        result = emitter("Warning", "Reason", "message")

    mock_kopf.event.assert_called_once_with(
        body, type="Warning", reason="Reason", message="message"
    )
    assert emitter.suppressed_count == 0
    assert result is None

    emitted_after = EVENTS_EMITTED_TOTAL.labels(
        namespace="openstudio-server", name="test-oscm", reason="Reason"
    )._value.get()
    assert emitted_after - emitted_before == 1.0


def test_call_shim_dry_run_suppression_shares_counter_and_metric(body):
    """Issue #501 — suppressed via ``__call__`` counts exactly like via ``emit``.

    The D11 gate lives inside :meth:`emit`; the shim must not bypass or
    double-count it. Mixing two shim calls with two direct ``emit``
    calls under ``dry_run=True`` must yield ``suppressed_count == 4``
    and a single ``EVENTS_DRY_RUN_SUPPRESSED_TOTAL`` delta of 4 on the
    CR's (namespace, name, reason) series (#237 / #311) — with
    ``kopf.event`` never touched.
    """
    suppressed_before = EVENTS_DRY_RUN_SUPPRESSED_TOTAL.labels(
        namespace="openstudio-server", name="test-oscm", reason="Reason"
    )._value.get()

    emitter = EventEmitter(body=body, dry_run=True)
    with patch.object(events_module, "kopf") as mock_kopf:
        emitter("Warning", "Reason", "via call one")
        emitter.emit("Warning", "Reason", "via emit one")
        emitter("Warning", "Reason", "via call two")
        emitter.emit("Warning", "Reason", "via emit two")

    mock_kopf.event.assert_not_called()
    assert emitter.suppressed_count == 4

    suppressed_after = EVENTS_DRY_RUN_SUPPRESSED_TOTAL.labels(
        namespace="openstudio-server", name="test-oscm", reason="Reason"
    )._value.get()
    assert suppressed_after - suppressed_before == 4.0


#: Issue #501 — body shapes where metadata is absent, partial, falsy, or
#: non-dict, paired with the (namespace, name) labels the extraction in
#: ``EventEmitter.__init__`` (#311) must resolve to. Current behavior:
#: ``str(metadata.get(...) or "<unknown>")`` — missing keys, ``None``,
#: and empty strings all fall back to ``"<unknown>"``; present values
#: are coerced through ``str()``. Extraction happens at construction
#: and never raises for any of these shapes.
_MISSING_METADATA_CASES = (
    ({}, "<unknown>", "<unknown>"),
    ({"spec": {}}, "<unknown>", "<unknown>"),
    ({"metadata": None}, "<unknown>", "<unknown>"),
    ({"metadata": {}}, "<unknown>", "<unknown>"),
    ({"metadata": "not-a-dict"}, "<unknown>", "<unknown>"),
    ({"metadata": {"name": "only-name"}}, "<unknown>", "only-name"),
    ({"metadata": {"namespace": "only-ns"}}, "only-ns", "<unknown>"),
    ({"metadata": {"namespace": "", "name": ""}}, "<unknown>", "<unknown>"),
    ({"metadata": {"namespace": None, "name": None}}, "<unknown>", "<unknown>"),
    ({"metadata": {"namespace": 123, "name": 456}}, "123", "456"),
)

_MISSING_METADATA_IDS = (
    "empty-body",
    "no-metadata-key",
    "metadata-none",
    "metadata-empty-dict",
    "metadata-non-dict",
    "name-only",
    "namespace-only",
    "falsy-strings",
    "none-values",
    "str-coercion",
)


@pytest.mark.parametrize(
    ("body_shape", "expected_namespace", "expected_name"),
    _MISSING_METADATA_CASES,
    ids=_MISSING_METADATA_IDS,
)
def test_missing_metadata_body_shapes_pin_unknown_labels(body_shape, expected_namespace,
                                                         expected_name):
    """Issue #501 — metadata-missing body shapes resolve to ``<unknown>`` labels.

    ``EventEmitter.__init__`` treats a missing ``metadata`` block as the
    ``"<unknown>"`` placeholder rather than raising (the timer wrapper
    already validated the body via the singleton guard). This pins, per
    shape, the exact (namespace, name) pair that ends up on the
    ``EVENTS_DRY_RUN_SUPPRESSED_TOTAL`` series — the publicly observable
    surface of the extraction (#311) — and that one shim-invoked
    suppressed emit under ``dry_run=True`` bumps exactly that series by
    1 with ``kopf.event`` untouched. Construction itself must not raise
    for any shape (a body kopf actually delivers always carries full
    metadata; these are the defensive fallbacks).
    """
    suppressed_before = EVENTS_DRY_RUN_SUPPRESSED_TOTAL.labels(
        namespace=expected_namespace, name=expected_name, reason="Reason"
    )._value.get()

    emitter = EventEmitter(body=body_shape, dry_run=True)
    with patch.object(events_module, "kopf") as mock_kopf:
        emitter("Warning", "Reason", "message")

    mock_kopf.event.assert_not_called()
    assert emitter.suppressed_count == 1

    suppressed_after = EVENTS_DRY_RUN_SUPPRESSED_TOTAL.labels(
        namespace=expected_namespace, name=expected_name, reason="Reason"
    )._value.get()
    assert suppressed_after - suppressed_before == 1.0


def test_mappingview_body_pins_unknown_labels_but_emit_still_flows(body):
    """Issue #501 / #232 — kopf 1.4x MappingView body: labels are ``<unknown>``.

    kopf >=1.4x delivers ``body`` as ``kopf._cogs.structs.bodies.Body``
    — a MappingView subclass, NOT a ``dict`` subclass. The extraction
    in ``__init__`` (#311) guards with ``isinstance(body, dict)``, so a
    MappingView body — even one carrying full metadata — resolves to
    ``("<unknown>", "<unknown>")`` labels. This is CURRENT pinned
    behavior (flagged in #501 as a surprise, not fixed there): the
    #232 end-to-end tests already drive MappingProxyType bodies through
    the handlers' suppressed paths, so those series are already
    labelled ``<unknown>`` in practice.

    The emit path itself is unaffected: with ``dry_run=False`` the
    proxy body flows to ``kopf.event`` unchanged (``kopf.event`` reads
    the mapping, it does not require a dict), and the emitted counter
    is bumped once on the ``<unknown>``-labelled series.
    """
    proxy_body = types.MappingProxyType(body)
    assert not isinstance(proxy_body, dict)  # the shape this pin exists for

    emitted_before = EVENTS_EMITTED_TOTAL.labels(
        namespace="<unknown>", name="<unknown>", reason="Reason"
    )._value.get()

    emitter = EventEmitter(body=proxy_body, dry_run=False)
    with patch.object(events_module, "kopf") as mock_kopf:
        emitter("Warning", "Reason", "message")

    mock_kopf.event.assert_called_once_with(
        proxy_body, type="Warning", reason="Reason", message="message"
    )
    assert emitter.suppressed_count == 0

    emitted_after = EVENTS_EMITTED_TOTAL.labels(
        namespace="<unknown>", name="<unknown>", reason="Reason"
    )._value.get()
    assert emitted_after - emitted_before == 1.0


def test_plain_dict_and_mappingview_extraction_asymmetry(body):
    """Issue #501 — plain dict and MappingView bodies are NOT extraction-equivalent.

    The issue asked for "kopf MappingView vs plain dict equivalence";
    the pinned reality is an asymmetry: the SAME underlying data
    extracts real (namespace, name) labels when delivered as a plain
    dict, and ``("<unknown>", "<unknown>")`` when delivered as a
    MappingView (the ``isinstance(body, dict)`` guard). Both emitters
    still gate identically (one suppressed emit each, same D11
    behavior) — only the counter labels differ. If a future change
    makes extraction MappingView-aware, this test flips and the
    #232 end-to-end pins keep the emit path honest.
    """
    proxy_body = types.MappingProxyType(body)

    dict_emitter = EventEmitter(body=body, dry_run=True)
    proxy_emitter = EventEmitter(body=proxy_body, dry_run=True)

    with patch.object(events_module, "kopf") as mock_kopf:
        dict_emitter.emit("Warning", "Reason", "message")
        proxy_emitter.emit("Warning", "Reason", "message")

    mock_kopf.event.assert_not_called()

    def _suppressed(namespace: str, name: str) -> float:
        return EVENTS_DRY_RUN_SUPPRESSED_TOTAL.labels(
            namespace=namespace, name=name, reason="Reason"
        )._value.get()

    before_dict = _suppressed("openstudio-server", "test-oscm")
    assert before_dict >= 1.0  # the dict emitter's series moved (sanity)
    before_proxy = _suppressed("<unknown>", "<unknown>")
    assert before_proxy >= 1.0  # the proxy emitter's series moved (sanity)

    assert dict_emitter.suppressed_count == 1
    assert proxy_emitter.suppressed_count == 1