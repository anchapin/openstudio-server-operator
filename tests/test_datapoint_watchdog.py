"""Unit tests for the zombie datapoint watchdog core loop (issue #10).

REST is mocked with ``responses`` (the light endpoint
``GET /data_points/status`` carries no timestamps on purpose — the whole
point of the operator clock) and the CR ``.status`` subresource with an
in-memory RFC 7386 merge-patch fake (same approach as test_status_store.py
and test_analysis_sla.py). All assertions target ``run_watchdog_tick``
directly; the kopf timer wrapper is thin wiring.
"""

import logging
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest
import responses
from kubernetes.client import ApiException
from prometheus_client import REGISTRY

from _fakes import FakeCustomObjectsApi, calls_to, make_emit, tick_failures_total
from _fakes import make_cr as _shared_make_cr
from openstudio_operator.config import OperatorConfig
from openstudio_operator.events import EventEmitter
from openstudio_operator.handlers.datapoint_watchdog import (
    DATAPOINT_REQUEUE_EXHAUSTED_EVENT,
    DATAPOINT_REQUEUED_EVENT,
    run_watchdog_tick,
    zombie_datapoint_watchdog,
)
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.status_store import StatusStore, StatusStoreConflictError

BASE = "http://web.test"
NAMESPACE = "openstudio-server"
NAME = "oscm"
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

SPEC = {
    "serverUrl": BASE,
    "datapointPolicy": {"maxDatapointRuntimeMinutes": 45, "maxAutoRequeues": 2},
}

# Shared-fake binding (issue #474): this module's make_cr default spec.
make_cr = partial(_shared_make_cr, default_spec=SPEC)


def requeued_total() -> float:
    return REGISTRY.get_sample_value("openstudio_operator_datapoints_requeued_total") or 0.0


def exhausted_total() -> float:
    return REGISTRY.get_sample_value("openstudio_operator_datapoints_requeue_exhausted_total") or 0.0


def register_started(*dp_ids: str) -> None:
    # Registered without a query string so it matches the client's
    # ?status=1&jobs=started; the light-view docs carry no timestamps.
    responses.get(
        f"{BASE}/data_points/status",
        json={"data_points": [{"_id": dp_id, "status": "started"} for dp_id in dp_ids]},
    )


def register_requeue(dp_id: str) -> None:
    responses.post(f"{BASE}/data_points/{dp_id}/requeue", status=204)


def tick(api, spec=None, client=None, exhausted_seen=None, now=NOW):
    store = StatusStore(NAMESPACE, NAME, api)
    config = OperatorConfig.from_spec(spec if spec is not None else SPEC)
    events, emit = make_emit()
    requeued = run_watchdog_tick(
        client if client is not None else OpenStudioClient(BASE),
        store,
        config,
        now=now,
        emit=emit,
        exhausted_seen=exhausted_seen if exhausted_seen is not None else set(),
    )
    return requeued, events


# --- startedSince lifecycle ---------------------------------------------------


@responses.activate
def test_started_since_created_on_first_observation():
    api = FakeCustomObjectsApi(make_cr())
    register_started("d1")

    requeued, events = tick(api)

    assert requeued == []
    assert events == []
    assert api.obj["status"]["startedSince"] == {"d1": NOW.isoformat()}


@responses.activate
def test_started_since_pruned_on_departure():
    api = FakeCustomObjectsApi(make_cr())
    register_started("d1")
    tick(api)
    register_started()  # d1 left the started view

    requeued, events = tick(api, now=NOW + timedelta(minutes=1))

    assert requeued == []
    assert events == []
    assert api.obj["status"]["startedSince"] == {}


@responses.activate
def test_started_since_reset_on_re_entry():
    """dp leaves and comes back → fresh clock, not the stale one."""
    api = FakeCustomObjectsApi(make_cr())
    register_started("d1")
    tick(api)
    register_started()  # departs
    tick(api, now=NOW + timedelta(minutes=1))
    later = NOW + timedelta(hours=2)
    register_started("d1")  # re-enters

    requeued, events = tick(api, now=later)

    # Old clock would be 2h old (way over 45m) and trip; fresh clock must not.
    assert requeued == []
    assert events == []
    assert calls_to("/requeue") == 0
    assert api.obj["status"]["startedSince"] == {"d1": later.isoformat()}


# --- Issue #472 — ANALYSIS_DATAPOINT_COUNT view label ---------------------------


