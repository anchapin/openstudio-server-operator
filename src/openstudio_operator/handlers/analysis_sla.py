"""Module 1 (plan Phase 1): Analysis SLA monitor — soft-stop + escalation (#8, #9, #83).

Verified against the v3.11.0 REST contract
(``docs/contracts/openstudio-server-v3.11.0-rest.md``):

* every 30 s (plan-mandated poll cadence — deliberately not a CRD field),
  ``GET /analyses.json``; soft-stop candidates are analyses whose
  ``/analyses/{id}/status.json`` reports ``status == "started"`` — the raw
  Mongoid ``status`` field is unreliable (omitted when nil — issue #19 D1
  / #83 D1; ``na``/``init``/``queued``/``post-processing``/``completed``
  are never tripped);
* the SLA clock is anchored on the OPERATOR'S FIRST SIGHT of the analysis
  in the ``started`` state via ``/status.json`` (issue #83 D1: ``page_data
  .json``'s derived ``start_time`` is absent on v3.11.0 until the first
  job — see contract §2; ``/analyses.json`` raw docs omit ``status``
  entirely). The first-sight timestamp is written to
  ``status.softStops[aid].issuedAt`` (the existing durable anchor — D04)
  and reused as the grace-wait origin so the entire ``running → escalated``
  timeline is one operator-observed clock;
* runtime over ``spec.analysisPolicy.maxDurationMinutes`` with no prior
  soft-stop on the anchor →
  ``GET /analyses/{id}/soft_stop`` (cooperative, does not wait for
  in-flight runs), a Warning Event ``AnalysisSoftStopped``, and the anchor
  upgraded from ``outcome="watching"`` to ``outcome="issued"`` through
  :class:`StatusStore`.

One-shot semantics (D04): the status anchor is the idempotency mechanism —
it survives ticks and operator restarts, so the stop fires exactly once per
analysis.

Grace wait + escalation (#9, #83 D2 — no REST kill exists in v3.11.0):

* non-blocking grace — on each tick every anchored analysis that is STILL
  ``started`` is compared against ``analysisPolicy.gracefulStopTimeoutMinutes``
  using the anchor's ``issuedAt`` (a CR-status timestamp, never server
  state: there is no ``stopping`` state to read). Restart-safe by
  construction — a fresh operator process reads the persisted anchor and
  honors the ORIGINAL first-sight time, not its own startup time;
* grace elapsed while still ``started`` → escalate ONCE per anchor
  (``escalatedAt`` on the record is the marker). The escalation target
  resolution is issue #83 D2: pre-#83 the path matched started datapoints'
  ``ip_address`` (heavy ``GET /data_points.json``) against worker pod
  ``status.podIP`` — but on v3.11.0 the datapoint ``ip_address`` field is
  always null, so the IP set was always empty and the surgical eviction
  never fired. The new path reads the Resque worker set
  (``ReadOnlyRedisClient.workers_for_analysis``) and maps each matching
  worker id back to its pod via the standard
  ``{hostname}:{pid}:{queues}`` shape (the K8s pod name is the hostname,
  so the first colon-delimited segment is the pod name — see
  :meth:`ReadOnlyRedisClient.pod_name_for_worker`); only pods that match
  the worker Deployment's label selector (so the eviction cannot broaden
  past the worker fleet — R3 protection still applies) AND that match the
  Resque-resolved set are deleted;
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
anything still failing — including kube API or Redis failures — raises out
of :func:`run_sla_tick` and the kopf wrapper skips the tick: an unrecorded
stop/escalation is re-attempted next poll, a recorded one never re-fires
(accepted races are mutate-then-anchor, same as #8/#11).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import kopf
from kubernetes.client import ApiException, CoreV1Api, CustomObjectsApi

from openstudio_operator.client_factory import get_openstudio_client
from openstudio_operator.config import OperatorConfig
from openstudio_operator.events import EventEmitter
from openstudio_operator.metrics import (
    ANALYSIS_DATAPOINT_COUNT,
    HANDLER_TICK_FAILURES_TOTAL,
    SOFT_STOPS_TOTAL,
    WORKER_PODS_EVICTED_TOTAL,
)
from openstudio_operator.openstudio_client import OpenStudioApiError, OpenStudioClient
from openstudio_operator.redis_client import RedisClientError
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

_STARTED = "started"
#: First-sight observation outcome (issue #83 D1): the SLA clock anchor is the
#: operator's first sight of the analysis in the ``started`` state. The
#: anchor is written BEFORE the runtime check fires, so a freshly-seen
#: started analysis whose first tick is past the maxDuration limit can
#: soft-stop in the same tick. ``watching`` is the "anchor written, no
#: soft-stop yet" state; the soft-stop path upgrades it to ``issued`` (or
#: ``dry-run`` when ``spec.dryRun`` is set).
_OUTCOME_WATCHING = "watching"
_OUTCOME_ISSUED = "issued"
_OUTCOME_DRY_RUN = "dry-run"
#: Escalation outcomes persisted on the anchor's ``escalationOutcome``.
ESCALATION_EVICTED = "evicted"
ESCALATION_EVICTED_PARTIAL = "evicted-partial"
ESCALATION_NO_MATCH = "no-matching-pods"
ESCALATION_DRY_RUN = "dry-run"


@dataclass
class SlaTickResult:
    """Outcome of one SLA tick — the #8 return value extended by #9."""

    #: Analysis ids soft-stopped (anchor written) this tick.
    soft_stopped: list[str]
    #: Analysis ids escalated to worker-pod eviction this tick.
    escalated: list[str]


