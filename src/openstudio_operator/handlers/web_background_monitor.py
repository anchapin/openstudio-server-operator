"""Module 5 (plan Phase 2): web_background stall detector + cooldown restart (issue #13, D07).

The plan's original signals — ``GET /cluster.json`` and MongoDB queue
inspection — are dead ends (``/cluster.json`` does not exist in v3.11.0 and
``/compute_nodes.json`` is unpopulated on K8s). The real queue fabric is
Redis (Service ``queue`` :6379, Resque queues ``simulations`` +
``requeued``), read STRICTLY read-only through
:class:`~openstudio_operator.redis_client.ReadOnlyRedisClient` (issue #12).

Stall condition — all three legs simultaneously (D07):

* A ``work queued`` — ``LLEN simulations > 0`` OR ``LLEN requeued > 0``;
* B ``nobody is processing`` — no FRESH Resque worker heartbeat within
  ``DEFAULT_WORKER_HEARTBEAT_STALE_SECONDS`` (judged by
  ``ReadOnlyRedisClient.stale_workers``, the client's blessed policy hook).
  A registered worker with a missing heartbeat key counts as stale; an
  EMPTY registry satisfies this leg vacuously (no worker is processing
  either — the sustained window absorbs the worker-startup transient);
* C ``Kubernetes says the fleet is fine`` — the worker Deployment (from
  ``spec.targetWorkerDeployment``, falling back to the helm-fixed name
  ``worker``) has at least one pod, and every pod is phase ``Running``
  with a ``Ready=True`` condition. The pod set is discovered via the
  Deployment's own ``spec.selector.matchLabels`` — never a hardcoded chart
  label guess. Unhealthy/absent pods mean K8s already knows something is
  wrong, so the "queue is lying" inference collapses and nothing fires.

A→B→C are checked cheapest-first with fail-fast, so a healthy cluster pays
two LLENs per tick and nothing else.

Sustained window — THE key design point: transient blips below
``webBackgroundPolicy.stallWindowMinutes`` NEVER trigger. The full
condition must be observed holding CONTINUOUSLY: a per-CR
:class:`StallWindowTracker` records the first tick that observed the whole
condition and clears on any tick that observed it broken. Restart-safety:
the tracker is in-memory (D04-clean cache, like the watchdog's
``exhausted_seen`` — presentation-ish state, no CR status map may be
abused for it), so an operator restart resets to fresh observation —
CONSERVATIVE: a restart can only delay a restart, never false-trigger one.
The window must also re-accumulate across sensing failures: a tick whose
Redis/K8s reads raise resets the tracker (a blind gap is no evidence of
continuity) before propagating, so the skip-and-retry (D12) starts the
window over.

Cooldown — the ACTION is rate-limited by the CR-anchored scalar
``status.lastWebBackgroundRestart`` (D04): the gate is the single decision
point, checked FIRST — before any Redis or Kubernetes read — and blocks
for one full stall window (``stallWindowMinutes`` is the only policy knob;
"never restart more than once per stall window"). Gated ticks return
without observing, so after a restart fires, a persisting stall must
re-sustain for a fresh full window once the gate re-opens: sustained
windows qualify the CONDITION, the cooldown qualifies the ACTION, and both
are the same length. A fresh operator process reading the persisted CR
honors a cooldown straddling its restart.

Action: restart the Deployment named by ``spec.targetWebBackgroundDeployment``
(empty falls back to the helm-chart-fixed name ``web-background``) via the
``kubectl.kubernetes.io/restartedAt`` pod-template annotation patch — the
same ``kubectl rollout restart`` mechanism as the worker recycler (#11),
with an explicit ``application/merge-patch+json`` content type (the
generated client defaults Deployment patches to json-patch). Pods are
NEVER deleted directly. A Warning Event ``WebBackgroundRestarted`` is
emitted, ``WEB_BACKGROUND_RESTARTS_TOTAL`` incremented, and only then is
``status.lastWebBackgroundRestart`` anchored — patch before anchor: if the
anchor write fails, the next tick re-issues one (harmless) extra rolling
restart, the same accepted race as #11/#8 (D12).

dryRun (D11): the Deployment patch is suppressed and the Warning Event is
dry-run-marked — but ``lastWebBackgroundRestart`` still advances. Same
deliberate choice as #11: a dry-run simulation paces exactly like a real
run (cooldown cadence observable, Event not re-emitted every tick), and
flipping ``spec.dryRun`` back to false changes only the mutation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

import kopf
from kubernetes.client import ApiException, AppsV1Api, CoreV1Api, CustomObjectsApi

from openstudio_operator.config import (
    DEFAULT_WORKER_HEARTBEAT_STALE_SECONDS,
    OperatorConfig,
)
from openstudio_operator.handlers.analysis_sla import EventEmitter
from openstudio_operator.metrics import WEB_BACKGROUND_RESTARTS_TOTAL
from openstudio_operator.redis_client import ReadOnlyRedisClient, RedisClientError
from openstudio_operator.status_store import (
    GROUP,
    MERGE_PATCH_CONTENT_TYPE,
    PLURAL,
    VERSION,
    StatusStore,
    StatusStoreError,
)

logger = logging.getLogger(__name__)

_SPEC = {"group": GROUP, "version": VERSION, "plural": PLURAL}

#: Tick cadence for detection (60 s, matching the scaffold's declared
#: cadence and the datapoint watchdog). Operator behavior, not cluster
#: policy — policy values live in the CRD spec/config (AGENTS.md).
POLL_INTERVAL_SECONDS = 60.0

#: Fallback when ``spec.targetWebBackgroundDeployment`` is empty: the helm
#: ``develop`` chart's fixed web_background Deployment name (AGENTS.md).
DEFAULT_WEB_BACKGROUND_DEPLOYMENT = "web-background"

#: Fallback when ``spec.targetWorkerDeployment`` is empty: the helm chart's
#: fixed worker Deployment name, whose pods are the liveness corroboration.
DEFAULT_WORKER_DEPLOYMENT = "worker"

#: Pod-template annotation driving the rolling restart — same key/values as
#: ``kubectl rollout restart``; only the value changing triggers a rollout.
RESTARTED_AT_ANNOTATION = "kubectl.kubernetes.io/restartedAt"

WEB_BACKGROUND_RESTARTED_EVENT = "WebBackgroundRestarted"

_RUNNING = "Running"

# Cache-only (D04): one Redis client session per redis URL, never operator
# state — mirrors the REST client caches of the sibling handlers.
_redis_client_cache: dict[str, ReadOnlyRedisClient] = {}


class WorkerDeploymentApi(Protocol):
    """Structural type of ``AppsV1Api`` as used here — tests fake exactly this."""

    def read_namespaced_deployment(self, name: str, namespace: str, **_: object) -> object: ...

    def patch_namespaced_deployment(
        self, name: str, namespace: str, body: dict, **_: object
    ) -> object: ...


class PodLister(Protocol):
    """Structural type of ``CoreV1Api`` as used here — tests fake exactly this."""

    def list_namespaced_pod(self, namespace: str, **_: object) -> object: ...


class StallWindowTracker:
    """In-memory sustained-window clock for one CR (conservative cache, D04).

    Records the first tick the full stall condition was observed and clears
    on any tick it was not (or on :meth:`reset`), so acting requires the
    condition to have been observed holding continuously for the whole
    window. An operator restart starts a fresh tracker: re-observation from
    scratch can only DELAY a restart, never false-trigger one. The action
    cooldown lives in the CR status instead and survives restarts.
    """

    def __init__(self) -> None:
        self.first_observed: datetime | None = None

    def reset(self) -> None:
        self.first_observed = None

    def observe(self, holds: bool, now: datetime, window: timedelta) -> bool:
        """Feed one tick's verdict; return whether the window is sustained."""
        if not holds:
            self.first_observed = None
            return False
        if self.first_observed is None:
            self.first_observed = now
        return now - self.first_observed >= window


