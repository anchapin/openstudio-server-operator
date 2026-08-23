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
from typing import TypeVar

import kopf
from kubernetes.client import (
    ApiException,
    AppsV1Api,
    BatchV1Api,
    CoreV1Api,
    CustomObjectsApi,
)
from kubernetes.config import ConfigException

from openstudio_operator._constants import CRD_GROUP, CRD_PLURAL, CRD_SPEC, CRD_VERSION
from openstudio_operator._k8s import apply_request_timeout, load_operator_kube_config
from openstudio_operator._time import utc_parser
from openstudio_operator.config import RedisSecretRef
from openstudio_operator.events import EventSink, emit_kopf_event
from openstudio_operator.metrics import (
    HANDLER_TICK_FAILURES_TOTAL,
    SINGLETON_ELECTION_TOTAL,
    SINGLETON_EXPECTED_HANDLERS,
    SINGLETON_LOSER_SKIPS_TOTAL,
    SINGLETON_WRAPPED_HANDLERS,
)

logger = logging.getLogger(__name__)

SINGLETON_CONFLICT_EVENT = "SingletonConflict"
SINGLETON_ACTIVE_EVENT = "SingletonActive"

#: Attribute marking handler fns already wrapped by the gate (idempotency).
GUARD_MARKER = "_openstudio_singleton_guarded"

_MISSING_CREATED = datetime.max.replace(tzinfo=UTC)


class SingletonGuardError(Exception):
    """Singleton-guard failure: unparseable CR metadata."""


#: Domain-flavored parse seam (issue #506): the ``_time.utc_parser`` closure
#: rejects ``None`` and re-raises parse failures as
#: :class:`SingletonGuardError` with the caller's ``context`` prefix — this
#: module's public exception contract.
_parse_utc = utc_parser(SingletonGuardError)


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
            CRD_GROUP, CRD_VERSION, namespace, CRD_PLURAL
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


# Issue #158 + #251 — the operator's single ``CustomObjectsApi()`` /
# ``AppsV1Api()`` / ``BatchV1Api()`` / ``CoreV1Api()`` construction points.
# Every handler and entrypoint that talks to the K8s API server imports the
# factories below — never the client classes directly. The per-class module
# globals are the cache slots (the :func:`reset_operator_k8s_client` test
# seam; ``tests/test_client_factory.py`` also patches
# ``singleton._operator_core_api`` directly). Since issue #494 the shared
# :func:`_cached_k8s_api` helper owns the load/cache/fallback logic ONCE;
# the AST tests in ``tests/test_singleton_registry_coverage.py``
# (``test_only_one_custom_objects_api_construction_point`` and
# ``test_only_one_v1_api_construction_point_per_factory``) enforce "exactly
# one bare no-arg construction call per client type in this file", which is
# why each wrapper passes its construction as a thunk carrying the literal
# ``XApi()`` call.
_operator_custom_objects_api: CustomObjectsApi | None = None
_operator_apps_api: AppsV1Api | None = None
_operator_batch_api: BatchV1Api | None = None
_operator_core_api: CoreV1Api | None = None

_K8sClientT = TypeVar("_K8sClientT")


