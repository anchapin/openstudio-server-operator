"""Unit tests for the retention pipeline (plan Module 4; #16, D09; actor move #78).

REST is mocked with ``responses`` (analyses index, heavy data_points index,
DELETE cascade), the CR ``.status`` subresource with the in-memory RFC 7386
merge-patch fake (same approach as test_analysis_sla.py / test_status_store.py)
and the Kubernetes batch API with ``FakeBatchV1Api`` built on generated-client
shapes (attribute-style Job objects, 404/409 ``ApiException``s). All
assertions target ``run_retention_tick`` directly; the CronJob entrypoint
(``openstudio_operator.prune_entrypoint``, covered by
test_prune_entrypoint.py) is thin wiring around it.

THE CARDINAL RULE is asserted as its own test
(``test_cardinal_never_deleted_without_verified_success``): pending, failed
and vanished Jobs all leave the analysis undeleted.

RBAC note (#78): the Job create/read/delete asserted here are covered by the
``batch/jobs`` verbs granted to the storage-pruner ServiceAccount's Role in
``deploy/storage-cronjob.yaml`` — the operator's own Role no longer holds
any batch permissions.
"""

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import responses
from kubernetes.client import ApiException
from prometheus_client import REGISTRY

from openstudio_operator.archival import archival_job_name, build_archival_job
from openstudio_operator.config import OperatorConfig, StoragePolicy
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.retention import (
    ANALYSIS_ARCHIVAL_FAILED_EVENT,
    ANALYSIS_ARCHIVAL_STARTED_EVENT,
    ANALYSIS_ARCHIVAL_SUCCEEDED_EVENT,
    ANALYSIS_DELETED_EVENT,
    run_retention_tick,
)
from openstudio_operator.status_store import StatusStore

BASE = "http://web.test"
NAMESPACE = "openstudio-server"
NAME = "oscm"
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

STORAGE = {
    "archiveToS3": True,
    "backend": "s3",
    "bucket": "os-archives",
    "secretRef": "archive-creds",
    "retentionDays": 7,
    "purgeCompletedNFSFiles": True,
}
SPEC = {"serverUrl": BASE, "storagePolicy": dict(STORAGE)}


def make_cr(spec: dict | None = None, status: dict | None = None) -> dict:
    return {
        "apiVersion": "energy.nrel.gov/v1alpha1",
        "kind": "OpenStudioClusterManager",
        "metadata": {"name": NAME, "namespace": NAMESPACE},
        "spec": copy.deepcopy(spec if spec is not None else SPEC),
        "status": copy.deepcopy(status if status is not None else {}),
    }


def archiving_status(analysis_id: str, job_name: str) -> dict:
    """Persisted in-flight record: an archival Job is being watched (operator restart state)."""
    return {
        "archivedAnalyses": {
            analysis_id: {
                "backend": "s3",
                "bucket": "os-archives",
                "jobName": job_name,
                "spawnedAt": (NOW - timedelta(minutes=5)).isoformat(),
            }
        }
    }


def dry_marker_status(analysis_id: str) -> dict:
    """Persisted dry-run marker: spawn was suppressed, nothing to watch (no jobName)."""
    return {
        "archivedAnalyses": {
            analysis_id: {
                "backend": "s3",
                "bucket": "os-archives",
                "spawnedAt": (NOW - timedelta(minutes=5)).isoformat(),
            }
        }
    }