_tracker_cache: dict[tuple[str, str], StallWindowTracker] = {}


def _get_tracker(namespace: str, name: str) -> StallWindowTracker:
    tracker = _tracker_cache.get((namespace, name))
    if tracker is None:
        tracker = StallWindowTracker()
        _tracker_cache[(namespace, name)] = tracker
    return tracker


def _get_redis_client(redis_url: str) -> ReadOnlyRedisClient:
    client = _redis_client_cache.get(redis_url)
    if client is None:
        client = ReadOnlyRedisClient(redis_url)
        _redis_client_cache[redis_url] = client
    return client


def _worker_pods_healthy(
    apps_api: WorkerDeploymentApi, pods_api: PodLister, *, namespace: str, deployment: str
) -> bool:
    """Leg C: the worker fleet looks fine to Kubernetes.

    Discovers pods via the worker Deployment's own selector (no hardcoded
    chart labels), then requires at least one pod, all ``Running``, all
    with ``Ready=True`` — read-only signals. Unreadable/absent fleet ⇒
    False (conservative: the "queue is lying" inference needs K8s vouching
    for the fleet). Raises ``ApiException`` so transport failures skip the
    tick (D12) instead of masquerading as an unhealthy fleet.
    """
    deployment_obj = apps_api.read_namespaced_deployment(deployment, namespace)
    match_labels = deployment_obj.spec.selector.match_labels
    if not match_labels:
        logger.warning(
            "worker Deployment %s/%s exposes no spec.selector.matchLabels — "
            "cannot corroborate pod health, stall condition leg C fails",
            namespace,
            deployment,
        )
        return False
    selector = ",".join(f"{key}={value}" for key, value in sorted(match_labels.items()))
    pods = pods_api.list_namespaced_pod(namespace, label_selector=selector).items or []
    if not pods:
        return False
    for pod in pods:
        status = pod.status
        if (status.phase or "") != _RUNNING:
            return False
        conditions = status.conditions or []
        if not any(c.type == "Ready" and c.status == "True" for c in conditions):
            return False
    return True


