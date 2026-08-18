"""Module 1 (plan Phase 1): Analysis SLA monitor & one-shot soft-stop (issue #8).

Verified against the v3.11.0 REST contract
(``.agents/skills/_shared/api-contracts/openstudio-server-v3.11.0-rest.md``):

* every 30 s (plan-mandated poll cadence — deliberately not a CRD field),
  ``GET /analyses.json``; candidates are analyses with ``status == "started"``
  only — ``na``/``init``/``queued``/``post-processing``/``completed`` are never
  touched;
* the SLA clock is anchored on ``GET /analyses/{id}/page_data.json``
  ``analysis.start_time`` (derived from the first job) — NEVER on
  ``created_at``: an analysis queued for days must not false-trip;
* runtime over ``spec.analysisPolicy.maxDurationMinutes`` with no prior
  anchor in ``status.softStops`` (checked BEFORE acting) →
  ``GET /analyses/{id}/soft_stop`` (cooperative, does not wait for in-flight
  runs), a Warning Event ``AnalysisSoftStopped``, and a
  ``status.softStops[id]`` record written through :class:`StatusStore`.

One-shot semantics (D04): the status anchor is the idempotency mechanism —
it survives ticks and operator restarts, so the stop fires exactly once per
analysis. Anchors are intentionally NOT pruned here: the escalation flow
(issue #9) grace-waits on them after the analysis leaves ``started``.

dryRun (D11): when ``spec.dryRun`` the REST call is suppressed and a
dry-run-marked Event is emitted instead; everything else (anchor, metric)
behaves identically, so flipping the flag changes only the mutation.

Failure handling (D12): the client retries transient REST failures itself;
anything still failing raises out of :func:`run_sla_tick` and the kopf
wrapper skips the tick — an unrecorded stop is re-attempted next poll, a
recorded one never re-fires.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import kopf
from kubernetes.client import CustomObjectsApi

from openstudio_operator.config import OperatorConfig
from openstudio_operator.metrics import SOFT_STOPS_TOTAL
from openstudio_operator.openstudio_client import OpenStudioApiError, OpenStudioClient
from openstudio_operator.status_store import (
    GROUP,
    PLURAL,
    VERSION,
    SoftStopRecord,
    StatusStore,
    StatusStoreError,
)

logger = logging.getLogger(__name__)

_SPEC = {"group": GROUP, "version": VERSION, "plural": PLURAL}

#: Poll cadence fixed by the plan (30 s). Not a CRD field: it is operator
#: behavior, not cluster policy — policy values live in the CRD spec/config.
POLL_INTERVAL_SECONDS = 30.0

ANALYSIS_SOFT_STOPPED_EVENT = "AnalysisSoftStopped"
_STARTED = "started"
_OUTCOME_ISSUED = "issued"
_OUTCOME_DRY_RUN = "dry-run"

#: Event sink: ``(type, reason, message)`` — kopf.event in production, a
#: recorder in tests. Escalation (#9) reuses the same sink for its events.
EventEmitter = Callable[[str, str, str], None]

# Cache-only (D04): one client session per server URL, never operator state.
_client_cache: dict[str, OpenStudioClient] = {}


def _get_client(server_url: str) -> OpenStudioClient:
    client = _client_cache.get(server_url)
    if client is None:
        client = OpenStudioClient(server_url)
        _client_cache[server_url] = client
    return client


def _page_data_start_time(client: OpenStudioClient, analysis_id: str) -> datetime | None:
    """SLA clock anchor from page_data — ``start_time``, never ``created_at``.

    ``None`` (absent/unusable) means "cannot judge yet": skip and let the next
    poll retry — e.g. an analysis whose first job has not produced a derived
    ``start_time`` yet.
    """
    page = client.get_analysis_page_data(analysis_id)
    analysis = page.get("analysis") if isinstance(page, dict) else None
    value = analysis.get("start_time") if isinstance(analysis, dict) else None
    if not isinstance(value, datetime):
        logger.warning(
            "analysis %s: page_data carries no usable start_time — skipping this tick",
            analysis_id,
        )
        return None
    return value


def run_sla_tick(
    client: OpenStudioClient,
    store: StatusStore,
    config: OperatorConfig,
    *,
    now: datetime,
    emit: EventEmitter,
) -> list[str]:
    """One SLA poll over ``GET /analyses.json``. Returns ids soft-stopped this tick.

    Pure function of its arguments plus the live server/CR state: no in-memory
    one-shot tracking — the ``status.softStops`` anchor is the only memory
    (D04). Raises on API/status-store failure so the caller can skip the tick
    (D12); the issue #9 escalation extends this return value with its
    grace-wait/eviction flow.
    """
    max_runtime = timedelta(minutes=config.analysis_policy.max_duration_minutes)
    if not config.analysis_policy.auto_soft_stop:
        logger.debug("analysisPolicy.autoSoftStop is false — SLA monitor passive this tick")
        return []
    soft_stops = store.get_soft_stops()
    soft_stopped: list[str] = []
    for doc in client.list_analyses():
        analysis_id = str(doc.get("_id") or "")
        if not analysis_id or doc.get("status") != _STARTED:
            continue
        if analysis_id in soft_stops:
            continue  # one-shot: the anchor outlives ticks and operator restarts
        start_time = _page_data_start_time(client, analysis_id)
        if start_time is None:
            continue
        runtime = now - start_time
        if runtime <= max_runtime:
            continue
        dry_run = config.dry_run
        if not dry_run:
            client.soft_stop_analysis(analysis_id)
        runtime_minutes = int(runtime // timedelta(minutes=1))
        message = (
            f"Analysis {analysis_id} runtime {runtime_minutes}m exceeds "
            f"maxDurationMinutes={config.analysis_policy.max_duration_minutes}"
        )
        if dry_run:
            message += " — soft stop suppressed (spec.dryRun)"
        else:
            message += " — soft stop issued"
        emit("Warning", ANALYSIS_SOFT_STOPPED_EVENT, message)
        SOFT_STOPS_TOTAL.inc()
        store.set_soft_stop(
            analysis_id,
            SoftStopRecord(
                issued_at=now,
                outcome=_OUTCOME_DRY_RUN if dry_run else _OUTCOME_ISSUED,
            ),
        )
        soft_stopped.append(analysis_id)
    return soft_stopped


@kopf.timer(_SPEC["group"], _SPEC["version"], _SPEC["plural"], interval=POLL_INTERVAL_SECONDS)
def analysis_sla_monitor(
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
        logger.warning("spec.serverUrl is empty — analysis SLA monitor idle this tick")
        return
    client = _get_client(config.server_url)
    store = StatusStore(namespace, name, CustomObjectsApi())

    def emit(event_type: str, reason: str, message: str) -> None:
        kopf.event(body, type=event_type, reason=reason, message=message)

    try:
        soft_stopped = run_sla_tick(client, store, config, now=datetime.now(UTC), emit=emit)
    except (OpenStudioApiError, StatusStoreError) as exc:
        logger.warning(
            "analysis SLA tick skipped, retrying next poll (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return
    if soft_stopped:
        logger.info("analysis SLA monitor soft-stopped %d analysis(es)", len(soft_stopped))
