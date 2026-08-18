"""Module 1 (plan Phase 1): Analysis SLA monitor — soft-stop + escalation (#8, #9).

Verified against the v3.11.0 REST contract
(``.agents/skills/_shared/api-contracts/openstudio-server-v3.11.0-rest.md``):

* every 30 s (plan-mandated poll cadence — deliberately not a CRD field),
  ``GET /analyses.json``; soft-stop candidates are analyses with
  ``status == "started"`` only — ``na``/``init``/``queued``/
  ``post-processing``/``completed`` are never touched;
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
analysis.

Grace wait + escalation (#9, D03 — no REST kill exists in v3.11.0):

* non-blocking grace — on each tick every anchored analysis that is STILL
  ``started`` is compared against ``analysisPolicy.gracefulStopTimeoutMinutes``
  using the anchor's ``issuedAt`` (a CR-status timestamp, never server
  state: there is no ``stopping`` state to read). Restart-safe by
  construction — a fresh operator process reads the persisted anchor and
  honors the ORIGINAL soft-stop time, not its own startup time;
* grace elapsed while still ``started`` → escalate ONCE per anchor
  (``escalatedAt`` on the record is the marker): ``GET /data_points.json``
  (heavy — escalation-only per the contract) supplies the ``ip_address`` of
  that analysis's started datapoints; worker pods are discovered via the
  pod-template selector of the Deployment named by
  ``spec.targetWorkerDeployment`` (empty → helm-fixed ``worker``), listed
  in the CR's namespace, and ONLY pods whose ``status.podIP`` matches a
  started datapoint's ``ip_address`` are deleted — surgical by IP, never a
  deployment-wide restart;
* ``analysisPolicy.forceDeleteOnEscalation`` switches the delete grace:
  ``false`` (default) passes NO ``grace_period_seconds`` — the kubelet
  honors each pod's own ``terminationGracePeriodSeconds`` (the chart's
  workers: 5200 s cap; the preStop hook touches ``kill.worker`` and QUITs
  Resque, so the container normally exits long before the cap — a
  cooperative drain); ``true`` passes ``grace_period_seconds=0`` —
  immediate kill (SIGKILL), no drain window;
* either way a Warning Event ``AnalysisEscalated`` is emitted (also when no
  pod matches — the escalation happened, it just found nothing to evict),
  ``WORKER_PODS_EVICTED_TOTAL`` counts per deleted (or, in dry-run,
  would-be-deleted) pod, and the anchor is stamped via
  :meth:`StatusStore.mark_soft_stop_escalated`;
* anchor retirement — an anchored analysis that left ``started``
  (completed, post-processing, …) or vanished from the API during grace
  means the soft stop worked: no escalation, and the anchor is pruned
  (``StatusStore.clear_soft_stop``). This is where the #8-deferred
  softStops pruning lands.

``analysisPolicy.autoSoftStop`` false makes the WHOLE module passive — no
soft-stops, no grace bookkeeping, no escalation, no pruning; anchors from a
previously enabled period persist untouched until the flag is re-enabled.

dryRun (D11): when ``spec.dryRun`` the mutations (soft_stop REST call, pod
deletes) are suppressed and dry-run-marked Events are emitted instead;
everything else (anchors, markers, metrics) behaves identically, so
flipping the flag changes only the mutation.

Failure handling (D12): the client retries transient REST failures itself;
anything still failing — including kube API failures — raises out of
:func:`run_sla_tick` and the kopf wrapper skips the tick: an unrecorded
stop/escalation is re-attempted next poll, a recorded one never re-fires
(accepted races are mutate-then-anchor, same as #8/#11).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import kopf
from kubernetes.client import ApiException, AppsV1Api, CoreV1Api, CustomObjectsApi

from openstudio_operator.config import OperatorConfig
from openstudio_operator.metrics import SOFT_STOPS_TOTAL, WORKER_PODS_EVICTED_TOTAL
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
#: Escalation Event (#9). The plan doc names only ``AnalysisSoftStopped`` /
#: ``WorkerRecycled``; this name follows the same <Subject><Action> shape.
ANALYSIS_ESCALATED_EVENT = "AnalysisEscalated"

#: Fallback when ``spec.targetWorkerDeployment`` is empty: the helm
#: ``develop`` chart's fixed worker Deployment name (AGENTS.md identifiers).
#: Duplicated from worker_recycler (which imports from this module) rather
#: than importing back — a cycle; a shared wiring module can absorb both.
_DEFAULT_WORKER_DEPLOYMENT = "worker"

_STARTED = "started"
_OUTCOME_ISSUED = "issued"
_OUTCOME_DRY_RUN = "dry-run"
#: Escalation outcomes persisted on the anchor's ``escalationOutcome``.
ESCALATION_EVICTED = "evicted"
ESCALATION_NO_MATCH = "no-matching-pods"
ESCALATION_DRY_RUN = "dry-run"

#: Event sink: ``(type, reason, message)`` — kopf.event in production, a
#: recorder in tests. Shared by the soft-stop and escalation flows.
EventEmitter = Callable[[str, str, str], None]

# Cache-only (D04): one client session per server URL, never operator state.
_client_cache: dict[str, OpenStudioClient] = {}


@dataclass
class SlaTickResult:
    """Outcome of one SLA tick — the #8 return value extended by #9."""

    #: Analysis ids soft-stopped (anchor written) this tick.
    soft_stopped: list[str]
    #: Analysis ids escalated to worker-pod eviction this tick.
    escalated: list[str]


