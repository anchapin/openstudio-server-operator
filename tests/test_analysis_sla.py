"""Unit tests for the analysis SLA monitor: soft-stop core (#8) + escalation (#9, #83).

REST is mocked with ``responses`` and the CR ``.status`` subresource with an
in-memory RFC 7386 merge-patch fake (same approach as test_status_store.py) —
no dependencies beyond the ``[dev]`` extra. The Kubernetes side of the
escalation is mocked with ``FakeAppsV1Api``/``FakeCoreV1Api`` built on the
same generated-client shapes the real APIs return (attribute-style models).
The Redis side (issue #83 D2) is mocked with ``FakeRedisClient`` that records
the worker-identity match. All assertions target ``run_sla_tick`` directly;
the kopf timer wrapper is thin wiring.

RBAC note: the pod deletes asserted here are covered by the ``pods``
``get/list/watch/delete`` verbs granted to the operator's namespaced Role in
``deploy/rbac.yaml`` (added in #3 specifically for this escalation).

Issue #83 — contract drift against the verified v3.11.0 REST + Resque layout:

* D1 (anchor re-sourcing): the SLA clock is anchored on the operator's
  first sight of the analysis in the ``started`` state via
  ``/analyses/{id}/status.json`` — NOT on ``page_data.start_time``, which
  is absent on v3.11.0 until the first job runs. The anchor is written
  to ``status.softStops[aid].issuedAt`` with ``outcome="watching"``;
  ``status.json`` is also consulted for the live status read (raw Mongoid
  docs omit ``status`` on a fresh analysis).
* D2 (escalation re-sourcing): the surgical pod eviction no longer
  matches started-datapoint ``ip_address`` (always null on v3.11.0)
  against pod IPs. It reads the Resque worker set
  (``workers_for_analysis``) and maps each matching worker id back to
  its pod via the standard ``{hostname}:{pid}:{queues}`` shape
  (``pod_name_for_worker``). The pod list is consulted only to verify
  the candidate pods exist in the namespace.
"""

import types
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest
import responses
from prometheus_client import REGISTRY, generate_latest

from _fakes import FakeAppsV1Api, FakeCustomObjectsApi, calls_to, make_emit
from _fakes import make_cr as _shared_make_cr
from openstudio_operator import metrics
from openstudio_operator._k8s import deployment_label_selector
from openstudio_operator.config import OperatorConfig
from openstudio_operator.events import EventEmitter
from openstudio_operator.handlers.analysis_sla import (
    ANALYSIS_ESCALATED_EVENT,
    ANALYSIS_SOFT_STOPPED_EVENT,
    _escalate_analysis,
    _status_is_started,
    run_sla_tick,
)
from openstudio_operator.metrics import HANDLER_TICK_FAILURES_TOTAL
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
# Anchor age 4 hours: well past the default 180m maxDuration, so the second
# tick (after the first-sight anchor) fires the soft-stop. The first-sight
# tick always sets issuedAt=NOW regardless of how long the analysis has
# actually been running on the server (issue #83 D1 — no server-side anchor).
FOUR_HOURS = NOW - timedelta(hours=4)

SPEC = {
    "serverUrl": BASE,
    "redisUrl": "redis://:pw@queue.test:6379",
    "analysisPolicy": {"maxDurationMinutes": 180, "gracefulStopTimeoutMinutes": GRACE_MINUTES},
}

WORKER_LABELS = {"app.kubernetes.io/name": "openstudio-server", "component": "worker"}

# Shared-fake binding (issue #474): this module's make_cr default spec.
make_cr = partial(_shared_make_cr, default_spec=SPEC)


def soft_stops_total() -> float:
    """Sum across all label series of ``soft_stops_total``.

    Issue #309 — the counter gained an ``outcome`` label, so the
    prometheus-registry sample lookup has to sum every labelled series
    (``{outcome=...}``) rather than reading the unlabelled ``_value``.
    Cribbed from
    ``tests/test_metrics_endpoint.py::_counter_total``'s labelled branch.
    """
    counter = REGISTRY.get_sample_value
    total = 0.0
    for outcome in ("issued", "dry-run"):
        val = counter(
            "openstudio_operator_soft_stops_total",
            {"outcome": outcome},
        )
        total += val or 0.0
    return total


def pods_evicted_total() -> float:
    """Sum across all label series of ``worker_pods_evicted_total``.

    Issue #309 — the counter gained an ``outcome`` label, so the
    prometheus-registry sample lookup has to sum every labelled series
    (``{outcome=...}``) rather than reading the unlabelled ``_value``.
    Cribbed from
    ``tests/test_metrics_endpoint.py::_counter_total``'s labelled branch.
    """
    counter = REGISTRY.get_sample_value
    total = 0.0
    for outcome in ("evicted", "evicted-partial", "no-matching-pods", "dry-run"):
        val = counter(
            "openstudio_operator_worker_pods_evicted_total",
            {"outcome": outcome},
        )
        total += val or 0.0
    return total


def register_analyses_index(*analysis_ids: str) -> None:
    """``GET /analyses.json`` returning the given ids.

    Raw Mongoid docs OMIT the ``status`` key on a fresh analysis (issue #19
    / #83 D1 live-verified) — the SLA tick now reads ``status.json`` per
    analysis to discover the real status. This fake reflects that shape.
    """
    responses.get(
        f"{BASE}/analyses.json",
        json=[{"_id": aid, "created_at": TEN_DAYS_AGO} for aid in analysis_ids],
    )


def register_analysis_status(analysis_id: str, status: str = "started") -> None:
    """``GET /analyses/{id}/status.json`` reporting a single-match ``{analysis: {...}}``."""
    responses.get(
        f"{BASE}/analyses/{analysis_id}/status.json",
        json={"analysis": {"_id": analysis_id, "id": analysis_id, "status": status}},
    )


def register_analysis_status_plural(*analysis_ids_status: tuple[str, str]) -> None:
    """``GET /analyses/{id}/status.json`` reporting the ``{analyses: [...]}`` plural wrapper."""
    responses.get(
        f"{BASE}/analyses/x/status.json",
        json={
            "analyses": [
                {"_id": aid, "id": aid, "status": s} for aid, s in analysis_ids_status
            ]
        },
    )


