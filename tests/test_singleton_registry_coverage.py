"""CI gate: every OSCM spawning handler registered by the operator MUST be wrapped
by the singleton guard (D05 — issue #14).

The singleton guard reaches into kopf's private registry
(``registry._spawning._handlers``, kopf 1.37+ — see :mod:`openstudio_operator.singleton`)
to wrap every OSCM ``@kopf.timer`` registered for the
``openstudioclustermanagers.energy.nrel.gov`` resource. That internal is
**not part of kopf's public API**: a kopf upgrade can move or rename it, and the
likely failure mode is NOT a crash but **silently unwrapped handlers** —
``install_singleton_guard`` warns and returns ``0``, the operator boots, and
the D05 enforcement quietly disappears.

This test makes that failure mode loud. It runs in CI (the project's lint+test
gate, ``.github/workflows/ci.yml``) and fails clearly when:

* the kopf internals change shape (no ``_spawning`` on the registry, no
  ``_handlers`` on the spawning registry, or ``_handlers`` is not a list),
* or any OSCM spawning handler reaches the registry unwrapped (a new handler
  module that forgot the guard, or the guard itself regressed).

Bound in ``pyproject.toml`` via ``kopf>=1.37,<1.45`` so a kopf upgrade forces a
deliberate bump here AND in this test (see issue #47).
"""

from __future__ import annotations

import importlib
import logging
import sys

import kopf
import pytest

from openstudio_operator import singleton

# Every handler module the operator ships under ``openstudio_operator.handlers/``
# registers exactly one ``@kopf.timer`` for the OSCM resource
# (``_SPEC["group"]/_SPEC["version"]/_SPEC["plural"]`` in each module). Adding a
# new module under that directory MUST either:
#
# (a) appear in this set, in which case this test enforces its timer is wrapped,
# (b) NOT register any OSCM spawning handler (e.g. pure on-event handlers), in
#     which case no entry is needed here.
#
# Update this set when a new OSCM spawning handler is added. Forgetting to do
# so will fail this test (and that is the point — see issue #47).
EXPECTED_OSCM_TIMER_HANDLER_IDS: frozenset[str] = frozenset(
    {
        "analysis_sla_monitor",
        "zombie_datapoint_watchdog",
        "hpa_floor_adjuster",
        "storage_pruner",
        "web_background_monitor",
        "worker_recycler",
    }
)


def _ensure_metrics_stubbed() -> None:
    """Stub ``start_metrics_server`` BEFORE the handlers package is imported.

    ``openstudio_operator.handlers.__init__`` calls ``start_metrics_server()``
    at import time. Binding port 9090 in a test process is undesirable (and
    noisy in CI when several test sessions overlap). Stub it once with a
    no-op so subsequent imports are free to call it.

    Safe to call repeatedly: only the first stub wins, and the no-op is
    indistinguishable from a successful bind for the purposes of this test.
    """
    import openstudio_operator.metrics as metrics_mod

    if getattr(metrics_mod.start_metrics_server, "__wrapped_for_test__", False):
        return

    def _stub(*args: object, **kwargs: object) -> int:
        return 9090

    _stub.__wrapped_for_test__ = True  # type: ignore[attr-defined]
    metrics_mod.start_metrics_server = _stub  # type: ignore[assignment]


def _ensure_handler_modules_loaded() -> None:
    """Import every production handler module so its ``@kopf.timer`` registers.

    Goes via the package import path so a single ``import openstudio_operator.handlers``
    brings in all six timers AND runs the operator's normal
    ``install_singleton_guard()`` call from ``__init__.py``. That call is
    idempotent — re-running it here (or having this test run after other tests
    that imported the package) does not double-wrap.
    """
    _ensure_metrics_stubbed()
    if "openstudio_operator.handlers" not in sys.modules:
        importlib.import_module("openstudio_operator.handlers")


def _spawning_handlers_list(registry: kopf.OperatorRegistry) -> list[object]:
    """Return the spawning-registry handler list, or raise loudly if internals moved.

    The singleton guard's contract (see :func:`singleton.install_singleton_guard`)
    is that ``registry._spawning._handlers`` is a list. If a kopf upgrade renames
    or moves that attribute, this test must fail at the boundary — NOT silently
    pass with an empty list. The error message names the missing attribute so
    the maintainer can adapt the guard (and update this test) deliberately.
    """
    spawning = getattr(registry, "_spawning", None)
    if spawning is None:
        raise AssertionError(
            "kopf internal structure changed: OperatorRegistry no longer has "
            "a ``_spawning`` attribute. The singleton guard (issue #47) "
            "cannot wrap OSCM handlers without it. See "
            "openstudio_operator/singleton.py:install_singleton_guard and "
            "update both the guard and tests/test_singleton_registry_coverage.py."
        )
    handlers = getattr(spawning, "_handlers", None)
    if handlers is None:
        raise AssertionError(
            "kopf internal structure changed: SpawningRegistry no longer has "
            "a ``_handlers`` attribute. The singleton guard (issue #47) "
            "cannot wrap OSCM handlers without it. See "
            "openstudio_operator/singleton.py:install_singleton_guard and "
            "update both the guard and tests/test_singleton_registry_coverage.py."
        )
    if not isinstance(handlers, list):
        raise TypeError(
            f"kopf internal structure changed: SpawningRegistry._handlers is "
            f"no longer a list (got {type(handlers).__name__}). The singleton "
            f"guard (issue #47) cannot wrap OSCM handlers. See "
            f"openstudio_operator/singleton.py:install_singleton_guard."
        )
    return handlers


