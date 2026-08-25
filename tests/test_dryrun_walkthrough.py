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
import responses
from prometheus_client import REGISTRY

from _fakes import FakePodsCoreV1Api, _merge_patch, calls_to, make_cr, make_emit
from openstudio_operator import metrics as _metrics
from openstudio_operator._k8s import MERGE_PATCH_CONTENT_TYPE, RESTARTED_AT_ANNOTATION
from openstudio_operator.archival import archival_job_name
from openstudio_operator.config import (
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
from openstudio_operator.handlers.web_background_monitor import (
    WEB_BACKGROUND_RESTARTED_EVENT,
    Leg2SafeguardState,
    StallWindowTracker,
    run_stall_tick,
)
from openstudio_operator.handlers.worker_recycler import (
    WORKER_RECYCLED_EVENT,
    run_recycler_tick,
)
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.redis_client import SIMULATIONS_QUEUE, ReadOnlyRedisClient
from openstudio_operator.retention import (
    ANALYSIS_ARCHIVAL_STARTED_EVENT,
    ANALYSIS_DELETED_EVENT,
    run_retention_tick,
)
from openstudio_operator.status_store import (
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
    """In-memory CustomObjectsApi with RFC 7386 merge-patch (status subresource).

    Kept local deliberately (#653): a narrative minimal get/patch pair —
    no list surface, no synthetic-409 seam — so each walkthrough section
    stays self-contained beside the scenario it serves.
    """

    def __init__(self, obj: dict) -> None:
        self.obj = copy.deepcopy(obj)
        self.patch_calls = 0

    def get_namespaced_custom_object_status(self, *_args, **_kwargs):
        return copy.deepcopy(self.obj)

    def patch_namespaced_custom_object_status(self, *_args, body, **_kwargs):
        self.patch_calls += 1
        _merge_patch(self.obj, body)
        return copy.deepcopy(self.obj)


def metric(name: str) -> float:
    return REGISTRY.get_sample_value(name) or 0.0


def labelled_metric(name: str, labels: dict[str, str]) -> float:
    """Read a labelled counter's value for a specific label combination.

    Issue #309 — counters gained labels so the bare ``metric(name)`` form
    (which returns None for labelled counters) no longer works for the
    four action counters. This helper pins the label-key vocabularies
    so the diff arithmetic in this walkthrough stays correct.
    """
    return REGISTRY.get_sample_value(name, labels) or 0.0


# Issue #727 — per-outcome summation helper. The metric docstrings in
# ``metrics.py`` are the source of truth for which labelled outcomes each
# Counter publishes (one ``<outcome>`` or ``<trigger>`` value per ``inc()``
# site). ``LABELED_COUNTER_OUTCOMES`` is the test-side mirror of those
# docstrings, keyed by metric name to a ``(label_name, values)`` pair.
# The four ``*_total()`` helpers below collapse to a single
# ``sum_labelled_counter`` call so the label-value tuple lives in exactly
# one place; the parity test at the bottom of this module enforces that
# the dict stays in lockstep with the metric docstrings.
LABELED_COUNTER_OUTCOMES: dict[str, tuple[str, tuple[str, ...]]] = {
    "openstudio_operator_soft_stops_total": (
        "outcome",
        ("issued", "dry-run"),
    ),
    "openstudio_operator_workers_recycled_total": (
        "trigger",
        ("analysis-completed", "interval-elapsed"),
    ),
    "openstudio_operator_worker_pods_evicted_total": (
        "outcome",
        ("evicted", "evicted-partial", "no-matching-pods", "dry-run"),
    ),
    "openstudio_operator_analyses_deleted_total": (
        "outcome",
        ("deleted",),
    ),
}


def sum_labelled_counter(
    name: str, label: str, values: tuple[str, ...]
) -> float:
    """Sum every series of a labelled Counter for the given label values.

    Issue #309 — counters gained labels so the bare ``metric(name)`` form
    no longer works for the four action counters. The label-values tuple
    is the canonical list maintained in :data:`LABELED_COUNTER_OUTCOMES`
    — the metric docstring in ``metrics.py`` is the source of truth
    (parity enforced by
    ``test_labeled_counter_outcomes_match_metric_docstrings``).
    """
    return sum(labelled_metric(name, {label: v}) for v in values)


def soft_stops_total() -> float:
    """Sum every ``outcome`` series of ``soft_stops_total`` (issue #309)."""
    label, values = LABELED_COUNTER_OUTCOMES["openstudio_operator_soft_stops_total"]
    return sum_labelled_counter("openstudio_operator_soft_stops_total", label, values)


def workers_recycled_total() -> float:
    """Sum every ``trigger`` series of ``workers_recycled_total`` (issue #309)."""
    label, values = LABELED_COUNTER_OUTCOMES[
        "openstudio_operator_workers_recycled_total"
    ]
    return sum_labelled_counter(
        "openstudio_operator_workers_recycled_total", label, values
    )


def worker_pods_evicted_total() -> float:
    """Sum every ``outcome`` series of ``worker_pods_evicted_total`` (issue #309)."""
    label, values = LABELED_COUNTER_OUTCOMES[
        "openstudio_operator_worker_pods_evicted_total"
    ]
    return sum_labelled_counter(
        "openstudio_operator_worker_pods_evicted_total", label, values
    )


def analyses_deleted_total() -> float:
    """Sum every ``outcome`` series of ``analyses_deleted_total`` (issue #309)."""
    label, values = LABELED_COUNTER_OUTCOMES[
        "openstudio_operator_analyses_deleted_total"
    ]
    return sum_labelled_counter(
        "openstudio_operator_analyses_deleted_total", label, values
    )


def fresh_cos() -> dict[str, float]:
    """Latest values of every counter the operator publishes — for before/after diffs."""
    return {
        "openstudio_operator_soft_stops_total": soft_stops_total(),
        "openstudio_operator_datapoints_requeued_total": metric(
            "openstudio_operator_datapoints_requeued_total"
        ),
        "openstudio_operator_datapoints_requeue_exhausted_total": metric(
            "openstudio_operator_datapoints_requeue_exhausted_total"
        ),
        "openstudio_operator_workers_recycled_total": workers_recycled_total(),
        "openstudio_operator_worker_pods_evicted_total": worker_pods_evicted_total(),
        "openstudio_operator_web_background_restarts_total": metric(
            "openstudio_operator_web_background_restarts_total"
        ),
        "openstudio_operator_analyses_archived_total": metric(
            "openstudio_operator_analyses_archived_total"
        ),
        "openstudio_operator_analyses_deleted_total": analyses_deleted_total(),
    }


# --- Module 1 / 1b: analysis SLA soft-stop + escalation -----------------------


SLA_SPEC = {
    "serverUrl": BASE,
    "analysisPolicy": {"maxDurationMinutes": 180, "gracefulStopTimeoutMinutes": GRACE_MIN},
}


class FakeApps:
    """Read-only deployment-selector fixture (int counter ``reads``).

    Kept local deliberately (#653): the narrative asserts a plain ``reads``
    int counter; the shared FakeAppsV1Api records ``(name, namespace)``
    tuples instead.
    """

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


def _soft_stop_path_responses(analysis_id: str) -> None:
    """Stage the responses for the new (issue #83 D1) soft-stop path.

    Pre-#83 used ``page_data.start_time`` as the SLA clock anchor and
    fetched it in a single tick. Post-#83 D1 the anchor is the
    operator-observed first sight of the analysis in ``started`` via
    ``/status.json``; the soft-stop fires on the SECOND tick (when the
    anchor's age exceeds ``maxDurationMinutes``). The pre-existing
    ``start_time`` argument is kept for call-site compatibility but
    no longer used in the path.
    """
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": analysis_id, "created_at": "2026-07-01T00:00:00Z"}],
    )
    responses.get(
        f"{BASE}/analyses/{analysis_id}/status.json",
        json={"analysis": {"_id": analysis_id, "id": analysis_id, "status": "started"}},
    )
    responses.get(
        f"{BASE}/analyses/{analysis_id}/status.json",
        json={"analysis": {"_id": analysis_id, "id": analysis_id, "status": "started"}},
    )
    responses.get(f"{BASE}/analyses/{analysis_id}/soft_stop", status=200, json={"result": "ok"})


def _escalation_path_responses(analysis_id: str) -> None:
    """Stage responses for the new (issue #83 D2) escalation path.

    Pre-#83 escalation matched started-datapoint ``ip_address`` against
    worker pod IPs (heavy ``/data_points.json``). Post-#83 D2 the SLA
    tick asks Redis (here a fake client) for the workers currently
    processing the analysis and maps each worker id's hostname segment
    back to a pod; ``/data_points.json`` is no longer consulted.
    """
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": analysis_id, "created_at": "2026-07-01T00:00:00Z"}],
    )
    responses.get(
        f"{BASE}/analyses/{analysis_id}/status.json",
        json={"analysis": {"_id": analysis_id, "id": analysis_id, "status": "started"}},
    )
    # issue #707: stop_analysis pre-escalation step
    responses.post(
        f"{BASE}/analyses/{analysis_id}/action",
        json={"status": "ok"},
        status=200,
    )


