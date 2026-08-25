"""Module 2 (plan Phase 2): zombie datapoint watchdog & bounded auto-requeue (issue #10, D06).

Verified against the v3.11.0 REST contract
(``docs/contracts/openstudio-server-v3.11.0-rest.md``):

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
  therefore exists instead of the coupled ``StatusStore.prune``. Issue
  #648 defines "disappears for good" operationally and wires the prune:
  see the anchor-hygiene paragraph below.
* Status-anchor hygiene (#648): every
  ``STATUS_ANCHOR_PRUNE_EVERY_N_TICKS``-th tick (10th → 600 s, the
  retention CronJob's cadence) the tick runs ONE heavy liveness pass —
  ``GET /data_points.json`` + ``GET /analyses.json`` (the heavy full
  listings, fetched at most once per hygiene pass, never per tick, and
  skipped entirely while no ``requeues``/``softStops``/``archivedAnalyses``
  entries exist to age out). An id absent from BOTH the light started
  view and its full listing for ``STATUS_ANCHOR_PRUNE_GONE_CONFIRMATIONS``
  consecutive heavy checks (3 → ~30 min of sustained absence) is pruned
  from the ``.status`` maps via :meth:`StatusStore.prune`; re-appearance
  at any heavy check resets its counter. The absence counters are an
  in-memory per-CR cache (uid-validated, #497 convention) on purpose:
  persisting them would add a NEW monotonic id-keyed ``.status`` map —
  the exact problem #648 fixes — and they are debounce state, not
  idempotency anchors: the prune decision derives purely from live
  server views plus the persisted maps, so a restart merely defers a
  prune by up to N heavy checks. The heavy pass also re-registers each
  map's live-id set with ``StatusStore.protect_anchor_keys`` so the #171
  cap eviction (#648 defense-in-depth) never drops a live id's anchor
  while an unprotected candidate remains. dryRun (D11): pruning status
  maps is operator-memory hygiene, not a cluster mutation — it proceeds
  under ``spec.dryRun`` exactly like every other ``.status`` write (the
  requeue budget increment, retention's dry-run markers); the one
  summary Event per pruned run flows through the D11-gated emitter and
  is suppressed/counted like every Normal Event.
* dryRun (D11): the REST call is suppressed and the Event message is
  dry-run-marked, but the budget is incremented EXACTLY as in a real run —
  so flipping ``spec.dryRun`` off never double-burns the budget. This
  mirrors the #8 anchor semantics (identical accounting, suppressed
  mutation only).
* Exhaustion Events fire once per datapoint per CR per operator process
  (the in-memory ``_EXHAUSTED_WARNED`` cache, keyed per CR + uid-validated
  since #497): presentation-only cache, D04-clean — no mutation depends on
  it, and the worst case after an operator restart (or a delete+recreate
  of the CR, #364/#497) is one duplicate Warning Event per still-exhausted
  datapoint. The ``requeues`` map alone drives the never-requeue-again
  guarantee.
* ``maxAutoRequeues: 0`` is the supported warn-only mode: over-runtime
  datapoints get the exhaustion Event immediately and are never requeued.

Failure handling (D12): the client retries transient REST failures itself;
anything still failing raises out of :func:`run_watchdog_tick` and the kopf
wrapper skips the tick — unrecorded requeues are re-attempted next poll,
recorded ones never re-fire.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import kopf

from openstudio_operator import _cr_cache as cr_cache
from openstudio_operator._constants import (
    CRD_SPEC,
    DATAPOINT_POLL_INTERVAL_SECONDS,
    STATUS_ANCHOR_PRUNE_EVERY_N_TICKS,
    STATUS_ANCHOR_PRUNE_GONE_CONFIRMATIONS,
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
from openstudio_operator.handlers.redis_layout_check import _check_redis_key_layout_for_cr
from openstudio_operator.metrics import (
    ANALYSIS_DATAPOINT_COUNT,
    DATAPOINTS_REQUEUE_EXHAUSTED_TOTAL,
    DATAPOINTS_REQUEUED_TOTAL,
)
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.singleton import operator_custom_objects_api
from openstudio_operator.status_store import (
    ARCHIVED_ANALYSES,
    REQUEUES,
    SOFT_STOPS,
    STARTED_SINCE,
    RequeueRecord,
    StatusStore,
)

logger = logging.getLogger(__name__)

#: Poll cadence (issue #165). See :data:`openstudio_operator._constants.DATAPOINT_POLL_INTERVAL_SECONDS`
#: — not a CRD field, it is operator behavior, not cluster policy (policy values
#: live in the CRD spec/config).
POLL_INTERVAL_SECONDS = DATAPOINT_POLL_INTERVAL_SECONDS

# Issue #495 — the ``@kopf.timer`` below consumes the canonical CRD identity
# object (``**CRD_SPEC`` from ``_constants``, imported at the top of this module
# with the rest of the dependency surface) instead of a per-module ``_SPEC``
# dict reassembled from the raw constants.

DATAPOINT_REQUEUED_EVENT = "DatapointRequeued"
DATAPOINT_REQUEUE_EXHAUSTED_EVENT = "DatapointRequeueExhausted"
#: Issue #648 — one Normal Event per hygiene pass that actually pruned
#: anchor entries (summary counts, never per-id: a 10k-entry age-out must
#: not become a 10k-Event storm). Flows through the D11-gated emitter.
STATUS_ANCHORS_PRUNED_EVENT = "StatusAnchorsPruned"


@dataclass
class AnchorPruneState:
    """Issue #648 — per-CR debounce state for the status-anchor hygiene pass.

    Deliberately in-memory cache, not ``.status`` (D04): the absence
    counters are a debounce on a decision that derives purely from live
    server views plus the persisted maps, so a restart (or the #364
    delete+recreate, via the uid-validated cache key) merely restarts the
    debounce — nothing mutating ever reads this state. Persisting the
    counters would add a new monotonic id-keyed status map, the exact
    growth problem #648 exists to fix.
    """

    #: Watchdog ticks seen for this CR by this process (drives the
    #: every-Nth-tick heavy cadence).
    ticks: int = 0
    #: datapoint id → consecutive heavy checks absent from every live view.
    absent_datapoints: dict[str, int] = field(default_factory=dict)
    #: analysis id → consecutive heavy checks absent from every live view.
    absent_analyses: dict[str, int] = field(default_factory=dict)

# Presentation-only (D04): datapoints whose exhaustion Warning has already
# been emitted for THIS CR in this operator process — dedupes per-tick Event
# spam. Not a source of truth: after a restart each still-exhausted datapoint
# re-emits exactly once, and no mutating decision reads this set.
#
# Issue #497 — per-CR cache keying convention: keyed by
# ``(namespace, name)`` and UID-VALIDATED (values are
# ``(recorded_uid, seen_ids)``; a lookup under a different uid is the #364
# delete+recreate signature and starts a fresh set — see
# :mod:`openstudio_operator._cr_cache` for the convention + census and
# :func:`reset_per_cr_caches` for the reset seam). Pre-#497 this was an
# UN-KEYED ``set[str]`` of datapoint ids, which assumed one CR for the
# process lifetime and could suppress CR B's exhaustion Warnings with CR
# A's dedup entries after a delete+recreate.
#
# Issue #583 — the lookup/evict/create/upgrade/reset mechanics live in
# :class:`openstudio_operator._cr_cache.PerCRCache`; this global is the
# module's instance of that holder.
_EXHAUSTED_WARNED: cr_cache.PerCRCache[set[str]] = cr_cache.PerCRCache(
    stale_log=(
        "exhaustion-dedup cache for %s/%s belongs to a deleted CR "
        "(recorded uid %r != observed %r) — starting fresh (#364 "
        "delete+recreate; #497 uid validation)"
    ),
    logger=logger,
)


def _get_exhausted_seen(namespace: str, name: str, uid: str | None = None) -> set[str]:
    """Return the per-CR exhaustion-dedup set, uid-validating the entry (#497).

    Lookup-time staleness check: a cached entry recorded under a different
    uid belongs to the DELETED predecessor CR (same ``(namespace, name)``,
    the #364 delete+recreate path) and is replaced with a fresh set — the
    new CR's still-exhausted datapoints re-earn their one-shot Warning
    instead of being silenced by the old CR's dedup bookkeeping.

    Issue #583 — the mechanics above are
    :meth:`openstudio_operator._cr_cache.PerCRCache.get_or_create`; this
    façade keeps the module's typed seam (the set is handed to
    ``run_watchdog_tick`` as the presentation cache).
    """
    return _EXHAUSTED_WARNED.get_or_create(namespace, name, uid, set)


# Issue #648 — the anchor-hygiene debounce state (see :class:`AnchorPruneState`
# for why it is a per-CR #497 cache and not ``.status``). Same convention as
# ``_EXHAUSTED_WARNED`` above: keyed ``(namespace, name)``, uid-validated at
# lookup, reset through the module's single ``reset_per_cr_caches`` seam.
_ANCHOR_PRUNE: cr_cache.PerCRCache[AnchorPruneState] = cr_cache.PerCRCache(
    stale_log=(
        "anchor-prune debounce state for %s/%s belongs to a deleted CR "
        "(recorded uid %r != observed %r) — starting fresh (#364 "
        "delete+recreate; #497 uid validation)"
    ),
    logger=logger,
)


def _get_anchor_prune_state(namespace: str, name: str, uid: str | None = None) -> AnchorPruneState:
    """Return the per-CR anchor-hygiene debounce state, uid-validating (#497)."""
    return _ANCHOR_PRUNE.get_or_create(namespace, name, uid, AnchorPruneState)


def reset_per_cr_caches(namespace: str | None = None, name: str | None = None) -> None:
    """Reset seam (#497): drop per-CR caches (all, or one CR).

    Clears both module caches: the exhaustion-dedup set and the #648
    anchor-prune debounce state. Pass neither argument to clear every
    entry (test isolation); pass both ``namespace`` and ``name`` to clear
    exactly one CR's entry (the shape a future ``@kopf.on.delete``
    handler would call — none exists today; the uid validation in the
    lookup façades closes the delete+recreate leak at lookup time in the
    meantime). Anything else is a caller bug and raises rather than
    silently clearing the wrong scope.
    """
    _EXHAUSTED_WARNED.reset(namespace, name)
    _ANCHOR_PRUNE.reset(namespace, name)


def _datapoint_ids(docs: list[dict]) -> list[str]:
    """Ordered, de-duplicated ``_id`` extraction from an id-bearing doc list."""
    ids: list[str] = []
    seen: set[str] = set()
    for doc in docs:
        dp_id = str(doc.get("_id") or "")
        if dp_id and dp_id not in seen:
            seen.add(dp_id)
            ids.append(dp_id)
    return ids


def _bump_absences(counters: dict[str, int], tracked: set[str], live: set[str]) -> None:
    """Advance the gone-for-good debounce for one id-space (issue #648).

    ``tracked`` is the set of ids currently keyed in the ``.status`` maps
    for this id-space; ``live`` the ids present in any live server view
    this heavy check. Absent ids gain one consecutive-absence tick;
    re-appearances RESET to zero (a datapoint back in any view must never
    age out on stale evidence); ids no longer tracked (already pruned or
    hand-cleared) drop out of the counter map entirely.
    """
    for key in tracked:
        if key in live:
            counters.pop(key, None)
        else:
            counters[key] = counters.get(key, 0) + 1
    for key in list(counters):
        if key not in tracked:
            del counters[key]


def _run_anchor_hygiene(
    client: OpenStudioClient,
    store: StatusStore,
    *,
    emit: EventEmitter,
    state: AnchorPruneState,
    started_ids: list[str],
) -> None:
    """One heavy liveness pass: age out gone-for-good anchors (#648).

    Runs on every ``STATUS_ANCHOR_PRUNE_EVERY_N_TICKS``-th watchdog tick.
    "Gone for good" = absent from the light started view AND from the
    heavy full listing of the id-space for
    ``STATUS_ANCHOR_PRUNE_GONE_CONFIRMATIONS`` consecutive heavy checks.
    REST/store failures raise → the tick is skipped (D12) and the debounce
    resumes on the next heavy check — an uncounted check never ages
    anything out. dryRun (D11): pruning ``.status`` maps is operator-memory
    hygiene, not a cluster mutation, so it proceeds under ``spec.dryRun``
    like every other status write; the summary Event is D11-gated by the
    emitter like every Normal Event.
    """
    requeues = store.get_requeues()
    soft_stops = store.get_soft_stops()
    archived = store.get_archived_analyses()
    tracked_datapoints = set(requeues)
    tracked_analyses = set(soft_stops) | set(archived)

    if not (tracked_datapoints or tracked_analyses):
        # Nothing keyed anywhere → nothing can be gone-for-good. Skip the
        # heavy listings entirely: an idle cluster pays zero heavy-view
        # cost from this pass (``startedSince`` alone is not tracked here
        # — its departure prune at the top of every tick already removes
        # every entry not in the current started view).
        state.absent_datapoints.clear()
        state.absent_analyses.clear()
        return

    live_datapoints = set(started_ids) | set(_datapoint_ids(client.list_datapoints()))
    live_analyses = set(_datapoint_ids(client.list_analyses()))

    _bump_absences(state.absent_datapoints, tracked_datapoints, live_datapoints)
    _bump_absences(state.absent_analyses, tracked_analyses, live_analyses)

    confirmed_datapoints = {
        key
        for key, count in state.absent_datapoints.items()
        if count >= STATUS_ANCHOR_PRUNE_GONE_CONFIRMATIONS
    }
    confirmed_analyses = {
        key
        for key, count in state.absent_analyses.items()
        if count >= STATUS_ANCHOR_PRUNE_GONE_CONFIRMATIONS
    }

    # Issue #648 defense-in-depth: register the LIVE sets (not the
    # debounce-augmented sets) so cap eviction never drops an anchor for
    # an id a live view just proved alive.
    store.protect_anchor_keys(REQUEUES, live_datapoints)
    store.protect_anchor_keys(STARTED_SINCE, live_datapoints)
    store.protect_anchor_keys(SOFT_STOPS, live_analyses)
    store.protect_anchor_keys(ARCHIVED_ANALYSES, live_analyses)

    if not (confirmed_datapoints or confirmed_analyses):
        return

    # The prune live sets keep the not-yet-confirmed absent ids "live" so
    # the N-check debounce survives the prune call itself (prune would
    # otherwise drop them on their FIRST absence).
    prune_datapoint_live = live_datapoints | (
        set(state.absent_datapoints) - confirmed_datapoints
    )
    prune_analysis_live = live_analyses | (set(state.absent_analyses) - confirmed_analyses)
    pruned = store.prune(
        live_datapoint_ids=prune_datapoint_live,
        live_analysis_ids=prune_analysis_live,
    )
    # Confirmed ids are gone from the maps now — retire their counters.
    for key in confirmed_datapoints:
        state.absent_datapoints.pop(key, None)
    for key in confirmed_analyses:
        state.absent_analyses.pop(key, None)

    if pruned:
        summary = ", ".join(f"{field}: {len(ids)}" for field, ids in sorted(pruned.items()))
        emit(
            "Normal",
            STATUS_ANCHORS_PRUNED_EVENT,
            f"Status-anchor hygiene pruned {len(pruned)} map(s) — {summary}; "
            f"ids absent from every live view for "
            f"{STATUS_ANCHOR_PRUNE_GONE_CONFIRMATIONS} consecutive heavy "
            f"checks (issue #648). Live anchors and their D06 budgets are "
            f"untouched.",
        )


def run_watchdog_tick(
    client: OpenStudioClient,
    store: StatusStore,
    config: OperatorConfig,
    *,
    now: datetime,
    emit: EventEmitter,
    exhausted_seen: set[str],
    anchor_prune_state: AnchorPruneState | None = None,
) -> list[str]:
    """One watchdog poll over the light started-datapoints view.

    Returns the datapoint ids requeued this tick. Pure function of its
    arguments plus the live server/CR state — the ``status.startedSince``
    and ``status.requeues`` anchors are the only memory that matters
    across ticks and operator restarts (D04). Raises on API/status-store
    failure so the caller can skip the tick (D12). ``exhausted_seen`` is
    the caller-owned presentation cache for one-shot exhaustion Events.
    ``anchor_prune_state`` is the caller-owned #648 hygiene debounce
    state (the wrapper passes the per-CR cached instance); ``None`` gives
    the tick a throwaway state whose tick counter never reaches the
    every-Nth-tick heavy cadence — the hygiene pass is inert for one-shot
    callers.
    """
    max_runtime = timedelta(minutes=config.datapoint_policy.max_datapoint_runtime_minutes)
    max_requeues = config.datapoint_policy.max_auto_requeues
    started_ids = _datapoint_ids(client.list_started_datapoints())
    # Issue #179 — observe the per-tick started-datapoint count from the
    # watchdog's initial poll. Recorded once per tick (per-observation).
    # Issue #472 — the ``view`` label separates this site's datapoints
    # population from the SLA monitor's analyses population; the two
    # units must never share a series.
    ANALYSIS_DATAPOINT_COUNT.labels(view="started_datapoints_per_tick").observe(
        len(started_ids)
    )
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

    # Status-anchor hygiene (#648): background age-out for gone-for-good
    # ids, on the every-Nth-tick heavy cadence. Runs AFTER the requeue
    # actions — a hygiene failure raises out of the tick (D12 skip-tick)
    # with the requeue anchors already recorded, so the retry is idempotent.
    prune_state = anchor_prune_state if anchor_prune_state is not None else AnchorPruneState()
    prune_state.ticks += 1
    if prune_state.ticks % STATUS_ANCHOR_PRUNE_EVERY_N_TICKS == 0:
        _run_anchor_hygiene(
            client, store, emit=emit, state=prune_state, started_ids=started_ids
        )
    return requeued


@kopf.timer(**CRD_SPEC, interval=POLL_INTERVAL_SECONDS)
@observe_tick_duration(module="datapoint_watchdog")
def zombie_datapoint_watchdog(
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
    this module contributes only its REST client wiring and the
    :func:`run_watchdog_tick` call. The shared
    :func:`openstudio_operator._oscm_handlers.observe_tick_duration`
    decorator (issue #395) still observes the wall-clock duration on
    ``HANDLER_TICK_DURATION_SECONDS.labels(module="datapoint_watchdog")``
    in a ``finally`` — regardless of success or caught exception.
    """

    def wire(config: OperatorConfig) -> OpenStudioClient:
        return get_openstudio_client(config.server_url)

    def tick(
        *,
        config: OperatorConfig,
        store: StatusStore,
        emit: EventEmitter,
        deps: OpenStudioClient,
        now: datetime,
    ) -> list[str]:
        # Issue #778 — datapoint_watchdog reads no Redis keys directly, but
        # other handlers that do (web_background_monitor, analysis_sla) depend
        # on the shared Resque layout. Validate on every tick so any drift is
        # surfaced immediately rather than waiting for the web_background_monitor's
        # 5-minute revalidation rider.
        _check_redis_key_layout_for_cr(body, logger=logger)
        return run_watchdog_tick(
            deps,
            store,
            config,
            now=now,
            emit=emit,
            exhausted_seen=_get_exhausted_seen(namespace, name, cr_cache.cr_uid(body)),
            anchor_prune_state=_get_anchor_prune_state(namespace, name, cr_cache.cr_uid(body)),
        )

    requeued = run_oscm_tick(
        spec=spec,
        body=body,
        namespace=namespace,
        name=name,
        logger=logger,
        module="datapoint_watchdog",
        tick_label="datapoint watchdog",
        idle_label="datapoint watchdog",
        custom_objects_api=operator_custom_objects_api,
        wire=wire,
        tick=tick,
    )
    if requeued:
        logger.info("datapoint watchdog requeued %d zombie datapoint(s)", len(requeued))


# Issue #285 / #407 — register this timer in the Python-level OSCM handler
# registry under fn.__name__ for the singleton guard's cross-check.
_register_oscm_handler(zombie_datapoint_watchdog)
