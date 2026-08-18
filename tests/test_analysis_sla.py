"""Unit tests for the analysis SLA monitor: soft-stop core (#8) + escalation (#9).

REST is mocked with ``responses`` and the CR ``.status`` subresource with an
in-memory RFC 7386 merge-patch fake (same approach as test_status_store.py) —
no dependencies beyond the ``[dev]`` extra. The Kubernetes side of the
escalation is mocked with ``FakeAppsV1Api``/``FakeCoreV1Api`` built on the
same generated-client shapes the real APIs return (attribute-style models).
All assertions target ``run_sla_tick`` directly; the kopf timer wrapper is
thin wiring.

RBAC note: the pod deletes asserted here are covered by the ``pods``
``get/list/watch/delete`` verbs granted to the operator's namespaced Role in
``deploy/rbac.yaml`` (added in #3 specifically for this escalation).
"""

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import responses
from prometheus_client import REGISTRY

from openstudio_operator.config import OperatorConfig
from openstudio_operator.handlers.analysis_sla import (
    ANALYSIS_ESCALATED_EVENT,
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
GRACE_MINUTES = 15
WITHIN_GRACE = NOW - timedelta(minutes=GRACE_MINUTES - 1)
PAST_GRACE = NOW - timedelta(minutes=GRACE_MINUTES + 1)

SPEC = {
    "serverUrl": BASE,
    "analysisPolicy": {"maxDurationMinutes": 180, "gracefulStopTimeoutMinutes": GRACE_MINUTES},
}

WORKER_LABELS = {"app.kubernetes.io/name": "openstudio-server", "component": "worker"}


def make_cr(spec: dict | None = None, status: dict | None = None) -> dict:
    return {
        "apiVersion": "energy.nrel.gov/v1alpha1",
        "kind": "OpenStudioClusterManager",
        "metadata": {"name": NAME, "namespace": NAMESPACE},
        "spec": copy.deepcopy(spec if spec is not None else SPEC),
        "status": copy.deepcopy(status if status is not None else {}),
    }


def anchored_status(
    analysis_id: str, issued_at: datetime, *, escalated_at: datetime | None = None
) -> dict:
    """A CR status carrying a persisted softStops anchor (operator restart state)."""
    record = {"issuedAt": issued_at.isoformat(), "outcome": "issued"}
    if escalated_at is not None:
        record["escalatedAt"] = escalated_at.isoformat()
        record["escalationOutcome"] = "evicted"
    return {"softStops": {analysis_id: record}}


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


def pods_evicted_total() -> float:
    return REGISTRY.get_sample_value("openstudio_operator_worker_pods_evicted_total") or 0.0


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
    responses.get(
        f"{BASE}/analyses/{analysis_id}/soft_stop", status=200, json={"result": "accepted"}
    )


def make_pod(name: str, ip: str | None, labels: dict | None = None):
    """Generated-client pod shape, attribute-style (V1Pod duck type)."""
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name, labels=dict(labels if labels is not None else WORKER_LABELS)
        ),
        status=SimpleNamespace(pod_ip=ip),
    )


def _label_selector_term_matches(labels: dict, term: str) -> bool:
    """AND-intersection of every term in the label selector.

    Honors the subset of the Kubernetes label-selector grammar that the
    issue #44 ``deployment_label_selector`` helper emits: ``k=v``,
    ``k in (v1,v2)``, ``k notin (v1,v2)``, bare ``k`` (Exists), and
    ``!k`` (DoesNotExist). Other terms (e.g. ``Gt``, ``Lt``) silently drop
    in the fake — the helper falls back to matchLabels-only in that case
    anyway, so the test surface stays small.
    """
    term = term.strip()
    if not term:
        return True
    if term.startswith("!"):
        return term[1:] not in labels
    if " notin " in term:
        # Grammar: "k notin (v1,v2)" → split on " notin "
        key, _, rest = term.partition(" notin ")
        vs = rest.strip().strip("()").split(",")
        return labels.get(key) not in vs
    if " in " in term:
        # Grammar: "k in (v1,v2)" → split on " in " (substring, not space)
        key, _, rest = term.partition(" in ")
        vs = rest.strip().strip("()").split(",")
        return labels.get(key) in vs
    if "=" in term:
        key, _, value = term.partition("=")
        return labels.get(key) == value
    return term in labels