class _FakeRedisForDryRun:
    """Minimal Resque client fake for the walkthrough tests (#83 D2).

    The walkthrough's escalation test stages a single Resque worker
    whose payload references the analysis; the operator's
    ``workers_for_analysis`` returns that worker id, and the matching
    pod is the one named in the worker id's hostname segment.
    """

    def __init__(self, workers: dict[str, list[str]] | None = None) -> None:
        self._workers = dict(workers if workers is not None else {})

    def workers_for_analysis(self, analysis_id: str) -> list[str]:
        return [wid for wid, aids in self._workers.items() if analysis_id in aids]

    def pod_name_for_worker(self, worker_id: str) -> str | None:
        from openstudio_operator.redis_client import ReadOnlyRedisClient

        return ReadOnlyRedisClient.pod_name_for_worker(self, worker_id)


@responses.activate
def test_dryrun_soft_stop_is_strict_suppression():
    """D11 contract: only the soft_stop REST call flips; event/metric/anchor are otherwise identical.

    Issue #83 D1: the soft-stop fires on the SECOND tick (when the
    first-sight anchor is past ``maxDurationMinutes``). Tick 1 writes
    the ``watching`` anchor; tick 2 (4h later) fires the soft-stop.
    Both tickers see the same D11 contract: only the REST call flips,
    event/metric/anchor are otherwise identical.
    """
    analysis_id = "a1"
    spec_dry = {**SLA_SPEC, "dryRun": True}
    spec_real = {**SLA_SPEC, "dryRun": False}
    cos_before = soft_stops_total()

    # dryRun=True path: two ticks
    _soft_stop_path_responses(analysis_id)
    api_dry = FakeCO(make_cr(spec_dry))
    store_dry = StatusStore(NAMESPACE, NAME, api_dry)
    cfg_dry = OperatorConfig.from_spec(spec_dry)
    redis_dry = _FakeRedisForDryRun({"worker-1:1:requeued,simulations": []})
    # Tick 1: first sight — no event, anchor written as `watching`.
    events_first, emit_first = make_emit()
    run_sla_tick(
        OpenStudioClient(BASE),
        store_dry,
        cfg_dry,
        now=NOW,
        emit=emit_first,
        namespace=NAMESPACE,
        pod_api=FakePodsCoreV1Api([], filter_label_selector=True),
        redis_client=redis_dry,
    )
    assert events_first == []
    assert api_dry.obj["status"]["softStops"][analysis_id]["outcome"] == "watching"
    before = soft_stops_total()
    # Tick 2: 4h later, anchor past maxDuration → soft-stop.
    events_dry, emit_dry = make_emit()
    run_sla_tick(
        OpenStudioClient(BASE),
        store_dry,
        cfg_dry,
        now=NOW + timedelta(hours=4),
        emit=emit_dry,
        namespace=NAMESPACE,
        pod_api=FakePodsCoreV1Api([], filter_label_selector=True),
        redis_client=redis_dry,
    )
    assert calls_to("/soft_stop") == 0
    assert len(events_dry) == 1
    assert events_dry[0][0] == "Warning"
    assert events_dry[0][1] == ANALYSIS_SOFT_STOPPED_EVENT
    assert "suppressed (spec.dryRun)" in events_dry[0][2]
    assert soft_stops_total() - before == 1
    assert api_dry.obj["status"]["softStops"][analysis_id]["outcome"] == "dry-run"

    # dryRun=False path: same two-tick shape, same one event, same
    # metric, but the mutation actually happened.
    _soft_stop_path_responses(analysis_id)
    api_real = FakeCO(make_cr(spec_real))
    store_real = StatusStore(NAMESPACE, NAME, api_real)
    cfg_real = OperatorConfig.from_spec(spec_real)
    redis_real = _FakeRedisForDryRun({"worker-1:1:requeued,simulations": []})
    events_first, emit_first = make_emit()
    run_sla_tick(
        OpenStudioClient(BASE),
        store_real,
        cfg_real,
        now=NOW,
        emit=emit_first,
        namespace=NAMESPACE,
        pod_api=FakePodsCoreV1Api([], filter_label_selector=True),
        redis_client=redis_real,
    )
    before = soft_stops_total()
    events_real, emit_real = make_emit()
    run_sla_tick(
        OpenStudioClient(BASE),
        store_real,
        cfg_real,
        now=NOW + timedelta(hours=4),
        emit=emit_real,
        namespace=NAMESPACE,
        pod_api=FakePodsCoreV1Api([], filter_label_selector=True),
        redis_client=redis_real,
    )
    assert calls_to("/soft_stop") == 1
    assert len(events_real) == 1
    assert events_real[0][:2] == events_dry[0][:2]
    assert "suppressed (spec.dryRun)" not in events_real[0][2]
    assert "issued" in events_real[0][2]
    assert soft_stops_total() - before == 1
    assert api_real.obj["status"]["softStops"][analysis_id]["outcome"] == "issued"

    # Net metric delta across both tickers' tick-2 fires: 2 (one per decision)
    assert soft_stops_total() - cos_before == 2


