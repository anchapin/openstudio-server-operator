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

Worker observation (issue #87): every sensing tick FIRST reads the Resque
worker set (SMEMBERS + heartbeat HGETALL through ``worker_heartbeats()``)
and advances the monotonic ``resque_workers_seen_max`` gauge —
unconditionally, even when both queues are empty — so a healthy idle fleet
shows a non-zero gauge within one poll and ``0`` with a reachable Redis
unambiguously means "no workers registered". A Redis failure on that read
raises before any gauge write: the tick is skipped (D12) and the gauge is
untouched, exactly the pre-#87 scrape-error path. The legs are then checked
cheapest-first with fail-fast, so a healthy cluster pays the worker read
plus two LLENs per tick and nothing else.

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
from openstudio_operator.handlers.analysis_sla import (
    EventEmitter,
    deployment_label_selector,
)
from openstudio_operator.metrics import (
    HANDLER_TICK_FAILURES_TOTAL,
    RESQUE_WORKERS_SEEN_MAX,
    WEB_BACKGROUND_RESTARTS_TOTAL,
)
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

#: Tick cadence (issue #165). See :data:`openstudio_operator._constants.WEB_BACKGROUND_POLL_INTERVAL_SECONDS`
#: — operator behavior, not cluster policy; policy values live in the CRD
#: spec/config (AGENTS.md).
from openstudio_operator._constants import (
    LAYOUT_WARNING_GRACE_SECONDS,
    WEB_BACKGROUND_POLL_INTERVAL_SECONDS,
)

POLL_INTERVAL_SECONDS = WEB_BACKGROUND_POLL_INTERVAL_SECONDS

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

#: Issue #44 — Resque key-layout leg-2 non-vacuity safeguard. Emitted ONCE
#: per operator process when an empty worker registry has been observed for
#: at least :data:`_LAYOUT_WARNING_GRACE_SECONDS` without any worker
#: heartbeat ever being seen — the signature of the centralized Resque key
#: constants not matching the live v3.11.0 layout. The stall condition's
#: "nobody is processing" branch is then vacuously true (the dangerous
#: silent misbehavior #13 design note).
RESQUE_KEY_LAYOUT_UNKNOWN_EVENT = "ResqueKeyLayoutUnknown"

#: How long the operator tolerates an empty worker registry with no prior
#: heartbeat observation before concluding the Resque layout is wrong and
#: emitting :data:`RESQUE_KEY_LAYOUT_UNKNOWN_EVENT`. 60 s — comfortably
#: more than one Resque heartbeat (5 s) but short enough that a misconfig
#: surfaces well within the first stall window. See
#: :data:`openstudio_operator._constants.LAYOUT_WARNING_GRACE_SECONDS`.
_LAYOUT_WARNING_GRACE_SECONDS = LAYOUT_WARNING_GRACE_SECONDS

#: Module-level cache (D04-clean): one Redis client session per redis URL,
#: never operator state — mirrors the REST client caches of the sibling
#: handlers. Plus three process-lifetime flags for the leg-2 safeguard:
#: the high-water mark of distinct workers ever observed (drives the
#: monotonic gauge), the first tick the empty-registry state began (for
#: the grace-period check), and a one-shot warning emission flag.
_redis_client_cache: dict[str, ReadOnlyRedisClient] = {}
_max_workers_seen: int = 0
_empty_registry_since: datetime | None = None
_resque_layout_warning_emitted: bool = False


def reset_leg2_safeguard_state() -> None:
    """Test-only: clear the process-lifetime leg-2 safeguard state.

    Also resets the monotonic :data:`RESQUE_WORKERS_SEEN_MAX` gauge so each
    test starts at zero — the Gauge is process-level (Prometheus client
    module-level singleton) and would otherwise bleed across tests.
    """
    global _max_workers_seen, _empty_registry_since, _resque_layout_warning_emitted
    _max_workers_seen = 0
    _empty_registry_since = None
    _resque_layout_warning_emitted = False
    RESQUE_WORKERS_SEEN_MAX.set(0)


_RUNNING = "Running"

# Note: ``_redis_client_cache``, ``_max_workers_seen``,
# ``_empty_registry_since``, and ``_resque_layout_warning_emitted`` live at
# the top of this module alongside ``RESQUE_KEY_LAYOUT_UNKNOWN_EVENT`` —
# colocating the issue #44 safeguard state with the constants it gates
# keeps the leg-2 fix auditable in one place.


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
        """Construct a per-CR sustained-window clock.

        Singleton-guard key invariant (issue #167, D05):

        Per-namespace state keyed by ``(namespace, name)``. The D05
        singleton guard (``openstudio_operator.singleton``) ensures only
        ONE OSCM CR per namespace, so this tuple uniquely identifies the
        active CR. Adding a second CR (e.g. for canary testing of the
        operator's logic) would cause the tracker to silently share state
        between the two — this is a known limitation until the
        singleton-guard is relaxed.

        Test of record for the singleton-guard invariant:
        ``tests/test_singleton_registry_coverage.py``
        (``test_all_oscm_spawning_handlers_are_singleton_guarded`` —
        fails loudly if a new ``@kopf.timer``/``@kopf.daemon`` is added
        without going through :func:`singleton.install_singleton_guard`,
        which is what enforces the one-CR-per-namespace rule).
        """
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
# Keyed by ``(namespace, name)``. The D05 singleton guard (see
# ``openstudio_operator.singleton`` and the invariant captured in
# ``StallWindowTracker.__init__``'s docstring) ensures at most one OSCM
# CR per namespace, so this tuple uniquely identifies the active CR.
# See ``tests/test_singleton_registry_coverage.py`` for the test that
# fails loudly if the singleton guard is bypassed by a new handler.
# Issue #167.


def _get_tracker(namespace: str, name: str) -> StallWindowTracker:
    tracker = _tracker_cache.get((namespace, name))
    if tracker is None:
        # D05 invariant — surface (don't raise) if the singleton guard has
        # been bypassed. The tracker would otherwise silently share state
        # between the two CRs, which is the bug the invariant guards
        # against. We warn-log rather than raise so a canary deploy that
        # legitimately wants two CRs in one namespace (e.g. to A/B the
        # operator's logic) doesn't crash the operator — they get the
        # warning, we keep ticking. See
        # ``tests/test_singleton_registry_coverage.py`` for the upstream
        # invariant; issue #167.
        other_names = sorted(n for ns, n in _tracker_cache if ns == namespace)
        if other_names and other_names[0] != name:
            logger.warning(
                "StallWindowTracker cache already holds a tracker for "
                "namespace %s under name(s) %r — D05 singleton guard has "
                "been bypassed (a second OSCM CR is being serviced in this "
                "namespace). The new CR (%r) will share state with the "
                "existing one until the process restarts. See issue #167 "
                "and tests/test_singleton_registry_coverage.py.",
                namespace,
                other_names,
                name,
            )
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
    chart labels; honors both ``matchLabels`` and ``matchExpressions`` —
    issue #44), then requires at least one pod, all ``Running``, all
    with ``Ready=True`` — read-only signals. Unreadable/absent fleet ⇒
    False (conservative: the "queue is lying" inference needs K8s vouching
    for the fleet). Raises ``ApiException`` so transport failures skip the
    tick (D12) instead of masquerading as an unhealthy fleet.
    """
    selector = deployment_label_selector(apps_api, deployment, namespace)
    if not selector:
        logger.warning(
            "worker Deployment %s/%s exposes neither matchLabels nor "
            "supported matchExpressions — cannot corroborate pod health, "
            "stall condition leg C fails",
            namespace,
            deployment,
        )
        return False
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
    now: datetime,
) -> bool:
    """The full D07 condition, cheapest leg first with fail-fast. Pure read.

    Issue #87 — the worker set is read FIRST, before leg A, on every
    sensing tick: the ``RESQUE_WORKERS_SEEN_MAX`` gauge advances even when
    both queues are empty, so an idle-but-healthy fleet is visible within
    one poll. Pre-#87 the gauge only moved under backlog, making ``0.0``
    ambiguous between "Redis unreachable / no workers / wrong keys" and
    "idle" (the ambiguity that cost real debugging time in the #66/#67
    live sessions).

    The leg-2 safeguard (#44): tracks distinct workers observed in
    process lifetime and the first tick an empty registry began. If an
    empty registry persists for :data:`_LAYOUT_WARNING_GRACE_SECONDS` with
    no prior heartbeat observation, the empty-registry branch of leg B is
    vacuously true — the most dangerous silent misbehavior this operator
    has (per the issue #13 design note). The safeguard makes that visible:

    * ``RESQUE_WORKERS_SEEN_MAX`` (Prometheus Gauge, monotonic) stays at 0;
    * once the grace window elapses, a one-shot Warning Event
      :data:`RESQUE_KEY_LAYOUT_UNKNOWN_EVENT` is emitted with diagnostics
      naming the empty-registry observation and pointing at the runbook.

    The original "treat empty as nobody processing" semantics are
    PRESERVED — the safeguard is purely additive (observability +
    one-shot warning). If a real worker heartbeat appears at any point
    during the grace window, the timer resets and no warning fires.
    """
    global _max_workers_seen, _empty_registry_since
    # Issue #87 — unconditional worker observation: read the worker set on
    # EVERY tick that successfully reads Redis and advance the monotonic
    # gauge before any leg evaluation, so a healthy idle fleet (empty
    # queues, workers heartbeating) shows a non-zero gauge within one
    # poll. A Redis failure here raises BEFORE any gauge write — the tick
    # is skipped (D12) and the gauge stays untouched (scrape-error path
    # unchanged).
    registered = redis_client.worker_heartbeats()
    # Track the high-water mark of distinct workers seen (#44 safeguard).
    if len(registered) > _max_workers_seen:
        _max_workers_seen = len(registered)
        RESQUE_WORKERS_SEEN_MAX.set(_max_workers_seen)
    # Leg A: work is queued (either managed Resque queue).
    depths = redis_client.queue_depths()
    if not any(depth > 0 for depth in depths.values()):
        return False
    # Leg B: nobody is processing — every registered heartbeat is stale
    # (vacuously true for an empty registry: no worker is processing either).
    # The #44 grace tracking stays leg-A-gated: the layout warning must only
    # ever fire under real load, never on an idle empty fleet.
    if not registered:
        if _empty_registry_since is None:
            _empty_registry_since = now
        # An empty registry holds vacuously: the stall can still hold.
    else:
        # A worker appeared — clear the grace window so the safeguard
        # doesn't false-fire on a later transient empty period.
        _empty_registry_since = None
    stale = redis_client.stale_workers(threshold_seconds=stale_seconds)
    if len(stale) < len(registered):
        return False
    # Leg C: Kubernetes vouches for the worker fleet.
    return _worker_pods_healthy(
        apps_api, pods_api, namespace=namespace, deployment=worker_deployment
    )


def _maybe_warn_resque_layout_unknown(
    *, now: datetime, emit: EventEmitter, logger: logging.Logger
) -> None:
    """Emit :data:`RESQUE_KEY_LAYOUT_UNKNOWN_EVENT` once if the safeguard triggers.

    Trigger: empty registry has held for :data:`_LAYOUT_WARNING_GRACE_SECONDS`
    AND ``_max_workers_seen == 0`` (never saw a worker heartbeat at all in
    this process) AND the operator is in a real load (leg A held this tick
    — the warn is gated on that so it doesn't fire on an idle cluster).
    Once fired, never fires again in this process (gate flag below).
    """
    global _resque_layout_warning_emitted
    if _resque_layout_warning_emitted:
        return
    if _max_workers_seen > 0:
        return  # we've seen workers before — empty registry is a transient cold start
    if _empty_registry_since is None:
        return
    if now - _empty_registry_since < _LAYOUT_WARNING_GRACE_SECONDS:
        return
    _resque_layout_warning_emitted = True
    elapsed = int((now - _empty_registry_since).total_seconds())
    message = (
        f"Resque worker registry has been empty for {elapsed}s while a "
        f"non-zero queue depth is observed. The centralized Resque key "
        f"constants (WORKER_REGISTRY_KEY='resque:workers') may not match "
        f"the live v3.11.0 Redis layout; the stall condition's "
        f"'nobody is processing' leg is vacuously true and could periodic-"
        f"restart web_background. Run docs/kind-validation.md Resque layout "
        f"checklist (#44). RESQUE_WORKERS_SEEN_MAX is 0 — alert on "
        f"`openstudio_operator_resque_workers_seen_max == 0 AND "
        f"queue depth > 0`."
    )
    logger.warning("Resque key layout unknown: %s", message)
    emit("Warning", RESQUE_KEY_LAYOUT_UNKNOWN_EVENT, message)


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
            now=now,
        )
    except (RedisClientError, ApiException):
        # Blind gap: a failed read is no evidence the condition held —
        # restart the window so skip-and-retry cannot stitch across it.
        tracker.reset()
        raise

    # Issue #44 — leg-2 non-vacuity safeguard. Best-effort: a failure to
    # emit the warning must not skip the tick (it's diagnostic, not load-
    # bearing on the stall evaluation).
    try:
        _maybe_warn_resque_layout_unknown(now=now, emit=emit, logger=logger)
    except Exception as exc:  # noqa: BLE001 — defensive only (diagnostic emit)
        logger.debug("leg-2 safeguard emit failed: %s", exc)

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
        HANDLER_TICK_FAILURES_TOTAL.labels(
            module="web_background_monitor", error_type=type(exc).__name__
        ).inc()
        logger.warning(
            "web_background monitor tick skipped, retrying next poll (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return
    if fired:
        logger.warning("web_background stall confirmed — Deployment restart issued")