class FakeCoreV1Api:
    """CoreV1Api stand-in: label-filtered pod list + recorded deletes.

    Honors ``label_selector`` exactly like the real API so tests can prove
    non-worker pods are never even candidates. Supports the full label-
    selector grammar the operator emits (matchLabels terms + the four
    matchExpressions operators ``In``/``NotIn``/``Exists``/``DoesNotExist``)
    so the issue #44 matchExpressions path is exercised end-to-end. The
    ``delete`` exercised via this fake is the ``pods`` delete verb from
    deploy/rbac.yaml (#3).
    """

    def __init__(self, pods: list) -> None:
        self.pods = pods
        self.list_calls: list[dict] = []
        self.deletes: list[dict] = []

    def list_namespaced_pod(self, namespace, label_selector=None, **kwargs):
        self.list_calls.append(
            {"namespace": namespace, "label_selector": label_selector, "kwargs": kwargs}
        )
        wanted = (label_selector or "").split(",") if label_selector else []
        items = [
            pod
            for pod in self.pods
            if all(_label_selector_term_matches(pod.metadata.labels, t) for t in wanted)
        ]
        return SimpleNamespace(items=items)

    def delete_namespaced_pod(self, name, namespace, **kwargs):
        self.deletes.append({"name": name, "namespace": namespace, "kwargs": kwargs})
        return {}


class FakeAppsV1Api:
    """AppsV1Api stand-in serving one Deployment's pod-template selector.

    Honors both ``matchLabels`` AND ``matchExpressions`` (issue #44 gap fix).
    Pass either or both via the constructor; the helper under test must
    translate both into the Kubernetes label-selector grammar and intersect
    them when both are set.
    """

    def __init__(
        self,
        match_labels: dict | None = None,
        *,
        match_expressions: list[SimpleNamespace] | None = None,
        name: str = "worker",
    ) -> None:
        self.match_labels = dict(match_labels if match_labels is not None else WORKER_LABELS)
        self.match_expressions = list(match_expressions if match_expressions is not None else [])
        self.reads: list[dict] = []
        self.name = name

    def read_namespaced_deployment(self, name, namespace, **kwargs):
        self.reads.append({"name": name, "namespace": namespace, "kwargs": kwargs})
        return SimpleNamespace(
            spec=SimpleNamespace(
                selector=SimpleNamespace(
                    match_labels=self.match_labels,
                    match_expressions=self.match_expressions,
                )
            )
        )


def tick(api, spec=None, client=None, *, pod_api=None, apps_api=None, now=NOW):
    store = StatusStore(NAMESPACE, NAME, api)
    config = OperatorConfig.from_spec(spec if spec is not None else SPEC)
    events, emit = make_emit()
    result = run_sla_tick(
        client if client is not None else OpenStudioClient(BASE),
        store,
        config,
        now=now,
        emit=emit,
        namespace=NAMESPACE,
        pod_api=pod_api,
        apps_api=apps_api,
    )
    return result, events


# --- One-shot semantics ------------------------------------------------------


@responses.activate
def test_soft_stop_fires_exactly_once_across_ticks():
    api = FakeCustomObjectsApi(make_cr())
    register_started_analysis("a1", NOW - timedelta(hours=4))
    register_started_analysis("a1", NOW - timedelta(hours=4))  # second tick's poll
    metric_before = soft_stops_total()

    result, events = tick(api)
    result2, events2 = tick(api)

    assert result.soft_stopped == ["a1"]
    assert result2.soft_stopped == []
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
    result, events = tick(api, client=OpenStudioClient(BASE))  # "process 2"

    assert result.soft_stopped == []
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
        json={
            "analysis": {
                "status": "started",
                "start_time": (NOW - timedelta(minutes=5)).isoformat(),
            }
        },
    )
    responses.get(
        f"{BASE}/analyses/a-long/page_data.json",
        json={
            "analysis": {"status": "started", "start_time": (NOW - timedelta(hours=4)).isoformat()}
        },
    )
    responses.get(f"{BASE}/analyses/a-long/soft_stop", status=200, json={"result": "accepted"})

    result, events = tick(FakeCustomObjectsApi(make_cr()))

    assert result.soft_stopped == ["a-long"]
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
        json={
            "analysis": {
                "status": "started",
                "start_time": (NOW - timedelta(minutes=180)).isoformat(),
            }
        },
    )
    api = FakeCustomObjectsApi(make_cr())

    result, events = tick(api)

    assert result.soft_stopped == []
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

    result, events = tick(api)

    assert result.soft_stopped == []
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

    result, events = tick(api, spec={**SPEC, "dryRun": True})
    result2, events2 = tick(api, spec={**SPEC, "dryRun": True})

    assert result.soft_stopped == ["a1"]
    assert result2.soft_stopped == []
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

    result, events = tick(api)

    assert result.soft_stopped == []
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

    result, events = tick(api, spec=spec)

    assert result.soft_stopped == []
    assert events == []
    assert calls_to("/page_data.json") == 0
    assert calls_to("/soft_stop") == 0
    assert "softStops" not in api.obj["status"]


