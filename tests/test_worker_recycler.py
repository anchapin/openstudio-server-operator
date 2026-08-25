"""Unit tests for the gated worker recycler (issue #11).

REST is mocked with ``responses`` (``GET /analyses.json`` only — the gate is
checked first, so gated ticks must not even poll), the CR ``.status``
subresource with an in-memory RFC 7386 merge-patch fake (same approach as
test_status_store.py / test_analysis_sla.py), and the Deployment patch with a
``FakeAppsV1Api`` recording calls — no live cluster, no extra dependencies
beyond the ``[dev]`` extra.
"""

import logging
import types
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest
import responses
from kubernetes.client import ApiException
from prometheus_client import REGISTRY

from _fakes import FakeAppsV1Api, FakeCustomObjectsApi, make_emit, tick_failures_total
from _fakes import make_cr as _shared_make_cr
from openstudio_operator._k8s import (
    DEFAULT_WORKER_DEPLOYMENT,
    MERGE_PATCH_CONTENT_TYPE,
    RESTARTED_AT_ANNOTATION,
)
from openstudio_operator.config import OperatorConfig
from openstudio_operator.events import EventEmitter
from openstudio_operator.handlers import worker_recycler as worker_recycler_module
from openstudio_operator.handlers.worker_recycler import (
    TRIGGER_ANALYSIS_COMPLETED,
    TRIGGER_INTERVAL_ELAPSED,
    WORKER_RECYCLED_EVENT,
    run_recycler_tick,
)
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.status_store import StatusStore, StatusStoreConflictError

BASE = "http://web.test"
NAMESPACE = "openstudio-server"
NAME = "oscm"
WORKER = "worker-recycle-target"
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

SPEC = {
    "serverUrl": BASE,
    "targetWorkerDeployment": WORKER,
    "workerPolicy": {
        "recycleWorkerIntervalHours": 12,
        "recycleAfterAnalysis": True,
        "minRecycleIntervalMinutes": 30,
    },
}

# Shared-fake binding (issue #474): this module's make_cr default spec.
make_cr = partial(_shared_make_cr, default_spec=SPEC)


def analyses_payload(*statuses: str) -> list[dict]:
    return [
        {"_id": f"a-{index}", "status": status, "created_at": "2026-08-01T00:00:00Z"}
        for index, status in enumerate(statuses)
    ]


def workers_recycled_total() -> float:
    """Sum across all label series of ``workers_recycled_total``.

    Issue #309 — the counter gained a ``trigger`` label, so the
    prometheus-registry sample lookup has to sum every labelled series
    (``{trigger=...}``) rather than reading the unlabelled ``_value``.
    Cribbed from
    ``tests/test_metrics_endpoint.py::_counter_total``'s labelled branch.
    """
    counter = REGISTRY.get_sample_value
    total = 0.0
    for trigger in (
        "analysis-completed",
        "interval-elapsed",
    ):
        val = counter(
            "openstudio_operator_workers_recycled_total",
            {"trigger": trigger},
        )
        total += val or 0.0
    return total


def register_analyses(payload: list[dict]) -> None:
    responses.get(f"{BASE}/analyses.json", json=payload)


def tick(api, apps=None, spec=None, *, now=NOW, client=None):
    store = StatusStore(NAMESPACE, NAME, api)
    config = OperatorConfig.from_spec(spec if spec is not None else SPEC)
    events, emit = make_emit()
    trigger = run_recycler_tick(
        client if client is not None else OpenStudioClient(BASE),
        store,
        config,
        apps if apps is not None else FakeAppsV1Api(),
        namespace=NAMESPACE,
        now=now,
        emit=emit,
    )
    return trigger, events


def expected_patch(now: datetime = NOW) -> dict:
    return {
        "spec": {
            "template": {
                "metadata": {"annotations": {RESTARTED_AT_ANNOTATION: now.isoformat()}}
            }
        }
    }


# --- Trigger 1: analysis completed, none started, gate open -------------------


