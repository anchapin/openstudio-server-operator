"""Unit tests for the CR .status store (issue #7): typed RMW, 409 retry, pruning.

The Kubernetes API is faked with an in-memory CustomObjectsApi stand-in that
speaks RFC 7386 merge-patch semantics (null deletes a key) and can be told to
fail patches with synthetic 409s. No extra test dependencies are used beyond
the [dev] extra (pytest; the kubernetes client ships ApiException).
"""

from datetime import UTC, datetime, timedelta, timezone
from functools import partial

import pytest
from kubernetes.client import ApiException

from _fakes import FakeCustomObjectsApi
from _fakes import make_cr as _shared_make_cr
from openstudio_operator import status_store
from openstudio_operator._k8s import MERGE_PATCH_CONTENT_TYPE
from openstudio_operator.status_store import (
    ArchivedAnalysisRecord,
    RequeueRecord,
    SoftStopRecord,
    StatusStore,
    StatusStoreConflictError,
    StatusStoreError,
)

NAMESPACE = "openstudio-server"
NAME = "oscm"

# Shared-fake binding (issue #474): status-store tests use an empty spec/status CR.
make_cr = partial(_shared_make_cr, default_spec={})


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


# --- Issue #488 — kube-api request-duration histogram --------------------------


def test_rmw_cycle_observes_kube_api_duration_histogram(store, api):
    """Issue #488 acceptance: a status-store RMW cycle observes the kube-api
    request-duration histogram on BOTH legs — the fresh GET in
    ``_read_status`` and the merge PATCH in ``_mutate`` — so a
    slow-but-successful apiserver is no longer invisible behind the #119
    409 counters."""
    from openstudio_operator import metrics

    histogram = metrics.KUBE_API_REQUEST_DURATION_SECONDS

    def count(verb: str) -> float:
        child = histogram.labels(verb=verb)
        return float(
            next(s.value for s in child._child_samples() if s.name == "_count")
        )

    get_before = count("get")
    patch_before = count("patch")

    store.set_soft_stop("a1", make_soft_stop())
    assert store.get_soft_stop("a1") == make_soft_stop()

    assert count("get") - get_before >= 1
    assert count("patch") - patch_before >= 1


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


def test_stall_window_started_at_round_trip_and_clear(store, api):
    """Issue #582 — the sustained-window BEGIN checkpoint scalar: set (ISO
    UTC at the boundary, parsed back tz-aware), then cleared with the
    explicit-null merge patch (RFC 7386 removal) exactly like the other
    status scalars."""
    when = datetime(2026, 8, 18, 9, 30, 0, tzinfo=UTC)
    store.set_stall_window_started_at(when)
    assert api.obj["status"]["stallWindowStartedAt"] == when.isoformat()
    assert store.get_stall_window_started_at() == when

    store.set_stall_window_started_at(None)
    assert "stallWindowStartedAt" not in api.obj["status"]
    assert store.get_stall_window_started_at() is None
    body, _ = api.patches[-1]
    assert body == {"status": {"stallWindowStartedAt": None}}


# --- Issue #649 — ineffective-restart breaker int scalar ------------------------


def test_ineffective_restarts_round_trip_and_default_zero(store, api):
    """Issue #649 — the breaker's consecutive-count scalar round-trips as a
    plain JSON integer; absent reads as ``0`` so pre-#649 CRs need no
    migration (the breaker starts disarmed)."""
    assert store.get_web_background_ineffective_restarts() == 0
    assert "webBackgroundIneffectiveRestarts" not in api.obj["status"]

    store.set_web_background_ineffective_restarts(3)
    assert api.obj["status"]["webBackgroundIneffectiveRestarts"] == 3
    assert store.get_web_background_ineffective_restarts() == 3
    body, _ = api.patches[-1]
    assert body == {"status": {"webBackgroundIneffectiveRestarts": 3}}

    store.set_web_background_ineffective_restarts(0)
    assert api.obj["status"]["webBackgroundIneffectiveRestarts"] == 0
    assert store.get_web_background_ineffective_restarts() == 0