# --- Grace wait + escalation (#9) -----------------------------------------------


def register_datapoints(docs: list[dict]) -> None:
    responses.get(f"{BASE}/data_points.json", json=docs)


def started_dps_payload(analysis_id: str, *ips: str) -> list[dict]:
    """Full-doc datapoints: started dps with ips + decoys (completed dp, other analysis)."""
    docs = [
        {
            "_id": f"dp-{analysis_id}-{ip}",
            "analysis_id": analysis_id,
            "status": "started",
            "ip_address": ip,
        }
        for ip in ips
    ]
    docs.append(
        {
            "_id": "dp-done",
            "analysis_id": analysis_id,
            "status": "completed",
            "ip_address": "10.9.9.9",
        }
    )
    docs.append(
        {
            "_id": "dp-other",
            "analysis_id": "someone-else",
            "status": "started",
            "ip_address": "10.8.8.8",
        }
    )
    return docs


def register_stuck_analysis(analysis_id: str, dps: list[dict] | None = None) -> None:
    """Analyses poll with the analysis still `started`; anchored, so no page_data needed."""
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": analysis_id, "status": "started", "created_at": TEN_DAYS_AGO}],
    )
    if dps is not None:
        register_datapoints(dps)


@responses.activate
def test_grace_not_yet_elapsed_waits_even_after_restart():
    """Persisted anchor 14m old (grace 15m), fresh handler objects → no escalation.

    Also the restart-mid-grace clock test for the waiting side: the grace is
    measured from the ORIGINAL anchor timestamp in the CR, never from
    operator (re)start time — so a restart never shortens NOR lengthens it.
    """
    api = FakeCustomObjectsApi(make_cr(status=anchored_status("a1", WITHIN_GRACE)))
    register_stuck_analysis("a1")
    pod_api = FakeCoreV1Api([make_pod("worker-1", "10.0.0.1")])
    apps = FakeAppsV1Api()
    metric_before = pods_evicted_total()

    result, events = tick(api, pod_api=pod_api, apps_api=apps, client=OpenStudioClient(BASE))

    assert result.soft_stopped == [] and result.escalated == []
    assert events == []
    assert calls_to("/data_points.json") == 0
    assert pod_api.deletes == []
    assert apps.reads == []
    assert api.patch_calls == 0  # nothing written — the anchor simply waits
    assert pods_evicted_total() - metric_before == 0


@responses.activate
def test_restart_mid_grace_escalates_from_original_anchor_time():
    """Anchor 16m old persisted BEFORE the operator restart → escalates NOW.

    A fresh handler (new client/store, no in-memory state) must honor the
    original issuedAt: had the clock restarted with the process, the grace
    would run another 15 minutes from boot.
    """
    api = FakeCustomObjectsApi(make_cr(status=anchored_status("a1", PAST_GRACE)))
    register_stuck_analysis("a1", started_dps_payload("a1", "10.0.0.1"))
    pod_api = FakeCoreV1Api([make_pod("worker-1", "10.0.0.1")])
    metric_before = pods_evicted_total()

    result, events = tick(
        api, pod_api=pod_api, apps_api=FakeAppsV1Api(), client=OpenStudioClient(BASE)
    )

    assert result.escalated == ["a1"]
    assert [d["name"] for d in pod_api.deletes] == ["worker-1"]
    assert pods_evicted_total() - metric_before == 1
    anchor = api.obj["status"]["softStops"]["a1"]
    assert anchor["issuedAt"] == PAST_GRACE.isoformat()  # original clock preserved
    assert anchor["escalatedAt"] == NOW.isoformat()
    assert anchor["escalationOutcome"] == "evicted"
    assert len(events) == 1
    event_type, reason, message = events[0]
    assert event_type == "Warning"
    assert reason == ANALYSIS_ESCALATED_EVENT
    assert "a1" in message and "16m" in message and "worker-1" in message and "10.0.0.1" in message


