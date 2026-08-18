"""Unit tests for the analysis SLA monitor core loop (issue #8).

REST is mocked with ``responses`` and the CR ``.status`` subresource with an
in-memory RFC 7386 merge-patch fake (same approach as test_status_store.py) —
no dependencies beyond the ``[dev]`` extra. All assertions target
``run_sla_tick`` directly; the kopf timer wrapper is thin wiring.
"""

import copy
from datetime import UTC, datetime, timedelta

import responses
from prometheus_client import REGISTRY

from openstudio_operator.config import OperatorConfig
from openstudio_operator.handlers.analysis_sla import (
    ANALYSIS_SOFT_STOPPED_EVENT,
    run_sla_tick,
)
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.status_store import StatusStore

BASE = "http://web.test"
NAMESPACE = "openstudio-server"
NAME = "oscm"
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
TEN_DAYS_AGO = (NOW - timedelta(days=10)).isoformat()

SPEC = {
    "serverUrl": BASE,
    "analysisPolicy": {"maxDurationMinutes": 180},
}


def make_cr(spec: dict | None = None) -> dict:
    return {
        "apiVersion": "energy.nrel.gov/v1alpha1",
        "kind": "OpenStudioClusterManager",
        "metadata": {"name": NAME, "namespace": NAMESPACE},
        "spec": copy.deepcopy(spec if spec is not None else SPEC),
        "status": {},
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


def soft_stops_total() -> float:
    return REGISTRY.get_sample_value("openstudio_operator_soft_stops_total") or 0.0


def register_started_analysis(
    analysis_id: str, start_time: datetime, *, created_at: str = TEN_DAYS_AGO
) -> None:
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": analysis_id, "status": "started", "created_at": created_at}],
    )
    responses.get(
        f"{BASE}/analyses/{analysis_id}/page_data.json",
        json={"analysis": {"status": "started", "start_time": start_time.isoformat()}},
    )
    responses.get(f"{BASE}/analyses/{analysis_id}/soft_stop", status=200, json={"result": "accepted"})


def tick(api, spec=None, client=None):
    store = StatusStore(NAMESPACE, NAME, api)
    config = OperatorConfig.from_spec(spec if spec is not None else SPEC)
    events, emit = make_emit()
    stopped = run_sla_tick(
        client if client is not None else OpenStudioClient(BASE),
        store,
        config,
        now=NOW,
        emit=emit,
    )
    return stopped, events


# --- One-shot semantics ------------------------------------------------------


@responses.activate
def test_soft_stop_fires_exactly_once_across_ticks():
    api = FakeCustomObjectsApi(make_cr())
    register_started_analysis("a1", NOW - timedelta(hours=4))
    register_started_analysis("a1", NOW - timedelta(hours=4))  # second tick's poll
    metric_before = soft_stops_total()

    stopped, events = tick(api)
    stopped2, events2 = tick(api)

    assert stopped == ["a1"]
    assert stopped2 == []
    assert calls_to("/soft_stop") == 1
    # Second tick skips before even fetching page_data (anchor checked first).
    assert calls_to("/page_data.json") == 1
    assert len(events) == 1
    assert events2 == []
    event_type, reason, message = events[0]
    assert event_type == "Warning"
    assert reason == ANALYSIS_SOFT_STOPPED_EVENT
    assert "a1" in message and "180" in message and "soft stop issued" in message
    assert soft_stops_total() - metric_before == 1
    assert api.obj["status"]["softStops"]["a1"]["outcome"] == "issued"
    assert api.obj["status"]["softStops"]["a1"]["issuedAt"] == NOW.isoformat()


@responses.activate
def test_anchor_survives_operator_restart():
    """Fresh client + store (new process state), same persisted CR → no double-fire."""
    api = FakeCustomObjectsApi(make_cr())
    register_started_analysis("a1", NOW - timedelta(hours=4))
    register_started_analysis("a1", NOW - timedelta(hours=4))

    tick(api, client=OpenStudioClient(BASE))  # "process 1"
    stopped, events = tick(api, client=OpenStudioClient(BASE))  # "process 2"

    assert stopped == []
    assert events == []
    assert calls_to("/soft_stop") == 1
    assert calls_to("/page_data.json") == 1
    assert api.patch_calls == 1  # only the original anchor write


# --- Clock anchor ------------------------------------------------------------


