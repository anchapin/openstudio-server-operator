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
from dataclasses import dataclass
from datetime import datetime, timedelta

import kopf

from openstudio_operator._k8s import (
    DEFAULT_WORKER_DEPLOYMENT,
    RESTARTED_AT_ANNOTATION,
    DeploymentManager,
    rolling_restart_deployment,
)
from openstudio_operator._oscm_handlers import (
    observe_tick_duration,
    run_oscm_tick,
)
from openstudio_operator._oscm_handlers import (
    register_fn as _register_oscm_handler,
)
from openstudio_operator.client_factory import get_openstudio_client
from openstudio_operator.config import OperatorConfig
from openstudio_operator.events import EventEmitter
from openstudio_operator.metrics import WORKERS_RECYCLED_TOTAL
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.singleton import operator_apps_api, operator_custom_objects_api
from openstudio_operator.status_store import (
    MERGE_PATCH_CONTENT_TYPE,  # noqa: F401 — re-export: tests import it from this module
    StatusStore,
)

logger = logging.getLogger(__name__)

#: Tick cadence (issue #165). See :data:`openstudio_operator._constants.WORKER_RECYCLE_POLL_INTERVAL_SECONDS`
#: — operator behavior, not cluster policy; policy values live in the CRD
#: spec/config (AGENTS.md).
from openstudio_operator._constants import WORKER_RECYCLE_POLL_INTERVAL_SECONDS

POLL_INTERVAL_SECONDS = WORKER_RECYCLE_POLL_INTERVAL_SECONDS

# Issue #495 — the ``@kopf.timer`` below consumes the canonical CRD identity
# object (``**CRD_SPEC`` from ``_constants``) instead of a per-module ``_SPEC``
# dict reassembled from the raw constants.
from openstudio_operator._constants import CRD_SPEC

# Issue #395 — DEFAULT_WORKER_DEPLOYMENT and RESTARTED_AT_ANNOTATION were
# declared here AND in web_background_monitor.py; both now live once in
# :mod:`openstudio_operator._k8s` (imported above) and are re-exported by
# this module so existing test imports keep resolving.

WORKER_RECYCLED_EVENT = "WorkerRecycled"

TRIGGER_ANALYSIS_COMPLETED = "analysis-completed"
TRIGGER_INTERVAL_ELAPSED = "interval-elapsed"

_COMPLETED = "completed"


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
    apps_api: DeploymentManager,
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
    dry_run = config.dry_run
    if not dry_run:
        # Issue #395 — shared rolling-restart patch (explicit RFC 7386
        # merge-patch content type applied inside the helper; preserves
        # sibling annotations).
        rolling_restart_deployment(apps_api, deployment=deployment, namespace=namespace, now=now)
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


@dataclass(frozen=True)
class _RecyclerTimerClients:
    """Client bundle the recycler timer's ``wire`` closure builds each tick (#473)."""

    client: OpenStudioClient
    apps_api: DeploymentManager


@kopf.timer(**CRD_SPEC, interval=POLL_INTERVAL_SECONDS)
@observe_tick_duration(module="worker_recycler")
def worker_recycler(
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
    this module contributes only its client wiring (REST + apps) and the
    :func:`run_recycler_tick` call. The shared
    :func:`openstudio_operator._oscm_handlers.observe_tick_duration`
    decorator (issue #395) still observes the wall-clock duration on
    ``HANDLER_TICK_DURATION_SECONDS.labels(module="worker_recycler")`` in
    a ``finally`` — regardless of success or caught exception.
    """

    def wire(config: OperatorConfig) -> _RecyclerTimerClients:
        return _RecyclerTimerClients(
            client=get_openstudio_client(config.server_url),
            apps_api=operator_apps_api(),
        )

    def tick(
        *,
        config: OperatorConfig,
        store: StatusStore,
        emit: EventEmitter,
        deps: _RecyclerTimerClients,
        now: datetime,
    ) -> str | None:
        return run_recycler_tick(
            deps.client,
            store,
            config,
            deps.apps_api,
            namespace=namespace,
            now=now,
            emit=emit,
        )

    trigger = run_oscm_tick(
        spec=spec,
        body=body,
        namespace=namespace,
        name=name,
        logger=logger,
        module="worker_recycler",
        tick_label="worker recycler",
        idle_label="worker recycler",
        custom_objects_api=operator_custom_objects_api,
        wire=wire,
        tick=tick,
    )
    if trigger:
        logger.info("worker recycled (trigger=%s)", trigger)


# Issue #285 / #407 — register this timer in the Python-level OSCM handler
# registry under fn.__name__ for the singleton guard's cross-check.
_register_oscm_handler(worker_recycler)
