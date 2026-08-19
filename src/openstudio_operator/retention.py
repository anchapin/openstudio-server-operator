"""Retention pipeline — archive, verify, delete (plan Module 4 / #16, D09; #78).

Orchestrates the #15 archival Job generator into the full pipeline:
eligibility → spawn Job → watch Job → verify → ``DELETE /analyses/{id}``.

**Actor (issue #78):** this pipeline no longer runs inside the operator
process. It is library code executed by the storage-prune CronJob
(``deploy/storage-cronjob.yaml``) via the entrypoint
:mod:`openstudio_operator.prune_entrypoint` — the operator core keeps no
storage polling loop at all (its only remaining poller is the Module 1 SLA
watch, which doubles as the completion observer; the server offers no push).
The tick cadence is the CronJob's ``schedule`` (``*/10 * * * *`` — the same
600 s cadence the in-operator timer used). Everything below is unchanged
pipeline logic: the same eligibility ordering, the same dryRun gate (the
entrypoint builds ``OperatorConfig`` from the CR ``spec``, so ``spec.dryRun``
suppresses Job spawn / failed-Job cleanup / analysis delete exactly as
before), and the same CR-``.status`` anchors. The operator and the prune
CronJob may write the CR status concurrently — safe because
:class:`~openstudio_operator.status_store.StatusStore` is a 409-retried
read-modify-write over disjoint status maps.

**NOT a disk-watermark pruner (deliberate, #78 invariant):** deletion stays
anchored on bookkeeping — retention eligibility → verified archival →
``DELETE /analyses/{id}`` (the server-side cascade that rm-rf's the NFS asset
dirs). A watermark-driven deleter that touched NFS contents directly would
bypass the MongoDB records and could destroy un-archived analyses; it is out
of scope by design (see docs/architecture-plan.md Module 4).

THE CARDINAL RULE (D09, acceptance): an analysis is NEVER deleted unless
its archival Job succeeded verification. The only path to
:meth:`OpenStudioClient.delete_analysis` runs through a ``verified_at``
record that is written exclusively when the Job's ``Complete`` condition is
observed — a Completed Job IS the verified upload (#15's ``rclone check``
size+hash gate aborts nonzero on any mismatch). Pending/failed/absent Jobs
never delete; a failed Job RETAINS the analysis and clears the in-flight
marker so a fresh spawn may retry.

Eligibility — ``status == "completed"`` (terminal state; every other state
is untouched) for longer than ``storagePolicy.retentionDays``:

* completion clock = the analysis doc's ``updated_at`` (from the light
  ``GET /analyses.json`` raw docs) — Mongoid stamps ``updated_at`` on the
  save that performs the ``completed`` transition and a completed doc is
  quiescent afterwards, so it is the closest completion proxy the light
  endpoint carries. ``created_at`` (the fallback when ``updated_at`` is
  unusable) would count queue wait + runtime toward retention and delete
  fresher completions; missing both → skip (cannot judge → never delete).
* age is compared ``>=`` retention: the exact boundary counts as elapsed,
  and ``retentionDays: 0`` therefore makes every completed analysis
  immediately eligible — the owner's batch workflow.

Module gating: ``storagePolicy.archiveToS3`` false → the whole pipeline is
passive — no archival configured means no deletion ever (the cardinal rule
dominates: unverified data is never destroyed). Enabled but
backend/bucket/secretRef incomplete → idle with a warning (``build_archival_job``
would reject the policy anyway). ``purgeCompletedNFSFiles`` false → archival
and verification still run, but the DELETE step is permanently withheld —
verified archives accumulate, the server keeps its documents.

In-flight tracking (D04): ``status.archivedAnalyses`` keyed by analysis id
is the pipeline's only memory — see
:class:`~openstudio_operator.status_store.ArchivedAnalysisRecord` for the
in-flight/verified/dry-run-marker shapes. Job watching reads the K8s batch
API (Job ``status.conditions``: ``Complete``/``Failed`` with status
``True``; neither → still running). Restart-safe by construction: a fresh
operator process reads the persisted record, recomputes the deterministic
Job name (:func:`archival_job_name`) and resumes WATCHING rather than
respawning.

Retry cadence: a failed (or vanished-mid-flight) Job emits a Warning Event,
clears its marker, and leaves the failed Job object in place for forensics;
the NEXT retention tick's spawn path deletes that Job (freeing the
deterministic name — this is what makes respawn idempotent: bounded object
count, never suffix-litter) and spawns a fresh one. Retries are unbounded
but tick-spaced (the CronJob schedule, 600 s by default); in-Job attempts
are already capped at 4 by ``backoffLimit``. Phase B (spawning) consults
the tracked-set snapshot from BEFORE phase A (reconciliation) mutated
anything, so every reconcile decision — failure clearing included — takes
effect for spawning on the next tick.

Datapoint trees: the Job archives ``data_points/{id}`` trees alongside the
analysis tree, so their ids come from ``GET /data_points.json`` (heavy —
the contract marks it escalation-only for the SLA poll; here it is fetched
at most once per tick and ONLY when a spawn is actually due, i.e. behind
the retention gate, not per poll).

dryRun (D11): Job spawn AND delete are suppressed with dry-run-marked
Events. Tracking still proceeds — a marker record (no ``jobName``) is
written so the suppression is once-per-analysis and the state machine is
observable in the CR; the delete suppression Event fires once, on the
verification tick, and stays silent afterwards (the persisted verified
record is the observable; per-tick Events would storm). Flipping dryRun
off: the marker is cleared by the next reconcile and real spawning starts
the tick after.

Ordering (D12, same accepted races as #8/#11): Job create → record write —
a lost record write is healed by the spawn path's read-first adopt logic
(deterministic name); delete → prune — a lost prune is healed by the
vanish-reconcile path. NFS bytes reclaimed are intentionally NOT
exposed as a Prometheus counter: neither the Job status nor the API
exposes a byte figure at delete time, and #50 removed the prior
``STORAGE_FREED_BYTES`` counter (#16's choice to never increment it left
a permanently-zero metric, which read as 'zero bytes ever freed'). A
future rclone-stats parse or pre/post ``du`` on the NFS tree could
reintroduce a real, sourced counter — the audit doc's Appendix D is the
canonical home for that decision when it lands.

Failure handling (D12): REST/kube/status-store failures raise out of
:meth:`run_retention_tick`; the prune entrypoint logs the skip and exits 0
(skip-tick parity with the old kopf wrapper) and everything re-derives from
the persisted records on the next scheduled run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from kubernetes.client import ApiException

from openstudio_operator.archival import archival_job_name, build_archival_job
from openstudio_operator.config import OperatorConfig
from openstudio_operator.metrics import ANALYSES_ARCHIVED_TOTAL, ANALYSES_DELETED_TOTAL
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.status_store import (
    ArchivedAnalysisRecord,
    StatusStore,
)

logger = logging.getLogger(__name__)

#: ``(event_type, reason, message)`` sink — ``kopf.event`` in the operator
#: handlers, a CoreV1 Events emitter in the prune entrypoint. Declared here
#: (not imported from ``handlers.analysis_sla``) so this module carries no
#: dependency on the kopf handler package.
EventEmitter = Callable[[str, str, str], None]

ANALYSIS_ARCHIVAL_STARTED_EVENT = "AnalysisArchivalStarted"
ANALYSIS_ARCHIVAL_SUCCEEDED_EVENT = "AnalysisArchivalSucceeded"
ANALYSIS_ARCHIVAL_FAILED_EVENT = "AnalysisArchivalFailed"
ANALYSIS_DELETED_EVENT = "AnalysisDeleted"

_COMPLETED = "completed"
_JOB_COMPLETE = "Complete"
_JOB_FAILED = "Failed"

# Cache-only (D04): one client session per server URL, never operator state
# — same convention as every sibling handler.
_client_cache: dict[str, OpenStudioClient] = {}


@dataclass
class RetentionTickResult:
    """Outcome of one retention tick (spawn/verify/delete decisions made)."""

    #: Analysis ids whose archival Job was spawned (or adopt-written) this tick.
    spawned: list[str]
    #: Analysis ids whose Job verification was observed (or adopted) this tick.
    verified: list[str]
    #: Analysis ids deleted this tick (verified uploads only — the cardinal rule).
    deleted: list[str]


class BatchApi(Protocol):
    """Structural type of ``BatchV1Api`` as used here — tests fake exactly this."""

    def read_namespaced_job(self, name: str, namespace: str, **_: object) -> object: ...

    def create_namespaced_job(self, namespace: str, body: dict, **_: object) -> object: ...

    def delete_namespaced_job(self, name: str, namespace: str, **_: object) -> object: ...


def _get_client(server_url: str) -> OpenStudioClient:
    client = _client_cache.get(server_url)
    if client is None:
        client = OpenStudioClient(server_url)
        _client_cache[server_url] = client
    return client


def _completion_time(doc: Mapping, analysis_id: str) -> datetime | None:
    """Retention clock from a raw analysis doc: ``updated_at``, else ``created_at``.

    The client boundary has already normalized ``*_at`` values to tz-aware
    UTC datetimes; anything else (absent/null) is unusable. ``None`` means
    "cannot judge" — the analysis is skipped, never deleted (see module
    docstring for the field choice).
    """
    updated = doc.get("updated_at")
    if isinstance(updated, datetime):
        return updated
    created = doc.get("created_at")
    if isinstance(created, datetime):
        logger.warning(
            "analysis %s: no usable updated_at — retention clock falling back to created_at",
            analysis_id,
        )
        return created
    logger.warning(
        "analysis %s: neither updated_at nor created_at usable — retention skips it",
        analysis_id,
    )
    return None


def _eligible(doc: Mapping, analysis_id: str, *, now: datetime, retention: timedelta) -> bool:
    """Completed longer than ``retention`` ago (``>=``: boundary counts; 0 = immediate)."""
    if doc.get("status") != _COMPLETED:
        return False
    completed_at = _completion_time(doc, analysis_id)
    if completed_at is None:
        return False
    return now - completed_at >= retention


def _read_job(batch_api: BatchApi, name: str, namespace: str) -> object | None:
    """Read a Job; ``None`` on 404, raise anything else (skip-tick, D12)."""
    try:
        return batch_api.read_namespaced_job(name, namespace)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def _job_terminal_condition(job: object) -> str | None:
    """Complete"/"Failed" when the Job has reached that terminal condition, else ``None``."""
    conditions = getattr(getattr(job, "status", None), "conditions", None) or []
    for condition in conditions:
        if getattr(condition, "status", None) != "True":
            continue
        condition_type = getattr(condition, "type", None)
        if condition_type in (_JOB_COMPLETE, _JOB_FAILED):
            return str(condition_type)
    return None