def test_ineffective_restarts_same_value_writes_once(store, api):
    """Idempotent write: re-setting the stored value is a no-op PATCH skip
    (the same guard every scalar/map setter carries)."""
    store.set_web_background_ineffective_restarts(2)
    patch_calls_after_set = api.patch_calls
    store.set_web_background_ineffective_restarts(2)
    assert api.patch_calls == patch_calls_after_set


def test_ineffective_restarts_retries_on_conflict(api, store, sleeps):
    """409-safe like every other ``.status`` write (D04): the RMW cycle
    re-reads and re-applies past synthetic conflicts, so a breaker count
    update is never lost to contention."""
    api.remaining_conflicts = 2
    store.set_web_background_ineffective_restarts(5)
    assert store.get_web_background_ineffective_restarts() == 5
    assert api.conflicts_seen == 2
    assert len(sleeps) == 2


@pytest.mark.parametrize(
    "corrupt",
    ["3", 3.5, True, [3], {"count": 3}],
)
def test_ineffective_restarts_corrupt_value_raises(store, api, corrupt):
    """Strict typing: only JSON integers parse (strings, floats, bools,
    arrays, objects raise ``StatusStoreError``) — the same posture the
    datetime scalars take via ``_parse_utc``; a silently coerced count
    could mis-arm the backoff schedule."""
    api.obj["status"]["webBackgroundIneffectiveRestarts"] = corrupt
    with pytest.raises(StatusStoreError):
        store.get_web_background_ineffective_restarts()


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


# --- Issue #119 — status-store 409 retry observability surface --------------------
#
# `openstudio_operator_status_conflicts_total` increments at every observed 409,
# BEFORE the backoff sleep, so a sustained conflict burst reads as N+1 from a
# single tick. `openstudio_operator_status_conflict_retries_exhausted_total`
# increments exactly once per StatusStoreConflictError raised. Both must stay
# in sync with the existing retry semantics — these tests pin the contract so
# a refactor doesn't silently lose visibility on contention storms.


def _counter_value(counter):
    """Read a prometheus_client.Counter's current sample value.

    prometheus_client 0.26.x exposes ``Counter._value`` as a ``MutexValue``
    whose ``.get()`` returns the current float directly (no iterable wrapper).
    Earlier versions returned a list of values; this helper picks whatever
    the installed client gives us.
    """
    raw = counter._value.get()
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        samples = list(raw)
    except TypeError:
        return float(raw)
    return float(samples[0].value) if samples else 0.0


def test_status_store_conflict_counter_increments_per_attempt(api, sleeps):
    """5 consecutive 409s (retried, then exhausted) → 5 per-attempt counters
    and 1 exhausted counter, exactly."""
    from openstudio_operator import metrics as metrics_module

    before_conflicts = _counter_value(metrics_module.STATUS_CONFLICTS_TOTAL)
    before_exhausted = _counter_value(metrics_module.STATUS_CONFLICT_RETRIES_EXHAUSTED_TOTAL)

    api.remaining_conflicts = status_store.MAX_CONFLICT_RETRIES
    store = StatusStore(NAMESPACE, NAME, api)

    with pytest.raises(StatusStoreConflictError, match="409"):
        store.set_last_recycle_at(datetime(2026, 8, 18, 8, 0, 0, tzinfo=UTC))

    assert api.patch_calls == status_store.MAX_CONFLICT_RETRIES

    after_conflicts = _counter_value(metrics_module.STATUS_CONFLICTS_TOTAL)
    after_exhausted = _counter_value(metrics_module.STATUS_CONFLICT_RETRIES_EXHAUSTED_TOTAL)
    # +1 per observed 409, +1 on exhaustion — matches the retry loop exactly.
    assert after_conflicts - before_conflicts == status_store.MAX_CONFLICT_RETRIES
    assert after_exhausted - before_exhausted == 1