def _cached_k8s_api(
    cache_attr: str,
    build: Callable[[], _K8sClientT],
    *,
    label: str,
    strict: bool,
) -> _K8sClientT:
    """Return the process-wide K8s client cached under ``cache_attr`` (issue #494).

    Shared engine for the four ``operator_*_api`` factories (issues #158 +
    #251). Behaviour, documented ONCE:

    * Cache: the client is cached in the factory's per-class module global
      (``cache_attr``) for the process's lifetime; all callers share one
      instance, so the underlying :class:`kubernetes.client.ApiClient`
      (HTTP connection pool, retry config) is reused across every operator
      tick. A non-``None`` cache slot short-circuits — no loader call, no
      construction.
    * Load: :func:`openstudio_operator._k8s.load_operator_kube_config` —
      the SINGLE public loader (issue #305) — tries
      ``kubernetes.config.load_incluster_config`` first, falling back to
      ``load_kube_config`` for local ``kopf run`` dev sessions. kopf >=1.44
      never initializes client-python's default ``Configuration``, so a
      client built with no loaded config raises ``LocationValueError`` on
      every call (proven live in #66's kind validation; since #493 the
      timer wrappers count + skip that).
    * Strict (``strict=True`` — the CustomObjectsApi factory): a
      :class:`kubernetes.config.ConfigException` from the loader propagates
      — callers fail closed (skip the tick, retry next poll per D12). The
      singleton guard's ``_gated`` wrapper relies on this raise to skip the
      tick when config is truly unavailable.
    * Lenient (``strict=False`` — the three ``*V1Api`` factories): the
      ``ConfigException`` is swallowed with one WARNING naming the API type
      (``label``) and a placeholder client is constructed against the
      uninitialised default ``Configuration`` — the ``*V1Api()`` constructor
      stores the default without raising; actual API calls fail later and
      handlers fail closed via ``HANDLER_TICK_FAILURES_TOTAL`` (issues #251
      / #286). This keeps the operator booting in CI / bare-clone
      environments where no kubeconfig exists.
    * Single construction point: each wrapper's ``build`` thunk carries the
      literal ``XApi()`` call so the AST gates in
      ``tests/test_singleton_registry_coverage.py`` (#158 / #251) keep
      seeing exactly one bare construction per client type in this file.
      Inline ``*Api()`` calls anywhere else are a regression: a future
      change to the loader (kubeconfig Secret reference, network-proxy
      client, …) would silently leave inline callsites behind, and the CI
      gate fails loudly.
    * Bounded requests (issue #579): every constructed client gets
      :func:`openstudio_operator._k8s.apply_request_timeout` — a
      :class:`openstudio_operator._k8s.BoundedK8sRequest` wrapper over
      the client's ``rest_client.request`` that defaults each request's
      ``_request_timeout`` to
      :data:`openstudio_operator._constants.K8S_REQUEST_TIMEOUT_SECONDS`
      (15 s) and translates the resulting urllib3 timeout into an
      in-``SKIP_TICK_EXCEPTIONS`` ``ApiException``. A black-holed
      apiserver connection therefore becomes a counted skip-tick (D12,
      retry next poll) instead of a forever-blocked timer — the pod
      stays ``Running`` and /metrics stays alive, so pre-#579 nothing
      self-healed.
    * Reset seam: tests drop the caches via
      :func:`reset_operator_k8s_client` (or by patching a slot directly)
      so the next factory call re-runs the load path with the freshly
      patched loaders.
    """
    cached = globals().get(cache_attr)
    if cached is not None:
        return cached
    try:
        load_operator_kube_config()
    except ConfigException:
        if strict:
            raise
        logger.warning(
            "K8s config not loaded for %s: returning placeholder client.", label
        )
    client = apply_request_timeout(build())
    globals()[cache_attr] = client
    return client


def operator_custom_objects_api() -> CustomObjectsApi:
    """Process-wide :class:`CustomObjectsApi` (issue #158) — strict; raises on no-config.

    Consumers: ``StatusStore``'s ``.status`` RMW path, ``SingletonGuard.list_crs``,
    and every handler's OSCM custom-object access. See :func:`_cached_k8s_api`.
    """
    return _cached_k8s_api(
        "_operator_custom_objects_api",
        lambda: CustomObjectsApi(),
        label="CustomObjectsApi",
        strict=True,
    )


def operator_apps_api() -> AppsV1Api:
    """Process-wide :class:`AppsV1Api` (issue #251) — lenient placeholder on no-config.

    Consumers: the worker recycler and the web_background monitor's Deployment
    reads/patches. See :func:`_cached_k8s_api`.
    """
    return _cached_k8s_api(
        "_operator_apps_api",
        lambda: AppsV1Api(),
        label="AppsV1Api",
        strict=False,
    )


def operator_batch_api() -> BatchV1Api:
    """Process-wide :class:`BatchV1Api` (issue #251) — lenient placeholder on no-config.

    Consumers: the retention pipeline's archive Job create/read/delete and the
    prune CronJob. See :func:`_cached_k8s_api`.
    """
    return _cached_k8s_api(
        "_operator_batch_api",
        lambda: BatchV1Api(),
        label="BatchV1Api",
        strict=False,
    )