def _datapoint_ids_for(client: OpenStudioClient) -> Callable[[str], list[str]]:
    """Lazy once-per-tick ``GET /data_points.json`` filtered by analysis id.

    The heavy poll happens only if some analysis actually needs a spawn this
    tick (the closure is built but not called otherwise) and at most once
    per tick regardless of how many spawns are due.
    """
    payload: list[dict] | None = None

    def ids_for(analysis_id: str) -> list[str]:
        nonlocal payload
        if payload is None:
            payload = client.get_datapoints_full()
        return [
            str(doc.get("_id"))
            for doc in payload
            if doc.get("_id") and str(doc.get("analysis_id") or "") == analysis_id
        ]

    return ids_for


def _fail_archival(
    store: StatusStore, analysis_id: str, job_name: str, detail: str, *, emit: EventEmitter
) -> None:
    """Job-failure posture: Warning Event, analysis RETAINED, marker cleared for retry."""
    emit(
        "Warning",
        ANALYSIS_ARCHIVAL_FAILED_EVENT,
        f"Analysis {analysis_id} archival failed — {detail}; analysis RETAINED, "
        f"retry scheduled next retention tick",
    )
    store.clear_archived_analysis(analysis_id)


def _delete_verified(
    client: OpenStudioClient,
    store: StatusStore,
    config: OperatorConfig,
    analysis_id: str,
    record: ArchivedAnalysisRecord,
    *,
    emit: EventEmitter,
    just_verified: bool,
) -> bool:
    """The delete step — reachable ONLY with a verified record (cardinal rule).

    ``DELETE /analyses/{id}`` is the server-side cascade that frees NFS
    assets + Mongo docs (contract file). Suppressed (analysis retained)
    under ``purgeCompletedNFSFiles`` false or ``spec.dryRun``; the dry-run
    suppression Event fires only on the verification tick (once), later
    ticks stay silent — the persisted verified record is the observable.
    Returns whether the delete executed.
    """
    if not config.storage_policy.purge_completed_nfs_files:
        logger.debug(
            "purgeCompletedNFSFiles false — verified analysis %s retained on the server",
            analysis_id,
        )
        return False
    message = (
        f"Analysis {analysis_id} deleted after verified archival to "
        f"{record.backend}:{record.bucket}/{analysis_id} — REST cascade frees NFS assets "
        f"+ Mongo documents"
    )
    if config.dry_run:
        if just_verified:
            message += " — delete suppressed (spec.dryRun)"
            emit("Normal", ANALYSIS_DELETED_EVENT, message)
        else:
            logger.debug("delete of verified analysis %s suppressed (spec.dryRun)", analysis_id)
        return False
    client.delete_analysis(analysis_id)
    ANALYSES_DELETED_TOTAL.inc()
    emit("Normal", ANALYSIS_DELETED_EVENT, message)
    store.clear_archived_analysis(analysis_id)  # post-deletion prune (eager)
    return True