def verified_status(analysis_id: str, job_name: str) -> dict:
    return {
        "archivedAnalyses": {
            analysis_id: {
                "backend": "s3",
                "bucket": "os-archives",
                "jobName": job_name,
                "spawnedAt": (NOW - timedelta(minutes=10)).isoformat(),
                "verifiedAt": (NOW - timedelta(minutes=2)).isoformat(),
            }
        }
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


def archived_total() -> float:
    return REGISTRY.get_sample_value("openstudio_operator_analyses_archived_total") or 0.0


def deleted_total() -> float:
    return REGISTRY.get_sample_value("openstudio_operator_analyses_deleted_total") or 0.0


def analysis(
    analysis_id: str,
    *,
    status: str = "completed",
    age_days: float = 10.0,
    created_age_days: float | None = None,
    with_updated: bool = True,
) -> dict:
    """Raw analysis doc; timestamps as the wire sends them (client normalizes)."""
    doc: dict = {"_id": analysis_id, "status": status}
    doc["created_at"] = (
        NOW - timedelta(days=created_age_days if created_age_days is not None else age_days + 20)
    ).isoformat()
    if with_updated:
        doc["updated_at"] = (NOW - timedelta(days=age_days)).isoformat()
    return doc


def register_analyses(*docs: dict) -> None:
    responses.get(f"{BASE}/analyses.json", json=list(docs))


def register_delete(analysis_id: str) -> None:
    responses.delete(f"{BASE}/analyses/{analysis_id}", status=204)


def register_datapoints(docs: list[dict]) -> None:
    responses.get(f"{BASE}/data_points.json", json=docs)


def make_job(name: str, *, complete: bool = False, failed: bool = False, running: bool = True):
    """Generated-client Job shape (attribute-style V1Job duck type).

    ``running`` with a non-terminal condition present but not True exercises
    the condition filter (a Job that exists but has not terminated).
    """
    conditions = []
    if complete:
        conditions.append(SimpleNamespace(type="Complete", status="True"))
    if failed:
        conditions.append(SimpleNamespace(type="Failed", status="True"))
    if running and not complete and not failed:
        conditions.append(SimpleNamespace(type="Complete", status="False"))
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(conditions=conditions or None, succeeded=0, failed=0),
    )


class FakeBatchV1Api:
    """BatchV1Api stand-in: in-memory Jobs with faithful 404/409 semantics.

    The create/read/delete exercised here are the ``batch/jobs`` verbs from
    deploy/storage-cronjob.yaml (the prune CronJob's Role, #78).
    """

    def __init__(self, jobs: list | None = None) -> None:
        self.jobs = {job.metadata.name: job for job in (jobs or [])}
        self.reads: list[dict] = []
        self.creates: list[dict] = []
        self.deletes: list[dict] = []

    def read_namespaced_job(self, name, namespace, **kwargs):
        self.reads.append({"name": name, "namespace": namespace})
        if name not in self.jobs:
            raise ApiException(status=404, reason="Not Found")
        return self.jobs[name]

    def create_namespaced_job(self, namespace, body, **kwargs):
        name = body["metadata"]["name"]
        if name in self.jobs:
            raise ApiException(status=409, reason="Conflict")
        job = make_job(name)
        self.jobs[name] = job
        self.creates.append({"namespace": namespace, "body": copy.deepcopy(body)})
        return job

    def delete_namespaced_job(self, name, namespace, **kwargs):
        self.deletes.append({"name": name, "namespace": namespace, "kwargs": kwargs})
        if name not in self.jobs:
            raise ApiException(status=404, reason="Not Found")
        del self.jobs[name]
        return {}


def tick(api, spec=None, client=None, *, batch_api, now=NOW):
    store = StatusStore(NAMESPACE, NAME, api)
    config = OperatorConfig.from_spec(spec if spec is not None else SPEC)
    events, emit = make_emit()
    result = run_retention_tick(
        client if client is not None else OpenStudioClient(BASE),
        store,
        config,
        now=now,
        emit=emit,
        namespace=NAMESPACE,
        batch_api=batch_api,
    )
    return result, events


# --- Eligibility (retention clock) ---------------------------------------------