def _view_count(view: str) -> float:
    """Sample count of the ``view``-labelled ANALYSIS_DATAPOINT_COUNT series.

    Issue #472 split the pre-existing unlabelled merge into two labelled
    series; ``get_sample_value`` reads the ``_count`` sample of exactly one
    series so the two observe sites can be asserted independently.
    """
    return (
        REGISTRY.get_sample_value(
            "openstudio_operator_analysis_datapoint_count_count", {"view": view}
        )
        or 0.0
    )


@responses.activate
def test_analysis_datapoint_count_view_label_is_started_datapoints_per_tick():
    """Issue #472 — the watchdog observe site must record on the
    ``view="started_datapoints_per_tick"`` series (``len(started_ids)`` —
    a count of DATAPOINTS) and never on the SLA monitor's
    ``view="analyses_per_tick"`` series: the two populations have
    different units and magnitudes, and the pre-#472 unlabelled merge
    made the family's percentiles meaningless. Pins the exact exposition
    label value so a refactor that swaps the two sites' label values (or
    drops the label) is caught at CI."""
    api = FakeCustomObjectsApi(make_cr())
    register_started("d1", "d2")
    watchdog_before = _view_count("started_datapoints_per_tick")
    sla_before = _view_count("analyses_per_tick")

    tick(api)

    watchdog_after = _view_count("started_datapoints_per_tick")
    sla_after = _view_count("analyses_per_tick")
    assert watchdog_after - watchdog_before == 1.0  # exactly one observation per tick
    # The watchdog site never touches the SLA's series — the two units
    # must stay separable on the dashboard.
    assert sla_after == sla_before


# --- Requeue triggering ---------------------------------------------------------


@responses.activate
def test_requeue_fires_when_over_runtime_and_under_budget():
    api = FakeCustomObjectsApi(make_cr())
    register_started("d1")
    register_requeue("d1")
    tick(api)  # clock created at NOW
    over = NOW + timedelta(minutes=46)
    metric_before = requeued_total()

    requeued, events = tick(api, now=over)

    assert requeued == ["d1"]
    assert calls_to("/requeue") == 1
    assert len(events) == 1
    event_type, reason, message = events[0]
    assert event_type == "Normal"
    assert reason == DATAPOINT_REQUEUED_EVENT
    assert "d1" in message and "45" in message and "1/2" in message and "issued" in message
    assert requeued_total() - metric_before == 1
    assert api.obj["status"]["requeues"]["d1"] == {
        "count": 1,
        "lastRequeuedAt": over.isoformat(),
    }
    # Pacing: the requeue resets the clock for a full fresh runtime window.
    assert api.obj["status"]["startedSince"]["d1"] == over.isoformat()


@responses.activate
def test_under_runtime_never_trips():
    api = FakeCustomObjectsApi(
        make_cr(status={"startedSince": {"d1": (NOW - timedelta(minutes=30)).isoformat()}})
    )
    register_started("d1")

    requeued, events = tick(api)

    assert requeued == []
    assert events == []
    assert calls_to("/requeue") == 0
    assert "requeues" not in api.obj["status"]


@responses.activate
def test_runtime_exactly_at_max_does_not_trip():
    api = FakeCustomObjectsApi(
        make_cr(status={"startedSince": {"d1": (NOW - timedelta(minutes=45)).isoformat()}})
    )
    register_started("d1")

    requeued, events = tick(api)

    assert requeued == []
    assert events == []


# --- Exhaustion (D06: no further action, ever) ---------------------------------


@responses.activate
def test_exhausted_never_requeued_and_evented_once():
    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "startedSince": {"d1": (NOW - timedelta(minutes=90)).isoformat()},
                "requeues": {
                    "d1": {"count": 2, "lastRequeuedAt": (NOW - timedelta(hours=2)).isoformat()}
                },
            }
        )
    )
    register_started("d1")
    register_requeue("d1")  # would fail the tick loudly if called
    metric_before = exhausted_total()
    seen = set()  # same operator process → shared presentation cache

    requeued, events = tick(api, exhausted_seen=seen)
    requeued2, events2 = tick(api, exhausted_seen=seen, now=NOW + timedelta(minutes=1))

    assert requeued == [] and requeued2 == []
    assert calls_to("/requeue") == 0
    assert len(events) == 1
    assert events2 == []
    event_type, reason, message = events[0]
    assert event_type == "Warning"
    assert reason == DATAPOINT_REQUEUE_EXHAUSTED_EVENT
    assert "d1" in message and "2/2" in message and "no further action" in message
    assert exhausted_total() - metric_before == 1
    # No state churn on an exhausted dp: budget and clock untouched.
    assert api.obj["status"]["requeues"]["d1"]["count"] == 2
    assert api.obj["status"]["startedSince"]["d1"] == (NOW - timedelta(minutes=90)).isoformat()


