"""Regression tests for the stop_analysis wiring (issue #710 / PR #757).

Tests cover:
1. stop_analysis fires exactly once across ticks (one-shot idempotency).
2. dryRun suppresses the REST call and short-circuits emit.
3. The stop_record anchor survives operator restarts (D04).
4. Flipping dryRun off does not reissue stop_analysis.
5. The stoppedAnalyses map respects STATUS_MAP_MAX_ENTRIES cap.
6. The audit doc row R2 is GATED with correct line references.
"""

import copy
import re
import types
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import responses
from prometheus_client import REGISTRY

from _fakes import FakeCustomObjectsApi, calls_to, make_emit
from _fakes import make_cr as _shared_make_cr
from openstudio_operator.config import OperatorConfig
from openstudio_operator.events import EventEmitter
from openstudio_operator.handlers.analysis_sla import run_sla_tick
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.status_store import STATUS_MAP_MAX_ENTRIES, StatusStore, StopRecord

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
    "redisUrl": "redis://:pw@queue.test:6379",
    "analysisPolicy": {"maxDurationMinutes": 180, "gracefulStopTimeoutMinutes": GRACE_MINUTES},
}

# Shared-fake binding: this module's make_cr default spec.
make_cr = partial(_shared_make_cr, default_spec=SPEC)


class FakeRedisClient:
    """Minimal stand-in for ReadOnlyRedisClient exposing the D2 surface."""

    def __init__(self, workers: dict[str, list[str]] | None = None) -> None:
        self._workers = dict(workers if workers is not None else {})
        self.workers_for_analysis_calls: list[str] = []

    def workers_for_analysis(self, analysis_id: str) -> list[str]:
        self.workers_for_analysis_calls.append(analysis_id)
        return [wid for wid, aids in self._workers.items() if analysis_id in aids]

    def pod_name_for_worker(self, worker_id: str) -> str | None:
        from openstudio_operator.redis_client import ReadOnlyRedisClient

        return ReadOnlyRedisClient.pod_name_for_worker(self, worker_id)


def register_analyses_index(*analysis_ids: str) -> None:
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": aid, "created_at": TEN_DAYS_AGO} for aid in analysis_ids],
    )


def register_analysis_status(analysis_id: str, status: str = "started") -> None:
    responses.get(
        f"{BASE}/analyses/{analysis_id}/status.json",
        json={"analysis": {"_id": analysis_id, "id": analysis_id, "status": status}},
    )


def register_started_analysis_via_status(analysis_id: str, *, status: str = "started") -> None:
    register_analyses_index(analysis_id)
    register_analysis_status(analysis_id, status=status)


def register_soft_stop(analysis_id: str) -> None:
    responses.get(
        f"{BASE}/analyses/{analysis_id}/soft_stop",
        status=200,
        json={"result": "accepted"},
    )


def register_stop_analysis(analysis_id: str) -> None:
    """POST /analyses/{id}/action with analysis_action=stop (issue #707)."""
    responses.post(
        f"{BASE}/analyses/{analysis_id}/action",
        json={"status": "ok"},
        status=200,
    )


def make_pod(name: str, ip: str | None = None, labels: dict | None = None):
    """Generated-client pod shape, attribute-style (V1Pod duck type)."""
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels=dict(
                labels
                if labels is not None
                else {"app.kubernetes.io/name": "openstudio-server", "component": "worker"}
            ),
        ),
        status=SimpleNamespace(pod_ip=ip),
    )


def _stop_stops_total() -> float:
    """Sum across all label series of ``stop_stops_total``."""
    counter = REGISTRY.get_sample_value
    total = 0.0
    for outcome in ("issued", "dry-run", "completed", "timeout"):
        val = counter(
            "openstudio_operator_stops_total",
            {"outcome": outcome},
        )
        total += val or 0.0
    return total


def tick(
    api,
    spec=None,
    client=None,
    *,
    pod_api=None,
    redis_client=None,
    now=NOW,
):
    store = StatusStore(NAMESPACE, NAME, api)
    effective_spec = spec if spec is not None else SPEC
    config = OperatorConfig.from_spec(effective_spec)
    events, emit = make_emit()
    result = run_sla_tick(
        client if client is not None else OpenStudioClient(BASE),
        store,
        config,
        now=now,
        emit=emit,
        namespace=NAMESPACE,
        pod_api=pod_api,
        redis_client=redis_client if redis_client is not None else FakeRedisClient(),
    )
    return result, events, store