def test_status_store_conflict_counter_does_not_increment_on_non_409(store, api, sleeps, monkeypatch):
    """A non-409 ApiException (e.g., 404) propagates without retry and must NOT
    touch the conflict counters — otherwise a 404 storm would look like 409
    contention to a dashboard alerting on `rate(_conflicts_total[5m])`."""
    from openstudio_operator import metrics as metrics_module

    before_conflicts = _counter_value(metrics_module.STATUS_CONFLICTS_TOTAL)
    before_exhausted = _counter_value(metrics_module.STATUS_CONFLICT_RETRIES_EXHAUSTED_TOTAL)

    def boom(*args, **kwargs):
        api.patch_calls += 1
        raise ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(api, "patch_namespaced_custom_object_status", boom)

    with pytest.raises(ApiException):
        store.set_last_recycle_at(datetime(2026, 8, 18, 8, 0, 0, tzinfo=UTC))

    assert (
        _counter_value(metrics_module.STATUS_CONFLICTS_TOTAL) - before_conflicts
    ) == 0
    assert (
        _counter_value(metrics_module.STATUS_CONFLICT_RETRIES_EXHAUSTED_TOTAL) - before_exhausted
    ) == 0


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


# --- Issue #171 — defensive cap on the CR .status maps --------------------------
#
# The CRD schema accepts unbounded maps (every field is `x-kubernetes-preserve-unknown-fields`
# with no `maxProperties`), so a CR with `update` on the status subresource can
# grow any of the four maps toward etcd's 1.5 MB object-size limit; the
# operator then reads + JSON-parses + merge-patches the full map on every
# timer tick. status_store._set_map_entry enforces a per-map cap
# (STATUS_MAP_MAX_ENTRIES = 10000) by dropping the oldest entries (sorted by
# key — the operator's keys are UUIDs, so the sort order is deterministic but
# not age-aware) before adding a new entry. These tests pin the cap behavior:
# the eviction is deterministic, the oldest entry goes first, the new entry
# is present, the Warning Event + counter fire exactly once per cap hit, and
# re-writing an existing key with the same value does NOT evict (the read
# path is short-circuited before the cap fires).


def _fill_map_to_cap(api, field: str) -> None:
    """Pre-populate ``api.obj['status'][field]`` to exactly STATUS_MAP_MAX_ENTRIES.

    Keys are deterministically sortable strings (``a00000``, ``a00001``, …)
    so the eviction test can assert which key was dropped. The cap value
    (10000) is large enough that creating the dict is cheap (under 100ms
    on a modern CPU) and the eviction patch is a single in-memory merge.
    """
    cap = status_store.STATUS_MAP_MAX_ENTRIES
    api.obj["status"] = {
        field: {f"a{i:05d}": make_soft_stop().to_dict() for i in range(cap)}
    }


@pytest.fixture()
def status_event_sink():
    """Install a recorder for cap-eviction events and restore on teardown.

    The default sink is a no-op (avoids coupling status_store to kopf); the
    handlers package installs the production kopf-backed sink at operator
    startup. Tests that want to observe the Warning Event install a recorder
    via :func:`status_store.set_event_sink`, then restore the default here
    so subsequent tests see a clean module-level state.
    """
    events: list[tuple[str, str, str, str]] = []
    status_store.set_event_sink(
        lambda namespace, name, reason, message: events.append(
            (namespace, name, reason, message)
        )
    )
    yield events
    status_store.set_event_sink(None)


