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

Issue #490 — this timer is also the carrier for the periodic Redis
key-layout revalidation: every tick first offers
:func:`_maybe_revalidate_redis_key_layout` the chance to re-run
``handlers._check_redis_key_layout_for_cr`` (once per 5-minute
:data:`REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL`), because a steady-state
cluster generates no OSCM watch events and the #163 boot-time check would
otherwise never re-fire. The rider runs BEFORE the stall evaluation
(independent of the cooldown gate) and never raises, so the stall
semantics above are unchanged; its gauges/log/Warning-deferral are the
existing key-layout surface, now stamped on a cadence.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import kopf
from kubernetes.client import ApiException

from openstudio_operator import _cr_cache as cr_cache
from openstudio_operator._constants import (
    CRD_SPEC,
    LAYOUT_WARNING_GRACE_SECONDS,
    REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL,
    WEB_BACKGROUND_POLL_INTERVAL_SECONDS,
)
from openstudio_operator._k8s import (
    DEFAULT_WORKER_DEPLOYMENT,
    RESTARTED_AT_ANNOTATION,
    DeploymentManager,
    PodLister,
    deployment_label_selector,
    rolling_restart_deployment,
)
from openstudio_operator._oscm_handlers import (
    observe_tick_duration,
    run_oscm_tick,
)
from openstudio_operator._oscm_handlers import (
    register_fn as _register_oscm_handler,
)
from openstudio_operator.client_factory import get_read_only_redis_client
from openstudio_operator.config import (
    DEFAULT_WORKER_HEARTBEAT_STALE_SECONDS,
    OperatorConfig,
)
from openstudio_operator.events import EventEmitter
from openstudio_operator.metrics import (
    KUBE_API_REQUEST_DURATION_SECONDS,
    RESQUE_QUEUE_DEPTH,
    RESQUE_QUEUE_DEPTH_FRESH,
    RESQUE_WORKERS_SEEN_MAX,
    STALL_WINDOW_ELAPSED_SECONDS,
    STALL_WINDOW_FRESH,
    WEB_BACKGROUND_RESTARTS_TOTAL,
    observe_duration,
)
from openstudio_operator.redis_client import ReadOnlyRedisClient, RedisClientError
from openstudio_operator.singleton import (
    operator_apps_api,
    operator_core_api,
    operator_custom_objects_api,
)
from openstudio_operator.status_store import (
    MERGE_PATCH_CONTENT_TYPE,  # noqa: F401 — re-export: tests import it from this module
    StatusStore,
)

logger = logging.getLogger(__name__)

#: Tick cadence (issue #165). See :data:`openstudio_operator._constants.WEB_BACKGROUND_POLL_INTERVAL_SECONDS`
#: — operator behavior, not cluster policy; policy values live in the CRD
#: spec/config (AGENTS.md).
POLL_INTERVAL_SECONDS = WEB_BACKGROUND_POLL_INTERVAL_SECONDS

#: Fallback when ``spec.targetWebBackgroundDeployment`` is empty: the helm
#: ``develop`` chart's fixed web_background Deployment name (AGENTS.md).
DEFAULT_WEB_BACKGROUND_DEPLOYMENT = "web-background"

# Issue #395 — DEFAULT_WORKER_DEPLOYMENT and RESTARTED_AT_ANNOTATION were
# declared here AND in worker_recycler.py; both now live once in
# :mod:`openstudio_operator._k8s` (imported above) and are re-exported by
# this module so existing test imports keep resolving.

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

#: Process-lifetime flags for the leg-2 safeguard (issue #44): the
#: high-water mark of distinct workers ever observed (drives the monotonic
#: gauge), the first tick the empty-registry state began (for the
#: grace-period check), and a one-shot warning emission flag.
#:
#: Issue #497 census note: these are deliberately UN-keyed process-lifetime
#: state, NOT per-CR caches — they diagnose the Resque key layout (a
#: Redis-server property, not a CR property) and the layout warning is
#: one-shot PER PROCESS by design. :func:`reset_leg2_safeguard_state` is
#: the existing test seam; a CR delete+recreate must not re-arm a
#: process-level diagnostic.
#:
#: The Redis client cache that used to live here was retired in #235 — one
#: ``lru_cache`` in :mod:`openstudio_operator.client_factory` now serves
#: every callsite (this handler, the SLA monitor's escalation lookup and the
#: boot-time key-layout probe), mirroring what #168 did for the REST client.
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


#: Issue #490 — when this module last re-ran the Redis key-layout
#: validation (``handlers._check_redis_key_layout_for_cr``) from the stall
#: tick. Process-lifetime state, deliberately UN-keyed (the same #497
#: census rationale as the leg-2 flags above: the Resque key layout is a
#: Redis-server property, not a CR property, and the D05 singleton guard
#: means at most one CR's tick drives the cadence anyway).
#: :func:`reset_key_layout_revalidation_state` is the test seam.
_last_key_layout_revalidation: datetime | None = None


