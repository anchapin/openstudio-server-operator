"""Direct tests for the four OSCM @kopf.timer wrappers (issue #231).

The four timer entrypoints the operator actually exposes to kopf are
:func:`openstudio_operator.handlers.analysis_sla.analysis_sla_monitor`,
:func:`openstudio_operator.handlers.datapoint_watchdog.zombie_datapoint_watchdog`,
:func:`openstudio_operator.handlers.web_background_monitor.web_background_monitor`,
and :func:`openstudio_operator.handlers.worker_recycler.worker_recycler`.

Each of those wrappers is the function the singleton guard wraps and the
function kopf invokes on its interval. Today every per-handler test
exercises only the underlying ``run_*_tick`` function; the thin wrapper
that wires :func:`OperatorConfig.from_spec`, constructs
:class:`EventEmitter` (D11 gate), calls ``datetime.now(UTC)``, returns
early on empty ``spec.serverUrl``, and increments
``HANDLER_TICK_FAILURES_TOTAL.labels(module=<name>, error_type=<class>)``
on the canonical skip-tick tuple (see the ``_oscm_handlers`` constants
below — issue #473 unified the four former per-module tuples onto
``SKIP_TICK_EXCEPTIONS``) is **not** directly tested. Issue
#231 closes that gap.

Acceptance criterion (issue #231):

* importing each wrapper and invoking it with a mocked ``run_*_tick``
  that raises the wrapper's exact exception tuple, asserts that
  ``HANDLER_TICK_FAILURES_TOTAL.labels(module=<name>, error_type=<class>)``
  increments by exactly 1 and that the wrapper returns ``None`` without
  re-raising;
* the empty-serverUrl branch logs the idle message and does NOT increment
  the failure counter.

No new HTTP/Redis mocking libraries are added — the existing AGENTS.md
mock inventory (``responses`` for the OpenStudio REST API and
``fakeredis`` for the read-only Redis client) stays untouched. This
module uses only ``monkeypatch`` + ad-hoc stub objects, the same pattern
as :mod:`tests.test_singleton_guard`.
"""

from __future__ import annotations

import copy
import logging

import pytest
from kubernetes.client import ApiException
from kubernetes.config import ConfigException
from prometheus_client import REGISTRY
from urllib3.exceptions import LocationValueError

from _fakes import FakeSecretsCoreV1Api, encode_secret_value
from openstudio_operator import singleton
from openstudio_operator.client_factory import get_read_only_redis_client
from openstudio_operator.config import RedisSecretRef
from openstudio_operator.handlers import (
    analysis_sla,
    datapoint_watchdog,
    web_background_monitor,
    worker_recycler,
)
from openstudio_operator.handlers.analysis_sla import SlaTickResult
from openstudio_operator.openstudio_client import OpenStudioApiError
from openstudio_operator.redis_client import RedisClientError
from openstudio_operator.singleton import SingletonGuard
from openstudio_operator.status_store import StatusStoreError

NAMESPACE = "openstudio-server"
NAME = "oscm"
UID = "uid-oscm"

#: Minimal body shape — the wrapper accepts any Mapping; only the D05 gate
#: (``_gated``) cares about ``metadata.{name,namespace,uid}`` for the
#: ``_same_cr`` identity check. The wrapper itself only forwards ``body``
#: to ``EventEmitter`` and the gated wrapper's body lookup, neither of
#: which the run_*_tick-mocked test paths actually exercise.
BODY = {
    "metadata": {
        "name": NAME,
        "namespace": NAMESPACE,
        "uid": UID,
        "creationTimestamp": "2026-08-10T08:00:00Z",
    },
}

#: ``dryRun=True`` makes :class:`EventEmitter` a no-op so the wrappers do
#: not call ``kopf.event`` (which needs an active operator handler
#: context). The mocked ``run_*_tick`` raises before any Event would fire
#: anyway, but ``dryRun`` is the cleanest hermetic posture and matches the
#: production contract for "tick was scheduled, not a real-world stop".
SPEC = {
    "serverUrl": "http://web.test",
    "redisUrl": "redis://:pw@queue.test:6379",
    "dryRun": True,
}


class _FakeCustomObjectsApi:
    """List-only :class:`CustomObjectsApi` stand-in for the D05 gate."""

    def __init__(self, items: list[dict]) -> None:
        self.items = copy.deepcopy(items)

    def list_namespaced_custom_object(self, group, version, namespace, plural):
        return {"items": copy.deepcopy(self.items)}


def _build_guard(body: dict) -> SingletonGuard:
    """Build a :class:`SingletonGuard` whose CR list resolves ``body`` as active.

    Mirrors the ``_same_cr`` identity heuristic (uid-or-name) so
    ``SingletonGuard.is_active(body, namespace)`` returns ``True`` for the
    body the wrapper test will pass.
    """
    cr = {
        "apiVersion": "energy.nrel.gov/v1alpha1",
        "kind": "OpenStudioClusterManager",
        "metadata": copy.deepcopy(body["metadata"]),
        "spec": copy.deepcopy(SPEC),
    }
    return SingletonGuard(_FakeCustomObjectsApi([cr]))


