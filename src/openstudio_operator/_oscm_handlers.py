"""Python-level OSCM handler registry (issue #250).

The kopf registry is the operational register for the operator's
``@kopf.timer`` / ``@kopf.daemon`` handlers. The Python-level registry
here provides a declarative seam that new handler modules call at module
import time via :func:`register`, and the singleton guard (D05,
:mod:`openstudio_operator.singleton`) cross-checks the two registries at
gate time. The AST test in
``tests/test_singleton_registry_coverage.py::test_python_registry_includes_all_oscm_spawning_handlers``
pins the agreement invariant so the failure mode that was the entire
motivation of the existing coverage test — "a new OSCM timer registered
without going through install_singleton_guard" — is now caught by the
test too.

The four OSCM handlers that PRE-DATE this registry (``analysis_sla_monitor``,
``zombie_datapoint_watchdog``, ``web_background_monitor``, ``worker_recycler``)
registered with kopf directly via their ``@kopf.timer`` decorators; per
the issue scope guard the existing registration paths are NOT modified.
Their IDs are listed in :data:`KNOWN_LEGACY_OSCM_HANDLER_IDS` so the
cross-check passes for them without retrofitting a ``register()`` call
into the handler modules. New handler modules MUST call
:func:`register` at module import time (after the ``@kopf.timer``
decorator).

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
#: (the kopf ``id`` attribute on the timer registry entry). New handler
#: modules call :func:`register` at module import time; the singleton guard
#: cross-checks this dict against the kopf registry at gate time.
REGISTRY: dict[str, Callable] = {}

#: Issue #250 — the four OSCM handlers that pre-dated the Python-level
#: registry. Their IDs are whitelisted here so the cross-check passes
#: without retrofitting a ``register()`` call into the handler modules
#: (scope guard: "do NOT modify the four existing handlers' registration
#: paths — only add the new registry decorator"). New handler modules
#: MUST NOT add to this set; they call :func:`register` instead.
KNOWN_LEGACY_OSCM_HANDLER_IDS: frozenset[str] = frozenset(
    {
        "analysis_sla_monitor",
        "zombie_datapoint_watchdog",
        "web_background_monitor",
        "worker_recycler",
    }
)


def register(handler_id: str, fn: Callable) -> Callable:
    """Register an OSCM ``@kopf.timer`` / ``@kopf.daemon`` handler.

    New handler modules call this at module import time — after the
    ``@kopf.timer`` decorator is applied and after the function is
    defined — so the Python-level registry knows about the handler before
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

    Encompasses BOTH the explicit :func:`register` entries AND the
    legacy IDs in :data:`KNOWN_LEGACY_OSCM_HANDLER_IDS`. The singleton
    guard's cross-check uses this function so the legacy-vs-new
    distinction is invisible to the gate.
    """
    return handler_id in REGISTRY or handler_id in KNOWN_LEGACY_OSCM_HANDLER_IDS


def reset_registry() -> None:
    """Drop every explicit :func:`register` entry (test seam).

    The production registry accumulates for the process's lifetime; the
    Pytest session re-imports modules across cases, so the same handler
    module ends up registering twice across the suite. The
    :func:`openstudio_operator.singleton.install_singleton_guard`
    cross-check uses ``REGISTRY`` membership (not the value), so a
    duplicate entry is benign — but tests that assert the explicit-set
    shape (e.g. "no new handler registered") need a clean slate. The
    legacy-id set is module-level and immutable; this seam clears only
    the dynamic entries.
    """
    REGISTRY.clear()