class DeploymentReader(Protocol):
    """Structural type of ``AppsV1Api`` as used here — tests fake exactly this."""

    def read_namespaced_deployment(
        self, name: str, namespace: str, **_: object
    ) -> object: ...


class WorkerPodApi(Protocol):
    """Structural type of ``CoreV1Api`` as used here — tests fake exactly this."""

    def list_namespaced_pod(self, namespace: str, **_: object) -> object: ...

    def delete_namespaced_pod(self, name: str, namespace: str, **_: object) -> object: ...


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
    namespace: str = "",
    pod_api: WorkerPodApi | None = None,
    apps_api: DeploymentReader | None = None,
) -> SlaTickResult:
    """One SLA poll over ``GET /analyses.json``: soft-stop, then grace/escalate.

    Pure function of its arguments plus the live server/CR state: no
    in-memory one-shot tracking — the ``status.softStops`` anchors are the
    only memory (D04). Phase A soft-stops over-runtime unanchored analyses
    (issue #8, unchanged); phase B walks every persisted anchor: still
    ``started`` past the grace → escalate once (issue #9), anything else →
    prune. Raises on API/status-store failure so the caller can skip the
    tick (D12).

    ``namespace``/``pod_api``/``apps_api`` wire the Kubernetes side of the
    escalation (pod listing/deletion in the CR's namespace); the apis
    default to the in-cluster clients and are injection seams for tests.
    """
    max_runtime = timedelta(minutes=config.analysis_policy.max_duration_minutes)
    if not config.analysis_policy.auto_soft_stop:
        logger.debug("analysisPolicy.autoSoftStop is false — SLA monitor passive this tick")
        return SlaTickResult(soft_stopped=[], escalated=[])
    soft_stops = store.get_soft_stops()
    analyses = client.list_analyses()
    status_by_id = {
        str(doc.get("_id") or ""): doc.get("status") for doc in analyses if doc.get("_id")
    }
    result = SlaTickResult(soft_stopped=[], escalated=[])
    for doc in analyses:
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
        result.soft_stopped.append(analysis_id)
    result.escalated = _grace_and_escalate(
        client,
        store,
        config,
        status_by_id=status_by_id,
        now=now,
        emit=emit,
        namespace=namespace,
        pod_api=pod_api,
        apps_api=apps_api,
    )
    return result