def register_started_analysis_via_status(
    analysis_id: str, *, status: str = "started"
) -> None:
    """Register the analyses index AND the status.json for one analysis.

    Helper for the common path: a single started analysis.
    """
    register_analyses_index(analysis_id)
    register_analysis_status(analysis_id, status=status)


def register_soft_stop(analysis_id: str) -> None:
    responses.get(f"{BASE}/analyses/{analysis_id}/soft_stop", status=200, json={"result": "accepted"})


def make_pod(name: str, ip: str | None = None, labels: dict | None = None):
    """Generated-client pod shape, attribute-style (V1Pod duck type).

    ``ip`` is no longer consulted by the escalation path (issue #83 D2);
    kept as a kwarg for test compatibility with helpers that pass IPs.
    """
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name, labels=dict(labels if labels is not None else WORKER_LABELS)
        ),
        status=SimpleNamespace(pod_ip=ip),
    )


class FakeCoreV1Api:
    """CoreV1Api stand-in: pod list + recorded deletes (issue #83 D2).

    The escalation path (issue #83 D2) consults the pod list to verify
    that each Resque-resolved candidate pod name actually exists in the
    namespace. The list is not used to find candidates (Resque does that)
    — it is used to defend against stale Resque records claiming a pod
    that has been deleted out-of-band.
    """

    def __init__(self, pods: list) -> None:
        self.pods = pods
        self.list_calls: list[dict] = []
        self.deletes: list[dict] = []

    def list_namespaced_pod(self, namespace, label_selector=None, **kwargs):
        self.list_calls.append(
            {"namespace": namespace, "label_selector": label_selector, "kwargs": kwargs}
        )
        return SimpleNamespace(items=list(self.pods))

    def delete_namespaced_pod(self, name, namespace, **kwargs):
        self.deletes.append({"name": name, "namespace": namespace, "kwargs": kwargs})
        return {}


class FakeRedisClient:
    """Minimal stand-in for ``ReadOnlyRedisClient`` exposing the D2 surface.

    Issue #83 D2: the SLA tick only needs ``workers_for_analysis`` and
    ``pod_name_for_worker``. Each fake worker is recorded as a tuple of
    ``(worker_id, analysis_ids_in_payload)`` so tests can stage both
    matching and non-matching workers in one Redis set.
    """

    def __init__(self, workers: dict[str, list[str]] | None = None) -> None:
        # workers: mapping of worker_id -> analysis_ids present in the worker's
        # payload args. Workers not in the map are treated as idle (no payload
        # match).
        self._workers = dict(workers if workers is not None else {})
        self.workers_for_analysis_calls: list[str] = []

    def workers_for_analysis(self, analysis_id: str) -> list[str]:
        self.workers_for_analysis_calls.append(analysis_id)
        return [wid for wid, aids in self._workers.items() if analysis_id in aids]

    def pod_name_for_worker(self, worker_id: str) -> str | None:
        from openstudio_operator.redis_client import ReadOnlyRedisClient

        return ReadOnlyRedisClient.pod_name_for_worker(self, worker_id)


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
        redis_client=redis_client if redis_client is not None else FakeRedisClient(),
    )
    return result, events


# --- First-sight anchor (D1) --------------------------------------------------


@responses.activate
def test_first_sight_writes_watching_anchor_without_soft_stop():
    """First tick of a started analysis: anchor is written (no soft-stop yet)."""
    api = FakeCustomObjectsApi(make_cr())
    register_started_analysis_via_status("a1")
    metric_before = soft_stops_total()

    result, events = tick(api)

    assert result.soft_stopped == []
    assert result.escalated == []
    assert events == []  # no soft-stop on first sight (runtime is 0)
    assert calls_to("/soft_stop") == 0
    assert soft_stops_total() - metric_before == 0
    # Anchor is present with the operator's first-sight timestamp.
    anchor = api.obj["status"]["softStops"]["a1"]
    assert anchor["issuedAt"] == NOW.isoformat()
    assert anchor["outcome"] == "watching"


# --- Issue #179 — per-CR datapoint-budget Histogram ---------------------------


def _histogram_count(histogram, **labels) -> float:
    """Sum of all sample counts in a Histogram (issue #179), label-scoped.

    Issue #472 makes ``ANALYSIS_DATAPOINT_COUNT`` a labelled Histogram
    (``view``); pass label kwargs to scope the sum to one series — with
    no kwargs the sum spans every series of the family (the pre-#472
    unlabelled shape). Summing the ``_count`` samples surfaces the total
    number of observations, which is what the SLA tick produces per
    .observe() call.
    """
    total = 0.0
    for fam in REGISTRY.collect():
        if fam.name != histogram._name:
            continue
        for sample in fam.samples:
            if not sample.name.endswith("_count"):
                continue
            if labels and any(sample.labels.get(k) != v for k, v in labels.items()):
                continue
            total += sample.value
    return total


@responses.activate
def test_analysis_datapoint_count_observed_on_sla_poll():
    """Issue #179 — the SLA tick records the per-tick analysis count via
    ``ANALYSIS_DATAPOINT_COUNT.labels(view="analyses_per_tick")
    .observe(len(analyses))`` after the initial ``/analyses.json`` poll.
    Each tick is one observation; the value is the count of analyses
    returned by the index endpoint. The observation is recorded even when
    ``autoSoftStop`` is false (the histogram is independent of the
    soft-stop decision — its purpose is to surface the per-tick
    analysis-size distribution, not the soft-stop rate)."""
    api = FakeCustomObjectsApi(make_cr())
    register_analyses_index("a1", "a2", "a3")
    for aid in ("a1", "a2", "a3"):
        register_analysis_status(aid, status="na")  # not started → no anchor
    before = _histogram_count(metrics.ANALYSIS_DATAPOINT_COUNT, view="analyses_per_tick")

    tick(api)

    after = _histogram_count(metrics.ANALYSIS_DATAPOINT_COUNT, view="analyses_per_tick")
    assert after - before == 1.0  # exactly one observation per tick


