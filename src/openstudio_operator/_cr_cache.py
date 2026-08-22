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
"""

from __future__ import annotations

from collections.abc import Mapping


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
