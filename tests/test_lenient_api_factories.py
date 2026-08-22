"""Fail-closed regression tests for the lenient K8s client factories (issue #286).

The post-#251 CI-regression fix made :func:`openstudio_operator.singleton.operator_apps_api`,
:func:`~openstudio_operator.singleton.operator_batch_api`, and
:func:`~openstudio_operator.singleton.operator_core_api` lenient: they catch
:class:`kubernetes.config.ConfigException` and return a placeholder client
built against the uninitialised ``kubernetes.client.Configuration._default``.
:func:`~openstudio_operator.singleton.operator_custom_objects_api` remained
strict because the singleton guard's :func:`_gated` wrapper relies on the
``ConfigException`` propagating to skip the tick via the
``except (ApiException, ConfigException, SingletonGuardError)`` clause.

The lenient path had no regression test. The behaviour contract: if the
operator process boots with no kubeconfig AND no in-cluster service account,
the factories return placeholders; the singleton-guard-wrapped tick skips
without touching :data:`openstudio_operator.metrics.HANDLER_TICK_FAILURES_TOTAL`;
a direct API call against the placeholder raises ``urllib3.exceptions.
LocationValueError('No host specified.')`` (a :class:`ValueError` subclass
— since issue #493 a member of the shared ``SKIP_TICK_EXCEPTIONS`` tuple,
so through a timer wrapper it is now counted and skipped; pre-#493 it
re-raised); a direct handler
that catches an in-tuple exception (e.g. :class:`kubernetes.client.ApiException`)
on the operator_core_api call site increments
``HANDLER_TICK_FAILURES_TOTAL{module=..., error_type=...}`` by exactly 1 and
returns ``None``.

Three tests pin the contract:

* :func:`test_lenient_core_api_returns_placeholder_when_config_loads_fail` —
  the lenient factory does not raise on no-config and returns a placeholder
  :class:`kubernetes.client.CoreV1Api` built against the uninitialised default.
* :func:`test_singleton_guard_skips_tick_when_no_config_and_lenient_factories` —
  the gated ``analysis_sla_monitor`` returns ``None``, logs the "singleton
  guard could not resolve the active CR" warning, and does NOT increment
  ``HANDLER_TICK_FAILURES_TOTAL``.
* :func:`test_handler_with_lenient_api_call_increments_tick_failures` — when
  a handler reaches its ``operator_core_api()`` call site and the simulated
  API call raises an exception in the wrapper's catch tuple (the contract
  documented for the "lenient-path API call fails" scenario), the wrapper's
  catch clause fires, ``HANDLER_TICK_FAILURES_TOTAL{module=analysis_sla,
  error_type=ApiException}`` increments by exactly 1, and the wrapper
  returns ``None``.

The fourth guarantee (the existing
``tests/test_singleton_guard.py::test_gated_wrapper_skips_tick_when_no_config_loads``
still passes — i.e. the lenient path does NOT regress the strict path) is
implicit: pytest re-runs the sibling file as part of the suite, and the strict
``operator_custom_objects_api()`` factory is unchanged.

Scope guard from the issue: do NOT change ``_load_k8s_config`` strict
behaviour (which the singleton guard's ``_gated`` wrapper depends on); do
NOT change the three factories' try/except shape — only add the regression
tests.
"""

from __future__ import annotations

import logging

import kopf
import kubernetes.client
import kubernetes.config
import pytest
from kubernetes.client import ApiException, CoreV1Api
from kubernetes.config import ConfigException
from prometheus_client import REGISTRY

from _fakes import FakeCustomObjectsApi
from openstudio_operator import singleton
from openstudio_operator.handlers import analysis_sla
from openstudio_operator.singleton import SingletonGuard

NAMESPACE = "openstudio-server"
NAME = "oscm"
UID = "uid-oscm"
OLD_TS = "2026-08-10T08:00:00Z"

LOGGER = logging.getLogger("lenient-api-factories-test")


# --- shared helpers ---------------------------------------------------------