def test_status_map_cap_drops_oldest_entries(api, store):
    """A map at the cap shrinks by one oldest entry when a new key is added.

    Pre-fills ``softStops`` to exactly STATUS_MAP_MAX_ENTRIES, adds one new
    key, and asserts:
      * the new key is present,
      * the smallest sorted key (the deterministic "oldest") was evicted,
      * the map size is back to the cap (not below — the cap is the steady
        state, not a one-shot shrink).
    """
    _fill_map_to_cap(api, "softStops")
    cap = status_store.STATUS_MAP_MAX_ENTRIES

    store.set_soft_stop("new-key", make_soft_stop())

    stops = store.get_soft_stops()
    assert "new-key" in stops
    # "a00000" is the smallest sortable key — the deterministic "oldest".
    assert "a00000" not in stops
    # The cap is a steady-state bound, not a one-shot shrink.
    assert len(stops) == cap
    # The next-smallest original key is now the smallest — the eviction
    # removed exactly one entry, not more.
    assert min(stops.keys()) == "a00001"


def test_status_map_cap_emits_warning_event_when_hit(api, store, status_event_sink):
    """A cap hit records a Warning Event with reason ``StatusMapCapped``.

    The recorder is installed via the module-level sink hook; the event
    carries the CR namespace, name, the cap reason, and a message that
    names the map (the task spec: "a message naming which map was trimmed").
    """
    _fill_map_to_cap(api, "softStops")

    store.set_soft_stop("new-key", make_soft_stop())

    assert len(status_event_sink) == 1
    namespace, name, reason, message = status_event_sink[0]
    assert namespace == NAMESPACE
    assert name == NAME
    assert reason == status_store.STATUS_MAP_CAPPED_EVENT
    assert "softStops" in message
    assert str(status_store.STATUS_MAP_MAX_ENTRIES) in message
    assert "new-key" in message


def test_status_map_cap_increments_counter(api, store):
    """The labelled counter ``status_map_caps_total{map_name="softStops"}``
    increments by exactly 1 per cap hit — retry-stable, not per 409 attempt.

    Pre-touches the labelled series so the before/after delta is observable
    (a labelled Counter with no observations does not expose the series
    until ``.labels(...).inc()`` has been called once). The increment fires
    AFTER the successful RMW (post-eviction), so a 409 burst that eventually
    succeeds still reads as a single increment.
    """
    from openstudio_operator import metrics as metrics_module

    counter = metrics_module.STATUS_MAP_CAPS_TOTAL
    # Pre-touch the series so the before-value is observable (see the
    # get_started_since counter pattern in test_metrics_endpoint.py).
    counter.labels(
        namespace=NAMESPACE, name=NAME, map_name="softStops"
    ).inc(0)
    per_series = getattr(counter, "_metrics", {})
    soft_stops_key = (NAMESPACE, NAME, "softStops")
    snapshot = per_series.get(soft_stops_key)
    assert snapshot is not None
    before = _counter_value(snapshot)

    _fill_map_to_cap(api, "softStops")
    store.set_soft_stop("new-key", make_soft_stop())

    after = _counter_value(per_series[(NAMESPACE, NAME, "softStops")])
    assert after - before == 1


def _counter_value(counter):
    """Read a prometheus_client.Counter's current sample value.

    prometheus_client 0.26.x exposes ``Counter._value`` as a ``MutexValue``
    whose ``.get()`` returns the current float directly (no iterable wrapper).
    Earlier versions returned a list of values; this helper picks whatever
    the installed client gives us. Works for both parent (unlabelled) and
    child (labelled) Counters — labelled parents expose ``_metrics`` and
    fall back to summing all child series.
    """
    if hasattr(counter, "_metrics"):
        # Labelled parent — sum across all child series.
        total = 0.0
        for child in counter._metrics.values():
            total += _counter_value(child)
        return total
    raw = counter._value.get()
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        samples = list(raw)
    except TypeError:
        return float(raw)
    return float(samples[0].value) if samples else 0.0


