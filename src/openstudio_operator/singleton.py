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
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import kopf
from kubernetes.client import (
    ApiException,
    AppsV1Api,
    BatchV1Api,
    CoreV1Api,
    CustomObjectsApi,
)
from kubernetes.config import ConfigException

from openstudio_operator._k8s import load_operator_kube_config
from openstudio_operator._time import parse_iso_utc
from openstudio_operator.metrics import (
    HANDLER_TICK_FAILURES_TOTAL,
    SINGLETON_ELECTION_TOTAL,
    SINGLETON_LOSER_SKIPS_TOTAL,
)
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

    Thin wrapper over :func:`openstudio_operator._time.parse_iso_utc` (D12
    boundary, issue #174) that re-raises ``ValueError`` as
    :class:`SingletonGuardError` with the caller's ``context`` prefix so the
    domain-specific exception type stays in this module's public contract.
    ``None`` is not a valid input here — the call sites guard for it — so we
    reject it explicitly with the same shape the legacy ``expected
    ISO-8601 string`` error used.
    """
    if value is None:
        raise SingletonGuardError(f"{context}: expected ISO-8601 string, got NoneType")
    try:
        return parse_iso_utc(value)
    except ValueError as exc:
        raise SingletonGuardError(f"{context}: {exc}") from exc


def _meta(obj: object) -> Mapping:
    # Mapping, not dict: kopf >=1.4x delivers body/metadata as MappingView
    # subclasses (kopf._cogs.structs.bodies.Body/Meta), which are NOT dicts.
    meta = obj.get("metadata") if isinstance(obj, Mapping) else None
    return meta if isinstance(meta, Mapping) else {}


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
            # Issue #239 — singleton-guard election outcome counter.
            # Increment on the zero-CR (idle) branch only when the state
            # CHANGED (the early-return above already filters unchanged
            # snapshots). Mirrors the existing change-gated log/Event
            # noise channel so steady state is silent.
            SINGLETON_ELECTION_TOTAL.labels(outcome="idle").inc()
            return
        winner_name = _cr_name(winner)
        if not losers:
            logger.info("serving OpenStudioClusterManager %s — single CR in namespace (D05)", winner_name)
            # Issue #239 — singleton-guard election outcome counter
            # (active branch — exactly one CR in namespace).
            SINGLETON_ELECTION_TOTAL.labels(outcome="active").inc()
            return

        # Issue #239 — singleton-guard election outcome counter
        # (conflict branch — more than one CR in namespace, losers
        # ignored). Increments on the same state-change gate as the
        # Warning/Normal Events below.
        SINGLETON_ELECTION_TOTAL.labels(outcome="conflict").inc()

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


# Issue #158 — the operator's single ``CustomObjectsApi()`` construction
# point. Every handler that talks to the K8s API server for the OSCM custom
# object imports THIS function — never ``CustomObjectsApi()`` directly. The
# factory loads the operator pod's service-account config (falling back to
# ``kube_config`` for local ``kopf run`` dev sessions), then caches the
# resulting client for the operator's lifetime. The AST test in
# ``tests/test_singleton_registry_coverage.py::test_only_one_custom_objects_api_construction_point``
# enforces "exactly one construction site" so a future regression that
# bypasses the factory fails the CI gate loudly.
_operator_custom_objects_api: CustomObjectsApi | None = None


def operator_custom_objects_api() -> CustomObjectsApi:
    """Return the process-wide :class:`CustomObjectsApi` (issue #158).

    The SINGLE ``CustomObjectsApi()`` construction point in the operator —
    every handler that needs to read or patch the OSCM ``.status``
    subresource (``StatusStore``'s RMW path), list CRs
    (``SingletonGuard.list_crs``), or talk to the K8s API server for any
    other custom-object reason imports this factory. Inline
    ``CustomObjectsApi()`` calls outside this function are a regression: a
    bare ``CustomObjectsApi()`` carries whatever the default kubeconfig
    resolution picks up (typically ``KUBERNETES_SERVICE_HOST`` /
    ``KUBERNETES_SERVICE_PORT`` envs and a service-account token mount),
    which is correct only because the operator Deployment is in-cluster.
    Any future change to this loader (kubeconfig Secret reference,
    network-proxy client, etc.) would silently leave inline callsites
    behind.

    Behaviour:

    * Loads the operator pod's in-cluster service-account config via
      :func:`openstudio_operator._k8s.load_operator_kube_config` (the
      SINGLE public loader, issue #305); the loader tries
      ``kubernetes.config.load_incluster_config`` first, on
      ``ConfigException`` falls back to
      ``kubernetes.config.load_kube_config``. Same posture as the
      original ``_build_custom_objects_api`` (issue #79) — kopf >=1.44
      never initializes client-python's default ``Configuration``, so a
      bare ``CustomObjectsApi()`` with no loaded config raises
      ``LocationValueError`` on every call (proven live in #66's kind
      validation).
    * Caches the resulting :class:`CustomObjectsApi` for the process's
      lifetime. All callers share the same instance, so the underlying
      :class:`kubernetes.client.ApiClient` (HTTP connection pool, retry
      config) is reused across every operator tick.
    * On config-loading failure, raises — callers fail closed (skip the
      tick, retry next poll per D12).

    Tests that mock ``kubernetes.config.load_incluster_config`` use
    :func:`reset_operator_k8s_client` to drop the cache between cases so
    the next call to :func:`operator_custom_objects_api` re-runs the
    load path with the freshly patched loader.
    """
    global _operator_custom_objects_api
    if _operator_custom_objects_api is None:
        load_operator_kube_config()
        _operator_custom_objects_api = CustomObjectsApi()
    return _operator_custom_objects_api


# Issue #251 — the operator's single ``AppsV1Api()`` / ``BatchV1Api()`` /
# ``CoreV1Api()`` construction points. The CustomObjectsApi analogue
# (:func:`operator_custom_objects_api`, issue #158) was the only centrally
# constructed K8s client; the other three were built inline at their
# call sites. The structural inconsistency (one K8s client centralised,
# three not) is the issue's motivation: a future change to the loader
# (kubeconfig Secret reference, network-proxy client, …) silently leaves
# the inline callsites behind. The factories below retire every inline
# ``*V1Api()`` site; the AST test in
# ``tests/test_singleton_registry_coverage.py::test_only_one_v1_api_construction_point_per_factory``
# enforces "exactly one construction site per client type" so a future
# regression that bypasses the factory fails the CI gate loudly.
_operator_apps_api: AppsV1Api | None = None
_operator_batch_api: BatchV1Api | None = None
_operator_core_api: CoreV1Api | None = None


def operator_apps_api() -> AppsV1Api:
    """Return the process-wide :class:`AppsV1Api` (issue #251).

    The SINGLE ``AppsV1Api()`` construction point in the operator. Every
    handler that needs to read or patch a Deployment (the worker recycler
    in :mod:`openstudio_operator.handlers.worker_recycler`, the
    web_background monitor in
    :mod:`openstudio_operator.handlers.web_background_monitor`) imports
    this factory instead of instantiating ``AppsV1Api`` inline. The
    factory loads the in-cluster / kubeconfig fallback via
    :func:`openstudio_operator._k8s.load_operator_kube_config` (the
    SINGLE public loader, issue #305) and caches the client for the
    operator's lifetime, sharing the underlying
    :class:`kubernetes.client.ApiClient` HTTP connection pool across
    every operator tick. Inline ``AppsV1Api()`` calls outside this
    function are a regression; the
    AST test in
    ``tests/test_singleton_registry_coverage.py::test_only_one_v1_api_construction_point_per_factory``
    fails the CI gate loudly.
    """
    global _operator_apps_api
    if _operator_apps_api is None:
        try:
            load_operator_kube_config()
        except ConfigException:
            # CI / bare-clone environments: leave the default
            # Configuration uninitialised. The AppsV1Api() constructor
            # stores the default without raising; actual API calls
            # would fail later, but handlers fail closed via
            # HANDLER_TICK_FAILURES_TOTAL. The strict operator_custom_objects_api
            # factory above still raises so the singleton guard correctly
            # skips the tick when config is truly unavailable.
            logger.warning(
                "K8s config not loaded for AppsV1Api: returning placeholder client."
            )
        _operator_apps_api = AppsV1Api()
    return _operator_apps_api


def operator_batch_api() -> BatchV1Api:
    """Return the process-wide :class:`BatchV1Api` (issue #251).

    The SINGLE ``BatchV1Api()`` construction point in the operator. The
    retention pipeline's archive Job create/read/delete
    (:mod:`openstudio_operator.retention`) and the prune CronJob
    (:mod:`openstudio_operator.prune_entrypoint`) import this factory
    instead of instantiating ``BatchV1Api`` inline. The factory loads
    the in-cluster / kubeconfig fallback via
    :func:`openstudio_operator._k8s.load_operator_kube_config` (the
    SINGLE public loader, issue #305) and caches the client for the
    operator's lifetime. Inline ``BatchV1Api()`` calls outside this
    function are a regression; the
    AST test in
    ``tests/test_singleton_registry_coverage.py::test_only_one_v1_api_construction_point_per_factory``
    fails the CI gate loudly.
    """
    global _operator_batch_api
    if _operator_batch_api is None:
        try:
            load_operator_kube_config()
        except ConfigException:
            # CI / bare-clone environments: leave the default
            # Configuration uninitialised. The BatchV1Api() constructor
            # stores the default without raising; actual API calls
            # would fail later, but handlers fail closed via
            # HANDLER_TICK_FAILURES_TOTAL. The strict operator_custom_objects_api
            # factory above still raises so the singleton guard correctly
            # skips the tick when config is truly unavailable.
            logger.warning(
                "K8s config not loaded for BatchV1Api: returning placeholder client."
            )
        _operator_batch_api = BatchV1Api()
    return _operator_batch_api


def operator_core_api() -> CoreV1Api:
    """Return the process-wide :class:`CoreV1Api` (issue #251).

    The SINGLE ``CoreV1Api()`` construction point in the operator. The
    SLA monitor's worker-pod-eviction path
    (:mod:`openstudio_operator.handlers.analysis_sla`,
    :func:`_escalate_analysis`), the web_background monitor's worker-pod
    liveness check
    (:mod:`openstudio_operator.handlers.web_background_monitor`), and the
    prune CronJob's Event emitter
    (:mod:`openstudio_operator.prune_entrypoint`) import this factory
    instead of instantiating ``CoreV1Api`` inline. The factory loads the
    in-cluster / kubeconfig fallback via
    :func:`openstudio_operator._k8s.load_operator_kube_config` (the
    SINGLE public loader, issue #305) and caches the client for the
    operator's lifetime. Inline ``CoreV1Api()`` calls outside this
    function are a regression; the
    AST test in
    ``tests/test_singleton_registry_coverage.py::test_only_one_v1_api_construction_point_per_factory``
    fails the CI gate loudly.
    """
    global _operator_core_api
    if _operator_core_api is None:
        try:
            load_operator_kube_config()
        except ConfigException:
            # CI / bare-clone environments: leave the default
            # Configuration uninitialised. The CoreV1Api() constructor
            # stores the default without raising; actual API calls
            # would fail later, but handlers fail closed via
            # HANDLER_TICK_FAILURES_TOTAL. The strict operator_custom_objects_api
            # factory above still raises so the singleton guard correctly
            # skips the tick when config is truly unavailable.
            logger.warning(
                "K8s config not loaded for CoreV1Api: returning placeholder client."
            )
        _operator_core_api = CoreV1Api()
    return _operator_core_api


def reset_operator_k8s_client() -> None:
    """Drop every cached K8s client (test seam — issues #158 + #251).

    The production factories cache for the operator's lifetime; tests that
    swap ``kubernetes.config.load_incluster_config`` or
    ``kubernetes.config.load_kube_config`` need to clear the cache between
    cases so the next call to any ``operator_*_api()`` factory re-runs
    the load path with the freshly patched loader. Idempotent; harmless to
    call when nothing is cached.
    """
    global _operator_custom_objects_api, _operator_apps_api
    global _operator_batch_api, _operator_core_api
    _operator_custom_objects_api = None
    _operator_apps_api = None
    _operator_batch_api = None
    _operator_core_api = None


def _get_guard() -> SingletonGuard:
    global _process_guard, _operator_custom_objects_api
    if _process_guard is None:
        # Issue #158 + #251 — the guard-rebuild path also clears the K8s
        # client caches so the new guard's client is built against the
        # currently loaded (or freshly-loaded) kubeconfig. The reset is
        # the test seam that keeps ``test_singleton_guard.py``'s
        # config-loader mocks in effect across cases: the existing tests
        # reset ``_process_guard`` to ``None`` between cases so a freshly
        # patched ``load_incluster_config`` is honored — without this
        # reset the factory would hand back a client built under a
        # previous test's mocks and the new mocks would never fire.
        # Production operators never reset ``_process_guard`` (the guard
        # is built once at the first tick and reused for the process
        # lifetime), so this only matters under test.
        _operator_custom_objects_api = None
        _process_guard = SingletonGuard(operator_custom_objects_api())
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
        # Mapping, not dict: kopf >=1.4x timer invocations deliver `body` as
        # kopf._cogs.structs.bodies.Body (a MappingView, not a dict subclass) —
        # a dict-only check skips EVERY in-cluster tick (live-found 2026-08-18,
        # issue #67 walkthrough; CI passes plain dicts, so tests never saw it).
        if not isinstance(body, Mapping) or not namespace:
            log.warning(
                "%s skipped: kopf invocation carried no body/namespace — cannot resolve "
                "the active CR (D05)",
                getattr(fn, "__name__", "handler"),
            )
            return None
        try:
            active = _get_guard().is_active(body, str(namespace))
        except (ApiException, ConfigException, SingletonGuardError) as exc:
            # Issue #307 — the gate's outer wrapper used to swallow this
            # exception silently (single WARNING log, no counter bump). The
            # inner per-handler ``try/except`` blocks only fire when the
            # apiserver reaches the handler body; here the exception is
            # raised BEFORE the inner wrapper ever sees the tick, so a
            # sustained apiserver outage produced zero
            # ``handler_tick_failures_total{module=...,error_type=ApiException}``
            # increments and SREs (alerting on the per-module counter, #117)
            # had no signal. Increment at the same site that logs the
            # WARNING so the per-tick observation is the SAME as the
            # log line — one bump per suppressed tick, labelled by the
            # wrapped handler's ``__name__`` (preserved by
            # ``functools.wraps`` at install time, falls back to
            # ``"handler"`` for un-named callables).
            HANDLER_TICK_FAILURES_TOTAL.labels(
                namespace=str(namespace),
                name=str(kwargs.get("name") or ""),
                module=getattr(fn, "__name__", "handler"),
                error_type=type(exc).__name__,
            ).inc()
            log.warning(
                "%s skipped this tick, retrying next poll — singleton guard could not "
                "resolve the active CR (%s: %s)",
                getattr(fn, "__name__", "handler"),
                type(exc).__name__,
                exc,
            )
            return None
        if not active:
            # Issue #403 — per-tick loser-suppression counter. The
            # change-gated SINGLETON_ELECTION_TOTAL{outcome="conflict"}
            # (issue #239) fires only when ``enforce()`` observes a
            # snapshot DIFFERENT from ``_last_state``, so a stable
            # multi-CR namespace produces zero per-tick signals and this
            # branch was a bare log.debug — the sustained per-tick load
            # was diagnosable only by reading logs at debug level. One
            # bump per suppressed tick, labelled by the wrapped
            # handler's ``__name__`` (same module vocabulary as
            # HANDLER_TICK_FAILURES_TOTAL) + the LOSER CR's identity.
            # Cardinality is bounded by the one-winner-per-namespace
            # invariant (D05) — one series per (module, namespace,
            # name) tuple. The log.debug stays: this counter does not
            # change the noise channel, it adds the /metrics signal.
            SINGLETON_LOSER_SKIPS_TOTAL.labels(
                module=getattr(fn, "__name__", "handler"),
                namespace=str(namespace),
                name=_cr_name(body),
            ).inc()
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

    Issue #250 — Python-level registry cross-check. Before wrapping each
    OSCM timer, the gate verifies the handler is registered in
    :mod:`openstudio_operator._oscm_handlers` (via an explicit
    ``register(handler_id, fn)`` call in the handler module). A new OSCM
    timer that forgot to register is logged at ERROR level and SKIPPED —
    the test
    ``tests/test_singleton_registry_coverage.py::test_python_registry_includes_all_oscm_spawning_handlers``
    fails the build before the operator can boot with a silently
    un-gated handler. Issue #285 retired the legacy-id whitelist that
    grandfathered the four pre-#250 timers — every OSCM handler must
    now register explicitly, so this cross-check has no exemptions.
    """
    # Local import: avoiding a top-level dependency on the Python-level
    # registry so the gate's import graph stays shallow (the registry is
    # only consulted when wrapping OSCM timers, not at import time).
    from openstudio_operator import _oscm_handlers

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
    skipped_unregistered = 0
    for index, handler in enumerate(list(handlers)):
        if not _selector_matches_oscms(handler):
            continue
        fn = getattr(handler, "fn", None)
        if fn is None or getattr(fn, GUARD_MARKER, False):
            continue
        handler_id = getattr(handler, "id", "?")
        # Issue #250 — cross-check the Python-level registry. A new OSCM
        # timer that forgot to call register() is logged loudly and the
        # wrap is SKIPPED so the test gate can fail before the operator
        # boots with a silently un-gated handler. Issue #285 retired the
        # legacy-id set, so there are no exemptions.
        if not _oscm_handlers.is_registered(handler_id):
            logger.error(
                "singleton guard (D05): OSCM timer %r is registered with kopf "
                "but NOT in the Python-level registry (issue #250). New "
                "handler modules MUST call "
                "openstudio_operator._oscm_handlers.register(%r, fn) at "
                "module import time. The handler is NOT gated this run — "
                "the singleton guard (D05) will NOT fire on its ticks until "
                "the registration is added. See "
                "tests/test_singleton_registry_coverage.py.",
                handler_id,
                handler_id,
            )
            skipped_unregistered += 1
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
    if skipped_unregistered:
        # Promote the per-handler ERROR above to a loud summary so the
        # operator on-call sees one line per boot, not N scattered logs.
        logger.error(
            "singleton guard (D05): %d OSCM timer(s) registered with kopf "
            "but missing from the Python-level registry (issue #250) — "
            "those handlers are NOT gated. See "
            "tests/test_singleton_registry_coverage.py.",
            skipped_unregistered,
        )
    return wrapped


def _emit_kopf_event(obj: dict, event_type: str, reason: str, message: str) -> None:
    kopf.event(obj, type=event_type, reason=reason, message=message)


def _check(namespace: str | None, logger: logging.Logger) -> None:
    """Shared loud-check body for the kopf startup/event wrappers."""
    if not namespace:
        logger.debug("singleton guard: no namespace in context — check skipped")
        return
    try:
        guard = _get_guard()
        items = guard.list_crs(namespace)
    except (ApiException, ConfigException) as exc:
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
    # Issue #116 — emit a one-time Warning per CR whose ``spec.redisUrl``
    # is empty. The operator cannot service Modules 3/5 (worker
    # recycling, web_background stall) without a Redis URL, and the
    # historical default ``redis://:openstudio@queue...`` was a
    # secret-leak that the empty default now explicitly rejects. The
    # single-callback cache is keyed on ``(namespace, name)`` so the
    # message is at most once per CR per operator restart.
    _emit_redis_url_guard_events(items, logger=logger)


_redis_url_warned: set[tuple[str, str]] = set()


def _has_redis_secret_ref(spec: dict) -> bool:
    """Whether ``spec.redisCredentials.secretRef`` is populated (issue #463).

    Tolerant shape check (the CRD enforces ``{name, key}`` strings; this
    runs on raw watch bodies, so it must not raise on hand-crafted specs):
    any dict carrying non-empty ``name`` and ``key`` counts as set.
    """
    credentials = spec.get("redisCredentials")
    if not isinstance(credentials, dict):
        return False
    secret_ref = credentials.get("secretRef")
    if not isinstance(secret_ref, dict):
        return False
    return bool(secret_ref.get("name")) and bool(secret_ref.get("key"))


def _emit_redis_url_guard_events(items, *, logger: logging.Logger) -> None:
    """Emit a one-time ``RedisUrlEmpty`` Warning Event per CR with empty ``spec.redisUrl`` (issue #116).

    The operator cannot service Modules 3/5 (worker recycling, web_background
    stall) without a Redis URL, and the historical default
    ``redis://:openstudio@queue...`` was a secret-leak that the empty default
    now explicitly rejects. The single-callback cache is keyed on
    ``(namespace, name)`` so the message is at most once per CR per operator
    restart.

    Issue #463: a CR that sets ``spec.redisCredentials.secretRef`` has a
    Secret-sourced URL — an empty ``spec.redisUrl`` is then the RECOMMENDED
    production shape, not a misconfiguration, so the guard stays silent for
    it (the URL resolves at client-construction time; a missing Secret/key
    surfaces there as ``RedisCredentialResolutionError`` on the Redis paths).

    The Warning Event is emitted directly via :func:`kopf.event` (the same
    kopf chokepoint :func:`_emit_kopf_event` uses for the SINGLETON_* events
    above). :func:`_check` is only called from :func:`singleton_guard_startup`
    (``@kopf.on.startup``) and :func:`singleton_guard_event``
    (``@kopf.on.event``), both active kopf callbacks where ``settings_var``
    is populated and the posting engine is enabled — so the queue/defer/drain
    detour through :mod:`openstudio_operator.handlers` is unnecessary, and
    routing it through here would invert the natural module dependency
    direction (handlers → singleton, not singleton → handlers). Issue #304.
    """
    for item in items:
        # Items may be raw OSCM dicts (``apiVersion``/``kind`` + ``metadata``
        # envelope) or the inner ``metadata`` already-extracted dict, depending
        # on the caller. Use a tolerant accessor.
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else item
        ns = str(meta.get("namespace") or "")
        nm = str(meta.get("name") or "")
        if not ns or not nm:
            logger.debug(
                "redis URL guard: skipping nameless item (got keys=%s)", sorted(item.keys()),
            )
            continue
        if (ns, nm) in _redis_url_warned:
            continue
        spec = item.get("spec") or {}
        if str(spec.get("redisUrl") or "") == "" and not _has_redis_secret_ref(spec):
            _redis_url_warned.add((ns, nm))
            logger.warning(
                "OSCM %s/%s has empty spec.redisUrl — issue #116: the operator "
                "cannot service Modules 3/5 (worker recycler + web_background "
                "stall). Set spec.redisCredentials.secretRef to a Secret key "
                "holding the full redis://:password@queue.<namespace>"
                ".svc.cluster.local:6379 URL (recommended, issue #463), or set "
                "spec.redisUrl explicitly to a CREDENTIAL-FREE redis:// URL "
                "(inline passwords are rejected at apply time since #463). "
                "The previous default `redis://:openstudio@queue...` baked a "
                "public-facing password into every published CRD and has been "
                "removed.",
                ns, nm,
            )
            _emit_kopf_event(
                {"metadata": {"namespace": ns, "name": nm}},
                "Warning",
                "RedisUrlEmpty",
                (
                    "spec.redisUrl is empty and spec.redisCredentials.secretRef "
                    "is unset (issue #116). Operator Modules 3/5 (worker "
                    "recycler + web_background stall) will be no-ops until a "
                    "Redis URL is configured. Recommended: store the full "
                    "redis://:password@queue... URL in a Secret and reference "
                    "it via spec.redisCredentials.secretRef (issue #463). The "
                    "previous default exposed the kind-recipe password "
                    "`openstudio` and has been removed."
                ),
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