@responses.activate
def test_exhaustion_reemits_once_after_simulated_restart():
    """exhausted_seen is presentation-only: a fresh process warns once more."""
    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "startedSince": {"d1": (NOW - timedelta(minutes=90)).isoformat()},
                "requeues": {"d1": {"count": 2, "lastRequeuedAt": NOW.isoformat()}},
            }
        )
    )
    register_started("d1")

    tick(api)  # process 1 warns
    requeued, events = tick(api, exhausted_seen=set())  # process 2: fresh cache

    assert requeued == []
    assert len(events) == 1
    assert events[0][1] == DATAPOINT_REQUEUE_EXHAUSTED_EVENT
    assert calls_to("/requeue") == 0


@responses.activate
def test_max_auto_requeues_zero_is_warn_only_mode():
    spec = {**SPEC, "datapointPolicy": {"maxDatapointRuntimeMinutes": 45, "maxAutoRequeues": 0}}
    api = FakeCustomObjectsApi(
        make_cr(spec, status={"startedSince": {"d1": (NOW - timedelta(minutes=60)).isoformat()}})
    )
    register_started("d1")

    requeued, events = tick(api, spec=spec)

    assert requeued == []
    assert calls_to("/requeue") == 0
    assert len(events) == 1
    assert events[0][0] == "Warning"
    assert events[0][1] == DATAPOINT_REQUEUE_EXHAUSTED_EVENT


# --- Budget durability across restarts and departures --------------------------


@responses.activate
def test_budget_survives_operator_restart():
    """Fresh client + store (new process state), same persisted CR → the
    budget keeps counting up; exactly maxAutoRequeues POSTs ever, then done."""
    api = FakeCustomObjectsApi(
        make_cr(status={"startedSince": {"d1": (NOW - timedelta(minutes=60)).isoformat()}})
    )
    register_started("d1")
    register_requeue("d1")

    tick(api, client=OpenStudioClient(BASE))  # requeue 1/2
    tick(api, client=OpenStudioClient(BASE), now=NOW + timedelta(minutes=46))  # 2/2
    requeued, events = tick(
        api, client=OpenStudioClient(BASE), now=NOW + timedelta(minutes=92)
    )

    assert calls_to("/requeue") == 2
    assert api.obj["status"]["requeues"]["d1"]["count"] == 2
    assert requeued == []
    assert len(events) == 1
    assert events[0][1] == DATAPOINT_REQUEUE_EXHAUSTED_EVENT


@responses.activate
def test_departure_keeps_requeue_budget():
    """A requeued dp legitimately leaves `started` while queued — its budget
    must survive the departure (else the bound would reset and unbound)."""
    api = FakeCustomObjectsApi(make_cr(status={"startedSince": {"d1": (NOW - timedelta(minutes=60)).isoformat()}}))
    register_started("d1")
    register_requeue("d1")
    tick(api)  # requeue 1/2, clock reset to NOW
    register_started()  # d1 departs (on the requeued queue)
    tick(api, now=NOW + timedelta(minutes=1))
    assert api.obj["status"]["startedSince"] == {}  # clock pruned…
    assert api.obj["status"]["requeues"]["d1"]["count"] == 1  # …budget kept

    later = NOW + timedelta(hours=1)
    register_started("d1")  # re-enters with a fresh clock
    tick(api, now=later)
    requeued, _ = tick(api, now=later + timedelta(minutes=46))

    assert requeued == ["d1"]  # second requeue fires…
    assert api.obj["status"]["requeues"]["d1"]["count"] == 2  # …counting 2/2, not 1/2 again


# --- Conservative post-restart backfill -----------------------------------------