def test_status_map_cap_does_not_evict_when_key_already_holds_same_value(api, store, status_event_sink):
    """Re-writing an existing key with the SAME value must not evict.

    The cap is in the "add a new entry" path, not in every write. If the
    map is at the cap and the operator is just refreshing an existing
    anchor (e.g. re-asserting a soft stop that was already recorded
    earlier), the read-path short-circuit (``current == encoded``) returns
    None BEFORE the cap fires — no eviction, no counter increment, no
    event. This pins the boundary so a future refactor doesn't accidentally
    widen the eviction trigger.
    """
    from openstudio_operator import metrics as metrics_module

    counter = metrics_module.STATUS_MAP_CAPS_TOTAL
    counter.labels(
        namespace=NAMESPACE, name=NAME, map_name="softStops"
    ).inc(0)
    per_series = getattr(counter, "_metrics", {})
    snapshot = per_series.get((NAMESPACE, NAME, "softStops"))
    before = _counter_value(snapshot)

    _fill_map_to_cap(api, "softStops")
    record = make_soft_stop()

    # The map already has 'a00000' with a record that differs by timestamp
    # (every SoftStopRecord fixture has the same issued_at, but the
    # reconstruction is identical to make_soft_stop's output). Re-write
    # an existing key with the same value — must be a no-op.
    store.set_soft_stop("a00001", record)

    # No eviction, no event, no counter increment.
    assert status_event_sink == []
    after = _counter_value(per_series[(NAMESPACE, NAME, "softStops")])
    assert after == before
    # The map is still at the cap (no entries added or removed).
    assert len(store.get_soft_stops()) == status_store.STATUS_MAP_MAX_ENTRIES


def test_status_map_cap_does_not_evict_on_clear(api, store, status_event_sink):
    """Clearing a key writes ``None`` and must not evict.

    The cap guard's ``encoded is not None`` check keeps the deletion path
    (clear_soft_stop / clear_started_since / clear_archived_analysis) out
    of the eviction path. A map at the cap stays at the cap after a clear
    — the operator's natural bound is preserved, not gamed by churn.
    """
    _fill_map_to_cap(api, "softStops")

    store.clear_soft_stop("a00042")

    assert status_event_sink == []
    assert len(store.get_soft_stops()) == status_store.STATUS_MAP_MAX_ENTRIES - 1
    assert "a00042" not in store.get_soft_stops()


# --- Issue #489 — status_map_entries lead-time gauge ----------------------------


def _gauge_map_entries(map_name: str) -> float:
    """Read ``STATUS_MAP_ENTRIES{NAMESPACE, NAME, map_name}`` current value.

    Calling ``.labels(...)`` creates the series at 0 if absent — the same
    pre-touch idiom the cap-counter tests above use, so before/after reads
    are observable without relying on a prior test having touched the series.
    """
    from openstudio_operator import metrics as metrics_module

    child = metrics_module.STATUS_MAP_ENTRIES.labels(
        namespace=NAMESPACE, name=NAME, map_name=map_name
    )
    return float(child._value.get())


def test_status_map_entries_gauge_tracks_len_for_all_four_maps(api, store):
    """Issue #489: after RMW writes into each map, the per-map gauge reads
    ``len(map)`` for all four ``map_name`` label values — the exact ``.status``
    map keys, same vocabulary as ``status_map_caps_total``.

    The stamp site is ``_read_status`` (shared by the RMW cycle's fresh GET
    and the typed getters), so the final write's read stamped all four maps
    and the getter re-reads below stamp identical values.
    """
    store.set_soft_stop("a1", make_soft_stop())
    store.set_soft_stop("a2", make_soft_stop(outcome="dry-run"))
    store.set_requeue("dp1", make_requeue(count=2))
    store.set_started_since("dp1", datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC))
    store.set_archived_analysis("a1", make_archived())

    assert len(store.get_soft_stops()) == 2
    assert len(store.get_requeues()) == 1
    assert len(store.get_started_since_map()) == 1
    assert len(store.get_archived_analyses()) == 1

    assert _gauge_map_entries("softStops") == 2.0
    assert _gauge_map_entries("requeues") == 1.0
    assert _gauge_map_entries("startedSince") == 1.0
    assert _gauge_map_entries("archivedAnalyses") == 1.0

    # Exposition shape — labels alphabetical (map_name < name < namespace),
    # so a future label rename/reorder is caught here, not on the on-call's
    # Grafana board.
    from prometheus_client import generate_latest

    exposition = generate_latest().decode()
    assert (
        f'openstudio_operator_status_map_entries{{map_name="softStops",'
        f'name="{NAME}",namespace="{NAMESPACE}"}} 2.0' in exposition
    )