def _oscm_spawning_handlers(registry: kopf.OperatorRegistry) -> list[object]:
    return [h for h in _spawning_handlers_list(registry) if singleton._selector_matches_oscms(h)]


# --- the loud failure-mode gate -----------------------------------------------


def test_all_oscm_spawning_handlers_are_singleton_guarded() -> None:
    """Every OSCM ``@kopf.timer`` registered by the operator must be wrapped.

    Fails when:

    * the kopf internals changed shape (see :func:`_spawning_handlers_list`),
    * a new OSCM spawning handler is registered but ``install_singleton_guard``
      did not wrap it (e.g. a new handler module that bypassed the import block
      in ``handlers/__init__.py``, or the guard's wrap logic regressed),
    * an expected OSCM timer is missing entirely (the import block in
      ``handlers/__init__.py`` is out of sync with the handler modules).
    """
    _ensure_handler_modules_loaded()

    registry = kopf.get_default_registry()
    _spawning_handlers_list(registry)  # boundary check: fails loudly if internals moved
    oscm = _oscm_spawning_handlers(registry)

    # Re-run the guard: the package import already ran it, but it is
    # idempotent and this is what the production wiring does — belt + braces.
    wrapped_count = singleton.install_singleton_guard(registry=registry)
    assert wrapped_count >= 0, (
        f"install_singleton_guard returned {wrapped_count} (expected >= 0); "
        f"kopf internals may have changed (see #47)."
    )

    # Re-snapshot after the re-run; wrapping replaces the handler in place.
    oscm = _oscm_spawning_handlers(registry)

    registered_ids = {getattr(h, "id", "?") for h in oscm}
    missing = EXPECTED_OSCM_TIMER_HANDLER_IDS - registered_ids
    assert not missing, (
        f"Expected OSCM timer handlers are NOT registered in the kopf "
        f"spawning registry: {sorted(missing)}. Did a handler module get "
        f"removed from ``openstudio_operator/handlers/__init__.py``'s import "
        f"block? See issue #47."
    )

    # Guard against accidental expansion: a future handler that registers an
    # OSCM timer but is not in EXPECTED_OSCM_TIMER_HANDLER_IDS would silently
    # fail this assertion and force the maintainer to add it — the same
    # "deliberate bump" semantics as the pyproject.toml upper bound.
    extra = registered_ids - EXPECTED_OSCM_TIMER_HANDLER_IDS
    assert not extra, (
        f"New OSCM spawning handler(s) registered but not declared in "
        f"EXPECTED_OSCM_TIMER_HANDLER_IDS: {sorted(extra)}. Add the new id to "
        f"tests/test_singleton_registry_coverage.py:EXPECTED_OSCM_TIMER_HANDLER_IDS "
        f"AND verify ``handlers/__init__.py`` wraps it (see issue #47)."
    )

    unwrapped = [h for h in oscm if not getattr(h.fn, singleton.GUARD_MARKER, False)]
    assert not unwrapped, (
        f"{len(unwrapped)} OSCM spawning handler(s) are NOT wrapped by the "
        f"singleton guard (D05): {[getattr(h, 'id', '?') for h in unwrapped]}. "
        f"install_singleton_guard() failed to gate them — the kopf internals "
        f"may have changed shape. See issue #47."
    )


def test_install_singleton_guard_emits_warning_on_missing_internals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When kopf internals move, the guard must warn loudly (not crash, not silent).

    The guard's contract — see :func:`singleton.install_singleton_guard` —
    is to log a warning naming the expected layout when the registry is not
    as expected, return ``0`` wrapped handlers, and let this test catch the
    silent-de-wrap. The guard itself MUST NOT raise: the operator must keep
    booting even when the kopf layout drifts (and the CI gate fails
    afterwards, loudly).
    """

    class _NoSpawning:
        """Registry-shaped object missing the ``_spawning`` attribute."""

    class _NoHandlers:
        class _spawning:
            """``_spawning`` exists but has no ``_handlers`` attribute."""

    class _WrongShape:
        class _spawning:
            _handlers = "not-a-list"  # type: ignore[assignment]

    with caplog.at_level(logging.WARNING, logger=singleton.logger.name):
        assert singleton.install_singleton_guard(registry=_NoSpawning()) == 0
        assert singleton.install_singleton_guard(registry=_NoHandlers()) == 0
        assert singleton.install_singleton_guard(registry=_WrongShape()) == 0

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3, (
        f"Expected 3 WARNING records (one per broken-registry shape); got "
        f"{len(warnings)}. The guard's loud-fail contract regressed — see "
        f"openstudio_operator/singleton.py:install_singleton_guard and issue #47."
    )
    for record in warnings:
        message = record.getMessage()
        assert "kopf registry internals not as expected" in message, (
            f"Guard warning message changed; the CI gate depends on this "
            f"text. Got: {message!r}. See issue #47."
        )
        assert "D05 enforcement disabled" in message, (
            f"Guard warning must name D05 so the operator on-call sees the "
            f"link to the policy decision. Got: {message!r}. See issue #47."
        )
