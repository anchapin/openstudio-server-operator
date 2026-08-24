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
at the API boundary. The parsing itself lives in
:mod:`openstudio_operator._time` (``parse_iso_utc`` — issue #174, D12); this
module's ``_parse_utc`` is a factory-built closure (``_time.utc_parser``,
issue #506) that re-raises ``ValueError`` as
:class:`StatusStoreError` with a per-call-site ``context`` prefix.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kubernetes.client import ApiException, CustomObjectsApi

from ._constants import CRD_GROUP, CRD_PLURAL, CRD_VERSION
from ._k8s import MERGE_PATCH_CONTENT_TYPE
from ._retry import _sleep
from ._time import utc_parser
from .metrics import (
    KUBE_API_REQUEST_DURATION_SECONDS,
    STATUS_CONFLICT_RETRIES_EXHAUSTED_TOTAL,
    STATUS_CONFLICTS_TOTAL,
    STATUS_MAP_CAPS_TOTAL,
    STATUS_MAP_ENTRIES,
    observe_duration,
)

# Issue #495 — the canonical CRD identity lives in ``_constants``
# (``CRD_GROUP``/``CRD_VERSION``/``CRD_PLURAL``/``CRD_SPEC``). These
# historical names remain as re-exports: the status-RMW call sites below
# and the pre-#495 test imports keep working without churn.
GROUP = CRD_GROUP
VERSION = CRD_VERSION
PLURAL = CRD_PLURAL

SOFT_STOPS = "softStops"
REQUEUES = "requeues"
STARTED_SINCE = "startedSince"
ARCHIVED_ANALYSES = "archivedAnalyses"
LAST_RECYCLE_AT = "lastRecycleAt"
LAST_WEB_BACKGROUND_RESTART_AT = "lastWebBackgroundRestart"
STALL_WINDOW_STARTED_AT = "stallWindowStartedAt"
WEB_BACKGROUND_INEFFECTIVE_RESTARTS = "webBackgroundIneffectiveRestarts"
DEFERRED_EVENTS = "deferredEvents"

# Issue #585 — the merge-patch media type's canonical home is ``_k8s``
# (every production consumer issues a Kubernetes API PATCH; the historical
# definition here made the shared utility ``_k8s`` import from this domain
# module). Imported above; the CR ``.status`` subresource patch below uses it.
MAX_CONFLICT_RETRIES = 5
_BACKOFF_BASE_SECONDS = 1.0

# Issue #171 — defensive cap on the four CR .status maps. The CRD schema
# accepts unbounded maps (every field is ``x-kubernetes-preserve-unknown-fields``
# with no ``maxProperties``), so an actor with ``update`` on the status
# subresource can grow any of the four maps to etd's 1.5 MB object-size limit;
# the operator then reads + JSON-parses + merge-patches the full map on every
# timer tick. The cap is enforced OPERATOR-SIDE in :meth:`StatusStore._set_map_entry`
# so the operator's own writes are bounded AND a malicious/buggy external write
# is clipped on the next operator-owned RMW. The cap value (10_000) is sized
# so an average ~100-byte entry keeps the largest map at ~1 MB — well under
# etcd's 1.5 MB default and enough headroom for the other three maps plus
# CR-level metadata.
STATUS_MAP_MAX_ENTRIES = 10_000

#: Issue #489 — the four capped ``.status`` maps, in no particular order.
#: The tuple drives the per-map size-gauge stamp in ``_read_status`` so the
#: vocabulary of ``STATUS_MAP_ENTRIES{map_name}`` is the exact ``.status``
#: map key set (same vocabulary as ``STATUS_MAP_CAPS_TOTAL{map_name}``).
_STATUS_MAP_FIELDS = (SOFT_STOPS, REQUEUES, STARTED_SINCE, ARCHIVED_ANALYSES)

#: Issue #171 — event reason for the cap-eviction Warning Event. A single
#: short string so dashboard filters / alert rules can match it.
STATUS_MAP_CAPPED_EVENT = "StatusMapCapped"

#: Issue #648 — live-anchor protection registry for the cap-eviction path
#: (defense-in-depth). Keyed ``(namespace, name, map field)`` → the set of
#: ids the watchdog's hygiene pass last observed ALIVE in a live server
#: view; the eviction in :meth:`StatusStore._set_map_entry` prefers
#: UNPROTECTED keys so a still-live id's D06 anchor (its ``requeues``
#: budget, its ``startedSince`` clock) is never the arbitrary lexicographic
#: victim at the cap. Deliberately a plain module-level dict (NOT a
#: :class:`~openstudio_operator._cr_cache.PerCRCache` entry): it is
#: advisory-only eviction-ordering state — never a source of truth (D04)
#: — whose worst-case staleness (a CR deleted mid-flight) self-heals on
#: the next heavy tick of whatever CR holds the name next, and whose size
#: is bounded by the live id count of the last registration. The watchdog
#: re-registers each map's live set on every hygiene pass
#: (:meth:`StatusStore.protect_anchor_keys`).
_PROTECTED_ANCHOR_KEYS: dict[tuple[str, str, str], frozenset[str]] = {}


def reset_protected_anchor_keys() -> None:
    """Drop every registered live-anchor protection set (test seam).

    Production never needs this — each hygiene pass replaces its CR's
    entries wholesale — but the test suite registers protections against
    shared ``(namespace, name)`` fixtures and must not leak them into
    unrelated cap-eviction cases. Mirrors the ``set_event_sink(None)``
    restore pattern below.
    """
    _PROTECTED_ANCHOR_KEYS.clear()

#: ``(namespace, name, reason, message)`` — kopf-backed sink in production,
#: recorder in tests. The StatusStore instance knows its own ``namespace``/
#: ``name`` and calls the sink with the CR body so the production sink can
#: emit a kopf event on the right CR. The default is a no-op so the
#: status_store is usable as a pure library (e.g. from test fixtures that
#: don't care about Events). The ``handlers/__init__.py`` entrypoint
#: installs the production kopf-backed sink at operator startup; tests
#: install a recorder via :func:`set_event_sink`.
#:
#: Issue #496 scope note: this alias is deliberately LOCAL (not moved to
#: :mod:`openstudio_operator.events` with ``TickEmitter``/``EventSink``).
#: It is used only inside this module (the ``set_event_sink`` seam's
#: annotations), its arity is genuinely distinct (namespace/name as separate
#: strings — the sink constructs the object itself, unlike
#: ``events.EventSink``'s leading CR-body dict), and the PUBLIC alias for
#: this exact seam already exists as
#: :data:`openstudio_operator.events_sinks.StatusEventSink`. Re-homing it
#: under yet another name would recreate the drift #496 collapses.
EmitStatusEvent = Callable[[str, str, str, str], None]


def _emit_status_map_event(_namespace: str, _name: str, _reason: str, _message: str) -> None:
    """Default no-op sink. Replaced by handlers package at operator startup.

    Kept as a module-level symbol (not a closure inside the method) so that
    the per-instance method-count and the test seam both agree on the same
    function name; the hot path stays a single attribute lookup on the
    module.
    """


_default_event_sink: EmitStatusEvent = _emit_status_map_event


def set_event_sink(sink: EmitStatusEvent | None) -> None:
    """Install/replace the map-cap event sink.

    ``None`` restores the default no-op. The handlers package installs the
    kopf-backed sink at operator startup; tests install a recorder via
    ``monkeypatch.setattr`` or by calling this directly. Idempotent.
    """
    global _emit_status_map_event
    _emit_status_map_event = sink if sink is not None else _default_event_sink


class StatusStoreError(Exception):
    """Status-store failure: corrupt stored values or exhausted retries."""


class StatusStoreConflictError(StatusStoreError):
    """The status patch kept conflicting (409) past MAX_CONFLICT_RETRIES."""


def _conflict_backoff(attempt: int) -> float:
    return _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) * (0.5 + random.random())