@responses.activate
def test_analysis_datapoint_count_view_label_is_analyses_per_tick():
    """Issue #472 — the SLA observe site must record on the
    ``view="analyses_per_tick"`` series (``len(analyses)`` — a count of
    ANALYSES) and never on the watchdog's
    ``view="started_datapoints_per_tick"`` series: the two populations
    have different units and magnitudes, and the pre-#472 unlabelled
    merge made the family's percentiles meaningless. Pins the exact
    exposition label value so a refactor that swaps the two sites' label
    values (or drops the label) is caught at CI."""
    api = FakeCustomObjectsApi(make_cr())
    register_analyses_index("a1", "a2")
    for aid in ("a1", "a2"):
        register_analysis_status(aid, status="na")  # not started → no anchor writes
    sla_before = _histogram_count(
        metrics.ANALYSIS_DATAPOINT_COUNT, view="analyses_per_tick"
    )
    watchdog_before = _histogram_count(
        metrics.ANALYSIS_DATAPOINT_COUNT, view="started_datapoints_per_tick"
    )

    tick(api)

    sla_after = _histogram_count(
        metrics.ANALYSIS_DATAPOINT_COUNT, view="analyses_per_tick"
    )
    watchdog_after = _histogram_count(
        metrics.ANALYSIS_DATAPOINT_COUNT, view="started_datapoints_per_tick"
    )
    assert sla_after - sla_before == 1.0
    # The SLA site never touches the watchdog's series — the two units
    # must stay separable on the dashboard.
    assert watchdog_after == watchdog_before
    # And the exposition form carries the literal label value.
    exposition = generate_latest().decode()
    assert (
        "openstudio_operator_analysis_datapoint_count_count"
        '{view="analyses_per_tick"}' in exposition
    )


@responses.activate
def test_analysis_datapoint_count_not_observed_when_auto_soft_stop_disabled():
    """Issue #179 — the histogram is wired to the post-initial-poll site
    in the SLA tick. ``autoSoftStop: false`` short-circuits the tick
    BEFORE the initial poll, so the histogram is NOT observed in that
    branch (no analysis list means no count to record). The
    "passive this tick" contract is preserved — no observations, no
    Events, no soft-stop decisions."""
    spec = {**SPEC, "analysisPolicy": {"maxDurationMinutes": 180, "autoSoftStop": False}}
    api = FakeCustomObjectsApi(make_cr(spec))
    register_analyses_index("a1")
    before = _histogram_count(metrics.ANALYSIS_DATAPOINT_COUNT, view="analyses_per_tick")

    result, events = tick(api, spec=spec)

    assert result.soft_stopped == [] and result.escalated == []
    assert events == []
    assert calls_to("/analyses.json") == 0  # no initial poll — disabled is a full stop
    after = _histogram_count(metrics.ANALYSIS_DATAPOINT_COUNT, view="analyses_per_tick")
    assert after == before


@responses.activate
def test_soft_stop_fires_on_subsequent_tick_when_anchor_is_old_enough():
    """Second tick: anchor age > maxDuration → soft-stop upgrade.

    Pre-#83 semantics: a first-sight anchor with `issuedAt` 4h old and
    `maxDurationMinutes=180` is well past the limit on the second tick.
    The soft-stop fires, the anchor's `outcome` upgrades to `issued`
    (or `dry-run`), and the soft_stop REST call goes out (or is
    suppressed in dryRun).
    """
    api = FakeCustomObjectsApi(
        make_cr(status={"softStops": {"a1": {"issuedAt": FOUR_HOURS.isoformat(),
                                            "outcome": "watching"}}})
    )
    register_analyses_index("a1")
    register_analysis_status("a1")
    register_soft_stop("a1")
    metric_before = soft_stops_total()

    result, events = tick(api)

    assert result.soft_stopped == ["a1"]
    assert result.escalated == []
    assert calls_to("/soft_stop") == 1
    assert len(events) == 1
    event_type, reason, message = events[0]
    assert event_type == "Warning"
    assert reason == ANALYSIS_SOFT_STOPPED_EVENT
    assert "a1" in message and "180" in message and "soft stop issued" in message
    assert "operator-first-sight clock" in message  # new anchor-source marker
    assert soft_stops_total() - metric_before == 1
    # The anchor is upgraded in place: issuedAt preserved, outcome → issued.
    anchor = api.obj["status"]["softStops"]["a1"]
    assert anchor["issuedAt"] == FOUR_HOURS.isoformat()  # original first-sight time
    assert anchor["outcome"] == "issued"


@responses.activate
def test_soft_stop_fires_exactly_once_across_ticks():
    """The anchor's `outcome="issued"` makes the soft-stop one-shot.

    Two-tick sequence: tick 1 (first sight, anchor=`watching`),
    tick 2 (4h later, runtime > maxDuration → soft-stop upgrade).
    A third tick on the same anchor verifies the soft-stop REST call
    is NOT re-issued (the soft-stop is one-shot; the escalation
    marker ``escalatedAt`` is what makes the escalation one-shot, tested
    separately).
    """
    api = FakeCustomObjectsApi(make_cr())
    register_started_analysis_via_status("a1")
    register_started_analysis_via_status("a1")  # second tick's poll
    register_started_analysis_via_status("a1")  # third tick's poll
    register_soft_stop("a1")

    # Tick 1: first sight, no soft-stop (anchor written as `watching`).
    result1, _ = tick(api, now=NOW)
    assert result1.soft_stopped == []
    assert calls_to("/soft_stop") == 0

    # Tick 2: 4h later, anchor is well over the 180m maxDuration → soft-stop.
    result2, events2 = tick(api, now=NOW + timedelta(hours=4))
    assert result2.soft_stopped == ["a1"]
    assert calls_to("/soft_stop") == 1
    assert len(events2) == 1

    # Tick 3: outcome="issued" → soft-stop not re-fired. (Escalation MAY
    # fire here because runtime > grace — that's covered by the
    # escalation tests; this test only asserts the soft-stop is one-shot.)
    result3, _ = tick(api, now=NOW + timedelta(hours=8))
    assert result3.soft_stopped == []
    assert calls_to("/soft_stop") == 1