@responses.activate
def test_escalation_deletes_only_pods_matching_started_dp_ips():
    """Decoys: non-worker pod (even with a matching IP), worker with foreign IP, completed dp."""
    api = FakeCustomObjectsApi(make_cr(status=anchored_status("a1", PAST_GRACE)))
    register_stuck_analysis("a1", started_dps_payload("a1", "10.0.0.1", "10.0.0.2"))
    web_labels = {"app.kubernetes.io/name": "openstudio-server", "component": "web"}
    pod_api = FakeCoreV1Api(
        [
            make_pod("worker-a", "10.0.0.1"),  # matches dp ip → delete
            make_pod("worker-b", "10.0.0.7"),  # worker, foreign IP → keep
            make_pod("worker-c", "10.0.0.2"),  # matches dp ip → delete
            make_pod("web-1", "10.0.0.1", labels=web_labels),  # matching IP, not a worker → keep
        ]
    )
    metric_before = pods_evicted_total()

    result, events = tick(api, pod_api=pod_api, apps_api=FakeAppsV1Api())

    assert result.escalated == ["a1"]
    assert [d["name"] for d in pod_api.deletes] == ["worker-a", "worker-c"]
    assert all(d["namespace"] == NAMESPACE for d in pod_api.deletes)
    # completed dp (10.9.9.9) and other analysis's dp (10.8.8.8) never targeted
    assert all(d["name"] != "worker-b" and d["name"] != "web-1" for d in pod_api.deletes)
    assert pods_evicted_total() - metric_before == 2
    assert len(events) == 1
    assert "worker-a" in events[0][2] and "worker-c" in events[0][2]


@responses.activate
def test_default_delete_passes_no_grace_seconds():
    """forceDeleteOnEscalation false (default) → grace_period_seconds None → kubelet
    honors the pod's own terminationGracePeriodSeconds (workers: 5200s drain window)."""
    api = FakeCustomObjectsApi(make_cr(status=anchored_status("a1", PAST_GRACE)))
    register_stuck_analysis("a1", started_dps_payload("a1", "10.0.0.1"))
    pod_api = FakeCoreV1Api([make_pod("worker-1", "10.0.0.1")])

    result, events = tick(api, pod_api=pod_api, apps_api=FakeAppsV1Api())

    assert result.escalated == ["a1"]
    assert len(pod_api.deletes) == 1
    assert pod_api.deletes[0]["kwargs"]["grace_period_seconds"] is None
    assert "default grace (drain)" in events[0][2]


@responses.activate
def test_force_delete_passes_grace_zero():
    """forceDeleteOnEscalation true → grace_period_seconds=0 → immediate kill, no drain."""
    force_spec = {
        **SPEC,
        "analysisPolicy": {**SPEC["analysisPolicy"], "forceDeleteOnEscalation": True},
    }
    api = FakeCustomObjectsApi(make_cr(spec=force_spec, status=anchored_status("a1", PAST_GRACE)))
    register_stuck_analysis("a1", started_dps_payload("a1", "10.0.0.1"))
    pod_api = FakeCoreV1Api([make_pod("worker-1", "10.0.0.1")])

    result, events = tick(api, spec=force_spec, pod_api=pod_api, apps_api=FakeAppsV1Api())

    assert result.escalated == ["a1"]
    assert pod_api.deletes[0]["kwargs"]["grace_period_seconds"] == 0
    assert "grace_period_seconds=0 (immediate kill)" in events[0][2]


