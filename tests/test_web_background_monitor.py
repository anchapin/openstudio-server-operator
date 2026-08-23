"""Unit tests for the web_background stall detector (issue #13, D07).

No OpenStudio REST is involved at all (the stall senses Redis + Kubernetes
only): Redis is mocked with ``fakeredis`` behind the real
``ReadOnlyRedisClient`` (``now_fn`` pinned to each tick's clock so
heartbeat staleness and window math share one instant), the CR ``.status``
subresource with the in-memory RFC 7386 merge-patch fake (same approach as
test_worker_recycler.py), the Deployment read/patch with a
``FakeAppsV1Api``, and pod listing with a ``FakeCoreV1Api`` — no live
cluster, no dependencies beyond the ``[dev]`` extra.
"""

import logging as _logging
import time as _time
import types
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import fakeredis
import pytest
from prometheus_client import REGISTRY

from _fakes import FakeAppsV1Api, FakeCustomObjectsApi, make_emit
from _fakes import make_cr as _shared_make_cr
from openstudio_operator._k8s import MERGE_PATCH_CONTENT_TYPE, RESTARTED_AT_ANNOTATION
from openstudio_operator.config import OperatorConfig
from openstudio_operator.events import EventEmitter
from openstudio_operator.handlers import web_background_monitor as _wbm
from openstudio_operator.handlers import web_background_monitor as wbm_module
from openstudio_operator.handlers.web_background_monitor import (
    DEFAULT_WEB_BACKGROUND_DEPLOYMENT,
    RESQUE_KEY_LAYOUT_UNKNOWN_EVENT,
    WEB_BACKGROUND_RESTARTED_EVENT,
    StallWindowTracker,
    run_stall_tick,
)
from openstudio_operator.handlers.web_background_monitor import (
    REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL as _REVALIDATION_INTERVAL,
)
from openstudio_operator.handlers.web_background_monitor import (
    _maybe_revalidate_redis_key_layout as _revalidate,
)
from openstudio_operator.handlers.web_background_monitor import (
    reset_key_layout_revalidation_state as _reset_revalidation,
)
from openstudio_operator.redis_client import (
    REQUEUED_QUEUE,
    SIMULATIONS_QUEUE,
    WORKER_REGISTRY_KEY,
    ReadOnlyRedisClient,
    RedisClientError,
)
from openstudio_operator.status_store import StatusStore

NAMESPACE = "openstudio-server"
NAME = "oscm"
WORKER = "worker-stall-target"
WEBBG = "webbg-restart-target"
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

SPEC = {
    "serverUrl": "http://web.test",
    "redisUrl": "redis://:pw@queue.test:6379",
    "targetWorkerDeployment": WORKER,
    "targetWebBackgroundDeployment": WEBBG,
    "webBackgroundPolicy": {"stallWindowMinutes": 10},
}

# Worker-selector string the fake AppsV1Api's default match_labels produce —
# the shared ``FakeAppsV1Api`` (#531) serves the same helm-chart selector the
# old module-local fake did.
SELECTOR_STRING = "app.kubernetes.io/name=openstudio-server,component=worker"

# Shared-fake binding (issue #474): this module's make_cr default spec.
make_cr = partial(_shared_make_cr, default_spec=SPEC)


def minute(n: int) -> timedelta:
    return timedelta(minutes=n)


def make_pod(phase: str = "Running", ready: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        status=SimpleNamespace(
            phase=phase,
            conditions=[SimpleNamespace(type="Ready", status="True" if ready else "False")],
        )
    )


class FakeCoreV1Api:
    """Serves a fixed pod list; records the label selectors used."""

    def __init__(self, pods: list[SimpleNamespace]) -> None:
        self.pods = pods
        self.selectors: list[str | None] = []

    def list_namespaced_pod(self, namespace, **kwargs):
        self.selectors.append(kwargs.get("label_selector"))
        return SimpleNamespace(items=list(self.pods))


class ExplodingRedis:
    """Fails any sensing call — proves gated ticks never sense at all."""

    def queue_depths(self) -> dict[str, int]:
        raise AssertionError("the gate must close before any sensing")

    def worker_heartbeats(self) -> dict[str, float | None]:
        raise AssertionError("the gate must close before any sensing")

    def stale_workers(self, threshold_seconds: float) -> set[str]:
        raise AssertionError("the gate must close before any sensing")


def restarts_total() -> float:
    return REGISTRY.get_sample_value("openstudio_operator_web_background_restarts_total") or 0.0


def make_redis(
    now: datetime = NOW,
    *,
    simulations: int = 0,
    requeued: int = 0,
    heartbeats: dict[str, float | None] | None = None,
) -> ReadOnlyRedisClient:
    """fakeredis-backed client with its staleness clock pinned to ``now``."""
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    for job in range(simulations):
        fake.rpush(SIMULATIONS_QUEUE, f"sim-job-{job}")
    for job in range(requeued):
        fake.rpush(REQUEUED_QUEUE, f"req-job-{job}")
    for worker_id, heartbeat in (heartbeats or {}).items():
        fake.sadd("resque:workers", worker_id)
        if heartbeat is not None:
            iso = datetime.fromtimestamp(heartbeat, tz=UTC).isoformat()
            fake.hset("resque:workers:heartbeat", worker_id, iso)
    return ReadOnlyRedisClient(SPEC["redisUrl"], connection=fake, now_fn=lambda: now.timestamp())


def stall_redis(now: datetime = NOW, *, queued: bool = True) -> ReadOnlyRedisClient:
    """The full stall condition on the Redis side: queued + all-stale heartbeats."""
    epoch = now.timestamp()
    return make_redis(
        now,
        simulations=2 if queued else 0,
        heartbeats={"w1": epoch - 600, "w2": epoch - 900},  # both > 300 s stale
    )


def tick(
    api,
    apps=None,
    pods=None,
    spec=None,
    *,
    now=NOW,
    tracker=None,
    redis=None,
):
    store = StatusStore(NAMESPACE, NAME, api)
    config = OperatorConfig.from_spec(spec if spec is not None else SPEC)
    events, emit = make_emit()
    if tracker is None:
        tracker = StallWindowTracker()
    fired = run_stall_tick(
        redis if redis is not None else stall_redis(now),
        store,
        config,
        apps if apps is not None else FakeAppsV1Api(),
        pods if pods is not None else FakeCoreV1Api([make_pod(), make_pod()]),
        namespace=NAMESPACE,
        now=now,
        emit=emit,
        tracker=tracker,
    )
    return fired, events


def expected_patch(now: datetime = NOW) -> dict:
    return {
        "spec": {
            "template": {"metadata": {"annotations": {RESTARTED_AT_ANNOTATION: now.isoformat()}}}
        }
    }


# --- Sustained stall fires exactly one restart ---------------------------------


def test_full_stall_sustained_fires_restart_once():
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    tracker = StallWindowTracker()
    metric_before = restarts_total()

    for offset in (0, 3, 6, 9):  # condition holds, window not yet sustained
        fired, events = tick(
            api,
            apps,
            pods,
            now=NOW + minute(offset),
            tracker=tracker,
            redis=stall_redis(NOW + minute(offset)),
        )
        assert fired is False
        assert events == []
    assert apps.patches == []

    fired, events = tick(
        api,
        apps,
        pods,
        now=NOW + minute(10),
        tracker=tracker,  # exactly the window
        redis=stall_redis(NOW + minute(10)),
    )

    assert fired is True
    assert len(apps.patches) == 1
    patch = apps.patches[0]
    assert patch["name"] == WEBBG
    assert patch["namespace"] == NAMESPACE
    assert patch["kwargs"]["_content_type"] == MERGE_PATCH_CONTENT_TYPE
    assert patch["body"] == expected_patch(NOW + minute(10))
    assert api.obj["status"]["lastWebBackgroundRestart"] == (NOW + minute(10)).isoformat()
    assert len(events) == 1
    event_type, reason, message = events[0]
    assert event_type == "Warning"
    assert reason == WEB_BACKGROUND_RESTARTED_EVENT
    assert WEBBG in message and "suppressed" not in message
    assert restarts_total() - metric_before == 1
    # Worker-fleet corroboration went through the worker Deployment selector.
    assert apps.reads == [(WORKER, NAMESPACE)] * 5
    assert pods.selectors == [SELECTOR_STRING] * 5