# --- Test 1: stop_analysis fires exactly once across ticks -----------------------------


@responses.activate
def test_stop_analysis_fires_exactly_once_across_ticks():
    """Three-tick sequence: soft_stop fires on tick 2, stop_analysis on tick 3.

    Tick 1: first sight of a1, no anchor → phase A anchor "watching".
    Tick 2: 4h later, anchor is past maxDuration → soft_stop fires, anchor
    upgraded to "issued". Grace still running (4h < 15m grace → NO stop_analysis).
    Tick 3: 8h later, grace expired → stop_analysis fires (no StopRecord yet),
    StopRecord written with outcome="issued". Escalation follows on same tick.
    Assert: stop_analysis POST called exactly ONCE.
    """
    api = FakeCustomObjectsApi(make_cr())
    register_started_analysis_via_status("a1")
    register_started_analysis_via_status("a1")  # tick 2
    register_started_analysis_via_status("a1")  # tick 3
    register_soft_stop("a1")  # fires on tick 2 (runtime > maxDuration)
    register_soft_stop("a1")
    register_stop_analysis("a1")  # fires on tick 3 (grace expired)

    # Tick 1: first sight, anchor written as "watching".
    result1, _, _ = tick(api, now=NOW)
    assert result1.soft_stopped == []
    assert result1.stopped == []

    # Tick 2: 4h later, runtime > maxDuration → soft_stop fires.
    # Grace still running (4h >> 15m grace), so stop_analysis NOT called yet.
    result2, _, _ = tick(api, now=NOW + timedelta(hours=4))
    assert result2.soft_stopped == ["a1"]
    assert result2.stopped == []  # grace not expired yet
    assert calls_to("/action") == 0

    # Tick 3: 8h later, grace expired → stop_analysis fires, then escalation.
    result3, _, _ = tick(api, now=NOW + timedelta(hours=8))
    assert result3.stopped == ["a1"]  # stop_analysis was issued this tick
    assert calls_to("/action") == 1  # exactly one POST across all ticks

    # Verify StopRecord was written.
    store = StatusStore(NAMESPACE, NAME, api)
    rec = store.get_stop_record("a1")
    assert rec is not None
    assert rec.outcome == "issued"


# --- Test 2: dryRun suppresses stop_analysis REST call and short-circuits emit ----------


@responses.activate
def test_dry_run_suppresses_stop_analysis_rest_call_and_marks_event():
    """spec.dryRun=True: REST call suppressed, emit short-circuits, anchor written.

    Pre-condition: anchor with outcome="issued" and issuedAt past grace so
    stop_analysis would fire on this tick. With dryRun=True the REST call
    is suppressed and the EventEmitter.emit short-circuits (post-#164), but
    the StopRecord is still written with outcome="dry-run".
    """
    spec = {**SPEC, "dryRun": True}
    api = FakeCustomObjectsApi(
        make_cr(
            spec,
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                    }
                }
            },
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1")
    # No stop_analysis mock: the call must be suppressed

    metric_before = _stop_stops_total()

    # Use production EventEmitter with dry_run=True to test the short-circuit.
    cr_body = make_cr(spec)
    proxy_body = types.MappingProxyType(cr_body)
    emitter = EventEmitter(body=proxy_body, dry_run=True)
    config = OperatorConfig.from_spec(spec)
    store = StatusStore(NAMESPACE, NAME, api)

    _ = run_sla_tick(
        OpenStudioClient(BASE),
        store,
        config,
        now=NOW,
        emit=emitter,
        namespace=NAMESPACE,
        pod_api=None,
        redis_client=FakeRedisClient(),
    )

    # stop_analysis would have fired (grace expired), but REST call suppressed.
    assert calls_to("/action") == 0

    # EventEmitter.emit short-circuits: suppressed_count incremented, no kopf.event.
    assert emitter.suppressed_count == 2

    # StopRecord written with outcome="dry-run".
    rec = store.get_stop_record("a1")
    assert rec is not None
    assert rec.outcome == "dry-run"

    # Metric incremented for "dry-run" outcome.
    assert _stop_stops_total() - metric_before == 1


# --- Test 3: stop_record survives operator restart ----------------------------------