@responses.activate
def test_eligibility_clock_is_updated_at_not_created_at():
    """Identical created_at (30d); only the one completed 8d ago is due — the
    retention clock is the doc's updated_at (completion proxy), never created_at."""
    register_analyses(
        analysis("a-old", age_days=8, created_age_days=30),
        analysis("a-new", age_days=6, created_age_days=30),
    )
    register_datapoints([])
    register_delete("a-old")
    register_delete("a-new")
    api = FakeCustomObjectsApi(make_cr())

    result, events = tick(api, batch_api=FakeBatchV1Api())

    assert result.spawned == ["a-old"]
    assert len(events) == 1 and "a-old" in events[0][2]
    assert calls_to("/data_points.json") == 1  # fetched only for the one due spawn


@responses.activate
def test_eligibility_boundary_exact_retention_age_counts():
    register_analyses(analysis("a-edge", age_days=7))
    register_datapoints([])
    api = FakeCustomObjectsApi(make_cr())

    result, _ = tick(api, batch_api=FakeBatchV1Api())

    assert result.spawned == ["a-edge"]  # >= : exactly retentionDays old is elapsed


@responses.activate
def test_retention_days_zero_is_immediately_eligible():
    """The owner's batch workflow: retentionDays 0 → a completion one second old is due."""
    register_analyses(analysis("a-fresh", age_days=1 / 86400))
    register_datapoints([])
    api = FakeCustomObjectsApi(make_cr(spec={**SPEC, "storagePolicy": {**STORAGE, "retentionDays": 0}}))

    result, _ = tick(api, spec={**SPEC, "storagePolicy": {**STORAGE, "retentionDays": 0}}, batch_api=FakeBatchV1Api())

    assert result.spawned == ["a-fresh"]


@responses.activate
def test_non_completed_and_timestampless_analyses_never_eligible():
    register_analyses(
        analysis("a-run", status="started", age_days=30),
        analysis("a-nostamp", age_days=30, with_updated=False, created_age_days=None) | {"created_at": None},
    )
    api = FakeCustomObjectsApi(make_cr())

    result, events = tick(api, batch_api=FakeBatchV1Api())

    assert result.spawned == []
    assert events == []
    assert calls_to("/data_points.json") == 0  # lazy fetch: nothing due → never fetched


@responses.activate
def test_missing_updated_at_falls_back_to_created_at():
    register_analyses(analysis("a-fb", age_days=10, with_updated=False, created_age_days=10))
    register_datapoints([])
    api = FakeCustomObjectsApi(make_cr())

    result, _ = tick(api, batch_api=FakeBatchV1Api())

    assert result.spawned == ["a-fb"]


# --- Spawn ----------------------------------------------------------------------


@responses.activate
def test_spawn_creates_deterministic_job_writes_inflight_record_and_events():
    register_analyses(analysis("a1"))
    register_datapoints([])
    api = FakeCustomObjectsApi(make_cr())
    batch = FakeBatchV1Api()

    result, events = tick(api, batch_api=batch)

    job_name = archival_job_name("a1")
    assert result.spawned == ["a1"]
    assert len(batch.creates) == 1
    body = batch.creates[0]["body"]
    assert batch.creates[0]["namespace"] == NAMESPACE
    assert body["metadata"]["name"] == job_name
    record = api.obj["status"]["archivedAnalyses"]["a1"]
    assert record["backend"] == "s3" and record["bucket"] == "os-archives"
    assert record["jobName"] == job_name
    assert record["spawnedAt"] == NOW.isoformat()
    assert "verifiedAt" not in record  # in-flight, NOT verified
    assert len(events) == 1
    event_type, reason, message = events[0]
    assert event_type == "Normal"
    assert reason == ANALYSIS_ARCHIVAL_STARTED_EVENT
    assert "a1" in message and "retentionDays=7" in message and "suppressed" not in message