def reset_key_layout_revalidation_state() -> None:
    """Test-only: clear the process-level key-layout revalidation clock (#490).

    The next ``_maybe_revalidate_redis_key_layout`` invocation runs the
    check unconditionally (``None`` clock = never validated this process),
    which is also the fresh-operator-start posture.
    """
    global _last_key_layout_revalidation
    _last_key_layout_revalidation = None


def _maybe_revalidate_redis_key_layout(
    body: dict, *, logger: logging.Logger, now: datetime
) -> None:
    """Issue #490 — re-run the Redis key-layout check when the cadence elapses.

    The #163 ``@kopf.on.event`` watch only fires on OSCM watch events (boot
    listing + CR edits); a steady-state cluster generates none, so without
    this rider the key-layout status gauge holds its boot value forever and
    a mid-flight Resque layout drift (helm chart upgrade to a different
    prefix, queue backend swap) is both unreported AND undetected. This
    rider re-invokes ``handlers._check_redis_key_layout_for_cr`` — the
    single validation code path, so the status gauge, its #490 freshness
    pair, the structured log line, and the ``RedisKeyLayoutDrift`` Warning
    deferral all behave exactly as at boot — at most once per
    :data:`REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL` (5 min, half the default
    ``stallWindowMinutes`` so drift surfaces before the first vacuous
    restart window can complete).

    Never raises: the underlying check is total (fully wrapped), so a
    failing validation degrades to the ``unreachable``/``error`` gauges
    and logs — the stall tick that carries it is unaffected. The deferred
    import breaks the handlers↔module cycle (``handlers/__init__``
    imports this module at package load; the check symbol only exists
    after that import block runs).
    """
    global _last_key_layout_revalidation
    if (
        _last_key_layout_revalidation is not None
        and now - _last_key_layout_revalidation < REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL
    ):
        return
    # Stamp BEFORE the call: the check is total (never raises), so this
    # records "attempted at" == "ran at" without a post-call line — and a
    # hypothetical future raising variant would retry next tick rather
    # than hot-looping.
    _last_key_layout_revalidation = now
    from openstudio_operator.handlers import _check_redis_key_layout_for_cr

    _check_redis_key_layout_for_cr(body, logger=logger)


_RUNNING = "Running"

# Note: ``_max_workers_seen``, ``_empty_registry_since``, and
# ``_resque_layout_warning_emitted`` live at the top of this module
# alongside ``RESQUE_KEY_LAYOUT_UNKNOWN_EVENT`` — colocating the issue #44
# safeguard state with the constants it gates keeps the leg-2 fix auditable
# in one place.


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


_tracker_cache: dict[tuple[str, str], tuple[str | None, StallWindowTracker]] = {}
# Keyed by ``(namespace, name)``; UID-VALIDATED since #497 (values are
# ``(recorded_uid, tracker)`` — a lookup under a different uid is the #364
# delete+recreate signature and starts a fresh tracker; see
# :mod:`openstudio_operator._cr_cache` for the convention + census and
# :func:`reset_per_cr_caches` for the reset seam). The D05 singleton guard
# (see ``openstudio_operator.singleton`` and the invariant captured in
# ``StallWindowTracker.__init__``'s docstring) ensures at most one OSCM
# CR per namespace, so the tuple uniquely identifies the active CR.
# See ``tests/test_singleton_registry_coverage.py`` for the test that
# fails loudly if the singleton guard is bypassed by a new handler.
# Issue #167.


def _get_tracker(namespace: str, name: str, uid: str | None = None) -> StallWindowTracker:
    entry = _tracker_cache.get((namespace, name))
    if entry is not None and cr_cache.uid_is_stale(entry[0], uid):
        # Issue #497 — the cached tracker belongs to the DELETED
        # predecessor CR (same ``(namespace, name)``, new uid — the #364
        # delete+recreate path). Its partially-accumulated window must NOT
        # carry into the new CR: that leak could satisfy the sustained
        # window on the new CR's FIRST holding ticks and fire a restart
        # earlier than a fresh observation would (not conservative).
        logger.info(
            "StallWindowTracker cache entry for %s/%s belongs to a deleted "
            "CR (recorded uid %r != observed %r) — starting a fresh "
            "sustained-window clock (#364 delete+recreate; #497 uid "
            "validation)",
            namespace,
            name,
            entry[0],
            uid,
        )
        entry = None
    if entry is None:
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
        _tracker_cache[(namespace, name)] = (uid, tracker)
        return tracker
    if uid is not None and entry[0] is None:
        # First uid sighting for an entry recorded pre-uid (or by a
        # uid-less caller): record it so later lookups can validate.
        _tracker_cache[(namespace, name)] = (uid, entry[1])
    return entry[1]