@responses.activate
def test_double_escalation_impossible():
    """Second tick after escalation → no-op: no new deletes, events, or REST polls."""
    api = FakeCustomObjectsApi(make_cr(status=anchored_status("a1", PAST_GRACE)))
    register_stuck_analysis("a1", started_dps_payload("a1", "10.0.0.1"))
    register_stuck_analysis("a1", started_dps_payload("a1", "10.0.0.1"))  # second tick's poll
    pod_api = FakeCoreV1Api([make_pod("worker-1", "10.0.0.1")])
    metric_before = pods_evicted_total()

    result, _ = tick(api, pod_api=pod_api, apps_api=FakeAppsV1Api())
    result2, events2 = tick(api, pod_api=pod_api, apps_api=FakeAppsV1Api())

    assert result.escalated == ["a1"]
    assert result2.escalated == []
    assert events2 == []  # no second event storm
    assert len(pod_api.deletes) == 1  # no second delete
    assert pods_evicted_total() - metric_before == 1
    assert calls_to("/data_points.json") == 1  # heavy poll not repeated


@responses.activate
def test_analysis_completed_during_grace_prunes_anchor_without_escalating():
    api = FakeCustomObjectsApi(make_cr(status=anchored_status("a1", PAST_GRACE)))
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": "a1", "status": "completed", "created_at": TEN_DAYS_AGO}],
    )
    pod_api = FakeCoreV1Api([make_pod("worker-1", "10.0.0.1")])

    result, events = tick(api, pod_api=pod_api, apps_api=FakeAppsV1Api())

    assert result.soft_stopped == [] and result.escalated == []
    assert events == []
    assert calls_to("/data_points.json") == 0
    assert pod_api.deletes == []
    # This is where #8's deferred softStops pruning lands (merge patch leaves
    # the emptied map behind as an empty dict — the anchor itself is gone):
    assert api.obj["status"].get("softStops", {}) == {}


@responses.activate
def test_analysis_vanished_from_api_prunes_anchor():
    api = FakeCustomObjectsApi(make_cr(status=anchored_status("a-gone", PAST_GRACE)))
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": "a-other", "status": "started", "created_at": TEN_DAYS_AGO}],
    )
    # a-other is a young, unanchored live analysis: polled for page_data, not tripped.
    responses.get(
        f"{BASE}/analyses/a-other/page_data.json",
        json={
            "analysis": {
                "status": "started",
                "start_time": (NOW - timedelta(minutes=5)).isoformat(),
            }
        },
    )
    pod_api = FakeCoreV1Api([make_pod("worker-1", "10.0.0.1")])

    result, events = tick(api, pod_api=pod_api, apps_api=FakeAppsV1Api())

    assert result.soft_stopped == [] and result.escalated == []
    assert events == []
    assert calls_to("/data_points.json") == 0
    assert "a-gone" not in api.obj["status"].get("softStops", {})


@responses.activate
def test_dry_run_suppresses_pod_deletes_and_marks_event():
    spec = {**SPEC, "dryRun": True}
    api = FakeCustomObjectsApi(make_cr(spec=spec, status=anchored_status("a1", PAST_GRACE)))
    register_stuck_analysis("a1", started_dps_payload("a1", "10.0.0.1"))
    pod_api = FakeCoreV1Api([make_pod("worker-1", "10.0.0.1")])
    metric_before = pods_evicted_total()

    result, events = tick(api, spec=spec, pod_api=pod_api, apps_api=FakeAppsV1Api())

    assert result.escalated == ["a1"]  # escalation decided, mutation suppressed
    assert pod_api.deletes == []
    assert pods_evicted_total() - metric_before == 1  # counts the decision, as #8 does
    assert len(events) == 1
    event_type, reason, message = events[0]
    assert event_type == "Warning"
    assert reason == ANALYSIS_ESCALATED_EVENT
    assert "worker-1" in message and "suppressed (spec.dryRun)" in message
    anchor = api.obj["status"]["softStops"]["a1"]
    assert anchor["escalatedAt"] == NOW.isoformat()
    assert anchor["escalationOutcome"] == "dry-run"