@responses.activate
def test_spawn_manifest_archives_the_analysiss_datapoint_trees():
    register_analyses(analysis("a1"))
    register_datapoints(
        [
            {"_id": "dp-1", "analysis_id": "a1", "status": "completed"},
            {"_id": "dp-2", "analysis_id": "a1", "status": "completed"},
            {"_id": "dp-other", "analysis_id": "someone-else", "status": "completed"},
        ]
    )
    api = FakeCustomObjectsApi(make_cr())
    batch = FakeBatchV1Api()

    tick(api, batch_api=batch)

    script = batch.creates[0]["body"]["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert "analyses/a1" in script
    assert "data_points/dp-1" in script and "data_points/dp-2" in script
    assert "data_points/dp-other" not in script  # other analyses' dps never archived here


# --- Job watching (phase A) ------------------------------------------------------


@responses.activate
def test_inflight_record_with_running_job_never_spawns_a_second_job():
    """In-flight dedup across ticks (and across restarts — same persisted record)."""
    job_name = archival_job_name("a1")
    register_analyses(analysis("a1"), analysis("a1"))  # two ticks' polls
    api = FakeCustomObjectsApi(make_cr(status=archiving_status("a1", job_name)))
    batch = FakeBatchV1Api([make_job(job_name)])

    result, events = tick(api, batch_api=batch)
    result2, events2 = tick(api, batch_api=batch)

    assert result.spawned == [] and result2.spawned == []
    assert events == [] and events2 == []
    assert batch.creates == []
    assert calls_to("/data_points.json") == 0  # nothing due: heavy poll never happens


@responses.activate
def test_orphan_inflight_job_without_record_is_adopted_not_respawned():
    """Record-write race (create succeeded, status write lost): read-first adopt."""
    job_name = archival_job_name("a1")
    register_analyses(analysis("a1"))
    api = FakeCustomObjectsApi(make_cr())
    batch = FakeBatchV1Api([make_job(job_name)])

    result, _ = tick(api, batch_api=batch)

    assert batch.creates == []
    assert result.spawned == []  # adoption is not a spawn decision
    record = api.obj["status"]["archivedAnalyses"]["a1"]
    assert record["jobName"] == job_name and "verifiedAt" not in record


@responses.activate
def test_restart_midflight_resumes_watching_rather_than_respawning():
    job_name = archival_job_name("a1")
    register_analyses(analysis("a1"), analysis("a1"))  # two ticks' polls
    register_delete("a1")
    api = FakeCustomObjectsApi(make_cr(status=archiving_status("a1", job_name)))
    batch = FakeBatchV1Api([make_job(job_name)])

    # Fresh client/store per tick = fresh operator process; persisted record drives resume.
    result, events = tick(api, client=OpenStudioClient(BASE), batch_api=batch)
    assert result.spawned == [] and result.deleted == []
    assert events == [] and batch.creates == []
    assert api.patch_calls == 0  # pure watching: nothing written

    batch.jobs[job_name] = make_job(job_name, complete=True, running=False)
    result2, events2 = tick(api, client=OpenStudioClient(BASE), batch_api=batch)

    assert result2.verified == ["a1"] and result2.deleted == ["a1"]
    assert batch.creates == []  # resumed + verified + deleted without ever respawning
    assert [reason for _, reason, _ in events2] == [
        ANALYSIS_ARCHIVAL_SUCCEEDED_EVENT,
        ANALYSIS_DELETED_EVENT,
    ]


@responses.activate
def test_job_success_verifies_then_deletes_and_prunes_status():
    job_name = archival_job_name("a1")
    register_analyses(analysis("a1"))
    register_analyses()  # second tick's poll: the delete removed a1 from the API
    register_delete("a1")
    api = FakeCustomObjectsApi(make_cr(status=archiving_status("a1", job_name)))
    batch = FakeBatchV1Api([make_job(job_name, complete=True, running=False)])
    archived_before, deleted_before = archived_total(), deleted_total()

    result, events = tick(api, batch_api=batch)

    assert result.verified == ["a1"] and result.deleted == ["a1"]
    assert calls_to("/analyses/a1") == 1  # DELETE /analyses/{id} — the cascade
    assert archived_total() - archived_before == 1
    assert deleted_total() - deleted_before == 1
    assert [(t, r) for t, r, _ in events] == [
        ("Normal", ANALYSIS_ARCHIVAL_SUCCEEDED_EVENT),
        ("Normal", ANALYSIS_DELETED_EVENT),
    ]
    assert "verification" in events[0][2] and "cascade" in events[1][2]
    assert api.obj["status"].get("archivedAnalyses", {}) == {}  # pruned after deletion

    result2, events2 = tick(api, batch_api=batch)  # post-delete tick is quiet
    assert result2.spawned == [] and result2.verified == [] and result2.deleted == []
    assert events2 == []
    assert calls_to("/analyses/a1") == 1  # no second delete


@responses.activate
def test_job_failure_retains_analysis_warns_and_retries_next_tick():
    job_name = archival_job_name("a1")
    register_analyses(analysis("a1"), analysis("a1"))  # two ticks' polls
    register_datapoints([])  # retry tick needs dp ids for the fresh manifest
    register_delete("a1")
    api = FakeCustomObjectsApi(make_cr(status=archiving_status("a1", job_name)))
    batch = FakeBatchV1Api([make_job(job_name, failed=True, running=False)])

    # Tick 1: failure detected — Warning, marker cleared, analysis RETAINED.
    result, events = tick(api, batch_api=batch)
    assert result.deleted == [] and result.verified == []
    assert calls_to("/analyses/a1") == 0
    assert len(events) == 1
    event_type, reason, message = events[0]
    assert event_type == "Warning"
    assert reason == ANALYSIS_ARCHIVAL_FAILED_EVENT
    assert "a1" in message and "RETAINED" in message and "retry" in message
    assert api.obj["status"].get("archivedAnalyses", {}) == {}

    # Tick 2: retry — failed Job removed (deterministic name freed), fresh Job spawned.
    result2, events2 = tick(api, batch_api=batch)
    assert result2.spawned == ["a1"]
    assert calls_to("/analyses/a1") == 0  # STILL never deleted without verified success
    assert [d["name"] for d in batch.deletes] == [job_name]
    assert batch.deletes[0]["kwargs"]["propagation_policy"] == "Foreground"
    assert len(batch.creates) == 1 and batch.creates[0]["body"]["metadata"]["name"] == job_name
    record = api.obj["status"]["archivedAnalyses"]["a1"]
    assert record["jobName"] == job_name and "verifiedAt" not in record
    # The retry's fresh spawn announces itself (the failure already warned once).
    assert [reason for _, reason, _ in events2] == [ANALYSIS_ARCHIVAL_STARTED_EVENT]


@responses.activate
def test_cardinal_never_deleted_without_verified_success():
    """THE acceptance test: pending, failed and vanished Jobs all → NO delete."""
    job_name = archival_job_name("a1")
    for batch_api in (
        FakeBatchV1Api([make_job(job_name)]),  # pending/running (condition not True)
        FakeBatchV1Api([make_job(job_name, failed=True, running=False)]),  # terminal failure
        FakeBatchV1Api(),  # Job object vanished mid-flight
    ):
        register_analyses(analysis("a1"))
        register_delete("a1")
        api = FakeCustomObjectsApi(make_cr(status=archiving_status("a1", job_name)))
        deletes_before = calls_to("/analyses/a1")

        result, events = tick(api, batch_api=batch_api)

        assert result.deleted == [] and result.verified == []
        assert calls_to("/analyses/a1") - deletes_before == 0  # DELETE never issued
        if batch_api.jobs:  # pending → silent watch; terminal failures → Warning
            assert events == [] or (
                events[0][0] == "Warning" and events[0][1] == ANALYSIS_ARCHIVAL_FAILED_EVENT
            )
        else:
            assert events and events[0][1] == ANALYSIS_ARCHIVAL_FAILED_EVENT


@responses.activate
def test_vanished_analysis_retires_its_record_without_touching_jobs():
    register_analyses()  # a1 gone from the API (e.g. delete succeeded, prune write failed)
    api = FakeCustomObjectsApi(make_cr(status=archiving_status("a1", archival_job_name("a1"))))
    batch = FakeBatchV1Api([make_job(archival_job_name("a1"))])

    result, events = tick(api, batch_api=batch)

    assert result.spawned == [] and result.deleted == []
    assert events == []
    assert batch.reads == []  # the Job is never even consulted
    assert api.obj["status"].get("archivedAnalyses", {}) == {}


@responses.activate
def test_verified_record_deletion_failure_retries_delete_next_tick():
    """Verified earlier but the delete failed (D12 skip) → delete retried, no re-archive."""
    job_name = archival_job_name("a1")
    register_analyses(analysis("a1"), analysis("a1"))
    register_delete("a1")
    register_delete("a1")
    api = FakeCustomObjectsApi(make_cr(status=verified_status("a1", job_name)))
    batch = FakeBatchV1Api([])  # Job TTL'd away — irrelevant once verified
    archived_before = archived_total()

    result, events = tick(api, batch_api=batch)

    assert result.deleted == ["a1"] and result.verified == []
    assert archived_total() - archived_before == 0  # no double-count: verification already recorded
    assert [reason for _, reason, _ in events] == [ANALYSIS_DELETED_EVENT]
    assert api.obj["status"].get("archivedAnalyses", {}) == {}


# --- Module gating ----------------------------------------------------------------


@responses.activate
def test_module_passive_without_archival_policy():
    """archiveToS3 false ⇒ no archival configured ⇒ no deletion ever (cardinal rule)."""
    register_analyses(analysis("a1"))
    api = FakeCustomObjectsApi(make_cr(spec={**SPEC, "storagePolicy": {**STORAGE, "archiveToS3": False}}))
    batch = FakeBatchV1Api()

    result, events = tick(
        api, spec={**SPEC, "storagePolicy": {**STORAGE, "archiveToS3": False}}, batch_api=batch
    )

    assert result.spawned == [] and result.deleted == []
    assert events == []
    assert calls_to("/analyses.json") == 0  # passive before any poll
    assert batch.creates == []


@responses.activate
def test_incomplete_storage_policy_idles():
    register_analyses(analysis("a1"))
    api = FakeCustomObjectsApi(
        make_cr(spec={**SPEC, "storagePolicy": {k: v for k, v in STORAGE.items() if k != "bucket"}})
    )
    batch = FakeBatchV1Api()

    result, _ = tick(
        api,
        spec={**SPEC, "storagePolicy": {k: v for k, v in STORAGE.items() if k != "bucket"}},
        batch_api=batch,
    )

    assert result.spawned == []
    assert calls_to("/analyses.json") == 0
    assert batch.creates == []


@responses.activate
def test_purge_false_archives_but_never_deletes():
    job_name = archival_job_name("a1")
    register_analyses(analysis("a1"), analysis("a1"))  # two ticks: withheld delete persists
    register_delete("a1")
    api = FakeCustomObjectsApi(make_cr(status=archiving_status("a1", job_name)))
    batch = FakeBatchV1Api([make_job(job_name, complete=True, running=False)])
    spec = {**SPEC, "storagePolicy": {**STORAGE, "purgeCompletedNFSFiles": False}}

    result, events = tick(api, spec=spec, batch_api=batch)
    result2, events2 = tick(api, spec=spec, batch_api=batch)

    assert result.verified == ["a1"] and result.deleted == []
    assert calls_to("/analyses/a1") == 0  # never deleted
    assert [reason for _, reason, _ in events] == [ANALYSIS_ARCHIVAL_SUCCEEDED_EVENT]
    assert result2.verified == [] and events2 == []  # quiet on later ticks
    record = api.obj["status"]["archivedAnalyses"]["a1"]
    assert record["verifiedAt"] == NOW.isoformat()  # verified state persists


# --- dryRun (D11) -------------------------------------------------------------------


@responses.activate
def test_dry_run_suppresses_spawn_and_delete_with_observable_tracking():
    dry = {**SPEC, "dryRun": True}
    register_analyses(*( [analysis("a1")] * 4 ))  # four ticks' polls
    register_datapoints([])
    register_delete("a1")
    api = FakeCustomObjectsApi(make_cr(spec=dry))
    batch = FakeBatchV1Api()

    # Tick 1 (dry): spawn suppressed, marker record written, ONE marked event.
    result, events = tick(api, spec=dry, batch_api=batch)
    assert result.spawned == ["a1"]  # the decision was made; the mutation was not
    assert batch.creates == []
    assert calls_to("/analyses/a1") == 0
    assert len(events) == 1
    event_type, reason, message = events[0]
    assert event_type == "Normal" and reason == ANALYSIS_ARCHIVAL_STARTED_EVENT
    assert "suppressed (spec.dryRun)" in message
    record = api.obj["status"]["archivedAnalyses"]["a1"]
    assert "jobName" not in record  # dry-run marker: nothing to watch
    assert record["spawnedAt"] == NOW.isoformat()

    # Tick 2 (still dry): marker inert — no event storm, no spawn.
    _, events2 = tick(api, spec=dry, batch_api=batch)
    assert events2 == [] and batch.creates == []

    # Tick 3 (dryRun lifted): marker cleared; tracked snapshot still blocks a spawn.
    _, events3 = tick(api, batch_api=batch)
    assert events3 == [] and batch.creates == []
    assert api.obj["status"].get("archivedAnalyses", {}) == {}

    # Tick 4: real spawn at last.
    result4, events4 = tick(api, batch_api=batch)
    assert result4.spawned == ["a1"] and len(batch.creates) == 1
    assert api.obj["status"]["archivedAnalyses"]["a1"]["jobName"] == archival_job_name("a1")
    assert events4[0][1] == ANALYSIS_ARCHIVAL_STARTED_EVENT and "suppressed" not in events4[0][2]


@responses.activate
def test_dry_run_suppresses_failed_job_cleanup_delete():
    """#42: flipping dryRun on after a real failed Job must not delete the Job.

    The failed-Job cleanup delete (frees the deterministic name for respawn)
    is a cluster mutation — under spec.dryRun it is suppressed: the failed
    Job object stays for forensics, the (suppressed) spawn writes the
    standard dry-run marker, and the real delete + respawn happen on the
    first tick after dryRun lifts.
    """
    dry = {**SPEC, "dryRun": True}
    job_name = archival_job_name("a1")
    register_analyses(*([analysis("a1")] * 3))  # dry tick + lift tick + real tick polls
    register_datapoints([])  # the real respawn needs dp ids
    register_delete("a1")
    api = FakeCustomObjectsApi(make_cr(spec=dry))
    batch = FakeBatchV1Api([make_job(job_name, failed=True, running=False)])

    # Dry tick: no Job delete, no Job create — only the dry-run marker.
    result, events = tick(api, spec=dry, batch_api=batch)
    assert result.spawned == ["a1"]
    assert batch.deletes == [] and batch.creates == []
    assert job_name in batch.jobs  # failed Job retained for forensics
    assert [reason for _, reason, _ in events] == [ANALYSIS_ARCHIVAL_STARTED_EVENT]
    assert "suppressed (spec.dryRun)" in events[0][2]
    record = api.obj["status"]["archivedAnalyses"]["a1"]
    assert "jobName" not in record  # dry-run marker shape

    # dryRun lifted (tick 2): the dry-run marker is cleared; the pre-reconcile
    # tracked snapshot still blocks a spawn this tick (same cadence as every
    # dry-run-marker lift) — and the failed Job is STILL untouched.
    register_analyses(analysis("a1"))
    _, events2 = tick(api, batch_api=batch)
    assert events2 == []
    assert batch.deletes == [] and batch.creates == []
    assert api.obj["status"].get("archivedAnalyses", {}) == {}

    # Tick 3: the retry delete + real respawn proceed as before.
    register_analyses(analysis("a1"))
    result3, _ = tick(api, batch_api=batch)
    assert result3.spawned == ["a1"]
    assert [d["name"] for d in batch.deletes] == [job_name]
    assert batch.deletes[0]["kwargs"]["propagation_policy"] == "Foreground"
    assert len(batch.creates) == 1 and batch.creates[0]["body"]["metadata"]["name"] == job_name
    assert api.obj["status"]["archivedAnalyses"]["a1"]["jobName"] == job_name


@responses.activate
def test_dry_run_suppresses_delete_of_verified_analysis():
    """Flip dryRun on mid-flight: verification (real Job) proceeds, delete is suppressed."""
    dry = {**SPEC, "dryRun": True}
    job_name = archival_job_name("a1")
    register_analyses(*( [analysis("a1")] * 2 ))
    register_delete("a1")
    api = FakeCustomObjectsApi(make_cr(spec=dry, status=archiving_status("a1", job_name)))
    batch = FakeBatchV1Api([make_job(job_name, complete=True, running=False)])
    archived_before, deleted_before = archived_total(), deleted_total()

    result, events = tick(api, spec=dry, batch_api=batch)

    assert result.verified == ["a1"] and result.deleted == []
    assert calls_to("/analyses/a1") == 0
    assert archived_total() - archived_before == 1  # the Job really did verify
    assert deleted_total() - deleted_before == 0
    assert [(t, r) for t, r, _ in events] == [
        ("Normal", ANALYSIS_ARCHIVAL_SUCCEEDED_EVENT),
        ("Normal", ANALYSIS_DELETED_EVENT),
    ]
    assert "suppressed (spec.dryRun)" in events[1][2]
    record = api.obj["status"]["archivedAnalyses"]["a1"]
    assert record["verifiedAt"] == NOW.isoformat()  # verified state observable, retained

    _, events2 = tick(api, spec=dry, batch_api=batch)  # later ticks: silent, no storm
    assert events2 == []


# --- Backend-agnostic spawn (issue #78 AC3) --------------------------------------


@pytest.mark.parametrize("backend", ["s3", "gcs", "azure"])
@responses.activate
def test_spawn_emits_the_backend_job_template_for_every_cloud_target(backend):
    """AC3: a spawn tick per backend creates EXACTLY the #15 generator's Job.

    The pipeline never mutates the generated manifest — for each of the three
    cloud targets the created object equals ``build_archival_job`` verbatim
    (remote type, envFrom creds, read-only NFS mount, deterministic name),
    which is the unit-level "runs cleanly across AWS, GCP, and Azure" gate.
    """
    policy = StoragePolicy(
        archive_to_s3=True,
        backend=backend,
        bucket="os-archives",
        secret_ref="archive-creds",
        retention_days=7,
        purge_completed_nfs_files=True,
    )
    spec = {
        "serverUrl": BASE,
        "storagePolicy": {
            "archiveToS3": True,
            "backend": backend,
            "bucket": "os-archives",
            "secretRef": "archive-creds",
            "retentionDays": 7,
            "purgeCompletedNFSFiles": True,
        },
    }
    register_analyses(analysis("a1"))
    register_datapoints([{"_id": "dp-1", "analysis_id": "a1"}])
    api = FakeCustomObjectsApi(make_cr(spec=spec))
    batch = FakeBatchV1Api()

    result, _ = tick(api, spec=spec, batch_api=batch)

    assert result.spawned == ["a1"]
    assert len(batch.creates) == 1
    assert batch.creates[0]["body"] == build_archival_job("a1", policy, NAMESPACE, ("dp-1",))
