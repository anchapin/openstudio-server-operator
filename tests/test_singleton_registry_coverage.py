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
from openstudio_operator._constants import CRD_GROUP, CRD_PLURAL, CRD_VERSION
from openstudio_operator.config import OperatorConfigError
from openstudio_operator.handlers.redis_layout_check import _check_redis_key_layout_for_cr
from openstudio_operator.redis_client import RedisClientError
from openstudio_operator.status_store import GROUP, PLURAL

# Every handler module the operator ships under ``openstudio_operator.handlers/``
# registers exactly one ``@kopf.timer`` for the OSCM resource
# (``@kopf.timer(**CRD_SPEC, ...)`` in each module). Adding a
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
        "openstudio_operator.handlers.redis_layout_check.get_read_only_redis_client", _stub_factory
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
        "openstudio_operator.handlers.redis_layout_check.get_read_only_redis_client", _stub_factory
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


def test_crd_identity_literals_live_only_in_constants() -> None:
    """Issue #495: the raw CRD identity strings appear in exactly one module.

    ``_constants`` owns the canonical identity (``CRD_GROUP`` /
    ``CRD_VERSION`` / ``CRD_PLURAL`` / ``CRD_SPEC``); every ``@kopf.timer``
    and ``@kopf.on.event`` in the operator consumes ``**CRD_SPEC``. A raw
    literal anywhere else is the silent-detachment regression #495 removed:
    a typo'd or half-updated decorator wires a handler to a resource whose
    watch never fires — the operator looks healthy while doing nothing for
    that CR. Exact-match on the three strings, so prose/docstring mentions
    do not trip the fence; the assert also pins that each literal is still
    DEFINED in ``_constants.py`` (a deletion cannot pass silently either).
    """
    src_root = Path(__file__).resolve().parents[1] / "src" / "openstudio_operator"
    magic = {CRD_GROUP, CRD_VERSION, CRD_PLURAL}
    seen: dict[str, set[str]] = {}
    for py in sorted(src_root.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        hits = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in magic
        }
        if hits:
            seen[str(py.relative_to(src_root))] = hits
    assert seen == {"_constants.py": magic}, (
        f"raw CRD identity literals outside _constants.py (issue #495 "
        f"regression — decorators must consume **CRD_SPEC): {seen}"
    )


def _find_kopf_event_calls() -> list[tuple[str, int, str]]:
    """Return ``(relative_path, lineno, dotted)`` for every ``kopf.event`` call site.

    Walks ``src/openstudio_operator/`` and locates ``ast.Call`` nodes whose
    function resolves to kopf's ``event`` in either import shape (issue
    #651):

    * attribute-shape — ``kopf.event(...)`` or a module-alias form like
      ``k.event(...)``: an :class:`ast.Attribute` whose ``attr`` is
      ``event`` and whose ``value`` is a plain :class:`ast.Name`. The
      value-is-Name restriction is load-bearing: the ``@kopf.on.event(...)``
      decorators used by four handler modules are ALSO ``Attribute`` nodes
      with ``attr == "event"``, but their value is an ``Attribute``
      (``kopf.on``), so they are deliberately not flagged.
    * bare-name shape — ``from kopf import event`` then ``event(...)``:
      an :class:`ast.Name` with id ``event``. AST parsing naturally skips
      comments and docstrings, so prose mentions of ``kopf.event(``
      cannot trip the fence — only real call nodes are collected.
    """
    src_root = Path(singleton.__file__).parent
    found: list[tuple[str, int, str]] = []
    for py in sorted(src_root.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            rel = str(py.relative_to(src_root.parent))
            if isinstance(func, ast.Name) and func.id == "event":
                found.append((rel, node.lineno, func.id))
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "event"
                and isinstance(func.value, ast.Name)
            ):
                dotted = f"{func.value.id}.{func.attr}"
                found.append((rel, node.lineno, dotted))
    return found