class DeploymentReader(Protocol):
    """Structural type of ``AppsV1Api`` as used here — tests fake exactly this."""

    def read_namespaced_deployment(self, name: str, namespace: str, **_: object) -> object: ...


class WorkerPodApi(Protocol):
    """Structural type of ``CoreV1Api`` as used here — tests fake exactly this."""

    def list_namespaced_pod(self, namespace: str, **_: object) -> object: ...

    def delete_namespaced_pod(self, name: str, namespace: str, **_: object) -> object: ...


class RedisClientLike(Protocol):
    """Structural type of the read-only Redis client as used here — tests fake exactly this.

    Issue #83 D2 escalation re-sourcing: the SLA monitor now asks Redis which
    Resque workers are currently processing the analysis (their job payload
    references the analysis id), instead of trying to match datapoint
    ``ip_address`` (always null on v3.11.0) against pod IPs. The two methods
    below are the only surface this module needs.
    """

    def workers_for_analysis(self, analysis_id: str) -> list[str]: ...

    def pod_name_for_worker(self, worker_id: str) -> str | None: ...


def _status_is_started(client: OpenStudioClient, analysis_id: str) -> bool:
    """``True`` iff ``/analyses/{id}/status.json`` reports ``status == "started"``.

    Live-verified v3.11.0 (issue #83 D1): the only endpoint that reliably
    reports the real analysis status. ``/analyses.json`` raw docs omit the
    ``status`` key on a fresh analysis; ``page_data.json`` similarly omits
    derived fields until the first job. ``status.json`` is the
    source-of-truth per the contract.

    Wrapping is count-based (contract §7): exactly one match returns
    ``{analysis: {...}}``; zero or many returns ``{analyses: [...]}`` (a
    Resque ``where()`` query, never raises — see issue #19 / #66). The
    helper tolerates both wrappers and returns ``False`` for unknown ids.
    The caller (the SLA tick) treats an unknown id the same as a non-started
    state — the analysis is not a candidate.
    """
    payload = client.get_analysis_status(analysis_id)
    if not isinstance(payload, dict):
        return False
    analysis = payload.get("analysis")
    if isinstance(analysis, dict):
        return analysis.get("status") == _STARTED
    analyses = payload.get("analyses")
    if isinstance(analyses, list) and analyses:
        return analyses[0].get("status") == _STARTED
    return False