@responses.activate
def test_anchor_survives_operator_restart():
    """Fresh client + store (new process state), same persisted CR → no double-fire.

    Pre-#83 invariant: the anchor is the durable source of truth (D04) and
    a restarted operator honors the ORIGINAL clock, never its own startup
    time. The pre-#83 test used the page_data.start_time-anchored anchor;
    the post-#83 D1 design uses the operator-first-sight anchor instead.
    The new operator's tick sees the persisted anchor and behaves
    accordingly: a pre-existing ``watching`` anchor past maxDuration
    fires the soft-stop; the soft-stop then anchors the grace phase
    (same anchor, no double-write).
    """
    api = FakeCustomObjectsApi(
        make_cr(status={"softStops": {"a1": {"issuedAt": FOUR_HOURS.isoformat(),
                                            "outcome": "watching"}}})
    )
    register_analyses_index("a1")
    register_analysis_status("a1")
    register_soft_stop("a1")
    register_soft_stop("a1")
    register_analyses_index("a1")
    register_analysis_status("a1")
    register_soft_stop("a1")
    # Two operators in sequence: first fires the soft-stop; second
    # observes outcome="issued" and does NOT re-fire the soft_stop.
    result1, _ = tick(api, client=OpenStudioClient(BASE))
    result2, _ = tick(api, client=OpenStudioClient(BASE))

    assert result1.soft_stopped == ["a1"]
    assert result2.soft_stopped == []
    assert calls_to("/soft_stop") == 1  # soft_stop REST call is one-shot
    # The soft-stop tick wrote the anchor upgrade (1 patch); the second
    # tick does not re-write the soft-stop. Escalation MAY also write —
    # this test only checks the soft-stop one-shot invariant; the
    # escalation marker ``escalatedAt`` (D04 idempotency) is covered by
    # ``test_double_escalation_impossible``.
    assert api.patch_calls >= 1


# --- Clock anchor: status.json is the source of truth (D1) -------------------


@responses.activate
def test_first_sight_under_max_duration_waits():
    """Tick 1 → 1s later: anchor exists, runtime < maxDuration → no soft-stop."""
    api = FakeCustomObjectsApi(make_cr())
    register_started_analysis_via_status("a1")

    tick(api, now=NOW)  # first sight
    # A second tick 1 second later: runtime = 1s, well under 180m max.
    result, events = tick(api, now=NOW + timedelta(seconds=1))

    assert result.soft_stopped == []
    assert events == []
    assert calls_to("/soft_stop") == 0
    # The anchor was NOT upgraded: outcome stays "watching".
    assert api.obj["status"]["softStops"]["a1"]["outcome"] == "watching"


@responses.activate
def test_runtime_exactly_at_max_does_not_trip():
    """A runtime EXACTLY at the limit is still under (strict >).

    Pre-#83 invariant preserved: the soft-stop REST call is NOT issued
    when the runtime is exactly ``maxDurationMinutes`` (strict greater-
    than). The escalation path is a separate concern; this test only
    asserts the soft-stop gate.
    """
    api = FakeCustomObjectsApi(make_cr())
    register_started_analysis_via_status("a1")
    register_analyses_index("a1")
    register_analysis_status("a1")

    # First sight at NOW, then tick at NOW + exactly 180m → runtime = max → no fire.
    tick(api, now=NOW)
    result, _ = tick(api, now=NOW + timedelta(minutes=180))

    assert result.soft_stopped == []
    assert calls_to("/soft_stop") == 0
    # The anchor stays at "watching" — no soft-stop upgrade happened.
    assert api.obj["status"]["softStops"]["a1"]["outcome"] == "watching"


# --- dryRun gating (D11) ------------------------------------------------------


@responses.activate
def test_dry_run_suppresses_soft_stop_and_marks_event():
    """dryRun: anchor written, soft-stop suppressed, event carries the marker."""
    api = FakeCustomObjectsApi(
        make_cr(
            {**SPEC, "dryRun": True},
            status={"softStops": {"a1": {"issuedAt": FOUR_HOURS.isoformat(),
                                         "outcome": "watching"}}},
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1")
    # No soft_stop mock: a real call would raise out of the tick.
    metric_before = soft_stops_total()

    result, events = tick(api, spec={**SPEC, "dryRun": True})

    assert result.soft_stopped == ["a1"]
    assert calls_to("/soft_stop") == 0
    assert len(events) == 1
    event_type, reason, message = events[0]
    assert event_type == "Warning"
    assert reason == ANALYSIS_SOFT_STOPPED_EVENT
    assert "dryRun" in message and "suppressed" in message
    assert soft_stops_total() - metric_before == 1
    assert api.obj["status"]["softStops"]["a1"]["outcome"] == "dry-run"


# --- Candidate filtering (status.json-driven) ---------------------------------


@responses.activate
def test_non_started_analyses_never_touched():
    """Analyses whose status.json reports a non-started state are ignored.

    The D1 trade-off: the operator must poll ``/status.json`` per analysis
    to learn the real status (raw Mongoid ``status`` is unreliable on
    v3.11.0). This test stages five non-started analyses and asserts the
    SLA tick polls each one for status, then makes no soft-stop or
    escalation decisions.
    """
    api = FakeCustomObjectsApi(make_cr())
    register_analyses_index(*[f"a-{status}" for status in
                              ("na", "init", "queued", "post-processing", "completed")])
    for status in ("na", "init", "queued", "post-processing", "completed"):
        register_analysis_status(f"a-{status}", status=status)

    result, events = tick(api)

    assert result.soft_stopped == []
    assert result.escalated == []
    assert events == []
    assert calls_to("/soft_stop") == 0
    # One status.json poll per analysis — the D1 trade-off (raw-doc status
    # is unreliable, so we MUST consult status.json for every candidate).
    assert calls_to("/status.json") == 5
    assert "softStops" not in api.obj["status"]


@responses.activate
def test_status_unknown_analyses_treated_as_no_candidate():
    """Unknown id → status.json returns the empty plural wrapper → not started.

    Contract §1: mongoid ``raise_not_found_error: false`` means
    /analyses/{id}/status.json returns 200 ``{analyses: []}`` (a
    ``where()`` query, never raises). The operator treats that as
    "no candidate" and never anchors the analysis.
    """
    api = FakeCustomObjectsApi(make_cr())
    register_analyses_index("a-ghost")
    responses.get(
        f"{BASE}/analyses/a-ghost/status.json",
        json={"analyses": []},
    )

    result, events = tick(api)

    assert result.soft_stopped == []
    assert events == []
    assert "softStops" not in api.obj["status"]


@responses.activate
def test_auto_soft_stop_disabled_makes_monitor_passive():
    """``autoSoftStop: false`` short-circuits both phase A and phase B."""
    spec = {**SPEC, "analysisPolicy": {"maxDurationMinutes": 180, "autoSoftStop": False}}
    api = FakeCustomObjectsApi(make_cr(spec))

    result, events = tick(api, spec=spec)

    assert result.soft_stopped == []
    assert result.escalated == []
    assert events == []
    # Even the index poll is skipped (D04 — auto_soft_stop off is a full stop).
    assert calls_to("/analyses.json") == 0
    assert "softStops" not in api.obj["status"]


# --- Grace wait + escalation (#9, #83 D2) --------------------------------------


@responses.activate
def test_grace_not_yet_elapsed_waits_even_after_restart():
    """An anchor 14m old (grace 15m) is still in grace — no escalation.

    Also the restart-mid-grace clock test for the waiting side: the grace
    is measured from the ORIGINAL first-sight timestamp in the CR, never
    from operator (re)start time — so a restart never shortens NOR
    lengthens it. (Pre-#83 invariant preserved post-#83 D1.)
    """
    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": WITHIN_GRACE.isoformat(),
                        "outcome": "issued",
                    }
                }
            }
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1")
    pod_api = FakeCoreV1Api([make_pod("worker-1")])
    redis_client = FakeRedisClient({"worker-1:1:requeued,simulations": ["a1"]})
    metric_before = pods_evicted_total()

    result, events = tick(
        api,
        pod_api=pod_api,
        redis_client=redis_client,
        client=OpenStudioClient(BASE),
    )

    assert result.soft_stopped == [] and result.escalated == []
    assert events == []
    assert pod_api.deletes == []
    assert api.patch_calls == 0  # nothing written — the anchor simply waits
    assert pods_evicted_total() - metric_before == 0
    # Resque wasn't consulted — the grace check short-circuited.
    assert redis_client.workers_for_analysis_calls == []