@responses.activate
def test_dryrun_escalation_is_strict_suppression():
    """D11 contract (escalation): only pod delete flips; event/metric/anchor otherwise identical.

    Issue #83 D2: the escalation resolves victim pods through the
    Resque worker set, not datapoint ``ip_address`` matching. The test
    stages a single Resque worker for the analysis; the matching pod
    is the worker id's hostname segment. ``/data_points.json`` is
    no longer consulted.
    """
    analysis_id = "a1"
    base = {**SLA_SPEC, "analysisPolicy": {**SLA_SPEC["analysisPolicy"], "gracefulStopTimeoutMinutes": GRACE_MIN}}
    spec_dry = {**base, "dryRun": True}
    spec_real = {**base, "dryRun": False}
    anchored = {"softStops": {analysis_id: {"issuedAt": PAST_GRACE.isoformat(), "outcome": "issued"}}}
    cos_before = worker_pods_evicted_total()

    def run_one(spec: dict, *, real: bool):
        _escalation_path_responses(analysis_id)
        api = FakeCO(make_cr(spec, status=anchored))
        pods = FakePodsCoreV1Api([_make_pod("worker-1", "10.0.0.1")], filter_label_selector=True)
        redis_client = _FakeRedisForDryRun(
            {"worker-1:1:requeued,simulations": [analysis_id]}
        )
        before = worker_pods_evicted_total()
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
            redis_client=redis_client,
        )
        return api, pods, events, worker_pods_evicted_total() - before

    # dryRun=True
    api_dry, pods_dry, events_dry, delta_dry = run_one(spec_dry, real=False)
    assert pods_dry.deletes == []
    # issue #707: two events — AnalysisStopped (stop suppressed) then AnalysisEscalated (pod deletions suppressed)
    assert len(events_dry) == 2
    assert events_dry[-1][:2] == ("Warning", ANALYSIS_ESCALATED_EVENT)
    assert "suppressed (spec.dryRun)" in events_dry[-1][2]
    assert delta_dry == 1
    assert api_dry.obj["status"]["softStops"][analysis_id]["escalationOutcome"] == "dry-run"

    # dryRun=False (real eviction)
    api_real, pods_real, events_real, delta_real = run_one(spec_real, real=True)
    assert [d["name"] for d in pods_real.deletes] == ["worker-1"]
    # issue #707: two events — AnalysisStopped then AnalysisEscalated
    assert len(events_real) == 2
    assert events_real[-1][:2] == ("Warning", ANALYSIS_ESCALATED_EVENT)
    assert "suppressed (spec.dryRun)" not in events_real[-1][2]
    assert delta_real == 1
    assert api_real.obj["status"]["softStops"][analysis_id]["escalationOutcome"] == "evicted"

    # Net: 2 deletes counted (one dry, one real) — the metric counts the decision.
    assert worker_pods_evicted_total() - cos_before == 2


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
    """Patch-only AppsV1Api stand-in for the worker-recycle section.

    Kept local deliberately (#653): records-only (no store, no
    merge-application) — the walkthrough asserts the OUTGOING patch body,
    and the shared FakeAppsV1Api's apply-to-``obj`` surface is surplus
    narrative here.
    """

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
    cos_before = workers_recycled_total()

    def run_one(spec: dict):
        _wr_responses()
        api = FakeCO(make_cr(spec))
        apps = FakeAppsWR()
        before = workers_recycled_total()
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
            workers_recycled_total() - before,
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

    assert workers_recycled_total() - cos_before == 2