def run_sla_tick(
    client: OpenStudioClient,
    store: StatusStore,
    config: OperatorConfig,
    *,
    now: datetime,
    emit: EventEmitter,
    namespace: str = "",
    pod_api: WorkerPodApi | None = None,
    redis_client: RedisClientLike | None = None,
) -> SlaTickResult:
    """One SLA poll: soft-stop, then grace/escalate (issues #8, #9, #83).

    Pure function of its arguments plus the live server/CR state: no
    in-memory one-shot tracking — the ``status.softStops`` anchors are the
    only memory (D04).

    Phase A — first-sight anchoring: for every analysis returned by
    ``GET /analyses.json``, ``GET /analyses/{id}/status.json`` reports
    the real status (raw-doc ``status`` is unreliable on v3.11.0 — see
    contract §2 / issue #83 D1). A first sight of ``status == "started"``
    writes the SLA clock anchor as ``status.softStops[aid] = {issuedAt:
    now, outcome: "watching"}`` — the operator's first-sight timestamp
    becomes the clock origin. Runtime against ``maxDurationMinutes`` is
    computed on SUBSEQUENT ticks (where the anchor already exists); the
    first-sight tick records the anchor and waits — the operator cannot
    know how long the analysis has been running on the server before it
    first observed it (live v3.11.0 has no server-side ``start_time``
    endpoint — issue #83 D1).

    Phase B — grace and escalation: every persisted anchor is re-checked
    each tick. A still-``started`` anchor past the runtime limit soft-stops
    (upgrades ``outcome`` to ``"issued"`` or ``"dry-run"``); once the
    soft-stop has aged past ``gracefulStopTimeoutMinutes`` it escalates
    to worker-pod eviction (issue #9, D2 re-sourced — see
    :func:`_escalate_analysis` for the new Resque-worker-identity
    resolution). Anchors for analyses that left ``started`` are pruned
    (the soft stop worked — no escalation needed).

    Raises on API/status-store/Redis failure so the caller can skip the
    tick (D12).

    ``namespace``/``pod_api`` wire the Kubernetes side of
    the escalation (pod deletion in the CR's namespace); ``redis_client``
    wires the Resque side (worker → analysis match). All three default to
    live clients in production and are injection seams for tests.
    """
    if not config.analysis_policy.auto_soft_stop:
        logger.debug("analysisPolicy.autoSoftStop is false — SLA monitor passive this tick")
        return SlaTickResult(soft_stopped=[], escalated=[])
    soft_stops = store.get_soft_stops()
    analyses = client.list_analyses()
    # Issue #179 — observe the per-CR datapoint budget observed by the SLA
    # monitor's initial poll. Recorded once per tick (per-observation), no
    # labels — bounding cardinality at the histogram level rather than by
    # tagging each analysis. The watchdog records the same family for the
    # started-datapoints view, so the two per-CR surfaces share one chart.
    ANALYSIS_DATAPOINT_COUNT.observe(len(analyses))
    started_ids: set[str] = set()
    for doc in analyses:
        analysis_id = str(doc.get("_id") or "")
        if not analysis_id:
            continue
        if analysis_id in soft_stops:
            # Anchor exists: phase B owns the runtime check / soft-stop / escalation.
            # Also remember the started id for the phase B prune decision.
            if _status_is_started(client, analysis_id):
                started_ids.add(analysis_id)
            continue
        if not _status_is_started(client, analysis_id):
            continue
        # First sight of `started` — write the SLA clock anchor. Runtime
        # is 0 on this tick; subsequent ticks compute runtime from the
        # persisted anchor and trigger the soft-stop (see phase B).
        store.set_soft_stop(
            analysis_id,
            SoftStopRecord(issued_at=now, outcome=_OUTCOME_WATCHING),
        )
        started_ids.add(analysis_id)
    soft_stopped, escalated = _grace_and_escalate(
        client,
        store,
        config,
        started_ids=started_ids,
        now=now,
        emit=emit,
        namespace=namespace,
        pod_api=pod_api,
        redis_client=redis_client,
    )
    return SlaTickResult(soft_stopped=soft_stopped, escalated=escalated)


