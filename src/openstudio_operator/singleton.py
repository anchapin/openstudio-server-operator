"""Passive singleton guard for OpenStudioClusterManager CRs (D05 — issue #14).

Policy: exactly one OSCM CR per namespace is served — the OLDEST one, always.
The rule is stateless and restart-safe: seniority is recomputed from the API
server's own ``metadata.creationTimestamp`` on every evaluation; nothing is
persisted and nothing in process memory decides who wins. The only in-memory
state (``SingletonGuard._last_state``) is a noise gate that deduplicates
log/Event emission on unchanged snapshots — it never influences resolution
and a restarted operator simply re-announces once.

Deterministic order (documented tiebreak): CRs sort by
``(creationTimestamp, metadata.name, metadata.uid)``.

* Equal creationTimestamps (K8s seconds resolution — same-second creation is
  possible) → lexicographically smallest ``metadata.name`` wins.
* ``metadata.uid`` is a purely defensive final tiebreak: names are unique
  within a namespace, so it is unreachable for real API objects.
* A CR missing ``creationTimestamp`` (impossible from the API, possible from
  hand-crafted fixtures) sorts as the youngest.

Passive policing: losing CRs are NEVER deleted or mutated — no finalizers, no
status patches, nothing. They get one Warning Event (``SingletonConflict``)
per state change and are refused service: their handler ticks return before
any side effect (no OpenStudio REST polls, no Deployment patches, no events).
The winner gets a Normal ``SingletonActive`` event whenever a conflict exists
— chosen deliberately so the winner's owner can see why the other CRs are
complaining; the losers carry the actual Warning per the issue text.

Zero CRs: the operator idles quietly — a single info log on the transition
to zero (and one at startup via the startup handler), never per tick.

Event wiring: a ``@kopf.on.event`` handler fires on every OSCM change AND on
the watch's initial listing, covering "at startup AND on CR changes" for any
number of CRs > 0. The zero-CR-at-startup case produces no watch events at
all, so a ``@kopf.on.startup`` handler performs the same check when
``POD_NAMESPACE`` is set (Downward API in the operator Deployment; absent in
bare ``kopf run`` dev sessions, where the event path still covers CRs > 0).

Handler gating — central, not per-file: :func:`install_singleton_guard`
post-processes the kopf registry AFTER the handler modules are imported (it
is called from ``handlers/__init__.py``) and wraps every spawning handler
(``@kopf.timer``/``@kopf.daemon``) registered for the OSCM resource. The
wrapper re-resolves the oldest CR from the API on EVERY tick — so a CR that
usurps the winner by being older is honored within one tick of any handler —
and returns early for losers. This gates all existing handler modules
(including files owned by sibling PRs, which are not touched) and any future
module added to the ``handlers/__init__.py`` import block above the install
call. Failure mode: if the CR list call fails, the tick is SKIPPED and
retried on the next poll (fail-closed, same posture as D12) — never served
on the assumption of being active.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import kopf
from kubernetes.client import ApiException, CustomObjectsApi

from openstudio_operator.status_store import GROUP, PLURAL, VERSION

logger = logging.getLogger(__name__)

SINGLETON_CONFLICT_EVENT = "SingletonConflict"
SINGLETON_ACTIVE_EVENT = "SingletonActive"

#: Attribute marking handler fns already wrapped by the gate (idempotency).
GUARD_MARKER = "_openstudio_singleton_guarded"

_MISSING_CREATED = datetime.max.replace(tzinfo=UTC)

#: ``(object, type, reason, message)`` — kopf.event in production, a recorder
#: in tests. The object is the full CR body the event is emitted on.
EventSink = Callable[[dict, str, str, str], None]


class SingletonGuardError(Exception):
    """Singleton-guard failure: unparseable CR metadata."""


def _parse_utc(value: Any, context: str) -> datetime:
    """Parse an ISO-8601 timestamp at the API boundary; always tz-aware UTC.

    Mirrors the (private) ``_parse_utc`` of ``status_store`` and
    ``_parse_timestamp`` of ``openstudio_client`` — keep all copies in sync.
    """
    if not isinstance(value, str):
        raise SingletonGuardError(
            f"{context}: expected ISO-8601 string, got {type(value).__name__}"
        )
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SingletonGuardError(f"{context}: unparseable timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _meta(obj: dict) -> dict:
    meta = obj.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _cr_name(obj: dict) -> str:
    return str(_meta(obj).get("name") or "<unnamed>")


def _cr_created(obj: dict) -> str:
    return str(_meta(obj).get("creationTimestamp") or "<no creationTimestamp>")


def _same_cr(a: dict, b: dict) -> bool:
    """Identity by uid when both carry one (real API objects), else by name."""
    ma, mb = _meta(a), _meta(b)
    if ma.get("uid") and mb.get("uid"):
        return ma["uid"] == mb["uid"]
    return ma.get("name") == mb.get("name")


def sort_crs_by_seniority(items: Sequence[dict]) -> list[dict]:
    """Oldest first, per the documented ``(created, name, uid)`` order."""
    def key(obj: dict) -> tuple[datetime, str, str]:
        name = _cr_name(obj)
        raw = _meta(obj).get("creationTimestamp")
        created = (
            _parse_utc(raw, f"OpenStudioClusterManager {name}: metadata.creationTimestamp")
            if raw
            else _MISSING_CREATED
        )
        return (created, str(_meta(obj).get("name") or ""), str(_meta(obj).get("uid") or ""))

    return sorted(items, key=key)


def resolve_active_cr(items: Sequence[dict]) -> dict | None:
    """The one CR the operator serves: the oldest, or ``None`` when there are none."""
    ordered = sort_crs_by_seniority(items)
    return ordered[0] if ordered else None


class SingletonGuard:
    """Stateless oldest-CR resolution plus a change-gated noise channel.

    ``is_active`` lists the CRs and resolves the winner on every call — no
    caching of the verdict, so it is correct across operator restarts and
    within one tick of any CR change. ``enforce`` is the loud half: it logs
    and emits Events only when the observed ``(active, losers)`` snapshot
    CHANGES, so steady state is silent.
    """

    def __init__(self, custom_api: CustomObjectsApi) -> None:
        self._custom_api = custom_api
        self._last_state: tuple[str | None, frozenset[str]] | None = None

    def list_crs(self, namespace: str) -> list[dict]:
        """All OSCM CRs in ``namespace`` (K8s custom-objects list)."""
        resp = self._custom_api.list_namespaced_custom_object(
            GROUP, VERSION, namespace, PLURAL
        )
        items = resp.get("items") or []
        return [item for item in items if isinstance(item, dict)]

    def is_active(self, body: dict, namespace: str) -> bool:
        """Whether ``body`` is the CR the operator serves (oldest in namespace)."""
        winner = resolve_active_cr(self.list_crs(namespace))
        return winner is not None and _same_cr(body, winner)

    def enforce(self, items: Sequence[dict], *, logger: logging.Logger, emit: EventSink) -> None:
        """Log + emit Events for a fresh CR snapshot, only on state CHANGE.

        * >1 CRs  → one ``logger.error`` naming every CR and the winner;
                    Warning ``SingletonConflict`` on each loser, Normal
                    ``SingletonActive`` on the winner.
        * 1 CR    → one info log on transition into the state; no Events.
        * 0 CRs   → one info log on transition to zero; no Events (none possible).
        """
        winner = resolve_active_cr(items)
        losers = [obj for obj in items if winner is None or not _same_cr(obj, winner)]
        state = (None if winner is None else _cr_name(winner), frozenset(_cr_name(o) for o in losers))
        if state == self._last_state:
            return
        self._last_state = state

        if winner is None:
            logger.info("no OpenStudioClusterManager CRs — operator idle (D05)")
            return
        winner_name = _cr_name(winner)
        if not losers:
            logger.info("serving OpenStudioClusterManager %s — single CR in namespace (D05)", winner_name)
            return

        roster = ", ".join(f"{_cr_name(o)} (created {_cr_created(o)})" for o in sort_crs_by_seniority(items))
        logger.error(
            "singleton violation (D05): %d OpenStudioClusterManager CRs exist [%s]; "
            "the oldest, %s, is served — all others are ignored by the operator",
            len(items),
            roster,
            winner_name,
        )
        for loser in losers:
            emit(
                loser,
                "Warning",
                SINGLETON_CONFLICT_EVENT,
                f"Ignored by the operator: {winner_name} is the oldest "
                f"OpenStudioClusterManager in this namespace (created {_cr_created(winner)} "
                f"vs {_cr_created(loser)}). Exactly one CR may be served per namespace (D05); "
                f"delete this CR or the other one.",
            )
        emit(
            winner,
            "Normal",
            SINGLETON_ACTIVE_EVENT,
            f"Served as the oldest of {len(items)} OpenStudioClusterManager CRs in this "
            f"namespace (D05); the others are ignored: {', '.join(_cr_name(o) for o in losers)}.",
        )


_process_guard: SingletonGuard | None = None


def set_guard(guard: SingletonGuard | None) -> None:
    """Install/replace the process-wide guard (test seam; kopf wiring builds lazily)."""
    global _process_guard
    _process_guard = guard


def _get_guard() -> SingletonGuard:
    global _process_guard
    if _process_guard is None:
        _process_guard = SingletonGuard(CustomObjectsApi())
    return _process_guard


def is_active_cr(body: dict, namespace: str) -> bool:
    """Module-level convenience over the process guard (see :meth:`SingletonGuard.is_active`)."""
    return _get_guard().is_active(body, namespace)


def _gated(fn: Callable) -> Callable:
    """Wrap a kopf spawning-handler fn with the per-tick oldest-CR gate."""

    @functools.wraps(fn)
    def wrapper(*args: object, **kwargs: object) -> object:
        body = kwargs.get("body") or {}
        namespace = kwargs.get("namespace")
        log = kwargs.get("logger") or logger
        if not isinstance(body, dict) or not namespace:
            log.warning(
                "%s skipped: kopf invocation carried no body/namespace — cannot resolve "
                "the active CR (D05)",
                getattr(fn, "__name__", "handler"),
            )
            return None
        try:
            active = _get_guard().is_active(body, str(namespace))
        except (ApiException, SingletonGuardError) as exc:
            log.warning(
                "%s skipped this tick, retrying next poll — singleton guard could not "
                "resolve the active CR (%s: %s)",
                getattr(fn, "__name__", "handler"),
                type(exc).__name__,
                exc,
            )
            return None
        if not active:
            log.debug(
                "%s skipped: %s is not the oldest OpenStudioClusterManager (D05)",
                getattr(fn, "__name__", "handler"),
                _cr_name(body),
            )
            return None
        return fn(*args, **kwargs)

    wrapper.__dict__[GUARD_MARKER] = True
    return wrapper


def _selector_matches_oscms(handler: object) -> bool:
    sel = getattr(handler, "selector", None)
    if sel is None:
        return False
    group = getattr(sel, "group", None)
    names = (getattr(sel, "any_name", None), getattr(sel, "plural", None))
    return str(group) == GROUP and any(str(name) == PLURAL for name in names if name is not None)


def install_singleton_guard(registry: object | None = None) -> int:
    """Gate every OSCM spawning (timer/daemon) handler in the registry.

    Central wiring for issue #14: handler modules stay untouched — this runs
    after they are imported (``handlers/__init__.py``) and replaces each OSCM
    spawning handler's fn with a :func:`_gated` wrapper that serves only the
    oldest CR, re-resolved from the API on every tick. Idempotent; returns
    the number of newly wrapped handlers. Uses kopf registry internals
    (``registry._spawning._handlers`` — kopf 1.37+); if the layout is not as
    expected, it warns loudly and gates nothing rather than failing silently.
    """
    reg = registry if registry is not None else kopf.get_default_registry()
    spawning = getattr(reg, "_spawning", None)
    handlers = getattr(spawning, "_handlers", None)
    if not isinstance(handlers, list):
        logger.warning(
            "singleton guard: kopf registry internals not as expected — "
            "OSCM handlers NOT gated (D05 enforcement disabled)"
        )
        return 0

    wrapped = 0
    for index, handler in enumerate(list(handlers)):
        if not _selector_matches_oscms(handler):
            continue
        fn = getattr(handler, "fn", None)
        if fn is None or getattr(fn, GUARD_MARKER, False):
            continue
        try:
            handlers[index] = dataclasses.replace(handler, fn=_gated(fn))
        except TypeError:
            logger.warning(
                "singleton guard: cannot wrap handler %r — not gateable",
                getattr(handler, "id", "?"),
            )
            continue
        wrapped += 1
    if wrapped:
        logger.info(
            "singleton guard (D05) gating %d OSCM handler(s): %s",
            wrapped,
            ", ".join(str(getattr(h, "id", "?")) for h in handlers if _selector_matches_oscms(h)),
        )
    return wrapped


def _emit_kopf_event(obj: dict, event_type: str, reason: str, message: str) -> None:
    kopf.event(obj, type=event_type, reason=reason, message=message)


def _check(namespace: str | None, logger: logging.Logger) -> None:
    """Shared loud-check body for the kopf startup/event wrappers."""
    if not namespace:
        logger.debug("singleton guard: no namespace in context — check skipped")
        return
    guard = _get_guard()
    try:
        items = guard.list_crs(namespace)
    except ApiException as exc:
        logger.warning(
            "singleton guard: could not list OSCM CRs, will retry on the next "
            "event/tick (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return
    try:
        guard.enforce(items, logger=logger, emit=_emit_kopf_event)
    except SingletonGuardError as exc:
        logger.warning(
            "singleton guard: skipping this check, will retry on the next "
            "event/tick (%s: %s)",
            type(exc).__name__,
            exc,
        )


@kopf.on.startup()
def singleton_guard_startup(logger: kopf.Logger, **_: object) -> None:
    """Startup check — the only path that notices a zero-CR namespace at boot.

    The watch's initial listing fires :func:`singleton_guard_event` for every
    pre-existing CR, but zero CRs produce no events at all, so this handler
    covers the "operator starts with nothing to do" info log. It needs the
    namespace via the ``POD_NAMESPACE`` Downward API (set in
    deploy/operator-deployment.yaml); without it the check is skipped.
    """
    _check(os.getenv("POD_NAMESPACE"), logger)


@kopf.on.event(GROUP, VERSION, PLURAL)
def singleton_guard_event(namespace: str, logger: kopf.Logger, **_: object) -> None:
    """On every OSCM change (incl. the initial listing): re-resolve + enforce."""
    _check(namespace, logger)