def test_empty_webbg_target_falls_back_to_chart_deployment_name():
    spec = {k: v for k, v in SPEC.items() if k != "targetWebBackgroundDeployment"}
    api = FakeCustomObjectsApi(make_cr(spec))
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    tick(api, apps, spec=spec, now=NOW, tracker=tracker, redis=stall_redis(NOW))
    fired, _ = tick(
        api,
        apps,
        spec=spec,
        now=NOW + minute(10),
        tracker=tracker,
        redis=stall_redis(NOW + minute(10)),
    )

    assert fired is True
    assert apps.patches[0]["name"] == DEFAULT_WEB_BACKGROUND_DEPLOYMENT


# --- THE core test: transient blips below the window never trigger --------------


def test_transient_blip_below_window_never_triggers():
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    # Hold the condition for 4 minutes (below the 10-minute window)…
    for offset in (0, 1, 2, 3, 4):
        tick(
            api,
            apps,
            now=NOW + minute(offset),
            tracker=tracker,
            redis=stall_redis(NOW + minute(offset)),
        )
    # …then it clears (queue drained) for two ticks…
    for offset in (5, 6):
        tick(
            api,
            apps,
            now=NOW + minute(offset),
            tracker=tracker,
            redis=stall_redis(NOW + minute(offset), queued=False),
        )
    # …and comes back. Total elapsed since first observation now far exceeds
    # the window, but NEITHER episode was sustained for a full window, so
    # the restart must never fire.
    for offset in (7, 8, 9, 10, 11, 13):
        tick(
            api,
            apps,
            now=NOW + minute(offset),
            tracker=tracker,
            redis=stall_redis(NOW + minute(offset)),
        )

    assert apps.patches == []
    assert "lastWebBackgroundRestart" not in api.obj["status"]


def test_heartbeat_recovery_mid_window_resets_the_clock():
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    # Stale for 8 minutes, then one worker heartbeats fresh (leg B breaks)…
    for offset in (0, 2, 4, 6, 8):
        tick(
            api,
            apps,
            now=NOW + minute(offset),
            tracker=tracker,
            redis=stall_redis(NOW + minute(offset)),
        )
    epoch = (NOW + minute(9)).timestamp()
    tick(
        api,
        apps,
        now=NOW + minute(9),
        tracker=tracker,
        redis=make_redis(
            NOW + minute(9), simulations=2, heartbeats={"w1": epoch - 5, "w2": epoch - 900}
        ),
    )
    # …stale again: only 1 minute of re-observed stall — no restart at +10.
    tick(api, apps, now=NOW + minute(10), tracker=tracker, redis=stall_redis(NOW + minute(10)))

    assert apps.patches == []


# --- Any single leg absent → no trigger -----------------------------------------


def test_empty_queues_no_trigger_even_sustained():
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    for offset in (0, 5, 10, 15):
        tick(
            api,
            apps,
            now=NOW + minute(offset),
            tracker=tracker,
            redis=stall_redis(NOW + minute(offset), queued=False),
        )

    assert apps.patches == []
    assert "lastWebBackgroundRestart" not in api.obj["status"]


def test_requeued_queue_alone_counts_as_queued_work():
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    for offset in (0, 5, 10):
        epoch = (NOW + minute(offset)).timestamp()
        tick(
            api,
            apps,
            now=NOW + minute(offset),
            tracker=tracker,
            redis=make_redis(NOW + minute(offset), requeued=3, heartbeats={"w1": epoch - 3600}),
        )  # simulations stays 0

    assert len(apps.patches) == 1


def test_fresh_heartbeat_no_trigger_even_sustained():
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    for offset in (0, 5, 10, 15):
        epoch = (NOW + minute(offset)).timestamp()
        tick(
            api,
            apps,
            now=NOW + minute(offset),
            tracker=tracker,
            redis=make_redis(
                NOW + minute(offset),
                simulations=2,
                heartbeats={"w1": epoch - 600, "w2": epoch - 10},
            ),
        )

    assert apps.patches == []


def test_unhealthy_worker_pods_no_trigger_even_sustained():
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    # A NotReady pod (readiness failing): K8s no longer vouches for the fleet.
    for offset in (0, 5, 10, 15):
        tick(
            api,
            apps,
            pods=FakeCoreV1Api([make_pod(ready=False), make_pod()]),
            now=NOW + minute(offset),
            tracker=tracker,
            redis=stall_redis(NOW + minute(offset)),
        )

    assert apps.patches == []


def test_pending_worker_pod_no_trigger():
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()
    pods = FakeCoreV1Api([make_pod(phase="Pending")])

    for offset in (0, 5, 10, 15):
        tick(
            api,
            apps,
            pods=pods,
            now=NOW + minute(offset),
            tracker=tracker,
            redis=stall_redis(NOW + minute(offset)),
        )

    assert apps.patches == []


def test_zero_worker_pods_no_trigger():
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()
    pods = FakeCoreV1Api([])

    for offset in (0, 5, 10, 15):
        tick(
            api,
            apps,
            pods=pods,
            now=NOW + minute(offset),
            tracker=tracker,
            redis=stall_redis(NOW + minute(offset)),
        )

    assert apps.patches == []


def test_empty_worker_registry_counts_as_nobody_processing():
    """Vacuous leg B: zero registered workers means no one is processing."""
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    for offset in (0, 5, 10):
        tick(
            api,
            apps,
            now=NOW + minute(offset),
            tracker=tracker,
            redis=make_redis(NOW + minute(offset), simulations=1),
        )

    assert len(apps.patches) == 1


# --- Cooldown: gate is the single decision point --------------------------------


def stall_ticks(api, apps, tracker, *offsets: int) -> None:
    for offset in offsets:
        tick(
            api,
            apps,
            now=NOW + minute(offset),
            tracker=tracker,
            redis=stall_redis(NOW + minute(offset)),
        )


def test_cooldown_blocks_second_restart_even_if_stall_persists():
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()
    metric_before = restarts_total()

    stall_ticks(api, apps, tracker, 0, 5, 10)  # first restart fires at +10
    assert len(apps.patches) == 1

    # Stall persists through and past the cooldown window. Gated ticks do
    # not even observe; the first open-gate tick re-observes on a fresh
    # clock, and even that needs a full window before the next restart.
    stall_ticks(api, apps, tracker, 11, 15, 19)
    stall_ticks(api, apps, tracker, 20, 21)
    stall_ticks(api, apps, tracker, 25, 30)

    assert len(apps.patches) == 1  # still exactly ONE restart
    assert restarts_total() - metric_before == 1
    assert api.obj["status"]["lastWebBackgroundRestart"] == (NOW + minute(10)).isoformat()