@responses.activate
def test_clock_is_page_data_start_time_not_created_at():
    """Both analyses created 10 days ago; only the long-STARTED one trips."""
    responses.get(
        f"{BASE}/analyses.json",
        json=[
            {"_id": "a-recent", "status": "started", "created_at": TEN_DAYS_AGO},
            {"_id": "a-long", "status": "started", "created_at": TEN_DAYS_AGO},
        ],
    )
    responses.get(
        f"{BASE}/analyses/a-recent/page_data.json",
        json={"analysis": {"status": "started", "start_time": (NOW - timedelta(minutes=5)).isoformat()}},
    )
    responses.get(
        f"{BASE}/analyses/a-long/page_data.json",
        json={"analysis": {"status": "started", "start_time": (NOW - timedelta(hours=4)).isoformat()}},
    )
    responses.get(f"{BASE}/analyses/a-long/soft_stop", status=200, json={"result": "accepted"})

    stopped, events = tick(FakeCustomObjectsApi(make_cr()))

    assert stopped == ["a-long"]
    assert calls_to("/soft_stop") == 1
    assert len(events) == 1
    assert "a-long" in events[0][2]


@responses.activate
def test_runtime_exactly_at_max_does_not_trip():
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": "a-edge", "status": "started", "created_at": TEN_DAYS_AGO}],
    )
    responses.get(
        f"{BASE}/analyses/a-edge/page_data.json",
        json={"analysis": {"status": "started", "start_time": (NOW - timedelta(minutes=180)).isoformat()}},
    )
    api = FakeCustomObjectsApi(make_cr())

    stopped, events = tick(api)

    assert stopped == []
    assert events == []
    assert calls_to("/soft_stop") == 0
    assert "softStops" not in api.obj["status"]


@responses.activate
def test_page_data_without_start_time_skips_this_tick():
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": "a1", "status": "started", "created_at": TEN_DAYS_AGO}],
    )
    responses.get(f"{BASE}/analyses/a1/page_data.json", json={"analysis": {"status": "started"}})
    api = FakeCustomObjectsApi(make_cr())

    stopped, events = tick(api)

    assert stopped == []
    assert events == []
    assert calls_to("/soft_stop") == 0
    assert "softStops" not in api.obj["status"]


# --- dryRun gating (D11) -------------------------------------------------------


@responses.activate
def test_dry_run_suppresses_rest_call_and_marks_event():
    api = FakeCustomObjectsApi(make_cr({**SPEC, "dryRun": True}))
    register_started_analysis("a1", NOW - timedelta(hours=4))
    register_started_analysis("a1", NOW - timedelta(hours=4))  # second tick's poll
    metric_before = soft_stops_total()

    stopped, events = tick(api, spec={**SPEC, "dryRun": True})
    stopped2, events2 = tick(api, spec={**SPEC, "dryRun": True})

    assert stopped == ["a1"]
    assert stopped2 == []
    # No soft_stop response registered: a real call would raise out of the tick.
    assert calls_to("/soft_stop") == 0
    assert len(events) == 1
    assert events2 == []
    event_type, reason, message = events[0]
    assert event_type == "Warning"
    assert reason == ANALYSIS_SOFT_STOPPED_EVENT
    assert "dryRun" in message and "suppressed" in message
    assert soft_stops_total() - metric_before == 1
    assert api.obj["status"]["softStops"]["a1"]["outcome"] == "dry-run"


# --- Candidate filtering --------------------------------------------------------


@responses.activate
def test_non_started_analyses_never_touched():
    responses.get(
        f"{BASE}/analyses.json",
        json=[
            {"_id": f"a-{status}", "status": status, "created_at": TEN_DAYS_AGO}
            for status in ("na", "init", "queued", "post-processing", "completed")
        ],
    )
    api = FakeCustomObjectsApi(make_cr())

    stopped, events = tick(api)

    assert stopped == []
    assert events == []
    assert calls_to("/page_data.json") == 0
    assert calls_to("/soft_stop") == 0
    assert "softStops" not in api.obj["status"]


@responses.activate
def test_auto_soft_stop_disabled_makes_monitor_passive():
    spec = {**SPEC, "analysisPolicy": {"maxDurationMinutes": 180, "autoSoftStop": False}}
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": "a1", "status": "started", "created_at": TEN_DAYS_AGO}],
    )
    api = FakeCustomObjectsApi(make_cr(spec))

    stopped, events = tick(api, spec=spec)

    assert stopped == []
    assert events == []
    assert calls_to("/page_data.json") == 0
    assert calls_to("/soft_stop") == 0
    assert "softStops" not in api.obj["status"]