def _reconcile_tracked(
    client: OpenStudioClient,
    store: StatusStore,
    config: OperatorConfig,
    analysis_id: str,
    record: ArchivedAnalysisRecord,
    *,
    completed_by_id: Mapping[str, Mapping],
    now: datetime,
    emit: EventEmitter,
    namespace: str,
    batch_api: BatchApi,
    result: RetentionTickResult,
) -> None:
    """Phase A: advance one tracked analysis's state machine by its Job's state."""
    if analysis_id not in completed_by_id:
        # Vanished from the API: our own delete whose prune write failed (the
        # D12 skip-and-retry lands here), or an out-of-band deletion — either
        # way there is nothing left to archive or delete. Retire the record.
        store.clear_archived_analysis(analysis_id)
        logger.info(
            "archivedAnalyses entry for %s retired (analysis no longer in the API)",
            analysis_id,
        )
        return
    if record.verified_at is not None:
        # Verified earlier (delete was withheld or failed) — retry the delete step.
        if _delete_verified(
            client, store, config, analysis_id, record, emit=emit, just_verified=False
        ):
            result.deleted.append(analysis_id)
        return
    if record.job_name is None:
        # Dry-run marker (D11): nothing to watch. Inert while dryRun holds;
        # once it lifts, clear so real spawning starts next tick.
        if config.dry_run:
            return
        store.clear_archived_analysis(analysis_id)
        logger.info(
            "dry-run archival marker for %s cleared (dryRun lifted) — real spawn next tick",
            analysis_id,
        )
        return
    job = _read_job(batch_api, record.job_name, namespace)
    if job is None:
        _fail_archival(
            store,
            analysis_id,
            record.job_name,
            "archival Job not found (deleted out-of-band or expired mid-flight)",
            emit=emit,
        )
        return
    condition = _job_terminal_condition(job)
    if condition is None:
        return  # still running — keep watching (in-flight dedup, restart-safe)
    if condition == _JOB_FAILED:
        # The failed Job object is left for forensics; the NEXT tick's spawn
        # path deletes it (freeing the deterministic name) and retries.
        _fail_archival(
            store, analysis_id, record.job_name, "archival Job failed (see Job pod logs)", emit=emit
        )
        return
    # Complete == VERIFIED upload (rclone check size+hash gate, #15).
    verified = ArchivedAnalysisRecord(
        backend=record.backend,
        bucket=record.bucket,
        verified_at=now,
        job_name=record.job_name,
        spawned_at=record.spawned_at,
    )
    store.set_archived_analysis(analysis_id, verified)
    ANALYSES_ARCHIVED_TOTAL.inc()
    result.verified.append(analysis_id)
    emit(
        "Normal",
        ANALYSIS_ARCHIVAL_SUCCEEDED_EVENT,
        f"Analysis {analysis_id} archival Job {record.job_name} completed verification "
        f"(size+hash) → {record.backend}:{record.bucket}/{analysis_id}",
    )
    if _delete_verified(client, store, config, analysis_id, verified, emit=emit, just_verified=True):
        result.deleted.append(analysis_id)


