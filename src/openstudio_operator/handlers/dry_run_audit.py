"""``DryRunToggled`` audit Event on ``spec.dryRun`` transitions (issue #397).

D11 gates every mutating action on ``spec.dryRun``, but the gate itself was
mute: a flip ``dryRun: false`` → ``true`` (or back) produced zero Events, so
an attacker with ``update`` on the OSCM CR could silently disable the
operator's mutation enforcement and the only post-hoc evidence was the CR's
``metadata.resourceVersion``. This module adds the missing audit signal: a
Normal ``DryRunToggled`` Kubernetes Event on every actual dryRun field
transition, citing old→new.

Design points:

* **Watch handler, not a timer.** A ``@kopf.on.event`` handler fires on every
  OSCM watch event (spec change, status patch, resync). The DIFF against the
  last-seen value is the gate — steady state (including the operator's own
  ``.status`` RMW patches, which fire watch events constantly) emits nothing.
* **kopf gives us no old/new on the watching path.** ``WatchingCause`` (what
  ``@kopf.on.event`` receives in kopf 1.37–1.44) carries ``type``/``event``/
  ``body`` but NOT the ``old``/``new``/``diff`` kwargs the change handlers
  (``@kopf.on.update``/``@kopf.on.field``) get — those are computed from the
  ``kopf.zalando.org/last-handled-configuration`` annotation, which kopf
  maintains by PATCHing the CR's main resource. Opting into that machinery
  would violate D04 (operator writes live in the ``.status`` subresource
  only), add ``patch`` pressure on the main resource, and churn
  ``resourceVersion`` on every cycle — the exact noise an audit signal must
  not add. So the prior value lives in a tiny in-process cache.
* **The cache is cache, never source of truth (D04).** ``_last_dry_run`` only
  de-dupes emission. A cache miss (operator start, watch initial listing,
  restart, or a CR first seen mid-flight) is a BASELINE observation — no
  Event. Consequences, documented for auditors:

  - a transition that happens while the operator is down is invisible to
    this signal (the watch stream is the only trigger) — the kube-apiserver
    audit log covers that window;
  - a watch partition + re-sync does NOT lose the edge: the cache holds the
    last value SEEN, so a toggle during the gap emits when the stream
    catches up (at-least-once semantics for real transitions, never a false
    baseline re-emit).

* **The audit Event deliberately bypasses the D11 gate.** Routing it through
  :class:`openstudio_operator.events.EventEmitter` would self-suppress
  exactly when an attacker turns dryRun ON — the one moment the signal must
  fire. It calls :func:`kopf.event` directly, the same chokepoint the
  singleton guard's ``SingletonConflict``/``SingletonActive`` Events use
  (those also must not be suppressible by the very flag under audit).
* **Posture gauge flip (issue #492).** On the same transition the handler
  also sets ``metrics.DRY_RUN_ACTIVE{namespace,name}`` (1.0/0.0) — the
  immediate, no-next-tick-latency stamp; the per-tick
  ``run_oscm_tick`` stamp is the backstop. Transition-gated like the
  Event (steady state re-stamps nothing), bypasses D11 like the Event
  (recording posture is not a mutation), and covers loser CRs like the
  Event.
* **Fires for EVERY OSCM CR, winner or loser.** This handler is NOT wrapped
  by the singleton ``_gated`` wrapper (which only reaches kopf's
  ``registry._spawning`` timers/daemons) and is deliberately not registered
  in :mod:`openstudio_operator._oscm_handlers` — that registry exists to
  gate spawning handlers. A dryRun flip on a LOSER CR is exactly as
  suspicious as one on the winner; the audit signal covers both.

* **Acting principal.** Watch events carry no user identity; the message
  cites the best-effort last-writer from ``metadata.managedFields`` and
  points at the kube-apiserver audit log for the authoritative principal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

import kopf

from openstudio_operator.status_store import GROUP, PLURAL, VERSION

logger = logging.getLogger(__name__)

#: ``(obj, type, reason, message)`` — kopf.event in production, a recorder
#: in tests (same shape as ``singleton.EventSink``).
EventSink = Callable[[dict, str, str, str], None]

#: Event reason a cluster admin queries:
#: ``kubectl get events --field-selector reason=DryRunToggled``
DRY_RUN_TOGGLED_EVENT = "DryRunToggled"

#: Last-seen ``spec.dryRun`` per CR, keyed ``(namespace, name)``. Cache-not-
#: source-of-truth — see the module docstring's D04 note. Entries are
#: dropped on DELETED watch events so CR churn cannot grow it unboundedly.
_last_dry_run: dict[tuple[str, str], bool] = {}

_MISSING = object()


def reset_audit_state() -> None:
    """Drop every cached last-seen value (test seam, mirrors ``reset_registry``).

    Production never calls this — the cache accumulates for the process
    lifetime and self-heals across restarts via the baseline-on-miss rule.
    """
    _last_dry_run.clear()


def _dry_run_of(body: object) -> bool:
    """Extract ``spec.dryRun`` with the same default as ``config.py`` (missing → False)."""
    if not isinstance(body, Mapping):
        return False
    spec = body.get("spec")
    if not isinstance(spec, Mapping):
        return False
    return bool(spec.get("dryRun", False))


def _acting_manager(body: object) -> str | None:
    """Best-effort last-writer name from ``metadata.managedFields``.

    Watch events carry no user identity; the most-recently-touched
    ``managedFields`` entry is the closest on-object hint. Returns ``None``
    when managedFields is absent (stripped, old apiserver, or handcrafted
    fixture) — callers word the message for that case.
    """
    if not isinstance(body, Mapping):
        return None
    meta = body.get("metadata")
    if not isinstance(meta, Mapping):
        return None
    managed = meta.get("managedFields")
    if not isinstance(managed, list):
        return None
    candidates = [m for m in managed if isinstance(m, Mapping)]
    if not candidates:
        return None
    # ISO-8601 timestamps sort lexicographically; absent time sorts oldest.
    latest = max(candidates, key=lambda m: str(m.get("time") or ""))
    manager = str(latest.get("manager") or "")
    return manager or None


def _emit_kopf_event(obj: dict, event_type: str, reason: str, message: str) -> None:
    kopf.event(obj, type=event_type, reason=reason, message=message)


def record_dry_run_transition(
    body: object,
    *,
    logger: logging.Logger,
    emit: EventSink | None = None,
) -> bool:
    """Diff-gated ``DryRunToggled`` emission for one CR observation.

    Pure decision core (the kopf handler is a thin wrapper): compare
    ``spec.dryRun`` against the last-seen cache entry for this CR and emit a
    Normal :data:`DRY_RUN_TOGGLED_EVENT` Event ONLY on an actual transition.
    Returns whether an Event was emitted. The old/new values ride in the
    message as the lowercase quoted strings ``"false"``/``"true"`` (kopf's
    ``kopf.event()`` accepts no Event labels in 1.37–1.44 — the reason +
    message pair is the queryable audit surface). ``emit`` defaults to this
    module's :func:`_emit_kopf_event`, resolved at call time so tests may
    inject a sink either way.
    """
    if emit is None:
        emit = _emit_kopf_event
    if not isinstance(body, Mapping):
        return False
    meta = body.get("metadata")
    if not isinstance(meta, Mapping):
        return False
    namespace = str(meta.get("namespace") or "")
    name = str(meta.get("name") or "")
    if not namespace or not name:
        logger.debug("dryRun audit: skipping nameless body (keys=%s)", sorted(body.keys()))
        return False
    key = (namespace, name)
    current = _dry_run_of(body)
    previous = _last_dry_run.get(key, _MISSING)
    _last_dry_run[key] = current
    if previous is _MISSING or previous == current:
        return False

    manager = _acting_manager(body)
    attribution = (
        f" Last writer per metadata.managedFields: {manager!r}; the acting "
        "principal is recorded in the kube-apiserver audit log."
        if manager
        else " metadata carries no managedFields hint; the acting principal "
        "is recorded in the kube-apiserver audit log."
    )
    if current:
        effect = "Operator mutations are now SUPPRESSED (dry-run mode, D11)."
    else:
        effect = "Operator mutations are now LIVE (D11 enforcement resumed)."
    old_label, new_label = ("true" if previous else "false"), ("true" if current else "false")
    message = (
        f"spec.dryRun toggled: {old_label} → {new_label} on "
        f"OpenStudioClusterManager {namespace}/{name} (issue #397 audit). "
        f"{effect}{attribution}"
    )
    logger.warning(
        "spec.dryRun toggled %s → %s on %s/%s (#397) — %s",
        old_label,
        new_label,
        namespace,
        name,
        "dry-run mode, mutations suppressed" if current else "enforcement live",
    )
    emit(
        {"metadata": {"namespace": namespace, "name": name}},
        "Normal",
        DRY_RUN_TOGGLED_EVENT,
        message,
    )
    # Issue #492 — flip the dry-run posture gauge immediately so a
    # spec.dryRun transition is visible at /metrics without waiting for
    # the next timer tick (the per-tick stamp in run_oscm_tick remains
    # the backstop — it covers the baseline-on-miss case: a transition
    # that happens while the operator is down is picked up by the next
    # tick). Covers every OSCM CR, loser CRs included — matching this
    # handler's audit-Event coverage — and deliberately NOT routed
    # through the D11 gate: the gauge records posture, it does not
    # mutate anything (the same reasoning that exempts the audit Event
    # itself).
    from openstudio_operator.metrics import DRY_RUN_ACTIVE

    DRY_RUN_ACTIVE.labels(namespace=namespace, name=name).set(
        1.0 if current else 0.0
    )
    return True


@kopf.on.event(GROUP, VERSION, PLURAL)
def dry_run_toggle_audit(
    body: kopf.Body,
    namespace: str,
    name: str,
    logger: kopf.Logger,
    **_kwargs: object,
) -> None:
    """On every OSCM watch event: emit ``DryRunToggled`` only on real transitions.

    Fires for every OSCM CR (singleton winner and losers alike — see the
    module docstring). DELETED events evict the cache entry instead of
    diffing (the deletion's final dryRun value is not a toggle).
    """
    event_type = _kwargs.get("type")
    key = (str(namespace), str(name))
    if event_type == "DELETED":
        _last_dry_run.pop(key, None)
        return
    record_dry_run_transition(body, logger=logger)
