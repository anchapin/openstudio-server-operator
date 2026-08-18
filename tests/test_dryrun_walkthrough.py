"""DryRun walkthrough contract tests — issue #45 (D11, audit doc §1).

This file codifies the D11 dryRun contract per handler as a single test
pair (dryRun=False vs dryRun=True). The existing per-handler test files
already cover the dryRun=True path in isolation
(``test_dry_run_suppresses_*``) and the dryRun=False path in isolation
(the ``test_trigger_1_*`` / ``test_deep_backlog_*`` / etc. "real path"
tests). What's not codified in a single test pair is the strict
*symmetry*: given the same setup, the ONLY difference between the two
runs is the mutation call — Event reason/message, anchor shape, and
metric counter are otherwise identical.

That's what each ``test_dryrun_*_is_strict_suppression`` test asserts.
The paired assertion is what the kind walkthrough evidence capture
needs: a human (or CI) can flip ``dryRun`` and observe that everything
observable changes by exactly the mutation.

The single ``test_dryrun_walkthrough_all_modules`` test threads all six
modules in walkthrough order and asserts the same mutable-suppression
contract per tick — this is the code-side analog of the kind walkthrough
steps 3-4.

These tests are CI-verified substitutes for the live-cluster pieces;
the kind runbook (``docs/kind-validation.md``) is the human evidence
capture surface.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import fakeredis
import pytest
import responses
from prometheus_client import REGISTRY

from openstudio_operator.archival import archival_job_name
from openstudio_operator.config import (
    DEFAULT_HPA_FLOOR_POLICY,
    OperatorConfig,
)
from openstudio_operator.handlers.analysis_sla import (
    ANALYSIS_ESCALATED_EVENT,
    ANALYSIS_SOFT_STOPPED_EVENT,
    run_sla_tick,
)
from openstudio_operator.handlers.datapoint_watchdog import (
    DATAPOINT_REQUEUED_EVENT,
    run_watchdog_tick,
)
from openstudio_operator.handlers.hpa_floor import (
    HPA_FLOOR_RAISED_EVENT,
    HpaFloorState,
    run_hpa_floor_tick,
)
from openstudio_operator.handlers.storage_pruner import (
    ANALYSIS_ARCHIVAL_STARTED_EVENT,
    ANALYSIS_DELETED_EVENT,
    run_retention_tick,
)
from openstudio_operator.handlers.web_background_monitor import (
    WEB_BACKGROUND_RESTARTED_EVENT,
    StallWindowTracker,
    run_stall_tick,
)
from openstudio_operator.handlers.worker_recycler import (
    RESTARTED_AT_ANNOTATION,
    WORKER_RECYCLED_EVENT,
    run_recycler_tick,
)
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.redis_client import ReadOnlyRedisClient
from openstudio_operator.status_store import (
    MERGE_PATCH_CONTENT_TYPE,
    StatusStore,
)

BASE = "http://web.test"
WORKER = "worker-walk"
WEBBG = "webbg-walk"
NAMESPACE = "openstudio-server"
NAME = "oscm"
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
GRACE_MIN = 15
PAST_GRACE = NOW - timedelta(minutes=GRACE_MIN + 1)


# --- Shared fakes --------------------------------------------------------------


class FakeCO:
    """In-memory CustomObjectsApi with RFC 7386 merge-patch (status subresource)."""

    def __init__(self, obj: dict) -> None:
        self.obj = copy.deepcopy(obj)
        self.patch_calls = 0

    def get_namespaced_custom_object_status(self, *_args, **_kwargs):
        return copy.deepcopy(self.obj)

    def patch_namespaced_custom_object_status(self, *_args, body, **_kwargs):
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


def make_cr(spec: dict, status: dict | None = None) -> dict:
    return {
        "apiVersion": "energy.nrel.gov/v1alpha1",
        "kind": "OpenStudioClusterManager",
        "metadata": {"name": NAME, "namespace": NAMESPACE},
        "spec": copy.deepcopy(spec),
        "status": copy.deepcopy(status if status is not None else {}),
    }


def make_emit():
    events: list[tuple[str, str, str]] = []

    def emit(event_type: str, reason: str, message: str) -> None:
        events.append((event_type, reason, message))

    return events, emit


def calls_to(suffix: str) -> int:
    return sum(1 for call in responses.calls if call.request.url.endswith(suffix))


def metric(name: str) -> float:
    return REGISTRY.get_sample_value(name) or 0.0


def fresh_cos() -> dict[str, float]:
    """Latest values of every counter the operator publishes — for before/after diffs."""
    return {
        "openstudio_operator_soft_stops_total": metric("openstudio_operator_soft_stops_total"),
        "openstudio_operator_datapoints_requeued_total": metric(
            "openstudio_operator_datapoints_requeued_total"
        ),
        "openstudio_operator_datapoints_requeue_exhausted_total": metric(
            "openstudio_operator_datapoints_requeue_exhausted_total"
        ),
        "openstudio_operator_workers_recycled_total": metric(
            "openstudio_operator_workers_recycled_total"
        ),
        "openstudio_operator_worker_pods_evicted_total": metric(
            "openstudio_operator_worker_pods_evicted_total"
        ),
        "openstudio_operator_web_background_restarts_total": metric(
            "openstudio_operator_web_background_restarts_total"
        ),
        "openstudio_operator_analyses_archived_total": metric(
            "openstudio_operator_analyses_archived_total"
        ),
        "openstudio_operator_analyses_deleted_total": metric(
            "openstudio_operator_analyses_deleted_total"
        ),
        "openstudio_operator_hpa_floor_adjustments_total": metric(
            "openstudio_operator_hpa_floor_adjustments_total"
        ),
    }


# --- Module 1 / 1b: analysis SLA soft-stop + escalation -----------------------


SLA_SPEC = {
    "serverUrl": BASE,
    "analysisPolicy": {"maxDurationMinutes": 180, "gracefulStopTimeoutMinutes": GRACE_MIN},
}


class FakePodApi:
    def __init__(self, pods: list) -> None:
        self.pods = pods
        self.deletes: list[dict] = []

    def list_namespaced_pod(self, namespace, label_selector=None, **_kw):
        wanted = (label_selector or "").split(",") if label_selector else []
        items = [
            pod
            for pod in self.pods
            if all(f"{k}={v}" in wanted for k, v in pod.metadata.labels.items())
        ]
        return SimpleNamespace(items=items)

    def delete_namespaced_pod(self, name, namespace, **kwargs):
        self.deletes.append({"name": name, "namespace": namespace, "kwargs": kwargs})
        return {}


class FakeApps:
    def __init__(self) -> None:
        self.reads = 0

    def read_namespaced_deployment(self, *_a, **_kw):
        self.reads += 1
        return SimpleNamespace(
            spec=SimpleNamespace(
                selector=SimpleNamespace(
                    match_labels={"component": "worker", "app": "os"}
                )
            )
        )


def _make_pod(name: str, ip: str | None):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={"component": "worker", "app": "os"}),
        status=SimpleNamespace(pod_ip=ip),
    )


def _soft_stop_path_responses(analysis_id: str, start_time: datetime) -> None:
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": analysis_id, "status": "started", "created_at": "2026-07-01T00:00:00Z"}],
    )
    responses.get(
        f"{BASE}/analyses/{analysis_id}/page_data.json",
        json={"analysis": {"status": "started", "start_time": start_time.isoformat()}},
    )
    responses.get(f"{BASE}/analyses/{analysis_id}/soft_stop", status=200, json={"result": "ok"})


def _escalation_path_responses(analysis_id: str) -> None:
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": analysis_id, "status": "started", "created_at": "2026-07-01T00:00:00Z"}],
    )
    responses.get(
        f"{BASE}/data_points.json",
        json=[
            {"_id": "dp-1", "analysis_id": analysis_id, "status": "started", "ip_address": "10.0.0.1"}
        ],
    )


@responses.activate
def test_dryrun_soft_stop_is_strict_suppression():
    """D11 contract: only the soft_stop REST call flips; event/metric/anchor are otherwise identical."""
    analysis_id = "a1"
    spec_dry = {**SLA_SPEC, "dryRun": True}
    spec_real = {**SLA_SPEC, "dryRun": False}
    start = NOW - timedelta(hours=4)
    cos_before = metric("openstudio_operator_soft_stops_total")

    # dryRun=True path
    _soft_stop_path_responses(analysis_id, start)
    api_dry = FakeCO(make_cr(spec_dry))
    before = metric("openstudio_operator_soft_stops_total")
    store_dry = StatusStore(NAMESPACE, NAME, api_dry)
    cfg_dry = OperatorConfig.from_spec(spec_dry)
    events_dry, emit_dry = make_emit()
    run_sla_tick(
        OpenStudioClient(BASE),
        store_dry,
        cfg_dry,
        now=NOW,
        emit=emit_dry,
        namespace=NAMESPACE,
        pod_api=FakePodApi([]),
        apps_api=FakeApps(),
    )
    assert calls_to("/soft_stop") == 0
    assert len(events_dry) == 1
    assert events_dry[0][0] == "Warning"
    assert events_dry[0][1] == ANALYSIS_SOFT_STOPPED_EVENT
    assert "suppressed (spec.dryRun)" in events_dry[0][2]
    assert metric("openstudio_operator_soft_stops_total") - before == 1
    assert api_dry.obj["status"]["softStops"][analysis_id]["outcome"] == "dry-run"

    # dryRun=False path with the same setup: SAME one event, SAME metric, but
    # the mutation actually happened and the anchor's outcome is 'issued'.
    _soft_stop_path_responses(analysis_id, start)
    api_real = FakeCO(make_cr(spec_real))
    before = metric("openstudio_operator_soft_stops_total")
    store_real = StatusStore(NAMESPACE, NAME, api_real)
    cfg_real = OperatorConfig.from_spec(spec_real)
    events_real, emit_real = make_emit()
    run_sla_tick(
        OpenStudioClient(BASE),
        store_real,
        cfg_real,
        now=NOW,
        emit=emit_real,
        namespace=NAMESPACE,
        pod_api=FakePodApi([]),
        apps_api=FakeApps(),
    )
    assert calls_to("/soft_stop") == 1
    assert len(events_real) == 1
    assert events_real[0][:2] == events_dry[0][:2]
    assert "suppressed (spec.dryRun)" not in events_real[0][2]
    assert "issued" in events_real[0][2]
    assert metric("openstudio_operator_soft_stops_total") - before == 1
    assert api_real.obj["status"]["softStops"][analysis_id]["outcome"] == "issued"

    # Net metric delta across both ticks: 2 (one per decision)
    assert metric("openstudio_operator_soft_stops_total") - cos_before == 2


@responses.activate
def test_dryrun_escalation_is_strict_suppression():
    """D11 contract (escalation): only pod delete flips; event/metric/anchor otherwise identical."""
    analysis_id = "a1"
    base = {**SLA_SPEC, "analysisPolicy": {**SLA_SPEC["analysisPolicy"], "gracefulStopTimeoutMinutes": GRACE_MIN}}
    spec_dry = {**base, "dryRun": True}
    spec_real = {**base, "dryRun": False}
    anchored = {"softStops": {analysis_id: {"issuedAt": PAST_GRACE.isoformat(), "outcome": "issued"}}}
    cos_before = metric("openstudio_operator_worker_pods_evicted_total")

    def run_one(spec: dict, *, real: bool):
        _escalation_path_responses(analysis_id)
        api = FakeCO(make_cr(spec, status=anchored))
        pods = FakePodApi([_make_pod("worker-1", "10.0.0.1")])
        apps = FakeApps()
        before = metric("openstudio_operator_worker_pods_evicted_total")
        store = StatusStore(NAMESPACE, NAME, api)
        cfg = OperatorConfig.from_spec(spec)
        events, emit = make_emit()
        run_sla_tick(
            OpenStudioClient(BASE),
            store,
            cfg,
            now=NOW,
            emit=emit,
            namespace=NAMESPACE,
            pod_api=pods,
            apps_api=apps,
        )
        return api, pods, events, metric("openstudio_operator_worker_pods_evicted_total") - before

    # dryRun=True
    api_dry, pods_dry, events_dry, delta_dry = run_one(spec_dry, real=False)
    assert pods_dry.deletes == []
    assert len(events_dry) == 1
    assert events_dry[0][:2] == ("Warning", ANALYSIS_ESCALATED_EVENT)
    assert "suppressed (spec.dryRun)" in events_dry[0][2]
    assert delta_dry == 1
    assert api_dry.obj["status"]["softStops"][analysis_id]["escalationOutcome"] == "dry-run"

    # dryRun=False (real eviction)
    api_real, pods_real, events_real, delta_real = run_one(spec_real, real=True)
    assert [d["name"] for d in pods_real.deletes] == ["worker-1"]
    assert len(events_real) == 1
    assert events_real[0][:2] == ("Warning", ANALYSIS_ESCALATED_EVENT)
    assert "suppressed (spec.dryRun)" not in events_real[0][2]
    assert delta_real == 1
    assert api_real.obj["status"]["softStops"][analysis_id]["escalationOutcome"] == "evicted"

    # Net: 2 deletes counted (one dry, one real) — the metric counts the decision.
    assert metric("openstudio_operator_worker_pods_evicted_total") - cos_before == 2


# --- Module 2: zombie datapoint watchdog ---------------------------------------


DPW_SPEC = {
    "serverUrl": BASE,
    "datapointPolicy": {"maxDatapointRuntimeMinutes": 45, "maxAutoRequeues": 2},
}


def _dpw_responses(dp_id: str, over_age_min: int) -> None:
    responses.get(
        f"{BASE}/data_points/status",
        json={"data_points": [{"_id": dp_id, "status": "started"}]},
    )
    responses.post(f"{BASE}/data_points/{dp_id}/requeue", status=204)


@responses.activate
def test_dryrun_requeue_is_strict_suppression():
    """D11 contract: only POST /requeue flips; event/metric/budget otherwise identical."""
    dp_id = "dp1"
    over_age_min = 60
    over = NOW - timedelta(minutes=over_age_min)
    spec_dry = {**DPW_SPEC, "dryRun": True}
    spec_real = {**DPW_SPEC, "dryRun": False}
    cos_before = metric("openstudio_operator_datapoints_requeued_total")

    # dryRun=True
    _dpw_responses(dp_id, over_age_min)
    api_dry = FakeCO(make_cr(spec_dry, status={"startedSince": {dp_id: over.isoformat()}}))
    before = metric("openstudio_operator_datapoints_requeued_total")
    store_dry = StatusStore(NAMESPACE, NAME, api_dry)
    cfg_dry = OperatorConfig.from_spec(spec_dry)
    events_dry, emit_dry = make_emit()
    run_watchdog_tick(
        OpenStudioClient(BASE),
        store_dry,
        cfg_dry,
        now=NOW,
        emit=emit_dry,
        exhausted_seen=set(),
    )
    assert calls_to("/requeue") == 0
    assert len(events_dry) == 1
    assert events_dry[0][:2] == ("Normal", DATAPOINT_REQUEUED_EVENT)
    assert "suppressed (spec.dryRun)" in events_dry[0][2]
    assert metric("openstudio_operator_datapoints_requeued_total") - before == 1
    assert api_dry.obj["status"]["requeues"][dp_id]["count"] == 1

    # dryRun=False
    _dpw_responses(dp_id, over_age_min)
    api_real = FakeCO(make_cr(spec_real, status={"startedSince": {dp_id: over.isoformat()}}))
    before = metric("openstudio_operator_datapoints_requeued_total")
    store_real = StatusStore(NAMESPACE, NAME, api_real)
    cfg_real = OperatorConfig.from_spec(spec_real)
    events_real, emit_real = make_emit()
    run_watchdog_tick(
        OpenStudioClient(BASE),
        store_real,
        cfg_real,
        now=NOW,
        emit=emit_real,
        exhausted_seen=set(),
    )
    assert calls_to("/requeue") == 1
    assert len(events_real) == 1
    assert events_real[0][:2] == ("Normal", DATAPOINT_REQUEUED_EVENT)
    assert "suppressed (spec.dryRun)" not in events_real[0][2]
    assert metric("openstudio_operator_datapoints_requeued_total") - before == 1
    assert api_real.obj["status"]["requeues"][dp_id]["count"] == 1

    # Counter: +2 across both ticks (one per decision).
    assert metric("openstudio_operator_datapoints_requeued_total") - cos_before == 2


# --- Module 3: worker recycler -------------------------------------------------


WR_SPEC = {
    "serverUrl": BASE,
    "targetWorkerDeployment": WORKER,
    "workerPolicy": {
        "recycleWorkerIntervalHours": 12,
        "recycleAfterAnalysis": True,
        "minRecycleIntervalMinutes": 30,
    },
}


class FakeAppsWR:
    def __init__(self) -> None:
        self.patches: list[dict] = []

    def patch_namespaced_deployment(self, name, namespace, body, **kwargs):
        self.patches.append({"name": name, "namespace": namespace, "body": body, "kwargs": kwargs})
        return {}


def _wr_responses() -> None:
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": "a-done", "status": "completed", "created_at": "2026-07-01T00:00:00Z"}],
    )


@responses.activate
def test_dryrun_worker_recycle_is_strict_suppression():
    """D11 contract: only the Deployment patch flips; event/metric/cooldown otherwise identical."""
    spec_dry = {**WR_SPEC, "dryRun": True}
    spec_real = {**WR_SPEC, "dryRun": False}
    cos_before = metric("openstudio_operator_workers_recycled_total")

    def run_one(spec: dict):
        _wr_responses()
        api = FakeCO(make_cr(spec))
        apps = FakeAppsWR()
        before = metric("openstudio_operator_workers_recycled_total")
        store = StatusStore(NAMESPACE, NAME, api)
        cfg = OperatorConfig.from_spec(spec)
        events, emit = make_emit()
        trigger = run_recycler_tick(
            OpenStudioClient(BASE),
            store,
            cfg,
            apps,
            namespace=NAMESPACE,
            now=NOW,
            emit=emit,
        )
        return (
            apps,
            events,
            trigger,
            metric("openstudio_operator_workers_recycled_total") - before,
            api,
        )

    apps_dry, events_dry, trigger_dry, delta_dry, api_dry = run_one(spec_dry)
    assert apps_dry.patches == []
    assert trigger_dry == "analysis-completed"
    assert len(events_dry) == 1
    assert events_dry[0][:2] == ("Normal", WORKER_RECYCLED_EVENT)
    assert "suppressed (spec.dryRun)" in events_dry[0][2]
    assert delta_dry == 1
    assert api_dry.obj["status"]["lastRecycleAt"] == NOW.isoformat()

    apps_real, events_real, trigger_real, delta_real, api_real = run_one(spec_real)
    assert len(apps_real.patches) == 1
    assert apps_real.patches[0]["name"] == WORKER
    assert apps_real.patches[0]["kwargs"]["_content_type"] == MERGE_PATCH_CONTENT_TYPE
    assert (
        apps_real.patches[0]["body"]["spec"]["template"]["metadata"]["annotations"][
            RESTARTED_AT_ANNOTATION
        ]
        == NOW.isoformat()
    )
    assert trigger_real == "analysis-completed"
    assert len(events_real) == 1
    assert events_real[0][:2] == ("Normal", WORKER_RECYCLED_EVENT)
    assert "suppressed (spec.dryRun)" not in events_real[0][2]
    assert delta_real == 1
    assert api_real.obj["status"]["lastRecycleAt"] == NOW.isoformat()

    assert metric("openstudio_operator_workers_recycled_total") - cos_before == 2


# --- Module 4: archival + storage pruner ---------------------------------------

STORAGE = {
    "archiveToS3": True,
    "backend": "s3",
    "bucket": "os-archives",
    "secretRef": "archive-creds",
    "retentionDays": 7,
    "purgeCompletedNFSFiles": True,
}
PRUNER_SPEC = {"serverUrl": BASE, "storagePolicy": dict(STORAGE)}


def _completed_doc(analysis_id: str, age_days: float = 10.0) -> dict:
    return {
        "_id": analysis_id,
        "status": "completed",
        "created_at": (NOW - timedelta(days=age_days + 20)).isoformat(),
        "updated_at": (NOW - timedelta(days=age_days)).isoformat(),
    }


class FakeBatch:
    def __init__(self, jobs: list | None = None) -> None:
        from kubernetes.client import ApiException

        self._ApiException = ApiException
        self.jobs = {j.metadata.name: j for j in (jobs or [])}
        self.creates: list[dict] = []
        self.deletes: list[dict] = []

    def read_namespaced_job(self, name, namespace, **_kw):
        if name not in self.jobs:
            raise self._ApiException(status=404, reason="Not Found")
        return self.jobs[name]

    def create_namespaced_job(self, namespace, body, **_kw):
        self.creates.append({"namespace": namespace, "body": body})
        return SimpleNamespace(metadata=SimpleNamespace(name=body["metadata"]["name"]))

    def delete_namespaced_job(self, name, namespace, **_kw):
        self.deletes.append({"name": name, "namespace": namespace})


def _pruner_responses(analysis_id: str, *, with_dp: bool = True, with_delete: bool = True) -> None:
    responses.get(f"{BASE}/analyses.json", json=[_completed_doc(analysis_id)])
    if with_dp:
        responses.get(f"{BASE}/data_points.json", json=[])
    if with_delete:
        responses.delete(f"{BASE}/analyses/{analysis_id}", status=204)


@responses.activate
def test_dryrun_archival_spawn_is_strict_suppression():
    """D11 contract: only Job create flips; event/metric/tracked-record otherwise identical."""
    analysis_id = "a1"
    spec_dry = {**PRUNER_SPEC, "dryRun": True}
    spec_real = {**PRUNER_SPEC, "dryRun": False}
    cos_before = metric("openstudio_operator_analyses_archived_total")

    def run_one(spec: dict):
        _pruner_responses(analysis_id)
        api = FakeCO(make_cr(spec))
        batch = FakeBatch()
        store = StatusStore(NAMESPACE, NAME, api)
        cfg = OperatorConfig.from_spec(spec)
        events, emit = make_emit()
        result = run_retention_tick(
            OpenStudioClient(BASE),
            store,
            cfg,
            now=NOW,
            emit=emit,
            namespace=NAMESPACE,
            batch_api=batch,
        )
        return batch, events, result, api

    batch_dry, events_dry, result_dry, api_dry = run_one(spec_dry)
    assert batch_dry.creates == []
    assert result_dry.spawned == [analysis_id]
    assert len(events_dry) == 1
    assert events_dry[0][:2] == ("Normal", ANALYSIS_ARCHIVAL_STARTED_EVENT)
    assert "suppressed (spec.dryRun)" in events_dry[0][2]
    # Marker record: no jobName (nothing to watch), but spawnedAt populated.
    record = api_dry.obj["status"]["archivedAnalyses"][analysis_id]
    assert "jobName" not in record
    assert record["spawnedAt"] == NOW.isoformat()

    batch_real, events_real, result_real, api_real = run_one(spec_real)
    assert len(batch_real.creates) == 1
    assert batch_real.creates[0]["body"]["metadata"]["name"] == archival_job_name(analysis_id)
    assert result_real.spawned == [analysis_id]
    assert len(events_real) == 1
    assert events_real[0][:2] == ("Normal", ANALYSIS_ARCHIVAL_STARTED_EVENT)
    assert "suppressed (spec.dryRun)" not in events_real[0][2]
    record = api_real.obj["status"]["archivedAnalyses"][analysis_id]
    assert record["jobName"] == archival_job_name(analysis_id)
    assert record["spawnedAt"] == NOW.isoformat()

    # archived_total increments ON the Complete verification, not on spawn —
    # both runs skipped it (no Job observably completed), so counter is unchanged.
    assert metric("openstudio_operator_analyses_archived_total") - cos_before == 0


@responses.activate
def test_dryrun_archival_delete_is_strict_suppression():
    """D11 contract: only DELETE /analyses/{id} flips; event/metric otherwise identical."""
    analysis_id = "a1"
    job_name = archival_job_name(analysis_id)
    anchored = {
        "archivedAnalyses": {
            analysis_id: {
                "backend": "s3",
                "bucket": "os-archives",
                "jobName": job_name,
                "spawnedAt": (NOW - timedelta(minutes=10)).isoformat(),
            }
        }
    }
    spec_dry = {**PRUNER_SPEC, "dryRun": True}
    spec_real = {**PRUNER_SPEC, "dryRun": False}
    deleted_before = metric("openstudio_operator_analyses_deleted_total")
    archived_before = metric("openstudio_operator_analyses_archived_total")

    def run_one(spec: dict):
        _pruner_responses(analysis_id, with_dp=False)
        api = FakeCO(make_cr(spec, status=anchored))
        complete_job = SimpleNamespace(
            metadata=SimpleNamespace(name=job_name),
            status=SimpleNamespace(
                conditions=[SimpleNamespace(type="Complete", status="True")],
                succeeded=1,
                failed=0,
            ),
        )
        batch = FakeBatch([complete_job])
        store = StatusStore(NAMESPACE, NAME, api)
        cfg = OperatorConfig.from_spec(spec)
        events, emit = make_emit()
        result = run_retention_tick(
            OpenStudioClient(BASE),
            store,
            cfg,
            now=NOW,
            emit=emit,
            namespace=NAMESPACE,
            batch_api=batch,
        )
        return batch, events, result, api

    batch_dry, events_dry, result_dry, api_dry = run_one(spec_dry)
    assert calls_to(f"/analyses/{analysis_id}") == 0
    assert result_dry.verified == [analysis_id]
    assert result_dry.deleted == []
    # Both events still fire (one for verification, one for the suppressed delete).
    assert any(ANALYSIS_ARCHIVAL_STARTED_EVENT == r for _, r, _ in events_dry) or any(
        "AnalysisArchivalSucceeded" in r for _, r, _ in events_dry
    )
    assert any(r == ANALYSIS_DELETED_EVENT for _, r, _ in events_dry)
    assert any("suppressed (spec.dryRun)" in m for _, _, m in events_dry)
    # Verified record STAYS persisted (delete was suppressed).
    assert api_dry.obj["status"]["archivedAnalyses"][analysis_id]["verifiedAt"] == NOW.isoformat()

    batch_real, events_real, result_real, api_real = run_one(spec_real)
    assert calls_to(f"/analyses/{analysis_id}") == 1
    assert result_real.verified == [analysis_id]
    assert result_real.deleted == [analysis_id]
    # Real delete event: no "suppressed" marker.
    delete_event = [e for e in events_real if e[1] == ANALYSIS_DELETED_EVENT]
    assert len(delete_event) == 1
    assert "suppressed (spec.dryRun)" not in delete_event[0][2]
    # Record pruned after successful delete (merge-patch leaves an empty dict).
    assert api_real.obj["status"].get("archivedAnalyses", {}) == {}

    # Metrics:
    # - archived_total: +1 in both (verification observed in both)
    # - deleted_total: +1 in real only
    assert metric("openstudio_operator_analyses_archived_total") - archived_before == 2
    assert metric("openstudio_operator_analyses_deleted_total") - deleted_before == 1


# --- Module 5: web_background stall detector ----------------------------------


WBM_SPEC = {
    "serverUrl": BASE,
    "redisUrl": "redis://:pw@queue.test:6379",
    "targetWorkerDeployment": WORKER,
    "targetWebBackgroundDeployment": WEBBG,
        "webBackgroundPolicy": {"stallWindowMinutes": 10},
}


class FakeAppsWBM:
    def __init__(self) -> None:
        self.patches: list[dict] = []
        self.reads: list[tuple[str, str]] = []

    def read_namespaced_deployment(self, name, namespace, **_kw):
        self.reads.append((name, namespace))
        return SimpleNamespace(
            spec=SimpleNamespace(
                selector=SimpleNamespace(match_labels={"component": "worker"})
            )
        )

    def patch_namespaced_deployment(self, name, namespace, body, **kwargs):
        self.patches.append({"name": name, "namespace": namespace, "body": body, "kwargs": kwargs})
        return {}


class FakePodsWBM:
    def __init__(self, pods: list) -> None:
        self.pods = pods

    def list_namespaced_pod(self, namespace, **_kw):
        return SimpleNamespace(items=list(self.pods))


def _stall_redis(at: datetime) -> ReadOnlyRedisClient:
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    fake.rpush("simulations", "j1")
    fake.rpush("simulations", "j2")
    epoch = at.timestamp()
    fake.sadd("resque:workers", "w1")
    fake.set("resque:workers:w1", str(epoch - 600))  # stale (>300s)
    return ReadOnlyRedisClient("redis://:pw@queue.test:6379", connection=fake)


def _pod_running() -> SimpleNamespace:
    return SimpleNamespace(
        status=SimpleNamespace(
            phase="Running",
            conditions=[SimpleNamespace(type="Ready", status="True")],
        )
    )


def _seed_stall(api: FakeCO, *, spec: dict, tracker: StallWindowTracker) -> None:
    """Drive the tracker through the full window so the next tick fires."""
    apps = FakeAppsWBM()
    pods = FakePodsWBM([_pod_running(), _pod_running()])
    store = StatusStore(NAMESPACE, NAME, api)
    cfg = OperatorConfig.from_spec(spec)
    for offset in (0, 5):
        events, emit = make_emit()
        run_stall_tick(
            _stall_redis(NOW + timedelta(minutes=offset)),
            store,
            cfg,
            apps,
            pods,
            namespace=NAMESPACE,
            now=NOW + timedelta(minutes=offset),
            emit=emit,
            tracker=tracker,
        )


def test_dryrun_web_background_restart_is_strict_suppression():
    """D11 contract: only the Deployment patch flips; event/metric/anchor otherwise identical."""
    spec_dry = {**WBM_SPEC, "dryRun": True}
    spec_real = {**WBM_SPEC, "dryRun": False}
    cos_before = metric("openstudio_operator_web_background_restarts_total")

    def run_one(spec: dict):
        api = FakeCO(make_cr(spec))
        tracker = StallWindowTracker()
        _seed_stall(api, spec=spec, tracker=tracker)
        apps = FakeAppsWBM()
        pods = FakePodsWBM([_pod_running(), _pod_running()])
        before = metric("openstudio_operator_web_background_restarts_total")
        store = StatusStore(NAMESPACE, NAME, api)
        cfg = OperatorConfig.from_spec(spec)
        events, emit = make_emit()
        run_stall_tick(
            _stall_redis(NOW + timedelta(minutes=10)),
            store,
            cfg,
            apps,
            pods,
            namespace=NAMESPACE,
            now=NOW + timedelta(minutes=10),
            emit=emit,
            tracker=tracker,
        )
        return apps, events, metric("openstudio_operator_web_background_restarts_total") - before, api

    apps_dry, events_dry, delta_dry, api_dry = run_one(spec_dry)
    assert apps_dry.patches == []
    assert len(events_dry) == 1
    assert events_dry[0][:2] == ("Warning", WEB_BACKGROUND_RESTARTED_EVENT)
    assert "suppressed (spec.dryRun)" in events_dry[0][2]
    assert delta_dry == 1
    assert api_dry.obj["status"]["lastWebBackgroundRestart"] == (NOW + timedelta(minutes=10)).isoformat()

    apps_real, events_real, delta_real, api_real = run_one(spec_real)
    assert len(apps_real.patches) == 1
    assert apps_real.patches[0]["name"] == WEBBG
    assert apps_real.patches[0]["kwargs"]["_content_type"] == MERGE_PATCH_CONTENT_TYPE
    assert len(events_real) == 1
    assert events_real[0][:2] == ("Warning", WEB_BACKGROUND_RESTARTED_EVENT)
    assert "suppressed (spec.dryRun)" not in events_real[0][2]
    assert delta_real == 1
    assert api_real.obj["status"]["lastWebBackgroundRestart"] == (NOW + timedelta(minutes=10)).isoformat()

    assert metric("openstudio_operator_web_background_restarts_total") - cos_before == 2


# --- Module 7 (Phase 4): HPA-floor adjuster -----------------------------------


HPA_WORKER_HPA = "worker-hpa"
HPA_SPEC = {**WBM_SPEC, "redisUrl": "redis://:pw@queue.test:6379"}


class FakeAutoscaling:
    def __init__(self, min_replicas: int = 1, max_replicas: int = 20) -> None:
        self.spec = SimpleNamespace(min_replicas=min_replicas, max_replicas=max_replicas)
        self.patches: list[dict] = []

    def read_namespaced_horizontalpodautoscaler(self, name, namespace, **_kw):
        return SimpleNamespace(spec=self.spec)

    def patch_namespaced_horizontalpodautoscaler(self, name, namespace, body, **kwargs):
        self.patches.append({"name": name, "namespace": namespace, "body": body, "kwargs": kwargs})
        self.spec.min_replicas = body["spec"]["minReplicas"]
        return {}


def _hpa_redis(simulations: int) -> ReadOnlyRedisClient:
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    for i in range(simulations):
        fake.rpush("simulations", f"j{i}")
    return ReadOnlyRedisClient("redis://:pw@queue.test:6379", connection=fake)


@pytest.fixture(autouse=True)
def _reset_baseline_cache():
    """The hpa_floor process-lifetime baseline cache must be fresh per test."""
    from openstudio_operator.handlers.hpa_floor import reset_captured_baseline_cache

    reset_captured_baseline_cache()
    yield
    reset_captured_baseline_cache()


def _hpa_anchor_state() -> HpaFloorState:
    """A state whose restart-conservative observation gate has fully opened."""
    state = HpaFloorState()
    cooldown = timedelta(seconds=DEFAULT_HPA_FLOOR_POLICY.cooldown_seconds)
    assert state.gate_open(NOW - 2 * cooldown, cooldown) is False
    assert state.gate_open(NOW - cooldown, cooldown) is True
    return state


def test_dryrun_hpa_floor_adjust_is_strict_suppression():
    """D11 contract: only the HPA patch flips; event/metric/cooldown otherwise identical."""
    cos_before = metric("openstudio_operator_hpa_floor_adjustments_total")

    def run_one(dry_run: bool):
        api = FakeAutoscaling(min_replicas=1, max_replicas=20)
        state = _hpa_anchor_state()
        before = metric("openstudio_operator_hpa_floor_adjustments_total")
        events, emit = make_emit()
        run_hpa_floor_tick(
            _hpa_redis(300),
            api,
            namespace=NAMESPACE,
            policy=DEFAULT_HPA_FLOOR_POLICY,
            now=NOW,
            emit=emit,
            state=state,
            dry_run=dry_run,
            effective_baseline_min_replicas=1,
        )
        return api, events, metric("openstudio_operator_hpa_floor_adjustments_total") - before, state

    api_dry, events_dry, delta_dry, state_dry = run_one(dry_run=True)
    assert api_dry.patches == []
    assert len(events_dry) == 1
    assert events_dry[0][:2] == ("Normal", HPA_FLOOR_RAISED_EVENT)
    assert "suppressed (spec.dryRun)" in events_dry[0][2]
    assert delta_dry == 1
    assert state_dry.last_adjusted_at == NOW

    api_real, events_real, delta_real, state_real = run_one(dry_run=False)
    assert len(api_real.patches) == 1
    assert api_real.patches[0]["name"] == HPA_WORKER_HPA
    assert api_real.patches[0]["kwargs"]["_content_type"] == MERGE_PATCH_CONTENT_TYPE
    assert api_real.patches[0]["body"] == {"spec": {"minReplicas": 8}}
    assert len(events_real) == 1
    assert events_real[0][:2] == ("Normal", HPA_FLOOR_RAISED_EVENT)
    assert "suppressed (spec.dryRun)" not in events_real[0][2]
    assert delta_real == 1
    assert state_real.last_adjusted_at == NOW

    assert metric("openstudio_operator_hpa_floor_adjustments_total") - cos_before == 2


# --- End-to-end walkthrough: all modules in one tick ---------------------------


@responses.activate
def test_dryrun_walkthrough_all_modules_suppress_mutations_only():
    """Phase 1 walkthrough steps 3-4: one tick per module, dryRun=True everywhere.

    The code-side analog of the kind walkthrough. For each module, asserts
    the only observable change is the "suppressed (spec.dryRun)" message
    suffix on the Event — the anchor/status/metric record is identical to
    a real run, and the cluster-mutation call count is zero.

    Per-module evidence (counter names from ``metrics.py`` / audit doc
    Appendix D):

    * Module 1   soft_stop:    ``openstudio_operator_soft_stops_total`` +1
    * Module 1b  escalation:   ``openstudio_operator_worker_pods_evicted_total`` +1
    * Module 2   requeue:      ``openstudio_operator_datapoints_requeued_total`` +1
    * Module 3   recycle:      ``openstudio_operator_workers_recycled_total`` +1
    * Module 4   archival:     tracker record w/o jobName (no metric, +1 on Complete)
    * Module 5   web_bg:       ``openstudio_operator_web_background_restarts_total`` +1
    * Phase 4    hpa_floor:    ``openstudio_operator_hpa_floor_adjustments_total`` +1
    """
    analysis_id = "a-phase1"
    dp_id = "dp-phase1"
    spec_dry = {
        "serverUrl": BASE,
        "redisUrl": "redis://:pw@queue.test:6379",
        "targetWorkerDeployment": WORKER,
        "targetWebBackgroundDeployment": WEBBG,
        "dryRun": True,
        "analysisPolicy": {
            "maxDurationMinutes": 180,
            "gracefulStopTimeoutMinutes": GRACE_MIN,
        },
        "datapointPolicy": {"maxDatapointRuntimeMinutes": 45, "maxAutoRequeues": 2},
        "workerPolicy": {
            "recycleWorkerIntervalHours": 12,
            "recycleAfterAnalysis": True,
            "minRecycleIntervalMinutes": 30,
        },
        "webBackgroundPolicy": {"stallWindowMinutes": 10},
        "storagePolicy": dict(STORAGE),
    }
    api = FakeCO(make_cr(spec_dry))
    # Module 1 (soft-stop) needs no anchor; Module 1b (escalation) needs the
    # anchor at PAST_GRACE so the grace check fires the same tick.
    api.obj["status"] = copy.deepcopy(
        {"startedSince": {dp_id: (NOW - timedelta(hours=2)).isoformat()}}
    )

    # Module 1 (soft-stop) tick: pre-existing completed analysis for the
    # recycler + the over-runtime started analysis for the SLA tick.
    responses.get(
        f"{BASE}/analyses.json",
        json=[
            {"_id": analysis_id, "status": "started", "created_at": "2026-07-01T00:00:00Z"},
            {"_id": "a-done", "status": "completed", "created_at": "2026-07-01T00:00:00Z"},
        ],
    )
    responses.get(
        f"{BASE}/analyses/{analysis_id}/page_data.json",
        json={
            "analysis": {
                "status": "started",
                "start_time": (NOW - timedelta(hours=4)).isoformat(),
            }
        },
    )
    responses.get(
        f"{BASE}/data_points/status",
        json={"data_points": [{"_id": dp_id, "status": "started"}]},
    )
    responses.post(f"{BASE}/data_points/{dp_id}/requeue", status=204)

    # Metrics before
    cos = fresh_cos()
    cfg = OperatorConfig.from_spec(spec_dry)
    store = StatusStore(NAMESPACE, NAME, api)

    # Module 1 (soft-stop) — first tick: no anchor yet, so the soft-stop
    # path fires and writes the anchor.
    pods_api = FakePodApi([_make_pod("worker-1", "10.0.0.1")])
    apps_api = FakeApps()
    events, emit = make_emit()
    run_sla_tick(
        OpenStudioClient(BASE),
        store,
        cfg,
        now=NOW,
        emit=emit,
        namespace=NAMESPACE,
        pod_api=pods_api,
        apps_api=apps_api,
    )
    soft_stop_events = [e for e in events if e[1] == ANALYSIS_SOFT_STOPPED_EVENT]
    assert len(soft_stop_events) == 1
    assert "suppressed (spec.dryRun)" in soft_stop_events[0][2]
    assert "soft stop issued" not in soft_stop_events[0][2]
    assert pods_api.deletes == []
    assert store.get_soft_stops()[analysis_id].outcome == "dry-run"
    anchored = {
        "softStops": {
            analysis_id: {"issuedAt": PAST_GRACE.isoformat(), "outcome": "issued"}
        },
        "startedSince": {dp_id: (NOW - timedelta(hours=2)).isoformat()},
    }
    api.obj["status"] = copy.deepcopy(anchored)
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": analysis_id, "status": "started", "created_at": "2026-07-01T00:00:00Z"}],
    )
    responses.get(
        f"{BASE}/data_points.json",
        json=[
            {
                "_id": "dp-1",
                "analysis_id": analysis_id,
                "status": "started",
                "ip_address": "10.0.0.1",
            }
        ],
    )
    events, emit = make_emit()
    run_sla_tick(
        OpenStudioClient(BASE),
        store,
        cfg,
        now=NOW,
        emit=emit,
        namespace=NAMESPACE,
        pod_api=pods_api,
        apps_api=apps_api,
    )
    escal_events = [e for e in events if e[1] == ANALYSIS_ESCALATED_EVENT]
    assert len(escal_events) == 1
    assert "suppressed (spec.dryRun)" in escal_events[0][2]
    assert pods_api.deletes == []
    assert store.get_soft_stops()[analysis_id].escalation_outcome == "dry-run"

    # Module 2 (zombie requeue)
    events, emit = make_emit()
    run_watchdog_tick(
        OpenStudioClient(BASE),
        store,
        cfg,
        now=NOW,
        emit=emit,
        exhausted_seen=set(),
    )
    requeue_events = [e for e in events if e[1] == DATAPOINT_REQUEUED_EVENT]
    assert len(requeue_events) == 1
    assert "suppressed (spec.dryRun)" in requeue_events[0][2]
    assert calls_to("/requeue") == 0
    assert store.get_requeues()[dp_id].count == 1

    # Module 3 (worker recycler)
    apps_wr = FakeAppsWR()
    events, emit = make_emit()
    run_recycler_tick(
        OpenStudioClient(BASE),
        store,
        cfg,
        apps_wr,
        namespace=NAMESPACE,
        now=NOW,
        emit=emit,
    )
    recycle_events = [e for e in events if e[1] == WORKER_RECYCLED_EVENT]
    assert len(recycle_events) == 1
    assert "suppressed (spec.dryRun)" in recycle_events[0][2]
    assert apps_wr.patches == []
    assert store.get_last_recycle_at() == NOW

    # Module 5 (web_background) — pre-seed the tracker
    apps_wbm = FakeAppsWBM()
    pods_wbm = FakePodsWBM([_pod_running(), _pod_running()])
    tracker = StallWindowTracker()
    for offset in (0, 5):
        events, emit = make_emit()
        run_stall_tick(
            _stall_redis(NOW + timedelta(minutes=offset)),
            store,
            cfg,
            apps_wbm,
            pods_wbm,
            namespace=NAMESPACE,
            now=NOW + timedelta(minutes=offset),
            emit=emit,
            tracker=tracker,
        )
    events, emit = make_emit()
    run_stall_tick(
        _stall_redis(NOW + timedelta(minutes=10)),
        store,
        cfg,
        apps_wbm,
        pods_wbm,
        namespace=NAMESPACE,
        now=NOW + timedelta(minutes=10),
        emit=emit,
        tracker=tracker,
    )
    wbm_events = [e for e in events if e[1] == WEB_BACKGROUND_RESTARTED_EVENT]
    assert len(wbm_events) == 1
    assert "suppressed (spec.dryRun)" in wbm_events[0][2]
    assert apps_wbm.patches == []
    assert store.get_last_web_background_restart_at() == (NOW + timedelta(minutes=10))

    # Module 4 (archival) — separate completed analysis, no in-flight record
    archive_analysis_id = "a-archive"
    responses.get(
        f"{BASE}/analyses.json",
        json=[_completed_doc(archive_analysis_id)],
    )
    # Module 4 needs an empty data_points.json (no datapoints for the archival).
    responses.get(f"{BASE}/data_points.json", json=[])
    batch = FakeBatch()
    events, emit = make_emit()
    run_retention_tick(
        OpenStudioClient(BASE),
        store,
        cfg,
        now=NOW,
        emit=emit,
        namespace=NAMESPACE,
        batch_api=batch,
    )
    archive_events = [e for e in events if e[1] == ANALYSIS_ARCHIVAL_STARTED_EVENT]
    assert len(archive_events) == 1
    assert "suppressed (spec.dryRun)" in archive_events[0][2]
    assert batch.creates == []
    record = api.obj["status"]["archivedAnalyses"][archive_analysis_id]
    assert "jobName" not in record  # dry-run marker

    # Phase 4 (HPA-floor adjuster)
    hpa_api = FakeAutoscaling(min_replicas=1, max_replicas=20)
    state = _hpa_anchor_state()
    events, emit = make_emit()
    run_hpa_floor_tick(
        _hpa_redis(300),
        hpa_api,
        namespace=NAMESPACE,
        policy=DEFAULT_HPA_FLOOR_POLICY,
        now=NOW,
        emit=emit,
        state=state,
        dry_run=True,
        effective_baseline_min_replicas=1,
    )
    hpa_events = [e for e in events if e[1] == HPA_FLOOR_RAISED_EVENT]
    assert len(hpa_events) == 1
    assert "suppressed (spec.dryRun)" in hpa_events[0][2]
    assert hpa_api.patches == []

    # Cumulative invariants after the full walkthrough with dryRun=True:
    # every decision counted (D11: metrics are decisions, not mutations),
    # every mutation-suppressed Event carried the marker, every state
    # anchor advanced as in a real run.
    diffs = {
        "openstudio_operator_soft_stops_total": metric(
            "openstudio_operator_soft_stops_total"
        )
        - cos["openstudio_operator_soft_stops_total"],
        "openstudio_operator_worker_pods_evicted_total": metric(
            "openstudio_operator_worker_pods_evicted_total"
        )
        - cos["openstudio_operator_worker_pods_evicted_total"],
        "openstudio_operator_datapoints_requeued_total": metric(
            "openstudio_operator_datapoints_requeued_total"
        )
        - cos["openstudio_operator_datapoints_requeued_total"],
        "openstudio_operator_workers_recycled_total": metric(
            "openstudio_operator_workers_recycled_total"
        )
        - cos["openstudio_operator_workers_recycled_total"],
        "openstudio_operator_web_background_restarts_total": metric(
            "openstudio_operator_web_background_restarts_total"
        )
        - cos["openstudio_operator_web_background_restarts_total"],
        "openstudio_operator_analyses_archived_total": metric(
            "openstudio_operator_analyses_archived_total"
        )
        - cos["openstudio_operator_analyses_archived_total"],
        "openstudio_operator_analyses_deleted_total": metric(
            "openstudio_operator_analyses_deleted_total"
        )
        - cos["openstudio_operator_analyses_deleted_total"],
        "openstudio_operator_hpa_floor_adjustments_total": metric(
            "openstudio_operator_hpa_floor_adjustments_total"
        )
        - cos["openstudio_operator_hpa_floor_adjustments_total"],
    }
    assert diffs == {
        "openstudio_operator_soft_stops_total": 1,
        "openstudio_operator_worker_pods_evicted_total": 1,
        "openstudio_operator_datapoints_requeued_total": 1,
        "openstudio_operator_workers_recycled_total": 1,
        "openstudio_operator_web_background_restarts_total": 1,
        "openstudio_operator_analyses_archived_total": 0,  # no Complete observed
        "openstudio_operator_analyses_deleted_total": 0,  # nothing verified
        "openstudio_operator_hpa_floor_adjustments_total": 1,
    }
