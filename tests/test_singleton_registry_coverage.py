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
from openstudio_operator.status_store import GROUP, PLURAL

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


# --- Issue #251 — exactly one construction site per ``*V1Api()`` factory -------
#
# Issue #251: the single-construction-point pattern audited in
# ``test_only_one_custom_objects_api_construction_point`` (issue #158) was
# applied only to ``CustomObjectsApi`` in
# ``singleton.operator_custom_objects_api()``. The companion K8s clients
# the operator and the prune CronJob use were constructed inline:
# ``prune_entrypoint.py`` called ``BatchV1Api()`` and ``CoreV1Api()``
# directly; ``analysis_sla.py`` called ``CoreV1Api()`` inline as the
# default for ``pod_api``; ``web_background_monitor.py`` called
# ``AppsV1Api()`` and ``CoreV1Api()`` inline; ``worker_recycler.py``
# called ``AppsV1Api()`` inline. The structural inconsistency (one K8s
# client centralised, three not) is the issue's motivation: a future
# change to the loader (kubeconfig Secret reference, network-proxy
# client, …) silently leaves the inline callsites behind.
#
# The fix: ``singleton.operator_apps_api()``, ``operator_batch_api()``,
# and ``operator_core_api()`` factories built from the same
# ``kubernetes.config.load_incluster_config / load_kube_config`` path.
# These tests pin the "exactly one construction site per client type"
# invariant at the source level via AST scan: if a maintainer adds a
# new ``*V1Api()`` call outside the factory, the CI gate fails loudly.
#
# Companion runtime contract tests live in
# ``tests/test_k8s_clients.py`` (caching + load-once behaviour).
#
# Scan scope: every ``.py`` file under ``src/openstudio_operator/``;
# the factory's own file (``singleton.py``) is the allowed construction
# site for each client type. Type annotations like ``batch_api: BatchV1Api``
# are not ``ast.Call`` nodes and are silently ignored by the walker —
# the test pins the construction-site invariant, not the type-import
# invariant. The latter is satisfied by the import block in
# ``singleton.py`` and the call sites (factories return instances of
# the imported classes, so the types don't need to be re-imported
# from ``kubernetes.client``).


#: The four K8s client types the operator uses, paired with the factory
#: that constructs them. Issue #251 picks the user's spelling as the
#: canonical name; we keep these together so the test enforces a
#: one-to-one mapping.
_V1_API_FACTORIES: dict[str, str] = {
    "CustomObjectsApi": "operator_custom_objects_api",
    "AppsV1Api": "operator_apps_api",
    "BatchV1Api": "operator_batch_api",
    "CoreV1Api": "operator_core_api",
}


def _find_v1_api_constructions(
    client_name: str,
) -> list[tuple[str, int]]:
    """Return ``(relative_path, lineno)`` for every ``{client_name}()`` call.

    Walks the operator's production source tree (``src/openstudio_operator/``),
    parses each ``.py`` file with :mod:`ast`, and locates ``Call`` nodes
    whose function is a bare ``client_name`` name (e.g. ``CoreV1Api``,
    ``BatchV1Api``, ``AppsV1Api``). The bare-name match excludes qualified
    calls like ``kubernetes.client.CoreV1Api()`` (which would already be a
    regression — the factory should be the only construction point
    regardless of how it's spelled).
    """
    src_root = Path(singleton.__file__).parent
    found: list[tuple[str, int]] = []
    for py in sorted(src_root.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Name) or func.id != client_name:
                continue
            if node.args or node.keywords:
                # The factory calls ``CoreV1Api()`` with no args; any
                # other signature (kwargs, positional) would be a
                # different construction site worth surfacing separately.
                continue
            found.append((str(py.relative_to(src_root.parent)), node.lineno))
    return found


def test_only_one_v1_api_construction_point_per_factory() -> None:
    """Issue #251: every K8s client type is constructed in exactly one place.

    ``singleton.py`` is the operator's only legitimate construction site
    for each ``*V1Api()`` type. Any inline ``*V1Api()`` outside the
    factory is a regression: the handler bypasses the in-cluster /
    kubeconfig fallback loader, so a future change to that loader
    silently leaves the inline callsite behind. The companion runtime
    contract tests live in ``tests/test_k8s_clients.py`` (caching +
    load-once behaviour).
    """
    src_root = Path(singleton.__file__).parent
    for client_name, factory_name in _V1_API_FACTORIES.items():
        found = _find_v1_api_constructions(client_name)
        assert len(found) == 1, (
            f"Expected exactly ONE {client_name}() construction in "
            f"src/openstudio_operator/; found {len(found)}: {found}. "
            f"Every handler must use "
            f"openstudio_operator.singleton.{factory_name}() "
            f"instead of constructing the client inline. See issue #251."
        )
        path, lineno = found[0]
        assert path.endswith("openstudio_operator/singleton.py"), (
            f"{client_name}() must be constructed only in singleton.py "
            f"(the factory); found it at {path}:{lineno}. See issue #251."
        )
    # Sanity: scan root must be the same one as the existing #158 test
    # uses — defensive in case the source-root heuristic ever changes.
    assert src_root.name == "openstudio_operator", (
        f"AST scan root drifted: expected 'openstudio_operator', got {src_root.name!r}. "
        f"Update both #158 and #251 tests in lockstep."
    )


