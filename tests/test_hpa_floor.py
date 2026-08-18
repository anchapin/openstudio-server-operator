"""Unit tests for the HPA-floor adjuster (issue #18, D10).

No OpenStudio REST is involved (the adjuster senses Redis + Kubernetes
only): Redis is mocked with ``fakeredis`` behind the real
``ReadOnlyRedisClient``, and the HPA read/patch with a
``FakeAutoscalingV1Api`` that records every patch payload — the
only-``minReplicas`` discipline is asserted from those recorded payloads,
not from source grepping. The cooldown clock is driven by passing explicit
``now`` datetimes (one shared NOW base), mirroring
test_web_background_monitor.py. All assertions target ``run_hpa_floor_tick``
directly; the kopf timer wrapper is thin wiring.
"""

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import fakeredis
import pytest
from kubernetes.client import ApiException
from prometheus_client import REGISTRY

from openstudio_operator.config import (
    DEFAULT_HPA_FLOOR_POLICY,
    HpaFloorPolicy,
)
from openstudio_operator.handlers.hpa_floor import (
    HPA_FLOOR_DECAYED_EVENT,
    HPA_FLOOR_RAISED_EVENT,
    WORKER_HPA_NAME,
    HpaFloorState,
    run_hpa_floor_tick,
)
from openstudio_operator.redis_client import ReadOnlyRedisClient, RedisClientError
from openstudio_operator.status_store import MERGE_PATCH_CONTENT_TYPE

NAMESPACE = "openstudio-server"
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
COOLDOWN = timedelta(seconds=DEFAULT_HPA_FLOOR_POLICY.cooldown_seconds)
POLICY = HpaFloorPolicy()  # defaults: tiers (500,10)(250,8)(120,6)(60,4)(25,3)(10,2), baseline 1


class FakeAutoscalingV1Api:
    """In-memory AutoscalingV1Api stand-in recording every patch payload."""

    def __init__(self, min_replicas: int = 1, max_replicas: int = 20) -> None:
        self.spec = SimpleNamespace(
            min_replicas=min_replicas, max_replicas=max_replicas
        )
        self.patches: list[dict] = []
        self.reads: list[tuple[str, str]] = []

    def read_namespaced_horizontalpodautoscaler(self, name, namespace, **kwargs):
        self.reads.append((name, namespace))
        return SimpleNamespace(spec=self.spec)

    def patch_namespaced_horizontalpodautoscaler(self, name, namespace, body, **kwargs):
        self.patches.append(
            {"name": name, "namespace": namespace, "body": copy.deepcopy(body), "kwargs": kwargs}
        )
        # Apply like the API server would, so subsequent ticks observe the new floor.
        self.spec.min_replicas = body["spec"]["minReplicas"]
        return {"metadata": {"name": name}}


class ExplodingRedis:
    def queue_depths(self) -> dict[str, int]:
        raise RedisClientError("queue fabric unreachable")


def make_redis(*, simulations: int = 0, requeued: int = 0) -> ReadOnlyRedisClient:
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    for job in range(simulations):
        fake.rpush("simulations", f"sim-job-{job}")
    for job in range(requeued):
        fake.rpush("requeued", f"req-job-{job}")
    return ReadOnlyRedisClient("redis://:pw@queue.test:6379", connection=fake)


def make_emit():
    events: list[tuple[str, str, str]] = []

    def emit(event_type: str, reason: str, message: str) -> None:
        events.append((event_type, reason, message))

    return events, emit


def adjustments_total() -> float:
    return REGISTRY.get_sample_value("openstudio_operator_hpa_floor_adjustments_total") or 0.0


def anchored_state() -> HpaFloorState:
    """A state whose restart-conservative observation gate has fully opened."""
    state = HpaFloorState()
    assert state.gate_open(NOW - 2 * COOLDOWN, COOLDOWN) is False  # first observation anchors
    assert state.gate_open(NOW - COOLDOWN, COOLDOWN) is True  # one cooldown later it opens
    return state


def tick(
    api: FakeAutoscalingV1Api,
    state: HpaFloorState,
    *,
    simulations: int = 0,
    requeued: int = 0,
    now: datetime = NOW,
    policy: HpaFloorPolicy = POLICY,
    dry_run: bool = False,
    redis: ReadOnlyRedisClient | None = None,
):
    events, emit = make_emit()
    fired = run_hpa_floor_tick(
        redis if redis is not None else make_redis(simulations=simulations, requeued=requeued),
        api,
        namespace=NAMESPACE,
        policy=policy,
        now=now,
        emit=emit,
        state=state,
        dry_run=dry_run,
    )
    return fired, events


# --- Threshold mapping (backlog → minReplicas), boundaries included -------------