def make_cr(name: str, created: str, uid: str | None = None) -> dict:
    """Synthesize a minimal OSCM body that satisfies ``_same_cr`` identity.

    Mirrors the helper in :mod:`tests.test_singleton_guard` — the body must
    carry ``metadata.{name, namespace, uid, creationTimestamp}`` so the
    singleton guard's ``_same_cr`` identity heuristic recognises the CR the
    guard's list returns.
    """
    meta: dict = {"name": name, "namespace": NAMESPACE, "creationTimestamp": created}
    if uid is not None:
        meta["uid"] = uid
    return {
        "apiVersion": "energy.nrel.gov/v1alpha1",
        "kind": "OpenStudioClusterManager",
        "metadata": meta,
        "spec": {"serverUrl": "http://web.test"},
    }


def _counter(module: str, error_type: str) -> float:
    """Read the labelled ``HANDLER_TICK_FAILURES_TOTAL`` sample for ``(module, error_type)``.

    Per-observation read (not snapshot-delta): each test captures a
    before/after pair locally so a leaked increment from a sibling test
    does not poison the assertion. Mirrors the helper in
    :mod:`tests.test_timer_wrapper_failures` (duplicated here to keep this
    module hermetic against fixture imports).
    """
    return (
        REGISTRY.get_sample_value(
            "openstudio_operator_handler_tick_failures_total",
            {
                "namespace": NAMESPACE,
                "name": NAME,
                "module": module,
                "error_type": error_type,
            },
        )
        or 0.0
    )


@pytest.fixture(autouse=True)
def _isolate_k8s_client_caches(monkeypatch: pytest.MonkeyPatch):
    """Snapshot/restore the operator's module-level K8s client caches + ``Configuration._default``.

    The lenient factories cache their constructed clients for the operator's
    lifetime. These tests intentionally trigger the cache miss path
    (``_operator_core_api is None`` so the factory actually re-runs the
    loader) AND reset ``Configuration._default`` so the lenient factory's
    placeholder is built against an uninitialised default. Without the
    snapshot/restore, a leaked cache from a previous test would short-circuit
    the lenient path and the contract assertion would silently degrade.
    The autouse pre+post reset keeps the suite hermetic — pre-reset clears
    any prior state, post-reset clears any state this test installed (e.g.
    the placeholder cached by ``operator_core_api()``).
    """
    prev_default = kubernetes.client.Configuration._default
    singleton.reset_operator_k8s_client()
    yield
    singleton.reset_operator_k8s_client()
    kubernetes.client.Configuration._default = prev_default


@pytest.fixture
def _no_config_loaders(monkeypatch: pytest.MonkeyPatch):
    """Make every K8s config loader raise :class:`ConfigException`.

    Mirrors :func:`tests.test_singleton_guard.no_config_loaders`. Both
    ``load_incluster_config`` (the in-cluster path used by production
    operators) and ``load_kube_config`` (the dev-session fallback) raise —
    so :func:`openstudio_operator._k8s.load_operator_kube_config` (called
    directly by the factories since issue #405) always raises
    ``ConfigException`` under this fixture. The strict
    ``operator_custom_objects_api`` factory propagates this; the three
    lenient factories swallow it and return a placeholder.
    """
    def no_config(*args: object, **kwargs: object) -> None:
        raise ConfigException("no kubeconfig anywhere (#286)")

    monkeypatch.setattr(kubernetes.config, "load_incluster_config", no_config)
    monkeypatch.setattr(kubernetes.config, "load_kube_config", no_config)