def reset_per_cr_caches(namespace: str | None = None, name: str | None = None) -> None:
    """Reset seam (#497): drop tracker-cache entries (all, or one CR).

    Pass neither argument to clear every entry (test isolation); pass both
    ``namespace`` and ``name`` to clear exactly one CR's entry (the shape a
    future ``@kopf.on.delete`` handler would call — none exists today; the
    uid validation in :func:`_get_tracker` closes the delete+recreate leak
    at lookup time in the meantime). Anything else is a caller bug and
    raises rather than silently clearing the wrong scope.
    """
    if namespace is None and name is None:
        _tracker_cache.clear()
    elif namespace is not None and name is not None:
        _tracker_cache.pop((namespace, name), None)
    else:
        raise ValueError(
            f"reset_per_cr_caches: pass both namespace and name, or neither "
            f"(got namespace={namespace!r}, name={name!r})"
        )


def _worker_pods_healthy(
    apps_api: DeploymentManager, pods_api: PodLister, *, namespace: str, deployment: str
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
    # Issue #488 — time the apiserver LIST (the network surface); the
    # phase checks below are pure computation.
    with observe_duration(KUBE_API_REQUEST_DURATION_SECONDS, verb="list"):
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
    apps_api: DeploymentManager,
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
    # Issue #238 — surface the operator's authoritative LLEN reads as a
    # Prometheus Gauge before the leg evaluation runs, so the metric is
    # populated on every sensing tick (matching the #87 unconditional
    # worker observation pattern: cheap path, no failure mode, advance
    # the gauge on every successful Redis read). The Leg-A cheap-fail
    # below still returns early when both depths are zero — the gauges
    # are already populated, so the operator's authoritative reading
    # remains visible to a scraper even on a perfectly idle fleet.
    depths = redis_client.queue_depths()
    for queue_name, depth in depths.items():
        RESQUE_QUEUE_DEPTH.labels(queue=queue_name).set(depth)
    # Issue #312 — paired freshness stamp set to ``time.time()`` on every
    # successful LLEN read. The ``RESQUE_QUEUE_DEPTH`` gauge above advances
    # on success but is NOT touched on the exception path (Redis
    # unreachable, ApiException from ``stale_workers`` / ``worker_heartbeats``
    # below). Without this stamp the previous tick's depth masquerades as
    # a live reading while the operator has lost visibility. Set at the
    # SAME site that advances the depth gauge so a dashboard's
    # ``time() - RESQUE_QUEUE_DEPTH_FRESH`` computation matches the depth
    # gauge's actual read time exactly.
    RESQUE_QUEUE_DEPTH_FRESH.set(time.time())
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
    apps_api: DeploymentManager,
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
        # Issue #254 — mirror the tracker reset on the gauge so a blind
        # gap does not leave the gauge pointing at a stale elapsed
        # value. The next successful tick (post-recovery) will start a
        # fresh observation window and the gauge will advance from 0
        # again.
        STALL_WINDOW_ELAPSED_SECONDS.set(0.0)
        raise

    # Issue #44 — leg-2 non-vacuity safeguard. Best-effort: a failure to
    # emit the warning must not skip the tick (it's diagnostic, not load-
    # bearing on the stall evaluation).
    try:
        _maybe_warn_resque_layout_unknown(now=now, emit=emit, logger=logger)
    except Exception as exc:  # noqa: BLE001 — defensive only (diagnostic emit)
        logger.debug("leg-2 safeguard emit failed: %s", exc)

    sustained = tracker.observe(holds, now, window)
    # Issue #254 — expose the tracker's accumulated state as a Gauge so
    # SREs have an early-warning signal between the first sustained
    # observation and the eventual ``web_background_restarts_total``
    # increment. The gauge reads the elapsed seconds when the condition
    # holds this tick (``tracker.first_observed`` was set by observe()
    # on the holding tick); 0 when it breaks (matching the tracker's own
    # reset semantics — a broken condition clears ``first_observed`` and
    # the gauge is cleared in lockstep). Set AFTER ``tracker.observe()``
    # so the value reflects the post-observe tracker state exactly.
    if holds and tracker.first_observed is not None:
        elapsed = (now - tracker.first_observed).total_seconds()
        STALL_WINDOW_ELAPSED_SECONDS.set(max(elapsed, 0.0))
    else:
        STALL_WINDOW_ELAPSED_SECONDS.set(0.0)
    # Issue #312 — paired freshness stamp set to ``time.time()`` on every
    # successful post-observe update (both the holding and broken paths).
    # The ``STALL_WINDOW_ELAPSED_SECONDS`` gauge above is updated on the
    # same two paths but is NOT touched on the exception path
    # (``RedisClientError`` | ``ApiException`` raised from
    # ``_stall_condition_holds``) — so a prior tick's value can
    # masquerade as a continuing stall window while the operator has in
    # fact lost visibility. Set at the SAME site that updates the
    # elapsed-gauge so a dashboard's
    # ``time() - STALL_WINDOW_FRESH`` computation matches the elapsed-
    # gauge's actual update time exactly. ``max(elapsed, 0.0)`` is a
    # paranoid guard against negative elapsed values when ``now`` ticks
    # backwards; the freshness stamp does not need the guard (we want
    # the moment we last touched the gauge, not the underlying elapsed).
    STALL_WINDOW_FRESH.set(time.time())
    if not sustained:
        return False

    deployment = config.target_web_background_deployment or DEFAULT_WEB_BACKGROUND_DEPLOYMENT
    dry_run = config.dry_run
    if not dry_run:
        # Issue #395 — shared rolling-restart patch (explicit RFC 7386
        # merge-patch content type applied inside the helper; preserves
        # sibling annotations).
        rolling_restart_deployment(apps_api, deployment=deployment, namespace=namespace, now=now)
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


@dataclass(frozen=True)
class _StallTimerClients:
    """Client bundle the stall monitor's ``wire`` closure builds each tick (#473)."""

    redis_client: ReadOnlyRedisClient
    apps_api: DeploymentManager
    pods_api: PodLister
    tracker: StallWindowTracker


@kopf.timer(**CRD_SPEC, interval=POLL_INTERVAL_SECONDS)
@observe_tick_duration(module="web_background_monitor")
def web_background_monitor(
    body: dict,
    spec: dict,
    namespace: str,
    name: str,
    logger: kopf.Logger,
    **_: object,
) -> None:
    """Timer handler: delegate the wrapper wiring to the shared tick-runner.

    Issue #473: config parse, the empty-serverUrl idle check, store /
    emitter / kube-API construction, the failure counter, and the skip log
    all live in :func:`openstudio_operator._oscm_handlers.run_oscm_tick`;
    this module contributes only its client wiring (Redis + apps + core +
    the stall-window tracker) and the :func:`run_stall_tick` call. The
    shared :func:`openstudio_operator._oscm_handlers.observe_tick_duration`
    decorator (issue #395) still observes the wall-clock duration on
    ``HANDLER_TICK_DURATION_SECONDS.labels
    (module="web_background_monitor")`` in a ``finally`` — regardless of
    success or caught exception.

    Issue #490 — the tick closure ALSO carries the periodic Redis
    key-layout revalidation (:func:`_maybe_revalidate_redis_key_layout`,
    before the stall evaluation and independent of its cooldown gate): a
    steady-state cluster generates no OSCM watch events, so this rider is
    what bounds the ``redis_key_layout_status_fresh`` staleness gap. It
    runs at most once per ``REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL`` (5
    min) and never raises, so the stall semantics below are unchanged.
    """

    def wire(config: OperatorConfig) -> _StallTimerClients:
        return _StallTimerClients(
            redis_client=get_read_only_redis_client(config.redis_url),
            apps_api=operator_apps_api(),
            pods_api=operator_core_api(),
            tracker=_get_tracker(namespace, name, cr_cache.cr_uid(body)),
        )

    def tick(
        *,
        config: OperatorConfig,
        store: StatusStore,
        emit: EventEmitter,
        deps: _StallTimerClients,
        now: datetime,
    ) -> bool:
        # Issue #490 — periodic key-layout revalidation rides this tick
        # BEFORE the stall evaluation (and therefore before the cooldown
        # gate inside run_stall_tick): the revalidation cadence must not
        # stall for a full stall window after every restart the monitor
        # itself issues.
        _maybe_revalidate_redis_key_layout(body, logger=logger, now=now)
        return run_stall_tick(
            deps.redis_client,
            store,
            config,
            deps.apps_api,
            deps.pods_api,
            namespace=namespace,
            now=now,
            emit=emit,
            tracker=deps.tracker,
        )

    fired = run_oscm_tick(
        spec=spec,
        body=body,
        namespace=namespace,
        name=name,
        logger=logger,
        module="web_background_monitor",
        tick_label="web_background monitor",
        idle_label="web_background monitor",
        custom_objects_api=operator_custom_objects_api,
        wire=wire,
        tick=tick,
    )
    if fired:
        logger.warning("web_background stall confirmed — Deployment restart issued")


# Issue #285 / #407 — register this timer in the Python-level OSCM handler
# registry under fn.__name__ for the singleton guard's cross-check.
_register_oscm_handler(web_background_monitor)