# --- Module 4: retention pipeline (runs in the storage-prune CronJob, #78) -----

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
    """Minimal BatchV1Api stand-in for the pruner section.

    Kept local deliberately (#653): the walkthrough only asserts the
    creates/deletes LEDGERS — no 404/409 semantics, no job store — and the
    shared ``_fakes.FakeBatchV1Api``'s faithful conflict/missing surfaces
    are surplus narrative here.
    """

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
    deleted_before = analyses_deleted_total()
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

    _batch_dry, events_dry, result_dry, api_dry = run_one(spec_dry)
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

    _batch_real, events_real, result_real, api_real = run_one(spec_real)
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
    assert analyses_deleted_total() - deleted_before == 1


# --- Module 5: web_background stall detector ----------------------------------


WBM_SPEC = {
    "serverUrl": BASE,
    "redisUrl": "redis://:pw@queue.test:6379",
    "targetWorkerDeployment": WORKER,
    "targetWebBackgroundDeployment": WEBBG,
        "webBackgroundPolicy": {"stallWindowMinutes": 10},
}


class FakeAppsWBM:
    """Read+patch AppsV1Api stand-in pinned to the WBM selector.

    Kept local deliberately (#653): serves the walkthrough's own
    ``{"component": "worker"}`` selector AND patches without applying —
    the D11 diff here reads the outgoing body only.
    """

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