@pytest.mark.parametrize(
    ("backlog", "floor"),
    [
        (0, 1),  # baseline
        (9, 1),  # just below the shallowest tier
        (10, 2),  # tier boundary is inclusive
        (24, 2),
        (25, 3),
        (59, 3),
        (60, 4),
        (119, 4),
        (120, 6),
        (249, 6),
        (250, 8),
        (499, 8),
        (500, 10),
        (5000, 10),  # saturates at the deepest tier
    ],
)
def test_floor_for_backlog_default_tiers(backlog, floor):
    assert DEFAULT_HPA_FLOOR_POLICY.floor_for(backlog) == floor


def test_floor_for_backlog_is_table_driven():
    policy = HpaFloorPolicy(tiers=((7, 3), (3, 2)), baseline_min_replicas=1)
    assert [policy.floor_for(n) for n in (0, 2, 3, 6, 7, 100)] == [1, 1, 2, 2, 3, 3]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tiers": ((10, 2), (10, 3))},  # duplicate thresholds
        {"tiers": ((0, 2),)},  # threshold below 1
        {"tiers": ((10, 0),)},  # floor below 1
        {"tiers": (), "baseline_min_replicas": 0},  # baseline below 1
        {"cooldown_seconds": 0},  # non-positive cooldown
    ],
)
def test_policy_rejects_invalid_tables(kwargs):
    with pytest.raises(ValueError):
        HpaFloorPolicy(**kwargs)


# --- Raise on deep backlog ------------------------------------------------------


def test_deep_backlog_raises_floor():
    api = FakeAutoscalingV1Api(min_replicas=1)
    state = anchored_state()
    before = adjustments_total()

    fired, events = tick(api, state, simulations=200, requeued=100)  # backlog 300 → 8

    assert fired is True
    assert len(api.patches) == 1
    assert api.patches[0]["body"] == {"spec": {"minReplicas": 8}}
    assert api.patches[0]["name"] == WORKER_HPA_NAME == "worker-hpa"
    assert api.patches[0]["kwargs"]["_content_type"] == MERGE_PATCH_CONTENT_TYPE
    assert events == [("Normal", HPA_FLOOR_RAISED_EVENT, events[0][2])]
    assert adjustments_total() == before + 1
    assert state.last_adjusted_at == NOW


def test_backlog_is_sum_not_max_of_queues():
    # sum 170+130=300 → floor 8; the max (130) would map to only 6.
    api = FakeAutoscalingV1Api(min_replicas=1)
    fired, _ = tick(api, anchored_state(), simulations=170, requeued=130)
    assert fired is True
    assert api.patches[0]["body"] == {"spec": {"minReplicas": 8}}


def test_raise_never_exceeds_hpa_max_replicas():
    # kind topology (06-worker.yaml): maxReplicas 2 clamps the deepest floor.
    api = FakeAutoscalingV1Api(min_replicas=1, max_replicas=2)
    fired, _ = tick(api, anchored_state(), simulations=5000)
    assert fired is True
    assert api.patches[0]["body"] == {"spec": {"minReplicas": 2}}


def test_unspecified_min_replicas_counts_as_one():
    api = FakeAutoscalingV1Api(min_replicas=1)
    api.spec.min_replicas = None  # omitted in the object → API-server default 1
    fired, _ = tick(api, anchored_state(), simulations=300)
    assert fired is True
    assert api.patches[0]["body"] == {"spec": {"minReplicas": 8}}


# --- No upward churn ------------------------------------------------------------


def test_current_floor_at_target_never_patches():
    api = FakeAutoscalingV1Api(min_replicas=8)
    state = anchored_state()
    before = adjustments_total()

    fired, events = tick(api, state, simulations=300)  # target 8 == current 8

    assert fired is False
    assert api.patches == []
    assert events == []
    assert adjustments_total() == before
    assert state.last_adjusted_at is None  # no cooldown consumed by a no-op


def test_current_floor_above_target_decays_symmetrically():
    # The adjuster owns the floor: a manual floor above the mapped target is
    # decayed, not silently respected (documented symmetric reconciliation).
    api = FakeAutoscalingV1Api(min_replicas=10)
    fired, events = tick(api, anchored_state(), simulations=300)  # target 8
    assert fired is True
    assert api.patches[0]["body"] == {"spec": {"minReplicas": 8}}
    assert events[0][:2] == ("Warning", HPA_FLOOR_DECAYED_EVENT)


# --- Decay to baseline on clear -------------------------------------------------


def test_clear_backlog_decays_to_baseline():
    api = FakeAutoscalingV1Api(min_replicas=8)  # e.g. raised by an earlier tick
    state = anchored_state()
    before = adjustments_total()

    fired, events = tick(api, state, simulations=0, requeued=0)

    assert fired is True
    assert api.patches[0]["body"] == {"spec": {"minReplicas": 1}}
    assert events[0][:2] == ("Warning", HPA_FLOOR_DECAYED_EVENT)
    assert adjustments_total() == before + 1


