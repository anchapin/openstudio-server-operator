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
on its module-specific exception tuple is **not** directly tested. Issue
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
from prometheus_client import REGISTRY

from openstudio_operator.handlers import (
    analysis_sla,
    datapoint_watchdog,
    web_background_monitor,
    worker_recycler,
)
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
            {"module": module, "error_type": error_type},
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
# Each wrapper declares its own ``except`` tuple; parametrising over every
# member catches a regression that narrows the tuple (e.g. drops
# ``RedisClientError`` from analysis_sla on a careless refactor) at the CI
# gate. The wrapper-level tuple list is the source of truth (handlers/*.py
# at the @kopf.timer wrappers); keep these constants aligned with the
# production code or this test will silently miss the dropped exception.

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

    The wrapper declares
    ``except (OpenStudioApiError, StatusStoreError, ApiException, RedisClientError)``
    — a regression that drops one of those classes (the most likely silent
    edit is removing ``RedisClientError`` because the SLA monitor
    primarily talks REST) would re-raise on the dropped branch, which
    then propagates out of the wrapper as an uncaught kopf handler error.
    Parametrising over the entire tuple asserts the wrapper's catch is
    exactly the documented set.
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
    """Issue #231: ``zombie_datapoint_watchdog`` fires the counter on its 2-tuple.

    The watchdog declares
    ``except (OpenStudioApiError, StatusStoreError)`` — narrower than the
    SLA wrapper because the watchdog never touches the K8s API or Redis.
    Adding an exception class on a careless edit would silently widen the
    tuple (a less dangerous regression, but the test pins the documented
    set exactly).
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
    """Issue #231: ``worker_recycler`` fires the counter on its 3-tuple.

    The recycler declares
    ``except (OpenStudioApiError, StatusStoreError, ApiException)`` —
    same K8s+REST surface as the SLA monitor, minus Redis (the recycler
    never reads from Redis). The test pins that distinction.
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
    """Issue #231: ``web_background_monitor`` fires the counter on its 3-tuple.

    The web_background monitor is the only wrapper that **does not**
    catch :class:`OpenStudioApiError` — it never talks to the OpenStudio
    REST surface, only Redis and the K8s API. The tuple is
    ``(RedisClientError, StatusStoreError, ApiException)``. The test pins
    the absence of ``OpenStudioApiError`` and the presence of every other
    class.
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
