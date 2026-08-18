"""Module 7 (plan Phase 4): HPA-floor adjuster — Redis backlog → worker-hpa
minReplicas (issue #18, decision D10).

D10: NO KEDA, no chart fork. The helm chart ships an UNCONDITIONAL CPU HPA
(``worker-hpa``, 2–20 in production) on the worker Deployment; deploying a
second autoscaler (KEDA ScaledObject) alongside it would produce two
controllers fighting over one scale target. Instead this adjuster raises
and lowers only the HPA's ``spec.minReplicas`` FLOOR from queue depth,
leaving the CPU signal — and ``maxReplicas``, and ``metrics`` — entirely
the chart's. KEDA is the documented future migration path (README), viable
only if the chart ever makes its HPA conditional.

Signal: ``ReadOnlyRedisClient.queue_depths()`` (#12, read-only) over the
two managed Resque queues. The backlog is the SUM of ``simulations`` and
``requeued`` depths, not the max: workers subscribe to
``QUEUES=requeued,simulations``, so the fleet's outstanding work is the
total pending across both queues — ``max`` would understate pressure
whenever both queues carry moderate load.

Mapping: :class:`~openstudio_operator.config.HpaFloorPolicy` — the
module-level tier table in ``config.py`` (policy as config, AGENTS.md; CRD
fields are a follow-up if tuning demands — the v1alpha1 schema is fixed).
A backlog at or above a tier's threshold maps to that tier's floor; below
every tier maps to the baseline. **The runtime baseline is the HPA's
``spec.minReplicas`` captured at operator startup** (issue #46,
:func:`resolve_baseline_min_replicas`); ``DEFAULT_HPA_BASELINE_MIN_REPLICAS``
in ``config.py`` is the FALLBACK consulted only when the HPA isn't
observable at startup. This is the chart-derived-baseline decision: the
chart already encodes the designed worker floor (NatLabRockies production
chart: 2; #19 kind manifest: 1), and the adjuster respects what's
deployed — hardcoding a different default would let decay undercut the
production chart's intent.

Adjustment semantics (symmetric reconciliation — the adjuster owns the
floor):

* RAISE when ``current < target`` — but never above the HPA's own
  ``maxReplicas`` (read-only respect; ``maxReplicas`` is never patched, so
  a kind manifest with max 2 clamps deep-backlog floors to 2). ``current
  >= target`` with a deep backlog is a no-op (no upward churn).
* DECAY when ``current > target`` — IMMEDIATE-on-clear down to the target
  (which is the chart-derived baseline when the backlog is fully drained,
  or a mid tier when it merely shrank), not a step-down ladder: the CPU
  HPA remains free to sit anywhere at or above the floor, and a lower
  floor only PERMITS scale-down — so decay cannot kill running
  datapoints, and waiting would just delay cost recovery. Both directions
  share the cooldown below, which is what actually prevents flapping.
  Decay BELOW the chart-derived baseline is impossible without an
  explicit override (issue #46).

Cooldown — anti-flap gate, one interval between ANY two adjustments
(default 300 s, ``HpaFloorPolicy.cooldown_seconds``): an adjustment
followed one tick later by the opposite signal does nothing until the
cooldown elapses. Restart-conservatism: the gate clock is IN-MEMORY
(``HpaFloorState`` per CR, D04-clean cache — the v1alpha1 CRD offers no
status scalar for this module, and ``lastRecycleAt`` belongs to the worker
recycler), so an operator restart forgets the last adjustment. The
conservative posture for that: a fresh process makes NO adjustment until
it has successfully observed the backlog for one full cooldown
(``first_observed_at`` anchors on the first successful queue read, and the
gate opens only ``cooldown`` after it). Tradeoff, documented: a restart
can DELAY an adjustment by up to one cooldown, never accelerate one — and
a crash-looping operator cannot flap the floor at all, since every process
lifetime starts with a closed gate. Sensing failures do not anchor
(``first_observed_at`` advances only on a successful read).

Events: a raise is ``Normal`` ``HpaFloorRaised`` (adding capacity is
routine, information-level); a decay is ``Warning`` ``HpaFloorDecayed``
(removing floor capacity is the direction worth human eyes).
``HPA_FLOOR_ADJUSTMENTS_TOTAL`` counts both.

dryRun (D11): the patch is suppressed, the Event carries a dry-run marker
— and the cooldown still advances (same deliberate pacing choice as
#11/#13: a dry-run simulation paces exactly like a real run, and flipping
``spec.dryRun`` back to false changes only the mutation).

Patch discipline, test-enforced: the ONLY field ever patched is
``spec.minReplicas``, via RFC 7386 merge-patch (explicit content type —
the generated client's default for HPA patches is json-patch).
``maxReplicas`` and ``metrics`` are never written. Reads/writes go through
``AutoscalingV1Api`` (autoscaling/v1 view of the chart's v2 HPA — the API
server converts; ``minReplicas`` is common to both).

Error discipline (D12): Redis/K8s failures raise and skip the tick; the
next 60 s poll retries naturally. A missing HPA (chart without it) is the
same skip-and-retry — loud in logs, never fatal.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Protocol

import kopf
from kubernetes.client import ApiException, AutoscalingV1Api

from openstudio_operator.config import (
    DEFAULT_HPA_BASELINE_MIN_REPLICAS,
    DEFAULT_HPA_FLOOR_POLICY,
    HpaFloorPolicy,
    OperatorConfig,
)
from openstudio_operator.handlers.analysis_sla import EventEmitter
from openstudio_operator.metrics import HPA_FLOOR_ADJUSTMENTS_TOTAL
from openstudio_operator.redis_client import ReadOnlyRedisClient, RedisClientError
from openstudio_operator.status_store import GROUP, MERGE_PATCH_CONTENT_TYPE, PLURAL, VERSION

logger = logging.getLogger(__name__)

_SPEC = {"group": GROUP, "version": VERSION, "plural": PLURAL}

#: Tick cadence (60 s, matching every sibling handler). Operator behavior,
#: not cluster policy — policy values live in config.py (AGENTS.md).
POLL_INTERVAL_SECONDS = 60.0

#: The chart-fixed HPA name (helm ``develop`` and the #19 kind manifest,
#: scripts/manifests/06-worker.yaml, agree: ``worker-hpa``). The CRD offers
#: no override field (v1alpha1 schema fixed, #4) — a constant, like the
#: sibling handlers' chart-fixed Deployment-name fallbacks.
WORKER_HPA_NAME = "worker-hpa"

HPA_FLOOR_RAISED_EVENT = "HpaFloorRaised"
HPA_FLOOR_DECAYED_EVENT = "HpaFloorDecayed"

#: K8s API-server default for an omitted ``spec.minReplicas``.
_MIN_REPLICAS_WHEN_UNSPECIFIED = 1

# Cache-only (D04): one Redis client session per redis URL, never operator
# state — mirrors the client caches of the sibling handlers.
_redis_client_cache: dict[str, ReadOnlyRedisClient] = {}

#: Issue #46 — chart-derived baseline. Captured at the FIRST call to
#: :func:`resolve_baseline_min_replicas` per namespace and held stable for
#: the lifetime of the operator process. Re-reading on every tick would
#: defeat the safety property: a concurrent chart edit (or a transient
#: API inconsistency) could silently lower the decay floor. Module-level
#: (NOT per-CR) cache: the HPA is chart-fixed, one per namespace.
_captured_baseline_cache: dict[str, int] = {}


class HpaApi(Protocol):
    """Structural type of ``AutoscalingV1Api`` as used here — tests fake exactly this."""

    def read_namespaced_horizontal_pod_autoscaler(
        self, name: str, namespace: str, **_: object
    ) -> object: ...

    def patch_namespaced_horizontalpodautoscaler(
        self, name: str, namespace: str, body: dict, **_: object
    ) -> object: ...


class HpaFloorState:
    """In-memory cooldown clock for one CR (conservative cache, D04).

    ``last_adjusted_at`` anchors the gate after this process has adjusted
    the floor; before any adjustment, ``first_observed_at`` (first
    SUCCESSFUL backlog read of this process) anchors it — see the module
    docstring for the restart-conservatism tradeoff. No CR status scalar
    exists for this module; a restart starts a fresh, closed gate.
    """

    def __init__(self) -> None:
        self.first_observed_at: datetime | None = None
        self.last_adjusted_at: datetime | None = None

    def gate_open(self, now: datetime, cooldown: timedelta) -> bool:
        """Whether an adjustment is permitted at ``now``.

        Must be called with a ``now`` that follows a successful backlog
        observation this tick (the caller reads Redis first): the very
        first successful observation anchors ``first_observed_at`` and
        keeps the gate closed for one full cooldown.
        """
        anchor = self.last_adjusted_at or self.first_observed_at
        if anchor is None:
            self.first_observed_at = now
            return False
        return now - anchor >= cooldown

    def record_adjustment(self, now: datetime) -> None:
        self.last_adjusted_at = now


_state_cache: dict[tuple[str, str], HpaFloorState] = {}


def _get_state(namespace: str, name: str) -> HpaFloorState:
    state = _state_cache.get((namespace, name))
    if state is None:
        state = HpaFloorState()
        _state_cache[(namespace, name)] = state
    return state


def _get_redis_client(redis_url: str) -> ReadOnlyRedisClient:
    client = _redis_client_cache.get(redis_url)
    if client is None:
        client = ReadOnlyRedisClient(redis_url)
        _redis_client_cache[redis_url] = client
    return client


def resolve_baseline_min_replicas(
    hpa_api: HpaApi,
    *,
    namespace: str,
    hpa_name: str = WORKER_HPA_NAME,
    fallback: int = DEFAULT_HPA_BASELINE_MIN_REPLICAS,
) -> int:
    """Return the runtime decay floor for ``namespace`` (issue #46).

    The chart's ``worker-hpa.spec.minReplicas`` IS the designed floor
    for the worker fleet — NatLabRockies production chart sets 2, the
    #19 kind manifest sets 1. A hardcoded decay floor of 1 would let
    decay on a production cluster drop ``minReplicas`` 2 → 1, undercutting
    the chart's intent (the original #46 bug).

    This function captures the HPA's current ``spec.minReplicas`` ONCE per
    namespace, at the first call, and returns the cached value for the
    lifetime of the operator process. Stability is the safety property:
    re-reading on every tick would let a concurrent chart edit (or a
    transient API inconsistency) silently lower the decay floor mid-run.

    Fallback semantics: if the HPA cannot be read at startup (NotFound,
    RBAC denied, transient API error, cluster not yet ready), the
    documented fallback (``DEFAULT_HPA_BASELINE_MIN_REPLICAS``) is
    cached in place of the captured value — a later successful read
    would be a different (newer) chart state, but process-lifetime
    stability is the invariant we trade for. A warning is logged once
    per capture event.
    """
    cached = _captured_baseline_cache.get(namespace)
    if cached is not None:
        return cached
    try:
        hpa = hpa_api.read_namespaced_horizontal_pod_autoscaler(hpa_name, namespace)
        captured = getattr(hpa.spec, "min_replicas", None) or _MIN_REPLICAS_WHEN_UNSPECIFIED
    except ApiException as exc:
        logger.warning(
            "HPA-floor baseline capture failed for %s/%s (%s: %s); "
            "using fallback baseline %d (chart not yet observable at startup)",
            namespace,
            hpa_name,
            type(exc).__name__,
            exc,
            fallback,
        )
        captured = fallback
    _captured_baseline_cache[namespace] = captured
    return captured


def reset_captured_baseline_cache() -> None:
    """Test-only: clear the process-lifetime baseline cache."""
    _captured_baseline_cache.clear()


def run_hpa_floor_tick(
    redis_client: ReadOnlyRedisClient,
    hpa_api: HpaApi,
    *,
    namespace: str,
    policy: HpaFloorPolicy,
    now: datetime,
    emit: EventEmitter,
    state: HpaFloorState,
    dry_run: bool,
    hpa_name: str = WORKER_HPA_NAME,
    effective_baseline_min_replicas: int | None = None,
) -> bool:
    """One floor evaluation. Returns whether an adjustment fired this tick.

    ``effective_baseline_min_replicas`` is the chart-derived decay floor
    (issue #46, :func:`resolve_baseline_min_replicas`); when supplied, it
    OVERRIDES ``policy.baseline_min_replicas`` for the target computation
    so decay never undercuts the chart's designed ``minReplicas``. When
    ``None``, the policy's own baseline is used (pre-#46 semantics;
    useful for tests that want to exercise the policy in isolation).

    Sense FIRST (two LLENs), then check the cooldown gate — the anchor must
    reflect a successful observation, and a closed gate suppressing only
    the action (not the sensing) is what makes the restart-conservative
    clock run from real observation. Raises ``RedisClientError`` /
    ``ApiException`` so the caller skips the tick (D12); a tick that raised
    never anchored and never patched.
    """
    depths = redis_client.queue_depths()
    backlog = sum(depths.values())
    if not state.gate_open(now, timedelta(seconds=policy.cooldown_seconds)):
        return False

    hpa = hpa_api.read_namespaced_horizontal_pod_autoscaler(hpa_name, namespace)
    current = getattr(hpa.spec, "min_replicas", None) or _MIN_REPLICAS_WHEN_UNSPECIFIED
    max_replicas = getattr(hpa.spec, "max_replicas", None)

    policy_baseline = policy.baseline_min_replicas
    if effective_baseline_min_replicas is not None:
        # Chart-derived baseline (issue #46): the HPA's ``minReplicas``
        # captured at startup is the designed floor; decay must never
        # undercut it. ``max`` lets a policy baseline > chart baseline
        # (e.g. an explicit, future CR override) still win, but the
        # default safety direction is chart-derived >= policy default.
        policy_baseline = max(policy_baseline, effective_baseline_min_replicas)
    target = policy.floor_for(backlog, baseline=policy_baseline)
    if current < target and max_replicas is not None:
        # Never raise the floor above the chart's own ceiling — read-only
        # respect; the HPA controller clamps behavior, we avoid writing an
        # inconsistent spec in the first place.
        target = min(target, max_replicas)

    if current == target:
        return False

    direction = "raise" if current < target else "decay"
    patch_body = {"spec": {"minReplicas": target}}
    if not dry_run:
        hpa_api.patch_namespaced_horizontalpodautoscaler(
            hpa_name, namespace, body=patch_body, _content_type=MERGE_PATCH_CONTENT_TYPE
        )

    message = (
        f"Resque backlog {backlog} (simulations + requeued) → floor {target} "
        f"(was {current}) — {'raising' if direction == 'raise' else 'decaying'} "
        f"{namespace}/{hpa_name} spec.minReplicas"
    )
    if dry_run:
        message += " — patch suppressed (spec.dryRun)"
    emit(
        "Normal" if direction == "raise" else "Warning",
        HPA_FLOOR_RAISED_EVENT if direction == "raise" else HPA_FLOOR_DECAYED_EVENT,
        message,
    )
    HPA_FLOOR_ADJUSTMENTS_TOTAL.inc()
    # Advances in dry-run too — see module docstring (D11 pacing choice,
    # identical to #11/#13).
    state.record_adjustment(now)
    return True


@kopf.timer(_SPEC["group"], _SPEC["version"], _SPEC["plural"], interval=POLL_INTERVAL_SECONDS)
def hpa_floor_adjuster(
    body: dict,
    spec: dict,
    namespace: str,
    name: str,
    logger: kopf.Logger,
    **_: object,
) -> None:
    """Timer thin wrapper: wire config/redis/apis/events, run one tick."""
    config = OperatorConfig.from_spec(spec)
    # Same idle posture as every sibling handler: an OSCM without serverUrl
    # is an incomplete CR, even though this adjuster senses Redis + K8s only.
    if not config.server_url:
        logger.warning("spec.serverUrl is empty — HPA-floor adjuster idle this tick")
        return
    redis_client = _get_redis_client(config.redis_url)
    hpa_api = AutoscalingV1Api()
    state = _get_state(namespace, name)
    # Issue #46 — chart-derived baseline. Captured at the first call per
    # namespace, held stable for the lifetime of the operator process.
    effective_baseline = resolve_baseline_min_replicas(hpa_api, namespace=namespace)

    def emit(event_type: str, reason: str, message: str) -> None:
        kopf.event(body, type=event_type, reason=reason, message=message)

    try:
        adjusted = run_hpa_floor_tick(
            redis_client,
            hpa_api,
            namespace=namespace,
            policy=DEFAULT_HPA_FLOOR_POLICY,
            now=datetime.now(UTC),
            emit=emit,
            state=state,
            dry_run=config.dry_run,
            effective_baseline_min_replicas=effective_baseline,
        )
    except (RedisClientError, ApiException) as exc:
        logger.warning(
            "HPA-floor adjuster tick skipped, retrying next poll (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return
    if adjusted:
        logger.info("HPA floor adjusted from Redis backlog (worker-hpa minReplicas)")