def test_shrinking_backlog_decays_to_mid_tier_immediately():
    api = FakeAutoscalingV1Api(min_replicas=8)
    fired, _ = tick(api, anchored_state(), simulations=70)  # backlog 70 → 4
    assert fired is True
    assert api.patches[0]["body"] == {"spec": {"minReplicas": 4}}


# --- Cooldown: anti-flap + restart-conservatism ---------------------------------


def test_cooldown_blocks_opposite_signal_until_elapsed():
    api = FakeAutoscalingV1Api(min_replicas=1)
    state = anchored_state()

    fired, _ = tick(api, state, simulations=300, now=NOW)  # raise 1 → 8
    assert fired is True

    # Backlog fully drains one tick later: gate closed, no decay patch.
    fired, events = tick(api, state, simulations=0, now=NOW + timedelta(seconds=60))
    assert fired is False
    assert events == []
    assert len(api.patches) == 1

    # Exactly one cooldown after the adjustment the decay may fire.
    fired, _ = tick(api, state, simulations=0, now=NOW + COOLDOWN)
    assert fired is True
    assert [p["body"]["spec"]["minReplicas"] for p in api.patches] == [8, 1]


def test_fresh_process_waits_out_one_cooldown_of_observation():
    api = FakeAutoscalingV1Api(min_replicas=1)
    state = HpaFloorState()  # fresh operator process: no anchor at all

    fired, _ = tick(api, state, simulations=300, now=NOW)
    assert fired is False  # first successful observation anchors, gate closed
    assert api.patches == []
    assert state.first_observed_at == NOW

    fired, _ = tick(api, state, simulations=300, now=NOW + COOLDOWN - timedelta(seconds=1))
    assert fired is False

    fired, _ = tick(api, state, simulations=300, now=NOW + COOLDOWN)
    assert fired is True
    assert api.patches[0]["body"] == {"spec": {"minReplicas": 8}}


def test_failed_sensing_never_anchors_the_gate():
    api = FakeAutoscalingV1Api(min_replicas=1)
    state = HpaFloorState()

    with pytest.raises(RedisClientError):
        tick(api, state, redis=ExplodingRedis(), simulations=300)
    assert state.first_observed_at is None
    assert api.patches == []

    # The next successful tick is what anchors — from NOW, not from the failure.
    fired, _ = tick(api, state, simulations=300, now=NOW)
    assert fired is False
    assert state.first_observed_at == NOW


def test_hpa_read_failure_skips_tick():
    api = FakeAutoscalingV1Api(min_replicas=1)

    def explode(name, namespace, **kwargs):
        raise ApiException(status=404, reason="NotFound")

    api.read_namespaced_horizontalpodautoscaler = explode
    with pytest.raises(ApiException):
        tick(api, anchored_state(), simulations=300)
    assert api.patches == []


# --- dryRun (D11) ---------------------------------------------------------------


def test_dry_run_suppresses_patch_but_paces_like_real():
    api = FakeAutoscalingV1Api(min_replicas=1)
    state = anchored_state()
    before = adjustments_total()

    fired, events = tick(api, state, simulations=300, dry_run=True)

    assert fired is True
    assert api.patches == []  # no mutation reached the cluster
    assert events[0][:2] == ("Normal", HPA_FLOOR_RAISED_EVENT)
    assert "dryRun" in events[0][2] and "suppressed" in events[0][2]
    assert adjustments_total() == before + 1  # decisions count in dry-run too
    assert state.last_adjusted_at == NOW  # cooldown advances in dry-run

    # Pacing: the immediate real signal after the dry-run raise is still gated.
    fired, _ = tick(api, state, simulations=0, now=NOW + timedelta(seconds=60))
    assert fired is False
    assert api.patches == []


# --- Only spec.minReplicas is ever patched --------------------------------------


def test_every_patch_payload_touches_only_min_replicas():
    api = FakeAutoscalingV1Api(min_replicas=1)
    state = anchored_state()

    tick(api, state, simulations=500, now=NOW)  # raise to 10
    tick(api, state, simulations=0, now=NOW + COOLDOWN)  # decay to 1

    assert len(api.patches) == 2
    for patch in api.patches:
        assert set(patch["body"]) == {"spec"}
        assert set(patch["body"]["spec"]) == {"minReplicas"}  # never maxReplicas/metrics
        assert isinstance(patch["body"]["spec"]["minReplicas"], int)
        assert patch["kwargs"]["_content_type"] == MERGE_PATCH_CONTENT_TYPE