@responses.activate
def test_trigger_1_fires_on_completed_with_no_started():
    api = FakeCustomObjectsApi(make_cr())
    register_analyses(analyses_payload("completed", "queued"))
    apps = FakeAppsV1Api()
    metric_before = workers_recycled_total()

    trigger, events = tick(api, apps)

    assert trigger == TRIGGER_ANALYSIS_COMPLETED
    assert len(apps.patches) == 1
    patch = apps.patches[0]
    assert patch["name"] == WORKER
    assert patch["namespace"] == NAMESPACE
    assert patch["kwargs"]["_content_type"] == MERGE_PATCH_CONTENT_TYPE
    assert patch["body"] == expected_patch()
    assert api.obj["status"]["lastRecycleAt"] == NOW.isoformat()
    assert len(events) == 1
    event_type, reason, message = events[0]
    assert event_type == "Normal"
    assert reason == WORKER_RECYCLED_EVENT
    assert WORKER in message and "suppressed" not in message
    assert workers_recycled_total() - metric_before == 1


@responses.activate
def test_started_analysis_blocks_trigger_1():
    # Gate open (2h > 30m cooldown) and interval NOT elapsed (2h < 12h):
    # only trigger 1 is in play, and the started analysis disarms it.
    api = FakeCustomObjectsApi(make_cr(status={"lastRecycleAt": (NOW - timedelta(hours=2)).isoformat()}))
    register_analyses(analyses_payload("completed", "started"))
    apps = FakeAppsV1Api()

    trigger, events = tick(api, apps)

    assert trigger is None
    assert apps.patches == []
    assert events == []
    assert api.obj["status"]["lastRecycleAt"] == (NOW - timedelta(hours=2)).isoformat()


@responses.activate
def test_no_completed_analysis_no_interval_elapsed_does_nothing():
    api = FakeCustomObjectsApi(make_cr(status={"lastRecycleAt": NOW.isoformat()}))
    register_analyses(analyses_payload("started", "queued"))
    apps = FakeAppsV1Api()

    trigger, events = tick(api, apps)

    assert trigger is None
    assert apps.patches == []
    assert events == []


@responses.activate
def test_recycle_after_analysis_disabled_disarms_trigger_1():
    spec = {
        **SPEC,
        "workerPolicy": {**SPEC["workerPolicy"], "recycleAfterAnalysis": False},
    }
    api = FakeCustomObjectsApi(make_cr(spec, status={"lastRecycleAt": NOW.isoformat()}))
    register_analyses(analyses_payload("completed"))
    apps = FakeAppsV1Api()

    trigger, events = tick(api, apps, spec=spec)

    assert trigger is None
    assert apps.patches == []
    assert events == []


@responses.activate
def test_empty_target_falls_back_to_chart_worker_deployment_name():
    api = FakeCustomObjectsApi(make_cr({k: v for k, v in SPEC.items() if k != "targetWorkerDeployment"}))
    register_analyses(analyses_payload("completed"))
    apps = FakeAppsV1Api()

    trigger, _ = tick(api, apps, spec={k: v for k, v in SPEC.items() if k != "targetWorkerDeployment"})

    assert trigger == TRIGGER_ANALYSIS_COMPLETED
    assert apps.patches[0]["name"] == DEFAULT_WORKER_DEPLOYMENT


# --- Trigger 2: interval elapsed ------------------------------------------------


@responses.activate
def test_trigger_2_fires_when_interval_elapsed_since_last_recycle():
    last = NOW - timedelta(hours=12, minutes=1)
    api = FakeCustomObjectsApi(make_cr(status={"lastRecycleAt": last.isoformat()}))
    register_analyses(analyses_payload())  # nothing completed; nothing started
    apps = FakeAppsV1Api()

    trigger, events = tick(api, apps)

    assert trigger == TRIGGER_INTERVAL_ELAPSED
    assert len(apps.patches) == 1
    assert apps.patches[0]["body"] == expected_patch()
    assert api.obj["status"]["lastRecycleAt"] == NOW.isoformat()
    assert len(events) == 1


@responses.activate
def test_interval_not_elapsed_no_completed_does_nothing():
    api = FakeCustomObjectsApi(make_cr(status={"lastRecycleAt": (NOW - timedelta(hours=6)).isoformat()}))
    register_analyses(analyses_payload())
    apps = FakeAppsV1Api()

    trigger, events = tick(api, apps)

    assert trigger is None
    assert apps.patches == []
    assert events == []


@responses.activate
def test_never_recycled_counts_as_infinitely_elapsed():
    api = FakeCustomObjectsApi(make_cr())
    register_analyses(analyses_payload())  # zero analyses at all
    apps = FakeAppsV1Api()

    trigger, _ = tick(api, apps)

    assert trigger == TRIGGER_INTERVAL_ELAPSED
    assert len(apps.patches) == 1
    assert api.obj["status"]["lastRecycleAt"] == NOW.isoformat()