def test_kopf_event_call_sites_live_only_in_events_module() -> None:
    """Issue #651: direct ``kopf.event`` calls live only in ``events.py``.

    AGENTS.md (since #496) declares
    :func:`openstudio_operator.events.emit_kopf_event` "the one shared
    direct ``kopf.event`` wrapper", yet ``QueuedKopfEventSink.flush_for``
    called ``kopf.event(...)`` directly at two sites — the chokepoint
    invariant had already drifted, and nothing structural prevented a
    third direct call site. This matters to D11: the ``EventEmitter``
    dry-run gate only protects events that flow through it, and while
    ``emit_kopf_event`` is deliberately un-gated for guard/audit
    Events, an unfenced chokepoint means the next handler that reaches
    for ``kopf.event`` directly silently escapes ``dryRun`` suppression
    with no CI signal.

    The fix (#651) routes the two ``flush_for`` sites through
    ``emit_kopf_event`` (behavior unchanged) and adds this AST gate,
    mirroring ``test_only_one_kubeconfig_loader_call_site`` (#305): any
    production-source ``kopf.event(`` call site (bare-name or
    attribute shape) outside ``openstudio_operator/events.py`` is a
    regression and fails the CI gate loudly.

    Note: a bare ``event(`` name match is intentionally conservative —
    any production callable named ``event`` collides with kopf's event
    API surface and should be renamed rather than exempted.
    """
    found = _find_kopf_event_calls()
    assert found, (
        "No ``kopf.event(`` call site found in src/openstudio_operator/. "
        "The shared wrapper openstudio_operator.events.emit_kopf_event() "
        "must call kopf.event directly — if the wrapper moved, update "
        "this test in lockstep. See issue #651."
    )
    offenders = [
        (path, lineno, dotted)
        for path, lineno, dotted in found
        if not path.endswith("openstudio_operator/events.py")
    ]
    assert not offenders, (
        f"Production code calls ``kopf.event`` outside "
        f"openstudio_operator/events.py: {offenders}. Every direct "
        f"``kopf.event`` call must live in the shared wrapper "
        f"openstudio_operator.events.emit_kopf_event() (issue #496); "
        f"handler paths that need the D11 dry-run gate use "
        f"openstudio_operator.events.EventEmitter instead, and "
        f"non-callback deferrals use "
        f"openstudio_operator.events_sinks.QueuedKopfEventSink "
        f"(issue #651)."
    )
    # Sanity: the two allowed sites inside events.py are the wrapper
    # itself and the EventEmitter.emit post — pin that the canonical
    # module still owns at least one direct call so the fence above can
    # never pass vacuously after a refactor moves the wrapper.
    events_module_sites = [
        (path, lineno, dotted)
        for path, lineno, dotted in found
        if path.endswith("openstudio_operator/events.py")
    ]
    assert events_module_sites, (
        "openstudio_operator/events.py no longer calls ``kopf.event`` "
        "directly — emit_kopf_event and/or EventEmitter.emit must keep "
        "their direct calls (issue #651)."
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
# time inside ``install_singleton_guard``. A new OSCM timer that forgets
# to register fails this test loudly.
#
# Issue #285 retired the legacy-id whitelist
# (``_oscm_handlers.KNOWN_LEGACY_OSCM_HANDLER_IDS``) that grandfathered
# the four pre-#250 timers — they register explicitly now, so the
# cross-check has no exemptions.


def test_python_registry_includes_all_oscm_spawning_handlers() -> None:
    """Issue #250: every OSCM timer must be in the Python-level registry.

    The Python-level registry
    (:mod:`openstudio_operator._oscm_handlers`) is the declarative
    seam: every OSCM spawning handler module (existing or new) calls
    ``register(handler_id, fn)`` at module import time, and the
    singleton guard cross-checks the Python registry against the kopf
    registry at gate time. A new OSCM timer that forgot to register
    would silently slip past the gate (the gate wraps every OSCM timer
    in the kopf registry; the Python registry is what the test pins
    here). Issue #285 retired the legacy-id set that used to exempt
    the four pre-#250 timers; the cross-check has no exemptions now.
    """
    from openstudio_operator import _oscm_handlers

    _ensure_handler_modules_loaded()

    registry = kopf.get_default_registry()
    _spawning_handlers_list(registry)  # boundary check: fails loudly if internals moved
    oscm = _oscm_spawning_handlers(registry)
    kopf_timer_ids = {getattr(h, "id", "?") for h in oscm}

    # Issue #285 — the Python-level registry is the sole source of
    # truth for OSCM handler ids known to the gate. The legacy-id
    # whitelist that used to be unioned in here is retired; every OSCM
    # timer must register explicitly via ``register(handler_id, fn)``.
    python_registry_ids = set(_oscm_handlers.REGISTRY.keys())

    missing = kopf_timer_ids - python_registry_ids
    assert not missing, (
        f"OSCM timer(s) registered with kopf but NOT in the Python-level "
        f"registry (issue #250 / #285): {sorted(missing)}. Every OSCM "
        f"handler module MUST call "
        f"openstudio_operator._oscm_handlers.register(handler_id, fn) "
        f"at module import time. See "
        f"tests/test_singleton_registry_coverage.py and issues #250 / #285."
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
    # Issue #285 retired KNOWN_LEGACY_OSCM_HANDLER_IDS — the legacy-id
    # seam is no longer needed because the cross-check has no exemptions.
    saved_registry = dict(_oscm_handlers.REGISTRY)
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


# --- Issue #570 — expected-count gauge + wrapped<expected partial-unwrap gap -----
#
# Issue #570: the two ``continue`` skip paths inside
# ``install_singleton_guard`` (a handler missing from the Python-level
# registry, #250; a ``dataclasses.replace`` TypeError) leave a
# kopf-registered OSCM timer running UN-GATED. Pre-#570 the only runtime
# signal was the boot-time ERROR log: ``SINGLETON_WRAPPED_HANDLERS``
# exported just the gated count (a plausible "3 of 4") and
# ``OpenStudioOperatorSingletonGuardUnwrapped`` fired only on ``== 0`` —
# a partial unwrap was invisible at /metrics and to alerting. The fix
# exports the expected count too
# (``SINGLETON_EXPECTED_HANDLERS``, sized from the same kopf-registry
# scan this file performs) and rekeys the alert onto a strict
# ``wrapped < expected``. These tests pin the partial-skip acceptance
# criterion (loud log + both gauges), the healthy parity (no false
# alert), and the internals-mismatch fallback (the Python-level registry
# population stands in so a total unwrap keeps firing under strict ``<``).


def _gauge_value_570(gauge) -> float:
    """Current value of an unlabelled module-global Gauge (#491 idiom)."""
    return float(gauge._value.get())


def test_install_partial_skip_exports_wrapped_lt_expected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #570 acceptance: one registered + one unregistered OSCM timer.

    The #570 partial-skip shape — two OSCM timers in the kopf registry,
    only one of them in the Python-level registry (#250) — must export
    the gap as a metric PAIR: ``SINGLETON_WRAPPED_HANDLERS == 1`` (the
    gated half) against ``SINGLETON_EXPECTED_HANDLERS == 2`` (the
    population kopf reports), so the rekeyed
    ``wrapped < expected`` alert fires where the historical ``== 0``
    expression read a plausible "1" and stayed silent. The loud ERROR
    log from the #250 cross-check is asserted alongside — the log names
    the orphan id, the gauge pair makes it scrapeable.
    """
    from openstudio_operator import _oscm_handlers
    from openstudio_operator.metrics import (
        SINGLETON_EXPECTED_HANDLERS,
        SINGLETON_WRAPPED_HANDLERS,
    )

    registry = kopf.OperatorRegistry()

    @kopf.timer(GROUP, "v1alpha1", PLURAL, interval=30.0, registry=registry)
    def oscm_timer_registered(body: dict, **_: object):
        return "served"

    @kopf.timer(GROUP, "v1alpha1", PLURAL, interval=60.0, registry=registry)
    def oscm_timer_orphan(body: dict, **_: object):
        return "ungated"

    # Identity-based id lookup (kopf dedupes colliding ids with
    # suffixes) — the same pattern as _make_gated_oscm_timer_403.
    registered_id = next(
        h.id for h in registry._spawning._handlers if h.fn is oscm_timer_registered
    )
    orphan_id = next(
        h.id for h in registry._spawning._handlers if h.fn is oscm_timer_orphan
    )

    saved_registry = dict(_oscm_handlers.REGISTRY)
    try:
        _oscm_handlers.REGISTRY.clear()
        # Register ONLY the first timer — the second is the #250
        # partial-skip shape (a handler module that forgot register()).
        _oscm_handlers.register(registered_id, oscm_timer_registered)

        with caplog.at_level(logging.ERROR, logger=singleton.logger.name):
            wrapped = singleton.install_singleton_guard(registry=registry)

        # The registered half wrapped; the orphan skipped.
        assert wrapped == 1, (
            f"Expected exactly 1 wrap (registered timer) with 1 orphan "
            f"skip; got {wrapped}. The #250 cross-check fence broke."
        )

        # Loud log: the per-handler ERROR names the orphan handler id.
        error_records = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR
            and "Python-level registry" in r.getMessage()
        ]
        assert any(orphan_id in r.getMessage() for r in error_records), (
            f"Expected the #250 cross-check ERROR to name the orphan id "
            f"{orphan_id!r}; captured: "
            f"{[r.getMessage() for r in caplog.records]!r}."
        )

        # The metric pair: expected counts BOTH kopf-reported timers,
        # wrapped counts only the gated one — 1 < 2 is the partial
        # unwrap made scrapeable (the == 0 alert could not see it).
        assert _gauge_value_570(SINGLETON_WRAPPED_HANDLERS) == 1.0, (
            "SINGLETON_WRAPPED_HANDLERS must read 1.0 (the gated half)."
        )
        assert _gauge_value_570(SINGLETON_EXPECTED_HANDLERS) == 2.0, (
            "SINGLETON_EXPECTED_HANDLERS must read 2.0 (the OSCM "
            "population kopf reports, counted before wrapping)."
        )

        # The orphan's kopf-side fn stays un-gated (the #250 fence's
        # existing contract, restated: the gap is real, not just visual).
        orphan_fn = next(
            h.fn
            for h in registry._spawning._handlers
            if getattr(h, "id", None) == orphan_id
        )
        assert not getattr(orphan_fn, singleton.GUARD_MARKER, False), (
            "The orphan timer's fn must NOT carry the gate marker."
        )
    finally:
        _oscm_handlers.REGISTRY.clear()
        _oscm_handlers.REGISTRY.update(saved_registry)


def test_install_exports_expected_equal_to_wrapped_on_healthy_registry() -> None:
    """Issue #570: a fully-registered registry reads expected == wrapped.

    No false alert: on the healthy path (every OSCM timer registered,
    every wrap succeeds) the pair must agree. The idempotent re-install
    must keep them equal too — the expected pre-pass counts handlers
    ALREADY carrying the marker, so a re-invocation reports the same
    population instead of collapsing the expectation to 0.
    """
    from openstudio_operator import _oscm_handlers
    from openstudio_operator.metrics import (
        SINGLETON_EXPECTED_HANDLERS,
        SINGLETON_WRAPPED_HANDLERS,
    )

    registry = kopf.OperatorRegistry()
    fns = []
    for _ in range(2):

        @kopf.timer(GROUP, "v1alpha1", PLURAL, interval=30.0, registry=registry)
        def oscm_timer_healthy(body: dict, **_: object):
            return "served"

        fns.append(oscm_timer_healthy)

    saved_registry = dict(_oscm_handlers.REGISTRY)
    try:
        _oscm_handlers.REGISTRY.clear()
        for fn in fns:
            kopf_id = next(h.id for h in registry._spawning._handlers if h.fn is fn)
            _oscm_handlers.register(kopf_id, fn)

        assert singleton.install_singleton_guard(registry=registry) == 2
        assert _gauge_value_570(SINGLETON_WRAPPED_HANDLERS) == 2.0
        assert _gauge_value_570(SINGLETON_EXPECTED_HANDLERS) == 2.0

        # Idempotent re-install: 0 NEW wraps, but the pair stays equal —
        # expected counts marker-carrying handlers on the re-scan.
        assert singleton.install_singleton_guard(registry=registry) == 0
        assert _gauge_value_570(SINGLETON_WRAPPED_HANDLERS) == 2.0
        assert _gauge_value_570(SINGLETON_EXPECTED_HANDLERS) == 2.0
    finally:
        _oscm_handlers.REGISTRY.clear()
        _oscm_handlers.REGISTRY.update(saved_registry)


def test_install_internals_mismatch_expected_falls_back_to_python_registry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #570: the mismatch branch sizes the expectation from REGISTRY.

    When the kopf layout is unreadable (the kopf-upgrade shape — the
    gate cannot scan kopf at all), the expected gauge must fall back to
    the Python-level ``_oscm_handlers.REGISTRY`` population so the
    rekeyed strict-``<`` alert keeps firing on the TOTAL unwrap
    (``0 < N``). Sizing the expectation from the unreadable kopf side
    would read ``0 < 0`` and silently re-open the exact gap #491's
    ``== 0`` clause used to cover.
    """
    from openstudio_operator import _oscm_handlers
    from openstudio_operator.metrics import (
        SINGLETON_EXPECTED_HANDLERS,
        SINGLETON_WRAPPED_HANDLERS,
    )

    class _InternalsShiftedRegistry:
        # _spawning carries no _handlers list — the post-upgrade shape
        # the gate's isinstance(handlers, list) check rejects.
        _spawning = object()

    saved_registry = dict(_oscm_handlers.REGISTRY)
    try:
        _oscm_handlers.REGISTRY.clear()
        # A known Python-registry population (two declared OSCM timers).
        _oscm_handlers.register("_fake_oscm_a", lambda *a, **k: None)
        _oscm_handlers.register("_fake_oscm_b", lambda *a, **k: None)

        with caplog.at_level(logging.WARNING, logger=singleton.logger.name):
            assert (
                singleton.install_singleton_guard(
                    registry=_InternalsShiftedRegistry()
                )
                == 0
            )

        assert _gauge_value_570(SINGLETON_WRAPPED_HANDLERS) == 0.0
        assert _gauge_value_570(SINGLETON_EXPECTED_HANDLERS) == 2.0, (
            "The internals-mismatch branch must size the expectation "
            "from len(_oscm_handlers.REGISTRY) so 0 < expected keeps "
            "the total-unwrap alert firing under strict <."
        )
        assert any(
            "registry internals not as expected" in r.getMessage()
            for r in caplog.records
        )
    finally:
        _oscm_handlers.REGISTRY.clear()
        _oscm_handlers.REGISTRY.update(saved_registry)


# --- Issue #285 — Python-level registry is the sole source of truth ------------
#
# Issue #285: ``_oscm_handlers.KNOWN_LEGACY_OSCM_HANDLER_IDS`` was a
# whitelist that exempted the four pre-#250 OSCM timers
# (analysis_sla_monitor, zombie_datapoint_watchdog, web_background_monitor,
# worker_recycler) from the explicit ``register()`` call. The whitelist was
# retired once all four migrated to call ``register(handler_id, fn)`` at
# module import time. This test pins:
#
#   1. No whitelist constant exists (or, if it does, is empty).
#   2. Every kopf @kopf.timer that returns a kopf event-emission handle
#      also has a corresponding ``_oscm_handlers.register()`` entry —
#      no orphans.
#
# The combined invariant is the regression fence: a future reviewer who
# adds a new OSCM timer MUST either (a) call ``register()`` at module
# import time, or (b) re-introduce the whitelist constant — and either
# change fails this test.
#
# Issue #304: ``singleton.py`` historically did a deferred
# ``from openstudio_operator.handlers import _emit_redis_warning_event`` from
# inside :func:`openstudio_operator.singleton._emit_redis_url_guard_events`,
# inverting the natural module dependency direction
# (handlers → singleton, NOT singleton → handlers). The deferred import was
# guarded by ``try/except ImportError`` so the import-time circular risk was
# hidden but real, and was reachable in any future import-ordering change. The
# fix is for ``_emit_redis_url_guard_events`` to call :func:`kopf.event`
# directly (the helper is invoked from kopf ``@kopf.on.startup`` and
# ``@kopf.on.event`` callbacks, where the ``settings_var`` ContextVar is
# populated and ``kopf.event(...)`` is callable directly). The redirect
# removes the inverted import, but the regression risk is "someone re-adds
# the import" or "a future sibling module pulls in handlers via a new
# transitive dep" — both must fail this test loudly.
#
# The test walks the singleton module's TRANSITIVE imports via AST (no
# import-time side effects, safe for the rest of the test suite's import
# order) and asserts that none of the visited modules are the
# ``openstudio_operator.handlers`` package OR any of its submodules. The
# scan is bounded by the project source tree (we only recurse into modules
# under ``src/openstudio_operator/``); third-party imports and stdlib are
# terminal leaves, not probed for their own internal graph (the test would
# never terminate otherwise).


_SCAN_ROOT = Path(singleton.__file__).resolve().parent
_HANDLERS_PKG = "openstudio_operator.handlers"


def _imported_modules_in_file(path: Path) -> set[str]:
    """Return the set of fully-qualified module names imported by ``path``.

    Walks every :class:`ast.Import` and :class:`ast.ImportFrom` node in the
    parsed file and returns the long form of each imported name. The result
    is bounded to module names that start with ``openstudio_operator.`` —
    third-party and stdlib imports are filtered out because (a) the
    regression we're guarding against is specifically a singleton↔handlers
    cycle, and (b) following them would explode the search into kopf,
    kubernetes, requests, etc., none of which the operator controls.

    For ``from openstudio_operator.x import y``, the returned name is
    ``openstudio_operator.x`` (the module, not the symbol). For
    ``import openstudio_operator.x``, the returned name is
    ``openstudio_operator.x``. For ``import openstudio_operator`` (the
    package itself), the returned name is ``openstudio_operator`` — which
    never matches the ``_HANDLERS_PKG`` prefix but is harmless to include.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        # Treat syntax errors as no imports — the linter / CI gate will
        # catch them elsewhere; the AST scan is for import-graph drift,
        # not syntax.
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("openstudio_operator."):
                # All openstudio_operator.* imports are surfaced — the
                # handlers-prefix check happens in the test assertion
                # itself (a singleton→handlers import lands in this
                # set and the test fails the build). Surfacing it here
                # is what the AST walk exists for; the visit set is
                # bounded to the production source tree by the
                # caller's path resolution.
                found.add(mod)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name.startswith("openstudio_operator."):
                    found.add(name)
    return found


def _transitive_openstudio_operator_imports(entry: Path) -> set[str]:
    """Walk the openstudio_operator import graph rooted at ``entry``.

    BFS over the production source tree (``src/openstudio_operator/``);
    modules outside that tree (third-party, stdlib) are terminal leaves.
    Returns the set of every module visited whose dotted name starts with
    ``openstudio_operator.`` — including the entry point itself. The
    ``singleton → handlers`` test gates against any module whose name is
    the ``openstudio_operator.handlers`` package OR a submodule thereof.
    """
    visited: set[str] = set()
    queue: list[Path] = [entry]
    while queue:
        path = queue.pop(0)
        # Map the on-disk path back to the dotted module name. ``singleton.py``
        # is ``openstudio_operator.singleton``; ``handlers/__init__.py`` is
        # ``openstudio_operator.handlers``; ``handlers/analysis_sla.py`` is
        # ``openstudio_operator.handlers.analysis_sla``. Anything else under
        # ``src/openstudio_operator/`` follows the same shape.
        try:
            rel = path.relative_to(_SCAN_ROOT.parent)
        except ValueError:
            continue
        parts = rel.with_suffix("").as_posix().split("/")
        dotted = ".".join(parts)
        if dotted in visited:
            continue
        visited.add(dotted)
        for imported in _imported_modules_in_file(path):
            if not imported.startswith("openstudio_operator."):
                continue
            # Translate the dotted name back to a file path. We only recurse
            # into modules that live under our source tree — third-party and
            # stdlib are pruned above.
            mod_parts = imported.split(".")
            candidate = _SCAN_ROOT.parent.joinpath(*mod_parts)
            py_file = candidate.with_suffix(".py")
            pkg_init = candidate / "__init__.py"
            next_path: Path | None = None
            if py_file.is_file():
                next_path = py_file
            elif pkg_init.is_file():
                next_path = pkg_init
            if next_path is not None and next_path not in queue:
                queue.append(next_path)
    return visited


def test_singleton_module_does_not_import_handlers() -> None:
    """Issue #304: singleton.py's transitive imports never touch handlers.

    The singleton guard is a SHARED utility — it is imported by every
    handler module (and by ``handlers/__init__.py`` itself). A singleton
    module that imports back from ``openstudio_operator.handlers``
    creates a circular import whose failure mode is hidden by the
    ``try/except ImportError`` defensive guard (or, in production, by
    import-order luck). The natural dep direction is handlers → singleton
    (and singleton → its OWN utility submodules like ``_time``,
    ``status_store``, ``metrics``); the handlers package must NOT appear
    anywhere in the singleton module's transitive import graph.

    The test walks the import graph from ``singleton.py`` via AST (no
    import-time side effects) and fails the build if any visited module
    is the ``openstudio_operator.handlers`` package or any of its
    submodules. The scan is bounded to the production source tree
    (``src/openstudio_operator/``); third-party and stdlib imports are
    terminal leaves. The message names the offending module(s) so a
    regression is one read to fix.
    """
    visited = _transitive_openstudio_operator_imports(Path(singleton.__file__))
    handlers_modules = {
        mod for mod in visited
        if mod == _HANDLERS_PKG or mod.startswith(_HANDLERS_PKG + ".")
    }
    assert not handlers_modules, (
        f"singleton.py transitively imports openstudio_operator.handlers — "
        f"inverted module dependency (issue #304). Found: "
        f"{sorted(handlers_modules)}. The natural dep direction is "
        f"handlers → singleton, never the other way around. If singleton "
        f"needs a handler-side helper, call kopf.event(...) directly "
        f"(the kopf @kopf.on.startup and @kopf.on.event callbacks that "
        f"invoke singleton._check are active kopf callbacks where "
        f"settings_var is populated). See issue #304 and "
        f"openstudio_operator/singleton.py:_emit_redis_url_guard_events."
    )


# --- Issue #285 — Python-level registry is the sole source of truth ------------
#
# Issue #285: ``_oscm_handlers.KNOWN_LEGACY_OSCM_HANDLER_IDS`` was a
# whitelist that exempted the four pre-#250 OSCM timers
# (analysis_sla_monitor, zombie_datapoint_watchdog, web_background_monitor,
# worker_recycler) from the explicit ``register()`` call. The whitelist was
# a maintenance burden — every future OSCM spawning handler must NOT be
# added to it (must call ``register()``), but a reviewer has to spot any
# new entry the next time a handler ships. After the four legacy handlers
# migrated, the whitelist was retired and the Python-level registry became
# the SOLE source of truth for OSCM handler ids known to the singleton
# guard's cross-check.
#
# This test pins both halves of the structural invariant at the CI gate:
#
# * no whitelist back-channel — ``KNOWN_LEGACY_OSCM_HANDLER_IDS`` is
#   gone (or, defensively, empty), so a future OSCM handler cannot
#   bypass the gate by being added to the whitelist instead of calling
#   ``register()``;
# * no orphan — every OSCM ``@kopf.timer`` registered by the operator
#   has a corresponding ``register()`` call in its handler module,
#   walked via the kopf spawning registry (the same registry the
#   singleton guard cross-checks).
#
# The companion ``test_python_registry_includes_all_oscm_spawning_handlers``
# pins the membership invariant in the more targeted form; this test is
# the explicit regression fence for #285 — re-introducing either half of
# the structural invariant (whitelist or orphan) fails this test loudly.
def test_no_oscm_handler_orphan_or_legacy_whitelist() -> None:
    """Issue #285: no orphan + no whitelist — the registry is sole source of truth.

    After #285, the singleton guard's cross-check
    (:func:`openstudio_operator.singleton.install_singleton_guard`) has
    no exemptions: every OSCM spawning handler (existing or new) MUST
    register explicitly via
    :func:`openstudio_operator._oscm_handlers.register` at module
    import time. This test pins the structural invariant at the CI gate
    so a regression that re-introduces a whitelist back-channel OR
    lets an OSCM timer skip ``register()`` fails loudly.

    Walks the kopf spawning registry (the same registry the singleton
    guard cross-checks at gate time) and asserts:

    1. ``_oscm_handlers.KNOWN_LEGACY_OSCM_HANDLER_IDS`` does NOT exist
       (or, defensively, is empty) — re-introducing the whitelist would
       silently let a future OSCM handler bypass the gate, defeating
       the entire purpose of the Python registry.
    2. Every OSCM ``@kopf.timer`` registered with kopf has a
       corresponding ``register()`` entry — no orphan; a handler that
       registered with kopf but forgot ``register()`` would skip the
       gate and run un-gated.
    """
    from openstudio_operator import _oscm_handlers

    _ensure_handler_modules_loaded()

    # 1. No whitelist back-channel. Issue #285 retired the legacy-id
    # set; the singleton guard's cross-check has no exemptions. The
    # constant itself must not exist — re-introducing it (even empty)
    # would silently let a future OSCM handler bypass the gate.
    assert not hasattr(_oscm_handlers, "KNOWN_LEGACY_OSCM_HANDLER_IDS"), (
        "_oscm_handlers.KNOWN_LEGACY_OSCM_HANDLER_IDS must be retired "
        "after issue #285 — the singleton guard's cross-check has no "
        "exemptions. Every OSCM timer must call register() explicitly. "
        "Re-introducing the constant (even empty) would silently let a "
        "future OSCM handler bypass the gate. Remove the constant "
        "entirely. See issue #285."
    )

    # 2. No orphan. Walk the kopf spawning registry and assert every
    # OSCM timer id has a corresponding ``register()`` call. This is the
    # structural complement of the membership assertion in
    # ``test_python_registry_includes_all_oscm_spawning_handlers``: that
    # test is the targeted gate; this assertion is the explicit
    # regression fence for #285's "every entry has a corresponding
    # register() call" acceptance criterion.
    registry = kopf.get_default_registry()
    _spawning_handlers_list(registry)  # boundary check: fails loudly if internals moved
    oscm = _oscm_spawning_handlers(registry)
    kopf_timer_ids = {getattr(h, "id", "?") for h in oscm}
    python_registry_ids = set(_oscm_handlers.REGISTRY.keys())

    orphans = kopf_timer_ids - python_registry_ids
    assert not orphans, (
        f"OSCM timer(s) registered with kopf but missing register() call "
        f"in handler module (issue #285 — no whitelist exemption): "
        f"{sorted(orphans)}. Every OSCM timer MUST call "
        f"openstudio_operator._oscm_handlers.register(handler_id, fn) "
        f"at module import time. See issue #285."
    )


# --- Issue #403 — SINGLETON_LOSER_SKIPS_TOTAL per-tick loser suppression --------
#
# The ``_gated`` wrapper skips every OSCM timer tick whose CR is not the
# oldest in the namespace (D05). Pre-#403 the skip branch was a bare
# ``log.debug(...)`` — no counter bump — and the change-gated
# ``SINGLETON_ELECTION_TOTAL{outcome="conflict"}`` only fires when
# ``enforce()`` sees a snapshot DIFFERENT from ``_last_state``, so a
# stable multi-CR namespace produced ZERO per-tick /metrics signals.
# #403 adds ``SINGLETON_LOSER_SKIPS_TOTAL{module,namespace,name}``,
# bumped inside the ``if not active:`` branch on every suppressed tick.
# This test pins the acceptance criterion: a multi-CR namespace (winner
# + loser) advances the counter monotonically, one increment per
# suppressed tick, and the cardinality stays bounded at one series per
# ``(module, namespace, name)`` tuple (the same shape as
# ``HANDLER_TICK_FAILURES_TOTAL`` — the singleton guard's
# one-winner-per-namespace invariant bounds it).

_NAMESPACE = "openstudio-server"
_OLD_TS = "2026-08-10T08:00:00Z"
_NEW_TS = "2026-08-11T08:00:00Z"


def _make_cr_403(name: str, created: str, uid: str) -> dict:
    return {
        "apiVersion": "energy.nrel.gov/v1alpha1",
        "kind": "OpenStudioClusterManager",
        "metadata": {
            "name": name,
            "namespace": _NAMESPACE,
            "creationTimestamp": created,
            "uid": uid,
        },
        "spec": {"serverUrl": f"http://{name}.test"},
    }


class _TwoCrCustomObjectsApi:
    """List-only CustomObjectsApi stand-in: oldest 'alpha' + loser 'beta'.

    Returns a fresh deepcopy-shaped snapshot per call (the guard never
    mutates), mirroring ``FakeCustomObjectsApi`` in
    ``tests/test_singleton_guard.py`` — re-declared locally so this CI
    gate file stays free of cross-test-module imports.
    """

    def __init__(self) -> None:
        self.list_calls = 0

    def list_namespaced_custom_object(self, group, version, namespace, plural):
        assert (group, version, plural) == (GROUP, "v1alpha1", PLURAL)
        assert namespace == _NAMESPACE
        self.list_calls += 1
        return {
            "items": [
                _make_cr_403("alpha", _OLD_TS, "uid-alpha"),
                _make_cr_403("beta", _NEW_TS, "uid-beta"),
            ]
        }


def _singleton_loser_skips_series(module: str) -> set[tuple[str, str, str]]:
    """Every ``(module, namespace, name)`` label tuple the family exposes
    for the given ``module`` label value.

    Scoped to a module value so the process-global REGISTRY's test
    sentinels (e.g. ``tests/test_metrics_endpoint.py`` pre-touches the
    family with ``module="__metrics_test_sentinel__"``) do not pollute
    the cardinality pin — the assertion targets the production
    vocabulary (the wrapped handler's ``__name__``).
    """
    series: set[tuple[str, str, str]] = set()
    for metric in singleton.SINGLETON_LOSER_SKIPS_TOTAL.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total") and sample.labels.get("module") == module:
                series.add(
                    (
                        str(sample.labels.get("module")),
                        str(sample.labels.get("namespace")),
                        str(sample.labels.get("name")),
                    )
                )
    return series


def _singleton_loser_skips_value(module: str, namespace: str, name: str) -> float:
    """Current value of the one series identified by the label tuple."""
    for metric in singleton.SINGLETON_LOSER_SKIPS_TOTAL.collect():
        for sample in metric.samples:
            if (
                sample.name.endswith("_total")
                and sample.labels.get("module") == module
                and sample.labels.get("namespace") == namespace
                and sample.labels.get("name") == name
            ):
                return float(sample.value)
    return 0.0


def _make_gated_oscm_timer_403() -> tuple[object, object]:
    """Registry with one registered + gated OSCM timer; returns (gated_fn, registry)."""
    from openstudio_operator import _oscm_handlers

    registry = kopf.OperatorRegistry()

    @kopf.timer(GROUP, "v1alpha1", PLURAL, interval=30.0, registry=registry)
    def oscm_timer(body: dict, spec: dict, **_: object):
        return ("served", spec.get("serverUrl"))

    # Issue #250 — the gate cross-checks every OSCM timer against the
    # Python-level registry before wrapping; register explicitly (same
    # pattern as make_registry_with_handlers in test_singleton_guard.py).
    kopf_id = next(
        h.id for h in registry._spawning._handlers if h.fn is oscm_timer
    )
    _oscm_handlers.register(kopf_id, oscm_timer)

    assert singleton.install_singleton_guard(registry=registry) == 1
    gated = next(
        h.fn for h in registry._spawning._handlers if singleton._selector_matches_oscms(h)
    )
    assert gated.__name__ == "oscm_timer"  # functools.wraps: the module label
    return gated, registry


def test_gated_wrapper_bumps_singleton_loser_skips_total_monotonically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #403 acceptance: a multi-CR namespace advances
    ``SINGLETON_LOSER_SKIPS_TOTAL{module,namespace,name}`` monotonically —
    exactly one increment per suppressed loser tick, zero for the winner's
    ticks — and the family's cardinality stays bounded at one series per
    ``(module, namespace, name)`` tuple (no winner series, no per-tick
    series growth).
    """
    gated, _ = _make_gated_oscm_timer_403()
    monkeypatch.setattr(singleton, "_process_guard", singleton.SingletonGuard(_TwoCrCustomObjectsApi()))
    log = logging.getLogger(__name__)

    loser_body = _make_cr_403("beta", _NEW_TS, "uid-beta")
    winner_body = _make_cr_403("alpha", _OLD_TS, "uid-alpha")

    before = _singleton_loser_skips_value("oscm_timer", _NAMESPACE, "beta")

    # Three suppressed loser ticks: each must advance the counter by
    # exactly 1.0 (monotonic, no coalescing — the change-gated election
    # counter is silent for a stable snapshot, this one must not be).
    observed = [before]
    for _ in range(3):
        assert (
            gated(
                body=loser_body,
                spec={"serverUrl": "http://b.test"},
                namespace=_NAMESPACE,
                name="beta",
                logger=log,
            )
            is None
        )
        observed.append(_singleton_loser_skips_value("oscm_timer", _NAMESPACE, "beta"))
    assert observed == [before, before + 1.0, before + 2.0, before + 3.0], (
        f"loser ticks must advance the counter monotonically by exactly "
        f"1.0 per suppressed tick; observed {observed}. See issue #403."
    )

    # The winner's tick runs the handler body (fail-open on the gate's
    # happy path) and must NOT bump the loser series — nor create a
    # winner-named series.
    assert (
        gated(
            body=winner_body,
            spec={"serverUrl": "http://a.test"},
            namespace=_NAMESPACE,
            name="alpha",
            logger=log,
        )
        == ("served", "http://a.test")
    )
    assert _singleton_loser_skips_value("oscm_timer", _NAMESPACE, "beta") == before + 3.0

    # Cardinality pin: for the production module vocabulary, exactly one
    # series for the family — the loser's (module, namespace, name)
    # tuple. The default REGISTRY is shared process-wide (the
    # metrics-endpoint test pre-touches a ``__metrics_test_sentinel__``
    # module series), so the pin is scoped to the wrapped handler's
    # ``__name__``; every loser-path invocation labelled with this exact
    # tuple bumps the same single series regardless of test ordering
    # (same assertion shape as the #307 helper in
    # tests/test_singleton_guard.py).
    assert _singleton_loser_skips_series("oscm_timer") == {
        ("oscm_timer", _NAMESPACE, "beta")
    }, (
        f"SINGLETON_LOSER_SKIPS_TOTAL cardinality must stay bounded at one "
        f"series per (module, namespace, name) tuple; observed "
        f"{_singleton_loser_skips_series('oscm_timer')}. See issue #403."
    )


# --- Issue #718: every K8s-API fake lives in tests/_fakes.py ------------
#
# Pre-#718, ``FakeCoreV1Api`` was a local class in
# ``tests/test_prune_entrypoint.py`` — the last K8s-API surface NOT in
# the shared ``_fakes.py`` module after #653's
# ``FakeBatchV1Api``/``FakePodsCoreV1Api`` consolidation. Issue #718
# promoted it into ``_fakes.py`` and the gate below prevents a future
# contributor from re-introducing a duplicate ``class FakeXxxApi``
# definition in any ``tests/test_*.py`` file.


def _local_fake_k8s_api_classes() -> list[tuple[str, str, int]]:
    """Walk ``tests/`` and return every ``class Fake<...>Api`` definition
    that lives OUTSIDE ``_fakes.py``.

    Each entry is ``(test_file_path, class_name, line_number)`` so the
    failure message points at the exact location of the violation.
    """
    tests_root = Path(__file__).parent
    found: list[tuple[str, str, int]] = []
    for py in sorted(tests_root.glob("test_*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            # Match ``Fake<Something>Api`` (CoreV1Api, AppsV1Api, etc.).
            if not node.name.startswith("Fake") or not node.name.endswith("Api"):
                continue
            found.append((str(py.relative_to(tests_root.parent)), node.name, node.lineno))
    return found


def test_all_k8s_api_fakes_live_in_tests_fakes_py() -> None:
    """Issue #718 — every ``Fake<...>Api`` class lives in ``tests/_fakes.py``.

    The shared fakes module is the single home for every K8s-API stand-in
    (CustomObjectsApi / AppsV1Api / BatchV1Api / PodsCoreV1Api /
    SecretsCoreV1Api / CoreV1Api). A duplicate local class would re-open
    the drift door #653 closed, and consumers would have to know which
    of two implementations they're using.
    """
    violations = _local_fake_k8s_api_classes()
    assert violations == [], (
        f"K8s-API fake classes must live in tests/_fakes.py only "
        f"(issue #718, #653 consolidation). Found local definitions at: "
        f"{[(p, n, l) for p, n, l in violations]}"
    )


# --- Issue #725: StatusEventSink has a single canonical definition ---------
#
# Pre-#725, ``EmitStatusEvent`` (status_store.py) and ``StatusEventSink``
# (events_sinks.py) were structurally-identical
# ``Callable[[str, str, str, str], None]`` aliases defined twice. Issue
# #725 collapsed ``EmitStatusEvent`` into a re-export of
# ``StatusEventSink``, and the gate below prevents re-introduction of
# a duplicate ``Callable[...]`` assignment in either module.


def _emit_status_event_alias_definitions() -> list[tuple[str, str, int]]:
    """Return every module-level ``EmitStatusEvent`` / ``StatusEventSink``
    binding across ``src/openstudio_operator/`` so the failure message
    names the exact location of any violation.

    Each entry is ``(module_path, alias_name, line_number)``. The re-export
    in status_store.py is permitted (it's the post-#725 shape); a
    fresh ``= Callable[...]`` assignment in either module is the violation.
    """
    from openstudio_operator import _constants  # canonical reference; not the test target

    src_root = Path(_constants.__file__).parent
    found: list[tuple[str, str, int]] = []
    for py in sorted(src_root.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in tree.body:
            if not isinstance(node, ast.AnnAssign):
                continue
            target = node.target
            if not isinstance(target, ast.Name):
                continue
            if target.id not in ("EmitStatusEvent", "StatusEventSink"):
                continue
            # The post-#725 status_store.py re-export is an
            # ``AnnAssign`` whose value is an ``ImportFrom`` (the re-export).
            # Anything else (a fresh ``Callable[...]`` assignment) is the
            # violation we want to surface.
            if isinstance(node.value, ast.Call):
                found.append((str(py.relative_to(src_root.parent)), target.id, node.lineno))
    return found


def test_emit_status_event_alias_has_single_definition() -> None:
    """Issue #725 — ``EmitStatusEvent`` / ``StatusEventSink`` is defined once.

    Pre-#725 two structurally-identical ``Callable[...]`` aliases existed
    in :mod:`status_store` and :mod:`events_sinks`. The collapse keeps the
    canonical home in :mod:`events_sinks` (``StatusEventSink``) and
    re-exports it as ``EmitStatusEvent`` from :mod:`status_store` (the
    only caller-facing name in the prior contract). A regression — a
    fresh ``Callable[...]`` assignment in either module — would recreate
    the drift #496/#725 closed.
    """
    violations = _emit_status_event_alias_definitions()
    assert violations == [], (
        f"EmitStatusEvent / StatusEventSink must each be defined at most once "
        f"across src/openstudio_operator/ (issue #725, #496 consolidation). "
        f"Found duplicate assignments at: {[(p, n, l) for p, n, l in violations]}"
    )