@responses.activate
def test_stop_anchor_survives_operator_restart():
    """Tick 1 in process A: soft_stop fires on tick 2, stop_analysis on tick 3.
    Process B (fresh StatusStore from same persisted CR): anchor re-read,
    NO new POST. Assert: exactly ONE POST across both processes.
    """
    api = FakeCustomObjectsApi(make_cr())
    register_started_analysis_via_status("a1")
    register_started_analysis_via_status("a1")  # tick 2 in process A
    register_started_analysis_via_status("a1")  # tick 3 in process A (grace expired)
    register_soft_stop("a1")
    register_soft_stop("a1")
    register_stop_analysis("a1")

    # Process A - Tick 1: first sight, anchor written as "watching".
    result1, _, _ = tick(api, now=NOW)
    assert result1.soft_stopped == []
    assert result1.stopped == []

    # Process A - Tick 2: soft_stop fires (4h past maxDuration).
    result2, _, _ = tick(api, now=NOW + timedelta(hours=4))
    assert result2.soft_stopped == ["a1"]
    assert result2.stopped == []  # grace not yet expired

    # Process A - Tick 3: grace expired → stop_analysis fires, StopRecord written.
    result3, _, _ = tick(api, now=NOW + timedelta(hours=8))
    assert result3.stopped == ["a1"]
    assert calls_to("/action") == 1

    # Simulate process restart: fresh StatusStore reads the same persisted CR body.
    # FakeCustomObjectsApi already holds the patched state from process A.
    api_restart = FakeCustomObjectsApi(copy.deepcopy(api.obj))
    register_started_analysis_via_status("a1")
    register_soft_stop("a1")
    # stop_analysis WOULD fire but StopRecord already exists

    result4, _, _ = tick(api_restart, now=NOW + timedelta(hours=12))
    assert result4.stopped == []  # StopRecord exists, no new POST
    assert calls_to("/action") == 1  # still exactly 1 POST total


# --- Test 4: flipping dryRun off does not reissue stop --------------------------------


@responses.activate
def test_flipping_dry_run_off_does_not_reissue_stop():
    """Tick 1: dryRun=True → stop_analysis suppressed, StopRecord outcome="dry-run".
    Tick 2: dryRun=False, StopRecord already exists → must NOT POST.
    Assert: ZERO stop_analysis calls across both ticks. Anchor stays "dry-run".
    """
    # Tick 1: dryRun=True, grace expired → stop_analysis would fire but suppressed.
    spec_dry = {**SPEC, "dryRun": True}
    api = FakeCustomObjectsApi(
        make_cr(
            spec_dry,
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                    }
                }
            },
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1")
    # No stop_analysis mock: suppressed

    cr_body = make_cr(spec_dry)
    proxy_body = types.MappingProxyType(cr_body)
    emitter = EventEmitter(body=proxy_body, dry_run=True)
    config_dry = OperatorConfig.from_spec(spec_dry)
    store = StatusStore(NAMESPACE, NAME, api)

    result1 = run_sla_tick(
        OpenStudioClient(BASE),
        store,
        config_dry,
        now=NOW,
        emit=emitter,
        namespace=NAMESPACE,
        pod_api=None,
        redis_client=FakeRedisClient(),
    )
    assert result1.stopped == ["a1"]  # handler processing still runs; only REST call suppressed
    assert calls_to("/action") == 0
    rec = store.get_stop_record("a1")
    assert rec is not None
    assert rec.outcome == "dry-run"

    # Tick 2: dryRun=False, but StopRecord already exists → no POST.
    spec_live = {**SPEC, "dryRun": False}
    api2 = FakeCustomObjectsApi(copy.deepcopy(api.obj))  # includes StopRecord
    register_analyses_index("a1")
    register_analysis_status("a1")
    # stop_analysis WOULD fire but StopRecord already exists
    store2 = StatusStore(NAMESPACE, NAME, api2)

    result2, _, _ = tick(api2, spec=spec_live, now=NOW + timedelta(hours=4))
    assert result2.stopped == []  # anchor exists, no new POST
    assert calls_to("/action") == 0  # zero across both ticks

    # Anchor outcome stays "dry-run" — not upgraded by flipping dryRun off.
    rec2 = store2.get_stop_record("a1")
    assert rec2 is not None
    assert rec2.outcome == "dry-run"


# --- Test 5: status.stoppedAnalyses map cap eviction ---------------------------------