def test_cooldown_expiry_allows_another_restart():
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    stall_ticks(api, apps, tracker, 0, 5, 10)  # first restart at +10
    # Gate opens strictly after +20; re-observation starts at +21 (the first
    # open-gate tick), so the second restart fires at +31 — sustained anew.
    stall_ticks(api, apps, tracker, 21, 25, 31)

    assert len(apps.patches) == 2
    assert apps.patches[1]["body"] == expected_patch(NOW + minute(31))
    assert api.obj["status"]["lastWebBackgroundRestart"] == (NOW + minute(31)).isoformat()


def test_gate_at_exact_window_boundary_stays_closed():
    """Cooldown is inclusive at the boundary (mirrors the #11 gate: <= blocks)."""
    last = NOW - minute(10)  # exactly one window ago
    api = FakeCustomObjectsApi(make_cr(status={"lastWebBackgroundRestart": last.isoformat()}))
    tracker = StallWindowTracker()
    tracker.first_observed = NOW - minute(99)  # long since sustained

    fired, events = tick(api, redis=ExplodingRedis(), tracker=tracker)

    assert fired is False
    assert events == []
    assert tracker.first_observed == NOW - minute(99)  # gated ticks never observe


def test_gate_closed_senses_nothing_at_all():
    status = {"lastWebBackgroundRestart": (NOW - minute(3)).isoformat()}
    api = FakeCustomObjectsApi(make_cr(status=status))
    apps = FakeAppsV1Api()

    fired, events = tick(api, apps, redis=ExplodingRedis(), tracker=StallWindowTracker())

    assert fired is False
    assert apps.patches == []
    assert apps.reads == []
    assert events == []


# --- Cooldown honored across operator restarts (D04) ----------------------------


def test_operator_restart_mid_cooldown_honors_persisted_anchor():
    """Fresh tracker (process state lost), same persisted CR: gate still wins.

    The stall condition has observably held since long before the anchor;
    only the CR-anchored cooldown keeps it from re-firing immediately.
    """
    anchor = NOW - minute(4)  # mid-cooldown (4 < 10)
    api = FakeCustomObjectsApi(make_cr(status={"lastWebBackgroundRestart": anchor.isoformat()}))
    apps = FakeAppsV1Api()
    fresh_tracker = StallWindowTracker()  # operator restarted: clock wiped

    fired, events = tick(api, apps, redis=ExplodingRedis(), tracker=fresh_tracker)

    assert fired is False
    assert apps.patches == []
    assert events == []
    assert api.obj["status"]["lastWebBackgroundRestart"] == anchor.isoformat()


def test_operator_restart_after_cooldown_needs_fresh_sustained_window():
    """Anchor expired + fresh process: still no instant re-fire. No
    ``stallWindowStartedAt`` is persisted here (the window never began
    before the restart), so the fresh tracker must accumulate the full
    window from its first holding tick (#582 restore path reads the
    anchor only when one was checkpointed)."""
    status = {"lastWebBackgroundRestart": (NOW - minute(11)).isoformat()}
    api = FakeCustomObjectsApi(make_cr(status=status))
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()  # fresh process: first_observed is None

    fired1, _ = tick(api, apps, now=NOW, tracker=tracker, redis=stall_redis(NOW))
    fired2, _ = tick(
        api, apps, now=NOW + minute(9), tracker=tracker, redis=stall_redis(NOW + minute(9))
    )
    fired3, _ = tick(
        api, apps, now=NOW + minute(10), tracker=tracker, redis=stall_redis(NOW + minute(10))
    )

    assert (fired1, fired2, fired3) == (False, False, True)
    assert len(apps.patches) == 1


# --- Stall-window start persisted across operator restarts (issue #582, D04) ----


def test_stall_begin_writes_window_started_at_status():
    """The first holding tick checkpoints the window begin to the CR (#582)."""
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    tick(api, apps, now=NOW, tracker=tracker, redis=stall_redis(NOW))

    assert api.obj["status"]["stallWindowStartedAt"] == NOW.isoformat()


def test_window_break_clears_window_started_at_status():
    """Any tick that observes the condition broken clears the anchor (#582) —
    the persisted window and the in-memory tracker reset in lockstep."""
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    tick(api, apps, now=NOW, tracker=tracker, redis=stall_redis(NOW))
    assert "stallWindowStartedAt" in api.obj["status"]
    tick(
        api,
        apps,
        now=NOW + minute(1),
        tracker=tracker,
        redis=stall_redis(NOW + minute(1), queued=False),
    )

    assert "stallWindowStartedAt" not in api.obj["status"]


def test_restart_mid_window_restores_elapsed_stall_window():
    """Issue #582 acceptance — mirrors the recycler's
    ``test_restart_mid_cooldown_honors_persisted_last_recycle_at``: a fresh
    tracker (process state lost) plus the same persisted CR must keep the
    elapsed window instead of restarting from zero. Without the checkpoint,
    a degraded cluster that bounces the operator mid-stall could defer the
    Module-5 restart indefinitely."""
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    metric_before = restarts_total()
    tracker_a = StallWindowTracker()

    # Process 1: stall holds at t=0/2/4 — 4 of the 10 required minutes,
    # anchor checkpointed at the t=0 stall-begin tick.
    for offset in (0, 2, 4):
        fired, _ = tick(
            api,
            apps,
            pods,
            now=NOW + minute(offset),
            tracker=tracker_a,
            redis=stall_redis(NOW + minute(offset)),
        )
        assert fired is False
    assert api.obj["status"]["stallWindowStartedAt"] == NOW.isoformat()
    assert apps.patches == []

    # Operator restart: FRESH tracker, same persisted CR (status survives).
    fresh_tracker = StallWindowTracker()
    fired, events = tick(
        api,
        apps,
        pods,
        now=NOW + minute(10),
        tracker=fresh_tracker,
        redis=stall_redis(NOW + minute(10)),
    )

    assert fired is True  # window measured from t=0 (restored), not t=10
    # Restore proven by the outcome: a fresh observation at t=10 would need
    # until t=20 to sustain; firing at t=10 is only possible if the tracker
    # inherited the t=0 anchor. Post-fire the tracker resets (consumed).
    assert fresh_tracker.first_observed is None
    assert len(apps.patches) == 1
    assert api.obj["status"]["lastWebBackgroundRestart"] == (NOW + minute(10)).isoformat()
    assert "stallWindowStartedAt" not in api.obj["status"]  # consumed by the fire
    assert restarts_total() - metric_before == 1
    assert len(events) == 1 and events[0][1] == WEB_BACKGROUND_RESTARTED_EVENT


def test_restore_honors_preexisting_anchor_older_than_threshold():
    """A stall that predates the restart fires without re-accumulating
    (#582): pre-seeded status (the exact shape a real restart reads), a
    fresh tracker, and a first holding tick already past the threshold."""
    status = {"stallWindowStartedAt": (NOW - minute(11)).isoformat()}
    api = FakeCustomObjectsApi(make_cr(status=status))
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])

    fired, _ = tick(
        api, apps, pods, now=NOW, tracker=StallWindowTracker(), redis=stall_redis(NOW)
    )

    assert fired is True
    assert len(apps.patches) == 1
    assert api.obj["status"]["lastWebBackgroundRestart"] == NOW.isoformat()
    assert "stallWindowStartedAt" not in api.obj["status"]


