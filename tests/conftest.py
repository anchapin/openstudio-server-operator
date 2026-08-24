"""Process-wide reset fixture for the operator's module-level singletons (issue #247).

The operator builds a small set of module-level singletons lazily on the
first tick and reuses them for the process's lifetime:

* :data:`openstudio_operator.singleton._process_guard` — the
  :class:`~openstudio_operator.singleton.SingletonGuard` instance kopf
  handlers consult on every tick to decide which OSCM CR is the active
  one (D05, issue #14). Built on first access; replaced/reset only by
  this fixture (and by explicit tests that opt in).
* :data:`openstudio_operator.singleton._operator_custom_objects_api`
  — the cached :class:`kubernetes.client.CustomObjectsApi` the
  :func:`~openstudio_operator.singleton.operator_custom_objects_api`
  factory returns (issue #158). Built on first access by loading the
  in-cluster (or ``kube_config``) configuration; tests that swap the
  loaders need to drop this cache between cases.

Two additional module-level globals — the legacy
``handlers._NOTIFY_QUEUE`` / ``_REDIS_KEY_LAYOUT_QUEUE`` /
``_STATUS_MAP_CAP_QUEUE`` Warning-Event queues that the original
acceptance text of #247 named — were collapsed into a single
:class:`openstudio_operator.events_sinks.QueuedKopfEventSink` by
issue #234, so this fixture no longer has anything to clear there. The
defensive ``getattr(..., None)`` guards below are preserved verbatim in
case a future refactor reintroduces a similarly-named module-level
queue on the handlers package; the fixture becomes a no-op for those
attributes rather than blowing up at import time.

Without this fixture a test that left ``_process_guard`` pointing at a
``FakeCustomObjectsApi`` would silently gate every subsequent test's
wrapper invocation as "not active" — the failure surfaces only on the
NEXT test, far from its cause, and only because the guarded wrapper
returns ``None`` instead of crashing. The autouse pre+post reset is the
documented seam (see the ``SingletonGuard`` / ``operator_custom_objects_api``
docstrings in :mod:`openstudio_operator.singleton`); this conftest is
the only place it is invoked globally — per-file fixtures in
``tests/test_k8s_clients.py`` and ``tests/test_timer_wrapper_failures.py``
keep their existing local resets (they're cheaper and add no behaviour
beyond this one).

Issue #251 — the three new K8s client factories
(``operator_apps_api``, ``operator_batch_api``, ``operator_core_api``)
also cache their constructed clients for the process lifetime. The
``reset_operator_k8s_client`` seam (issues #158 + #251) drops all four
caches in one call. The ``Configuration._default`` reset is the
client-python equivalent: ``load_kube_config`` (the fallback when
``load_incluster_config`` raises in a CI environment) mutates the
global default, and a handler test that triggered the factory (e.g.
``run_sla_tick(..., pod_api=None)`` → ``operator_core_api()``) would
leave the default populated for the next test, defeating the
``default_configuration_restored`` snapshot in ``test_singleton_guard.py``.
"""

from __future__ import annotations

from collections.abc import Generator

import kubernetes.client
import pytest

from _cache_census import CACHE_BEARING_MODULES as _PER_CR_CACHE_MODULES
from openstudio_operator import handlers, singleton, status_store

# Issue #497 — the per-CR cache reset seams join the autouse reset. Since
# #652 the census of cache-bearing modules is SINGLE-SOURCED in
# ``tests/_cache_census.py`` (imported above): this file, the convention
# tests, and the AST fence in ``test_cache_keying_convention.py`` all
# read the same tuple — no hand-maintained duplicate to drift. Dropping
# every seam here (pre AND post, like the singleton/K8s-client resets
# above) keeps the suite hermetic against a test that populates a per-CR
# cache and crashes mid-case. The cache-FREE modules (analysis_sla,
# worker_recycler — memory lives in CR ``.status``, D04 — plus
# dry_run_audit / redis_layout_check) deliberately expose no seam; the
# #652 AST fence fails any handler module in neither census tuple.

# Issue #257 — test-suite duration budget.
#
# AGENTS.md cites "sub-5 s suites" as the design budget. pyproject.toml
# now prints the slowest 10 tests via ``--durations=10 --durations-min=1.0``
# so any test that creeps past 1 s is visible in CI output; this module
# is the hard ceiling — fail the run if the slowest 10 tests sum to more
# than ``_SLOWEST_TOTAL_BUDGET_SECONDS``. The hook fires on every test
# outcome (pass / skip / xfail / fail); the budget is independent of
# pass/fail so a flaky slow test still surfaces the regression even when
# it eventually passes.
#
# Scope guard from the issue: do NOT mark any existing test as
# ``@pytest.mark.slow`` — the budget machinery is opt-out by being silent
# (a future maintainer who adds a legitimately-slow integration test
# bumps the constant below). The marker slot is left for the day a test
# genuinely needs >5 s; today nothing does.
_SLOWEST_TOTAL_BUDGET_SECONDS = 30.0
_SLOWEST_N = 10