def _counter(module: str, error_type: str) -> float:
    """Read the ``HANDLER_TICK_FAILURES_TOTAL`` labelled sample for a tuple.

    Per-observation read (not snapshot-delta): the test captures a
    before/after pair locally so a leaked increment from a sibling test
    does not poison the assertion. Process-wide singletons require
    per-call deltas — the wrapper increments exactly once per failing
    tick, no more.
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


def _raise(exc: BaseException):
    """Return a no-arg function that raises ``exc`` (the patched run_*_tick)."""

    def _boom(*_a: object, **_k: object) -> object:
        raise exc

    return _boom


@pytest.fixture(autouse=True)
def _isolate_process_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the D05 singleton guard's module-level cache between tests.

    The operator builds the guard once (on the first wrapper tick) and
    reuses it for the process lifetime; without a between-test reset a
    cache from a previous case would carry into the next and a body that
    does not match its CR list would be silently gated out (the wrapper
    would never run, masking the failure path). ``None`` is the documented
    "not yet built" sentinel — :func:`openstudio_operator.singleton._get_guard`
    constructs a real guard on the next read.
    """
    monkeypatch.setattr("openstudio_operator.singleton._process_guard", None)


@pytest.fixture
def _stub_operator_k8s_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub :func:`operator_custom_objects_api` in every handler module.

    Each wrapper constructs ``StatusStore(namespace, name,
    operator_custom_objects_api())`` BEFORE invoking ``run_*_tick``. In
    CI there is no in-cluster kubeconfig, so the real factory would try
    to load it and fail. The run_*_tick raises before :class:`StatusStore`
    ever touches its stored client, so any sentinel object works.
    """
    sentinel = object()
    for module in (analysis_sla, datapoint_watchdog, worker_recycler, web_background_monitor):
        monkeypatch.setattr(module, "operator_custom_objects_api", lambda: sentinel)


# --- per-wrapper exception tuples (issue #231) ---------------------------------
#
# Issue #473 replaced the four divergent per-wrapper ``except`` tuples
# with ONE canonical tuple — ``_oscm_handlers.SKIP_TICK_EXCEPTIONS``,
# the union of the historical per-module tuples — shared by all four
# wrappers via ``run_oscm_tick``. The parametrised sets below enumerate
# each wrapper's HISTORICAL tuple members (the union superset is pinned
# by ``tests/test_oscm_tick_runner.py``); every listed member must still
# be caught by the shared tuple. Parametrising over every member catches
# a regression that narrows the tuple (e.g. drops
# ``RedisClientError`` from analysis_sla on a careless refactor) at the
# CI gate. ``_oscm_handlers.SKIP_TICK_EXCEPTIONS`` is the source of
# truth; keep these constants a subset of it or this test will silently
# miss the dropped exception.

ANALYSIS_SLA_EXCEPTIONS: tuple[pytest.param, ...] = (
    pytest.param(OpenStudioApiError("api"), id="OpenStudioApiError"),
    pytest.param(StatusStoreError("store"), id="StatusStoreError"),
    pytest.param(ApiException(status=500, reason="boom"), id="ApiException"),
    pytest.param(RedisClientError("redis"), id="RedisClientError"),
)

DATAPOINT_WATCHDOG_EXCEPTIONS: tuple[pytest.param, ...] = (
    pytest.param(OpenStudioApiError("api"), id="OpenStudioApiError"),
    pytest.param(StatusStoreError("store"), id="StatusStoreError"),
)

WORKER_RECYCLER_EXCEPTIONS: tuple[pytest.param, ...] = (
    pytest.param(OpenStudioApiError("api"), id="OpenStudioApiError"),
    pytest.param(StatusStoreError("store"), id="StatusStoreError"),
    pytest.param(ApiException(status=500, reason="boom"), id="ApiException"),
)

WEB_BACKGROUND_EXCEPTIONS: tuple[pytest.param, ...] = (
    pytest.param(RedisClientError("redis"), id="RedisClientError"),
    pytest.param(StatusStoreError("store"), id="StatusStoreError"),
    pytest.param(ApiException(status=500, reason="boom"), id="ApiException"),
)


# --- analysis_sla_monitor — 4-tuple exception coverage -------------------------


@pytest.mark.parametrize("exc", ANALYSIS_SLA_EXCEPTIONS)
def test_analysis_sla_monitor_wrapper_counts_module_specific_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _stub_operator_k8s_client: None,
    exc: BaseException,
) -> None:
    """Issue #231: ``analysis_sla_monitor`` fires the counter on every tuple member.

    Since #473 the wrapper catches the shared
    ``_oscm_handlers.SKIP_TICK_EXCEPTIONS`` —
    ``(OpenStudioApiError, StatusStoreError, ApiException, RedisClientError)``,
    identical to analysis_sla's historical 4-tuple (the union's superset).
    A regression that drops one of those classes (the most likely silent
    edit is removing ``RedisClientError`` because the SLA monitor
    primarily talks REST) would re-raise on the dropped branch, which
    then propagates out of the wrapper as an uncaught kopf handler error.
    Parametrising over the entire set asserts the wrapper's catch still
    covers the documented members.
    """
    monkeypatch.setattr(analysis_sla, "run_sla_tick", _raise(exc))

    error_type = type(exc).__name__
    before = _counter("analysis_sla", error_type)
    with caplog.at_level(logging.WARNING, logger="openstudio_operator.handlers.analysis_sla"):
        result = analysis_sla.analysis_sla_monitor(
            body=BODY,
            spec=SPEC,
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
        )
    after = _counter("analysis_sla", error_type)

    assert result is None, (
        f"wrapper must swallow {error_type} and return None (D12: skip the "
        f"tick and retry on the next poll); got {result!r}"
    )
    assert after - before == 1.0, (
        f"HANDLER_TICK_FAILURES_TOTAL{{module=analysis_sla, error_type="
        f"{error_type}}} must increment by exactly 1; observed delta "
        f"{after - before}. See issue #231 — the wrapper's failure path "
        f"regressed."
    )
    # Issue #117 — the wrapper logs the same failure at WARNING so the
    # operator log forwarder surfaces it. Pinning the log line keeps the
    # metric + log correlation intact for the on-call.
    assert f"analysis SLA tick skipped, retrying next poll ({error_type}" in caplog.text, (
        f"wrapper must log the WARNING describing the skipped tick with "
        f"the error type; got {caplog.text!r}"
    )


def test_analysis_sla_monitor_wrapper_empty_server_url_logs_idle_and_no_failure_increment(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _stub_operator_k8s_client: None,
) -> None:
    """Issue #231: empty ``serverUrl`` branch logs idle and does NOT touch the counter.

    The wrapper's first check is ``if not config.server_url: log + return``,
    so ``run_*_tick`` is never called and the failure counter must stay
    put. This is the regression-fence: a future edit that delays the
    early-return past the run_*_tick call (e.g. moves the config check
    below the try/except) would make empty-serverUrl ticks increment the
    counter, which is wrong — an incomplete CR is not a tick failure.
    """
    empty_spec = {**SPEC, "serverUrl": ""}

    before = _counter("analysis_sla", "OpenStudioApiError")
    with caplog.at_level(logging.WARNING, logger="openstudio_operator.handlers.analysis_sla"):
        result = analysis_sla.analysis_sla_monitor(
            body=BODY,
            spec=empty_spec,
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
        )
    after = _counter("analysis_sla", "OpenStudioApiError")

    assert result is None
    assert after == before, (
        f"Empty serverUrl branch must NOT increment "
        f"HANDLER_TICK_FAILURES_TOTAL; observed delta {after - before}. "
        f"An incomplete CR is not a tick failure."
    )
    assert "analysis SLA monitor idle" in caplog.text, (
        f"Empty serverUrl branch must log the idle message; got {caplog.text!r}"
    )


# --- zombie_datapoint_watchdog — 2-tuple exception coverage -------------------


@pytest.mark.parametrize("exc", DATAPOINT_WATCHDOG_EXCEPTIONS)
def test_zombie_datapoint_watchdog_wrapper_counts_module_specific_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _stub_operator_k8s_client: None,
    exc: BaseException,
) -> None:
    """Issue #231: ``zombie_datapoint_watchdog`` fires the counter on its historical tuple members.

    Pre-#473 the watchdog declared ``except (OpenStudioApiError,
    StatusStoreError)``; #473 moved all four wrappers onto the shared
    ``_oscm_handlers.SKIP_TICK_EXCEPTIONS`` union, so those members (and
    the rest of the union) are caught. Parametrising over the historical
    set keeps the CI gate that a tuple-narrowing regression re-raises on
    a dropped branch, which would propagate out of the wrapper as an
    uncaught kopf handler error.
    """
    monkeypatch.setattr(datapoint_watchdog, "run_watchdog_tick", _raise(exc))

    error_type = type(exc).__name__
    before = _counter("datapoint_watchdog", error_type)
    with caplog.at_level(
        logging.WARNING, logger="openstudio_operator.handlers.datapoint_watchdog"
    ):
        result = datapoint_watchdog.zombie_datapoint_watchdog(
            body=BODY,
            spec=SPEC,
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
        )
    after = _counter("datapoint_watchdog", error_type)

    assert result is None
    assert after - before == 1.0, (
        f"HANDLER_TICK_FAILURES_TOTAL{{module=datapoint_watchdog, error_type="
        f"{error_type}}} must increment by exactly 1; observed delta "
        f"{after - before}."
    )
    assert (
        f"datapoint watchdog tick skipped, retrying next poll ({error_type}"
        in caplog.text
    ), (
        f"wrapper must log the WARNING describing the skipped tick with "
        f"the error type; got {caplog.text!r}"
    )


def test_zombie_datapoint_watchdog_wrapper_empty_server_url_logs_idle_and_no_failure_increment(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _stub_operator_k8s_client: None,
) -> None:
    """Issue #231: empty ``serverUrl`` branch on the watchdog logs idle, no counter bump."""
    empty_spec = {**SPEC, "serverUrl": ""}

    before = _counter("datapoint_watchdog", "OpenStudioApiError")
    with caplog.at_level(
        logging.WARNING, logger="openstudio_operator.handlers.datapoint_watchdog"
    ):
        result = datapoint_watchdog.zombie_datapoint_watchdog(
            body=BODY,
            spec=empty_spec,
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
        )
    after = _counter("datapoint_watchdog", "OpenStudioApiError")

    assert result is None
    assert after == before
    assert "datapoint watchdog idle" in caplog.text