@responses.activate
def test_escalation_without_matching_pods_still_anchors_and_events():
    """No worker pod carries a started dp's IP → nothing deleted, but the escalation
    still happens exactly once (Warning Event + marker), so it cannot storm per tick."""
    api = FakeCustomObjectsApi(make_cr(status=anchored_status("a1", PAST_GRACE)))
    register_stuck_analysis("a1", started_dps_payload("a1", "10.0.0.1"))
    pod_api = FakeCoreV1Api([make_pod("worker-far", "10.0.0.7")])
    register_stuck_analysis("a1", started_dps_payload("a1", "10.0.0.1"))  # second tick's poll

    result, events = tick(api, pod_api=pod_api, apps_api=FakeAppsV1Api())
    result2, events2 = tick(api, pod_api=pod_api, apps_api=FakeAppsV1Api())

    assert result.escalated == ["a1"] and result2.escalated == []
    assert pod_api.deletes == []
    assert len(events) == 1 and events2 == []
    assert "no worker pods matched" in events[0][2]
    assert api.obj["status"]["softStops"]["a1"]["escalationOutcome"] == "no-matching-pods"


@responses.activate
def test_escalated_anchor_skips_grace_phase_entirely():
    """A pre-escalated persisted anchor (post-restart) is inert: no polls, no deletes."""
    api = FakeCustomObjectsApi(
        make_cr(status=anchored_status("a1", PAST_GRACE, escalated_at=NOW - timedelta(minutes=5)))
    )
    register_stuck_analysis("a1")
    pod_api = FakeCoreV1Api([make_pod("worker-1", "10.0.0.1")])

    result, events = tick(api, pod_api=pod_api, apps_api=FakeAppsV1Api())

    assert result.escalated == []
    assert events == []
    assert calls_to("/data_points.json") == 0
    assert pod_api.deletes == []
    assert api.patch_calls == 0


@responses.activate
def test_auto_soft_stop_false_keeps_module_passive_even_with_old_anchor():
    spec = {**SPEC, "analysisPolicy": {**SPEC["analysisPolicy"], "autoSoftStop": False}}
    api = FakeCustomObjectsApi(make_cr(spec=spec, status=anchored_status("a1", PAST_GRACE)))
    register_stuck_analysis("a1")
    pod_api = FakeCoreV1Api([make_pod("worker-1", "10.0.0.1")])

    result, events = tick(api, spec=spec, pod_api=pod_api, apps_api=FakeAppsV1Api())

    assert result.soft_stopped == [] and result.escalated == []
    assert events == []
    assert calls_to("/data_points.json") == 0
    assert pod_api.deletes == []
    assert api.obj["status"]["softStops"]["a1"].get("escalatedAt") is None  # untouched


# --- Issue #44: pod discovery with matchExpressions -------------------------


from openstudio_operator.handlers.analysis_sla import deployment_label_selector


def _exp(key, operator, values=None):
    """Helper: build a matchExpressions entry as the generated client does."""
    return SimpleNamespace(key=key, operator=operator, values=values or [])


def test_deployment_label_selector_match_labels_only():
    apps = FakeAppsV1Api(match_labels=WORKER_LABELS)
    assert deployment_label_selector(apps, "worker", NAMESPACE) == (
        "app.kubernetes.io/name=openstudio-server,component=worker"
    )


def test_deployment_label_selector_match_expressions_in_operator():
    """A matchExpressions-only selector with ``In`` builds the (,) grammar term."""
    apps = FakeAppsV1Api(
        match_labels={},
        match_expressions=[_exp("tier", "In", ["worker", "background"])],
    )
    assert deployment_label_selector(apps, "worker", NAMESPACE) == "tier in (worker,background)"


def test_deployment_label_selector_match_expressions_exists_and_notexists():
    """``Exists`` → bare key, ``DoesNotExist`` → ``!key``."""
    apps = FakeAppsV1Api(
        match_labels={},
        match_expressions=[
            _exp("app", "Exists"),
            _exp("deprecated", "DoesNotExist"),
        ],
    )
    selector = deployment_label_selector(apps, "worker", NAMESPACE)
    assert "app" in selector.split(",") and "!deprecated" in selector.split(",")