def _stall_condition_holds(
    redis_client: ReadOnlyRedisClient,
    apps_api: WorkerDeploymentApi,
    pods_api: PodLister,
    *,
    namespace: str,
    worker_deployment: str,
    stale_seconds: float,
) -> bool:
    """The full D07 condition, cheapest leg first with fail-fast. Pure read."""
    # Leg A: work is queued (either managed Resque queue).
    depths = redis_client.queue_depths()
    if not any(depth > 0 for depth in depths.values()):
        return False
    # Leg B: nobody is processing — every registered heartbeat is stale
    # (vacuously true for an empty registry: no worker is processing either).
    registered = redis_client.worker_heartbeats()
    stale = redis_client.stale_workers(threshold_seconds=stale_seconds)
    if len(stale) < len(registered):
        return False
    # Leg C: Kubernetes vouches for the worker fleet.
    return _worker_pods_healthy(
        apps_api, pods_api, namespace=namespace, deployment=worker_deployment
    )


def run_stall_tick(
    redis_client: ReadOnlyRedisClient,
    store: StatusStore,
    config: OperatorConfig,
    apps_api: WorkerDeploymentApi,
    pods_api: PodLister,
    *,
    namespace: str,
    now: datetime,
    emit: EventEmitter,
    tracker: StallWindowTracker,
) -> bool:
    """One stall evaluation. Returns whether a restart fired this tick.

    THE GATE is the single decision point and is checked first: while the
    ``status.lastWebBackgroundRestart`` cooldown (one stall window) holds,
    the tick returns before any Redis/Kubernetes read — nothing can bypass
    it. Raises on Redis/K8s/status-store failure so the caller skips the
    tick (D12); an unanchored restart is re-attempted next poll (the
    tracker only resets once the anchor is written), an anchored one never
    re-fires within the window.
    """
    window = timedelta(minutes=config.web_background_policy.stall_window_minutes)
    last_restart = store.get_last_web_background_restart_at()
    if last_restart is not None and now - last_restart <= window:
        return False  # gate closed: no sensing, no observation, no action

    try:
        holds = _stall_condition_holds(
            redis_client,
            apps_api,
            pods_api,
            namespace=namespace,
            worker_deployment=config.target_worker_deployment or DEFAULT_WORKER_DEPLOYMENT,
            stale_seconds=DEFAULT_WORKER_HEARTBEAT_STALE_SECONDS,
        )
    except (RedisClientError, ApiException):
        # Blind gap: a failed read is no evidence the condition held —
        # restart the window so skip-and-retry cannot stitch across it.
        tracker.reset()
        raise

    if not tracker.observe(holds, now, window):
        return False

    deployment = config.target_web_background_deployment or DEFAULT_WEB_BACKGROUND_DEPLOYMENT
    restart_value = now.astimezone(UTC).isoformat()
    patch_body = {
        "spec": {
            "template": {"metadata": {"annotations": {RESTARTED_AT_ANNOTATION: restart_value}}}
        }
    }
    dry_run = config.dry_run
    if not dry_run:
        # Explicit merge-patch content type: the generated client's default
        # selection for Deployment patches is json-patch (ops array), which a
        # dict body is not. RFC 7386 merge preserves sibling annotations.
        apps_api.patch_namespaced_deployment(
            deployment,
            namespace,
            body=patch_body,
            _content_type=MERGE_PATCH_CONTENT_TYPE,
        )
    message = (
        f"Queue stall sustained {int(window // timedelta(minutes=1))}m (work queued on "
        f"simulations/requeued, no fresh Resque worker heartbeat in "
        f"{DEFAULT_WORKER_HEARTBEAT_STALE_SECONDS:.0f}s, worker pods Running) — restarting "
        f"web_background Deployment {namespace}/{deployment} via {RESTARTED_AT_ANNOTATION} patch"
    )
    if dry_run:
        message += " — patch suppressed (spec.dryRun)"
    emit("Warning", WEB_BACKGROUND_RESTARTED_EVENT, message)
    WEB_BACKGROUND_RESTARTS_TOTAL.inc()
    # Advances in dry-run too — see module docstring (D11 pacing choice,
    # identical to #11). Patch before anchor: the accepted D12 re-attempt race.
    store.set_last_web_background_restart_at(now)
    # Only now is the action fully recorded — the NEXT restart must
    # re-sustain a fresh full window once the cooldown re-opens the gate.
    tracker.reset()
    return True


