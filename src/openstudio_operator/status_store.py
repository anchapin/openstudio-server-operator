"""CR ``.status`` durable-store helper (D04) — issue #7.

Every handler reads and mutates the OpenStudioClusterManager ``.status``
subresource through :class:`StatusStore`; no direct status patches belong in
handler code. The persisted CR is the single source of truth — the store
re-reads before every write, and anything retained in memory anywhere in the
operator is a cache at best (the ``cache`` property here is invalidated on
write and must never be treated as authoritative).

Conflict safety: mutations are read-modify-write cycles — GET the status
subresource, compute a JSON merge patch, PATCH it — and on HTTP 409 the whole
cycle restarts with the patch recomputed from a fresh read, so a mutation is
a pure function of its arguments, never of state captured on an earlier
attempt. Retries are bounded and end in :class:`StatusStoreConflictError`;
callers then skip the tick and retry naturally on the next poll (idempotency
comes from the status anchors, per D12).

Transport mechanics — NOT operator policy; policy values (timeouts,
retention, intervals) live in the CRD spec / ``config.py`` per AGENTS.md:
    * ``MAX_CONFLICT_RETRIES`` — bound on 409 re-read/re-patch cycles.
    * ``_BACKOFF_BASE_SECONDS`` — jittered exponential backoff between retries.

Timestamps are tz-aware UTC ``datetime`` in Python and ISO-8601 strings only
at the API boundary. This mirrors the ``_parse_timestamp`` convention of
``openstudio_client``; that symbol is private there, so a small documented
helper is duplicated here — keep the two in sync.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kubernetes.client import ApiException, CustomObjectsApi

GROUP = "energy.nrel.gov"
VERSION = "v1alpha1"
PLURAL = "openstudioclustermanagers"

SOFT_STOPS = "softStops"
REQUEUES = "requeues"
STARTED_SINCE = "startedSince"
ARCHIVED_ANALYSES = "archivedAnalyses"
LAST_RECYCLE_AT = "lastRecycleAt"
LAST_WEB_BACKGROUND_RESTART_AT = "lastWebBackgroundRestart"

MERGE_PATCH_CONTENT_TYPE = "application/merge-patch+json"

MAX_CONFLICT_RETRIES = 5
_BACKOFF_BASE_SECONDS = 1.0


class StatusStoreError(Exception):
    """Status-store failure: corrupt stored values or exhausted retries."""


class StatusStoreConflictError(StatusStoreError):
    """The status patch kept conflicting (409) past MAX_CONFLICT_RETRIES."""


def _sleep(seconds: float) -> None:
    """Separate seam so tests can record backoff instead of sleeping."""
    time.sleep(seconds)


def _conflict_backoff(attempt: int) -> float:
    return _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) * (0.5 + random.random())


def _to_utc(value: datetime) -> datetime:
    """Coerce a datetime to tz-aware UTC; naive input is assumed UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_utc(value: Any, context: str) -> datetime:
    """Parse an ISO-8601 timestamp at the API boundary; always tz-aware UTC.

    Accepts ``Z`` suffixes, zone offsets and fractional seconds; naive input
    is assumed UTC. Mirrors the (private) ``_parse_timestamp`` convention of
    ``openstudio_client`` — keep both in sync.
    """
    if not isinstance(value, str):
        raise StatusStoreError(f"{context}: expected ISO-8601 string, got {type(value).__name__}")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise StatusStoreError(f"{context}: unparseable timestamp {value!r}") from exc
    return _to_utc(parsed)


def _required(raw: Mapping[str, Any], key: str, context: str) -> Any:
    if key not in raw:
        raise StatusStoreError(f"{context}: missing required field {key!r}")
    return raw[key]


