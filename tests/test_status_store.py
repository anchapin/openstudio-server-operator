"""Unit tests for the CR .status store (issue #7): typed RMW, 409 retry, pruning.

The Kubernetes API is faked with an in-memory CustomObjectsApi stand-in that
speaks RFC 7386 merge-patch semantics (null deletes a key) and can be told to
fail patches with synthetic 409s. No extra test dependencies are used beyond
the [dev] extra (pytest; the kubernetes client ships ApiException).
"""

import copy
from datetime import UTC, datetime, timedelta, timezone

import pytest
from kubernetes.client import ApiException

from openstudio_operator import status_store
from openstudio_operator.status_store import (
    MERGE_PATCH_CONTENT_TYPE,
    ArchivedAnalysisRecord,
    RequeueRecord,
    SoftStopRecord,
    StatusStore,
    StatusStoreConflictError,
    StatusStoreError,
)

NAMESPACE = "openstudio-server"
NAME = "oscm"


def make_soft_stop(outcome: str = "issued") -> SoftStopRecord:
    return SoftStopRecord(issued_at=datetime(2026, 8, 18, 8, 0, 0, tzinfo=UTC), outcome=outcome)


def make_requeue(count: int = 1) -> RequeueRecord:
    return RequeueRecord(count=count, last_requeued_at=datetime(2026, 8, 18, 9, 30, 0, tzinfo=UTC))


def make_archived() -> ArchivedAnalysisRecord:
    return ArchivedAnalysisRecord(
        backend="s3",
        bucket="os-archives",
        verified_at=datetime(2026, 8, 18, 10, 0, 0, tzinfo=UTC),
    )


def make_cr() -> dict:
    return {
        "apiVersion": f"{status_store.GROUP}/{status_store.VERSION}",
        "kind": "OpenStudioClusterManager",
        "metadata": {"name": NAME, "namespace": NAMESPACE},
        "spec": {},
        "status": {},
    }


class FakeCustomObjectsApi:
    """In-memory CustomObjectsApi stand-in with RFC 7386 merge-patch + synthetic 409s."""

    def __init__(self, obj: dict, patch_conflicts: int = 0) -> None:
        self.obj = copy.deepcopy(obj)
        self.remaining_conflicts = patch_conflicts
        self.get_calls = 0
        self.patch_calls = 0
        self.patches: list[tuple[dict, str | None]] = []

    def get_namespaced_custom_object_status(self, group, version, namespace, plural, name):
        self.get_calls += 1
        return copy.deepcopy(self.obj)

    def patch_namespaced_custom_object_status(
        self, group, version, namespace, plural, name, body, _content_type=None
    ):
        self.patch_calls += 1
        self.patches.append((copy.deepcopy(body), _content_type))
        if self.remaining_conflicts > 0:
            self.remaining_conflicts -= 1
            raise ApiException(status=409, reason="Conflict")
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


