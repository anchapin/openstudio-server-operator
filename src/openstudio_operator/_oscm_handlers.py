"""Python-level OSCM handler registry (issue #250).

The kopf registry is the operational register for the operator's
``@kopf.timer`` / ``@kopf.daemon`` handlers. The Python-level registry
here provides a declarative seam that EVERY OSCM spawning handler
module calls at module import time via :func:`register` (or, since
#407, its ``__name__``-introspecting convenience :func:`register_fn`),
and the singleton guard (D05, :mod:`openstudio_operator.singleton`)
cross-checks the two registries at gate time. The AST test in
``tests/test_singleton_registry_coverage.py::test_python_registry_includes_all_oscm_spawning_handlers``
pins the agreement invariant so the failure mode that was the entire
motivation of the existing coverage test — "a new OSCM timer registered
without going through install_singleton_guard" — is now caught by the
test too.

Issue #285 retired the legacy-id whitelist
(``KNOWN_LEGACY_OSCM_HANDLER_IDS``) that grandfathered the four
pre-#250 timers (``analysis_sla_monitor``, ``zombie_datapoint_watchdog``,
``web_background_monitor``, ``worker_recycler``). Each of those handler
modules now calls :func:`register` at import time, so the cross-check
passes for them without a side-channel exemption. The Python registry
is therefore the SOLE source of truth for OSCM handler ids known to the
gate; a regression that adds a new OSCM ``@kopf.timer`` without
``register()`` fails the test loudly before the operator can boot with
a silently un-gated handler.

Why both registries? The kopf registry is the OPERATIONAL one — kopf
itself iterates over it to invoke the timer fns. The Python registry
here is the DECLARATIVE one — a maintainer writing a new handler module
states their intent explicitly, and the cross-check in
:func:`openstudio_operator.singleton.install_singleton_guard` verifies
that intent matches the kopf state. The previous test
(``EXPECTED_OSCM_TIMER_HANDLER_IDS``) closed the same gap by asserting
the kopf registry against a hard-coded set in the test file; the new
mechanism pushes the declaration INTO the handler module itself, so the
two always agree without a hand-maintained test set.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

from kubernetes.client import ApiException
from kubernetes.config import ConfigException
from urllib3.exceptions import LocationValueError

from openstudio_operator.config import OperatorConfig, OperatorConfigError
from openstudio_operator.events import EventEmitter
from openstudio_operator.openstudio_client import OpenStudioApiError
from openstudio_operator.redis_client import RedisClientError
from openstudio_operator.status_store import StatusStore, StatusStoreError

#: Issue #717 — graceful-shutdown guard: set by the SIGTERM handler in
#: ``__main__.py``, checked at the start of every ``run_oscm_tick``
#: invocation so mid-tick writes are not interrupted by a new tick starting
#: after the signal arrives.
_shutdown_requested = False

#: Issue #784 — cross-handler failure tracking for composite outage detection.
#: Maps ``module`` -> last failure timestamp (time.time()). Pruned to entries
#: younger than OUTAGE_WINDOW_SECONDS on every tick so the window is exact.
_OUTAGE_WINDOW_SECONDS = 60.0
_cross_handler_failures: dict[str, float] = {}


def is_shutdown_requested() -> bool:
    """Return whether SIGTERM has been received and graceful shutdown is in progress."""
    return _shutdown_requested


#: Process-wide registry of OSCM spawning handlers, keyed by handler id
#: (the kopf ``id`` attribute on the timer registry entry). Every OSCM
#: spawning handler module calls :func:`register` at module import time;
#: the singleton guard cross-checks this dict against the kopf registry
#: at gate time. Issue #285 retired the legacy-id whitelist that used to
#: grandfather the four pre-#250 timers — they register explicitly now.
REGISTRY: dict[str, Callable] = {}


def register(handler_id: str, fn: Callable) -> Callable:
    """Register an OSCM ``@kopf.timer`` / ``@kopf.daemon`` handler.

    Every OSCM spawning handler module (existing or new) MUST call this
    at module import time — after the ``@kopf.timer`` decorator is
    applied and after the function is defined — so the Python-level
    registry knows about the handler before
    :func:`openstudio_operator.singleton.install_singleton_guard` runs.
    The singleton guard cross-checks the Python registry against the kopf
    registry; a missing entry fails the new
    ``test_python_registry_includes_all_oscm_spawning_handlers`` test
    loudly. Returns ``fn`` so the call site can also use it as a no-op
    identity decorator:

    .. code-block:: python

        @kopf.timer(**CRD_SPEC, interval=POLL_INTERVAL_SECONDS)
        @register("my_new_handler", lambda: None)  # bracket-shaped awkward
        def my_new_handler(...): ...

    The canonical pattern (since #407) is the :func:`register_fn`
    convenience at module bottom, which introspects the id from the
    function's ``__name__`` so it cannot drift out of agreement with
    the kopf ``id``:

    .. code-block:: python

        from openstudio_operator._oscm_handlers import register_fn

        @kopf.timer(**CRD_SPEC, interval=POLL_INTERVAL_SECONDS)
        def my_new_handler(...): ...

        register_fn(my_new_handler)

    Handler id MUST match the kopf ``id`` attribute (which is the
    function ``__name__`` by default). Mismatched ids are stored
    verbatim — the cross-check would then fail with the offending id —
    so do not invent a new id here without naming the function to match.
    """
    REGISTRY[handler_id] = fn
    return fn


def register_fn(fn: Callable) -> Callable:
    """Register an OSCM handler under its own ``__name__`` (issue #407).

    Thin call-site convenience over :func:`register`: the handler id is
    introspected from ``fn.__name__`` instead of being typed a second
    time at the call site. Because the kopf ``id`` attribute defaults to
    the decorated function's ``__name__``, this shape cannot disagree
    with the kopf registry entry — the string-typed id duplication the
    four handler modules carried pre-#407 is gone:

    .. code-block:: python

        from openstudio_operator._oscm_handlers import (
            register_fn as _register_oscm_handler,
        )

        @kopf.timer(**CRD_SPEC, interval=POLL_INTERVAL_SECONDS)
        def my_new_handler(...): ...

        _register_oscm_handler(my_new_handler)

    Use :func:`register` directly only when the handler id must differ
    from ``fn.__name__`` (none of the production timers do).
    """
    return register(fn.__name__, fn)


def is_registered(handler_id: str) -> bool:
    """Return whether ``handler_id`` is an OSCM handler known to the registry.

    Membership is membership of the explicit :func:`register` dict. Issue
    #285 retired the legacy-id set that used to short-circuit this check
    for the four pre-#250 timers — every OSCM handler must now register
    explicitly, so the gate's cross-check is membership-based against
    :data:`REGISTRY` alone.
    """
    return handler_id in REGISTRY


def reset_registry() -> None:
    """Drop every explicit :func:`register` entry (test seam).

    The production registry accumulates for the process's lifetime; the
    Pytest session re-imports modules across cases, so the same handler
    module ends up registering twice across the suite. The
    :func:`openstudio_operator.singleton.install_singleton_guard`
    cross-check uses ``REGISTRY`` membership (not the value), so a
    duplicate entry is benign — but tests that assert the explicit-set
    shape (e.g. "no new handler registered") need a clean slate.
    """
    REGISTRY.clear()


# Shared @kopf.timer wrapper decorator (issue #395).
# The four OSCM timer handlers each had a near-identical wrapper that:
#   (1) captures ``_started = time.perf_counter()``,
#   (2) calls the inner ``_xxx_impl(...)`` body,
#   (3) in a ``finally``, observes the elapsed wall-clock on
#       ``HANDLER_TICK_DURATION_SECONDS.labels(module="<name>")``.
# This decorator centralizes that pattern so handler modules no longer
# need to copy-paste the wrapper.
#
# Usage:
#     from openstudio_operator._oscm_handlers import observe_tick_duration
#
#     @kopf.timer(**CRD_SPEC, interval=POLL_INTERVAL_SECONDS)
#     @observe_tick_duration(module="my_handler")
#     def my_handler_impl(...): ...


def observe_tick_duration(*, module: str):
    """Decorator factory for the standard OSCM timer tick duration observation.

    Wraps the inner impl function, captures ``time.perf_counter()`` at entry,
    calls the impl, and in a ``finally`` block observes the elapsed wall-clock
    on ``HANDLER_TICK_DURATION_SECONDS.labels(module=module)``.

    The import of ``HANDLER_TICK_DURATION_SECONDS`` is deferred to call time
    to avoid import-order issues during module load.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            _started = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                from openstudio_operator.metrics import HANDLER_TICK_DURATION_SECONDS
                HANDLER_TICK_DURATION_SECONDS.labels(module=module).observe(
                    time.perf_counter() - _started
                )

        return wrapper

    return decorator


# Shared OSCM timer tick-runner (issue #473).
#
# The four OSCM timer handlers each carried a near-identical ~35-line
# wrapper around their ``run_*_tick`` call: ``OperatorConfig.from_spec``,
# the empty-``serverUrl`` idle warning, ``StatusStore`` /
# ``EventEmitter`` / kube-API construction, and a try/tick/except tail
# that bumped ``HANDLER_TICK_FAILURES_TOTAL`` and logged the per-handler
# skip-tick warning. Only the client wiring
# and the tick invocation differed — and the copy-pasted except tuples
# had already drifted (analysis_sla caught RedisClientError;
# datapoint_watchdog omitted ApiException; web_background_monitor omitted
# OpenStudioApiError). #473 centralizes the wrapper the same way #395
# centralized the timing half and #234 collapsed the queue/drain sinks:
# each handler module now contributes ONLY its ``wire`` closure (the
# per-module clients) and its ``tick`` closure (the ``run_*_tick`` call).

#: The canonical skip-tick exception tuple (issue #473): the UNION of the
#: four historical per-wrapper tuples, so no handler silently lost a
#: catch in the unification. Tick failures AND wiring failures skip the
#: tick and retry on the next poll (D12); anything outside this tuple
#: propagates to kopf as an uncaught handler error (fail-closed, pinned
#: by the issue #249 negative tests in
#: ``tests/test_timer_wrapper_failures.py``).
#:
#: Issue #475 added ``OperatorConfigError`` as an EXPLICIT member. Pre-#475
#: it subclassed ``RedisClientError``, so the historical wrappers'
#: ``except RedisClientError`` catches already caught it at runtime — the
#: runtime-EFFECTIVE historical union always included it, and the literal
#: #473 union silently dropped it when #475 severed the parentage. Explicit
#: membership preserves the D12 posture: a config/wiring failure (TLS
#: CA-bundle misconfiguration, Resque key-layout drift) skips the tick,
#: bumps the failure counter, and retries on the next poll — where a fixed
#: Secret, CR spec, or re-mounted bundle is picked up live — instead of
#: propagating as an uncaught kopf handler error.
#:
#: Issue #493 added the wiring/CONSTRUCTION failure members (the two
#: classes the client-construction path actually raises through — see the
#: explicit block inside the tuple below) and moved ``wire(config)`` (and
#: the store/emitter construction that precedes it) INSIDE the guarded
#: region, so a kubeconfig-load or client-construction failure gets the
#: same counted skip as a tick failure instead of escaping as an uncaught
#: kopf handler error.
SKIP_TICK_EXCEPTIONS: tuple[type[Exception], ...] = (
    OpenStudioApiError,
    StatusStoreError,
    ApiException,
    RedisClientError,
    OperatorConfigError,
    # Wiring/construction failures (issue #493): the classes the
    # client-construction path actually raises through.
    #
    # * ``ConfigException`` — ``kubernetes.config.ConfigException``, what
    #   :func:`openstudio_operator._k8s.load_operator_kube_config` /
    #   the strict ``operator_custom_objects_api`` factory propagate when
    #   neither the in-cluster service-account config nor the
    #   ``~/.kube/config`` fallback can be loaded.
    # * ``LocationValueError`` — ``urllib3.exceptions.LocationValueError``
    #   ("No host specified."), what the kubernetes client's urllib3
    #   transport raises when an API object built against an uninitialised
    #   default ``Configuration`` makes its first real call (the lenient
    #   ``operator_*_api`` placeholder path; proven live in #66's kind
    #   validation). It exists in both urllib3 1.26 and 2.x, so the import
    #   is stable across the whole supported ``kubernetes>=29.3,<37`` range.
    #
    # D12 correctness of skip-on-wiring-failure: the fix for a wiring
    # failure (misconfigured chart, rotated secret, re-mounted kubeconfig)
    # lands OUTSIDE the tick loop, so skipping + retrying next poll picks
    # the fix up live — the same D12 reasoning #475 documented for
    # ``OperatorConfigError``. Deliberately NOT caught: bare ``Exception``
    # or ``ValueError`` — a malformed (non-empty) redis URL still fails
    # closed as an uncaught kopf handler error (pinned by the #249
    # negative tests); only these two named construction-failure classes
    # widened the tuple.
    ConfigException,
    LocationValueError,
)

_DepsT = TypeVar("_DepsT")
_ResultT = TypeVar("_ResultT")


def run_oscm_tick(
    *,
    spec: dict,
    body: dict,
    namespace: str,
    name: str,
    logger: logging.Logger,
    module: str,
    tick_label: str,
    idle_label: str,
    custom_objects_api: Callable[[], object],
    wire: Callable[[OperatorConfig], _DepsT],
    tick: Callable[..., _ResultT],
) -> _ResultT | None:
    """Run one OSCM timer tick through the shared wrapper wiring (issue #473).

    Owns everything the four handler wrappers used to duplicate:

    * ``OperatorConfig.from_spec(spec)`` — the single config path;
    * the #492 config-state posture gauges — ``stamp_config_posture_
      gauges`` sets ``dry_run_active`` / ``server_url_set`` /
      ``redis_url_set`` / ``auto_soft_stop_enabled`` for this CR
      immediately after the config parse succeeds and BEFORE the idle
      check / guarded try (posture is stamped on idle and
      wiring-failing ticks too — it exists independent of tick
      success);
    * the empty-``spec.serverUrl`` idle check — logs ``"<idle_label> idle
      this tick"`` and returns ``None`` WITHOUT touching the failure
      counter (an incomplete CR is not a tick failure);
    * ``StatusStore(namespace, name, custom_objects_api())`` — the caller
      passes its module-level factory so tests can monkeypatch the name
      in the handler module's namespace exactly as before;
    * ``EventEmitter(body=body, dry_run=config.dry_run)`` — the D11 gate;
    * the guarded try — client construction (``custom_objects_api()``,
      :class:`StatusStore`, :class:`EventEmitter`, ``wire(config)``) AND
      the tick invocation run INSIDE the region catching
      :data:`SKIP_TICK_EXCEPTIONS` (issue #493 — pre-#493 the wire call
      ran outside it, so a kubeconfig-load / client-construction failure
      escaped as an uncaught kopf handler error instead of the counted
      skip). Anything in the tuple — whether raised by the wiring or by
      the tick — increments ``HANDLER_TICK_FAILURES_TOTAL.labels(
      namespace, name, module, error_type)`` exactly once and logs the
      single per-handler skip-tick warning (the sole ``%``-formatted
      site of that wording in ``src/``); anything else propagates.

    The handler module contributes the two closures:

    * ``wire(config)`` — build the module's own clients (REST client,
      Redis client, core/apps APIs, tracker caches, …); runs INSIDE the
      guarded try (issue #493), so a raising constructor (kubeconfig
      load, TLS validation, URL parsing that raises an in-tuple class)
      is counted, logged, and skipped like a tick failure — the D12
      posture, because the fix lands outside the tick loop and the next
      poll picks it up live;
    * ``tick(config=..., store=..., emit=..., deps=..., now=...)`` —
      invoke the module's ``run_*_tick``; receives the constructed
      wiring plus ``now=datetime.now(UTC)``.

    Returns the tick's return value, or ``None`` when the tick was
    skipped (idle CR or caught wiring/tick failure) — the D12 "retry
    next poll" posture.

    Issue #469 — the scheduler heartbeat: EVERY invocation stamps
    ``HANDLER_LAST_TICK_TIMESTAMP.labels(module=module)`` to
    ``time.time()`` in the outer ``finally`` below, on ALL terminal
    paths (successful tick, empty-``serverUrl`` idle return, caught
    skip-tuple failure — including wiring/construction failures since
    #493 —, and propagating uncaught exception). A wiring-failing-but-
    scheduled operator therefore reads as ALIVE on the heartbeat gauge
    while its ``HANDLER_TICK_FAILURES_TOTAL`` climbs — exactly the
    sustained-outage signature SREs alert on. The
    heartbeat answers whether the scheduler is invoking this module's
    timer at all — a flat gauge is the only /metrics-visible signature
    of a silently-dead scheduler (kopf internals shift so
    install_singleton_guard returns 0 and the timers are unwrapped, the
    CR is deleted, the kopf scheduling loop wedges); every other
    registry signal is event-driven and reads green while nothing runs.
    The ``dry_run_audit`` handler is CONSCIOUSLY EXCLUDED — it is an
    ``@kopf.on.event`` watch handler, not a timer, so it ticks on CR
    events with no cadence against which a staleness gap could be
    thresholded.
    """
    # Issue #717 — graceful shutdown: skip new ticks once SIGTERM has been received.
    # The in-progress tick completes naturally so in-flight status writes are not
    # interrupted. The singleton guard also prevents new ticks from starting (D05).
    if _shutdown_requested:
        logger.debug("shutdown requested — %s idle this tick", idle_label)
        return None
    # Issue #776 — schema-evolution guard: serverUrl is a required field.
    # If absent or empty, emit a Warning and return None (idle path) so the
    # operator fails loudly rather than silently misconfiguring.
    if not spec.get("serverUrl"):
        logger.warning(
            "spec.serverUrl is required but is absent or empty — "
            "%s idle this tick",
            idle_label,
        )
        from openstudio_operator.metrics import (
            HANDLER_LAST_TICK_TIMESTAMP,
            stamp_config_posture_gauges,
        )

        stamp_config_posture_gauges(
            namespace=namespace,
            name=name,
            dry_run=spec.get("dryRun", False),
            server_url_set=False,
            redis_url_set=bool(spec.get("redisUrl")),
            auto_soft_stop=spec.get("analysisPolicy", {}).get("autoSoftStop", True),
        )
        HANDLER_LAST_TICK_TIMESTAMP.labels(module=module).set(time.time())
        return None
    config = OperatorConfig.from_spec(spec)
    # Issue #492 — config-state posture gauges: stamped immediately
    # after the config parse succeeds and BEFORE the idle check / the
    # guarded try, so all four gauges advance even on ticks that go
    # idle (empty serverUrl → server_url_set=0 IS the posture) or fail
    # wiring (#493) — posture exists independent of tick success. The
    # ``dry_run_audit`` watch handler additionally flips
    # ``dry_run_active`` immediately on a spec.dryRun transition (no
    # next-tick latency); this per-tick stamp is the backstop for all
    # four fields (worst case one timer interval).
    from openstudio_operator.metrics import stamp_config_posture_gauges

    stamp_config_posture_gauges(
        namespace=namespace,
        name=name,
        dry_run=config.dry_run,
        server_url_set=bool(config.server_url),
        redis_url_set=bool(config.redis_url or config.redis_credentials.secret_ref),
        auto_soft_stop=config.analysis_policy.auto_soft_stop,
    )
    try:
        if not config.server_url:
            logger.warning("spec.serverUrl is empty — %s idle this tick", idle_label)
            return None
        now = datetime.now(UTC)
        try:
            # Issue #493 — client construction (the kube-API factory
            # call, StatusStore/EventEmitter, and the handler's wire
            # closure) runs INSIDE the guarded region: a kubeconfig-load
            # or client-construction failure raises an in-tuple class
            # (ConfigException / LocationValueError) and gets the same
            # counted skip-tick treatment as a tick failure.
            store = StatusStore(namespace, name, custom_objects_api())
            emit = EventEmitter(body=body, dry_run=config.dry_run)
            deps = wire(config)
            return tick(config=config, store=store, emit=emit, deps=deps, now=now)
        except SKIP_TICK_EXCEPTIONS as exc:
            from openstudio_operator.metrics import (
                HANDLER_CROSS_HANDLER_OUTAGE_TOTAL,
                HANDLER_TICK_FAILURES_TOTAL,
            )

            HANDLER_TICK_FAILURES_TOTAL.labels(
                namespace=namespace,
                name=name,
                module=module,
                error_type=type(exc).__name__,
            ).inc()
            # Issue #784 — cross-handler composite outage detection.
            now_ts = time.time()
            _cross_handler_failures[module] = now_ts
            cutoff = now_ts - _OUTAGE_WINDOW_SECONDS
            recent_modules = [m for m, ts in _cross_handler_failures.items() if ts > cutoff]
            if len(recent_modules) >= 2:
                module_set = "-".join(sorted(recent_modules))
                HANDLER_CROSS_HANDLER_OUTAGE_TOTAL.labels(module_set=module_set).inc()
            # Prune stale entries (written to a temp var first to avoid
            # F823 "referenced before assignment" from the self-ref
            # comprehension on _cross_handler_failures).
            _pruned: dict[str, float] = {m: ts for m, ts in _cross_handler_failures.items() if ts > cutoff}
            _cross_handler_failures.clear()
            _cross_handler_failures.update(_pruned)
            logger.warning(
                "%s tick skipped, retrying next poll (%s: %s)",
                tick_label,
                type(exc).__name__,
                exc,
            )
            return None
    finally:
        # Issue #469 — scheduler heartbeat: stamped at the END of every
        # invocation regardless of terminal path (the scheduler invoked
        # the timer = alive, even if the tick itself failed or skipped).
        # dry_run_audit is NOT stamped here: @kopf.on.event, event-driven,
        # no cadence — consciously excluded (see docstring).
        from openstudio_operator.metrics import HANDLER_LAST_TICK_TIMESTAMP

        HANDLER_LAST_TICK_TIMESTAMP.labels(module=module).set(time.time())