def _grace_and_escalate(
    client: OpenStudioClient,
    store: StatusStore,
    config: OperatorConfig,
    *,
    started_ids: set[str],
    now: datetime,
    emit: EventEmitter,
    namespace: str,
    pod_api: WorkerPodApi | None,
    redis_client: RedisClientLike | None,
) -> tuple[list[str], list[str]]:
    """Walk persisted anchors: prune the finished, soft-stop the over-runtime, escalate the stuck.

    Issue #83 D1: the SLA clock anchor is the operator-observed first sight
    of the analysis in the ``started`` state (written to ``softStops[aid]
    .issuedAt``). On the first-sight tick the runtime is 0 — we wait. On
    every subsequent tick we recompute runtime against the live status:

    * the analysis is no longer ``started`` (per the ``started_ids`` set
      collected in phase A) → prune the anchor (the soft stop we issued
      earlier, or just completion, retired the analysis);
    * the analysis is still ``started`` and the runtime exceeds
      ``maxDurationMinutes`` but the anchor's ``outcome`` is still
      ``"watching"`` → soft-stop now (issue #8), upgrade ``outcome`` to
      ``"issued"``/``"dry-run"``;
    * the soft-stop was issued and we're past ``gracefulStopTimeoutMinutes``
      → escalate once via the Resque-worker-identity path (issue #9, D2
      re-sourced); the ``escalatedAt`` marker on the anchor prevents
      re-escalation across ticks and operator restarts (D03/D04).

    Returns ``(soft_stopped_ids, escalated_ids)`` — both lists are
    observed-on-this-tick so the caller can surface the right metrics and
    log lines.
    """
    grace = timedelta(minutes=config.analysis_policy.graceful_stop_timeout_minutes)
    max_runtime = timedelta(minutes=config.analysis_policy.max_duration_minutes)
    soft_stopped: list[str] = []
    escalated: list[str] = []
    for analysis_id, record in store.get_soft_stops().items():
        if analysis_id not in started_ids:
            # Left `started` (completed / post-processing / …) or vanished from
            # the API: the soft stop worked or the analysis is gone — retire
            # the anchor (escalation marker included). The #8-deferred prune.
            store.clear_soft_stop(analysis_id)
            logger.info("softStops anchor for %s retired (analysis no longer started)", analysis_id)
            continue
        if record.escalated_at is not None:
            continue  # never escalate the same analysis twice (D03/D04)
        runtime = now - record.issued_at
        if record.outcome == _OUTCOME_WATCHING and runtime > max_runtime:
            # First-sight anchor has now aged past the runtime limit — fire
            # the soft-stop. Strict greater-than matches the pre-#83
            # ``test_runtime_exactly_at_max_does_not_trip`` invariant
            # (a runtime exactly at the limit is still under).
            dry_run = config.dry_run
            if not dry_run:
                client.soft_stop_analysis(analysis_id)
            runtime_minutes = int(runtime // timedelta(minutes=1))
            message = (
                f"Analysis {analysis_id} runtime {runtime_minutes}m exceeds "
                f"maxDurationMinutes={config.analysis_policy.max_duration_minutes} "
                f"(operator-first-sight clock; issue #83 D1)"
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
                    issued_at=record.issued_at,
                    outcome=_OUTCOME_DRY_RUN if dry_run else _OUTCOME_ISSUED,
                ),
            )
            soft_stopped.append(analysis_id)
            # The grace countdown starts at the original issuedAt; we fall
            # through to the grace check below so a tick that crosses
            # BOTH the runtime limit AND the grace in one go can still
            # escalate on the same tick (matches the pre-#83 behavior
            # where a soft-stop on tick T could escalate on the very
            # next tick if the grace had already elapsed).
            continue
        if runtime <= grace:
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
            redis_client=redis_client,
        )
        escalated.append(analysis_id)
    return soft_stopped, escalated