@dataclass(frozen=True)
class SoftStopRecord:
    """Value of ``status.softStops[analysis_id]`` — a soft stop the operator issued.

    The optional escalation fields (#9) are the one-shot marker for the
    Kubernetes-side grace-wait/eviction escalation: ``escalated_at is not
    None`` means the analysis was already escalated and must never be
    escalated again. They stay ``None`` for plain anchors, and anchors
    persisted before #9 (no such keys) parse unchanged — the softStops map
    is ``x-kubernetes-preserve-unknown-fields`` in the CRD, so no schema
    change is involved.
    """

    issued_at: datetime
    outcome: str
    escalated_at: datetime | None = None
    escalation_outcome: str | None = None

    def to_dict(self) -> dict[str, Any]:
        encoded: dict[str, Any] = {"issuedAt": _to_utc(self.issued_at).isoformat(), "outcome": self.outcome}
        if self.escalated_at is not None:
            encoded["escalatedAt"] = _to_utc(self.escalated_at).isoformat()
        if self.escalation_outcome is not None:
            encoded["escalationOutcome"] = self.escalation_outcome
        return encoded

    @classmethod
    def from_dict(cls, raw: Any, context: str) -> SoftStopRecord:
        if not isinstance(raw, Mapping):
            raise StatusStoreError(f"{context}: expected object, got {type(raw).__name__}")
        escalated_at = raw.get("escalatedAt")
        return cls(
            issued_at=_parse_utc(_required(raw, "issuedAt", context), f"{context}.issuedAt"),
            outcome=str(_required(raw, "outcome", context)),
            escalated_at=None if escalated_at is None else _parse_utc(escalated_at, f"{context}.escalatedAt"),
            escalation_outcome=None if raw.get("escalationOutcome") is None else str(raw["escalationOutcome"]),
        )


@dataclass(frozen=True)
class RequeueRecord:
    """Value of ``status.requeues[datapoint_id]`` — auto-requeue bookkeeping."""

    count: int
    last_requeued_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {"count": self.count, "lastRequeuedAt": _to_utc(self.last_requeued_at).isoformat()}

    @classmethod
    def from_dict(cls, raw: Any, context: str) -> RequeueRecord:
        if not isinstance(raw, Mapping):
            raise StatusStoreError(f"{context}: expected object, got {type(raw).__name__}")
        return cls(
            count=int(_required(raw, "count", context)),
            last_requeued_at=_parse_utc(
                _required(raw, "lastRequeuedAt", context), f"{context}.lastRequeuedAt"
            ),
        )


@dataclass(frozen=True)
class ArchivedAnalysisRecord:
    """Value of ``status.archivedAnalyses[analysis_id]`` — retention-pipeline state (#16).

    The map is the retention pipeline's ONLY memory (D04) and carries two
    record shapes, told apart by the optional fields (the CRD map is
    ``x-kubernetes-preserve-unknown-fields``, so no schema change is
    involved):

    * in-flight — ``verified_at is None``: an archival Job is being watched
      (``job_name``/``spawned_at`` set), OR a dry-run marker (D11 — spawn
      suppressed, hence ``job_name is None``: nothing to watch; the record
      exists so the suppression is observable and once-per-analysis).
    * verified — ``verified_at is not None``: the Job's ``Complete``
      condition was observed (the verified-upload gate) and the server-side
      delete may proceed. ``job_name``/``spawned_at`` survive the
      transition as forensics.

    Records persisted before #7 gained in-flight support (backend/bucket/
    verifiedAt only) parse as verified records unchanged.
    """

    backend: str
    bucket: str | None
    verified_at: datetime | None = None
    job_name: str | None = None
    spawned_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        encoded: dict[str, Any] = {"backend": self.backend, "bucket": self.bucket}
        if self.verified_at is not None:
            encoded["verifiedAt"] = _to_utc(self.verified_at).isoformat()
        if self.job_name is not None:
            encoded["jobName"] = self.job_name
        if self.spawned_at is not None:
            encoded["spawnedAt"] = _to_utc(self.spawned_at).isoformat()
        return encoded

    @classmethod
    def from_dict(cls, raw: Any, context: str) -> ArchivedAnalysisRecord:
        if not isinstance(raw, Mapping):
            raise StatusStoreError(f"{context}: expected object, got {type(raw).__name__}")
        verified_at = raw.get("verifiedAt")
        spawned_at = raw.get("spawnedAt")
        job_name = raw.get("jobName")
        return cls(
            backend=str(_required(raw, "backend", context)),
            bucket=raw.get("bucket"),
            verified_at=(
                None if verified_at is None else _parse_utc(verified_at, f"{context}.verifiedAt")
            ),
            job_name=None if job_name is None else str(job_name),
            spawned_at=(
                None if spawned_at is None else _parse_utc(spawned_at, f"{context}.spawnedAt")
            ),
        )