def test_status_map_entries_gauge_stamped_on_plain_reads_without_writes(api, store):
    """Issue #489: read-only getters stamp the gauge — no RMW needed.

    The monotonic ``archivedAnalyses`` case matters most: on a long-lived
    cluster the operator reads the map on ticks where it writes nothing,
    and the gauge must still trend toward the cap. A map absent from the
    status stamps 0 — verified here against a pre-seeded nonzero sentinel
    so the 0.0 proves the read stamped it (not merely that ``.labels()``
    creates the series at 0).
    """
    from openstudio_operator import metrics as metrics_module

    api.obj["status"] = {
        "archivedAnalyses": {f"a{i}": make_archived().to_dict() for i in range(7)},
        "softStops": {"x1": make_soft_stop().to_dict()},
    }
    # Pre-seed the absent-map series at a nonzero sentinel.
    metrics_module.STATUS_MAP_ENTRIES.labels(
        namespace=NAMESPACE, name=NAME, map_name="requeues"
    ).set(42.0)

    assert len(store.get_archived_analyses()) == 7

    assert _gauge_map_entries("archivedAnalyses") == 7.0
    assert _gauge_map_entries("softStops") == 1.0
    assert _gauge_map_entries("requeues") == 0.0  # stamped 0 by the read
    assert _gauge_map_entries("startedSince") == 0.0
    assert api.patch_calls == 0  # pure reads — the gauge never forces a write


def test_status_map_entries_gauge_tracks_len_up_to_and_through_the_cap(
    api, store, monkeypatch, status_event_sink
):
    """Issue #489: with a small cap, the gauge reports ``len`` pre-cap and
    holds at the cap once evictions begin — the lead-time window the gauge
    exists to expose (the counter + Warning Event fire only at the eviction).
    """
    monkeypatch.setattr(status_store, "STATUS_MAP_MAX_ENTRIES", 5)
    cap = status_store.STATUS_MAP_MAX_ENTRIES
    api.obj["status"] = {
        "archivedAnalyses": {f"k{i}": make_archived().to_dict() for i in range(cap - 1)}
    }

    # Pre-cap: one below the cap — the alert band (> 0.8 * cap) territory.
    assert len(store.get_archived_analyses()) == cap - 1
    assert _gauge_map_entries("archivedAnalyses") == float(cap - 1)
    assert status_event_sink == []

    # Reaching the cap: adding the 5th entry evicts nothing (len < cap on add).
    # The stamp is READ-time — the RMW's fresh GET observed the pre-write
    # map — so the next read (every real tick reads before deciding) stamps
    # the post-write length.
    store.set_archived_analysis("new-1", make_archived())
    assert len(store.get_archived_analyses()) == cap
    assert _gauge_map_entries("archivedAnalyses") == float(cap)
    assert status_event_sink == []

    # Past the cap: the post-hoc signals fire and the map holds AT the cap —
    # the gauge reads the steady-state bound, the trend before it was the
    # lead time.
    store.set_archived_analysis("new-2", make_archived())
    assert len(store.get_archived_analyses()) == cap
    assert _gauge_map_entries("archivedAnalyses") == float(cap)
    assert status_event_sink  # fired only now — after the gauge already trended


# --- deferredEvents (#402) -----------------------------------------------------