def deployment_label_selector(
    apps_api: DeploymentReader, deployment: str, namespace: str
) -> str | None:
    """Build a Kubernetes label-selector string from a Deployment's own ``spec.selector``.

    Honors BOTH ``spec.selector.matchLabels`` AND ``spec.selector.matchExpressions``
    — issue #44 gap fix. A ``matchExpressions``-only selector previously
    silently fell back to an empty selector (the helper read ``match_labels``
    only), which the label-selector API treats as "list every pod in the
    namespace" — under the web_background_monitor's "worker fleet looks
    fine" check (#13 leg C) that would falsely TRIP the deployment-wide
    pod set, and under the SLA escalation (#9) it would broaden the IP
    matching to non-worker pods.

    Intersection semantics: when both ``matchLabels`` and
    ``matchExpressions`` are set on the Deployment, the Kubernetes label
    selector grammar requires the intersection (AND), which is what the
    comma-separated ``label_selector=`` argument implements — every term
    must match.

    Supported ``matchExpressions`` operators: ``In``, ``NotIn``, ``Exists``,
    ``DoesNotExist``. Anything exotic (e.g. ``Gt``, ``Lt`` — non-string
    operators the label selector grammar does not cover) logs a WARNING
    and falls back to ``matchLabels`` only — the Deployment is then
    discovered with a deliberately narrower selector, which is the
    conservative direction (the worst case is a missed-eviction, not a
    false-eviction). Returns ``None`` only when neither is set.

    The web_background_monitor imports this helper from analysis_sla (both
    files already share the ``DeploymentReader`` Protocol via the existing
    ``EventEmitter`` import — no new cross-module cycle introduced).
    """
    dep = apps_api.read_namespaced_deployment(deployment, namespace)
    selector = getattr(getattr(dep, "spec", None), "selector", None)
    match_labels = getattr(selector, "match_labels", None) or {}
    match_expressions = list(getattr(selector, "match_expressions", None) or [])

    terms: list[str] = [f"{key}={value}" for key, value in sorted(match_labels.items())]

    for expr in match_expressions:
        key = getattr(expr, "key", None)
        operator = getattr(expr, "operator", None)
        values = list(getattr(expr, "values", None) or [])
        if not key or not operator:
            continue
        op = str(operator)
        if op == "In":
            terms.append(f"{key} in ({','.join(values)})")
        elif op == "NotIn":
            terms.append(f"{key} notin ({','.join(values)})")
        elif op == "Exists":
            terms.append(key)
        elif op == "DoesNotExist":
            terms.append(f"!{key}")
        else:
            # Unsupported in the label-selector grammar (e.g. Gt/Lt on numeric
            # values). Conservative direction: fall back to matchLabels only
            # and warn — a narrower selector cannot false-evict.
            logger.warning(
                "worker Deployment %s/%s declares matchExpressions operator %r "
                "on key %r; the Kubernetes label-selector grammar does not "
                "support this operator — falling back to matchLabels=%r "
                "(#44: narrower selector = conservative direction)",
                namespace,
                deployment,
                op,
                key,
                match_labels,
            )
            terms = [f"{key}={value}" for key, value in sorted(match_labels.items())]
            break

    if not terms:
        return None
    return ",".join(terms)