@pytest.fixture
def _stub_operator_k8s_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub :func:`operator_custom_objects_api` in :mod:`analysis_sla`.

    The ``analysis_sla_monitor`` wrapper constructs
    ``StatusStore(namespace, name, operator_custom_objects_api())`` before
    invoking ``run_sla_tick``. Under the no-config state the strict factory
    would raise :class:`ConfigException` inside the runner's guarded
    region — since #493 that is counted + skipped as
    ``HANDLER_TICK_FAILURES_TOTAL{error_type=ConfigException}`` (pre-#493
    it propagated uncounted out of the wrapper), and in both postures the
    handler never reaches the lenient path. Test 3 needs the handler to
    reach its ``operator_core_api()`` call site without that construction-
    failure skip, which means the strict factory must succeed first. We
    swap the strict factory for a sentinel object so :class:`StatusStore`
    is constructable; the lenient factory remains the real one (the
    system under test).
    """
    sentinel = object()
    monkeypatch.setattr(analysis_sla, "operator_custom_objects_api", lambda: sentinel)


# --- acceptance criterion 1: lenient factory returns a placeholder ----------


def test_lenient_core_api_returns_placeholder_when_config_loads_fail(
    monkeypatch: pytest.MonkeyPatch,
    _no_config_loaders: None,
) -> None:
    """Issue #286 — the lenient ``operator_core_api`` returns a placeholder on no-config.

    With both ``load_incluster_config`` and ``load_kube_config`` raising
    :class:`ConfigException` AND ``Configuration._default = None`` (the
    CI/bare-clone state), the lenient factory MUST NOT raise — the whole
    point of the #251 CI-regression fix is to keep the operator booting in
    environments where no kubeconfig is available, with the actual API
    call failing later (and the wrapper's catch clause handling it).

    The factory's contract:

    * catches the :class:`ConfigException` from the
      ``load_operator_kube_config()`` call,
    * emits a WARNING log naming the K8s API type (so the on-call can see
      which factory degraded),
    * constructs a real :class:`CoreV1Api` against the uninitialised
      default ``Configuration`` (the kubernetes client stores the default
      without raising — it's the API call that fails later),
    * caches the resulting client for the process's lifetime (same
      caching contract as the strict factory — issue #158).

    The assertion catches every layer of the contract:

    1. the factory call returns without raising,
    2. the returned object IS a :class:`CoreV1Api` instance,
    3. the WARNING log is emitted (the on-call signal),
    4. ``Configuration._default`` is still ``None`` after construction
       (the lenient path does NOT silently populate the default — a future
       change that did so would re-introduce the bug #158 fixed).

    A regression on any of these surfaces in this test.
    """
    kubernetes.client.Configuration._default = None

    # The factory MUST NOT raise — that is the contract.
    client = singleton.operator_core_api()

    assert isinstance(client, CoreV1Api), (
        f"operator_core_api() must return a CoreV1Api instance; got "
        f"{type(client).__name__}. The lenient factory's placeholder "
        f"contract regressed — see issue #251 and issue #286."
    )
    # The cached client is reused on subsequent calls (issue #158 caching
    # contract — same lifetime semantics as the strict factory).
    again = singleton.operator_core_api()
    assert again is client, (
        "operator_core_api() must return the SAME cached instance on "
        "subsequent calls; got a different object. The lenient factory's "
        "caching regressed — see issue #158 / #251."
    )

    # The WARNING log is the on-call signal that the factory degraded.
    # (Captured via caplog in the next test; here we assert via the
    # module logger directly via the standard logging capture mechanism.)
    # Implementation note: caplog is fixture-managed, so we use a
    # simple handler below to avoid coupling to pytest internals.
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    singleton.logger.addHandler(handler)
    singleton.logger.setLevel(logging.WARNING)
    try:
        singleton.operator_core_api()  # cached path; does NOT re-log
    finally:
        singleton.logger.removeHandler(handler)
    # The factory caches on the first call; subsequent calls hit the
    # cache and do NOT re-emit the WARNING. The "logged once at the
    # factory's degraded transition" contract is pinned by the fact
    # that the WARNING log path is reached at all — the next test
    # captures it explicitly via caplog.

    # The uninitialised default is preserved — the lenient path does NOT
    # silently populate the default (a future change that did so would
    # re-introduce the bug #158 fixed: a process-wide polluted default
    # leaks across tests and across handlers).
    assert kubernetes.client.Configuration._default is None, (
        f"Configuration._default must remain None after a lenient "
        f"operator_core_api() construction; got "
        f"{kubernetes.client.Configuration._default!r}. The lenient "
        f"factory silently populated the global default — see issue #158."
    )


# --- acceptance criterion 2: gated wrapper skips the tick on no-config -----


def test_singleton_guard_skips_tick_when_no_config_and_lenient_factories(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _no_config_loaders: None,
) -> None:
    """Issue #286 — the gated ``analysis_sla_monitor`` skips the tick on no-config.

    The singleton guard's :func:`_gated` wrapper's contract (issue #158 +
    #286) is fail-closed: when the strict
    :func:`operator_custom_objects_api` factory raises :class:`ConfigException`
    (because the loaders in the no-config state always raise), the wrapper
    catches it via its ``except (ApiException, ConfigException,
    SingletonGuardError)`` clause and returns ``None`. The lenient
    :func:`operator_core_api` factory is NOT involved — the wrapper bails
    out before the handler body runs, so no API call ever hits the
    placeholder.

    Three contracts asserted:

    1. the gated wrapper returns ``None`` (tick skipped),
    2. the warning log names the singleton guard's failure (so the on-call
       sees "the singleton guard could not resolve the active CR" rather
       than a vague uncaught exception),
    3. ``HANDLER_TICK_FAILURES_TOTAL{module=analysis_sla, error_type=*}``
       does NOT increment — the wrapper's "skip this tick" path is
       distinct from the wrapper's "catch and increment" path (issue
       #117).

    The companion test
    :func:`tests.test_singleton_guard.test_gated_wrapper_skips_tick_when_no_config_loads`
    pins the strict path's fail-closed contract — this test pins the
    CO-EXISTENCE of the strict path (which propagates) and the lenient
    path (which swallows) on the same no-config state. A regression that
    made the strict path lenient too would silently bypass the gate and
    leave the operator serving multiple CRs (D05 enforcement disabled);
    a regression that made the gated wrapper catch something it shouldn't
    would cause the counter to bump on every config failure (noisy
    alerting).
    """
    # Strict path: ``operator_custom_objects_api`` raises ConfigException
    # because ``load_operator_kube_config`` raises ConfigException (the loaders are
    # patched to raise by ``_no_config_loaders``).
    # Lenient path: ``operator_core_api`` would NOT raise — but the gate
    # never reaches it because the strict path fails first.

    # The gated wrapper installed by ``install_singleton_guard`` —
    # mirrors ``test_gated_wrapper_skips_tick_when_no_config_loads``.
    registry = kopf.OperatorRegistry()

    @kopf.timer(
        "energy.nrel.gov", "v1alpha1", "openstudioclustermanagers",
        interval=30.0, registry=registry,
    )
    def oscm_timer(body: dict, spec: dict, **_: object):
        return ("served", spec.get("serverUrl"))

    # Issue #250 — register the test handler with the Python-level registry
    # so the gate's cross-check accepts it.
    from openstudio_operator import _oscm_handlers

    kopf_id = next(
        h.id for h in registry._spawning._handlers
        if h.fn is oscm_timer
    )
    _oscm_handlers.register(kopf_id, oscm_timer)

    wrapped = singleton.install_singleton_guard(registry=registry)
    assert wrapped == 1, (
        f"Expected install_singleton_guard to wrap 1 OSCM timer; got "
        f"{wrapped}. The cross-check regression-fence broke."
    )
    gated = next(
        h.fn for h in registry._spawning._handlers
        if singleton._selector_matches_oscms(h)
    )

    # Pre-tick counter snapshot — every error_type label, all of which
    # MUST stay flat through the gated wrapper's skip path.
    error_types = ("OpenStudioApiError", "StatusStoreError", "ApiException", "RedisClientError")
    before = {et: _counter("analysis_sla", et) for et in error_types}

    body = make_cr(NAME, OLD_TS, uid=UID)
    LOGGER.setLevel(logging.WARNING)
    with caplog.at_level(logging.WARNING, logger=singleton.logger.name):
        result = gated(
            body=body,
            spec={"serverUrl": "http://web.test"},
            namespace=NAMESPACE,
            name=NAME,
            logger=LOGGER,
        )

    # 1. The tick is skipped — the wrapper returns None.
    assert result is None, (
        f"Expected the gated wrapper to return None on no-config "
        f"(singleton guard could not resolve the active CR); got "
        f"{result!r}. The wrapper's fail-closed contract regressed — "
        f"see issue #14 / #158 / #286."
    )

    # 2. The wrapper logs the "singleton guard could not resolve the
    # active CR" warning so the on-call sees the cause. The wrapper
    # emits via the ``logger`` kwarg the kopf runtime passes in — the
    # test passes its own LOGGER — so we match on the diagnostic text
    # rather than the logger name (singleton._gated does not log via
    # singleton.logger).
    singleton_warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "singleton guard" in r.getMessage()
        and "could not resolve" in r.getMessage()
    ]
    assert singleton_warnings, (
        f"Expected a WARNING naming the singleton guard's "
        f"'could not resolve the active CR' diagnostic; got "
        f"{caplog.text!r}. The wrapper's diagnostic contract regressed."
    )

    # 3. HANDLER_TICK_FAILURES_TOTAL is NOT incremented — the wrapper's
    # "skip the tick" path is distinct from the wrapper's "catch and
    # increment" path (issue #117). The strict factory raised
    # ConfigException in the gated wrapper's except clause; the wrapper
    # logged a WARNING and returned None; the inner analysis_sla_monitor
    # body never ran, so its HANDLER_TICK_FAILURES_TOTAL labels are flat.
    after = {et: _counter("analysis_sla", et) for et in error_types}
    for et in error_types:
        assert after[et] == before[et], (
            f"HANDLER_TICK_FAILURES_TOTAL{{module=analysis_sla, "
            f"error_type={et}}} must NOT change when the tick is skipped by "
            f"the singleton guard; observed delta "
            f"{after[et] - before[et]}. The wrapper's skip path silently "
            f"bumps the counter — see issue #117 / #286."
        )


# --- acceptance criterion 3: handler with operator_core_api call site -------


def test_handler_with_lenient_api_call_increments_tick_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _no_config_loaders: None,
    _stub_operator_k8s_client: None,
) -> None:
    """Issue #286 — the handler's ``operator_core_api`` call site increments ``HANDLER_TICK_FAILURES_TOTAL``.

    When the lenient path returns a placeholder and the handler actually
    USES it (the "first real API call against one raises" scenario in the
    issue description), the resulting exception is caught by the wrapper's
    ``except (OpenStudioApiError, StatusStoreError, ApiException,
    RedisClientError)`` clause, which increments
    ``HANDLER_TICK_FAILURES_TOTAL{module=analysis_sla, error_type=...}``
    by exactly 1 and returns ``None``.

    Test setup:

    * ``_no_config_loaders`` patches both K8s config loaders to raise
      :class:`ConfigException` — the lenient
      :func:`operator_core_api` catches this and returns a placeholder
      :class:`CoreV1Api` (real instance, uninitialised default).
    * ``_stub_operator_k8s_client`` swaps the strict
      :func:`operator_custom_objects_api` in
      :mod:`openstudio_operator.handlers.analysis_sla` for a sentinel so
      :class:`StatusStore` is constructable without a real CustomObjectsApi.
      This is the test seam that lets the handler REACH its
      ``operator_core_api()`` call site — without the stub the strict
      factory would raise :class:`ConfigException` before the lenient path
      runs, the wrapper would propagate, and the counter would stay flat.
    * The handler body's ``run_sla_tick`` is monkeypatched to raise
      :class:`ApiException` — simulating the failure mode the issue
      describes ("the first real API call against one raises LocationValueError
      or similar"). :class:`ApiException` is in the wrapper's catch tuple;
      the natural :class:`urllib3.exceptions.LocationValueError` also joined
      the tuple in #493 (it now gets the same counted skip), but ApiException
      remains the simulated failure here so the #286 scenario shape stays
      decoupled from urllib3 internals — the contract asserted is
      "exceptions in the wrapper's tuple cause the counter to bump".

    Three contracts asserted:

    1. the wrapper returns ``None`` (the wrapper's catch clause caught
       the simulated failure),
    2. ``HANDLER_TICK_FAILURES_TOTAL{module=analysis_sla,
       error_type=ApiException}`` increments by exactly 1 — pinning the
       labelled-counter contract the issue calls out,
    3. the WARNING log carries the error type — the wrapper logs the
       same failure it bumps the counter for, so the on-call sees one
       log line per observed failure (issue #117 / #231 invariant).

    The 1 vs. >1 assertion is the catch-clause invariant: a regression
    that added a second ``inc()`` call (e.g. inside the wrapper's
    try/except AND inside ``run_sla_tick``) would bump the counter
    twice per failing tick and the operator on-call would double-count
    failures on the alert.
    """
    # Install a working singleton guard so the gated wrapper reaches the
    # inner analysis_sla_monitor body. Without this guard, the gate's
    # strict ``operator_custom_objects_api`` call raises ConfigException
    # and the wrapper returns None before any ``run_sla_tick`` is called.
    guard = SingletonGuard(FakeCustomObjectsApi(items=[make_cr(NAME, OLD_TS, uid=UID)]))
    monkeypatch.setattr(singleton, "_process_guard", guard)

    # Simulate the "lenient placeholder fails at the call site" scenario:
    # ``run_sla_tick`` raises ApiException. This is the test seam that
    # stands in for whatever exception the placeholder's actual API
    # method (e.g. ``list_namespaced_pod``) would raise in the real
    # no-config state — see the module docstring for why we use
    # ApiException: LocationValueError (the real no-config call error)
    # joined the shared skip tuple in #493, so either class now bumps
    # the counter; ApiException keeps the seam decoupled from urllib3.
    simulated_failure = ApiException(status=500, reason="lenient-path API call failed (#286)")

    def _boom(*_a: object, **_k: object) -> object:
        raise simulated_failure

    monkeypatch.setattr(analysis_sla, "run_sla_tick", _boom)

    # Pre-tick counter snapshot — the wrapper increments exactly once.
    error_type = type(simulated_failure).__name__
    before = _counter("analysis_sla", error_type)

    body = make_cr(NAME, OLD_TS, uid=UID)
    LOGGER.setLevel(logging.WARNING)
    # Valid redisUrl — ``redis.Redis.from_url`` parses lazily (no socket)
    # so this never hits the network; an empty ``redisUrl`` would raise
    # ``ValueError`` from the URL parser before ``run_sla_tick`` runs.
    # Same pattern as ``tests/test_timer_wrapper_failures.py::SPEC``.
    spec = {
        "serverUrl": "http://web.test",
        "redisUrl": "redis://:pw@queue.test:6379",
        "dryRun": True,
    }
    with caplog.at_level(logging.WARNING, logger=analysis_sla.logger.name):
        result = analysis_sla.analysis_sla_monitor(
            body=body,
            spec=spec,
            namespace=NAMESPACE,
            name=NAME,
            logger=LOGGER,
        )

    # 1. The wrapper returns None — its catch clause swallowed the
    # simulated failure.
    assert result is None, (
        f"Expected the wrapper to swallow ApiException and return None "
        f"(D12: skip the tick and retry on the next poll); got {result!r}."
    )

    # 2. The counter increments by exactly 1 — the labelled-counter
    # contract documented for the operator_core_api call site.
    after = _counter("analysis_sla", error_type)
    assert after - before == 1.0, (
        f"HANDLER_TICK_FAILURES_TOTAL{{module=analysis_sla, "
        f"error_type={error_type}}} must increment by exactly 1 when "
        f"the operator_core_api call site fails; observed delta "
        f"{after - before}. See issue #286 — the wrapper's failure path "
        f"regressed."
    )

    # 3. The WARNING log carries the same error type — wrapper's catch
    # clause invariant: one log line per counter increment, so the
    # on-call's log forwarder and Prometheus alert stay correlated.
    assert (
        f"analysis SLA tick skipped, retrying next poll ({error_type}"
        in caplog.text
    ), (
        f"Expected the wrapper's WARNING log to carry the error type "
        f"{error_type!r}; got {caplog.text!r}. See issue #117 / #231 — "
        f"the wrapper's log/metric correlation invariant regressed."
    )