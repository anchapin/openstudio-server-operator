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

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import fakeredis
import pytest
from prometheus_client import REGISTRY

from openstudio_operator.config import OperatorConfig
from openstudio_operator.handlers.web_background_monitor import (
    DEFAULT_WEB_BACKGROUND_DEPLOYMENT,
    MERGE_PATCH_CONTENT_TYPE,
    RESTARTED_AT_ANNOTATION,
    WEB_BACKGROUND_RESTARTED_EVENT,
    StallWindowTracker,
    run_stall_tick,
)
from openstudio_operator.redis_client import ReadOnlyRedisClient, RedisClientError
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

WORKER_SELECTOR = {"app.kubernetes.io/name": "openstudio-server", "component": "worker"}
SELECTOR_STRING = "app.kubernetes.io/name=openstudio-server,component=worker"


def minute(n: int) -> timedelta:
    return timedelta(minutes=n)


def make_cr(spec: dict | None = None, status: dict | None = None) -> dict:
    return {
        "apiVersion": "energy.nrel.gov/v1alpha1",
        "kind": "OpenStudioClusterManager",
        "metadata": {"name": NAME, "namespace": NAMESPACE},
        "spec": copy.deepcopy(spec if spec is not None else SPEC),
        "status": copy.deepcopy(status if status is not None else {}),
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


class FakeAppsV1Api:
    """Records Deployment patches; serves the worker selector for pod discovery."""

    def __init__(self, selector: dict | None = None) -> None:
        self.selector = dict(selector if selector is not None else WORKER_SELECTOR)
        self.patches: list[dict] = []
        self.reads: list[tuple[str, str]] = []

    def read_namespaced_deployment(self, name, namespace, **kwargs):
        self.reads.append((name, namespace))
        return SimpleNamespace(
            spec=SimpleNamespace(selector=SimpleNamespace(match_labels=dict(self.selector)))
        )

    def patch_namespaced_deployment(self, name, namespace, body, **kwargs):
        self.patches.append(
            {"name": name, "namespace": namespace, "body": copy.deepcopy(body), "kwargs": kwargs}
        )
        return {"metadata": {"name": name}}


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


def make_emit():
    events: list[tuple[str, str, str]] = []

    def emit(event_type: str, reason: str, message: str) -> None:
        events.append((event_type, reason, message))

    return events, emit


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
        fake.rpush("simulations", f"sim-job-{job}")
    for job in range(requeued):
        fake.rpush("requeued", f"req-job-{job}")
    for worker_id, heartbeat in (heartbeats or {}).items():
        fake.sadd("resque:workers", worker_id)
        if heartbeat is not None:
            fake.set(f"resque:workers:{worker_id}", str(heartbeat))
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
    """Anchor expired + fresh process: still no instant re-fire — the in-memory
    window reset is the documented conservative restart-safety choice."""
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


# --- Sensing failure: skip tick, restart the window (D12) ------------------------


def test_sensing_failure_raises_resets_tracker_and_is_retried():
    api = FakeCustomObjectsApi(make_cr())
    apps = FakeAppsV1Api()
    tracker = StallWindowTracker()

    tick(api, apps, now=NOW, tracker=tracker, redis=stall_redis(NOW))
    assert tracker.first_observed == NOW

    class FlakyRedis:
        """Leg A read fails — the kind of transient the wrapper must skip."""

        def queue_depths(self) -> dict[str, int]:
            raise RedisClientError("LLEN failed: connection reset")

    with pytest.raises(RedisClientError):
        tick(api, apps, now=NOW + minute(1), tracker=tracker, redis=FlakyRedis())

    # Blind gap: the window restarted, so the stall must re-sustain fully.
    assert tracker.first_observed is None
    stall_ticks(api, apps, tracker, 2, 6, 11, 12)
    assert len(apps.patches) == 1  # fired at +12 (first_observed = +2)
    assert api.obj["status"]["lastWebBackgroundRestart"] == (NOW + minute(12)).isoformat()


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


# --- Issue #44 leg-2 non-vacuity safeguard -------------------------------------


from openstudio_operator.handlers import web_background_monitor as wbm_module
from openstudio_operator.handlers.web_background_monitor import (
    RESQUE_KEY_LAYOUT_UNKNOWN_EVENT,
)


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