def test_sensing_failure_clears_persisted_window_start():
    """Blind gaps break the persisted window too (#582): the documented
    re-accumulate-across-sensing-failures semantics must survive the
    checkpoint — a failed read is no evidence of continuity, so the next
    holding tick may not restore across the gap."""
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    tick(api, apps, now=NOW, tracker=tracker, redis=stall_redis(NOW))
    assert "stallWindowStartedAt" in api.obj["status"]

    class FlakyRedis:
        def worker_heartbeats(self) -> dict[str, float | None]:
            raise RedisClientError("SMEMBERS failed: connection reset")

        def queue_depths(self) -> dict[str, int]:
            raise RedisClientError("LLEN failed: connection reset")

    with pytest.raises(RedisClientError):
        tick(api, apps, now=NOW + minute(1), tracker=tracker, redis=FlakyRedis())

    assert "stallWindowStartedAt" not in api.obj["status"]
    assert tracker.first_observed is None


def test_fired_restart_consumes_window_start_anchor():
    """The anchor is spent when the restart fires (#582): a stale anchor
    must not let the first open-gate tick after the cooldown restore a
    pre-accumulated window and re-fire without re-sustaining."""
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    stall_ticks(api, apps, tracker, 0, 5, 10)  # fires at +10
    assert len(apps.patches) == 1
    assert "stallWindowStartedAt" not in api.obj["status"]

    # Gate re-opens at +21; the fresh tracker finds no anchor to restore
    # and must earn a full new window (no second restart through +30).
    stall_ticks(api, apps, tracker, 21, 25, 30)
    assert len(apps.patches) == 1


# --- Sensing failure: skip tick, restart the window (D12) ------------------------


def test_sensing_failure_raises_resets_tracker_and_is_retried():
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    tick(api, apps, now=NOW, tracker=tracker, redis=stall_redis(NOW))
    assert tracker.first_observed == NOW

    class FlakyRedis:
        """Sensing read fails — the kind of transient the wrapper must skip.

        Since #87 the worker-set read runs FIRST (unconditional gauge), so
        the failure surfaces on ``worker_heartbeats`` before leg A ever
        reads the queue depths.
        """

        def worker_heartbeats(self) -> dict[str, float | None]:
            raise RedisClientError("SMEMBERS failed: connection reset")

        def queue_depths(self) -> dict[str, int]:
            raise RedisClientError("LLEN failed: connection reset")

    with pytest.raises(RedisClientError):
        tick(api, apps, now=NOW + minute(1), tracker=tracker, redis=FlakyRedis())

    # Blind gap: the window restarted, so the stall must re-sustain fully.
    assert tracker.first_observed is None
    stall_ticks(api, apps, tracker, 2, 6, 11, 12)
    assert len(apps.patches) == 1  # fired at +12 (first_observed = +2)
    assert api.obj["status"]["lastWebBackgroundRestart"] == (NOW + minute(12)).isoformat()


# --- Issue #312 — freshness timestamp gauges detect stale data ------------------


def queue_depth_fresh() -> float:
    return REGISTRY.get_sample_value("openstudio_operator_resque_queue_depth_fresh") or 0.0


def stall_window_fresh() -> float:
    return REGISTRY.get_sample_value("openstudio_operator_stall_window_fresh") or 0.0


def _reset_freshness_gauges() -> None:
    """Reset the freshness gauges between tests.

    Both gauges are process-level Prometheus singletons — without the
    reset, an earlier test that touched them leaks the value into the
    next test's ``time.time() > stale`` assertion. Mirrors
    :func:`wbm_module.reset_leg2_safeguard_state` for the leg-2 gauge.
    """
    from openstudio_operator import metrics as _metrics

    _metrics.RESQUE_QUEUE_DEPTH_FRESH.set(0.0)
    _metrics.STALL_WINDOW_FRESH.set(0.0)


def test_resque_queue_depth_fresh_advances_on_successful_sensing_tick():
    """Issue #312 acceptance: the freshness stamp for ``RESQUE_QUEUE_DEPTH``
    advances on every sensing tick that successfully reads Redis, BEFORE
    any leg evaluation runs (the same unconditional-advance pattern
    from issue #87 / #238). The stamp is the ``time.time()`` value at
    the moment ``queue_depths()`` returned — a monotonic timestamp
    that dashboards use to compute staleness. Verified end-to-end via
    the full ``run_stall_tick`` path (a normal sensing tick that
    doesn't fire a restart still has to advance the freshness stamp).
    """
    from openstudio_operator import metrics as _metrics

    _reset_freshness_gauges()
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    tracker = StallWindowTracker()
    wbm_module.reset_leg2_safeguard_state()

    before = _time.time()
    tick(
        api,
        apps,
        pods,
        now=NOW,
        tracker=tracker,
        redis=make_redis(NOW, simulations=2),
    )
    after = _time.time()

    fresh = queue_depth_fresh()
    assert before <= fresh <= after
    assert _metrics.RESQUE_QUEUE_DEPTH_FRESH._value.get() == fresh


def test_stall_window_fresh_advances_on_holding_and_broken_paths():
    """Issue #312 acceptance: the freshness stamp for
    ``STALL_WINDOW_ELAPSED_SECONDS`` advances on BOTH the holding path
    (condition held, ``elapsed`` is set) and the broken path (condition
    cleared, ``0.0`` is set). Verified by running a holding tick
    followed by a broken tick and asserting the stamp strictly
    increases across both — same site as the data gauge, so the
    freshness/value pair stays locked together for the dashboard's
    staleness computation.
    """
    from openstudio_operator import metrics as _metrics

    _reset_freshness_gauges()
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    tracker = StallWindowTracker()
    wbm_module.reset_leg2_safeguard_state()

    # Holding tick (full stall condition): the elapsed-gauge is set to
    # the elapsed seconds since first_observed (here 0 since NOW == NOW).
    before_hold = _time.time()
    tick(api, apps, pods, now=NOW, tracker=tracker, redis=stall_redis(NOW))
    after_hold = _time.time()
    fresh_after_hold = stall_window_fresh()
    assert before_hold <= fresh_after_hold <= after_hold
    assert _metrics.STALL_WINDOW_ELAPSED_SECONDS._value.get() >= 0.0

    # Small sleep so the second timestamp strictly differs from the
    # first (the gauge uses real-time ``time.time()``).
    _time.sleep(0.01)

    # Broken tick (queues drained): the elapsed-gauge is set to 0.0.
    before_break = _time.time()
    tick(
        api,
        apps,
        pods,
        now=NOW + minute(1),
        tracker=tracker,
        redis=stall_redis(NOW + minute(1), queued=False),
    )
    after_break = _time.time()
    fresh_after_break = stall_window_fresh()
    assert before_break <= fresh_after_break <= after_break
    assert _metrics.STALL_WINDOW_ELAPSED_SECONDS._value.get() == 0.0
    # Broken path strictly newer than holding path.
    assert fresh_after_break > fresh_after_hold