# --- Issue #305 — single kubeconfig loader call site ----------------------------
#
# Issue #305: the kubeconfig loader — ``load_incluster_config`` /
# ``load_kube_config`` — was duplicated verbatim between
# ``singleton._load_k8s_config`` and ``prune_entrypoint._load_kube_config``.
# Both copies have been replaced with thin delegations to the SINGLE
# public loader :func:`openstudio_operator._k8s.load_operator_kube_config`.
# The companion v1-API construction gate (issue #251) enforces the
# construction pattern but did not cover the loader itself, so a future
# maintainer who re-introduces an inline loader call would have left the
# two files in disagreement silently. The AST test below pins the
# single-site invariant at the source level: every production-source
# ``load_incluster_config(`` or ``load_kube_config(`` call must live in
# ``_k8s.py``.

#: The two client-python loader symbols the operator touches. Bare-name
#: (``load_incluster_config(...)``) AND attribute
#: (``kube_config.load_incluster_config(...)``) call shapes are caught —
#: the existing singleton.py used the attribute shape, the existing
#: prune_entrypoint.py used the bare-name shape, and a future regression
#: that flips either spelling is rejected by the same gate.
_KUBECONFIG_LOADER_NAMES = frozenset({"load_incluster_config", "load_kube_config"})


def _find_kube_config_loader_calls() -> list[tuple[str, int, str]]:
    """Return ``(relative_path, lineno, name)`` for every loader call site.

    Walks ``src/openstudio_operator/`` and locates ``ast.Call`` nodes
    whose function is either a bare ``load_incluster_config`` /
    ``load_kube_config`` name OR an attribute ending in either name
    (e.g. ``kube_config.load_incluster_config()``). The
    attribute-shape match is necessary because the original
    ``singleton._load_k8s_config`` used the qualified form
    (``kube_config.load_incluster_config()`` via a local module-alias
    import); without it, a regression that re-introduces the
    attribute-shape call would slip past the gate.
    """
    src_root = Path(singleton.__file__).parent
    found: list[tuple[str, int, str]] = []
    for py in sorted(src_root.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in _KUBECONFIG_LOADER_NAMES:
                found.append((str(py.relative_to(src_root.parent)), node.lineno, func.id))
            elif isinstance(func, ast.Attribute) and func.attr in _KUBECONFIG_LOADER_NAMES:
                found.append((str(py.relative_to(src_root.parent)), node.lineno, func.attr))
    return found


def test_only_one_kubeconfig_loader_call_site() -> None:
    """Issue #305: ``load_incluster_config`` / ``load_kube_config`` live in exactly one place.

    The kubeconfig loader is the upstream gate every K8s API call
    shares — the operators's :func:`operator_custom_objects_api`,
    :func:`operator_apps_api`, :func:`operator_batch_api`, and
    :func:`operator_core_api` factories all depend on it, and so does
    :mod:`openstudio_operator.prune_entrypoint`'s pre-factory preload.
    Before #305, the inline ``try: load_incluster_config(); except
    ConfigException: load_kube_config()`` lived in two places
    (``singleton._load_k8s_config`` and
    ``prune_entrypoint._load_kube_config``); a future loader change
    (kubeconfig Secret reference, network-proxy client, custom CA
    bundle) would have to land in two files in lockstep, and the
    existing v1-API construction gate (issue #251) covers the
    construction step but not the loader step.

    The fix: the loader is now hosted at
    :func:`openstudio_operator._k8s.load_operator_kube_config` and
    both wrappers delegate to it. This test pins the single-site
    invariant at the source level: any production-source
    ``load_incluster_config(`` or ``load_kube_config(`` call outside
    ``_k8s.py`` is a regression — and the test fails the CI gate
    loudly so the regression cannot reach ``develop``.

    Note: the test mocks in ``tests/test_singleton_guard.py`` and
    ``tests/test_k8s_clients.py`` monkeypatch
    ``kubernetes.config.load_incluster_config`` /
    ``kubernetes.config.load_kube_config`` — those are TEST files
    (the AST scan is scoped to ``src/openstudio_operator/``) and the
    monkeypatched symbols are still resolved through the local
    import inside ``load_operator_kube_config``, so the existing
    load-order contract tests
    (``test_get_guard_loads_config_before_building_client``,
    ``test_factory_loads_incluster_config``) continue to pass without
    modification.
    """
    src_root = Path(singleton.__file__).parent
    found = _find_kube_config_loader_calls()
    assert found, (
        "No ``load_incluster_config(`` or ``load_kube_config(`` call "
        "site found in src/openstudio_operator/. The SINGLE public "
        "loader in openstudio_operator._k8s.load_operator_kube_config() "
        "must call into kubernetes.config. See issue #305."
    )
    offenders = [
        (path, lineno, name)
        for path, lineno, name in found
        if not path.endswith("openstudio_operator/_k8s.py")
    ]
    assert not offenders, (
        f"Production code calls ``{_KUBECONFIG_LOADER_NAMES}`` outside "
        f"openstudio_operator/_k8s.py: {offenders}. Every K8s client "
        f"the operator builds must go through the SINGLE public loader "
        f"openstudio_operator._k8s.load_operator_kube_config(); "
        f"inline ``load_incluster_config(`` / ``load_kube_config(`` "
        f"calls in any other module are a regression (issue #305). "
        f"The companion wrappers "
        f"openstudio_operator.singleton._load_k8s_config and "
        f"openstudio_operator.prune_entrypoint._load_kube_config "
        f"both delegate to the canonical loader."
    )
    # Sanity: the scan root must match the heuristic used by the #158
    # and #251 tests — defensive in case the source-root heuristic
    # ever drifts (the test would otherwise silently scan a wrong
    # subtree and pass).
    assert src_root.name == "openstudio_operator", (
        f"AST scan root drifted: expected 'openstudio_operator', got "
        f"{src_root.name!r}. Update the #158, #251, and #305 AST "
        f"tests in lockstep."
    )


# --- Issue #250 — Python-level registry cross-check ----------------------------
#
# Issue #250: ``EXPECTED_OSCM_TIMER_HANDLER_IDS`` (the frozenset above) was
# the ONLY enforcement of the "every new OSCM handler is gated" invariant
# in production code. A maintainer who added a new handler module but
# forgot to extend the test set would pass CI trivially. The fix:
# introduce a Python-level registry
# (:mod:`openstudio_operator._oscm_handlers`) that new handler modules
# call at module import time via ``register(handler_id, fn)``, and
# cross-check the kopf registry against the Python registry at gate
# time inside ``install_singleton_guard``. The four existing handlers
# (analysis_sla_monitor, zombie_datapoint_watchdog, web_background_monitor,
# worker_recycler) are exempt from the explicit ``register()`` call
# (issue scope guard: "do NOT modify the four existing handlers'
# registration paths"); their IDs are listed in
# ``_oscm_handlers.KNOWN_LEGACY_OSCM_HANDLER_IDS`` so the cross-check
# passes for them. A new OSCM timer that forgets to register fails this
# test loudly.


def test_python_registry_includes_all_oscm_spawning_handlers() -> None:
    """Issue #250: every OSCM timer must be in the Python-level registry.

    The Python-level registry
    (:mod:`openstudio_operator._oscm_handlers`) is the declarative
    seam: new handler modules call ``register(handler_id, fn)`` at
    module import time, and the singleton guard cross-checks the
    Python registry against the kopf registry at gate time. A new
    OSCM timer that forgot to register would silently slip past the
    gate (the gate wraps every OSCM timer in the kopf registry; the
    Python registry is what the test pins here). The four existing
    handlers are exempt from the explicit ``register()`` call via
    ``_oscm_handlers.KNOWN_LEGACY_OSCM_HANDLER_IDS`` so the scope
    guard ("do NOT modify the four existing handlers' registration
    paths") is honored.
    """
    from openstudio_operator import _oscm_handlers

    _ensure_handler_modules_loaded()

    registry = kopf.get_default_registry()
    _spawning_handlers_list(registry)  # boundary check: fails loudly if internals moved
    oscm = _oscm_spawning_handlers(registry)
    kopf_timer_ids = {getattr(h, "id", "?") for h in oscm}

    # The Python-level registry is the union of the explicit registry
    # entries (new handler modules) AND the legacy-id set (the four
    # pre-#250 handlers exempted from the explicit ``register()`` call).
    python_registry_ids = (
        set(_oscm_handlers.REGISTRY.keys())
        | set(_oscm_handlers.KNOWN_LEGACY_OSCM_HANDLER_IDS)
    )

    missing = kopf_timer_ids - python_registry_ids
    assert not missing, (
        f"OSCM timer(s) registered with kopf but NOT in the Python-level "
        f"registry (issue #250): {sorted(missing)}. New handler modules "
        f"MUST call "
        f"openstudio_operator._oscm_handlers.register(handler_id, fn) "
        f"at module import time. See "
        f"tests/test_singleton_registry_coverage.py and issue #250."
    )

    # Guard against accidental expansion: a future maintainer who adds
    # an explicit ``register()`` call for a legacy handler would
    # silently expand the Python registry. That is harmless per se
    # (the cross-check is membership-based, not value-based), but the
    # legacy-id set is the canonical "no extra wiring needed" seal — a
    # double-registration is a hint that the legacy handler is now
    # meeting the new-style contract and could be removed from the
    # legacy set in a follow-up. The test reports this so the
    # maintainer can clean up.
    explicit_legacy = (
        set(_oscm_handlers.REGISTRY.keys())
        & set(_oscm_handlers.KNOWN_LEGACY_OSCM_HANDLER_IDS)
    )
    assert not explicit_legacy, (
        f"OSCM timer(s) in the legacy-id set ALSO have an explicit "
        f"register() entry (issue #250): {sorted(explicit_legacy)}. "
        f"Either remove the redundant register() call from the handler "
        f"module, OR remove the id from "
        f"_oscm_handlers.KNOWN_LEGACY_OSCM_HANDLER_IDS — pick one."
    )


def test_install_singleton_guard_skips_unregistered_oscm_handler(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #250: a new OSCM timer that forgot to register is LOUDLY skipped.

    The gate's cross-check fires an ERROR log and skips the wrap when
    an OSCM timer is in the kopf registry but NOT in the Python-level
    registry. The wrapping is skipped so the test gate
    (``test_python_registry_includes_all_oscm_spawning_handlers``)
    catches the regression before the operator can boot with a
    silently un-gated handler. This test wires the cross-check
    directly: inject a fake OSCM handler into the kopf registry,
    call ``install_singleton_guard`` against a clean registry, and
    assert the wrapping was skipped and the error log recorded.
    """
    from openstudio_operator import _oscm_handlers

    class _FakeRegistry:
        class _spawning:
            _handlers: list[object]

    class _FakeSelector:
        group = GROUP
        any_name = None
        plural = PLURAL

    class _FakeHandler:
        id = "_oscm_handler_that_forgot_to_register"
        selector = _FakeSelector()

        def __init__(self) -> None:
            self.fn = lambda *args, **kwargs: None

    fake_registry = _FakeRegistry()
    fake_registry._spawning._handlers = [_FakeHandler()]

    # Save and restore the Python registry around the call so the test
    # is hermetic against the suite-wide singleton-import side effect.
    saved_registry = dict(_oscm_handlers.REGISTRY)
    saved_legacy = _oscm_handlers.KNOWN_LEGACY_OSCM_HANDLER_IDS
    try:
        _oscm_handlers.REGISTRY.clear()
        with caplog.at_level(logging.ERROR, logger=singleton.logger.name):
            wrapped = singleton.install_singleton_guard(registry=fake_registry)
        assert wrapped == 0, (
            f"Expected install_singleton_guard to wrap 0 handlers when the "
            f"OSCM timer is missing from the Python registry; got "
            f"{wrapped}. The cross-check regression-fence broke."
        )
        error_records = [
            r for r in caplog.records
            if r.levelno == logging.ERROR
            and "Python-level registry" in r.getMessage()
        ]
        assert len(error_records) >= 1, (
            f"Expected at least one ERROR log naming the Python-level "
            f"registry cross-check; got {len(error_records)}. The "
            f"loud-fail contract regressed — see issue #250."
        )
        # The kopf-side handler.fn must NOT be the wrapped fn (the
        # gate skipped wrapping because the registry cross-check failed).
        assert not getattr(fake_registry._spawning._handlers[0].fn, singleton.GUARD_MARKER, False), (
            "The kopf handler's fn was wrapped despite the cross-check "
            "failing. The regression-fence broke — the operator would "
            "boot with a silently un-gated handler."
        )
    finally:
        _oscm_handlers.REGISTRY.clear()
        _oscm_handlers.REGISTRY.update(saved_registry)
        # KNOWN_LEGACY_OSCM_HANDLER_IDS is a frozenset; defensively
        # reassign in case a future maintainer makes it mutable.
        assert _oscm_handlers.KNOWN_LEGACY_OSCM_HANDLER_IDS is saved_legacy, (
            "KNOWN_LEGACY_OSCM_HANDLER_IDS is not a frozenset (it was "
            "replaced by the test seam). Update the gate's cross-check "
            "and the test in lockstep."
        )