@kopf.timer(_SPEC["group"], _SPEC["version"], _SPEC["plural"], interval=POLL_INTERVAL_SECONDS)
def web_background_monitor(
    body: dict,
    spec: dict,
    namespace: str,
    name: str,
    logger: kopf.Logger,
    **_: object,
) -> None:
    """Timer thin wrapper: wire config/redis/store/apis/events, run one tick."""
    config = OperatorConfig.from_spec(spec)
    # Same idle posture as every sibling handler: an OSCM without serverUrl
    # is an incomplete CR, even though this monitor senses Redis + K8s only.
    if not config.server_url:
        logger.warning("spec.serverUrl is empty — web_background monitor idle this tick")
        return
    redis_client = _get_redis_client(config.redis_url)
    store = StatusStore(namespace, name, CustomObjectsApi())
    apps_api = AppsV1Api()
    pods_api = CoreV1Api()
    tracker = _get_tracker(namespace, name)

    def emit(event_type: str, reason: str, message: str) -> None:
        kopf.event(body, type=event_type, reason=reason, message=message)

    try:
        fired = run_stall_tick(
            redis_client,
            store,
            config,
            apps_api,
            pods_api,
            namespace=namespace,
            now=datetime.now(UTC),
            emit=emit,
            tracker=tracker,
        )
    except (RedisClientError, StatusStoreError, ApiException) as exc:
        logger.warning(
            "web_background monitor tick skipped, retrying next poll (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return
    if fired:
        logger.warning("web_background stall confirmed — Deployment restart issued")