@responses.activate
def test_restart_mid_grace_escalates_from_original_anchor_time():
    """Anchor 16m old persisted BEFORE the operator restart → escalates NOW.

    The first-sight anchor is the SLA clock AND the grace origin (D04);
    a fresh operator process honors the original issuedAt so the grace
    is preserved across restarts.
    """
    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                    }
                }
            }
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1")
    pod_api = FakeCoreV1Api([make_pod("worker-1")])
    redis_client = FakeRedisClient({"worker-1:1:requeued,simulations": ["a1"]})
    metric_before = pods_evicted_total()

    result, events = tick(
        api,
        pod_api=pod_api,
        redis_client=redis_client,
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
    assert "a1" in message and "16m" in message
    assert "worker-1" in message and "worker-1:1:requeued,simulations" in message


# --- Issue #83 D2: Resque-worker-identity escalation -------------------------


@responses.activate
def test_escalation_uses_resque_worker_identity_not_ip_matching():
    """D2: escalation targets the pod named by the matching Resque worker id.

    Two workers are in the Resque registry: one processing ``a1`` (the
    candidate victim), one processing a different analysis. The escalation
    must delete ONLY the pod whose hostname matches the ``a1`` worker's
    id, regardless of any pod IP — and even if other workers' pods have
    IPs that happen to match (defensive: the IP-based matching pre-#83
    would have deleted them).
    """
    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                    }
                }
            }
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1")
    pod_api = FakeCoreV1Api(
        [
            make_pod("worker-a", "10.0.0.1"),  # victim
            make_pod("worker-b", "10.0.0.2"),  # different analysis, IP doesn't matter
            make_pod("worker-c", "10.0.0.3"),  # idle, no payload
        ]
    )
    redis_client = FakeRedisClient(
        {
            "worker-a:7:requeued,simulations": ["a1"],
            "worker-b:9:requeued,simulations": ["someone-else"],
        }
    )
    metric_before = pods_evicted_total()

    result, _ = tick(
        api,
        pod_api=pod_api,
        redis_client=redis_client,
    )

    assert result.escalated == ["a1"]
    assert [d["name"] for d in pod_api.deletes] == ["worker-a"]
    assert pods_evicted_total() - metric_before == 1
    # Resque was consulted for the right analysis id.
    assert redis_client.workers_for_analysis_calls == ["a1"]


@responses.activate
def test_escalation_skips_workers_with_no_matching_pod_in_namespace():
    """D2 defensive: a Resque record claiming a non-existent pod is skipped.

    The pod list is the namespace's source of truth — if Resque reports
    a worker whose hostname does not match any pod in the namespace
    (stale record, pod deleted out-of-band, or worker from a different
    cluster), the operator skips that candidate and only deletes the
    candidates that DO exist.
    """
    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                    }
                }
            }
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1")
    pod_api = FakeCoreV1Api([make_pod("worker-a")])  # only worker-a exists
    redis_client = FakeRedisClient(
        {
            "worker-a:7:requeued,simulations": ["a1"],
            "ghost-pod:99:requeued,simulations": ["a1"],  # no matching pod
        }
    )

    result, _ = tick(
        api,
        pod_api=pod_api,
        redis_client=redis_client,
    )

    assert result.escalated == ["a1"]
    # Only worker-a's pod is deleted; the ghost is silently dropped.
    assert [d["name"] for d in pod_api.deletes] == ["worker-a"]


@responses.activate
def test_escalation_with_no_matching_workers_records_no_match():
    """D2: no Resque workers currently processing the analysis → no-match outcome.

    Matches the pre-#83 semantics (``escalationOutcome="no-matching-pods"``)
    for the case where the escalation HAPPENED but the source set is
    empty. The Warning Event is still emitted exactly once (the
    escalation marker on the anchor prevents re-emission).
    """
    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                    }
                }
            }
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1")
    pod_api = FakeCoreV1Api([make_pod("worker-1")])
    redis_client = FakeRedisClient()  # empty Resque registry

    result, events = tick(
        api,
        pod_api=pod_api,
        redis_client=redis_client,
    )

    assert result.escalated == ["a1"]
    assert pod_api.deletes == []
    assert "no Resque workers" in events[0][2]
    assert api.obj["status"]["softStops"]["a1"]["escalationOutcome"] == "no-matching-pods"