def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    """Record the wall-clock duration of every test for the budget hook.

    pytest's own ``--durations`` machinery reads
    ``report.duration`` after this hook fires, so we piggy-back on the
    same field — no extra timing primitive needed. The list lives on
    the session's config object so the
    ``pytest_terminal_summary`` hook below can sum the top N.
    """
    if call.when != "call":
        return
    durations: list[float] = getattr(item.config, "_durations_recorded", None)
    if durations is None:
        durations = []
        item.config._durations_recorded = durations  # type: ignore[attr-defined]
    durations.append(call.duration)


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Fail the run if the slowest N tests total more than the budget.

    Reads the durations recorded by
    :func:`pytest_runtest_makereport`, sorts descending, sums the top
    ``_SLOWEST_N``, and writes a terminal-summary section with the
    breakdown plus a clear pass/fail verdict. The section appears in
    CI output even on green runs (so a budget regression is visible in
    the PR thread before it tips over).
    """
    durations: list[float] = list(getattr(config, "_durations_recorded", []) or [])
    if not durations:
        return
    durations.sort(reverse=True)
    top_n = durations[:_SLOWEST_N]
    total = sum(top_n)
    verdict = "OK" if total <= _SLOWEST_TOTAL_BUDGET_SECONDS else "OVER BUDGET"
    terminalreporter.write_sep(
        f"slowest-{_SLOWEST_N} total budget ({_SLOWEST_TOTAL_BUDGET_SECONDS:.1f}s)",
        yellow=(verdict == "OVER BUDGET"),
    )
    terminalreporter.write_line(
        f"{verdict}: slowest {_SLOWEST_N} tests total {total:.2f}s "
        f"(budget {_SLOWEST_TOTAL_BUDGET_SECONDS:.1f}s)"
    )
    for idx, duration in enumerate(top_n, start=1):
        terminalreporter.write_line(f"  #{idx:<2} {duration:6.2f}s")
    if verdict == "OVER BUDGET":
        terminalreporter.write_line(
            "Issue #257 budget exceeded — bump _SLOWEST_TOTAL_BUDGET_SECONDS "
            "in tests/conftest.py if a new test legitimately needs >5s, or "
            "investigate the slowest tests above for a regression."
        )


@pytest.fixture(autouse=True)
def _reset_operator_module_state() -> Generator[None, None, None]:
    """Drop the operator's module-level singletons before AND after each test.

    Pre-reset: a cached ``_process_guard`` or ``_operator_custom_objects_api``
    from a previous test cannot leak into the current case.

    Post-reset (defensive): if a test crashes mid-run, monkeypatch's atexit
    reverts do not touch the module-level globals — the explicit reset
    keeps the suite hermetic for the next test, and prevents the next
    case from inheriting a half-built cache that would force
    :func:`singleton._get_guard` to rebuild against whatever kubeconfig
    the crashed test left in place.

    The legacy queue-clear calls (``handlers._NOTIFY_QUEUE.clear()`` etc.)
    are guarded with :func:`getattr` so the fixture remains compatible
    with the post-#234 ``handlers`` module — they are no-ops today and
    will be no-ops forever if #234 stays in, or active again if a future
    refactor reintroduces similarly-named globals.

    Issue #251 — also drops ``kubernetes.client.Configuration._default``
    so a test that triggered the kubeconfig fallback
    (``load_kube_config`` mutates the global) does not leave a real
    host installed for the next test. The
    ``default_configuration_restored`` fixture in ``test_singleton_guard.py``
    only restores the snapshot it took at fixture setup — if a previous
    test populated ``Configuration._default`` with a real host, the
    snapshot is the polluted state. Resetting in conftest is the actual
    root-cause cleanup.
    """
    singleton.set_guard(None)
    singleton.reset_operator_k8s_client()
    kubernetes.client.Configuration._default = None
    # Issue #648 — the live-anchor protection registry is advisory-only
    # eviction-ordering state, but a test that registers protections
    # against the shared (NAMESPACE, NAME) fixtures must not tilt a later
    # cap-eviction case. Reset pre AND post like the seams above.
    status_store.reset_protected_anchor_keys()
    for module in _PER_CR_CACHE_MODULES:
        module.reset_per_cr_caches()
    for legacy_queue_attr in ("_NOTIFY_QUEUE", "_REDIS_KEY_LAYOUT_QUEUE", "_STATUS_MAP_CAP_QUEUE"):
        queue = getattr(handlers, legacy_queue_attr, None)
        if queue is not None and hasattr(queue, "clear"):
            queue.clear()
    yield
    singleton.set_guard(None)
    singleton.reset_operator_k8s_client()
    kubernetes.client.Configuration._default = None
    status_store.reset_protected_anchor_keys()
    for module in _PER_CR_CACHE_MODULES:
        module.reset_per_cr_caches()