def test_deployment_label_selector_intersects_match_labels_and_match_expressions():
    """When BOTH are set, the helper produces an intersection (AND)."""
    apps = FakeAppsV1Api(
        match_labels={"app": "worker"},
        match_expressions=[_exp("tier", "In", ["worker"])],
    )
    selector = deployment_label_selector(apps, "worker", NAMESPACE)
    # The kubernetes label_selector= param is a comma-separated AND.
    assert "app=worker" in selector.split(",")
    assert "tier in (worker)" in selector.split(",")


def test_deployment_label_selector_falls_back_to_matchlabels_on_unsupported_operator(caplog):
    """An exotic operator (e.g. ``Gt``) → matchLabels only + warn.

    Narrower selector = conservative direction (a missed-eviction, never
    a false-eviction; matches the issue #13 design note).
    """
    import logging

    caplog.set_level(logging.WARNING, logger="openstudio_operator.handlers.analysis_sla")
    apps = FakeAppsV1Api(
        match_labels={"app": "worker"},
        match_expressions=[_exp("priority", "Gt", ["0"])],
    )
    selector = deployment_label_selector(apps, "worker", NAMESPACE)
    assert selector == "app=worker"  # narrow, no `priority` term
    # Warning logged
    assert any("matchExpressions operator" in rec.message for rec in caplog.records)


def test_deployment_label_selector_returns_none_when_neither_set():
    """No selector at all → None (the caller must decide what to do)."""
    apps = FakeAppsV1Api(match_labels={}, match_expressions=[])
    assert deployment_label_selector(apps, "worker", NAMESPACE) is None


@responses.activate
def test_escalation_with_match_expressions_only_selector_finds_worker_pods():
    """End-to-end: a Deployment with matchExpressions-only still finds the pods.

    Reproduces the issue #44 gap: the old helper returned ``None`` (or empty)
    for a matchExpressions-only selector, which silently broadened the
    pod set to the whole namespace (or narrowed it to nothing). Either way
    the escalation missed real victims. With the fix, both the escalation
    (#9, this test) and the stall detector (#13) find the right pods.
    """
    apps = FakeAppsV1Api(
        match_labels={},
        match_expressions=[_exp("tier", "In", ["worker"])],
    )
    worker_pod = make_pod("worker-x", "10.0.0.5", labels={"tier": "worker"})
    decoy_pod = make_pod(
        "web-y", "10.0.0.5", labels={"tier": "web"}
    )  # same IP, wrong tier — must NOT match
    pod_api = FakeCoreV1Api([worker_pod, decoy_pod])

    api = FakeCustomObjectsApi(make_cr(status=anchored_status("a1", PAST_GRACE)))
    register_stuck_analysis("a1", started_dps_payload("a1", "10.0.0.5"))

    result, _ = tick(api, pod_api=pod_api, apps_api=apps)

    assert result.escalated == ["a1"]
    assert [d["name"] for d in pod_api.deletes] == ["worker-x"]


@responses.activate
def test_escalation_matchlabels_and_matchexpressions_intersection_pods():
    """Both terms on the selector → only pods matching BOTH are victims."""
    apps = FakeAppsV1Api(
        match_labels={"component": "worker"},
        match_expressions=[_exp("tier", "In", ["worker"])],
    )
    pod_api = FakeCoreV1Api(
        [
            make_pod("worker-only", "10.0.0.1", labels={"component": "worker", "tier": "worker"}),
            make_pod("worker-wrongtier", "10.0.0.2", labels={"component": "worker", "tier": "web"}),
            make_pod("tierless-worker", "10.0.0.3", labels={"component": "worker"}),
            make_pod("tier-only", "10.0.0.4", labels={"tier": "worker"}),
        ]
    )
    api = FakeCustomObjectsApi(make_cr(status=anchored_status("a1", PAST_GRACE)))
    register_stuck_analysis(
        "a1", started_dps_payload("a1", "10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4")
    )

    result, _ = tick(api, pod_api=pod_api, apps_api=apps)

    assert result.escalated == ["a1"]
    # Real pod (both terms) is the only victim; the pod with wrong tier is
    # already correctly excluded by the intersection at the LIST step
    # (FakeCoreV1Api's label selector semantics); IPs not matching are
    # then excluded by the IP filter (#9 design).
    assert [d["name"] for d in pod_api.deletes] == ["worker-only"]


# Suppress the no-handler warning from the caplog helper used above