# --- THE GATE: single decision point ---------------------------------------------


@responses.activate
def test_gate_closed_blocks_trigger_1_without_even_polling():
    api = FakeCustomObjectsApi(make_cr(status={"lastRecycleAt": (NOW - timedelta(minutes=5)).isoformat()}))
    # No /analyses.json response registered: any attempt to poll would raise.
    apps = FakeAppsV1Api()

    trigger, events = tick(api, apps)

    assert trigger is None
    assert apps.patches == []
    assert events == []
    assert len(responses.calls) == 0


@responses.activate
def test_gate_closed_blocks_trigger_2():
    last = NOW - timedelta(hours=13)  # interval (12h) long elapsed…
    spec = {
        **SPEC,
        "workerPolicy": {**SPEC["workerPolicy"], "minRecycleIntervalMinutes": 24 * 60},
    }
    api = FakeCustomObjectsApi(make_cr(spec, status={"lastRecycleAt": last.isoformat()}))
    register_analyses(analyses_payload())
    apps = FakeAppsV1Api()

    # …but the cooldown (24h) has not elapsed (13h ≤ 24h): gate closed wins.
    trigger, events = tick(api, apps, spec=spec)

    assert trigger is None
    assert apps.patches == []
    assert events == []


@responses.activate
def test_gate_expiry_allows_recycle_again():
    api = FakeCustomObjectsApi(make_cr(status={"lastRecycleAt": (NOW - timedelta(minutes=31)).isoformat()}))
    register_analyses(analyses_payload("completed"))
    register_analyses(analyses_payload("completed"))  # second tick's poll
    apps = FakeAppsV1Api()

    trigger, _ = tick(api, apps)

    assert trigger == TRIGGER_ANALYSIS_COMPLETED
    assert api.obj["status"]["lastRecycleAt"] == NOW.isoformat()


@responses.activate
def test_gate_boundary_exactly_at_cooldown_stays_closed():
    api = FakeCustomObjectsApi(make_cr(status={"lastRecycleAt": (NOW - timedelta(minutes=30)).isoformat()}))
    # No registered response: a poll would raise — proving the gate said no first.
    apps = FakeAppsV1Api()

    trigger, _ = tick(api, apps)

    assert trigger is None
    assert apps.patches == []
    assert len(responses.calls) == 0


# --- Single decision point: burst of completions → exactly one recycle ----------


@responses.activate
def test_multiple_completions_in_quick_succession_recycle_exactly_once():
    api = FakeCustomObjectsApi(make_cr())
    # Burst: three analyses complete across consecutive ticks minutes apart.
    register_analyses(analyses_payload("completed"))
    register_analyses(analyses_payload("completed", "completed"))
    register_analyses(analyses_payload("completed", "completed", "completed"))
    apps = FakeAppsV1Api()
    metric_before = workers_recycled_total()

    t1 = NOW
    t2 = NOW + timedelta(minutes=2)
    t3 = NOW + timedelta(minutes=7)
    trigger1, events1 = tick(api, apps, now=t1)
    trigger2, events2 = tick(api, apps, now=t2)
    trigger3, events3 = tick(api, apps, now=t3)

    assert trigger1 == TRIGGER_ANALYSIS_COMPLETED
    assert trigger2 is None
    assert trigger3 is None
    assert len(apps.patches) == 1  # ONE rolling restart for the whole burst
    assert apps.patches[0]["body"] == expected_patch(t1)
    assert len(events1) == 1
    assert events2 == [] and events3 == []
    assert workers_recycled_total() - metric_before == 1
    assert api.obj["status"]["lastRecycleAt"] == t1.isoformat()


@responses.activate
def test_burst_then_gate_expiry_recycles_again_at_next_window():
    api = FakeCustomObjectsApi(make_cr())
    register_analyses(analyses_payload("completed"))
    register_analyses(analyses_payload("completed"))  # still just completed, later tick
    apps = FakeAppsV1Api()

    trigger1, _ = tick(api, apps, now=NOW)
    trigger2, _ = tick(api, apps, now=NOW + timedelta(minutes=31))

    assert trigger1 == TRIGGER_ANALYSIS_COMPLETED
    # The level-based approximation re-arms after the gate window — bounded
    # to one recycle per minRecycleIntervalMinutes by design (documented).
    assert trigger2 == TRIGGER_ANALYSIS_COMPLETED
    assert len(apps.patches) == 2
    assert apps.patches[1]["body"] == expected_patch(NOW + timedelta(minutes=31))