# --- worker_recycler — 3-tuple exception coverage ------------------------------


@pytest.mark.parametrize("exc", WORKER_RECYCLER_EXCEPTIONS)
def test_worker_recycler_wrapper_counts_module_specific_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _stub_operator_k8s_client: None,
    exc: BaseException,
) -> None:
    """Issue #231: ``worker_recycler`` fires the counter on its historical tuple members.

    Pre-#473 the recycler declared ``except (OpenStudioApiError,
    StatusStoreError, ApiException)`` — K8s+REST, no Redis; #473 moved
    all four wrappers onto the shared ``_oscm_handlers.SKIP_TICK_EXCEPTIONS``
    union, so those members are still caught.
    """
    monkeypatch.setattr(worker_recycler, "run_recycler_tick", _raise(exc))

    error_type = type(exc).__name__
    before = _counter("worker_recycler", error_type)
    with caplog.at_level(logging.WARNING, logger="openstudio_operator.handlers.worker_recycler"):
        result = worker_recycler.worker_recycler(
            body=BODY,
            spec=SPEC,
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
        )
    after = _counter("worker_recycler", error_type)

    assert result is None
    assert after - before == 1.0, (
        f"HANDLER_TICK_FAILURES_TOTAL{{module=worker_recycler, error_type="
        f"{error_type}}} must increment by exactly 1; observed delta "
        f"{after - before}."
    )
    assert (
        f"worker recycler tick skipped, retrying next poll ({error_type}" in caplog.text
    ), (
        f"wrapper must log the WARNING describing the skipped tick with "
        f"the error type; got {caplog.text!r}"
    )