class StatusStore:
    """Typed get/set/prune over the OSCM ``.status`` subresource (D04).

    One instance per namespace/CR; handlers construct it once (or use
    :meth:`in_cluster`) and share it — never patch ``.status`` directly.
    """

    def __init__(self, namespace: str, name: str, custom_api: CustomObjectsApi) -> None:
        self._namespace = namespace
        self._name = name
        self._api = custom_api
        self._cache: dict[str, Any] | None = None

    @classmethod
    def in_cluster(cls, namespace: str, name: str) -> StatusStore:
        """Build a store using the operator pod's service account."""
        from kubernetes import config as kube_config

        kube_config.load_incluster_config()
        return cls(namespace, name, CustomObjectsApi())

    @property
    def cache(self) -> dict[str, Any] | None:
        """Last status observed by a read — cache-only, invalidated on write.

        Never a source of truth: handlers may consult it as a hint within a
        tick, but decisions must be based on fresh reads (D04).
        """
        return self._cache

    # --- Transport core -----------------------------------------------------

    def _read_status(self) -> dict[str, Any]:
        obj = self._api.get_namespaced_custom_object_status(
            GROUP, VERSION, self._namespace, PLURAL, self._name
        )
        status = obj.get("status") or {}
        self._cache = status
        return status

    def _mutate(self, build_patch: Callable[[dict[str, Any]], dict[str, Any] | None]) -> None:
        """Read-modify-write with conflict-safe retry on 409.

        ``build_patch`` receives the freshly read status dict (which it must
        not mutate) and returns the merge-patch body to apply, or ``None`` to
        skip the write when nothing changed. On 409 the cycle restarts — fresh
        GET, patch recomputed from that read — so ``build_patch`` must derive
        its result purely from its arguments. JSON merge patch (RFC 7386)
        never deletes absent keys, so removals (pruning, clearing a scalar)
        must be expressed as explicit ``None`` values.
        """
        conflict: ApiException | None = None
        for attempt in range(1, MAX_CONFLICT_RETRIES + 1):
            status = self._read_status()
            patch = build_patch(status)
            if patch is None:
                return
            try:
                self._api.patch_namespaced_custom_object_status(
                    GROUP,
                    VERSION,
                    self._namespace,
                    PLURAL,
                    self._name,
                    body=patch,
                    _content_type=MERGE_PATCH_CONTENT_TYPE,
                )
            except ApiException as exc:
                if exc.status != 409:
                    raise
                conflict = exc
                if attempt < MAX_CONFLICT_RETRIES:
                    _sleep(_conflict_backoff(attempt))
                continue
            self._cache = None
            return
        raise StatusStoreConflictError(
            f"status patch for {self._namespace}/{self._name} conflicted (409) "
            f"{MAX_CONFLICT_RETRIES} times"
        ) from conflict

    def _read_map(self, field: str) -> dict[str, Any]:
        raw = self._read_status().get(field) or {}
        if not isinstance(raw, Mapping):
            raise StatusStoreError(f"status.{field}: expected object, got {type(raw).__name__}")
        return dict(raw)

    def _set_map_entry(self, field: str, key: str, encoded: Any) -> None:
        def build_patch(status: dict[str, Any]) -> dict[str, Any] | None:
            current_field = status.get(field)
            current = current_field.get(key) if isinstance(current_field, Mapping) else None
            if current == encoded:
                return None
            return {"status": {field: {key: encoded}}}

        self._mutate(build_patch)

    def _get_scalar(self, field: str) -> datetime | None:
        raw = self._read_status().get(field)
        return None if raw is None else _parse_utc(raw, f"status.{field}")

    def _set_scalar(self, field: str, when: datetime | None) -> None:
        encoded = None if when is None else _to_utc(when).isoformat()

        def build_patch(status: dict[str, Any]) -> dict[str, Any] | None:
            if status.get(field) == encoded:
                return None
            return {"status": {field: encoded}}

        self._mutate(build_patch)

    # --- softStops ----------------------------------------------------------

    def get_soft_stops(self) -> dict[str, SoftStopRecord]:
        return {
            key: SoftStopRecord.from_dict(val, f"status.{SOFT_STOPS}[{key!r}]")
            for key, val in self._read_map(SOFT_STOPS).items()
        }

    def get_soft_stop(self, analysis_id: str) -> SoftStopRecord | None:
        raw = self._read_map(SOFT_STOPS).get(analysis_id)
        return (
            None
            if raw is None
            else SoftStopRecord.from_dict(raw, f"status.{SOFT_STOPS}[{analysis_id!r}]")
        )

    def set_soft_stop(self, analysis_id: str, record: SoftStopRecord) -> None:
        self._set_map_entry(SOFT_STOPS, analysis_id, record.to_dict())

    def mark_soft_stop_escalated(self, analysis_id: str, when: datetime, outcome: str) -> None:
        """Stamp the one-shot escalation marker onto an existing anchor (#9).

        Merges ``escalatedAt``/``escalationOutcome`` into whatever the anchor
        currently holds (preserving ``issuedAt``/``outcome`` read fresh inside
        the conflict-safe cycle) rather than overwriting a whole record the
        caller snapshotted earlier. A vanished anchor raises: only this
        operator writes ``softStops``, so its disappearance mid-mutation is
        corruption, not a race to swallow.
        """
        encoded_at = _to_utc(when).isoformat()

        def build_patch(status: dict[str, Any]) -> dict[str, Any] | None:
            entries = status.get(SOFT_STOPS)
            current = entries.get(analysis_id) if isinstance(entries, Mapping) else None
            if not isinstance(current, Mapping):
                raise StatusStoreError(
                    f"status.{SOFT_STOPS}[{analysis_id!r}]: anchor vanished before escalation marker"
                )
            encoded = dict(current)
            encoded["escalatedAt"] = encoded_at
            encoded["escalationOutcome"] = outcome
            if encoded == current:
                return None
            return {"status": {SOFT_STOPS: {analysis_id: encoded}}}

        self._mutate(build_patch)

    def clear_soft_stop(self, analysis_id: str) -> None:
        """Delete ``status.softStops[analysis_id]`` — anchor-retirement prune.

        Counterpart to :meth:`set_soft_stop` for the SLA monitor's grace
        phase (#9): an anchored analysis that left ``started`` (completed,
        post-processing, …) or vanished from the API has no further
        soft-stop/escalation business, so its anchor — escalation marker
        included — is dropped. Called only while the whole SLA module is
        active; with ``analysisPolicy.autoSoftStop`` false the module is
        passive and anchors persist untouched. Idempotent: clearing an
        absent key writes nothing.
        """
        self._set_map_entry(SOFT_STOPS, analysis_id, None)

    # --- requeues ------------------------------------------------------------

    def get_requeues(self) -> dict[str, RequeueRecord]:
        return {
            key: RequeueRecord.from_dict(val, f"status.{REQUEUES}[{key!r}]")
            for key, val in self._read_map(REQUEUES).items()
        }

    def get_requeue(self, datapoint_id: str) -> RequeueRecord | None:
        raw = self._read_map(REQUEUES).get(datapoint_id)
        return (
            None
            if raw is None
            else RequeueRecord.from_dict(raw, f"status.{REQUEUES}[{datapoint_id!r}]")
        )

    def set_requeue(self, datapoint_id: str, record: RequeueRecord) -> None:
        self._set_map_entry(REQUEUES, datapoint_id, record.to_dict())

    # --- startedSince --------------------------------------------------------

    def get_started_since_map(self) -> dict[str, datetime]:
        return {
            key: _parse_utc(val, f"status.{STARTED_SINCE}[{key!r}]")
            for key, val in self._read_map(STARTED_SINCE).items()
        }

    def get_started_since(self, datapoint_id: str) -> datetime | None:
        raw = self._read_map(STARTED_SINCE).get(datapoint_id)
        return None if raw is None else _parse_utc(raw, f"status.{STARTED_SINCE}[{datapoint_id!r}]")

    def set_started_since(self, datapoint_id: str, when: datetime) -> None:
        self._set_map_entry(STARTED_SINCE, datapoint_id, _to_utc(when).isoformat())

    def clear_started_since(self, datapoint_id: str) -> None:
        """Delete ``status.startedSince[datapoint_id]`` — departure prune.

        Counterpart to :meth:`set_started_since` for the datapoint watchdog
        (#10): a datapoint that leaves the ``started`` set must lose its
        operator clock so a later re-entry starts fresh. Deliberately does
        NOT touch ``status.requeues`` — the requeue budget outlives
        departure (a requeued datapoint legitimately leaves ``started``
        while it sits on the ``requeued`` queue; wiping its budget then
        would unbound the requeue loop), so the coupled :meth:`prune` is
        the wrong tool here. Idempotent: clearing an absent key writes
        nothing (merge patch encodes the removal as an explicit ``None``).
        """
        self._set_map_entry(STARTED_SINCE, datapoint_id, None)

    # --- archivedAnalyses ----------------------------------------------------

    def get_archived_analyses(self) -> dict[str, ArchivedAnalysisRecord]:
        return {
            key: ArchivedAnalysisRecord.from_dict(val, f"status.{ARCHIVED_ANALYSES}[{key!r}]")
            for key, val in self._read_map(ARCHIVED_ANALYSES).items()
        }

    def get_archived_analysis(self, analysis_id: str) -> ArchivedAnalysisRecord | None:
        raw = self._read_map(ARCHIVED_ANALYSES).get(analysis_id)
        return (
            None
            if raw is None
            else ArchivedAnalysisRecord.from_dict(
                raw, f"status.{ARCHIVED_ANALYSES}[{analysis_id!r}]"
            )
        )

    def set_archived_analysis(self, analysis_id: str, record: ArchivedAnalysisRecord) -> None:
        self._set_map_entry(ARCHIVED_ANALYSES, analysis_id, record.to_dict())

    def clear_archived_analysis(self, analysis_id: str) -> None:
        """Delete ``status.archivedAnalyses[analysis_id]`` (#16).

        The retention pipeline's three exits all land here: after the
        server-side delete succeeds (post-deletion prune — the analysis
        leaving the API would also prune it via :meth:`prune`, but the
        pipeline prunes eagerly), when a failed archival Job clears the
        in-flight marker so the deterministic Job name can be reused for a
        fresh spawn, and when a tracked analysis vanishes from the API
        (delete-then-failed-prune race, or out-of-band deletion).
        Idempotent: clearing an absent key writes nothing.
        """
        self._set_map_entry(ARCHIVED_ANALYSES, analysis_id, None)

    # --- scalars ---------------------------------------------------------------

    def get_last_recycle_at(self) -> datetime | None:
        return self._get_scalar(LAST_RECYCLE_AT)

    def set_last_recycle_at(self, when: datetime | None) -> None:
        self._set_scalar(LAST_RECYCLE_AT, when)

    def get_last_web_background_restart_at(self) -> datetime | None:
        return self._get_scalar(LAST_WEB_BACKGROUND_RESTART_AT)

    def set_last_web_background_restart_at(self, when: datetime | None) -> None:
        self._set_scalar(LAST_WEB_BACKGROUND_RESTART_AT, when)

    # --- pruning ---------------------------------------------------------------

    def prune(
        self,
        *,
        live_analysis_ids: Iterable[str] | None = None,
        live_datapoint_ids: Iterable[str] | None = None,
    ) -> None:
        """Drop map entries whose analysis/datapoint id is not in the live set.

        Callers pass the ids that still need tracking (how "live" is defined
        is each handler's domain knowledge); every keyed entry outside that
        set is deleted — covering both completion (id left the unfinished
        set) and disappearance (id no longer returned by the API). ``None``
        (the default) leaves that id-space untouched, so each handler prunes
        only the maps it has fresh knowledge for.
        """
        analysis_live = None if live_analysis_ids is None else set(live_analysis_ids)
        datapoint_live = None if live_datapoint_ids is None else set(live_datapoint_ids)
        scopes: list[tuple[str, set[str] | None]] = [
            (SOFT_STOPS, analysis_live),
            (ARCHIVED_ANALYSES, analysis_live),
            (REQUEUES, datapoint_live),
            (STARTED_SINCE, datapoint_live),
        ]

        def build_patch(status: dict[str, Any]) -> dict[str, Any] | None:
            deletions: dict[str, Any] = {}
            for field, live in scopes:
                if live is None:
                    continue
                entries = status.get(field)
                if not isinstance(entries, Mapping):
                    continue
                dead = {key: None for key in entries if key not in live}
                if dead:
                    deletions[field] = dead
            return {"status": deletions} if deletions else None

        self._mutate(build_patch)