# --- Cooldown survives operator restarts (D04) -----------------------------------


@responses.activate
def test_restart_mid_cooldown_honors_persisted_last_recycle_at():
    """Fresh client/store/apps (new process state), same persisted CR."""
    api = FakeCustomObjectsApi(make_cr(status={"lastRecycleAt": (NOW - timedelta(minutes=10)).isoformat()}))
    register_analyses(analyses_payload("completed"))
    apps = FakeAppsV1Api()

    trigger, events = tick(
        api, apps, now=NOW, client=OpenStudioClient(BASE)  # "process 2": fresh everything
    )

    assert trigger is None
    assert apps.patches == []
    assert events == []
    assert len(responses.calls) == 0  # gate decided before any poll


@responses.activate
def test_restart_after_cooldown_recycles_from_persisted_state():
    api = FakeCustomObjectsApi(make_cr(status={"lastRecycleAt": (NOW - timedelta(hours=13)).isoformat()}))
    register_analyses(analyses_payload("completed"))
    apps = FakeAppsV1Api()

    trigger, _ = tick(api, apps, now=NOW, client=OpenStudioClient(BASE))

    assert trigger == TRIGGER_ANALYSIS_COMPLETED
    assert len(apps.patches) == 1


# --- Mechanism: annotation patch, never deletes, no scratch-clearing -------------


@responses.activate
def test_patch_payload_is_restarted_at_annotation_on_pod_template():
    api = FakeCustomObjectsApi(make_cr())
    register_analyses(analyses_payload("completed"))
    apps = FakeAppsV1Api()

    tick(api, apps)

    assert len(apps.patches) == 1
    body = apps.patches[0]["body"]
    assert set(body) == {"spec"}
    assert set(body["spec"]) == {"template"}
    assert body["spec"]["template"]["metadata"]["annotations"] == {
        RESTARTED_AT_ANNOTATION: NOW.isoformat()
    }
    # The fake exposes no delete path at all — the recycler never calls one.
    assert not hasattr(apps, "delete_namespaced_deployment")
    assert not hasattr(apps, "delete_namespaced_pod")


def test_no_scratch_clearing_code_exists():
    """D08 grep-proofing: the module contains no clearing/rm/prune machinery.

    Word-boundary patterns so legit tokens (``spec.template``) don't match.
    """
    import inspect
    import re

    from openstudio_operator.handlers import worker_recycler

    source = inspect.getsource(worker_recycler)
    banned = (
        r"\btemp\b",
        r"\btempfile\b",
        r"\btmp\b",
        r"rm\s+-rf",
        r"\bshutil\b",
        r"\bsubprocess\b",
        r"\bos\.remove\b",
        r"\bos\.system\b",
        r"\brmtree\b",
    )
    for pattern in banned:
        assert not re.search(pattern, source), f"banned scratch-clearing token /{pattern}/ in module"


# --- dryRun (D11) -----------------------------------------------------------------


@responses.activate
def test_dry_run_suppresses_patch_marks_event_and_advances_last_recycle_at():
    spec = {**SPEC, "dryRun": True}
    api = FakeCustomObjectsApi(make_cr(spec))
    register_analyses(analyses_payload("completed"))
    register_analyses(analyses_payload("completed"))  # second tick's poll
    apps = FakeAppsV1Api()
    metric_before = workers_recycled_total()

    trigger1, events1 = tick(api, apps, spec=spec)
    trigger2, events2 = tick(api, apps, spec=spec, now=NOW + timedelta(minutes=2))

    assert trigger1 == TRIGGER_ANALYSIS_COMPLETED
    assert trigger2 is None  # lastRecycleAt advanced in dry-run: paced like a real run
    assert apps.patches == []  # mutation suppressed
    assert len(events1) == 1
    event_type, reason, message = events1[0]
    assert event_type == "Normal"
    assert reason == WORKER_RECYCLED_EVENT
    assert "spec.dryRun" in message and "suppressed" in message
    assert events2 == []
    assert workers_recycled_total() - metric_before == 1
    assert api.obj["status"]["lastRecycleAt"] == NOW.isoformat()