def test_worker_recycler_wrapper_empty_server_url_logs_idle_and_no_failure_increment(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _stub_operator_k8s_client: None,
) -> None:
    """Issue #231: empty ``serverUrl`` branch on the recycler logs idle, no counter bump."""
    empty_spec = {**SPEC, "serverUrl": ""}

    before = _counter("worker_recycler", "OpenStudioApiError")
    with caplog.at_level(logging.WARNING, logger="openstudio_operator.handlers.worker_recycler"):
        result = worker_recycler.worker_recycler(
            body=BODY,
            spec=empty_spec,
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
        )
    after = _counter("worker_recycler", "OpenStudioApiError")

    assert result is None
    assert after == before
    assert "worker recycler idle" in caplog.text


# --- web_background_monitor — 3-tuple exception coverage ----------------------


@pytest.mark.parametrize("exc", WEB_BACKGROUND_EXCEPTIONS)
def test_web_background_monitor_wrapper_counts_module_specific_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _stub_operator_k8s_client: None,
    exc: BaseException,
) -> None:
    """Issue #231: ``web_background_monitor`` fires the counter on its historical tuple members.

    Pre-#473 the web_background monitor was the only wrapper that did
    **not** catch :class:`OpenStudioApiError` — it never talks to the
    OpenStudio REST surface, only Redis and the K8s API; its tuple was
    ``(RedisClientError, StatusStoreError, ApiException)``. #473 moved
    all four wrappers onto the shared
    ``_oscm_handlers.SKIP_TICK_EXCEPTIONS`` union, so those members are
    still caught.
    """
    monkeypatch.setattr(web_background_monitor, "run_stall_tick", _raise(exc))

    error_type = type(exc).__name__
    before = _counter("web_background_monitor", error_type)
    with caplog.at_level(
        logging.WARNING, logger="openstudio_operator.handlers.web_background_monitor"
    ):
        result = web_background_monitor.web_background_monitor(
            body=BODY,
            spec=SPEC,
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
        )
    after = _counter("web_background_monitor", error_type)

    assert result is None
    assert after - before == 1.0, (
        f"HANDLER_TICK_FAILURES_TOTAL{{module=web_background_monitor, "
        f"error_type={error_type}}} must increment by exactly 1; observed "
        f"delta {after - before}."
    )
    assert (
        f"web_background monitor tick skipped, retrying next poll ({error_type}"
        in caplog.text
    ), (
        f"wrapper must log the WARNING describing the skipped tick with "
        f"the error type; got {caplog.text!r}"
    )


def test_web_background_monitor_wrapper_empty_server_url_logs_idle_and_no_failure_increment(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _stub_operator_k8s_client: None,
) -> None:
    """Issue #231: empty ``serverUrl`` branch on web_background logs idle, no counter bump.

    The web_background monitor senses Redis + K8s only and never the
    OpenStudio REST surface, so ``spec.serverUrl`` is functionally unused.
    The handler still observes the shared "incomplete CR → idle this
    tick" posture every sibling handler takes — the test pins that
    consistency.
    """
    empty_spec = {**SPEC, "serverUrl": ""}

    before = _counter("web_background_monitor", "RedisClientError")
    with caplog.at_level(
        logging.WARNING, logger="openstudio_operator.handlers.web_background_monitor"
    ):
        result = web_background_monitor.web_background_monitor(
            body=BODY,
            spec=empty_spec,
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
        )
    after = _counter("web_background_monitor", "RedisClientError")

    assert result is None
    assert after == before
    assert "web_background monitor idle" in caplog.text


