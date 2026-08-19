"""Module 2 (plan Phase 2): zombie datapoint watchdog & bounded auto-requeue (issue #10, D06).

Verified against the v3.11.0 REST contract
(``.agents/skills/_shared/api-contracts/openstudio-server-v3.11.0-rest.md``):

* every 60 s (plan-mandated poll cadence — deliberately not a CRD field),
  ``GET /data_points/status?status=1&jobs=started`` — the LIGHT view. The
  ``jobs`` param does the filtering (Rails quirk) and the response carries
  NO timestamps, so the runtime clock is operator-tracked in
  ``status.startedSince`` (D06): created on first observation of a
  datapoint in ``started``, cleared when it leaves, re-created on re-entry
  (a datapoint that leaves and comes back gets a FRESH clock);
* runtime over ``spec.datapointPolicy.maxDatapointRuntimeMinutes`` with
  ``status.requeues[dp].count < maxAutoRequeues`` →
  ``POST /data_points/{id}/requeue`` (204), a Normal Event
  ``DatapointRequeued``, ``DATAPOINTS_REQUEUED_TOTAL`` incremented, and
  ``status.requeues[dp]`` bumped through :class:`StatusStore`;
* exhaustion (over runtime AND budget spent): Warning Event
  ``DatapointRequeueExhausted`` + ``DATAPOINTS_REQUEUE_EXHAUSTED_TOTAL``
  and NOTHING else, ever — no auto-escalation (D06). Kubernetes-side pod
  eviction is the separate Module 1 escalation flow's job.

Documented decisions (beyond the issue text):

* Post-restart clock backfill is CONSERVATIVE: a datapoint observed in
  ``started`` with no ``startedSince`` entry (operator restarted, or the
  entry was pruned) starts its clock at ``now`` — never punitive, since
  the light endpoint cannot prove how long the datapoint has actually run.
* Requeue pacing: a successful (or dry-run) requeue ALSO resets
  ``startedSince[dp]`` to ``now``. Without this, a datapoint that lingers
  in the ``started`` view for a few server-side transition ticks would
  burn the whole budget at one requeue per tick. With it, every requeue
  gets a full ``maxDatapointRuntimeMinutes`` window to take effect before
  the datapoint is considered zombie again.
* Budget durability: ``status.requeues`` entries are NOT pruned when a
  datapoint leaves ``started`` — a requeued datapoint legitimately leaves
  the view while it sits on the ``requeued`` Resque queue, and wiping its
  budget then would reset the bound and unbound the loop. Entries persist
  until a datapoint disappears for good; ``StatusStore.clear_started_since``
  therefore exists instead of the coupled ``StatusStore.prune``.
* dryRun (D11): the REST call is suppressed and the Event message is
  dry-run-marked, but the budget is incremented EXACTLY as in a real run —
  so flipping ``spec.dryRun`` off never double-burns the budget. This
  mirrors the #8 anchor semantics (identical accounting, suppressed
  mutation only).
* Exhaustion Events fire once per datapoint per operator process
  (in-memory ``exhausted_seen`` set): presentation-only cache, D04-clean —
  no mutation depends on it, and the worst case after an operator restart
  is one duplicate Warning Event per still-exhausted datapoint. The
  ``requeues`` map alone drives the never-requeue-again guarantee.
* ``maxAutoRequeues: 0`` is the supported warn-only mode: over-runtime
  datapoints get the exhaustion Event immediately and are never requeued.

Failure handling (D12): the client retries transient REST failures itself;
anything still failing raises out of :func:`run_watchdog_tick` and the kopf
wrapper skips the tick — unrecorded requeues are re-attempted next poll,
recorded ones never re-fire.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import kopf
from kubernetes.client import CustomObjectsApi

from openstudio_operator.client_factory import get_openstudio_client
from openstudio_operator.config import OperatorConfig
from openstudio_operator.events import EventEmitter
from openstudio_operator.metrics import (
    ANALYSIS_DATAPOINT_COUNT,
    DATAPOINTS_REQUEUE_EXHAUSTED_TOTAL,
    DATAPOINTS_REQUEUED_TOTAL,
    HANDLER_TICK_FAILURES_TOTAL,
)
from openstudio_operator.openstudio_client import OpenStudioApiError, OpenStudioClient
from openstudio_operator.status_store import (
    GROUP,
    PLURAL,
    VERSION,
    RequeueRecord,
    StatusStore,
    StatusStoreError,
)

logger = logging.getLogger(__name__)

_SPEC = {"group": GROUP, "version": VERSION, "plural": PLURAL}

#: Poll cadence (issue #165). See :data:`openstudio_operator._constants.DATAPOINT_POLL_INTERVAL_SECONDS`
#: — not a CRD field, it is operator behavior, not cluster policy (policy values
#: live in the CRD spec/config).
from openstudio_operator._constants import DATAPOINT_POLL_INTERVAL_SECONDS

POLL_INTERVAL_SECONDS = DATAPOINT_POLL_INTERVAL_SECONDS

DATAPOINT_REQUEUED_EVENT = "DatapointRequeued"
DATAPOINT_REQUEUE_EXHAUSTED_EVENT = "DatapointRequeueExhausted"

# Presentation-only (D04): datapoints whose exhaustion Warning has already
# been emitted in this operator process — dedupes per-tick Event spam. Not a
# source of truth: after a restart each still-exhausted datapoint re-emits
# exactly once, and no mutating decision reads this set.
_EXHAUSTED_WARNED: set[str] = set()


def _datapoint_ids(docs: list[dict]) -> list[str]:
    """Ordered, de-duplicated ``_id`` extraction from the light view."""
    ids: list[str] = []
    seen: set[str] = set()
    for doc in docs:
        dp_id = str(doc.get("_id") or "")
        if dp_id and dp_id not in seen:
            seen.add(dp_id)
            ids.append(dp_id)
    return ids


def run_watchdog_tick(
    client: OpenStudioClient,
    store: StatusStore,
    config: OperatorConfig,
    *,
    now: datetime,
    emit: EventEmitter,
    exhausted_seen: set[str],
) -> list[str]:
    """One watchdog poll over the light started-datapoints view.

    Returns the datapoint ids requeued this tick. Pure function of its
    arguments plus the live server/CR state — the ``status.startedSince``
    and ``status.requeues`` anchors are the only memory that matters
    across ticks and operator restarts (D04). Raises on API/status-store
    failure so the caller can skip the tick (D12). ``exhausted_seen`` is
    the caller-owned presentation cache for one-shot exhaustion Events.
    """
    max_runtime = timedelta(minutes=config.datapoint_policy.max_datapoint_runtime_minutes)
    max_requeues = config.datapoint_policy.max_auto_requeues
    started_ids = _datapoint_ids(client.list_started_datapoints())
    # Issue #179 — observe the per-CR datapoint budget observed by the
    # watchdog's initial poll. Recorded once per tick (per-observation), no
    # labels — the SLA monitor records the same family for the analyses
    # view, sharing one chart on the operator's dashboard.
    ANALYSIS_DATAPOINT_COUNT.observe(len(started_ids))
    live = set(started_ids)
    started_since = store.get_started_since_map()

    # Departure prune: the datapoint left `started` (completed, or sitting on
    # the requeued queue) — drop its clock so a later re-entry starts fresh.
    for departed in set(started_since) - live:
        store.clear_started_since(departed)
        del started_since[departed]

    # First observation — new datapoint, re-entry, or post-restart backfill:
    # conservative fresh clock (never punitive; the light view has no
    # timestamps to prove otherwise).
    for dp_id in started_ids:
        if dp_id not in started_since:
            store.set_started_since(dp_id, now)
            started_since[dp_id] = now

    requeues = store.get_requeues()
    requeued: list[str] = []
    for dp_id in started_ids:
        if now - started_since[dp_id] <= max_runtime:
            continue
        budget = requeues[dp_id].count if dp_id in requeues else 0
        if budget >= max_requeues:
            if dp_id not in exhausted_seen:
                exhausted_seen.add(dp_id)
                runtime_minutes = int((now - started_since[dp_id]) // timedelta(minutes=1))
                emit(
                    "Warning",
                    DATAPOINT_REQUEUE_EXHAUSTED_EVENT,
                    f"Datapoint {dp_id} runtime {runtime_minutes}m exceeds "
                    f"maxDatapointRuntimeMinutes="
                    f"{config.datapoint_policy.max_datapoint_runtime_minutes} "
                    f"with requeue budget exhausted ({budget}/{max_requeues}) — "
                    f"no further action (no auto-escalation)",
                )
                DATAPOINTS_REQUEUE_EXHAUSTED_TOTAL.inc()
            continue
        dry_run = config.dry_run
        if not dry_run:
            client.requeue_datapoint(dp_id)
        runtime_minutes = int((now - started_since[dp_id]) // timedelta(minutes=1))
        message = (
            f"Datapoint {dp_id} runtime {runtime_minutes}m exceeds "
            f"maxDatapointRuntimeMinutes="
            f"{config.datapoint_policy.max_datapoint_runtime_minutes} "
            f"— requeue {budget + 1}/{max_requeues}"
        )
        message += " suppressed (spec.dryRun)" if dry_run else " issued"
        emit("Normal", DATAPOINT_REQUEUED_EVENT, message)
        DATAPOINTS_REQUEUED_TOTAL.inc()
        store.set_requeue(dp_id, RequeueRecord(count=budget + 1, last_requeued_at=now))
        # Pacing: fresh clock so this requeue gets a full runtime window to
        # take effect before the datapoint is considered zombie again.
        store.set_started_since(dp_id, now)
        started_since[dp_id] = now
        requeued.append(dp_id)
    return requeued


@kopf.timer(_SPEC["group"], _SPEC["version"], _SPEC["plural"], interval=POLL_INTERVAL_SECONDS)
def zombie_datapoint_watchdog(
    body: dict,
    spec: dict,
    namespace: str,
    name: str,
    logger: kopf.Logger,
    **_: object,
) -> None:
    """Timer thin wrapper: wire config/client/store/events, run one tick."""
    config = OperatorConfig.from_spec(spec)
    if not config.server_url:
        logger.warning("spec.serverUrl is empty — datapoint watchdog idle this tick")
        return
    client = get_openstudio_client(config.server_url)
    store = StatusStore(namespace, name, CustomObjectsApi())
    # Issue #164 — single source of truth for Event emission; class wraps
    # kopf.event with the dry-run gate (D11) and exposes a ``__call__``
    # shim so the existing ``emit("Warning", REASON, message)`` call
    # sites below keep working unchanged.
    emit = EventEmitter(body=body, dry_run=config.dry_run)

    try:
        requeued = run_watchdog_tick(
            client,
            store,
            config,
            now=datetime.now(UTC),
            emit=emit,
            exhausted_seen=_EXHAUSTED_WARNED,
        )
    except (OpenStudioApiError, StatusStoreError) as exc:
        HANDLER_TICK_FAILURES_TOTAL.labels(
            module="datapoint_watchdog", error_type=type(exc).__name__
        ).inc()
        logger.warning(
            "datapoint watchdog tick skipped, retrying next poll (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return
    if requeued:
        logger.info("datapoint watchdog requeued %d zombie datapoint(s)", len(requeued))
