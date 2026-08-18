"""Unit tests for the zombie datapoint watchdog core loop (issue #10).

REST is mocked with ``responses`` (the light endpoint
``GET /data_points/status`` carries no timestamps on purpose — the whole
point of the operator clock) and the CR ``.status`` subresource with an
in-memory RFC 7386 merge-patch fake (same approach as test_status_store.py
and test_analysis_sla.py). All assertions target ``run_watchdog_tick``
directly; the kopf timer wrapper is thin wiring.
"""

import copy
from datetime import UTC, datetime, timedelta

import responses
from prometheus_client import REGISTRY

from openstudio_operator.config import OperatorConfig
from openstudio_operator.handlers.datapoint_watchdog import (
    DATAPOINT_REQUEUE_EXHAUSTED_EVENT,
    DATAPOINT_REQUEUED_EVENT,
    run_watchdog_tick,
)
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.status_store import StatusStore

BASE = "http://web.test"
NAMESPACE = "openstudio-server"
NAME = "oscm"
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

SPEC = {
    "serverUrl": BASE,
    "datapointPolicy": {"maxDatapointRuntimeMinutes": 45, "maxAutoRequeues": 2},
}


def make_cr(spec: dict | None = None, status: dict | None = None) -> dict:
    return {
        "apiVersion": "energy.nrel.gov/v1alpha1",
        "kind": "OpenStudioClusterManager",
        "metadata": {"name": NAME, "namespace": NAMESPACE},
        "spec": copy.deepcopy(spec if spec is not None else SPEC),
        "status": copy.deepcopy(status if status is not None else {}),
    }


class FakeCustomObjectsApi:
    """In-memory CustomObjectsApi stand-in with RFC 7386 merge-patch."""

    def __init__(self, obj: dict) -> None:
        self.obj = copy.deepcopy(obj)
        self.patch_calls = 0

    def get_namespaced_custom_object_status(self, group, version, namespace, plural, name):
        return copy.deepcopy(self.obj)

    def patch_namespaced_custom_object_status(
        self, group, version, namespace, plural, name, body, _content_type=None
    ):
        self.patch_calls += 1
        _merge_patch(self.obj, body)
        return copy.deepcopy(self.obj)


def _merge_patch(target: dict, patch: dict) -> None:
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_patch(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def make_emit():
    events: list[tuple[str, str, str]] = []

    def emit(event_type: str, reason: str, message: str) -> None:
        events.append((event_type, reason, message))

    return events, emit


def calls_to(suffix: str) -> int:
    return sum(1 for call in responses.calls if call.request.url.endswith(suffix))


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