def _spawn_archival(
    client: OpenStudioClient,
    store: StatusStore,
    config: OperatorConfig,
    analysis_id: str,
    datapoint_ids_for: Callable[[str], list[str]],
    *,
    now: datetime,
    emit: EventEmitter,
    namespace: str,
    batch_api: BatchApi,
    result: RetentionTickResult,
) -> None:
    """Phase B: spawn (or adopt) the archival Job for one eligible analysis.

    ``datapoint_ids_for`` is the lazy once-per-tick heavy-poll closure — the
    adopt branches never build a manifest, so they never trigger the fetch.
    """
    policy = config.storage_policy
    job_name = archival_job_name(analysis_id)
    existing = _read_job(batch_api, job_name, namespace)
    if existing is not None:
        condition = _job_terminal_condition(existing)
        if condition == _JOB_COMPLETE:
            # Verified upload with no tracked record (record write lost after
            # a completed Job, or operator state wiped): adopt — the Job
            # itself is the proof, so the cardinal rule still holds.
            record = ArchivedAnalysisRecord(
                backend=policy.backend,
                bucket=policy.bucket,
                verified_at=now,
                job_name=job_name,
                spawned_at=now,
            )
            store.set_archived_analysis(analysis_id, record)
            ANALYSES_ARCHIVED_TOTAL.inc()
            result.verified.append(analysis_id)
            emit(
                "Normal",
                ANALYSIS_ARCHIVAL_SUCCEEDED_EVENT,
                f"Analysis {analysis_id} adopted already-completed archival Job "
                f"{job_name} as verified → {policy.backend}:{policy.bucket}/{analysis_id}",
            )
            if _delete_verified(
                client, store, config, analysis_id, record, emit=emit, just_verified=True
            ):
                result.deleted.append(analysis_id)
            return
        if condition is None:
            # In-flight Job with no record (record-write race): adopt it —
            # NEVER a second Job for the same analysis.
            store.set_archived_analysis(
                analysis_id,
                ArchivedAnalysisRecord(
                    backend=policy.backend,
                    bucket=policy.bucket,
                    job_name=job_name,
                    spawned_at=now,
                ),
            )
            logger.info("adopted in-flight archival Job %s for %s", job_name, analysis_id)
            return
        # Failed terminal Job occupying the deterministic name: remove it so a
        # fresh Job can spawn (this IS the next tick after the failure event).
        # D11 (#42): the cleanup delete is a cluster mutation — suppressed
        # under spec.dryRun; the failed Job stays for forensics and the
        # first real spawn tick after dryRun lifts performs the removal.
        if not config.dry_run:
            batch_api.delete_namespaced_job(job_name, namespace, propagation_policy="Foreground")
            logger.info("removed failed archival Job %s — respawning (retry)", job_name)

    datapoint_ids = datapoint_ids_for(analysis_id)
    manifest = build_archival_job(analysis_id, policy, namespace, datapoint_ids)
    dry_run = config.dry_run
    if not dry_run:
        batch_api.create_namespaced_job(namespace, manifest)
    # Mutate-then-anchor (D12 accepted race): a lost record write is healed by
    # this same read-first logic next tick. Dry-run markers carry no jobName.
    store.set_archived_analysis(
        analysis_id,
        ArchivedAnalysisRecord(
            backend=policy.backend,
            bucket=policy.bucket,
            job_name=None if dry_run else job_name,
            spawned_at=now,
        ),
    )
    result.spawned.append(analysis_id)
    message = (
        f"Analysis {analysis_id} completed beyond retentionDays={policy.retention_days} — "
        f"spawning archival Job {namespace}/{job_name} → "
        f"{policy.backend}:{policy.bucket}/{analysis_id} "
        f"({len(datapoint_ids)} datapoint trees)"
    )
    if dry_run:
        message += " — Job spawn suppressed (spec.dryRun)"
    emit("Normal", ANALYSIS_ARCHIVAL_STARTED_EVENT, message)