@responses.activate
def test_post_restart_backfill_is_conservative():
    """dp already started on the server, operator restarts with no anchor →
    fresh clock (now), no immediate trip — even with budget remaining."""
    api = FakeCustomObjectsApi(make_cr())  # empty status: full restart
    register_started("d1")
    register_requeue("d1")

    requeued, events = tick(api, client=OpenStudioClient(BASE))

    assert requeued == []
    assert events == []
    assert calls_to("/requeue") == 0
    assert api.obj["status"]["startedSince"] == {"d1": NOW.isoformat()}


# --- dryRun gating (D11) ---------------------------------------------------------


@responses.activate
def test_dry_run_suppresses_rest_call_and_burns_budget_like_real():
    spec = {**SPEC, "dryRun": True}
    over = NOW - timedelta(minutes=60)
    api = FakeCustomObjectsApi(make_cr(spec, status={"startedSince": {"d1": over.isoformat()}}))
    register_started("d1")
    metric_before = requeued_total()

    requeued, events = tick(api, spec=spec)

    # No requeue response registered: a real POST would raise out of the tick.
    assert calls_to("/requeue") == 0
    assert requeued == ["d1"]
    assert len(events) == 1
    event_type, reason, message = events[0]
    assert event_type == "Normal"
    assert reason == DATAPOINT_REQUEUED_EVENT
    assert "dryRun" in message and "suppressed" in message
    assert requeued_total() - metric_before == 1
    # Budget accounted exactly as a real run: count=1 against maxAutoRequeues=2.
    assert api.obj["status"]["requeues"]["d1"]["count"] == 1

    # Flip dryRun off well past the runtime window: the burned budget holds —
    # only ONE real requeue remains, then exhaustion. No double-burn.
    spec_off = {**SPEC, "dryRun": False}
    register_requeue("d1")
    requeued2, _ = tick(api, spec=spec_off, now=NOW + timedelta(minutes=120))
    requeued3, events3 = tick(api, spec=spec_off, now=NOW + timedelta(minutes=180))

    assert requeued2 == ["d1"]
    assert requeued3 == []
    assert calls_to("/requeue") == 1
    assert events3[0][1] == DATAPOINT_REQUEUE_EXHAUSTED_EVENT


# --- Issue #232: kopf 1.4x MappingView body end-to-end through EventEmitter -----


import types


@responses.activate
def test_run_watchdog_tick_accepts_mappingview_body_in_event_emitter():
    """Issue #232 — MappingView (types.MappingProxyType) body end-to-end.

    kopf >=1.4x delivers ``body`` to timer handlers as a MappingView subclass
    (not a dict subclass). This regression test wires
    ``types.MappingProxyType`` into ``EventEmitter``, drives
    ``run_watchdog_tick`` through its requeue emit path, and asserts:

    1. ``EventEmitter.dry_run`` gate still records the suppression through
       ``EventEmitter.suppressed_count`` (D11 end-to-end);
    2. ``body.get('metadata')`` introspection keeps working — the access
       shape ``kopf.event`` and any status-update helper rely on.

    No production code change; the test fails the day a kopf upgrade makes
    the body shape incompatible with ``EventEmitter``. Scope guard: handler /
    guard untouched.
    """
    spec = {**SPEC, "dryRun": True}
    over = NOW - timedelta(minutes=60)
    api = FakeCustomObjectsApi(
        make_cr(spec, status={"startedSince": {"d1": over.isoformat()}})
    )
    register_started("d1")

    # CR body as kopf 1.4x would deliver it: a MappingView, not a dict subclass.
    cr_body = make_cr(spec)
    proxy_body = types.MappingProxyType(cr_body)
    assert not isinstance(proxy_body, dict)  # the shape this regression exists for
    # (2) body.get('metadata') still resolves through the proxy.
    assert proxy_body.get("metadata") == cr_body["metadata"]

    # (1) Wire the production EventEmitter (not the test closure stub) — the
    # D11 gate under test lives here.
    emitter = EventEmitter(body=proxy_body, dry_run=True)
    config = OperatorConfig.from_spec(spec)
    store = StatusStore(NAMESPACE, NAME, api)

    requeued = run_watchdog_tick(
        OpenStudioClient(BASE),
        store,
        config,
        now=NOW,
        emit=emitter,
        exhausted_seen=set(),
    )

    assert requeued == ["d1"]
    # Dry-run gate still records exactly the one requeue Normal Event.
    assert emitter.suppressed_count == 1
    assert emitter.dry_run is True