@responses.activate
def test_default_delete_passes_no_grace_seconds():
    """forceDeleteOnEscalation false (default) → grace_period_seconds None → kubelet
    honors the pod's own terminationGracePeriodSeconds (workers: 5200s drain window)."""
    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                    }
                }
            }
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1")
    pod_api = FakeCoreV1Api([make_pod("worker-1")])
    redis_client = FakeRedisClient({"worker-1:1:requeued,simulations": ["a1"]})

    result, events = tick(
        api,
        pod_api=pod_api,
        redis_client=redis_client,
    )

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
    api = FakeCustomObjectsApi(
        make_cr(
            spec=force_spec,
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
    pod_api = FakeCoreV1Api([make_pod("worker-1")])
    redis_client = FakeRedisClient({"worker-1:1:requeued,simulations": ["a1"]})

    result, events = tick(
        api,
        spec=force_spec,
        pod_api=pod_api,
        redis_client=redis_client,
    )

    assert result.escalated == ["a1"]
    assert pod_api.deletes[0]["kwargs"]["grace_period_seconds"] == 0
    assert "grace_period_seconds=0 (immediate kill)" in events[0][2]


@responses.activate
def test_double_escalation_impossible():
    """Second tick after escalation → no-op: no new Resque polls, no deletes."""
    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                        "escalatedAt": NOW.isoformat(),
                        "escalationOutcome": "evicted",
                    }
                }
            }
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1")
    pod_api = FakeCoreV1Api([make_pod("worker-1")])
    redis_client = FakeRedisClient({"worker-1:1:requeued,simulations": ["a1"]})
    metric_before = pods_evicted_total()

    result, events = tick(
        api,
        pod_api=pod_api,
        redis_client=redis_client,
    )

    assert result.escalated == []
    assert events == []
    assert pod_api.deletes == []
    assert pods_evicted_total() - metric_before == 0
    # The escalatedAt marker short-circuits before Resque is consulted.
    assert redis_client.workers_for_analysis_calls == []


@responses.activate
def test_analysis_completed_during_grace_prunes_anchor_without_escalating():
    """An anchored analysis that left `started` → prune, no escalation."""
    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                    }
                }
            }
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1", status="completed")
    pod_api = FakeCoreV1Api([make_pod("worker-1")])
    redis_client = FakeRedisClient({"worker-1:1:requeued,simulations": ["a1"]})

    result, events = tick(
        api,
        pod_api=pod_api,
        redis_client=redis_client,
    )

    assert result.soft_stopped == [] and result.escalated == []
    assert events == []
    assert pod_api.deletes == []
    assert api.obj["status"].get("softStops", {}) == {}


@responses.activate
def test_analysis_vanished_from_api_prunes_anchor():
    """Analysis no longer in the analyses index → prune the anchor (vanished)."""
    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "softStops": {
                    "a-gone": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                    }
                }
            }
        )
    )
    register_analyses_index("a-other")  # a-gone vanished; a-other is unrelated
    register_analysis_status("a-other")  # for the a-other poll path (skipped — not anchored)
    pod_api = FakeCoreV1Api([make_pod("worker-1")])
    redis_client = FakeRedisClient()

    result, events = tick(
        api,
        pod_api=pod_api,
        redis_client=redis_client,
    )

    assert result.soft_stopped == [] and result.escalated == []
    assert events == []
    assert "a-gone" not in api.obj["status"].get("softStops", {})


@responses.activate
def test_dry_run_suppresses_pod_deletes_and_marks_event():
    """D11: dryRun suppresses the pod delete; event, metric, and marker still record."""
    spec = {**SPEC, "dryRun": True}
    api = FakeCustomObjectsApi(
        make_cr(
            spec=spec,
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
    pod_api = FakeCoreV1Api([make_pod("worker-1")])
    redis_client = FakeRedisClient({"worker-1:1:requeued,simulations": ["a1"]})
    metric_before = pods_evicted_total()

    result, events = tick(
        api,
        spec=spec,
        pod_api=pod_api,
        redis_client=redis_client,
    )

    assert result.escalated == ["a1"]  # decision made, mutation suppressed
    assert pod_api.deletes == []
    assert pods_evicted_total() - metric_before == 1  # counts the decision
    assert len(events) == 1
    event_type, reason, message = events[0]
    assert event_type == "Warning"
    assert reason == ANALYSIS_ESCALATED_EVENT
    assert "worker-1" in message and "suppressed (spec.dryRun)" in message
    anchor = api.obj["status"]["softStops"]["a1"]
    assert anchor["escalatedAt"] == NOW.isoformat()
    assert anchor["escalationOutcome"] == "dry-run"


@responses.activate
def test_escalated_anchor_skips_grace_phase_entirely():
    """A pre-escalated persisted anchor is inert: no polls, no Redis, no deletes."""
    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                        "escalatedAt": (NOW - timedelta(minutes=5)).isoformat(),
                        "escalationOutcome": "evicted",
                    }
                }
            }
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1")
    pod_api = FakeCoreV1Api([make_pod("worker-1")])
    redis_client = FakeRedisClient({"worker-1:1:requeued,simulations": ["a1"]})

    result, events = tick(
        api,
        pod_api=pod_api,
        redis_client=redis_client,
    )

    assert result.escalated == []
    assert events == []
    assert pod_api.deletes == []
    assert api.patch_calls == 0
    assert redis_client.workers_for_analysis_calls == []