def run_retention_tick(
    client: OpenStudioClient,
    store: StatusStore,
    config: OperatorConfig,
    *,
    now: datetime,
    emit: EventEmitter,
    namespace: str,
    batch_api: BatchApi,
) -> RetentionTickResult:
    """One retention tick: reconcile tracked archives, then spawn newly due ones.

    Pure function of its arguments plus live server/CR/Job state — no
    in-memory pipeline state (D04); ``status.archivedAnalyses`` is the only
    memory. Phase A walks every tracked record; phase B spawns for eligible
    analyses NOT in the pre-reconcile tracked snapshot, so phase-A decisions
    (failure clears, dry-run marker clears) take effect for spawning on the
    NEXT tick. Raises on API/status-store failure so the caller skips the
    tick (D12). ``batch_api`` is the injection seam for tests.
    """
    result = RetentionTickResult(spawned=[], verified=[], deleted=[])
    policy = config.storage_policy
    if not policy.archive_to_s3:
        logger.debug(
            "storagePolicy.archiveToS3 is false — retention pipeline passive "
            "(no archival configured ⇒ no deletion ever, D09 cardinal rule)"
        )
        return result
    if not (policy.backend and policy.bucket and policy.secret_ref):
        logger.warning(
            "storagePolicy archival enabled but backend/bucket/secretRef incomplete — "
            "storage pruner idle this tick"
        )
        return result

    completed_by_id = {
        str(doc.get("_id") or ""): doc
        for doc in client.list_analyses()
        if doc.get("_id") and doc.get("status") == _COMPLETED
    }
    tracked = store.get_archived_analyses()  # snapshot BEFORE reconcile mutations

    for analysis_id, record in tracked.items():
        _reconcile_tracked(
            client,
            store,
            config,
            analysis_id,
            record,
            completed_by_id=completed_by_id,
            now=now,
            emit=emit,
            namespace=namespace,
            batch_api=batch_api,
            result=result,
        )

    retention = timedelta(days=policy.retention_days)
    due = [
        analysis_id
        for analysis_id, doc in completed_by_id.items()
        if analysis_id not in tracked and _eligible(doc, analysis_id, now=now, retention=retention)
    ]
    if due:
        datapoint_ids_for = _datapoint_ids_for(client)
        for analysis_id in due:
            _spawn_archival(
                client,
                store,
                config,
                analysis_id,
                datapoint_ids_for,
                now=now,
                emit=emit,
                namespace=namespace,
                batch_api=batch_api,
                result=result,
            )
    return result