# --- Issue #466 — tick-level error/recovery paths (D12) -------------------------
#
# The wrapper-level exception tuple is pinned by test_timer_wrapper_failures.py
# (issue #231) with a MOCKED run_watchdog_tick. These tests close the remaining
# gap: real errors arising INSIDE run_watchdog_tick — REST 5xx through the real
# client's retry envelope, real 409s through the real StatusStore RMW, and raw
# kubernetes ApiExceptions re-raised verbatim by the store's non-409 path.


class ConflictingFakeCustomObjectsApi(FakeCustomObjectsApi):
    """Merge-patch fake whose first ``patch_conflicts`` patches raise 409.

    Same synthetic-409 approach as tests/test_status_store.py — exercised here
    through the full tick so the store's bounded retry is observed from the
    watchdog's point of view, not the store's.
    """

    def __init__(self, obj: dict, patch_conflicts: int = 0) -> None:
        super().__init__(obj)
        self.remaining_conflicts = patch_conflicts
        self.conflicts_seen = 0

    def patch_namespaced_custom_object_status(
        self, group, version, namespace, plural, name, body, _content_type=None
    ):
        if self.remaining_conflicts > 0:
            self.remaining_conflicts -= 1
            self.conflicts_seen += 1
            raise ApiException(status=409, reason="Conflict")
        return super().patch_namespaced_custom_object_status(
            group, version, namespace, plural, name, body, _content_type=_content_type
        )


class ExplodingPatchFakeCustomObjectsApi(FakeCustomObjectsApi):
    """Every status patch raises a raw non-409 ApiException (API-server blip).

    StatusStore._patch_status re-raises non-409 ApiExceptions verbatim
    (status_store.py: ``if exc.status != 409: raise``) — so this surfaces a
    raw kubernetes exception at the tick boundary, not a StatusStoreError.
    """

    def patch_namespaced_custom_object_status(
        self, group, version, namespace, plural, name, body, _content_type=None
    ):
        raise ApiException(status=500, reason="Internal Server Error")


def started_view_calls() -> int:
    # ``calls_to`` suffix-matches, but the light view carries a query string
    # (?status=1&jobs=started) — count by prefix instead.
    return sum(
        1 for call in responses.calls if call.request.url.startswith(f"{BASE}/data_points/status")
    )


def call_wrapper(monkeypatch, status: dict | None = None, api=None):
    """Invoke the production kopf timer wrapper directly (same direct-call
    pattern as test_timer_wrapper_failures.py) with the k8s seam stubbed."""
    if api is None:
        api = FakeCustomObjectsApi(make_cr(status=status))
    monkeypatch.setattr(
        "openstudio_operator.handlers.datapoint_watchdog.operator_custom_objects_api",
        lambda: api,
    )
    return zombie_datapoint_watchdog(
        body=make_cr(),
        spec=SPEC,
        namespace=NAMESPACE,
        name=NAME,
        logger=logging.getLogger("test"),
    )


@responses.activate
def test_sustained_503_on_started_view_exhausts_client_retries_and_wrapper_skips_tick(
    monkeypatch, caplog
):
    """Issue #466 (a): GET /data_points/status 503s on every attempt → the
    client's GET-only 3× retry envelope exhausts (4 attempts) → OpenStudioApiError
    raises out of run_watchdog_tick → the wrapper swallows it, bumps
    HANDLER_TICK_FAILURES_TOTAL with all four labels, logs the skip-tick
    warning, and returns None (D12: the next poll retries naturally)."""
    responses.get(f"{BASE}/data_points/status", status=503)
    client_sleeps: list[float] = []
    monkeypatch.setattr("openstudio_operator.openstudio_client._sleep", client_sleeps.append)
    api = FakeCustomObjectsApi(make_cr())  # never reached — tick dies on the first call

    before = tick_failures_total(NAMESPACE, NAME, "datapoint_watchdog", "OpenStudioApiError")
    result = call_wrapper(monkeypatch, api=api)
    after = tick_failures_total(NAMESPACE, NAME, "datapoint_watchdog", "OpenStudioApiError")

    assert result is None, "wrapper must swallow OpenStudioApiError and return None (D12)"
    # max_retries=3 → 4 total GET attempts on the light view, all 503.
    assert started_view_calls() == 4
    # Jittered backoff sleeps before each RETRY (not before the first attempt).
    assert len(client_sleeps) == 3
    # after/before both read the exact 4-label sample — the delta == 1 IS the
    # label-exactness proof (a wrong namespace/name/module/error_type reads 0.0).
    assert after - before == 1.0
    assert "datapoint watchdog tick skipped, retrying next poll (OpenStudioApiError" in caplog.text
    # D04 recovery anchor: the tick died before any status write — nothing recorded.
    assert api.obj["status"] == {}