# --- Issue #493 — wiring/construction failure through a real handler wire -----


def test_analysis_sla_monitor_wrapper_counts_client_construction_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _stub_operator_k8s_client: None,
) -> None:
    """Issue #493: a client-construction failure inside ``wire`` skips the tick.

    Representative handler-level check of the #493 semantics (the shared
    wrapper is exercised per-exception in ``tests/test_oscm_tick_runner.py``;
    this test proves the wiring seam reaches it from a real ``@kopf.timer``
    entrypoint). ``analysis_sla_monitor``'s ``wire`` closure builds its REST
    client, ``operator_core_api()``, and Redis client each tick; the
    kubeconfig-load failure class (``ConfigException`` — what the factory
    propagates when neither the in-cluster config nor ``~/.kube/config``
    loads) previously escaped ``run_oscm_tick`` as an uncaught kopf handler
    error. Since #493 the ``wire`` call runs inside the guarded region, so
    the same failure yields the standard D12 posture: warning log,
    ``HANDLER_TICK_FAILURES_TOTAL{module=analysis_sla,
    error_type=ConfigException}`` +1, clean ``None`` return.
    """
    monkeypatch.setattr(
        analysis_sla,
        "operator_core_api",
        _raise(ConfigException("no kubeconfig anywhere (#493)")),
    )

    before = _counter("analysis_sla", "ConfigException")
    with caplog.at_level(logging.WARNING, logger="openstudio_operator.handlers.analysis_sla"):
        result = analysis_sla.analysis_sla_monitor(
            body=BODY,
            spec=SPEC,
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
        )
    after = _counter("analysis_sla", "ConfigException")

    assert result is None, (
        "wrapper must skip the tick on a wiring ConfigException (D12: retry "
        "next poll); the fix lands outside the tick loop"
    )
    assert after - before == 1.0, (
        "HANDLER_TICK_FAILURES_TOTAL{module=analysis_sla, "
        "error_type=ConfigException} must increment by exactly 1 on a client-"
        f"construction failure; observed delta {after - before}."
    )
    assert (
        "analysis SLA tick skipped, retrying next poll (ConfigException" in caplog.text
    ), (
        f"wrapper must log the WARNING describing the skipped wiring failure; "
        f"got {caplog.text!r}"
    )


# --- Issue #249 — per-wrapper exception-tuple negative coverage ----------------
#
# The four parametrised cases above assert that every class INSIDE the
# wrapper's declared tuple increments HANDLER_TICK_FAILURES_TOTAL by
# exactly 1. The reverse direction is just as important: a class that is
# NOT in the wrapper's tuple must NOT increment the counter and must
# propagate out (the kopf runtime then handles it as an uncaught handler
# error — the operator's design is fail-closed, so a stray
# ``RuntimeError`` in a handler becomes a loud watcher event rather than
# silently skipped ticks). The negative cases here cover that surface:
#
#   * each wrapper is invoked with run_*_tick monkeypatched to raise a
#     class NOT in the shared tuple (``ValueError`` — chosen because it
#     is the stdlib's generic "something went wrong" exception and is
#     guaranteed not to subclass any tuple member);
#   * the wrapper's HANDLER_TICK_FAILURES_TOTAL must NOT increment on
#     this branch;
#   * the exception must propagate out (re-raise through the wrapper)
#     because the wrapper's ``except`` tuple does not catch it.
#
# Note the #493 subclass subtlety: ``urllib3.exceptions.LocationValueError``
# IS a :class:`ValueError` subclass AND a tuple member — but exception
# matching is subclass-directed, so ``except LocationValueError`` does not
# catch the bare PARENT ``ValueError``; the negative cases below still
# propagate exactly as they did pre-#493.
#
# Without these tests a future patch that adds ``ValueError`` (or any
# other class) to the tuple would silently widen the catch and
# the ``error_type`` label cardinality would drift undetected — the
# on-call would only notice once ``rate(handler_tick_failures_total)``
# stops firing on the genuine kopf-handler-error signal they expect to
# alert on.
#
# The four modules use distinct sets so the "not in tuple" assertion
# truly tests a class that is NOT in the wrapper's declared set:

#: ``ValueError`` is NOT a subclass of any tuple member and not itself
#: in the shared tuple — a clean "out of tuple" sentinel for the
#: negative cases below. (Its subclass ``LocationValueError`` joined the
#: tuple in #493; the parent stays out.)
_OUT_OF_TUPLE_EXCEPTION = ValueError("out of tuple — must propagate, not increment")