def test_freshness_gauges_stale_on_redis_failure():
    """Issue #312 acceptance: the freshness stamp MUST NOT advance on
    the exception path — a Redis/K8s-sensing failure must leave the
    stamp untouched so dashboards can compute
    ``time() - RESQUE_QUEUE_DEPTH_FRESH`` and alert on a sustained
    gap. The data gauges can hold a prior tick's value (the failure
    mode #312 fixes); the freshness gauges must NOT — that gap is
    the alert signal.

    Verifies both gauges in one tick using the same ``FlakyRedis`` that
    the pre-#312 ``test_sensing_failure_raises_resets_tracker_and_is_
    retried`` test exercises, then asserts the freshness stamps are
    exactly the values from the prior successful tick (the issue-#312
    regression fence: a future refactor that wraps the gauge writes in
    a try/finally or bumps the freshness on exception would silently
    unmask the failure and is caught here).
    """
    from openstudio_operator import metrics as _metrics

    _reset_freshness_gauges()
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    tracker = StallWindowTracker()
    wbm_module.reset_leg2_safeguard_state()

    # Good tick: both freshness gauges advance to ~NOW.
    good_redis = make_redis(NOW, simulations=2)
    tick(api, apps, pods, now=NOW, tracker=tracker, redis=good_redis)
    queue_fresh_before = queue_depth_fresh()
    stall_fresh_before = stall_window_fresh()
    assert queue_fresh_before > 0.0
    assert stall_fresh_before > 0.0
    # Note: in the good tick the condition doesn't hold (heartbeats are
    # missing but the worker registry is empty — leg A holds but leg B
    # holds vacuously; the stall will start to accumulate on the next
    # tick). We don't depend on the data gauge here — only on the
    # freshness stamps being non-zero after a successful tick.

    # Allow some real time to pass so ``time.time()`` strictly advances.
    _time.sleep(0.05)

    # Flaky tick: the sensing raises — both freshness gauges must stay
    # pinned at their pre-tick values.
    class FlakyRedis:
        def worker_heartbeats(self) -> dict[str, float | None]:
            raise RedisClientError("SMEMBERS failed: connection reset")

        def queue_depths(self) -> dict[str, int]:
            raise RedisClientError("LLEN failed: connection reset")

    with pytest.raises(RedisClientError):
        tick(
            api,
            apps,
            pods,
            now=NOW + minute(1),
            tracker=tracker,
            redis=FlakyRedis(),
        )

    assert queue_depth_fresh() == queue_fresh_before
    assert stall_window_fresh() == stall_fresh_before
    # Belt-and-braces: the gauges' internal values are also untouched.
    assert _metrics.RESQUE_QUEUE_DEPTH_FRESH._value.get() == queue_fresh_before
    assert _metrics.STALL_WINDOW_FRESH._value.get() == stall_fresh_before


# --- dryRun (D11) ----------------------------------------------------------------


def test_dry_run_suppresses_patch_marks_event_and_advances_anchor():
    spec = {**SPEC, "dryRun": True}
    api = FakeCustomObjectsApi(make_cr(spec))
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()
    metric_before = restarts_total()

    fired1, events1 = tick(api, apps, spec=spec, now=NOW, tracker=tracker, redis=stall_redis(NOW))
    fired2, events2 = tick(
        api,
        apps,
        spec=spec,
        now=NOW + minute(10),
        tracker=tracker,
        redis=stall_redis(NOW + minute(10)),
    )
    fired3, events3 = tick(
        api,
        apps,
        spec=spec,
        now=NOW + minute(12),
        tracker=tracker,
        redis=stall_redis(NOW + minute(12)),
    )

    assert fired1 is False
    assert fired2 is True
    assert fired3 is False  # anchor advanced in dry-run: paced like a real run
    assert apps.patches == []  # mutation suppressed
    assert events1 == [] and events3 == []
    assert len(events2) == 1
    event_type, reason, message = events2[0]
    assert event_type == "Warning"
    assert reason == WEB_BACKGROUND_RESTARTED_EVENT
    assert "spec.dryRun" in message and "suppressed" in message
    assert restarts_total() - metric_before == 1
    assert api.obj["status"]["lastWebBackgroundRestart"] == (NOW + minute(10)).isoformat()


# --- StallWindowTracker unit semantics -------------------------------------------


def test_tracker_observes_resets_and_sustains():
    tracker = StallWindowTracker()
    window = minute(10)

    assert tracker.observe(False, NOW, window) is False
    assert tracker.first_observed is None

    assert tracker.observe(True, NOW, window) is False  # first observation
    assert tracker.first_observed == NOW
    assert tracker.observe(True, NOW + minute(9), window) is False
    assert tracker.observe(True, NOW + minute(10), window) is True  # >= window sustained

    tracker.reset()
    assert tracker.first_observed is None
    assert tracker.observe(True, NOW + minute(30), window) is False  # fresh clock


def test_stall_tracker_documents_singleton_guard_invariant() -> None:
    """Issue #167: the ``(namespace, name)`` key invariant must be stated.

    The module-level :data:`_tracker_cache` keys per-CR sustained-window
    clocks on ``(namespace, name)``. The D05 singleton guard
    (``openstudio_operator.singleton``) ensures only ONE OSCM CR per
    namespace, so the tuple uniquely identifies the active CR. The
    invariant is correct but, until this issue, was unstated in the
    code — a future maintainer who bypassed the guard (e.g. for a
    canary deploy) would discover the cache silently shares state
    between the two CRs.

    This test is a regression fence: it asserts the docstring on
    :meth:`StallWindowTracker.__init__` carries the invariant AND names
    the test that fails loudly when the singleton guard is bypassed.
    If a future refactor drops or rewrites the docstring, this test
    fails and forces the author to consciously preserve the invariant.
    """
    doc = StallWindowTracker.__init__.__doc__ or ""
    assert "singleton guard" in doc, (
        "StallWindowTracker.__init__ docstring must mention the singleton "
        "guard — see issue #167 and the key invariant on "
        "(namespace, name) in handlers/web_background_monitor.py."
    )
    assert "(namespace, name)" in doc, (
        "StallWindowTracker.__init__ docstring must state the (namespace, "
        "name) keying scheme that the singleton guard makes unique — "
        "see issue #167."
    )
    assert "tests/test_singleton_registry_coverage.py" in doc, (
        "StallWindowTracker.__init__ docstring must reference the "
        "singleton-guard test of record (tests/test_singleton_registry_"
        "coverage.py) — issue #167."
    )