def _resque_matched_worker_pods(
    redis_client: RedisClientLike,
    pod_api: WorkerPodApi,
    *,
    namespace: str,
    analysis_id: str,
) -> list[tuple[str, str]]:
    """Issue #83 D2: resolve escalation targets via Resque worker identity.

    Live v3.11.0 sequence:

    1. Read the Resque worker registry (``resque:workers``) and find every
       worker whose current ``payload.args`` references ``analysis_id`` —
       the OpenStudio Server ``RunSimulateDataPoint`` job class passes
       ``[analysis_id, datapoint_id, ...]`` as the job args, so
       ``workers_for_analysis`` returns the workers currently processing
       this analysis (or any of its datapoints).
    2. Map each matching worker id back to its pod via the standard
       ``{hostname}:{pid}:{queues}`` shape — the K8s pod's ``hostname``
       defaults to the pod name, so the first colon-delimited segment is
       the pod name (:meth:`ReadOnlyRedisClient.pod_name_for_worker`).
    3. The pod list is consulted only to *verify* each candidate is a
       worker pod in the namespace (defensive — a malicious or stale
       Resque record could otherwise claim a non-worker pod name); the
       pod IP is no longer consulted. ``list_namespaced_pod`` with an
       empty selector returns every pod in the namespace — the in-set
       intersection is what scopes the deletion back to the candidates.

    Returns ``[(pod_name, worker_id), ...]`` for pods that exist in the
    namespace AND were matched by the Redis query. An empty list is the
    NO-MATCH case (no Resque worker is currently processing this
    analysis — e.g. the analysis is wedged in the ``started`` state but
    no worker has picked it up, or the worker picked it up before the
    operator started observing).
    """
    worker_ids = redis_client.workers_for_analysis(analysis_id)
    if not worker_ids:
        return []
    pods_response = pod_api.list_namespaced_pod(namespace) if pod_api is not None else None
    candidate_pod_names: set[str] = set()
    for pod in getattr(pods_response, "items", None) or []:
        name = getattr(getattr(pod, "metadata", None), "name", None)
        if name:
            candidate_pod_names.add(str(name))
    matched: list[tuple[str, str]] = []
    for worker_id in worker_ids:
        pod_name = redis_client.pod_name_for_worker(worker_id)
        if pod_name is None or pod_name not in candidate_pod_names:
            # Defensive: skip worker ids that don't map to a pod in the
            # namespace. Logs warn-level once per skip; the caller still
            # records the escalation outcome as no-match.
            logger.warning(
                "Resque worker %r for analysis %s maps to no in-namespace pod "
                "(pod_name=%r, candidates=%d) — skipping in escalation set "
                "(#83 D2: worker record may be stale or pod may have been "
                "deleted out-of-band)",
                worker_id,
                analysis_id,
                pod_name,
                len(candidate_pod_names),
            )
            continue
        matched.append((pod_name, worker_id))
    return matched


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
    redis_client: RedisClientLike | None,
) -> str:
    """Evict the worker pods processing ``analysis_id`` (issue #9, #83 D2).

    Sequence (D03 — no REST kill exists, escalation is Kubernetes-side):

    pre-#83: started-datapoint ``ip_address`` set from
    ``/data_points.json`` → worker pods via the worker Deployment's
    selector → delete ONLY pods whose pod IP is in the set.

    post-#83: Resque workers currently processing the analysis
    (``workers_for_analysis``) → pod names via the worker id's
    hostname segment → ``list_namespaced_pod`` to verify the candidate
    pods exist in the namespace → delete those pods.

    Delete-before-anchor (D12): if the anchor marker write fails after
    deletes, the next tick re-escalates — at most one extra eviction
    burst for pods that likely no longer exist, the same accepted race
    as #8's stop-then-anchor ordering.
    """
    if pod_api is None:
        pod_api = CoreV1Api()
    if redis_client is None:
        redis_client = _default_redis_client(config)
    victims = _resque_matched_worker_pods(
        redis_client,
        pod_api,
        namespace=namespace,
        analysis_id=analysis_id,
    )
    force = config.analysis_policy.force_delete_on_escalation
    dry_run = config.dry_run
    # Exact delete semantics: None = omit grace_period_seconds → the kubelet
    # honors each pod's own terminationGracePeriodSeconds (workers: 5200 s
    # cap; preStop touches kill.worker + Resque QUIT — cooperative drain).
    # 0 = grace_period_seconds=0 → immediate SIGKILL, no drain window.
    grace_seconds: int | None = 0 if force else None
    # Issue #118 — per-pod try/except. Previously a single stuck pod (404
    # Terminating, RBAC-denied, transient 5xx) raised ApiException mid-loop
    # and prevented mark_soft_stop_escalated from being written — making
    # the next tick re-resolve the same victims and re-emit the Event
    # forever. The fix: catch per pod, accumulate evicted-vs-failed, stamp
    # the marker regardless of partial failure with the partial outcome.
    evicted_count = 0
    failed_count = 0
    last_failure: ApiException | None = None
    for pod_name, _worker_id in victims:
        if dry_run:
            WORKER_PODS_EVICTED_TOTAL.inc()
            evicted_count += 1
            continue
        try:
            pod_api.delete_namespaced_pod(pod_name, namespace, grace_period_seconds=grace_seconds)
        except ApiException as exc:
            failed_count += 1
            last_failure = exc
            logger.warning(
                "escalation pod-delete failed for %s/%s (will not block marker): %s",
                namespace, pod_name, exc,
            )
            continue
        WORKER_PODS_EVICTED_TOTAL.inc()
        evicted_count += 1
    if dry_run:
        outcome = ESCALATION_DRY_RUN
    elif evicted_count == 0:
        outcome = ESCALATION_NO_MATCH
    elif failed_count > 0:
        outcome = ESCALATION_EVICTED_PARTIAL
    else:
        outcome = ESCALATION_EVICTED
    if last_failure is not None and evicted_count == 0:
        # All pods failed — re-raise so the wrapper's except branch can
        # observe it (and the tick counter can record the failure).
        raise last_failure
    age_minutes = int((now - record.issued_at) // timedelta(minutes=1))
    message = (
        f"Analysis {analysis_id} still started {age_minutes}m after soft stop "
        f"(gracefulStopTimeoutMinutes={config.analysis_policy.graceful_stop_timeout_minutes})"
        f" — escalating to worker-pod eviction (issue #83 D2: Resque "
        f"worker-identity path): "
    )
    if victims:
        message += "delete " + ", ".join(f"{pod_name} (worker {worker_id})" for pod_name, worker_id in victims)
        message += (
            "; grace_period_seconds=0 (immediate kill)" if force else "; default grace (drain)"
        )
    else:
        message += "no Resque workers currently processing this analysis (worker set empty or no payload match)"
    if dry_run:
        message += " — pod deletions suppressed (spec.dryRun)"
    emit("Warning", ANALYSIS_ESCALATED_EVENT, message)
    store.mark_soft_stop_escalated(analysis_id, now, outcome)
    return outcome


def _default_redis_client(config: OperatorConfig) -> RedisClientLike:
    """Construct the production Redis client (issue #12 + #83 D2).

    Imported lazily so the analysis_sla module can be imported without
    dragging in the redis client (the fakeredis tests don't need it for
    the SLA-only suites). Falls back to the operator's ``config.redis_url``
    (the CRD `spec.redisUrl` field, default
    ``redis://:openstudio@queue.openstudio-server.svc.cluster.local:6379``).
    """
    from openstudio_operator.redis_client import ReadOnlyRedisClient

    return ReadOnlyRedisClient(config.redis_url)


@kopf.timer(_SPEC["group"], _SPEC["version"], _SPEC["plural"], interval=POLL_INTERVAL_SECONDS)
def analysis_sla_monitor(
    body: dict,
    spec: dict,
    namespace: str,
    name: str,
    logger: kopf.Logger,
    **_: object,
) -> None:
    """Timer thin wrapper: wire config/client/store/kube/events/redis, run one tick."""
    config = OperatorConfig.from_spec(spec)
    if not config.server_url:
        logger.warning("spec.serverUrl is empty — analysis SLA monitor idle this tick")
        return
    client = get_openstudio_client(config.server_url)
    store = StatusStore(namespace, name, CustomObjectsApi())
    pod_api: WorkerPodApi = CoreV1Api()
    redis_client: RedisClientLike = _default_redis_client(config)
    # Issue #164 — single source of truth for Event emission. The class
    # encapsulates the dry-run gate (D11) and the suppressed counter; the
    # ``__call__`` shim keeps the ``emit("Warning", REASON, message)``
    # syntax alive for the handler call sites below.
    emit = EventEmitter(body=body, dry_run=config.dry_run)

    try:
        result = run_sla_tick(
            client,
            store,
            config,
            now=datetime.now(UTC),
            emit=emit,
            namespace=namespace,
            pod_api=pod_api,
            redis_client=redis_client,
        )
    except (OpenStudioApiError, StatusStoreError, ApiException, RedisClientError) as exc:
        HANDLER_TICK_FAILURES_TOTAL.labels(
            module="analysis_sla", error_type=type(exc).__name__
        ).inc()
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