def test_analysis_sla_monitor_does_not_count_out_of_tuple_exception(
    monkeypatch: pytest.MonkeyPatch,
    _stub_operator_k8s_client: None,
) -> None:
    """``ValueError`` (outside the shared skip tuple) must NOT increment the counter.

    Since #473 analysis_sla_monitor catches the shared
    ``_oscm_handlers.SKIP_TICK_EXCEPTIONS`` —
    ``(OpenStudioApiError, StatusStoreError, ApiException, RedisClientError)``.
    A regression that adds ``ValueError`` to that tuple (e.g. a
    well-meaning "let's catch more things") would silently widen the
    counter's trigger set. The negative case here pins the tuple's exact
    membership: out-of-tuple classes propagate out, the counter stays
    put.
    """
    monkeypatch.setattr(analysis_sla, "run_sla_tick", _raise(_OUT_OF_TUPLE_EXCEPTION))

    before = _counter("analysis_sla", "ValueError")
    with pytest.raises(ValueError, match="out of tuple"):
        analysis_sla.analysis_sla_monitor(
            body=BODY,
            spec=SPEC,
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
        )
    after = _counter("analysis_sla", "ValueError")
    assert after == before, (
        f"HANDLER_TICK_FAILURES_TOTAL{{module=analysis_sla, error_type=ValueError}} "
        f"must NOT increment for an out-of-tuple class; observed delta {after - before}. "
        f"See issue #249 — the wrapper's failure path silently widened."
    )


def test_zombie_datapoint_watchdog_does_not_count_out_of_tuple_exception(
    monkeypatch: pytest.MonkeyPatch,
    _stub_operator_k8s_client: None,
) -> None:
    """``ValueError`` (outside the shared skip tuple) must NOT increment the counter.

    Since #473 the watchdog catches the shared
    ``_oscm_handlers.SKIP_TICK_EXCEPTIONS`` union (its historical 2-tuple
    ``(OpenStudioApiError, StatusStoreError)`` was the narrowest of the
    four — see ``test_raw_api_exception_from_status_patch_is_counted_by_
    shared_skip_tuple`` in ``tests/test_datapoint_watchdog.py`` for the
    widening's behavioral pin). ``ValueError`` is not in the union: it
    must propagate, not silently start counting.
    """
    monkeypatch.setattr(datapoint_watchdog, "run_watchdog_tick", _raise(_OUT_OF_TUPLE_EXCEPTION))

    before = _counter("datapoint_watchdog", "ValueError")
    with pytest.raises(ValueError, match="out of tuple"):
        datapoint_watchdog.zombie_datapoint_watchdog(
            body=BODY,
            spec=SPEC,
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
        )
    after = _counter("datapoint_watchdog", "ValueError")
    assert after == before, (
        f"HANDLER_TICK_FAILURES_TOTAL{{module=datapoint_watchdog, error_type=ValueError}} "
        f"must NOT increment for an out-of-tuple class; observed delta {after - before}."
    )


def test_worker_recycler_does_not_count_out_of_tuple_exception(
    monkeypatch: pytest.MonkeyPatch,
    _stub_operator_k8s_client: None,
) -> None:
    """``ValueError`` (outside the shared skip tuple) must NOT increment the counter.

    Since #473 the recycler catches the shared
    ``_oscm_handlers.SKIP_TICK_EXCEPTIONS`` union (its historical tuple
    was ``(OpenStudioApiError, StatusStoreError, ApiException)`` —
    K8s+REST, no Redis). ``ValueError`` is not in the union: it must
    propagate, not silently start counting — otherwise the
    ``error_type`` label cardinality would drift undetected.
    """
    monkeypatch.setattr(worker_recycler, "run_recycler_tick", _raise(_OUT_OF_TUPLE_EXCEPTION))

    before = _counter("worker_recycler", "ValueError")
    with pytest.raises(ValueError, match="out of tuple"):
        worker_recycler.worker_recycler(
            body=BODY,
            spec=SPEC,
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
        )
    after = _counter("worker_recycler", "ValueError")
    assert after == before, (
        f"HANDLER_TICK_FAILURES_TOTAL{{module=worker_recycler, error_type=ValueError}} "
        f"must NOT increment for an out-of-tuple class; observed delta {after - before}."
    )


def test_web_background_monitor_does_not_count_out_of_tuple_exception(
    monkeypatch: pytest.MonkeyPatch,
    _stub_operator_k8s_client: None,
) -> None:
    """``ValueError`` (outside the shared skip tuple) must NOT increment the counter.

    Since #473 the web_background monitor catches the shared
    ``_oscm_handlers.SKIP_TICK_EXCEPTIONS`` union (its historical tuple
    ``(RedisClientError, StatusStoreError, ApiException)`` did NOT
    include :class:`OpenStudioApiError` — it never talks to the
    OpenStudio REST surface). ``ValueError`` is not in the union: it
    must propagate, not silently start counting — otherwise the
    ``error_type`` label cardinality would drift undetected.
    """
    monkeypatch.setattr(web_background_monitor, "run_stall_tick", _raise(_OUT_OF_TUPLE_EXCEPTION))

    before = _counter("web_background_monitor", "ValueError")
    with pytest.raises(ValueError, match="out of tuple"):
        web_background_monitor.web_background_monitor(
            body=BODY,
            spec=SPEC,
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
        )
    after = _counter("web_background_monitor", "ValueError")
    assert after == before, (
        f"HANDLER_TICK_FAILURES_TOTAL{{module=web_background_monitor, error_type=ValueError}} "
        f"must NOT increment for an out-of-tuple class; observed delta {after - before}."
    )