def test_get_tracker_warns_when_singleton_guard_is_bypassed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #167: when the D05 singleton guard is bypassed, ``_get_tracker`` warns.

    Two OSCM CRs in the same namespace is the bug the singleton guard
    prevents. If a maintainer adds a second CR (e.g. canary A/B) the
    tracker cache would silently share state — ``_get_tracker`` must
    surface this with a Warning log so the behavior is visible, not
    silent. We don't raise because the canary use case is legitimate.
    """
    import logging

    # Fresh cache: build two distinct keys for the SAME namespace.
    wbm_module._tracker_cache.clear()
    try:
        caplog.set_level(logging.WARNING, logger=wbm_module.logger.name)
        first = wbm_module._get_tracker("ns-shared", "oscm-a")
        # First call must not warn — there is no prior tracker for this ns.
        assert "ns-shared" not in caplog.text
        # Second call with a DIFFERENT name in the SAME namespace: warn.
        second = wbm_module._get_tracker("ns-shared", "oscm-b")
        assert "singleton guard" in caplog.text
        assert "oscm-a" in caplog.text and "oscm-b" in caplog.text
        # The cache now holds both — we share state, we don't raise.
        assert len(wbm_module._tracker_cache) == 2
        assert first is not second
    finally:
        wbm_module._tracker_cache.clear()


def test_delete_recreate_starts_fresh_stall_window() -> None:
    """Issue #497: a delete+recreated singleton CR (#364) starts a fresh window.

    CR A (uid-a) observes the stall holding for 9 of the 10 required
    minutes, then the CR is deleted and recreated under the SAME
    ``(namespace, name)`` with a new uid. The pre-#497 leak: the
    (namespace, name)-keyed tracker survived the delete, so the recreated
    CR's next holding tick satisfied CR A's window and restarted
    web_background early — NOT the conservative fresh-observation
    semantics D07 promises. The #497 uid validation must discard the
    stale tracker and force the new CR to earn its own full window.
    """
    wbm_module._tracker_cache.clear()
    try:
        api = FakeCustomObjectsApi(make_cr())
        apps = FakeAppsV1Api()
        pods = FakeCoreV1Api([make_pod(), make_pod()])
        metric_before = restarts_total()

        # CR A (uid-a): stall holds at t=0 and t=9 — window not yet sustained.
        for offset in (0, 9):
            fired, events = tick(
                api,
                apps,
                pods,
                now=NOW + minute(offset),
                tracker=wbm_module._get_tracker(NAMESPACE, NAME, uid="uid-a"),
                redis=stall_redis(NOW + minute(offset)),
            )
            assert fired is False
            assert events == []
        assert apps.patches == []
        assert (
            wbm_module._get_tracker(NAMESPACE, NAME, uid="uid-a").first_observed == NOW
        )

        # Delete + recreate (#364): same (namespace, name), NEW uid — the
        # uid mismatch must discard CR A's 9-minute accumulation. The
        # recreate also DESTROYS ``.status`` (so CR A's #582
        # ``stallWindowStartedAt`` anchor dies with the CR): CR B ticks
        # against a fresh CR object, exactly what the apiserver serves
        # after a delete+recreate.
        tracker_b = wbm_module._get_tracker(NAMESPACE, NAME, uid="uid-b")
        assert tracker_b.first_observed is None  # fresh clock — no leak
        api_b = FakeCustomObjectsApi(make_cr())

        # The recreated CR's first holding tick (t=10 — the timestamp that
        # WOULD have satisfied CR A's leaked window) must NOT restart.
        fired, events = tick(
            api_b,
            apps,
            pods,
            now=NOW + minute(10),
            tracker=tracker_b,
            redis=stall_redis(NOW + minute(10)),
        )
        assert fired is False
        assert events == []
        assert apps.patches == []
        assert restarts_total() - metric_before == 0

        # The recreated CR earns its OWN window from t=10 → sustained at t=20.
        fired, events = tick(
            api_b,
            apps,
            pods,
            now=NOW + minute(20),
            tracker=tracker_b,
            redis=stall_redis(NOW + minute(20)),
        )
        assert fired is True
        assert len(events) == 1 and events[0][1] == WEB_BACKGROUND_RESTARTED_EVENT
        assert len(apps.patches) == 1
        assert restarts_total() - metric_before == 1
    finally:
        wbm_module._tracker_cache.clear()


def test_reset_per_cr_caches_seam_drops_scoped_and_all() -> None:
    """Issue #497 reset seam: one-CR drop, full drop, and the ValueError fence."""
    wbm_module._tracker_cache.clear()
    try:
        kept_key = ("other-ns", "other")
        wbm_module._get_tracker(NAMESPACE, NAME, uid="uid-a")
        wbm_module._get_tracker(*kept_key, uid="uid-x")

        wbm_module.reset_per_cr_caches(NAMESPACE, NAME)
        assert (NAMESPACE, NAME) not in wbm_module._tracker_cache
        assert kept_key in wbm_module._tracker_cache

        wbm_module.reset_per_cr_caches()
        assert wbm_module._tracker_cache == {}

        with pytest.raises(ValueError):
            wbm_module.reset_per_cr_caches(namespace=NAMESPACE)
        with pytest.raises(ValueError):
            wbm_module.reset_per_cr_caches(name=NAME)
    finally:
        wbm_module._tracker_cache.clear()


# --- Issue #44 leg-2 non-vacuity safeguard -------------------------------------


def workers_seen_max() -> float:
    return REGISTRY.get_sample_value("openstudio_operator_resque_workers_seen_max") or 0.0


def test_empty_registry_with_no_prior_heartbeats_warns_once_after_grace():
    """Issue #44: empty registry + never-seen-worker + sustained wait → warning.

    Reproduces the silent-misbehavior signature: the stall condition's
    "nobody is processing" leg is vacuously true. The safeguard must emit
    exactly one Warning Event named ResqueKeyLayoutUnknown after the
    60-second grace, AND keep the gauge at 0 (so an SRE can alert on it).
    """
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    tracker = StallWindowTracker()
    wbm_module.reset_leg2_safeguard_state()
    baseline_gauge = workers_seen_max()
    metric_before = restarts_total()

    # Two ticks under 60 s: empty registry seen, but no warning yet.
    for offset in (0, 30):
        _fired, events = tick(
            api,
            apps,
            pods,
            now=NOW + timedelta(seconds=offset),
            tracker=tracker,
            redis=make_redis(NOW + timedelta(seconds=offset), simulations=1),
        )
        assert _fired is False  # leg B vacuously holds but tracker is fresh
        assert events == []
    assert RESQUE_KEY_LAYOUT_UNKNOWN_EVENT not in [e[1] for e in events]

    # At 60 s the empty registry has been held for the full grace period.
    fired, events = tick(
        api,
        apps,
        pods,
        now=NOW + timedelta(seconds=60),
        tracker=tracker,
        redis=make_redis(NOW + timedelta(seconds=60), simulations=1),
    )
    assert fired is False  # tracker.observe(... 60s) hasn't crossed 10m window
    assert len(events) == 1
    event_type, reason, message = events[0]
    assert event_type == "Warning"
    assert reason == RESQUE_KEY_LAYOUT_UNKNOWN_EVENT
    assert "WORKER_REGISTRY_KEY" in message
    assert "docs/kind-validation.md" in message
    assert workers_seen_max() == baseline_gauge  # still 0 — never saw a worker

    # Subsequent ticks re-firing the empty registry do NOT re-emit the event
    # (one-shot per process).
    fired2, events2 = tick(
        api,
        apps,
        pods,
        now=NOW + timedelta(seconds=120),
        tracker=tracker,
        redis=make_redis(NOW + timedelta(seconds=120), simulations=1),
    )
    assert fired2 is False
    assert events2 == []
    assert restarts_total() - metric_before == 0  # safeguard is diagnostic only


def test_resque_layout_warning_message_names_current_worker_registry_key(monkeypatch):
    """Issue #594: the ResqueKeyLayoutUnknown Warning must name the CURRENT
    ``WORKER_REGISTRY_KEY`` value — the message is interpolated from the
    ``redis_client`` constant, never a string literal, so an SRE following
    the runbook verifies with redis-cli against the key the operator
    actually reads. If the registry key is ever corrected again (the #66
    live validation moved worker heartbeats once already), the message
    tracks it instead of silently naming a dead key.
    """
    wbm_module.reset_leg2_safeguard_state()
    # Grace window opened 61 s ago (past the 60 s grace), never saw a worker.
    monkeypatch.setattr(wbm_module, "_empty_registry_since", NOW - timedelta(seconds=61))
    events, emit = make_emit()

    wbm_module._maybe_warn_resque_layout_unknown(
        now=NOW, emit=emit, logger=_logging.getLogger(__name__)
    )

    assert len(events) == 1
    event_type, reason, message = events[0]
    assert event_type == "Warning"
    assert reason == RESQUE_KEY_LAYOUT_UNKNOWN_EVENT
    assert "WORKER_REGISTRY_KEY" in message
    assert WORKER_REGISTRY_KEY in message


def test_warning_does_not_fire_when_heartbeats_ever_observed():
    """If a heartbeat appears at any point, the safeguard clears the grace."""
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    tracker = StallWindowTracker()
    wbm_module.reset_leg2_safeguard_state()

    # Tick 0: one worker heartbeat present.
    epoch_0 = NOW.timestamp()
    tick(
        api,
        apps,
        pods,
        now=NOW,
        tracker=tracker,
        redis=make_redis(NOW, simulations=1, heartbeats={"w1": epoch_0 - 1}),
    )
    assert workers_seen_max() == 1.0

    # 90 s later: registry empty, but we saw a worker earlier — grace never
    # accumulates. NO warning.
    fired, events = tick(
        api,
        apps,
        pods,
        now=NOW + timedelta(seconds=90),
        tracker=tracker,
        redis=make_redis(NOW + timedelta(seconds=90), simulations=1),
    )
    assert fired is False
    assert events == []
    assert RESQUE_KEY_LAYOUT_UNKNOWN_EVENT not in [e[1] for e in events]