def _stall_redis(at: datetime) -> ReadOnlyRedisClient:
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    fake.rpush(SIMULATIONS_QUEUE, "j1")
    fake.rpush(SIMULATIONS_QUEUE, "j2")
    epoch = at.timestamp()
    fake.sadd("resque:workers", "w1")
    # LIVE-VERIFIED v3.11.0 layout (#66): heartbeat HASH with ISO8601 values.
    stale_iso = datetime.fromtimestamp(epoch - 600, tz=UTC).isoformat()
    fake.hset("resque:workers:heartbeat", "w1", stale_iso)  # stale (>300s)
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
    pods = FakePodsCoreV1Api([_pod_running(), _pod_running()])
    store = StatusStore(NAMESPACE, NAME, api)
    cfg = OperatorConfig.from_spec(spec)
    for offset in (0, 5):
        _events, emit = make_emit()
        leg2_state = Leg2SafeguardState()
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
            leg2_state=leg2_state,
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
        pods = FakePodsCoreV1Api([_pod_running(), _pod_running()])
        before = metric("openstudio_operator_web_background_restarts_total")
        store = StatusStore(NAMESPACE, NAME, api)
        cfg = OperatorConfig.from_spec(spec)
        events, emit = make_emit()
        leg2_state = Leg2SafeguardState()
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
            leg2_state=leg2_state,
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


# --- Issue #727 drift-fence: LABELED_COUNTER_OUTCOMES � metric docstrings ------


