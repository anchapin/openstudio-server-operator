"""Module 3 (plan Phase 2): gated worker recycler (issue #11, decision D08).

What recycling is (D08): a rolling restart of the worker Deployment via the
``kubectl.kubernetes.io/restartedAt`` pod-template annotation — the same
mechanism as ``kubectl rollout restart``, HPA-compatible. Worker scratch is
``emptyDir``, so the restart itself IS the scratch cleanup; the plan's
force-clear clause was dropped and no clearing code exists here. Pods are
NEVER deleted directly — the Deployment controller owns pod turnover.

Detection semantics (precise):

* Trigger 1 ``analysis-completed`` — level-based, not edge-based: it fires
  when ``workerPolicy.recycleAfterAnalysis`` is true AND at least one
  analysis has ``status == "completed"`` AND no analysis has
  ``status == "started"`` right now. A true edge ("transitioned to
  completed this tick") is approximated by that level snapshot; completed
  analyses persist server-side, so the level stays true and the trigger
  stays armed — the GATE below is the authoritative rate limiter, bounding
  the approximation to at most one recycle per min-interval window.
* Trigger 2 ``interval-elapsed`` — ``now - status.lastRecycleAt`` exceeds
  ``workerPolicy.recycleWorkerIntervalHours``. ``lastRecycleAt == None``
  (fresh CR / never recycled) counts as infinitely elapsed: otherwise the
  interval trigger could never fire its first time.
* THE GATE — single decision point, checked FIRST, before any REST poll:
  ``lastRecycleAt is None or now - lastRecycleAt >
  workerPolicy.minRecycleIntervalMinutes``. When closed, NOTHING recycles,
  from either trigger — several analyses completing in quick succession
  produce at most one recycle per min-interval window (anti-restart-storm).
* The gate state ``status.lastRecycleAt`` lives in the CR ``.status``
  subresource (D04): a fresh operator process reading the persisted CR
  honors a cooldown straddling an operator restart.

Recycle action: patch the Deployment named by ``spec.targetWorkerDeployment``
(empty falls back to the helm-chart-fixed name ``worker``) with the
``restartedAt`` annotation set to ``now`` (ISO-8601 UTC), emit a Normal
Event ``WorkerRecycled``, increment ``WORKERS_RECYCLED_TOTAL``, then anchor
``status.lastRecycleAt = now`` through :class:`StatusStore`. Patch before
anchor: if the anchor write fails, the next tick re-attempts one (harmless)
extra rolling restart — same accepted race as the SLA monitor's
stop-then-anchor ordering (D12).

dryRun (D11): the Deployment patch is suppressed and the Event is
dry-run-marked — but ``lastRecycleAt`` still advances. Deliberate: a
dry-run simulation then paces exactly like a real run (gate cadence is
observable, and the Event is not re-emitted every tick while dry-run is
on); flipping ``spec.dryRun`` back to false changes only the mutation.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol

import kopf
from kubernetes.client import ApiException

from openstudio_operator.client_factory import get_openstudio_client
from openstudio_operator.config import OperatorConfig
from openstudio_operator.events import EventEmitter
from openstudio_operator.metrics import (
    HANDLER_TICK_DURATION_SECONDS,
    HANDLER_TICK_FAILURES_TOTAL,
    WORKERS_RECYCLED_TOTAL,
)
from openstudio_operator.openstudio_client import OpenStudioApiError, OpenStudioClient
from openstudio_operator.singleton import operator_apps_api, operator_custom_objects_api
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

#: Tick cadence (issue #165). See :data:`openstudio_operator._constants.WORKER_RECYCLE_POLL_INTERVAL_SECONDS`
#: — operator behavior, not cluster policy; policy values live in the CRD
#: spec/config (AGENTS.md).
from openstudio_operator._constants import WORKER_RECYCLE_POLL_INTERVAL_SECONDS

POLL_INTERVAL_SECONDS = WORKER_RECYCLE_POLL_INTERVAL_SECONDS

#: Fallback when ``spec.targetWorkerDeployment`` is empty: the helm
#: ``develop`` chart's fixed worker Deployment name (AGENTS.md identifiers).
DEFAULT_WORKER_DEPLOYMENT = "worker"

#: Pod-template annotation driving the rolling restart — same key/values as
#: ``kubectl rollout restart``; only the value changing triggers a rollout.
RESTARTED_AT_ANNOTATION = "kubectl.kubernetes.io/restartedAt"

WORKER_RECYCLED_EVENT = "WorkerRecycled"

TRIGGER_ANALYSIS_COMPLETED = "analysis-completed"
TRIGGER_INTERVAL_ELAPSED = "interval-elapsed"

_COMPLETED = "completed"

class DeploymentPatcher(Protocol):
    """Structural type of ``AppsV1Api`` as used here — tests fake exactly this."""

    def patch_namespaced_deployment(
        self, name: str, namespace: str, body: dict, **_: object
    ) -> object: ...


def _blocks_recycle(status: object) -> bool:
    """Only ``started`` counts as in-flight work (issue #11 spec).

    ``na``/``init``/``queued`` have not dispatched worker jobs yet;
    ``post-processing``/``completed`` are not ``started`` per the trigger
    definition — the gate remains the authoritative protection either way.
    """
    return status == "started"


def _armed_trigger(
    analyses: list[dict], config: OperatorConfig, *, now: datetime, last_recycle_at: datetime | None
) -> str | None:
    """Which trigger (if any) is armed. Pure; no side effects."""
    if (
        config.worker_policy.recycle_after_analysis
        and any(doc.get("status") == _COMPLETED for doc in analyses)
        and not any(_blocks_recycle(doc.get("status")) for doc in analyses)
    ):
        return TRIGGER_ANALYSIS_COMPLETED
    if last_recycle_at is None or (
        now - last_recycle_at > timedelta(hours=config.worker_policy.recycle_worker_interval_hours)
    ):
        return TRIGGER_INTERVAL_ELAPSED
    return None


def run_recycler_tick(
    client: OpenStudioClient,
    store: StatusStore,
    config: OperatorConfig,
    apps_api: DeploymentPatcher,
    *,
    namespace: str,
    now: datetime,
    emit: EventEmitter,
) -> str | None:
    """One recycler evaluation. Returns the trigger reason if a recycle fired, else ``None``.

    THE GATE is the single decision point and is checked first: when the
    cooldown has not elapsed, the tick returns before even polling analyses —
    no trigger can bypass it. Raises on REST/status-store failure so the
    caller skips the tick (D12); an unrecorded recycle is re-attempted next
    poll, a recorded one never re-fires.
    """
    last_recycle_at = store.get_last_recycle_at()
    cooldown = timedelta(minutes=config.worker_policy.min_recycle_interval_minutes)
    if last_recycle_at is not None and now - last_recycle_at <= cooldown:
        return None  # gate closed: NOTHING recycles, from either trigger
    trigger = _armed_trigger(
        client.list_analyses(), config, now=now, last_recycle_at=last_recycle_at
    )
    if trigger is None:
        return None

    deployment = config.target_worker_deployment or DEFAULT_WORKER_DEPLOYMENT
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
        f"Recycled worker Deployment {namespace}/{deployment} (trigger: {trigger}) "
        f"— rolling restart via {RESTARTED_AT_ANNOTATION} patch"
    )
    if dry_run:
        message += " — patch suppressed (spec.dryRun)"
    emit("Normal", WORKER_RECYCLED_EVENT, message)
    WORKERS_RECYCLED_TOTAL.labels(trigger=trigger).inc()
    # Advances in dry-run too — see module docstring (D11 pacing choice).
    store.set_last_recycle_at(now)
    return trigger


@kopf.timer(_SPEC["group"], _SPEC["version"], _SPEC["plural"], interval=POLL_INTERVAL_SECONDS)
def worker_recycler(
    body: dict,
    spec: dict,
    namespace: str,
    name: str,
    logger: kopf.Logger,
    **_: object,
) -> None:
    """Timer thin wrapper: wire config/client/store/apps/events, run one tick."""
    # Issue #308 — wall-clock observation of the wrapper invocation. The
    # ``finally`` guarantees observation regardless of success or caught
    # exception, so the slow-tick signal is independent of the failure
    # counter and a sustained degradation between the healthy band and
    # the eventual ``handler_tick_failures_total`` increment is visible.
    _started = time.perf_counter()
    try:
        _worker_recycler_impl(
            body=body,
            spec=spec,
            namespace=namespace,
            name=name,
            logger=logger,
        )
    finally:
        HANDLER_TICK_DURATION_SECONDS.labels(module="worker_recycler").observe(
            time.perf_counter() - _started
        )


def _worker_recycler_impl(
    *,
    body: dict,
    spec: dict,
    namespace: str,
    name: str,
    logger: kopf.Logger,
) -> None:
    """Inner body of :func:`worker_recycler` (issue #308)."""
    config = OperatorConfig.from_spec(spec)
    if not config.server_url:
        logger.warning("spec.serverUrl is empty — worker recycler idle this tick")
        return
    client = get_openstudio_client(config.server_url)
    store = StatusStore(namespace, name, operator_custom_objects_api())
    apps_api = operator_apps_api()
    # Issue #164 — single source of truth for Event emission; class wraps
    # kopf.event with the dry-run gate (D11) and exposes a ``__call__``
    # shim so the existing ``emit("Normal", REASON, message)`` call site
    # below keeps working unchanged.
    emit = EventEmitter(body=body, dry_run=config.dry_run)

    try:
        trigger = run_recycler_tick(
            client,
            store,
            config,
            apps_api,
            namespace=namespace,
            now=datetime.now(UTC),
            emit=emit,
        )
    except (OpenStudioApiError, StatusStoreError, ApiException) as exc:
        HANDLER_TICK_FAILURES_TOTAL.labels(
            namespace=namespace,
            name=name,
            module="worker_recycler",
            error_type=type(exc).__name__,
        ).inc()
        logger.warning(
            "worker recycler tick skipped, retrying next poll (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return
    if trigger:
        logger.info("worker recycled (trigger=%s)", trigger)