@responses.activate
def test_auto_soft_stop_false_keeps_module_passive_even_with_old_anchor():
    """``autoSoftStop: false`` is a full stop: the existing anchor is also untouched."""
    spec = {**SPEC, "analysisPolicy": {**SPEC["analysisPolicy"], "autoSoftStop": False}}
    api = FakeCustomObjectsApi(
        make_cr(
            spec=spec,
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
    pod_api = FakeCoreV1Api([make_pod("worker-1")])
    redis_client = FakeRedisClient({"worker-1:1:requeued,simulations": ["a1"]})

    result, events = tick(
        api,
        spec=spec,
        pod_api=pod_api,
        redis_client=redis_client,
    )

    assert result.soft_stopped == [] and result.escalated == []
    assert events == []
    assert pod_api.deletes == []
    assert api.obj["status"]["softStops"]["a1"].get("escalatedAt") is None
    assert redis_client.workers_for_analysis_calls == []


# --- Issue #118 — escalation Event re-emission bound ---------------------------


@responses.activate
def test_escalation_with_partial_pod_delete_failure_stamps_partial_outcome_and_does_not_re_emit():
    """Pod A evicts; Pod B raises on delete_namespaced_pod. The function must
    (a) record the partial outcome, (b) stamp mark_soft_stop_escalated anyway,
    (c) NOT re-emit ``AnalysisEscalated`` on the next tick (because
    ``escalatedAt`` is now set on the anchor)."""
    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                    }
                }
            }
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1")
    pods = [make_pod("worker-1"), make_pod("worker-2")]

    class PartialPodApi(FakeCoreV1Api):
        def delete_namespaced_pod(self, name, namespace, **kwargs):
            if name == "worker-2":
                from kubernetes.client import ApiException

                raise ApiException(status=500, reason="Internal Server Error")
            return super().delete_namespaced_pod(name, namespace, **kwargs)

    pod_api = PartialPodApi(pods)
    # Two Resque workers currently processing the analysis — victims will
    # be 2 entries, exercising the per-pod try/except in the loop.
    redis_client = FakeRedisClient(
        {
            "worker-1:1:requeued,simulations": ["a1"],
            "worker-2:1:requeued,simulations": ["a1"],
        }
    )

    result, events = tick(
        api,
        pod_api=pod_api,
        redis_client=redis_client,
    )

    assert result.escalated == ["a1"]
    assert len(events) == 1
    assert events[0][:2] == ("Warning", ANALYSIS_ESCALATED_EVENT)
    # outcome recorded as partial
    anchor = api.obj["status"]["softStops"]["a1"]
    assert anchor["escalationOutcome"] == "evicted-partial"
    assert anchor["escalatedAt"] is not None
    # pod_api saw exactly one real delete call before the failure.
    assert [d["name"] for d in pod_api.deletes] == ["worker-1"]

    # Second tick — must be a no-op (anchor is pre-escalated). Resque and
    # pod-side mocks must be re-supplied identically.
    api2 = FakeCustomObjectsApi(
        make_cr(
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                        "escalatedAt": anchor["escalatedAt"],
                        "escalationOutcome": "evicted-partial",
                    }
                }
            }
        )
    )
    register_analyses_index("a1")  # register on the new api
    register_analysis_status("a1")
    redis_client2 = FakeRedisClient(
        {
            "worker-1:1:requeued,simulations": ["a1"],
            "worker-2:1:requeued,simulations": ["a1"],
        }
    )
    result2, events2 = tick(
        api2,
        pod_api=FakeCoreV1Api(pods),
        redis_client=redis_client2,
    )
    assert result2.escalated == []
    assert events2 == []


@responses.activate
def test_escalation_with_all_pod_deletes_failing_re_raises_for_wrapper():
    """Both pod deletes raise: every pod failed → ``escalationOutcome`` is
    ``no-matching-pods`` (no pods successfully evicted) AND the last
    failure is re-raised. The re-raise is what the production
    ``analysis_sla_monitor`` wrapper's ``except ApiException`` branch
    catches and feeds into ``HANDLER_TICK_FAILURES_TOTAL`` (#117).

    The test exercises the same path with pytest.raises — wrapping
    mirrors the production wrapper.
    """
    from kubernetes.client import ApiException

    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                    }
                }
            }
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1")

    class AllFailPodApi(FakeCoreV1Api):
        def delete_namespaced_pod(self, name, namespace, **kwargs):
            raise ApiException(status=404, reason="Not Found")

    pod_api = AllFailPodApi([make_pod("worker-1"), make_pod("worker-2")])
    redis_client = FakeRedisClient(
        {
            "worker-1:1:requeued,simulations": ["a1"],
            "worker-2:1:requeued,simulations": ["a1"],
        }
    )

    with pytest.raises(ApiException, match="404"):
        tick(
            api,
            pod_api=pod_api,
            redis_client=redis_client,
        )


def handler_tick_failures_total(module: str, error_type: str) -> float:
    """Read the labelled-counter value from HANDLER_TICK_FAILURES_TOTAL."""
    counter = HANDLER_TICK_FAILURES_TOTAL
    metrics_dict = getattr(counter, "_metrics", None)
    if metrics_dict:
        snap = metrics_dict.get((module, error_type))
        if snap is not None:
            return float(snap._value.get())
    raw = getattr(counter, "_value", None)
    if raw is not None:
        v = raw.get()
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


# --- Issue #44: pod discovery with matchExpressions -------------------------


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
    assert "app=worker" in selector.split(",")
    assert "tier in (worker)" in selector.split(",")


def test_deployment_label_selector_falls_back_to_matchlabels_on_unsupported_operator(caplog):
    """An exotic operator (e.g. ``Gt``) → matchLabels only + warn."""
    import logging

    caplog.set_level(logging.WARNING, logger="openstudio_operator.handlers.analysis_sla")
    apps = FakeAppsV1Api(
        match_labels={"app": "worker"},
        match_expressions=[_exp("priority", "Gt", ["0"])],
    )
    selector = deployment_label_selector(apps, "worker", NAMESPACE)
    assert selector == "app=worker"
    assert any("matchExpressions operator" in rec.message for rec in caplog.records)


def test_deployment_label_selector_returns_none_when_neither_set():
    """No selector at all → None (the caller must decide what to do)."""
    apps = FakeAppsV1Api(match_labels={}, match_expressions=[])
    assert deployment_label_selector(apps, "worker", NAMESPACE) is None


# --- _status_is_started helper (D1) ------------------------------------------