def test_each_wrapper_out_of_tuple_class_is_distinct_from_in_tuple_class() -> None:
    """Sanity fence: ``ValueError`` is NOT a subclass of any in-tuple class.

    The negative-case tests above rely on ``ValueError`` being
    genuinely out-of-tuple for every wrapper. If a future refactor
    made :class:`ValueError` a subclass of any in-tuple class (very
    unlikely, but the bug is silent), the negative tests would catch
    the in-tuple exceptions and pass with a counter increment — the
    regression would never surface. This sanity test pins the
    subclass relationship explicitly so any drift here fails loud.
    """
    for in_tuple_cls in (
        OpenStudioApiError,
        StatusStoreError,
        ApiException,
        RedisClientError,
        ConfigException,
        LocationValueError,
    ):
        assert not issubclass(ValueError, in_tuple_cls), (
            f"ValueError unexpectedly subclasses {in_tuple_cls.__name__}; "
            f"the issue #249 negative-case tests would silently start "
            f"catching in-tuple exceptions."
        )


# --- Issue #567 — secretRef-only CR through the Redis wire closures -------------
#
# The #463 factory gained ``(redis_url, secret_ref, namespace)`` but no
# production call site passed ``secret_ref``: a secretRef-only CR
# (``spec.redisUrl`` empty — AGENTS.md's "preferred production shape")
# passed the singleton guard and stamped ``redis_url_set=1`` on the
# posture gauge, then ``ReadOnlyRedisClient("")`` raised ``ValueError``
# at wire time — deliberately NOT in ``SKIP_TICK_EXCEPTIONS``, so the
# stall monitor and the SLA monitor crashed as uncaught kopf handler
# errors on EVERY tick. #567 wires ``secret_ref`` + the CR namespace
# through both wire closures; these tests drive a secretRef-only spec
# through the REAL ``run_oscm_tick`` + ``wire`` path (only the per-module
# ``run_*_tick`` is stubbed, to capture the resolved client) with a
# faked Secret read, asserting the resolved client is used instead of a
# raised ``ValueError``.

SECRET_URL = "redis://:rotated-pw@queue.openstudio-server.svc.cluster.local:6379"
SECRET_REF_NAME = "openstudio-redis"
SECRET_REF_KEY = "redis-url"
EXPECTED_SECRET_REF = RedisSecretRef(name=SECRET_REF_NAME, key=SECRET_REF_KEY)


def _secret_ref_spec(*, redis_url: str = "") -> dict:
    """A spec with ``spec.redisCredentials.secretRef`` set (issue #463 shape)."""
    return {
        "serverUrl": "http://web.test",
        "redisUrl": redis_url,
        "redisCredentials": {"secretRef": {"name": SECRET_REF_NAME, "key": SECRET_REF_KEY}},
        "dryRun": True,
    }


@pytest.fixture
def _fake_secret_reader(monkeypatch: pytest.MonkeyPatch) -> FakeSecretsCoreV1Api:
    """Serve the #463 Secret read from ``singleton._operator_core_api``.

    The REAL ``get_read_only_redis_client`` runs (no factory stub): the
    lru_cache is cleared on setup/teardown so the resolution — and the
    ``.calls`` assertion — is fresh for each case.
    """
    get_read_only_redis_client.cache_clear()
    fake = FakeSecretsCoreV1Api({SECRET_REF_KEY: encode_secret_value(SECRET_URL)})
    monkeypatch.setattr(singleton, "_operator_core_api", fake)
    yield fake
    get_read_only_redis_client.cache_clear()


@pytest.mark.parametrize(
    "inline_redis_url",
    ["", "redis://:stale-inline-pw@queue.test:6379"],
    ids=["secret-ref-only", "secret-ref-wins-over-inline"],
)
def test_analysis_sla_monitor_wrapper_resolves_redis_client_from_secret_ref(
    monkeypatch: pytest.MonkeyPatch,
    _stub_operator_k8s_client: None,
    _fake_secret_reader: FakeSecretsCoreV1Api,
    inline_redis_url: str,
) -> None:
    """Issue #567: the SLA wire closure hands the tick a Secret-resolved client.

    Pre-#567 the secretRef-only case raised ``ValueError`` out of the
    wrapper (uncaught kopf handler error, every tick); the test completing
    IS the no-crash assertion. The captured client must be built from the
    Secret-held URL (preferred over any inline ``spec.redisUrl`` — the
    #463 acceptance), and the Secret read must target the referenced
    Secret in the CR's namespace.
    """
    # pod_api: any sentinel — run_sla_tick is stubbed below.
    monkeypatch.setattr(analysis_sla, "operator_core_api", lambda: object())
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> SlaTickResult:
        captured["redis_client"] = kwargs["redis_client"]
        return SlaTickResult(soft_stopped=[], escalated=[])

    monkeypatch.setattr(analysis_sla, "run_sla_tick", _capture)

    result = analysis_sla.analysis_sla_monitor(
        body=BODY,
        spec=_secret_ref_spec(redis_url=inline_redis_url),
        namespace=NAMESPACE,
        name=NAME,
        logger=logging.getLogger("test"),
    )

    # The timer wrapper returns None by design (it logs rather than
    # returns); the tick RAN iff the capture fired.
    assert result is None
    assert "redis_client" in captured, (
        "the tick must RUN (not skip) — the wire resolved a working client. "
        "See issue #567."
    )
    client = captured["redis_client"]
    assert client._redis_url == SECRET_URL, (
        f"run_sla_tick must receive the Secret-resolved client; got URL "
        f"{client._redis_url!r}. See issue #567 — the wire closure must "
        f"pass secret_ref + namespace to get_read_only_redis_client."
    )
    assert _fake_secret_reader.calls == [(SECRET_REF_NAME, NAMESPACE)], (
        "the Secret read must target exactly the referenced Secret in "
        "the CR's namespace (issue #567)"
    )