@responses.activate
def test_status_stopped_analyses_map_cap_eviction():
    """stoppedAnalyses at 10000 entries + 1 new entry evicts the oldest (sorted by UUID).

    Assert: oldest entry evicted (a00000), cap counter incremented exactly once.
    """
    from openstudio_operator import metrics as metrics_module

    cap = STATUS_MAP_MAX_ENTRIES
    api = FakeCustomObjectsApi(make_cr())

    # Pre-fill stoppedAnalyses to exactly STATUS_MAP_MAX_ENTRIES with
    # deterministic keys (a00000, a00001, …) so the eviction victim
    # is predictable: the smallest sorted key (a00000) is evicted first.
    api.obj["status"] = {
        "stoppedAnalyses": {
            f"a{i:05d}": StopRecord(
                issued_at=datetime(2026, 8, 18, 8, 0, 0, tzinfo=UTC),
                outcome="issued",
            ).to_dict()
            for i in range(cap)
        }
    }

    counter = metrics_module.STATUS_MAP_CAPS_TOTAL
    # Pre-touch the labelled series so the before-value is observable.
    counter.labels(namespace=NAMESPACE, name=NAME, map_name="stoppedAnalyses").inc(0)
    per_series = getattr(counter, "_metrics", {})
    snapshot_key = (NAMESPACE, NAME, "stoppedAnalyses")
    snapshot = per_series.get(snapshot_key)
    assert snapshot is not None

    def _counter_value(counter_metric):
        if hasattr(counter_metric, "_metrics"):
            total = 0.0
            for child in counter_metric._metrics.values():
                total += _counter_value(child)
            return total
        raw = counter_metric._value.get()
        if isinstance(raw, (int, float)):
            return float(raw)
        return 0.0

    before = _counter_value(per_series[snapshot_key])

    # Insert one more entry. The oldest (smallest sorted key "a00000") is evicted.
    store = StatusStore(NAMESPACE, NAME, api)
    store.set_stop_record(
        "new-entry",
        StopRecord(
            issued_at=datetime(2026, 8, 18, 8, 0, 0, tzinfo=UTC),
            outcome="issued",
        ),
    )

    # Verify new entry present, oldest evicted, cap is steady-state.
    stopped = api.obj["status"]["stoppedAnalyses"]
    assert "new-entry" in stopped
    assert "a00000" not in stopped  # oldest evicted first
    assert min(stopped.keys()) == "a00001"  # cap is steady-state
    assert len(stopped) == cap

    after = _counter_value(per_series[snapshot_key])
    assert after - before == 1.0  # incremented exactly once


# --- Test 6: audit doc row R2 is GATED ------------------------------------------


def test_audit_doc_row_r2_is_gated():
    """Parse §1.1 row R2 and assert:
    - status column reads exactly "GATED"
    - gate line cites handlers/analysis_sla.py:43x (near :440)
    - openstudio_client.py line reference is current (>= :355)
    """
    doc_path = "docs/audit-dryrun-idempotency.md"
    with open(doc_path) as f:
        content = f.read()

    # Find the §1.1 table and row R2.
    rows = content.split("\n")
    r2_line = None
    for row in rows:
        if re.match(r"\|\s*R2\s*\|", row) and "action" in row.lower():
            r2_line = row
            break

    assert r2_line is not None, "Row R2 not found in audit doc §1.1"

    # Assert status column reads "GATED".
    cols = [c.strip() for c in r2_line.split("|")]
    status_col = cols[-1] if cols[-1] else cols[-2] if len(cols) > 1 else ""
    assert status_col == "GATED", f"Row R2 status is {status_col!r}, expected 'GATED'"

    # Gate line must cite handlers/analysis_sla.py near :440.
    gate_col = cols[3] if len(cols) > 3 else ""
    assert (
        "handlers/analysis_sla.py" in gate_col
    ), f"Gate line doesn't cite analysis_sla.py: {gate_col!r}"
    # Accept lines :438–:452 (flexibility around exact citation).
    # :440 (3 digits) is followed by a backtick in the markdown inline-code span.
    assert re.search(r":44[0-9](?:\s|$|,|`)", gate_col) or re.search(
        r":44[0-9]{1,2}(?:\s|$|,|`)", gate_col
    ), f"Gate line doesn't cite expected line range ~440: {gate_col!r}"

    # openstudio_client.py reference must be current (>= :355).
    assert (
        "openstudio_client.py" in r2_line
    ), "openstudio_client.py not cited in row R2"
    oc_match = re.search(r"openstudio_client\.py:(\d+)", r2_line)
    assert oc_match is not None, "openstudio_client.py line reference not found in row R2"
    line_num = int(oc_match.group(1))
    assert line_num >= 355, (
        f"openstudio_client.py reference is :{line_num}, expected >= :355 "
        "(current definition line)"
    )