@pytest.fixture()
def sleeps(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr(status_store, "_sleep", recorded.append)
    return recorded


@pytest.fixture()
def api():
    return FakeCustomObjectsApi(make_cr())


@pytest.fixture()
def store(api):
    return StatusStore(NAMESPACE, NAME, api)


# --- Map get/set round-trips -------------------------------------------------


def test_soft_stop_round_trip_and_wire_format(store, api):
    record = make_soft_stop()
    store.set_soft_stop("a1", record)
    assert store.get_soft_stop("a1") == record
    assert store.get_soft_stops() == {"a1": record}
    assert api.obj["status"]["softStops"]["a1"] == {
        "issuedAt": "2026-08-18T08:00:00+00:00",
        "outcome": "issued",
    }


# --- softStops escalation marker + prune (#9) ----------------------------------


def test_escalation_marker_round_trip_and_wire_format(store, api):
    when = datetime(2026, 8, 18, 8, 20, 0, tzinfo=UTC)
    store.set_soft_stop("a1", make_soft_stop())
    store.mark_soft_stop_escalated("a1", when, "evicted")
    record = store.get_soft_stop("a1")
    assert record is not None
    assert record.escalated_at == when
    assert record.escalation_outcome == "evicted"
    assert record.issued_at == make_soft_stop().issued_at  # original anchor preserved
    assert record.outcome == "issued"
    assert api.obj["status"]["softStops"]["a1"] == {
        "issuedAt": "2026-08-18T08:00:00+00:00",
        "outcome": "issued",
        "escalatedAt": "2026-08-18T08:20:00+00:00",
        "escalationOutcome": "evicted",
    }


def test_pre_escalation_anchor_without_keys_parses_unchanged(api, store):
    """Anchors persisted before #9 (no escalation keys) parse with None fields."""
    api.obj["status"] = {
        "softStops": {"a1": {"issuedAt": "2026-08-18T08:00:00+00:00", "outcome": "issued"}}
    }
    record = store.get_soft_stop("a1")
    assert record == make_soft_stop()
    assert record.escalated_at is None and record.escalation_outcome is None


def test_mark_soft_stop_escalated_missing_anchor_raises(store):
    with pytest.raises(StatusStoreError, match="anchor vanished"):
        store.mark_soft_stop_escalated("ghost", datetime(2026, 8, 18, 8, 0, 0, tzinfo=UTC), "evicted")


def test_mark_soft_stop_escalated_is_idempotent(store, api):
    when = datetime(2026, 8, 18, 8, 20, 0, tzinfo=UTC)
    store.set_soft_stop("a1", make_soft_stop())
    store.mark_soft_stop_escalated("a1", when, "dry-run")
    patches_after_first = api.patch_calls
    store.mark_soft_stop_escalated("a1", when, "dry-run")
    assert api.patch_calls == patches_after_first  # identical marker writes nothing


def test_mark_soft_stop_escalated_retries_on_conflict(api, store, sleeps):
    api.remaining_conflicts = 1
    store.set_soft_stop("a1", make_soft_stop())
    when = datetime(2026, 8, 18, 8, 20, 0, tzinfo=UTC)
    store.mark_soft_stop_escalated("a1", when, "no-matching-pods")
    assert store.get_soft_stop("a1").escalation_outcome == "no-matching-pods"
    assert len(sleeps) == 1  # one jittered backoff between the two cycles


def test_clear_soft_stop_deletes_only_that_key_and_is_idempotent(store, api):
    store.set_soft_stop("a1", make_soft_stop())
    store.set_soft_stop("a2", make_soft_stop(outcome="dry-run"))
    store.clear_soft_stop("a1")
    assert set(api.obj["status"]["softStops"]) == {"a2"}
    patches_after_clear = api.patch_calls
    store.clear_soft_stop("a1")  # absent key: no write
    assert api.patch_calls == patches_after_clear
    assert set(api.obj["status"]["softStops"]) == {"a2"}


def test_clear_soft_stop_also_removes_escalation_marker(store, api):
    store.set_soft_stop("a1", make_soft_stop())
    store.mark_soft_stop_escalated("a1", datetime(2026, 8, 18, 8, 20, 0, tzinfo=UTC), "evicted")
    store.clear_soft_stop("a1")
    assert "softStops" not in api.obj["status"] or "a1" not in api.obj["status"]["softStops"]
    assert store.get_soft_stop("a1") is None


def test_requeue_round_trip_and_wire_format(store, api):
    record = make_requeue(count=2)
    store.set_requeue("dp1", record)
    assert store.get_requeue("dp1") == record
    assert store.get_requeues() == {"dp1": record}
    assert api.obj["status"]["requeues"]["dp1"] == {
        "count": 2,
        "lastRequeuedAt": "2026-08-18T09:30:00+00:00",
    }


def test_started_since_round_trip_and_wire_format(store, api):
    when = datetime(2026, 8, 18, 7, 15, 0, tzinfo=UTC)
    store.set_started_since("dp1", when)
    assert store.get_started_since("dp1") == when
    assert store.get_started_since_map() == {"dp1": when}
    assert api.obj["status"]["startedSince"]["dp1"] == "2026-08-18T07:15:00+00:00"


def test_clear_started_since_deletes_only_that_key(store, api):
    """Departure prune (#10): removes the clock entry, leaves requeues intact."""
    when = datetime(2026, 8, 18, 7, 15, 0, tzinfo=UTC)
    store.set_started_since("dp1", when)
    store.set_started_since("dp2", when)
    store.set_requeue("dp1", make_requeue(count=1))

    store.clear_started_since("dp1")

    assert store.get_started_since("dp1") is None
    assert store.get_started_since_map() == {"dp2": when}
    assert api.obj["status"]["requeues"]["dp1"]["count"] == 1


def test_clear_started_since_is_idempotent(store, api):
    patches_before = api.patch_calls
    store.clear_started_since("never-existed")
    assert api.patch_calls == patches_before  # absent key → no write at all


def test_archived_analysis_round_trip_and_wire_format(store, api):
    record = make_archived()
    store.set_archived_analysis("a1", record)
    assert store.get_archived_analysis("a1") == record
    assert store.get_archived_analyses() == {"a1": record}
    assert api.obj["status"]["archivedAnalyses"]["a1"] == {
        "backend": "s3",
        "bucket": "os-archives",
        "verifiedAt": "2026-08-18T10:00:00+00:00",
    }


def test_missing_keys_return_none(store):
    assert store.get_soft_stop("nope") is None
    assert store.get_requeue("nope") is None
    assert store.get_started_since("nope") is None
    assert store.get_archived_analysis("nope") is None


def test_set_same_value_twice_writes_once(store, api):
    record = make_soft_stop()
    store.set_soft_stop("a1", record)
    store.set_soft_stop("a1", record)
    assert api.patch_calls == 1


# --- Scalar get/set round-trips -------------------------------------------------


def test_scalar_round_trip_normalizes_to_utc(store, api):
    when = datetime(2026, 8, 18, 8, 0, 0, tzinfo=timezone(timedelta(hours=-6)))
    store.set_last_recycle_at(when)
    assert api.obj["status"]["lastRecycleAt"] == "2026-08-18T14:00:00+00:00"
    assert store.get_last_recycle_at() == datetime(2026, 8, 18, 14, 0, 0, tzinfo=UTC)


def test_scalar_naive_datetime_assumed_utc(store, api):
    # Naive on purpose: the boundary convention assumes UTC for naive input.
    store.set_last_web_background_restart_at(datetime(2026, 8, 18, 12, 0, 0))  # noqa: DTZ001
    assert store.get_last_web_background_restart_at() == datetime(
        2026, 8, 18, 12, 0, 0, tzinfo=UTC
    )


def test_scalar_clear_with_none_sends_null(store, api):
    store.set_last_recycle_at(datetime(2026, 8, 18, 8, 0, 0, tzinfo=UTC))
    store.set_last_recycle_at(None)
    assert "lastRecycleAt" not in api.obj["status"]
    assert store.get_last_recycle_at() is None
    body, _ = api.patches[-1]
    assert body == {"status": {"lastRecycleAt": None}}


# --- Conflict-safe RMW ----------------------------------------------------------


def test_409_then_success_re_reads_and_reapplies(api, sleeps):
    api.remaining_conflicts = 2
    store = StatusStore(NAMESPACE, NAME, api)
    record = make_soft_stop()

    store.set_soft_stop("a1", record)

    assert api.patch_calls == 3
    assert api.get_calls == 3  # one fresh read per RMW attempt
    assert len(sleeps) == 2
    assert all(delay > 0 for delay in sleeps)
    body, content_type = api.patches[-1]
    assert content_type == MERGE_PATCH_CONTENT_TYPE
    assert body == {"status": {"softStops": {"a1": record.to_dict()}}}
    assert store.get_soft_stop("a1") == record


def test_conflict_retries_bounded_then_raises(api, sleeps):
    api.remaining_conflicts = 99
    store = StatusStore(NAMESPACE, NAME, api)

    with pytest.raises(StatusStoreConflictError, match="409"):
        store.set_last_recycle_at(datetime(2026, 8, 18, 8, 0, 0, tzinfo=UTC))

    assert api.patch_calls == status_store.MAX_CONFLICT_RETRIES
    assert len(sleeps) == status_store.MAX_CONFLICT_RETRIES - 1


def test_non_409_api_error_propagates_without_retry(store, api, sleeps, monkeypatch):
    def boom(*args, **kwargs):
        api.patch_calls += 1
        raise ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(api, "patch_namespaced_custom_object_status", boom)

    with pytest.raises(ApiException):
        store.set_last_recycle_at(datetime(2026, 8, 18, 8, 0, 0, tzinfo=UTC))

    assert api.get_calls == 1
    assert api.patch_calls == 1
    assert sleeps == []


# --- Pruning ---------------------------------------------------------------------


def test_prune_on_completion_drops_finished_analysis_entries(store, api):
    api.obj["status"] = {
        "softStops": {"a1": make_soft_stop("stopped").to_dict(), "a2": make_soft_stop().to_dict()},
        "archivedAnalyses": {"a1": make_archived().to_dict()},
        "requeues": {"dp9": make_requeue().to_dict()},
    }

    store.prune(live_analysis_ids={"a2"})  # a1 completed → no longer live

    assert set(store.get_soft_stops()) == {"a2"}
    assert store.get_archived_analyses() == {}
    # The datapoint id-space was left alone (None).
    assert set(store.get_requeues()) == {"dp9"}


def test_prune_on_disappearance_drops_vanished_datapoint_entries(store, api):
    api.obj["status"] = {
        "requeues": {"dp1": make_requeue().to_dict(), "dp2": make_requeue(2).to_dict()},
        "startedSince": {
            "dp1": "2026-08-18T07:00:00Z",
            "dp2": "2026-08-18T07:30:00+00:00",
        },
        "softStops": {"a9": make_soft_stop().to_dict()},
    }

    store.prune(live_datapoint_ids={"dp2"})  # dp1 vanished from the server

    assert set(store.get_requeues()) == {"dp2"}
    assert set(store.get_started_since_map()) == {"dp2"}
    # The analysis id-space was left alone (None).
    assert set(store.get_soft_stops()) == {"a9"}


def test_prune_sends_explicit_nulls_because_merge_patch_keeps_absent_keys(store, api):
    api.obj["status"] = {
        "softStops": {"a1": make_soft_stop().to_dict(), "a2": make_soft_stop().to_dict()},
    }

    store.prune(live_analysis_ids={"a2"})

    body, content_type = api.patches[-1]
    assert content_type == MERGE_PATCH_CONTENT_TYPE
    # Absent maps (archivedAnalyses here) contribute no deletions at all.
    assert body == {"status": {"softStops": {"a1": None}}}


def test_prune_all_live_skips_write(store, api):
    api.obj["status"] = {"softStops": {"a1": make_soft_stop().to_dict()}}
    store.prune(live_analysis_ids={"a1"})
    assert api.patch_calls == 0


# --- Corrupt stored state / cache-only stance -------------------------------------


def test_corrupt_started_since_value_raises(store, api):
    api.obj["status"] = {"startedSince": {"dp1": "not-a-date"}}
    with pytest.raises(StatusStoreError, match="unparseable timestamp"):
        store.get_started_since_map()


def test_started_since_accepts_z_suffix_from_manual_writes(store, api):
    api.obj["status"] = {"startedSince": {"dp1": "2026-08-18T12:00:00Z"}}
    assert store.get_started_since("dp1") == datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)


def test_cache_is_invalidated_on_write_and_never_authoritative(store, api):
    assert store.get_started_since("dp1") is None
    assert store.cache == {}  # populated by the read, cache-only (D04)
    store.set_started_since("dp1", datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC))
    assert store.cache is None  # invalidated: next decision must re-read
