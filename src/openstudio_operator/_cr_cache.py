"""Per-CR in-memory cache keying + reset convention (issue #497).

The operator's durable memory is the CR ``.status`` subresource (D04) —
every handler-module cache below is the documented "in-memory state is
cache, never source of truth" layer on top of it. Before #497 those
caches had drifted keying schemes and no reset seams: some keyed by
``(namespace, name)``, some un-keyed assuming a single CR for the
process lifetime, none invalidated when the singleton CR was deleted
and recreated under the SAME name (the #364 recreate path) — a stale
entry keyed only by name could leak CR A's state into CR B.

THE CONVENTION (#497) — every module-level per-CR cache MUST:

1. Be keyed by ``(namespace, name)`` — the D05 singleton guard ensures
   at most one ACTIVE CR per namespace, so the tuple identifies the
   active CR (same invariant as the ``StallWindowTracker`` keying, #167).
2. Be UID-VALIDATED: the cache entry records the CR's ``metadata.uid``
   at write time (``cr_uid(body)``), and a lookup whose observed uid
   differs from the recorded one (:func:`uid_is_stale`) treats the entry
   as belonging to the DELETED predecessor and starts fresh. This closes
   the delete+recreate leak at lookup time — no deletion hook required.
   A missing uid on either side (synthetic bodies, older tests) never
   declares staleness: keying still holds via ``(namespace, name)``.
3. Expose ``reset_per_cr_caches(namespace=None, name=None)`` as the
   reset seam — pass neither argument to drop ALL entries, both to drop
   exactly one CR's entry; anything else raises ``ValueError``. The
   seam is the test-isolation surface AND the wiring point for a future
   ``@kopf.on.delete`` handler (none exists today — see the gap note in
   the module docstrings of the cache-bearing handlers).

Since #583 the convention is EXECUTABLE, not just documented:
:class:`PerCRCache` (below) is the generic uid-validated holder — a
``dict`` subclass keyed ``(namespace, name)`` with ``(recorded_uid,
value)`` entries — owning the get-or-create / evict-on-stale-uid /
first-sighting-upgrade / scoped-reset mechanics that
``datapoint_watchdog._get_exhausted_seen`` and
``web_background_monitor._get_tracker`` each hand-rolled around the
two primitives. The cache-bearing modules instantiate it under their
historical module globals (``_EXHAUSTED_WARNED`` / ``_tracker_cache``)
and keep only thin typed façades plus ``reset_per_cr_caches`` seams
delegating to :meth:`PerCRCache.reset` — a third cache-bearing module
(or a semantics fix to the upgrade/eviction branches) now lands ONCE
here instead of copy-pasting.

Census (#497 — what each module holds and why):

* ``handlers/analysis_sla`` — NO module-level per-CR cache. The SLA
  clock anchors live in ``status.softStops`` (D04) and
  :func:`run_sla_tick` is a pure function of its arguments. Nothing to
  key, nothing to reset.
* ``handlers/datapoint_watchdog`` — ``_EXHAUSTED_WARNED``, the
  presentation-only exhaustion-Warning dedup set. Was an UN-KEYED
  ``set[str]`` of datapoint ids (assumed one CR); now convention-shaped
  (keyed + uid-validated + reset seam).
* ``handlers/worker_recycler`` — NO module-level per-CR cache. The
  recycle gate state lives in ``status.lastRecycleAt`` (D04). Nothing
  to key, nothing to reset.
* ``handlers/web_background_monitor`` — ``_tracker_cache`` of
  sustained-window clocks, already ``(namespace, name)``-keyed since
  #167; #497 adds uid validation + the reset seam. The three leg-2
  safeguard globals (``_max_workers_seen``, ``_empty_registry_since``,
  ``_resque_layout_warning_emitted``, issue #44) are deliberately
  UN-keyed process-lifetime state: they diagnose the Resque key layout
  — a Redis-server property, not a CR property — and the layout warning
  is one-shot PER PROCESS by design (``reset_leg2_safeguard_state`` is
  the existing test seam). Keying them per CR would re-arm a
  process-level diagnostic per CR churn without adding safety.
* ``handlers/dry_run_audit`` — ``_last_dry_run``, the last-seen
  ``spec.dryRun`` dedup map keyed ``(namespace, name)`` (#397).
  Deliberately NOT convention-shaped: a cache miss is a harmless
  baseline observation (no Event), entries are dropped on the DELETED
  watch event — so the #364 delete+recreate path is handled by the
  watch stream, not uid validation — and ``reset_audit_state()`` is
  the test seam. Classified cache-free by the #652 census (no
  ``reset_per_cr_caches`` seam) and carried as a documented exception
  for its one cache-shaped global.
* ``handlers/redis_layout_check`` — NO module-level per-CR cache (the
  liveness gauge is re-stamped on every check). Classified cache-free
  (#652).
* ``events_sinks`` — the deferred-Warning queue holds per-ENTRY
  ``(namespace, name, reason, message)`` tuples (keyed per entry, not
  per CR); the ``@kopf.on.event`` drain handler fires on the DELETED
  watch event too, so a deleted CR's queued entries drain-and-clear at
  deletion, and the #402 persisted mirror dies with the CR's
  ``.status``. No leak surface; no change.
* ``singleton`` / ``client_factory`` — OUT OF #497'S SCOPE. The
  ``singleton`` factory globals already have the
  ``reset_operator_k8s_client()`` / ``set_guard(None)`` seams (#158 /
  #251 / #494 territory); ``client_factory``'s ``lru_cache`` is keyed
  by the RESOLVED server/Redis URL (config identity), and the cached
  clients hold no per-CR state — a recreated CR with the same URL
  correctly reuses the same stateless client.

Since #652 the census is fenced, not just documented: the
machine-readable classification lives in ``tests/_cache_census.py``
(``CACHE_BEARING_MODULES`` / ``CACHE_FREE_MODULES`` — single-sourced;
conftest's autouse reset imports it), and the AST gate in
``tests/test_cache_keying_convention.py`` fails any handler module
that declares cache-shaped state without being classified.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TypeVar


def cr_uid(body: object) -> str | None:
    """Extract ``metadata.uid`` from a kopf body; ``None`` when absent.

    kopf delivers the full custom-resource object as ``body`` (a Mapping —
    plain ``dict`` in tests, kopf's ``Body`` MappingView in production);
    real CRs always carry a server-assigned ``metadata.uid``, synthetic
    test bodies may not. ``None`` is a valid answer — the uid-validation
    convention treats a missing uid on either side as "cannot validate"
    and falls back to pure ``(namespace, name)`` keying.
    """
    getter = getattr(body, "get", None)
    if not callable(getter):
        return None
    metadata = getter("metadata")
    if not isinstance(metadata, Mapping):
        return None
    uid = metadata.get("uid")
    return str(uid) if uid else None


def uid_is_stale(recorded: str | None, observed: str | None) -> bool:
    """``True`` iff ``recorded`` definitively belongs to a DIFFERENT CR object.

    Staleness requires BOTH uids present and unequal — the delete+recreate
    signature (same ``(namespace, name)``, new uid, the #364 path). A
    ``None`` on either side never declares staleness: pre-#497 cache
    entries recorded no uid, and synthetic bodies carry none, so the
    lookup degrades to the (still singleton-guard-valid) tuple keying
    instead of discarding state it cannot prove stale.
    """
    return recorded is not None and observed is not None and recorded != observed


_ValueT = TypeVar("_ValueT")


class PerCRCache(dict[tuple[str, str], tuple[str | None, _ValueT]]):
    """Generic uid-validated per-CR cache — the convention, executable (#583).

    A ``dict`` subclass so the raw entry map (``{(namespace, name):
    (recorded_uid, value)}``) stays introspectable under the module
    global each cache-bearing handler module already exposes — tests,
    audits, and the reset seam all read the plain dict interface
    (``clear`` / ``len`` / ``in`` / iteration / ``== {}``). Owns the
    mechanics every cache-bearing module hand-rolled before #583:

    1. keyed get-or-create through a caller-supplied ``factory``;
    2. uid validation at lookup (:func:`uid_is_stale`) — an entry
       recorded under a different uid belongs to the DELETED predecessor
       CR (the #364 delete+recreate path) and is evicted with the
       module's own INFO wording (the ``stale_log`` %-style template,
       fixed at construction so each module keeps its historical text);
    3. the "first uid sighting for a pre-uid entry" upgrade — record the
       uid in place without discarding state that cannot be proven stale;
    4. the scoped :meth:`reset` (all / exactly-one / ``ValueError``)
       behind each module's ``reset_per_cr_caches`` seam.

    ``on_fresh`` (optional) runs BEFORE a brand-new entry is inserted,
    with ``(namespace, name)`` — the hook for module-specific
    creation-time diagnostics (``web_background_monitor`` uses it for
    the #167 D05 singleton-bypass warning, which must scan the OTHER
    cache keys before the new one lands).
    """

    def __init__(
        self,
        *,
        stale_log: str,
        logger: logging.Logger,
        on_fresh: Callable[[str, str], None] | None = None,
    ) -> None:
        """Build an empty cache bound to its eviction-log wording and logger.

        ``stale_log`` is a %-style template rendered with
        ``(namespace, name, recorded_uid, observed_uid)`` on eviction;
        each module passes its historical wording byte-identically.
        """
        super().__init__()
        self._stale_log = stale_log
        self._logger = logger
        self._on_fresh = on_fresh

    def get_or_create(
        self,
        namespace: str,
        name: str,
        uid: str | None,
        factory: Callable[[], _ValueT],
    ) -> _ValueT:
        """Uid-validating get-or-create: the ONE lookup path (#497/#583).

        Present entry whose recorded uid differs from the observed one
        (:func:`uid_is_stale` — the #364 delete+recreate signature):
        evict (INFO log through the ``stale_log`` wording) and fall
        through to a fresh entry. Missing entry: run ``on_fresh`` (if
        any), insert ``(uid, factory())``, return it. Present entry
        whose recorded uid is still ``None`` while the observed uid is
        not: record the uid in place (``None`` never declares
        staleness, so the entry survives — it just becomes validatable).
        Present entry otherwise: return the cached value with stable
        object identity across lookups.
        """
        key = (namespace, name)
        entry = self.get(key)
        if entry is not None and uid_is_stale(entry[0], uid):
            self._logger.info(self._stale_log, namespace, name, entry[0], uid)
            entry = None
        if entry is None:
            if self._on_fresh is not None:
                self._on_fresh(namespace, name)
            value = factory()
            self[key] = (uid, value)
            return value
        if uid is not None and entry[0] is None:
            # First uid sighting for an entry recorded pre-uid (or by a
            # uid-less caller): record it so later lookups can validate.
            self[key] = (uid, entry[1])
        return entry[1]

    def reset(self, namespace: str | None = None, name: str | None = None) -> None:
        """Scoped reset behind the ``reset_per_cr_caches`` seams.

        Pass neither argument to clear every entry (test isolation); pass
        both ``namespace`` and ``name`` to drop exactly one CR's entry
        (the shape a future ``@kopf.on.delete`` handler would call — none
        exists today; :meth:`get_or_create`'s uid validation closes the
        delete+recreate leak at lookup time in the meantime). Anything
        else is a caller bug and raises rather than silently clearing the
        wrong scope.
        """
        if namespace is None and name is None:
            self.clear()
        elif namespace is not None and name is not None:
            self.pop((namespace, name), None)
        else:
            raise ValueError(
                f"reset_per_cr_caches: pass both namespace and name, or neither "
                f"(got namespace={namespace!r}, name={name!r})"
            )