@responses.activate
def test_status_409_during_set_requeue_resolved_by_store_bounded_retry(monkeypatch):
    """Issue #466 (b): one 409 on the requeue-recording status patch (the
    issue's ``mark_datapoint_requeued`` — the store call is ``set_requeue``)
    → StatusStore re-reads and re-applies internally → the tick completes
    with the requeue recorded and the pacing clock reset. No exception."""
    store_sleeps: list[float] = []
    monkeypatch.setattr("openstudio_operator.status_store._sleep", store_sleeps.append)
    api = ConflictingFakeCustomObjectsApi(
        make_cr(status={"startedSince": {"d1": (NOW - timedelta(minutes=60)).isoformat()}}),
        patch_conflicts=1,
    )
    register_started("d1")
    register_requeue("d1")

    requeued, events = tick(api)

    assert requeued == ["d1"]
    assert api.conflicts_seen == 1
    # One backoff sleep inside the store's retry, then success.
    assert len(store_sleeps) == 1
    # set_requeue success + pacing set_started_since success = 2 landed patches.
    assert api.patch_calls == 2
    assert api.obj["status"]["requeues"]["d1"] == {"count": 1, "lastRequeuedAt": NOW.isoformat()}
    assert api.obj["status"]["startedSince"]["d1"] == NOW.isoformat()
    assert len(events) == 1
    assert events[0][:2] == ("Normal", DATAPOINT_REQUEUED_EVENT)


@responses.activate
def test_sustained_status_409_raises_out_of_tick_with_requeue_unrecorded(monkeypatch):
    """Issue #466 (b, raise side): 409s past MAX_CONFLICT_RETRIES →
    StatusStoreConflictError raises out of run_watchdog_tick (caught by the
    wrapper's StatusStoreError branch). The REST requeue already fired but
    was NOT recorded — exactly the documented D12 recovery shape: "unrecorded
    requeues are re-attempted next poll, recorded ones never re-fire"."""
    monkeypatch.setattr("openstudio_operator.status_store._sleep", lambda _s: None)
    api = ConflictingFakeCustomObjectsApi(
        make_cr(status={"startedSince": {"d1": (NOW - timedelta(minutes=60)).isoformat()}}),
        patch_conflicts=99,
    )
    register_started("d1")
    register_requeue("d1")

    with pytest.raises(StatusStoreConflictError, match="409"):
        tick(api)

    # The mutation happened before the failed status write; nothing was anchored.
    assert calls_to("/requeue") == 1
    assert "requeues" not in api.obj["status"]


@responses.activate
def test_raw_api_exception_from_status_patch_is_counted_by_shared_skip_tuple(monkeypatch, caplog):
    """Issue #466 (c), post-#473 — the shared tuple widened the catch.

    StatusStore re-raises non-409 ApiExceptions verbatim. Pre-#473 the
    watchdog wrapper's except tuple was ``(OpenStudioApiError,
    StatusStoreError)`` — narrower than its siblings — so a raw
    kubernetes ApiException escaped the wrapper uncounted. #473 unified
    the four wrappers onto one canonical skip-tick tuple (the union of
    the historical per-module tuples) in
    ``_oscm_handlers.run_oscm_tick``, which includes ``ApiException``:
    the exception is now swallowed, counted, logged, and the tick is
    re-attempted next poll (D12) — same fail-soft posture the
    analysis_sla / worker_recycler / web_background wrappers always had.
    """
    api = ExplodingPatchFakeCustomObjectsApi(make_cr())
    register_started("d1")

    before = tick_failures_total(NAMESPACE, NAME, "datapoint_watchdog", "ApiException")
    result = call_wrapper(monkeypatch, api=api)

    assert result is None, "shared tuple must swallow ApiException and return None (D12)"
    assert tick_failures_total(NAMESPACE, NAME, "datapoint_watchdog", "ApiException") - before == 1.0
    assert "datapoint watchdog tick skipped, retrying next poll (ApiException" in caplog.text
    # The tick died at the very first status write (first observation clock).
    assert api.patch_calls == 0
    assert api.obj["status"] == {}
