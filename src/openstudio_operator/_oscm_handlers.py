"""Python-level OSCM handler registry (issue #250).

The kopf registry is the operational register for the operator's
``@kopf.timer`` / ``@kopf.daemon`` handlers. The Python-level registry
here provides a declarative seam that EVERY OSCM spawning handler
module calls at module import time via :func:`register`, and the
singleton guard (D05, :mod:`openstudio_operator.singleton`)
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

from collections.abc import Callable

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

        @kopf.timer(GROUP, VERSION, PLURAL, interval=POLL_INTERVAL_SECONDS)
        @register("my_new_handler", lambda: None)  # bracket-shaped awkward
        def my_new_handler(...): ...

    The canonical pattern is a bare call at module bottom:

    .. code-block:: python

        from openstudio_operator._oscm_handlers import register

        @kopf.timer(GROUP, VERSION, PLURAL, interval=POLL_INTERVAL_SECONDS)
        def my_new_handler(...): ...

        register("my_new_handler", my_new_handler)

    Handler id MUST match the kopf ``id`` attribute (which is the
    function ``__name__`` by default). Mismatched ids are stored
    verbatim — the cross-check would then fail with the offending id —
    so do not invent a new id here without naming the function to match.
    """
    REGISTRY[handler_id] = fn
    return fn


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