# --- Issue #232: kopf 1.4x MappingView body end-to-end through EventEmitter -----


@responses.activate
def test_run_recycler_tick_accepts_mappingview_body_in_event_emitter():
    """Issue #232 — MappingView (types.MappingProxyType) body end-to-end.

    kopf >=1.4x delivers ``body`` to timer handlers as a MappingView subclass
    (not a dict subclass). This regression test wires
    ``types.MappingProxyType`` into ``EventEmitter``, drives
    ``run_recycler_tick`` through its recycle emit path, and asserts:

    1. ``EventEmitter.dry_run`` gate still records the suppression through
       ``EventEmitter.suppressed_count`` (D11 end-to-end);
    2. ``body.get('metadata')`` introspection keeps working — the access
       shape ``kopf.event`` and any status-update helper rely on.

    No production code change; the test fails the day a kopf upgrade makes
    the body shape incompatible with ``EventEmitter``. Scope guard: handler /
    guard untouched.
    """
    spec = {**SPEC, "dryRun": True}
    api = FakeCustomObjectsApi(make_cr(spec))
    apps = FakeAppsV1Api()
    register_analyses(analyses_payload("completed"))

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

    trigger = run_recycler_tick(
        OpenStudioClient(BASE),
        store,
        config,
        apps,
        namespace=NAMESPACE,
        now=NOW,
        emit=emitter,
    )

    assert trigger == TRIGGER_ANALYSIS_COMPLETED
    # Dry-run gate still records exactly the one WorkerRecycled Normal Event;
    # the Deployment patch is suppressed (mutation off, Event on).
    assert apps.patches == []
    assert emitter.suppressed_count == 1
    assert emitter.dry_run is True


# --- Issue #467 — tick-level error/recovery paths (D12) -------------------------
#
# The wrapper-level exception TUPLE is pinned by test_timer_wrapper_failures.py
# (issue #231) with a MOCKED run_recycler_tick. These tests close the remaining
# gap: real errors arising INSIDE run_recycler_tick — REST 5xx through the real
# client's retry envelope, real 409s through the real StatusStore RMW, and a
# raw kubernetes ApiException from the Deployment-patch seam. The worker
# recycler's wrapper catches the 3-tuple (OpenStudioApiError, StatusStoreError,
# ApiException) — wider than datapoint_watchdog's 2-tuple (#493 owns
# standardizing) — so every error here lands in the skip-tick branch.