def test_labeled_counter_outcomes_match_metric_docstrings():
    """Issue #727 — drift-fence for :data:`LABELED_COUNTER_OUTCOMES`.

    The metric docstrings in ``metrics.py`` are the source of truth for
    which labelled outcomes each Counter publishes — every ``inc(...)``
    site uses one of those string values. ``LABELED_COUNTER_OUTCOMES``
    is the test-side mirror of those docstrings; the four ``*_total()``
    helpers above read from it via :func:`sum_labelled_counter`.

    This test walks each metric's docstring for backtick-quoted
    identifier-shaped strings (e.g. ``dry-run``, ``evicted-partial``),
    filters out the label name itself (the "Labelled by ``outcome``"
    marker) plus a small set of code-reference / narrative tokens that
    share the same backtick identifier shape, and asserts the remaining
    set equals the outcomes tuple in ``LABELED_COUNTER_OUTCOMES``.

    Contract: a future outcome added at the metric site (the docstring
    cites a new ``<backtick>`` value) fails THIS test until the dict is
    updated to match — forcing the test author to acknowledge the new
    outcome before the walkthrough goes silent on it.
    """
    import re

    backticked_identifier = re.compile(r"``([a-z][a-z0-9]*(?:[-_][a-z0-9]+)*)``")

    # Code-reference / narrative tokens that share the backtick-identifier
    # shape in the docstrings but aren't outcomes. Add to this set only
    # when a docstring legitimately gains a new non-outcome identifier —
    # the per-metric assertion below fails FIRST if a real outcome is
    # added, forcing acknowledgment of the new outcome in
    # ``LABELED_COUNTER_OUTCOMES``.
    narrative_tokens = {
        "issue",  # "issue #309" / "issue #10"
        "incremented",  # "Incremented at the ..."
        "run_sla_tick", "run_recycler_tick", "run_retention_tick", "run_stall_tick",
        "_escalate_analysis", "_stall_condition_holds", "_armed_trigger",
        "inc", "evicted_count", "failed_count",
    }

    cases = [
        (_metrics.SOFT_STOPS_TOTAL, "openstudio_operator_soft_stops_total"),
        (
            _metrics.WORKERS_RECYCLED_TOTAL,
            "openstudio_operator_workers_recycled_total",
        ),
        (
            _metrics.WORKER_PODS_EVICTED_TOTAL,
            "openstudio_operator_worker_pods_evicted_total",
        ),
        (
            _metrics.ANALYSES_DELETED_TOTAL,
            "openstudio_operator_analyses_deleted_total",
        ),
    ]

    for counter, metric_name in cases:
        # The walkthrough must keep its dict entry for this Counter.
        assert metric_name in LABELED_COUNTER_OUTCOMES, (
            f"LABELED_COUNTER_OUTCOMES missing entry for {metric_name!r}; "
            f"the walkthrough still needs to sum this Counter"
        )

        label_names = list(getattr(counter, "_labelnames", ()))
        assert len(label_names) == 1, (
            f"{metric_name}: expected a single label name, got {label_names!r}"
        )
        label_name = label_names[0]

        docstring = counter._documentation or ""
        backticked = set(backticked_identifier.findall(docstring))
        # Outcomes = backticked identifiers, minus the label name, minus
        # the narrative tokens (code references + English narrative words).
        docstring_outcomes = backticked - {label_name} - narrative_tokens

        dict_label, dict_values = LABELED_COUNTER_OUTCOMES[metric_name]
        assert dict_label == label_name, (
            f"{metric_name}: LABELED_COUNTER_OUTCOMES label {dict_label!r} "
            f"disagrees with metric _labelnames {label_name!r}"
        )
        assert set(dict_values) == docstring_outcomes, (
            f"{metric_name}: LABELED_COUNTER_OUTCOMES outcomes "
            f"{sorted(dict_values)!r} disagrees with metric docstring "
            f"outcomes {sorted(docstring_outcomes)!r}. "
            f"Update LABELED_COUNTER_OUTCOMES to add the new outcome "
            f"(or fix the docstring)."
        )


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
    # Issue #83 D1: the SLA clock anchor is the operator's first sight of
    # the analysis in ``started`` via ``/status.json``; the soft-stop fires
    # on the SECOND tick (when the anchor's age exceeds the 180m
    # ``maxDurationMinutes``).
    responses.get(
        f"{BASE}/analyses.json",
        json=[
            {"_id": analysis_id, "created_at": "2026-07-01T00:00:00Z"},
            {"_id": "a-done", "created_at": "2026-07-01T00:00:00Z"},
        ],
    )
    responses.get(
        f"{BASE}/analyses/{analysis_id}/status.json",
        json={"analysis": {"_id": analysis_id, "id": analysis_id, "status": "started"}},
    )
    responses.get(
        f"{BASE}/analyses/{analysis_id}/status.json",
        json={"analysis": {"_id": analysis_id, "id": analysis_id, "status": "started"}},
    )
    responses.get(
        f"{BASE}/analyses/a-done/status.json",
        json={"analysis": {"_id": "a-done", "id": "a-done", "status": "completed"}},
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

    # Module 1 (soft-stop) — first tick: first sight of started → no
    # soft-stop, anchor written as ``watching`` (issue #83 D1).
    pods_api = FakePodsCoreV1Api([_make_pod("worker-1", "10.0.0.1")], filter_label_selector=True)
    redis_client = _FakeRedisForDryRun({"worker-1:1:requeued,simulations": [analysis_id]})
    events, emit = make_emit()
    run_sla_tick(
        OpenStudioClient(BASE),
        store,
        cfg,
        now=NOW,
        emit=emit,
        namespace=NAMESPACE,
        pod_api=pods_api,
        redis_client=redis_client,
    )
    assert store.get_soft_stops()[analysis_id].outcome == "watching"
    # Second tick: 4h later, anchor past maxDuration → soft-stop.
    events, emit = make_emit()
    run_sla_tick(
        OpenStudioClient(BASE),
        store,
        cfg,
        now=NOW + timedelta(hours=4),
        emit=emit,
        namespace=NAMESPACE,
        pod_api=pods_api,
        redis_client=redis_client,
    )
    soft_stop_events = [e for e in events if e[1] == ANALYSIS_SOFT_STOPPED_EVENT]
    assert len(soft_stop_events) == 1
    assert "suppressed (spec.dryRun)" in soft_stop_events[0][2]
    assert "soft stop issued" not in soft_stop_events[0][2]
    assert pods_api.deletes == []
    assert store.get_soft_stops()[analysis_id].outcome == "dry-run"
    # Module 1b (escalation) — pre-seed the anchor at PAST_GRACE so the
    # grace check fires the same tick (matches the pre-#83 test).
    anchored = {
        "softStops": {
            analysis_id: {"issuedAt": PAST_GRACE.isoformat(), "outcome": "issued"}
        },
        "startedSince": {dp_id: (NOW - timedelta(hours=2)).isoformat()},
    }
    api.obj["status"] = copy.deepcopy(anchored)
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": analysis_id, "created_at": "2026-07-01T00:00:00Z"}],
    )
    responses.get(
        f"{BASE}/analyses/{analysis_id}/status.json",
        json={"analysis": {"_id": analysis_id, "id": analysis_id, "status": "started"}},
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
        redis_client=redis_client,
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
    pods_wbm = FakePodsCoreV1Api([_pod_running(), _pod_running()])
    tracker = StallWindowTracker()
    for offset in (0, 5):
        events, emit = make_emit()
        leg2_state = Leg2SafeguardState()
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
            leg2_state=leg2_state,
        )
    events, emit = make_emit()
    leg2_state = Leg2SafeguardState()
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
        leg2_state=leg2_state,
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

    # Cumulative invariants after the full walkthrough with dryRun=True:
    # every decision counted (D11: metrics are decisions, not mutations),
    # every mutation-suppressed Event carried the marker, every state
    # anchor advanced as in a real run.
    diffs = {
        "openstudio_operator_soft_stops_total": soft_stops_total()
        - cos["openstudio_operator_soft_stops_total"],
        "openstudio_operator_worker_pods_evicted_total": worker_pods_evicted_total()
        - cos["openstudio_operator_worker_pods_evicted_total"],
        "openstudio_operator_datapoints_requeued_total": metric(
            "openstudio_operator_datapoints_requeued_total"
        )
        - cos["openstudio_operator_datapoints_requeued_total"],
        "openstudio_operator_workers_recycled_total": workers_recycled_total()
        - cos["openstudio_operator_workers_recycled_total"],
        "openstudio_operator_web_background_restarts_total": metric(
            "openstudio_operator_web_background_restarts_total"
        )
        - cos["openstudio_operator_web_background_restarts_total"],
        "openstudio_operator_analyses_archived_total": metric(
            "openstudio_operator_analyses_archived_total"
        )
        - cos["openstudio_operator_analyses_archived_total"],
        "openstudio_operator_analyses_deleted_total": analyses_deleted_total()
        - cos["openstudio_operator_analyses_deleted_total"],
    }
    assert diffs == {
        "openstudio_operator_soft_stops_total": 1,
        "openstudio_operator_worker_pods_evicted_total": 1,
        "openstudio_operator_datapoints_requeued_total": 1,
        "openstudio_operator_workers_recycled_total": 1,
        "openstudio_operator_web_background_restarts_total": 1,
        "openstudio_operator_analyses_archived_total": 0,  # no Complete observed
        "openstudio_operator_analyses_deleted_total": 0,  # nothing verified
    }
