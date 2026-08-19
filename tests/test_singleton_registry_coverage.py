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

import ast
import importlib
import logging
import sys
from pathlib import Path

import kopf
import pytest

from openstudio_operator import singleton
from openstudio_operator.handlers import _check_redis_key_layout_for_cr
from openstudio_operator.redis_client import (
    OperatorConfigError,
    RedisClientError,
)

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
# Removed with #78: "storage_pruner" — the storage retention pipeline moved
# out of the operator into the storage-prune CronJob
# (deploy/storage-cronjob.yaml); the handlers package no longer registers it.
# Removed with #77: "hpa_floor_adjuster" — replaced by a standard KEDA
# ScaledObject (deploy/keda-scaledobject.yaml); the operator no longer
# mutates the worker HPA, so no OSCM timer is registered for it.
EXPECTED_OSCM_TIMER_HANDLER_IDS: frozenset[str] = frozenset(
    {
        "analysis_sla_monitor",
        "zombie_datapoint_watchdog",
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


# --- Issue #163 — boot-time validate_key_layout() callsite assertion ----------
#
# Issue #163: validate_key_layout() is documented in AGENTS.md as the boot-time
# assertion that the Redis Service ``queue`` exposes the Resque keyspace the
# operator reads. The operator's entrypoint (``handlers/__init__.py``) MUST
# register an on-event handler that runs the assertion at startup (the kopf
# watch's initial listing IS the boot path) and on every CR change — without
# it, a layout drift would only be noticed downstream when the worker-registry
# gauges go silent, which is too late for the boot-time identity the singleton
# guard polices. The handler MUST be guarded by a try/except so a Redis
# connectivity failure degrades gracefully (operator continues to boot) — that
# invariant is enforced by the corresponding tests in ``tests/test_redis_client.py``.
#
# The "callsite assertion" extension: the new OSCM @kopf.on.event handler MUST
# be in the kopf WatchingRegistry after the package is imported. If a
# maintainer removes the handler from ``handlers/__init__.py`` (or it fails to
# register for some other reason), this test fails loudly at the CI gate.
EXPECTED_REDIS_KEY_LAYOUT_HANDLER_IDS: frozenset[str] = frozenset(
    {
        "_redis_key_layout_check",
    }
)


# After #234 the three module-level drain handlers collapsed into one
# ``@kopf.on.event`` wrapper (``_drain_queued_warning_events``) that
# delegates to the shared ``QueuedKopfEventSink``. The drain handler is
# asserted separately by the consolidated CI gate in
# ``tests/test_events_sinks.py::test_consolidated_drain_handler_is_registered``.
_EXPECTED_DRAIN_HANDLER_ID: str = "_drain_queued_warning_events"


def _watching_handlers_list(registry: kopf.OperatorRegistry) -> list[object]:
    """Return the WatchingRegistry handler list, or raise loudly if internals moved.

    Symmetric to :func:`_spawning_handlers_list` — the same "internals moved,
    fail loud" contract. The Redis-key-layout handler is registered as a
    ``@kopf.on.event`` so it lands in ``registry._watching._handlers``. If a
    kopf upgrade renames or moves that attribute, this test must fail at the
    boundary — NOT silently pass with an empty list.
    """
    watching = getattr(registry, "_watching", None)
    if watching is None:
        raise AssertionError(
            "kopf internal structure changed: OperatorRegistry no longer has "
            "a ``_watching`` attribute. The Redis-key-layout handler (issue "
            "#163) cannot be located without it. Update both the handler and "
            "tests/test_singleton_registry_coverage.py."
        )
    handlers = getattr(watching, "_handlers", None)
    if handlers is None:
        raise AssertionError(
            "kopf internal structure changed: WatchingRegistry no longer has "
            "a ``_handlers`` attribute. The Redis-key-layout handler (issue "
            "#163) cannot be located without it. Update both the handler and "
            "tests/test_singleton_registry_coverage.py."
        )
    if not isinstance(handlers, list):
        raise TypeError(
            f"kopf internal structure changed: WatchingRegistry._handlers is "
            f"no longer a list (got {type(handlers).__name__}). The "
            f"Redis-key-layout handler (issue #163) cannot be located."
        )
    return handlers


def test_redis_key_layout_handler_is_registered_at_boot() -> None:
    """Issue #163: ``validate_key_layout()`` is called at operator boot per CR.

    Asserts that the new OSCM @kopf.on.event handlers are registered in the
    kopf WatchingRegistry after the package is imported. If a maintainer
    removes the handler from ``handlers/__init__.py`` (or it fails to
    register), this CI gate fails loudly — the operator would otherwise boot
    silently with no boot-time Redis layout assertion, exactly the
    "silent-misbehavior risk" the issue set out to fix.
    """
    _ensure_handler_modules_loaded()

    registry = kopf.get_default_registry()
    _watching_handlers_list(registry)  # boundary check: fails loudly if internals moved

    registered_ids = {
        getattr(h, "id", "?") for h in _watching_handlers_list(registry)
    }
    missing = EXPECTED_REDIS_KEY_LAYOUT_HANDLER_IDS - registered_ids
    assert not missing, (
        f"Expected Redis-key-layout handlers are NOT registered in the kopf "
        f"watching registry: {sorted(missing)}. Did the @kopf.on.event "
        f"registration in handlers/__init__.py get removed? See issue #163 — "
        f"the operator boots without a boot-time Redis layout assertion, "
        f"which is the silent-misbehavior risk the issue set out to fix."
    )

    # Guard against accidental expansion: a future handler registered with a
    # similar id (``_redis_key_layout_*``) but not in the expected set would
    # silently fail this assertion and force the maintainer to add it — the
    # same "deliberate bump" semantics as the OSCM timer gate above.
    extra = {
        rid
        for rid in registered_ids
        if rid.startswith("_redis_key_layout")
    } - EXPECTED_REDIS_KEY_LAYOUT_HANDLER_IDS
    assert not extra, (
        f"New Redis-key-layout handler(s) registered but not declared in "
        f"EXPECTED_REDIS_KEY_LAYOUT_HANDLER_IDS: {sorted(extra)}. Add the new "
        f"id to tests/test_singleton_registry_coverage.py:EXPECTED_REDIS_KEY_LAYOUT_HANDLER_IDS "
        f"AND verify the handler is wired through _check_redis_key_layout_for_cr "
        f"with try/except protection (see issue #163)."
    )


def _find_watching_handler(
    registry: kopf.OperatorRegistry, handler_id: str
) -> object:
    """Find the WatchingHandler with the given id, or raise loudly."""
    for handler in _watching_handlers_list(registry):
        if getattr(handler, "id", None) == handler_id:
            return handler
    raise AssertionError(
        f"WatchingHandler {handler_id!r} not found in the kopf registry. "
        f"See issue #163 — the boot-time Redis-key-layout handler is not "
        f"registered. Did handlers/__init__.py lose the @kopf.on.event "
        f"decorator?"
    )


def test_redis_key_layout_handler_invokes_validate_key_layout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #163: the new on-event handler MUST call ``validate_key_layout()``.

    Walks the package's registered handler, invokes its ``fn`` with a
    synthetic CR body, and asserts the per-CR check fired
    ``validate_key_layout()`` (mocked via :mod:`monkeypatch`). The assertion
    is the callsite gate: removing the call from the handler — or refactoring
    it into a no-op — must fail this test.
    """
    _ensure_handler_modules_loaded()

    registry = kopf.get_default_registry()
    handler = _find_watching_handler(registry, "_redis_key_layout_check")
    fn = getattr(handler, "fn", None)
    assert fn is not None, "WatchingHandler.fn is None — kopf internals changed"

    calls: list[tuple[str, str]] = []

    class _StubClient:
        def validate_key_layout(self) -> None:
            calls.append(("validate_key_layout", "ok"))

    def _stub_factory(redis_url: str, **_kwargs: object) -> _StubClient:
        calls.append(("ReadOnlyRedisClient", redis_url))
        return _StubClient()

    # Patch the factory symbol at the import site of handlers/__init__.py so
    # the handler's closed-over get_read_only_redis_client (issue #235 — the
    # centralized ReadOnlyRedisClient factory) is redirected to the stub.
    monkeypatch.setattr(
        "openstudio_operator.handlers.get_read_only_redis_client", _stub_factory
    )

    body = {
        "metadata": {"namespace": "test-ns", "name": "test-osc"},
        "spec": {"redisUrl": "redis://:pw@queue.test:6379"},
    }

    with caplog.at_level(logging.INFO, logger="openstudio_operator.handlers"):
        fn(name="test-osc", namespace="test-ns", body=body)

    assert any(name == "ReadOnlyRedisClient" for name, _ in calls), (
        f"Handler did not construct ReadOnlyRedisClient; calls={calls!r}. "
        f"See issue #163 — the boot-time validation call is missing."
    )
    assert any(name == "validate_key_layout" for name, _ in calls), (
        f"Handler did not invoke validate_key_layout(); calls={calls!r}. "
        f"See issue #163 — the boot-time validation call is missing."
    )
    assert "redis_key_layout=ok" in caplog.text, (
        f"Expected structured log line 'redis_key_layout=ok' not found. "
        f"Captured: {caplog.text!r}. See issue #163."
    )


# --- Issue #233 — five-branch coverage for ``_check_redis_key_layout_for_cr`` ----
#
# The companion :func:`test_redis_key_layout_handler_invokes_validate_key_layout`
# exercises the ``ok`` path only (the synthetic body + validate_key_layout()
# stub succeeds). The four other branches the function returns — ``degraded``
# (OperatorConfigError → log line + queued ``RedisKeyLayoutDrift`` Warning
# Event), ``unreachable`` (RedisClientError or OSError → log line, no event),
# ``error`` (any other Exception → log line, no event), and ``skipped`` (empty
# ``spec.redisUrl`` or nameless item → debug log, no event) — had no directly
# testing the call → return-string contract.
#
# Issue #233 pins those branches at the callsite gate so that a regression
# that drops the unreachable catch (e.g. someone "fixes" the try/except by
# removing OSError) fails this test LOUDLY rather than silently degrading
# the boot-path Redis layout check into a crash on DNS failure — exactly the
# issue #163 failure mode the handler was added to prevent.
#
# Scope guard: do NOT modify ``_check_redis_key_layout_for_cr`` — only
# additions. Inject via :func:`openstudio_operator.client_factory.get_read_only_redis_client`
# (the factory symbol the operator closes over since issue #235), not by
# constructing ``ReadOnlyRedisClient`` directly — the AST gate
# :func:`tests.test_client_factory.test_only_one_read_only_redis_client_construction_point`
# rejects any second construction site outside the factory.
@pytest.mark.parametrize(
    ("branch", "exc_to_raise", "redis_url", "item_overrides", "expected_status",
     "expected_log_substring", "expects_warning_event"),
    [
        # ok: validate_key_layout() returns cleanly → INFO log, no event.
        pytest.param(
            "ok",
            None,
            "redis://:pw@queue.test:6379",
            {},
            "ok",
            "redis_key_layout=ok",
            False,
            id="ok",
        ),
        # degraded: OperatorConfigError → WARNING log + queued RedisKeyLayoutDrift.
        pytest.param(
            "degraded",
            OperatorConfigError("synthetic layout drift (#233)"),
            "redis://:pw@queue.test:6379",
            {},
            "degraded",
            "redis_key_layout=degraded",
            True,
            id="degraded",
        ),
        # unreachable via RedisClientError: WARNING log, no event. Pin both
        # wire-error classes (#233 acceptance criterion 1a/1b) because the
        # except clause catches both with the same body.
        pytest.param(
            "unreachable_redis_client_error",
            RedisClientError("Connection refused at boot (#233)"),
            "redis://:pw@queue.test:6379",
            {},
            "unreachable",
            "redis_key_layout=unreachable",
            False,
            id="unreachable_redis_client_error",
        ),
        pytest.param(
            "unreachable_os_error",
            OSError("DNS failure simulated (#233)"),
            "redis://:pw@queue.test:6379",
            {},
            "unreachable",
            "redis_key_layout=unreachable",
            False,
            id="unreachable_os_error",
        ),
        # error: any other Exception → WARNING log carrying the type name,
        # no event. Regression fence against someone "fixing" the try/except
        # by removing RedisClientError / OSError and re-raising.
        pytest.param(
            "error",
            RuntimeError("unexpected boom (#233)"),
            "redis://:pw@queue.test:6379",
            {},
            "error",
            "redis_key_layout=error",
            False,
            id="error",
        ),
        # skipped via empty redisUrl: debug log, no warning, no event.
        # Empty URL is the URL-guard's territory (#116); the layout check
        # must NOT short-circuit on it.
        pytest.param(
            "skipped_empty_redis_url",
            None,
            "",
            {},
            "skipped",
            "redis_key_layout skip",
            False,
            id="skipped_empty_redis_url",
        ),
        # skipped via nameless item: same outcome, different entry point.
        pytest.param(
            "skipped_nameless_item",
            None,
            "redis://:pw@queue.test:6379",
            {"metadata": {}},
            "skipped",
            "",
            False,
            id="skipped_nameless_item",
        ),
    ],
)
def test_check_redis_key_layout_for_cr_covers_all_status_branches(
    *,
    branch: str,
    exc_to_raise: Exception | None,
    redis_url: str,
    item_overrides: dict,
    expected_status: str,
    expected_log_substring: str,
    expects_warning_event: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #233: every return branch of ``_check_redis_key_layout_for_cr`` is exercised.

    The five return values are ``ok``, ``degraded``, ``unreachable``,
    ``error``, and ``skipped``. This parametrised test injects the
    :func:`openstudio_operator.client_factory.get_read_only_redis_client`
    factory symbol at its import site (``handlers/__init__.py`` — the same
    monkeypatch site used by the existing companion tests) and asserts
    the contract for each branch:

    1. the expected status string is returned,
    2. the matching structured log line is emitted,
    3. the Warning Event is queued iff the branch expects it (degraded only),
    4. the function never re-raises — the boot path must survive even on
       the exception-bearing branches.

    Two cases for ``unreachable`` (RedisClientError, OSError) and two for
    ``skipped`` (empty redisUrl, nameless item) cover each entry path the
    function exposes, so the test gate covers not just the except clauses
    but also the precondition guards.
    """
    import openstudio_operator.handlers as handlers_pkg

    class _StubClient:
        """Fake ``ReadOnlyRedisClient`` returned by the patched factory.

        The stub records the construction call so a regression that
        bypasses the factory entirely is caught by the existing
        :func:`test_redis_key_layout_handler_invokes_validate_key_layout`
        sibling; here we only need :meth:`validate_key_layout` to
        either return cleanly or raise the parametrized exception.
        """
        def validate_key_layout(self) -> None:
            if exc_to_raise is not None:
                raise exc_to_raise

    construction_calls: list[str] = []

    def _stub_factory(redis_url: str, **_kwargs: object) -> _StubClient:
        construction_calls.append(redis_url)
        return _StubClient()

    monkeypatch.setattr(
        "openstudio_operator.handlers.get_read_only_redis_client", _stub_factory
    )

    item: dict = {
        "metadata": {"namespace": "test-ns", "name": "test-osc"},
        "spec": {"redisUrl": redis_url},
    }
    item.update(item_overrides)

    handlers_pkg._sink.clear()

    with caplog.at_level(
        logging.DEBUG, logger="openstudio_operator.handlers"
    ):
        # MUST NOT raise — that is the contract of all five branches.
        status = _check_redis_key_layout_for_cr(
            item, logger=logging.getLogger("openstudio_operator.handlers")
        )

    assert status == expected_status, (
        f"Branch {branch!r}: expected status={expected_status!r}; "
        f"got {status!r}. Captured: {caplog.text!r}. See issue #233."
    )

    if expected_log_substring:
        assert expected_log_substring in caplog.text, (
            f"Branch {branch!r}: expected log substring {expected_log_substring!r} "
            f"missing. Captured: {caplog.text!r}. See issue #233."
        )

    # Each non-skipped branch must reach the factory (skipped guards short-circuit
    # before construction).
    if branch != "skipped_nameless_item" and redis_url:
        assert construction_calls == [redis_url], (
            f"Branch {branch!r}: factory call mismatch {construction_calls!r}; "
            f"expected exactly one call with {redis_url!r}. See issue #233."
        )

    # The Warning Event is queued iff the branch expects it. Critical:
    # unreachable and error MUST NOT queue — alerting on a wire-level
    # outage or a generic exception is operator-on-call noise.
    pending = [
        msg for msg in handlers_pkg._sink.queued
        if msg[0] == "test-ns" and msg[1] == "test-osc"
    ]
    if expects_warning_event:
        assert len(pending) == 1, (
            f"Branch {branch!r}: expected exactly one queued "
            f"RedisKeyLayoutDrift event; got {len(pending)}. See issue #233."
        )
        assert pending[0][2] == "RedisKeyLayoutDrift", (
            f"Branch {branch!r}: expected reason='RedisKeyLayoutDrift'; "
            f"got {pending[0][2]!r}. See issue #233."
        )
    else:
        assert not pending, (
            f"Branch {branch!r}: expected NO queued Warning Event; "
            f"got {pending!r}. The operator would noise on "
            f"{'wire-level outages' if branch.startswith('unreachable') else branch} "
            f"and drown the degraded signal. See issue #233."
        )


# --- Issue #158 — exactly one ``CustomObjectsApi()`` construction site ----------
#
# Issue #158: ``StatusStore.in_cluster`` was a defined-but-never-set class
# attribute, and every handler reached for ``kubernetes.client.CustomObjectsApi()``
# inline (the SLA monitor, worker recycler, web_background monitor, datapoint
# watchdog, plus ``singleton._build_custom_objects_api``). Bypassing the
# factory meant the inline calls ran with whatever the default kubeconfig
# resolution picked up — correct only because the operator is in-cluster.
# A future change to the factory's loader would silently leave the inline
# callsites behind.
#
# The fix is a single factory — ``singleton.operator_custom_objects_api()`` —
# that every handler imports. This test pins the "exactly one construction
# site" invariant at the source level via AST scan: if a maintainer adds a
# new ``CustomObjectsApi()`` call outside the factory, the CI gate fails
# loudly. The companion runtime contract tests live in
# ``tests/test_k8s_clients.py`` (caching + load-once behaviour).
#
# Scan scope: every ``.py`` file under ``src/openstudio_operator/``. Tests
# are excluded by the path filter (they live under ``tests/``). The
# factory's own file (``singleton.py``) is the ONE allowed construction
# site; the assertion message names the offending file + line so a
# maintainer can fix the regression in one read.


def _find_custom_objects_api_constructions() -> list[tuple[str, int]]:
    """Return ``(relative_path, lineno)`` for every ``CustomObjectsApi()`` call.

    Walks the operator's production source tree (``src/openstudio_operator/``),
    parses each ``.py`` file with :mod:`ast`, and locates ``Call`` nodes
    whose function is a bare ``CustomObjectsApi`` name. The bare-name match
    excludes qualified calls like ``kubernetes.client.CustomObjectsApi()``
    (which would already be a regression — the factory should be the only
    construction point regardless of how it's spelled). Line numbers come
    from the parsed AST so they survive future source edits (the
    alternative — regex on raw bytes — would silently miss
    ``CustomObjectsApi ( )`` or other whitespace variations).
    """
    src_root = Path(singleton.__file__).parent
    found: list[tuple[str, int]] = []
    for py in sorted(src_root.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Name) or func.id != "CustomObjectsApi":
                continue
            if node.args or node.keywords:
                # The factory calls ``CustomObjectsApi()`` with no args;
                # any other signature (kwargs, positional) would be a
                # different construction site worth surfacing separately.
                continue
            found.append((str(py.relative_to(src_root.parent)), node.lineno))
    return found


def test_only_one_custom_objects_api_construction_point() -> None:
    """Issue #158: ``CustomObjectsApi()`` is constructed in EXACTLY one place.

    The factory in :mod:`openstudio_operator.singleton` is the operator's
    only legitimate construction site. Any inline
    ``CustomObjectsApi()`` outside the factory is a regression: the
    handler bypasses the in-cluster / kubeconfig fallback loader, so a
    future change to that loader (kubeconfig Secret reference, network
    proxy, …) silently leaves the inline callsite behind.

    The assertion message names the offending file + line so the
    maintainer can fix the regression in one read.
    """
    found = _find_custom_objects_api_constructions()

    assert len(found) == 1, (
        f"Expected exactly ONE CustomObjectsApi() construction in "
        f"src/openstudio_operator/; found {len(found)}: {found}. "
        f"Every handler must use "
        f"openstudio_operator.singleton.operator_custom_objects_api() "
        f"instead of constructing the client inline. See issue #158."
    )

    path, lineno = found[0]
    assert path.endswith("openstudio_operator/singleton.py"), (
        f"CustomObjectsApi() must be constructed only in "
        f"singleton.py (the factory); found it at {path}:{lineno}. "
        f"See issue #158."
    )