def _to_utc(value: datetime) -> datetime:
    """Coerce a datetime to tz-aware UTC; naive input is assumed UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


#: Domain-flavored parse seam (issue #506): the ``_time.utc_parser`` closure
#: rejects ``None`` and re-raises parse failures as
#: :class:`StatusStoreError` with the caller's ``context`` prefix — this
#: module's public exception contract.
_parse_utc = utc_parser(StatusStoreError)


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

    One instance per namespace/CR; handlers construct it once — passing the
    :class:`CustomObjectsApi` from
    :func:`openstudio_operator.singleton.operator_custom_objects_api` — and
    share it. Never patch ``.status`` directly.
    """

    def __init__(self, namespace: str, name: str, custom_api: CustomObjectsApi) -> None:
        self._namespace = namespace
        self._name = name
        self._api = custom_api
        self._cache: dict[str, Any] | None = None

    @property
    def cache(self) -> dict[str, Any] | None:
        """Last status observed by a read — cache-only, invalidated on write.

        Never a source of truth: handlers may consult it as a hint within a
        tick, but decisions must be based on fresh reads (D04).
        """
        return self._cache

    # --- Transport core -----------------------------------------------------

    def _read_status(self) -> dict[str, Any]:
        # Issue #488 — time the apiserver GET itself (the network surface);
        # the #489 gauge stamping below is pure computation.
        with observe_duration(KUBE_API_REQUEST_DURATION_SECONDS, verb="get"):
            obj = self._api.get_namespaced_custom_object_status(
                GROUP, VERSION, self._namespace, PLURAL, self._name
            )
        status = obj.get("status") or {}
        # Issue #489 — stamp the per-map size gauges at the single read site
        # every RMW cycle (inside ``_mutate``, post-409-retry-loop fresh GET)
        # and every typed getter lands on, so all four maps are stamped on
        # every read. ``.set()`` is idempotent, so a 409 burst re-stamping
        # per attempt is free; a corrupt non-mapping field is skipped (the
        # getter will raise on it — stamping 0 would under-report a map the
        # read is about to reject). The lead-time signal for the #171 cap:
        # the gauge trends toward STATUS_MAP_MAX_ENTRIES before the
        # post-hoc ``STATUS_MAP_CAPS_TOTAL`` + ``StatusMapCapped`` Event.
        for field in _STATUS_MAP_FIELDS:
            raw = status.get(field)
            if isinstance(raw, Mapping) or raw is None:
                STATUS_MAP_ENTRIES.labels(
                    namespace=self._namespace, name=self._name, map_name=field
                ).set(len(raw) if isinstance(raw, Mapping) else 0)
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
                # Issue #488 — time the apiserver PATCH itself; the 409
                # counting below stays per-attempt on the error side.
                with observe_duration(KUBE_API_REQUEST_DURATION_SECONDS, verb="patch"):
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
                # Issue #119 — count the conflict at the moment we observe it,
                # BEFORE the backoff sleep. A sustained burst reads as N+1
                # in a single tick. Issue #311 — labelled by (namespace, name)
                # so a multi-CR operator process can attribute the conflict
                # burst to the specific CR.
                STATUS_CONFLICTS_TOTAL.labels(namespace=self._namespace, name=self._name).inc()
                conflict = exc
                if attempt < MAX_CONFLICT_RETRIES:
                    _sleep(_conflict_backoff(attempt))
                continue
            self._cache = None
            return
        # Issue #119 — count exhaustion just before raising so an SRE alerting
        # on `rate(... _exhausted_total[5m]) > 0` can fire on "real"
        # (retry-budgeted-out) contention, distinct from per-attempt conflicts.
        # Issue #311 — labelled by (namespace, name) so a multi-CR operator
        # process can distinguish which CR is exhausting its retry budget.
        STATUS_CONFLICT_RETRIES_EXHAUSTED_TOTAL.labels(
            namespace=self._namespace, name=self._name
        ).inc()
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
        # Closure populated by `build_patch` on a successful eviction, read
        # after `_mutate` returns to emit the per-eviction observability
        # (counter + Warning Event) exactly once per actual cap hit. The
        # retry-stable emission is intentional: the counter is NOT bumped
        # per 409 attempt — see STATUS_MAP_CAPS_TOTAL docs in metrics.py.
        evicted_keys: list[str] = []

        def build_patch(status: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal evicted_keys
            current_field = status.get(field)
            is_mapping = isinstance(current_field, Mapping)
            current = current_field.get(key) if is_mapping else None

            # Issue #171 — defensive cap. When the map is at the cap AND the
            # call is actually adding a new value (encoded is not None,
            # value-changed), drop the oldest entries by sorted key to make
            # room. The key sort is a deterministic proxy for "oldest" —
            # the operator's keys are UUIDs, so the sort order is unique
            # but not age-aware. A map that is already at the cap but is
            # being re-written with the same value, or being cleared (None),
            # does not evict: the eviction is in the path that adds a new
            # entry, not in every write.
            #
            # Issue #648 — the eviction order is ANCHOR-AWARE: ids the
            # watchdog's hygiene pass last saw alive in a live server view
            # (``_PROTECTED_ANCHOR_KEYS``) are evicted only after every
            # unprotected candidate is exhausted, so the arbitrary
            # lexicographic victim is never a still-live zombie datapoint's
            # ``requeues`` budget (the D06 anchor whose silent loss re-arms
            # the unbounded requeue loop ``maxAutoRequeues`` bounds). The
            # protection is defense-in-depth behind the hygiene pass's own
            # prune — when EVERY candidate is protected the hard cap still
            # dominates (the etcd 1.5 MB bound is not negotiable) and the
            # per-eviction Event below names the protected losses.
            #
            # Merge-patch semantics (RFC 7386, mirrored by the API Server):
            # a dict at the field level is merged per-key, not replaced as a
            # whole. To remove a key we MUST send its full patch with None
            # (the same shape the existing ``prune`` uses for completion
            # exits). The new entry goes in as the usual ``{key: encoded}``
            # pair. The resulting patch is therefore ``{evicted: None, ...,
            # new_key: encoded}`` — every entry we want to keep PLUS the
            # explicit-null for each evicted key.
            if (
                encoded is not None
                and is_mapping
                and len(current_field) >= STATUS_MAP_MAX_ENTRIES
                and current != encoded
            ):
                num_to_drop = len(current_field) - STATUS_MAP_MAX_ENTRIES + 1
                protected = _PROTECTED_ANCHOR_KEYS.get(
                    (self._namespace, self._name, field), frozenset()
                )
                unprotected_first = [k for k in sorted(current_field) if k not in protected]
                protected_last = [k for k in sorted(current_field) if k in protected]
                evicted_keys = (unprotected_first + protected_last)[:num_to_drop]
                evicted_set = set(evicted_keys)
                patch_field: dict[str, Any] = {k: None for k in evicted_keys}
                for k, v in current_field.items():
                    if k not in evicted_set:
                        patch_field[k] = v
                patch_field[key] = encoded
                return {"status": {field: patch_field}}

            if current == encoded:
                return None
            return {"status": {field: {key: encoded}}}

        self._mutate(build_patch)

        # Per-eviction observability: emit AFTER the successful RMW so the
        # counter + Event fire exactly once per actual cap hit, not per 409
        # attempt. The eviction logic above is purely data (the new map
        # shape), so the side effects live here — outside the retry loop.
        # Issue #311 — labelled by (namespace, name, map_name) so a multi-CR
        # operator process can attribute the cap hit to the specific CR.
        if evicted_keys:
            STATUS_MAP_CAPS_TOTAL.labels(
                namespace=self._namespace, name=self._name, map_name=field
            ).inc()
            preview = ", ".join(repr(k) for k in evicted_keys[:5])
            if len(evicted_keys) > 5:
                preview += f" (+{len(evicted_keys) - 5} more)"
            message = (
                f"status.{field} size capped at {STATUS_MAP_MAX_ENTRIES}: "
                f"dropped {len(evicted_keys)} entries ({preview}) to "
                f"make room for key {key!r}. Defensive cap (issue #171); "
                f"the underlying status map is the operator's only durable "
                f"state, and an unbounded map would amplify RMW cost on "
                f"every tick."
            )
            # Issue #648 — when every eviction candidate was a protected
            # live anchor, the hard cap forced a protected loss; say so
            # (the D06 anchor reset is exactly what the operator must
            # never do silently).
            protected = _PROTECTED_ANCHOR_KEYS.get(
                (self._namespace, self._name, field), frozenset()
            )
            protected_dropped = [k for k in evicted_keys if k in protected]
            if protected_dropped:
                message += (
                    f" WARNING: {len(protected_dropped)} evicted entry(ies) were "
                    f"live-anchor-protected (no unprotected candidate remained — "
                    f"the hard cap dominates; issue #648)."
                )
            _emit_status_map_event(
                self._namespace,
                self._name,
                STATUS_MAP_CAPPED_EVENT,
                message,
            )

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

    def _get_int(self, field: str) -> int:
        """Read an integer ``.status`` scalar; absent reads as ``0``.

        Issue #649 — the web_background ineffective-restart counter is the
        first integer status field (the existing scalar helpers are
        datetime-typed). Strict-typed: a non-integer stored value (a string,
        a float, a bool — JSON round-trips all three) raises
        :class:`StatusStoreError` rather than coercing silently, the same
        posture the datetime scalars take via ``_parse_utc``. The default-0
        convention means pre-#649 CRs (no such key) parse as "no ineffective
        restarts" without a migration.
        """
        raw = self._read_status().get(field)
        if raw is None:
            return 0
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise StatusStoreError(f"status.{field}: expected integer, got {type(raw).__name__}")
        return raw

    def _set_int(self, field: str, value: int) -> None:
        def build_patch(status: dict[str, Any]) -> dict[str, Any] | None:
            if status.get(field) == value:
                return None
            return {"status": {field: value}}

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

    # --- deferredEvents (#402) -------------------------------------------------

    def get_deferred_events(self) -> list[dict[str, str]]:
        """Read ``status.deferredEvents`` — the persisted deferred-Warning queue.

        Issue #402: every accepted ``QueuedKopfEventSink`` deferral is
        mirrored here so an operator crash does not silently drop queued
        Warning Events; the sink's ``flush_for`` drains this list first
        and then clears it. Values are stringified at the boundary — the
        wire format is JSON, and the sink's tuples are all-strings by
        construction, so the coercion is a defensive no-op for well-formed
        entries.
        """
        raw = self._read_status().get(DEFERRED_EVENTS) or []
        if not isinstance(raw, list):
            raise StatusStoreError(
                f"status.{DEFERRED_EVENTS}: expected array, got {type(raw).__name__}"
            )
        entries: list[dict[str, str]] = []
        for i, entry in enumerate(raw):
            if not isinstance(entry, Mapping):
                raise StatusStoreError(
                    f"status.{DEFERRED_EVENTS}[{i}]: expected object, "
                    f"got {type(entry).__name__}"
                )
            entries.append({str(k): str(v) for k, v in entry.items()})
        return entries

    def append_deferred_event(
        self, entry: Mapping[str, str], *, max_entries: int | None = None
    ) -> None:
        """Append one deferred Warning Event to ``status.deferredEvents`` (#402).

        Conflict-safe RMW like every other ``.status`` write (D04): the
        current array is re-read inside the cycle and the patch carries
        the full replacement list (JSON merge patch replaces arrays
        wholesale). ``max_entries`` is the persisted twin of the sink's
        ``MAX_DEFERRED_WARNING_EVENTS`` cap — a defensive backstop so a
        clear-failure streak cannot grow the array past the in-memory
        queue's own bound; the append is skipped (no write) when the
        list is already at the cap. The caller (the sink) owns drop
        accounting; this method never counts or emits.
        """
        encoded = {str(k): str(v) for k, v in entry.items()}

        def build_patch(status: dict[str, Any]) -> dict[str, Any] | None:
            current = status.get(DEFERRED_EVENTS)
            if not isinstance(current, list):
                current = []
            if max_entries is not None and len(current) >= max_entries:
                return None
            return {"status": {DEFERRED_EVENTS: [*current, encoded]}}

        self._mutate(build_patch)

    def clear_deferred_events(self) -> None:
        """Delete ``status.deferredEvents`` after a successful drain (#402).

        Idempotent: clearing an absent array writes nothing (the
        merge-patch removal is an explicit ``None``, same shape the map
        clears use).
        """

        def build_patch(status: dict[str, Any]) -> dict[str, Any] | None:
            if not status.get(DEFERRED_EVENTS):
                return None
            return {"status": {DEFERRED_EVENTS: None}}

        self._mutate(build_patch)

    # --- scalars ---------------------------------------------------------------

    def get_last_recycle_at(self) -> datetime | None:
        return self._get_scalar(LAST_RECYCLE_AT)

    def set_last_recycle_at(self, when: datetime | None) -> None:
        self._set_scalar(LAST_RECYCLE_AT, when)

    def get_last_web_background_restart_at(self) -> datetime | None:
        return self._get_scalar(LAST_WEB_BACKGROUND_RESTART_AT)

    def set_last_web_background_restart_at(self, when: datetime | None) -> None:
        self._set_scalar(LAST_WEB_BACKGROUND_RESTART_AT, when)

    def get_stall_window_started_at(self) -> datetime | None:
        return self._get_scalar(STALL_WINDOW_STARTED_AT)

    def set_stall_window_started_at(self, when: datetime | None) -> None:
        self._set_scalar(STALL_WINDOW_STARTED_AT, when)

    def get_web_background_ineffective_restarts(self) -> int:
        """Read ``status.webBackgroundIneffectiveRestarts`` (#649) — the
        consecutive-ineffective-restart count the circuit breaker keys on.

        Absent reads as ``0`` (pre-#649 CRs need no migration); corrupt
        non-integer values raise :class:`StatusStoreError`.
        """
        return self._get_int(WEB_BACKGROUND_INEFFECTIVE_RESTARTS)

    def set_web_background_ineffective_restarts(self, count: int) -> None:
        """Write ``status.webBackgroundIneffectiveRestarts`` (#649).

        Conflict-safe RMW like every other ``.status`` write (D04); writing
        the value already stored is a no-op (no apiserver PATCH).
        """
        self._set_int(WEB_BACKGROUND_INEFFECTIVE_RESTARTS, count)

    # --- pruning ---------------------------------------------------------------

    def protect_anchor_keys(self, field: str, keys: Iterable[str]) -> None:
        """Register the live-id set protecting ``field`` at cap-eviction time.

        Issue #648 (defense-in-depth): the watchdog's hygiene pass calls
        this once per heavy check with the ids it just observed ALIVE in a
        live server view; :meth:`_set_map_entry`'s eviction then prefers
        unprotected keys, so a still-live zombie datapoint's ``requeues``
        budget anchor survives an arbitrary cap hit even if the hygiene
        pass itself is broken or delayed. Replaces the CR's previous set
        wholesale — a shrinking live view must SHRINK the protection, not
        accumulate it. ``field`` is one of the four ``_STATUS_MAP_FIELDS``
        map names; an unknown field name raises (a typo'd field would
        silently protect nothing).
        """
        if field not in _STATUS_MAP_FIELDS:
            raise StatusStoreError(
                f"protect_anchor_keys: unknown status map field {field!r} "
                f"(expected one of {_STATUS_MAP_FIELDS})"
            )
        _PROTECTED_ANCHOR_KEYS[(self._namespace, self._name, field)] = frozenset(keys)

    def prune(
        self,
        *,
        live_analysis_ids: Iterable[str] | None = None,
        live_datapoint_ids: Iterable[str] | None = None,
    ) -> dict[str, list[str]]:
        """Drop map entries whose analysis/datapoint id is not in the live set.

        Callers pass the ids that still need tracking (how "live" is defined
        is each handler's domain knowledge); every keyed entry outside that
        set is deleted — covering both completion (id left the unfinished
        set) and disappearance (id no longer returned by the API). ``None``
        (the default) leaves that id-space untouched, so each handler prunes
        only the maps it has fresh knowledge for.

        Issue #648 — returns the ids actually pruned, as a ``{field: ids}``
        dict (empty when nothing was dead), captured from the patch the
        successful RMW applied — the retry loop may recompute the patch
        from fresher reads, so the return value always reflects the last
        (applied) computation, never a superseded 409 attempt. Callers use
        it for their one-per-run summary Event.
        """
        analysis_live = None if live_analysis_ids is None else set(live_analysis_ids)
        datapoint_live = None if live_datapoint_ids is None else set(live_datapoint_ids)
        scopes: list[tuple[str, set[str] | None]] = [
            (SOFT_STOPS, analysis_live),
            (ARCHIVED_ANALYSES, analysis_live),
            (REQUEUES, datapoint_live),
            (STARTED_SINCE, datapoint_live),
        ]
        applied: dict[str, list[str]] = {}

        def build_patch(status: dict[str, Any]) -> dict[str, Any] | None:
            nonlocal applied
            applied = {}
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
                    applied[field] = sorted(dead)
            return {"status": deletions} if deletions else None

        self._mutate(build_patch)
        return applied