class ConflictingFakeCustomObjectsApi(FakeCustomObjectsApi):
    """Merge-patch fake whose first ``patch_conflicts`` patches raise 409.

    Same synthetic-409 approach as tests/test_status_store.py — exercised here
    through the full tick so the store's bounded retry is observed from the
    recycler's point of view, not the store's.
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


class ExplodingAppsV1Api:
    """``patch_namespaced_deployment`` always raises (K8s API-server down).

    Mirrors ExplodingPatchFakeCustomObjectsApi in test_datapoint_watchdog.py,
    but on the Deployment-patch seam instead of the status seam.
    """

    def __init__(self) -> None:
        self.patch_attempts = 0

    def patch_namespaced_deployment(self, name, namespace, body, **kwargs):
        self.patch_attempts += 1
        raise ApiException(status=500, reason="Internal Server Error")


class TimeoutAppsV1Api:
    """``patch_namespaced_deployment`` always raises a timeout ApiException.

    Issue #796 — the BoundedK8sRequest wrapper translates urllib3
    timeout errors into ApiException(status=0, reason="K8s API request timed out").
    This class simulates that path through the actual handler tick function.
    """

    def __init__(self) -> None:
        self.patch_attempts = 0

    def patch_namespaced_deployment(self, name, namespace, body, **kwargs):
        self.patch_attempts += 1
        raise ApiException(
            status=0,
            reason="K8s API request timed out (bounded at 15s by K8S_REQUEST_TIMEOUT_SECONDS, issue #579): ReadTimeoutError: HTTPConnectionPool(host='apiserver.incluster.example', port=6443): Read timed out. (read timeout=15)",
        )


def analyses_calls() -> int:
    return sum(
        1 for call in responses.calls if call.request.url.startswith(f"{BASE}/analyses.json")
    )


def call_wrapper(monkeypatch, api=None, apps=None):
    """Invoke the production kopf timer wrapper directly (same direct-call
    pattern as test_timer_wrapper_failures.py) with both k8s seams stubbed."""
    if api is None:
        api = FakeCustomObjectsApi(make_cr())
    monkeypatch.setattr(
        "openstudio_operator.handlers.worker_recycler.operator_custom_objects_api",
        lambda: api,
    )
    monkeypatch.setattr(
        "openstudio_operator.handlers.worker_recycler.operator_apps_api",
        lambda: apps if apps is not None else FakeAppsV1Api(),
    )
    return worker_recycler_module.worker_recycler(
        body=make_cr(),
        spec=SPEC,
        namespace=NAMESPACE,
        name=NAME,
        logger=logging.getLogger("test"),
    )


@responses.activate
def test_sustained_503_on_list_analyses_exhausts_client_retries_and_wrapper_skips_tick(
    monkeypatch, caplog
):
    """Issue #467 (a): GET /analyses.json 503s on every attempt → the client's
    GET-only 3× retry envelope exhausts (4 attempts) → OpenStudioApiError raises
    out of run_recycler_tick → the wrapper swallows it, bumps
    HANDLER_TICK_FAILURES_TOTAL with all four labels, logs the skip-tick
    warning, and returns None (D12: the next poll retries naturally)."""
    responses.get(f"{BASE}/analyses.json", status=503)
    client_sleeps: list[float] = []
    monkeypatch.setattr("openstudio_operator.openstudio_client._sleep", client_sleeps.append)
    api = FakeCustomObjectsApi(make_cr())  # gate open, tick dies at the poll
    apps = FakeAppsV1Api()

    before = tick_failures_total(NAMESPACE, NAME, "worker_recycler", "OpenStudioApiError")
    result = call_wrapper(monkeypatch, api=api, apps=apps)
    after = tick_failures_total(NAMESPACE, NAME, "worker_recycler", "OpenStudioApiError")

    assert result is None, "wrapper must swallow OpenStudioApiError and return None (D12)"
    # max_retries=3 → 4 total GET attempts on /analyses.json, all 503.
    assert analyses_calls() == 4
    # Jittered backoff sleeps before each RETRY (not before the first attempt).
    assert len(client_sleeps) == 3
    # after/before both read the exact 4-label sample — the delta == 1 IS the
    # label-exactness proof (a wrong namespace/name/module/error_type reads 0.0).
    assert after - before == 1.0
    assert "worker recycler tick skipped, retrying next poll (OpenStudioApiError" in caplog.text
    # D04 recovery anchor: the tick died before any mutation or status write.
    assert apps.patches == []
    assert api.patch_calls == 0
    assert api.obj["status"] == {}


@responses.activate
def test_status_409_during_set_last_recycle_at_resolved_by_store_bounded_retry(monkeypatch):
    """Issue #467 (b): one 409 on the anchor write (``set_last_recycle_at``)
    → StatusStore re-reads and re-applies internally → the tick completes with
    the recycle recorded. No exception; exactly one landed patch."""
    store_sleeps: list[float] = []
    monkeypatch.setattr("openstudio_operator.status_store._sleep", store_sleeps.append)
    api = ConflictingFakeCustomObjectsApi(make_cr(), patch_conflicts=1)
    register_analyses(analyses_payload("completed"))
    apps = FakeAppsV1Api()

    trigger, events = tick(api, apps)

    assert trigger == TRIGGER_ANALYSIS_COMPLETED
    assert api.conflicts_seen == 1
    # One backoff sleep inside the store's retry, then success.
    assert len(store_sleeps) == 1
    # The conflict was seen by the fake but not counted as a landed patch.
    assert api.patch_calls == 1
    assert len(apps.patches) == 1
    assert api.obj["status"]["lastRecycleAt"] == NOW.isoformat()
    assert len(events) == 1
    assert events[0][:2] == ("Normal", WORKER_RECYCLED_EVENT)


@responses.activate
def test_api_exception_from_deployment_patch_is_caught_by_wrapper(monkeypatch, caplog):
    """Issue #467 (c) — pins CURRENT behavior; #493 owns any change.

    A raw kubernetes ApiException from the Deployment-patch seam
    (rolling_restart_deployment → patch_namespaced_deployment, K8s API-server
    down) raises out of run_recycler_tick. Unlike datapoint_watchdog's
    2-tuple wrapper, this wrapper's except tuple includes ApiException, so it
    is caught: skip-tick warning, HANDLER_TICK_FAILURES_TOTAL bump with
    error_type="ApiException", clean None return. The restart was ATTEMPTED
    but never anchored — next tick re-attempts it (D12)."""
    register_analyses(analyses_payload("completed"))
    apps = ExplodingAppsV1Api()
    api = FakeCustomObjectsApi(make_cr())
    metric_before = workers_recycled_total()

    before = tick_failures_total(NAMESPACE, NAME, "worker_recycler", "ApiException")
    result = call_wrapper(monkeypatch, api=api, apps=apps)
    after = tick_failures_total(NAMESPACE, NAME, "worker_recycler", "ApiException")

    assert result is None, "wrapper must swallow ApiException and return None (3-tuple)"
    assert apps.patch_attempts == 1
    assert after - before == 1.0
    assert "worker recycler tick skipped, retrying next poll (ApiException" in caplog.text
    # The patch raised BEFORE the Event/counter/anchor lines ran.
    assert api.patch_calls == 0
    assert api.obj["status"] == {}
    assert workers_recycled_total() - metric_before == 0


@responses.activate
def test_timeout_api_exception_from_deployment_patch_is_caught_by_wrapper(monkeypatch, caplog):
    """Issue #796 — integration test: timeout ApiException through the real tick function.

    The BoundedK8sRequest wrapper translates urllib3 timeout errors into
    ApiException(status=0, reason="K8s API request timed out..."). This test
    verifies the full end-to-end path: a FakeAppsV1Api that raises this
    timeout ApiException, called through the real run_recycler_tick function,
    must result in the skip counter being incremented and no exception escaping.
    """
    register_analyses(analyses_payload("completed"))
    apps = TimeoutAppsV1Api()
    api = FakeCustomObjectsApi(make_cr())

    before = tick_failures_total(NAMESPACE, NAME, "worker_recycler", "ApiException")
    result = call_wrapper(monkeypatch, api=api, apps=apps)
    after = tick_failures_total(NAMESPACE, NAME, "worker_recycler", "ApiException")

    assert result is None, "wrapper must swallow timeout ApiException and return None"
    assert apps.patch_attempts == 1
    assert after - before == 1.0, "HANDLER_TICK_FAILURES_TOTAL must be incremented for ApiException"
    assert "worker recycler tick skipped, retrying next poll (ApiException" in caplog.text
    assert "timed out" in caplog.text, "timeout reason must be logged"


@responses.activate
def test_sustained_anchor_409_after_restart_fired_raises_benign_delete_then_anchor_race(
    monkeypatch,
):
    """Issue #467 (d) — pins CURRENT behavior; the module docstring documents
    this as the accepted delete-then-anchor race (D12, worker_recycler.py:36-40:
    "Patch before anchor: if the anchor write fails, the next tick re-attempts
    one (harmless) extra rolling restart").

    Sustained 409s past MAX_CONFLICT_RETRIES → StatusStoreConflictError raises
    out of run_recycler_tick (a StatusStoreError subclass, so the wrapper's
    catch turns it into a skip-tick, not a crash). The restart FIRED but the
    anchor never recorded — proving the race window is benign: the next tick
    reads the missing lastRecycleAt as an open gate and re-fires exactly one
    extra rolling restart, then anchors successfully."""
    monkeypatch.setattr("openstudio_operator.status_store._sleep", lambda _s: None)
    api = ConflictingFakeCustomObjectsApi(make_cr(), patch_conflicts=99)
    register_analyses(analyses_payload("completed"))
    register_analyses(analyses_payload("completed"))  # next tick's poll
    apps = FakeAppsV1Api()

    with pytest.raises(StatusStoreConflictError, match="409"):
        tick(api, apps)

    # The restart happened before the failed anchor write; nothing was anchored.
    assert len(apps.patches) == 1
    assert api.conflicts_seen == 5  # MAX_CONFLICT_RETRIES bound observed
    assert "lastRecycleAt" not in api.obj["status"]

    # Recovery: conflicts settle → the unanchored recycle re-fires once (the
    # documented harmless extra restart), then the anchor lands and the gate
    # closes behind it.
    api.remaining_conflicts = 0
    trigger, events = tick(api, apps)

    assert trigger == TRIGGER_ANALYSIS_COMPLETED
    assert len(apps.patches) == 2
    assert api.obj["status"]["lastRecycleAt"] == NOW.isoformat()
    assert len(events) == 1