def test_warning_does_not_fire_during_grace_period():
    """Empty registry seen for less than the grace period → no warning."""
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    tracker = StallWindowTracker()
    wbm_module.reset_leg2_safeguard_state()

    # Only 30 s of empty registry.
    for offset in (0, 30):
        _fired, events = tick(
            api,
            apps,
            pods,
            now=NOW + timedelta(seconds=offset),
            tracker=tracker,
            redis=make_redis(NOW + timedelta(seconds=offset), simulations=1),
        )
        assert events == []
    assert workers_seen_max() == 0.0


def test_gauge_tracks_high_water_mark_of_workers_seen():
    """The gauge is monotonic: once workers seen, never drops."""
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    tracker = StallWindowTracker()
    wbm_module.reset_leg2_safeguard_state()

    epoch = NOW.timestamp()
    # Three workers present.
    tick(
        api,
        apps,
        pods,
        now=NOW,
        tracker=tracker,
        redis=make_redis(
            NOW,
            simulations=1,
            heartbeats={"w1": epoch - 1, "w2": epoch - 2, "w3": epoch - 3},
        ),
    )
    assert workers_seen_max() == 3.0

    # Now only one — gauge stays at 3 (high-water mark).
    tick(
        api,
        apps,
        pods,
        now=NOW + minute(1),
        tracker=tracker,
        redis=make_redis(NOW + minute(1), simulations=1, heartbeats={"w1": epoch}),
    )
    assert workers_seen_max() == 3.0

    # Empty — gauge stays at 3.
    tick(
        api,
        apps,
        pods,
        now=NOW + minute(2),
        tracker=tracker,
        redis=make_redis(NOW + minute(2), simulations=1),
    )
    assert workers_seen_max() == 3.0


# --- Issue #87: gauge emitted unconditionally, every sensing tick -----------------


def test_gauge_populates_on_idle_fleet_empty_queues():
    """#87: empty queues + workers heartbeating → gauge reads the worker
    count within ONE tick (the #66 live shape: 4 workers, zero jobs —
    previously scraped an ambiguous 0.0)."""
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    tracker = StallWindowTracker()
    wbm_module.reset_leg2_safeguard_state()

    epoch = NOW.timestamp()
    fired, events = tick(
        api,
        apps,
        pods,
        now=NOW,
        tracker=tracker,
        redis=make_redis(
            NOW,
            simulations=0,
            requeued=0,
            heartbeats={"w1": epoch - 5, "w2": epoch - 10, "w3": epoch - 15, "w4": epoch - 20},
        ),
    )

    assert fired is False
    assert events == []
    assert workers_seen_max() == 4.0  # idle fleet, non-zero within one poll
    # Idle sensing observes workers but never corroborates pods (leg A's
    # fail-fast returns before leg C's Deployment/pod reads).
    assert apps.reads == []
    assert apps.patches == []


def test_gauge_correct_with_workers_and_backlog():
    """#87: busy fleet — workers present + backlog → still the worker count
    (the pre-#87 queue-conditional behavior is preserved under load)."""
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    tracker = StallWindowTracker()
    wbm_module.reset_leg2_safeguard_state()

    epoch = NOW.timestamp()
    fired, events = tick(
        api,
        apps,
        pods,
        now=NOW,
        tracker=tracker,
        redis=make_redis(
            NOW,
            simulations=11,  # the #84 live shape: 11 dps queued
            heartbeats={"w1": epoch - 5, "w2": epoch - 10, "w3": epoch - 15, "w4": epoch - 20},
        ),
    )

    assert fired is False  # fresh heartbeats: leg B broken, no stall
    assert events == []
    assert workers_seen_max() == 4.0


def test_gauge_untouched_when_redis_unreachable():
    """#87: Redis unreachable on the worker read → the tick raises (D12
    skip) and the gauge is untouched — the scrape-error path is unchanged."""
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    tracker = StallWindowTracker()
    wbm_module.reset_leg2_safeguard_state()

    # A good idle tick first, so the gauge holds a non-zero high-water mark.
    epoch = NOW.timestamp()
    tick(
        api,
        apps,
        pods,
        now=NOW,
        tracker=tracker,
        redis=make_redis(NOW, heartbeats={"w1": epoch - 5, "w2": epoch - 5, "w3": epoch - 5}),
    )
    assert workers_seen_max() == 3.0
    tracker.first_observed = NOW  # prove the blind-gap reset too

    class UnreachableRedis:
        def worker_heartbeats(self) -> dict[str, float | None]:
            raise RedisClientError("SMEMBERS failed: connection refused")

    with pytest.raises(RedisClientError):
        tick(api, apps, pods, now=NOW + minute(1), tracker=tracker, redis=UnreachableRedis())

    assert workers_seen_max() == 3.0  # untouched by the failed tick
    assert tracker.first_observed is None  # blind gap restarted the window


def test_gauge_observable_via_metrics_endpoint():
    """The gauge is exported via /metrics (issue #17 contract).

    The full exposition-format check lives in
    ``tests/test_metrics_endpoint.py::test_every_declared_counter_family_in_registry_exposition``
    (now extended for the gauge). This test only asserts the gauge is
    settable and readable from the operator process — i.e. the safeguard
    machinery can drive it.
    """
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    tracker = StallWindowTracker()
    wbm_module.reset_leg2_safeguard_state()
    epoch = NOW.timestamp()
    tick(
        api,
        apps,
        pods,
        now=NOW,
        tracker=tracker,
        redis=make_redis(NOW, simulations=1, heartbeats={"w1": epoch}),
    )
    assert workers_seen_max() == 1.0


def test_warning_does_not_fire_when_idle_no_queue_work():
    """No leg-A (queue empty) → no warning, even with empty registry forever.

    The safeguard is gated on the same load condition as the stall
    evaluation — false-firing on an idle cluster would be noise.
    """
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    tracker = StallWindowTracker()
    wbm_module.reset_leg2_safeguard_state()

    # 5 minutes of empty registry, zero queue depth — leg A never holds.
    for offset in (0, 60, 120, 300):
        _fired, events = tick(
            api,
            apps,
            pods,
            now=NOW + timedelta(seconds=offset),
            tracker=tracker,
            redis=make_redis(NOW + timedelta(seconds=offset)),
        )
        assert events == []
    assert RESQUE_KEY_LAYOUT_UNKNOWN_EVENT not in [e[1] for e in events]


# --- Issue #232: kopf 1.4x MappingView body end-to-end through EventEmitter -----