@responses.activate
def test_status_is_started_with_singular_wrapper():
    """Live v3.11.0 single-match wrapper: ``{analysis: {status: "started"}}``."""
    responses.get(
        f"{BASE}/analyses/a1/status.json",
        json={"analysis": {"_id": "a1", "status": "started"}},
    )
    assert _status_is_started(OpenStudioClient(BASE), "a1") is True


@responses.activate
def test_status_is_started_with_plural_wrapper():
    """Count-based plural wrapper: exactly one match → still started."""
    responses.get(
        f"{BASE}/analyses/a1/status.json",
        json={"analyses": [{"_id": "a1", "status": "started"}]},
    )
    assert _status_is_started(OpenStudioClient(BASE), "a1") is True


@responses.activate
def test_status_is_started_false_for_completed():
    responses.get(
        f"{BASE}/analyses/a1/status.json",
        json={"analysis": {"_id": "a1", "status": "completed"}},
    )
    assert _status_is_started(OpenStudioClient(BASE), "a1") is False


@responses.activate
def test_status_is_started_false_for_unknown_id():
    """Contract §1: unknown id → 200 ``{analyses: []}`` (never 404)."""
    responses.get(
        f"{BASE}/analyses/ghost/status.json",
        json={"analyses": []},
    )
    assert _status_is_started(OpenStudioClient(BASE), "ghost") is False


# --- _escalate_analysis direct coverage (D2) ---------------------------------


@responses.activate
def test_escalate_analysis_uses_redis_resolved_pod_set():
    """Direct coverage of the D2 escalation helper, independent of the SLA tick."""
    api = FakeCustomObjectsApi(
        make_cr(
            status={
                "softStops": {
                    "a1": {
                        "issuedAt": PAST_GRACE.isoformat(),
                        "outcome": "issued",
                    }
                }
            }
        )
    )
    store = StatusStore(NAMESPACE, NAME, api)
    pod_api = FakeCoreV1Api(
        [
            make_pod("worker-a"),
            make_pod("worker-b"),
        ]
    )
    redis_client = FakeRedisClient(
        {
            "worker-a:7:requeued,simulations": ["a1"],
            "worker-b:9:requeued,simulations": ["a1"],  # both processing
        }
    )

    _events, emit = make_emit()
    outcome = _escalate_analysis(
        OpenStudioClient(BASE),
        store,
        OperatorConfig.from_spec(SPEC),
        "a1",
        record=store.get_soft_stop("a1"),
        now=NOW,
        emit=emit,
        namespace=NAMESPACE,
        pod_api=pod_api,
        redis_client=redis_client,
    )

    assert outcome == "evicted"
    assert {d["name"] for d in pod_api.deletes} == {"worker-a", "worker-b"}
    assert api.obj["status"]["softStops"]["a1"]["escalationOutcome"] == "evicted"


# --- Issue #232: kopf 1.4x MappingView body end-to-end through EventEmitter -----


@responses.activate
def test_run_sla_tick_accepts_mappingview_body_in_event_emitter():
    """Issue #232 — MappingView (types.MappingProxyType) body end-to-end.

    kopf >=1.4x delivers ``body`` to timer handlers as a
    ``kopf._cogs.structs.bodies.Body`` — a MappingView subclass, NOT a dict
    subclass. ``test_singleton_guard.py::test_gated_wrapper_accepts_non_dict_mapping_body``
    pins this only at the singleton-gate boundary; the four ``@kopf.timer``
    wrappers pass ``body=body`` straight into
    ``EventEmitter(body=body, dry_run=...)``. This regression test wires
    ``types.MappingProxyType`` (the standard-library stand-in for a non-dict
    mapping) into ``EventEmitter``, drives ``run_sla_tick`` through its
    soft-stop emit path, and asserts:

    1. ``EventEmitter.dry_run`` gate still records the suppression through
       ``EventEmitter.suppressed_count`` (D11 end-to-end);
    2. ``body.get('metadata')`` introspection keeps working — the access
       shape ``kopf.event`` and any status-update helper rely on.

    No production code change; the test fails the day a kopf upgrade makes
    the body shape incompatible with ``EventEmitter`` (e.g. by binding it
    to ``dict`` semantics). Scope guard: handler / guard untouched.
    """
    # Anchor 2h old with maxDurationMinutes=60 → soft-stop fires this tick;
    # gracefulStopTimeoutMinutes=600 keeps the escalation path dormant so
    # suppressed_count reflects exactly the soft-stop Warning.
    spec = {
        "serverUrl": BASE,
        "redisUrl": "redis://:pw@queue.test:6379",
        "dryRun": True,
        "analysisPolicy": {"maxDurationMinutes": 60, "gracefulStopTimeoutMinutes": 600},
    }
    api = FakeCustomObjectsApi(
        make_cr(
            spec,
            status={
                "softStops": {
                    "a1": {"issuedAt": (NOW - timedelta(hours=2)).isoformat(), "outcome": "watching"},
                },
            },
        )
    )
    register_analyses_index("a1")
    register_analysis_status("a1")

    # CR body as kopf 1.4x would deliver it: a MappingView, not a dict subclass.
    cr_body = make_cr(spec)
    proxy_body = types.MappingProxyType(cr_body)
    assert not isinstance(proxy_body, dict)  # the shape this regression exists for
    # (2) body.get('metadata') still resolves through the proxy — the access
    # pattern kopf.event and any status-update helper rely on.
    assert proxy_body.get("metadata") == cr_body["metadata"]

    # (1) Wire the production EventEmitter (not the test closure stub) — the
    # D11 gate under test lives here.
    emitter = EventEmitter(body=proxy_body, dry_run=True)
    config = OperatorConfig.from_spec(spec)
    store = StatusStore(NAMESPACE, NAME, api)

    result = run_sla_tick(
        OpenStudioClient(BASE),
        store,
        config,
        now=NOW,
        emit=emitter,
        namespace=NAMESPACE,
        pod_api=None,
        redis_client=FakeRedisClient(),
    )

    assert result.soft_stopped == ["a1"]
    assert result.escalated == []
    # The dry-run gate still records exactly the one soft-stop Warning.
    assert emitter.suppressed_count == 1
    assert emitter.dry_run is True