def operator_core_api() -> CoreV1Api:
    """Process-wide :class:`CoreV1Api` (issue #251) — lenient placeholder on no-config.

    Consumers: the SLA monitor's worker-pod eviction, the web_background
    monitor's worker-pod liveness check, the prune CronJob's Event emitter,
    and the #463 Secret read in :mod:`openstudio_operator.client_factory`.
    See :func:`_cached_k8s_api`.
    """
    return _cached_k8s_api(
        "_operator_core_api",
        lambda: CoreV1Api(),
        label="CoreV1Api",
        strict=False,
    )


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
    return str(group) == CRD_GROUP and any(
        str(name) == CRD_PLURAL for name in names if name is not None
    )


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

    Issue #491 — the wrap count is recorded at boot on
    :data:`openstudio_operator.metrics.SINGLETON_WRAPPED_HANDLERS` (set
    at the end of this function, and to ``0`` on the registry-internals
    mismatch branch below): ``0`` on a booted operator that expects
    timers is the silent-unwrap failure mode the kopf pin documents,
    made scrapeable. The gauge counts handlers CARRYING the gate marker
    (not newly-wrapped ones), so an idempotent re-install — which wraps
    nothing new — keeps reporting the actual gated population instead
    of clobbering the reading to 0.

    Issue #570 — the EXPECTED count is recorded alongside it on
    :data:`openstudio_operator.metrics.SINGLETON_EXPECTED_HANDLERS`,
    sized from the OSCM spawning-handler population kopf actually
    reports (this function's own scan, counted before wrapping). A
    PARTIAL unwrap — one handler skipped on the missing-registration
    (#250) or ``dataclasses.replace`` TypeError ``continue`` paths —
    leaves a kopf-registered OSCM timer running UN-GATED while the #491
    wrap gauge reads a plausible ``3 of 4``; the pair makes the gap
    scrapeable and alertable (``wrapped < expected``). On the
    registry-internals-mismatch branch the kopf-side scan is impossible,
    so the Python-level ``_oscm_handlers.REGISTRY`` population (the
    operator's own declaration of its OSCM timers) stands in as the
    expectation — keeping ``0 < expected`` firing for the total-unwrap
    shape under a strict ``<`` alert expression.
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
        # Issue #491 — the silent-unwrap shape, made scrapeable: whatever
        # the reason the gate could not run (kopf upgrade moved the
        # private _spawning._handlers layout), the boot gauge must record
        # ZERO wrapped handlers so the wrap-gap alert fires.
        SINGLETON_WRAPPED_HANDLERS.set(0)
        # Issue #570 — the kopf-side scan is impossible here, so the
        # Python-level registry (#250 — the operator's own declaration of
        # its OSCM timer population) stands in as the expectation. This
        # keeps ``wrapped == 0 < expected`` firing under the strict-``<``
        # alert expression: with expected sourced from the (unreadable)
        # kopf layout the pair would read 0 < 0 and the total unwrap
        # would fall back to being invisible again.
        SINGLETON_EXPECTED_HANDLERS.set(len(_oscm_handlers.REGISTRY))
        return 0

    # Issue #570 — expected-count pre-pass: the OSCM spawning-handler
    # population kopf actually reports (the same scan the coverage test
    # performs), counted BEFORE wrapping. Entries whose fn is None are
    # excluded — they can never carry the gate, so counting them would
    # permanently depress the pair; entries already carrying the marker
    # (an idempotent re-install) ARE counted, keeping expected == gated
    # on the healthy re-install path.
    expected = sum(
        1
        for h in handlers
        if _selector_matches_oscms(h) and getattr(h, "fn", None) is not None
    )
    SINGLETON_EXPECTED_HANDLERS.set(expected)

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
    # Issue #491 — boot-time wrap-count gauge. Counted as the OSCM
    # spawning handlers CARRYING the gate marker after this pass (not the
    # newly-wrapped ``wrapped``): a fresh boot equals ``wrapped`` (every
    # previously-unmarked handler was just wrapped), and an idempotent
    # re-install (0 new wraps) keeps reporting the actual gated
    # population — a re-invocation must not clobber the gauge to 0, the
    # value that IS the silent-unwrap alert expression. Handlers skipped
    # for missing registration carry no marker and are correctly absent.
    # Issue #570 — the #491 reading alone cannot express "3 is wrong";
    # the expected-count gauge set in the pre-pass above is the
    # denominator that makes the partial skip (wrapped < expected) a
    # scrapeable, alertable gap instead of a plausible-looking fraction.
    gated = sum(
        1
        for h in handlers
        if _selector_matches_oscms(h)
        and getattr(getattr(h, "fn", None), GUARD_MARKER, False)
    )
    SINGLETON_WRAPPED_HANDLERS.set(gated)
    return wrapped


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
        guard.enforce(items, logger=logger, emit=emit_kopf_event)
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
    # historical default `redis://:openstudio@queue...` was a
    # secret-leak that the empty default now explicitly rejects. The
    # single-callback cache is keyed on ``(namespace, name)`` so the
    # message is at most once per CR per operator restart.
    _emit_redis_url_guard_events(items, logger=logger)
    # Issue #606 — the RBAC resourceNames fence is fail-visible: probe the
    # Secret read once per CR so a 403 (custom secretRef name outside the
    # Role's canonical grant) surfaces as a Warning Event naming the RBAC
    # cause, not just as counted skip-ticks.
    _emit_redis_secret_ref_forbidden_events(items, logger=logger)


_redis_url_warned: set[tuple[str, str]] = set()


def _redis_secret_ref_of(spec: dict) -> RedisSecretRef | None:
    """Extract a well-formed ``secretRef`` from a raw spec (issues #463, #606).

    Tolerant shape check (the CRD enforces ``{name, key}`` strings; this
    runs on raw watch bodies, so it must not raise on hand-crafted specs):
    returns a :class:`~openstudio_operator.config.RedisSecretRef` only when
    ``spec.redisCredentials.secretRef`` carries non-empty string ``name``
    and ``key``; anything else (absent, half-configured, wrong types)
    yields ``None``.
    """
    credentials = spec.get("redisCredentials")
    if not isinstance(credentials, dict):
        return None
    secret_ref = credentials.get("secretRef")
    if not isinstance(secret_ref, dict):
        return None
    name = secret_ref.get("name")
    key = secret_ref.get("key")
    if not isinstance(name, str) or not isinstance(key, str) or not name or not key:
        return None
    return RedisSecretRef(name=name, key=key)


def _has_redis_secret_ref(spec: dict) -> bool:
    """Whether ``spec.redisCredentials.secretRef`` is populated (issue #463).

    Tolerant shape check (the CRD enforces ``{name, key}`` strings; this
    runs on raw watch bodies, so it must not raise on hand-crafted specs):
    any dict carrying non-empty ``name`` and ``key`` counts as set.
    """
    return _redis_secret_ref_of(spec) is not None


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

    The Warning Event is emitted via
    :func:`openstudio_operator.events.emit_kopf_event` (the shared direct
    ``kopf.event`` wrapper, issue #496 — the same chokepoint the SINGLETON_*
    events above use). :func:`_check` is only called from :func:`singleton_guard_startup`
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
            emit_kopf_event(
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


_redis_secret_ref_forbidden_warned: set[tuple[str, str]] = set()


def _emit_redis_secret_ref_forbidden_events(items, *, logger: logging.Logger) -> None:
    """Issue #606 — one-time ``RedisSecretRefForbidden`` Warning per CR whose
    secretRef names a Secret the operator Role cannot read.

    The #606 fence is the RBAC ``resourceNames`` contract in
    ``deploy/rbac.yaml``: the operator SA's ``secrets: get`` grant covers
    only the canonical Secret name(s) the shipped tooling creates (default:
    ``openstudio-redis``). The CRD pattern stays wider (any
    ``openstudio-redis*`` name is apply-legal), so a CR naming a custom
    Secret is denied at READ time — pre-#606 that surfaced only as counted
    skip-ticks and a key-layout ``"unreachable"`` log line carrying a bare
    ``403 Forbidden``. This probe makes it fail-visible: once per CR, the
    guard resolves the secretRef through the SAME factory path the ticks
    use and, on a 403 (detected via the ``__cause__`` ApiException the
    factory chains), emits a Warning Event naming the RBAC cause and both
    remedies — widen the Role's ``resourceNames`` or point the secretRef
    at a granted Secret.

    Semantics:

    * **403** — mark ``(namespace, name)`` as warned (at-most-once per CR
      per operator restart, mirroring ``_redis_url_warned``), log loudly,
      and emit the Event. Re-probing a known-403 on every watch event
      would only re-read a Secret the Role denies forever.
    * **Success** — mark and stay silent (the probe doubles as a warm-up
      of the factory's client LRU).
    * **Any other resolution failure** (404 missing Secret, missing key,
      bad value, no kubeconfig / connectivity) — stay silent AND stay
      unmarked: those are the #567 surfaces (key-layout status, counted
      skip-ticks) and may be transient, so the next watch event re-probes
      once the condition clears.

    The probe must never crash the guard: every failure mode is caught
    (broad-except by design — a boot-time probe outranking the operator's
    own boot would be its own incident). The factory import is
    function-local because ``client_factory`` imports
    ``operator_core_api`` from THIS module — a module-level import would
    be a cycle; deferred to call time it is the standard break.
    """
    from openstudio_operator.client_factory import get_read_only_redis_client

    for item in items:
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else item
        ns = str(meta.get("namespace") or "")
        nm = str(meta.get("name") or "")
        if not ns or not nm:
            continue
        if (ns, nm) in _redis_secret_ref_forbidden_warned:
            continue
        spec = item.get("spec") or {}
        ref = _redis_secret_ref_of(spec) if isinstance(spec, dict) else None
        if ref is None:
            continue
        try:
            get_read_only_redis_client("", secret_ref=ref, namespace=ns)
        except Exception as exc:  # noqa: BLE001 — the probe must never crash the guard
            cause = exc.__cause__
            if not (isinstance(cause, ApiException) and cause.status == 403):
                continue
            _redis_secret_ref_forbidden_warned.add((ns, nm))
            logger.warning(
                "OSCM %s/%s: Secret read for spec.redisCredentials."
                "secretRef %r DENIED (403 Forbidden, issue #606): the "
                "operator Role grants secrets:get only on the canonical "
                "name(s) in deploy/rbac.yaml resourceNames (default: "
                "'openstudio-redis'). Either add %r to the Role's "
                "resourceNames (a deliberate, reviewable RBAC change) or "
                "point the secretRef at a granted Secret. Until then every "
                "Redis-dependent tick skips with a counted failure.",
                ns, nm, ref.name, ref.name,
            )
            emit_kopf_event(
                {"metadata": {"namespace": ns, "name": nm}},
                "Warning",
                "RedisSecretRefForbidden",
                (
                    f"Secret read for spec.redisCredentials.secretRef "
                    f"{ref.name!r} was denied (403 Forbidden, issue #606): "
                    f"the default operator Role (deploy/rbac.yaml) grants "
                    f"secrets:get only on the canonical name(s) via "
                    f"resourceNames (default: 'openstudio-redis'). Either "
                    f"add {ref.name!r} to the Role's resourceNames (a "
                    f"deliberate, reviewable RBAC change) or point "
                    f"spec.redisCredentials.secretRef at a granted Secret. "
                    f"Redis-dependent ticks skip with counted failures "
                    f"until resolved."
                ),
            )
        else:
            _redis_secret_ref_forbidden_warned.add((ns, nm))


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


@kopf.on.event(**CRD_SPEC)
def singleton_guard_event(namespace: str, logger: kopf.Logger, **_: object) -> None:
    """On every OSCM change (incl. the initial listing): re-resolve + enforce."""
    _check(namespace, logger)