def test_run_stall_tick_accepts_mappingview_body_in_event_emitter():
    """Issue #232 — MappingView (types.MappingProxyType) body end-to-end.

    kopf >=1.4x delivers ``body`` to timer handlers as a MappingView subclass
    (not a dict subclass). This regression test wires
    ``types.MappingProxyType`` into ``EventEmitter``, drives
    ``run_stall_tick`` through its restart emit path (sustained stall window
    on a queue with stale heartbeats and a healthy worker fleet), and asserts:

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
    pods = FakeCoreV1Api([make_pod(), make_pod()])
    tracker = StallWindowTracker()
    wbm_module.reset_leg2_safeguard_state()

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
    redis = stall_redis(NOW)

    # Two ticks: the first establishes the sustained window; the second fires
    # the restart Warning Event (the only emit path under full stall).
    fired1 = run_stall_tick(
        redis,
        store,
        config,
        apps,
        pods,
        namespace=NAMESPACE,
        now=NOW,
        emit=emitter,
        tracker=tracker,
    )
    fired2 = run_stall_tick(
        stall_redis(NOW + minute(10)),
        store,
        config,
        apps,
        pods,
        namespace=NAMESPACE,
        now=NOW + minute(10),
        emit=emitter,
        tracker=tracker,
    )

    assert fired1 is False  # window accumulating, not yet sustained
    assert fired2 is True   # window sustained at 10m, restart fires
    assert apps.patches == []  # mutation suppressed
    # Dry-run gate still records exactly the one restart Warning Event.
    assert emitter.suppressed_count == 1
    assert emitter.dry_run is True


# --- Issue #490 — periodic key-layout revalidation riding the stall tick -------
#
# The #163 key-layout check behind the @kopf.on.event watch only re-runs on
# OSCM watch events (boot listing + CR edits); a steady-state cluster
# generates none. The #490 rider re-runs the check from this module's timer
# tick at most once per REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL (5 min,
# _constants.py), stamping the paired freshness gauge through the existing
# handlers code path. The tests below pin: the first-tick fire, the
# interval gate (skip within, fire at/after), the reset seam, and the
# wiring — the kopf handler's tick closure carries the rider BEFORE the
# stall evaluation so the restart cooldown cannot stall revalidation.


class _CheckRecorder:
    """Stand-in for ``handlers._check_redis_key_layout_for_cr`` (#490 tests)."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    def __call__(self, item, *, logger):  # test-double signature: mirrors the check
        self.calls.append((item, logger.name))
        return "ok"


def _record_check(monkeypatch: pytest.MonkeyPatch) -> _CheckRecorder:
    recorder = _CheckRecorder()
    monkeypatch.setattr(
        "openstudio_operator.handlers.web_background_monitor"
        "._check_redis_key_layout_for_cr",
        recorder,
    )
    return recorder


def test_key_layout_revalidation_fires_on_first_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh process (``None`` clock) revalidates on the very first tick.

    That first fire initializes the freshness gauge promptly after boot and
    bounds the drift window from process start — the boot-time watch check
    and this rider agree within one timer interval.
    """
    _reset_revalidation()
    recorder = _record_check(monkeypatch)
    body = make_cr()

    _revalidate(body, logger=_logging.getLogger("test"), now=NOW)

    assert len(recorder.calls) == 1, (
        f"Expected the first tick to run the key-layout check; got "
        f"{len(recorder.calls)} calls. See issue #490."
    )
    assert recorder.calls[0][0] is body, "the check must receive the CR body"
    assert _wbm._last_key_layout_revalidation == NOW


def test_key_layout_revalidation_skips_within_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticks inside REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL do not re-run the check.

    The rider must not turn the 60 s stall cadence into a 60 s SCAN cadence
    — the interval gate is the cost bound (per-run cost capped by
    VALIDATE_SCAN_KEY_BUDGET, but still a Redis round-trip set).
    """
    _reset_revalidation()
    recorder = _record_check(monkeypatch)
    body = make_cr()

    _revalidate(body, logger=_logging.getLogger("test"), now=NOW)
    assert len(recorder.calls) == 1

    # 4 minutes later — inside the 5-minute interval: no second run.
    _revalidate(body, logger=_logging.getLogger("test"), now=NOW + minute(4))
    assert len(recorder.calls) == 1, (
        "Revalidation fired inside the interval — the cadence gate is "
        "broken (a SCAN storm against Redis). See issue #490."
    )


def test_key_layout_revalidation_fires_again_at_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At exactly the interval elapsed the gate re-opens (boundary is >=).

    The staleness alert thresholds at ``2 * interval``, so an exact-interval
    re-fire keeps the alert arithmetic honest: the gap can never reach 2x
    while the rider is alive.
    """
    _reset_revalidation()
    recorder = _record_check(monkeypatch)
    body = make_cr()

    _revalidate(body, logger=_logging.getLogger("test"), now=NOW - _REVALIDATION_INTERVAL)
    assert len(recorder.calls) == 1
    _revalidate(body, logger=_logging.getLogger("test"), now=NOW)
    assert len(recorder.calls) == 2, (
        "Revalidation did not re-fire at exactly the interval elapsed — "
        "the gate must be `< interval` (fire on >=). See issue #490."
    )


def test_reset_key_layout_revalidation_state_clears_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test seam restores the fresh-process posture (clock ``None``)."""
    _reset_revalidation()
    recorder = _record_check(monkeypatch)
    body = make_cr()

    _revalidate(body, logger=_logging.getLogger("test"), now=NOW)
    assert _wbm._last_key_layout_revalidation == NOW
    _reset_revalidation()
    assert _wbm._last_key_layout_revalidation is None

    # And the next invocation runs unconditionally again.
    _revalidate(body, logger=_logging.getLogger("test"), now=NOW + timedelta(seconds=1))
    assert len(recorder.calls) == 2


def test_web_background_timer_tick_carries_key_layout_revalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kopf handler's tick closure runs the rider BEFORE the stall tick.

    Wiring-level acceptance for #490: invoking the real
    ``web_background_monitor`` timer (run_stall_tick stubbed to a no-op so
    only the wrapper + rider execute) runs the key-layout check on the
    first tick, skips it on an immediate second tick (interval gate), and
    re-runs it once the clock is rewound past the interval. The rider also
    precedes the stall evaluation, so the restart cooldown inside
    run_stall_tick cannot stall revalidation.
    """
    _reset_revalidation()
    recorder = _record_check(monkeypatch)
    stall_calls: list[dict] = []
    monkeypatch.setattr(
        _wbm, "run_stall_tick", lambda *args, **kwargs: stall_calls.append(kwargs) or False
    )
    # StatusStore construction needs a CustomObjectsApi; the stall tick is
    # stubbed so a sentinel never gets used (the test_timer_wrapper_failures
    # pattern).
    monkeypatch.setattr(_wbm, "operator_custom_objects_api", lambda: object())
    # #567 — the wire closure now passes secret_ref=/namespace= kwargs
    # (secretRef-aware factory path); the stub absorbs them.
    monkeypatch.setattr(
        _wbm, "get_read_only_redis_client", lambda redis_url, **_kwargs: object()
    )

    spec = {**SPEC, "dryRun": True}
    body = make_cr(spec)

    def invoke() -> None:
        _wbm.web_background_monitor(
            body=body,
            spec=spec,
            namespace=NAMESPACE,
            name=NAME,
            logger=_logging.getLogger("test"),
        )

    invoke()
    assert len(recorder.calls) == 1, (
        "The timer tick did not run the key-layout revalidation — the #490 "
        "rider is not wired into the tick closure."
    )
    assert len(stall_calls) == 1, "the stall evaluation must still run after the rider"

    # Immediate second tick: interval gate holds.
    invoke()
    assert len(recorder.calls) == 1

    # Rewind the clock past the interval (against the REAL wall clock the
    # wrapper stamps — run_oscm_tick uses datetime.now(UTC), not this
    # module's pinned NOW): the rider fires again.
    _wbm._last_key_layout_revalidation = (
        datetime.now(UTC) - _REVALIDATION_INTERVAL - timedelta(seconds=1)
    )
    invoke()
    assert len(recorder.calls) == 2
    assert len(stall_calls) == 3