def _grace_and_escalate(
    client: OpenStudioClient,
    store: StatusStore,
    config: OperatorConfig,
    *,
    status_by_id: dict[str, str | None],
    now: datetime,
    emit: EventEmitter,
    namespace: str,
    pod_api: WorkerPodApi | None,
    apps_api: DeploymentReader | None,
) -> list[str]:
    """Walk persisted anchors: prune the finished, wait on the young, escalate the stuck.

    ``status_by_id`` is this tick's ``GET /analyses.json`` snapshot (one
    shared poll). Anchors written by phase A above are re-read fresh here
    but always age 0, so they only ever wait. Returns escalated ids.
    """
    grace = timedelta(minutes=config.analysis_policy.graceful_stop_timeout_minutes)
    escalated: list[str] = []
    for analysis_id, record in store.get_soft_stops().items():
        if status_by_id.get(analysis_id) != _STARTED:
            # Left `started` (completed / post-processing / …) or vanished from
            # the API: the soft stop worked or the analysis is gone — retire
            # the anchor (escalation marker included). The #8-deferred prune.
            store.clear_soft_stop(analysis_id)
            logger.info("softStops anchor for %s retired (analysis no longer started)", analysis_id)
            continue
        if record.escalated_at is not None:
            continue  # never escalate the same analysis twice (D03/D04)
        if now - record.issued_at <= grace:
            continue  # non-blocking grace still running — wait, mutate nothing
        _escalate_analysis(
            client,
            store,
            config,
            analysis_id,
            record=record,
            now=now,
            emit=emit,
            namespace=namespace,
            pod_api=pod_api,
            apps_api=apps_api,
        )
        escalated.append(analysis_id)
    return escalated


def _started_datapoint_ips(client: OpenStudioClient, analysis_id: str) -> set[str]:
    """``ip_address`` set of the analysis's started datapoints.

    The ONLY sanctioned use of ``GET /data_points.json`` (heavy, no
    server-side filtering — the contract marks it escalation-only): full
    docs supply ``status`` and ``ip_address``. Non-started datapoints and
    other analyses' datapoints contribute nothing.
    """
    ips: set[str] = set()
    for doc in client.get_datapoints_full():
        if str(doc.get("analysis_id") or "") != analysis_id:
            continue
        if doc.get("status") != _STARTED:
            continue
        ip = doc.get("ip_address")
        if ip:
            ips.add(str(ip))
    return ips


def _worker_pod_selector(apps_api: DeploymentReader, config: OperatorConfig, namespace: str) -> str | None:
    """Label selector of the worker Deployment's pod template.

    Read from the cluster instead of hardcoding chart labels: the selector
    the Deployment itself owns is by definition what separates worker pods
    from web/web_background/mongo pods. ``None`` (no selector) lists every
    pod in the namespace — unreachable for apps/v1 Deployments (a selector
    is required); IP matching still scopes any deletion to pods actually
    running the stuck analysis's datapoints.
    """
    deployment = config.target_worker_deployment or _DEFAULT_WORKER_DEPLOYMENT
    dep = apps_api.read_namespaced_deployment(deployment, namespace)
    selector = getattr(getattr(dep, "spec", None), "selector", None)
    match_labels = getattr(selector, "match_labels", None) or {}
    return ",".join(f"{key}={value}" for key, value in sorted(match_labels.items())) or None


def _matching_worker_pods(
    apps_api: DeploymentReader,
    pod_api: WorkerPodApi,
    config: OperatorConfig,
    *,
    namespace: str,
    target_ips: set[str],
) -> list[tuple[str, str]]:
    """Worker pods in the namespace whose pod IP runs a started datapoint."""
    selector = _worker_pod_selector(apps_api, config, namespace)
    pods = getattr(pod_api.list_namespaced_pod(namespace, label_selector=selector), "items", None)
    victims: list[tuple[str, str]] = []
    for pod in pods or []:
        pod_ip = getattr(getattr(pod, "status", None), "pod_ip", None)
        name = getattr(getattr(pod, "metadata", None), "name", None)
        if pod_ip and name and str(pod_ip) in target_ips:
            victims.append((str(name), str(pod_ip)))
    return victims