@pytest.mark.parametrize(
    "inline_redis_url",
    ["", "redis://:stale-inline-pw@queue.test:6379"],
    ids=["secret-ref-only", "secret-ref-wins-over-inline"],
)
def test_web_background_monitor_wrapper_resolves_redis_client_from_secret_ref(
    monkeypatch: pytest.MonkeyPatch,
    _stub_operator_k8s_client: None,
    _fake_secret_reader: FakeSecretsCoreV1Api,
    inline_redis_url: str,
) -> None:
    """Issue #567: the stall-monitor wire closure hands the tick a Secret-resolved client.

    Same regression shape as the SLA case: pre-#567 the secretRef-only
    spec raised ``ValueError`` at wire time on every tick. The #490
    key-layout rider is no-op'd here (it has its own direct secretRef
    coverage in ``tests/test_redis_client.py``) so this test stays
    hermetic — without the patch the rider would run the REAL
    ``validate_key_layout()`` against the resolved host.
    """
    # apps/pods sentinels — run_stall_tick is stubbed below.
    monkeypatch.setattr(web_background_monitor, "operator_apps_api", lambda: object())
    monkeypatch.setattr(web_background_monitor, "operator_core_api", lambda: object())
    monkeypatch.setattr(
        web_background_monitor,
        "_maybe_revalidate_redis_key_layout",
        lambda *a, **_k: None,
    )
    captured: dict[str, object] = {}

    def _capture(*args: object, **_kwargs: object) -> bool:
        captured["redis_client"] = args[0]  # first positional arg of run_stall_tick
        return False

    monkeypatch.setattr(web_background_monitor, "run_stall_tick", _capture)

    result = web_background_monitor.web_background_monitor(
        body=BODY,
        spec=_secret_ref_spec(redis_url=inline_redis_url),
        namespace=NAMESPACE,
        name=NAME,
        logger=logging.getLogger("test"),
    )

    assert result is None, "wrapper returns None on a non-firing (False) stall tick"
    assert "redis_client" in captured, (
        "the tick must RUN (not skip) — the wire resolved a working client. "
        "See issue #567."
    )
    client = captured["redis_client"]
    assert client._redis_url == SECRET_URL, (
        f"run_stall_tick must receive the Secret-resolved client; got URL "
        f"{client._redis_url!r}. See issue #567."
    )
    assert _fake_secret_reader.calls == [(SECRET_REF_NAME, NAMESPACE)]


def test_web_background_monitor_wrapper_secret_ref_failure_counts_as_skip(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _stub_operator_k8s_client: None,
) -> None:
    """Issue #567 D12 posture: an unresolvable secretRef SKIPS the tick, not crashes.

    A missing Secret raises ``RedisCredentialResolutionError`` (a
    ``RedisClientError`` subclass — an explicit ``SKIP_TICK_EXCEPTIONS``
    member), so a secretRef-only CR whose Secret is not (yet) created
    gets the counted skip-tick + retry-next-poll treatment — the Secret
    created after the first attempt is picked up on the very next tick
    (the factory never memoizes raised exceptions).
    """
    get_read_only_redis_client.cache_clear()
    monkeypatch.setattr(
        singleton,
        "_operator_core_api",
        FakeSecretsCoreV1Api(exc=ApiException(status=404, reason="Not Found")),
    )
    monkeypatch.setattr(web_background_monitor, "operator_apps_api", lambda: object())
    monkeypatch.setattr(web_background_monitor, "operator_core_api", lambda: object())
    monkeypatch.setattr(
        web_background_monitor,
        "_maybe_revalidate_redis_key_layout",
        lambda *a, **_k: None,
    )
    stall_calls: list[object] = []
    monkeypatch.setattr(
        web_background_monitor,
        "run_stall_tick",
        lambda *a, **k: stall_calls.append(a) or False,
    )

    before = _counter("web_background_monitor", "RedisCredentialResolutionError")
    with caplog.at_level(
        logging.WARNING, logger="openstudio_operator.handlers.web_background_monitor"
    ):
        result = web_background_monitor.web_background_monitor(
            body=BODY,
            spec=_secret_ref_spec(),
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
        )
    after = _counter("web_background_monitor", "RedisCredentialResolutionError")

    assert result is None
    assert not stall_calls, "the tick must not run when the Secret cannot be read"
    assert after - before == 1.0, (
        "HANDLER_TICK_FAILURES_TOTAL{module=web_background_monitor, "
        "error_type=RedisCredentialResolutionError} must increment by "
        f"exactly 1; observed delta {after - before}."
    )
    assert (
        "web_background monitor tick skipped, retrying next poll "
        "(RedisCredentialResolutionError" in caplog.text
    )
    get_read_only_redis_client.cache_clear()