def test_deferred_events_append_get_clear_round_trip(store, api):
    """``status.deferredEvents`` round-trips through the typed RMW (#402).

    The array is the crash-surviving mirror of the in-process
    ``QueuedKopfEventSink`` queue: append writes the full replacement
    list (JSON merge patch replaces arrays wholesale), get returns the
    plain dicts, clear removes the key entirely (explicit ``None``).
    """
    entry = {
        "namespace": NAMESPACE,
        "name": NAME,
        "reason": "StatusMapCapped",
        "message": "status.softStops size capped at 10000 (issue #171)",
    }
    store.append_deferred_event(entry)
    assert api.obj["status"]["deferredEvents"] == [entry]
    assert store.get_deferred_events() == [entry]

    second = dict(entry, reason="RedisUrlEmpty", message="spec.redisUrl is empty (issue #116).")
    store.append_deferred_event(second)
    assert store.get_deferred_events() == [entry, second]
    assert api.obj["status"]["deferredEvents"] == [entry, second]

    store.clear_deferred_events()
    assert store.get_deferred_events() == []
    assert "deferredEvents" not in api.obj["status"]


def test_append_deferred_event_is_409_safe_rmw(api, sleeps):
    """The append re-reads and recomputes its patch on 409 (#402, D04).

    Two synthetic conflicts force the RMW to restart twice; the final
    patch must carry the replacement list derived from the FRESH read
    (one entry — not three stacked retries), and the jittered backoff
    must have slept between attempts.
    """
    api.remaining_conflicts = 2
    store = StatusStore(NAMESPACE, NAME, api)

    store.append_deferred_event(
        {"namespace": NAMESPACE, "name": NAME, "reason": "R", "message": "m"}
    )

    assert api.obj["status"]["deferredEvents"] == [
        {"namespace": NAMESPACE, "name": NAME, "reason": "R", "message": "m"}
    ]
    assert len(api.patches) == 3  # 2 conflicts + 1 success, each with a full patch
    for body, content_type in api.patches:
        assert content_type == MERGE_PATCH_CONTENT_TYPE
        assert body == {
            "status": {
                "deferredEvents": [
                    {"namespace": NAMESPACE, "name": NAME, "reason": "R", "message": "m"}
                ]
            }
        }
    assert len(sleeps) == 2


def test_append_deferred_event_respects_max_entries_backstop(store, api):
    """The ``max_entries`` backstop skips the append at the cap (#402).

    The persisted array is the twin of the sink's in-memory queue and
    must never outgrow that queue's own bound: at the cap the append is
    a no-op (no patch written). The sink owns drop accounting — this
    path writes nothing, counts nothing, emits nothing.
    """
    filler = {"namespace": NAMESPACE, "name": NAME, "reason": "R", "message": "m"}
    api.obj["status"]["deferredEvents"] = [dict(filler) for _ in range(3)]

    store.append_deferred_event(dict(filler), max_entries=3)

    assert api.patch_calls == 0
    assert len(store.get_deferred_events()) == 3


def test_clear_deferred_events_absent_writes_nothing(store, api):
    """Clearing an absent array is a no-op — no patch, no error (#402)."""
    store.clear_deferred_events()
    assert api.patch_calls == 0


def test_get_deferred_events_rejects_corrupt_shapes(store, api):
    """Corrupt stored values raise ``StatusStoreError`` (#402).

    A non-array field or a non-object item is corruption (only the
    operator writes this field, through the typed accessors) — surface
    it loudly rather than coercing; the sink catches, logs, and retries
    the drain on the next tick.
    """
    api.obj["status"]["deferredEvents"] = "nope"
    with pytest.raises(StatusStoreError, match="expected array"):
        store.get_deferred_events()

    api.obj["status"]["deferredEvents"] = ["nope"]
    with pytest.raises(StatusStoreError, match=r"expected object"):
        store.get_deferred_events()