def _escalate_analysis(
    client: OpenStudioClient,
    store: StatusStore,
    config: OperatorConfig,
    analysis_id: str,
    *,
    record: SoftStopRecord,
    now: datetime,
    emit: EventEmitter,
    namespace: str,
    pod_api: WorkerPodApi | None,
    apps_api: DeploymentReader | None,
) -> str:
    """Evict the worker pods running ``analysis_id``'s started datapoints.

    Sequence (D03 — no REST kill exists, escalation is Kubernetes-side):
    started-datapoint ``ip_address`` set from ``/data_points.json`` → worker
    pods via the worker Deployment's selector → delete ONLY pods whose
    pod IP is in the set. Delete-before-anchor (D12): if the anchor marker
    write fails after deletes, the next tick re-escalates — at most one
    extra eviction burst for pods that likely no longer exist, the same
    accepted race as #8's stop-then-anchor ordering.
    """
    if pod_api is None:
        pod_api = CoreV1Api()
    if apps_api is None:
        apps_api = AppsV1Api()
    target_ips = _started_datapoint_ips(client, analysis_id)
    victims = _matching_worker_pods(
        apps_api, pod_api, config, namespace=namespace, target_ips=target_ips
    )
    force = config.analysis_policy.force_delete_on_escalation
    dry_run = config.dry_run
    # Exact delete semantics: None = omit grace_period_seconds → the kubelet
    # honors each pod's own terminationGracePeriodSeconds (workers: 5200 s
    # cap; preStop touches kill.worker + Resque QUIT — cooperative drain).
    # 0 = grace_period_seconds=0 → immediate SIGKILL, no drain window.
    grace_seconds: int | None = 0 if force else None
    for pod_name, _ in victims:
        if not dry_run:
            pod_api.delete_namespaced_pod(pod_name, namespace, grace_period_seconds=grace_seconds)
        WORKER_PODS_EVICTED_TOTAL.inc()
    outcome = (
        ESCALATION_DRY_RUN
        if dry_run
        else (ESCALATION_EVICTED if victims else ESCALATION_NO_MATCH)
    )
    age_minutes = int((now - record.issued_at) // timedelta(minutes=1))
    message = (
        f"Analysis {analysis_id} still started {age_minutes}m after soft stop "
        f"(gracefulStopTimeoutMinutes={config.analysis_policy.graceful_stop_timeout_minutes})"
        f" — escalating to worker-pod eviction: "
    )
    if victims:
        message += "delete " + ", ".join(f"{pod_name} ({ip})" for pod_name, ip in victims)
        message += "; grace_period_seconds=0 (immediate kill)" if force else "; default grace (drain)"
    else:
        message += "no worker pods matched the started datapoints' ip_address set"
    if dry_run:
        message += " — pod deletions suppressed (spec.dryRun)"
    emit("Warning", ANALYSIS_ESCALATED_EVENT, message)
    store.mark_soft_stop_escalated(analysis_id, now, outcome)
    return outcome


@kopf.timer(_SPEC["group"], _SPEC["version"], _SPEC["plural"], interval=POLL_INTERVAL_SECONDS)
def analysis_sla_monitor(
    body: dict,
    spec: dict,
    namespace: str,
    name: str,
    logger: kopf.Logger,
    **_: object,
) -> None:
    """Timer thin wrapper: wire config/client/store/kube/events, run one tick."""
    config = OperatorConfig.from_spec(spec)
    if not config.server_url:
        logger.warning("spec.serverUrl is empty — analysis SLA monitor idle this tick")
        return
    client = _get_client(config.server_url)
    store = StatusStore(namespace, name, CustomObjectsApi())
    pod_api: WorkerPodApi = CoreV1Api()
    apps_api: DeploymentReader = AppsV1Api()

    def emit(event_type: str, reason: str, message: str) -> None:
        kopf.event(body, type=event_type, reason=reason, message=message)

    try:
        result = run_sla_tick(
            client,
            store,
            config,
            now=datetime.now(UTC),
            emit=emit,
            namespace=namespace,
            pod_api=pod_api,
            apps_api=apps_api,
        )
    except (OpenStudioApiError, StatusStoreError, ApiException) as exc:
        logger.warning(
            "analysis SLA tick skipped, retrying next poll (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return
    if result.soft_stopped:
        logger.info("analysis SLA monitor soft-stopped %d analysis(es)", len(result.soft_stopped))
    if result.escalated:
        logger.warning(
            "analysis SLA monitor escalated %d analysis(es) to worker-pod eviction: %s",
            len(result.escalated),
            ", ".join(result.escalated),
        )
