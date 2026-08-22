"""Focused test for the shared OSCM tick-runner (issue #473).

``run_oscm_tick`` in :mod:`openstudio_operator._oscm_handlers` owns the
wrapper wiring the four timer handlers used to duplicate: config parse,
the empty-``serverUrl`` idle branch, store/emitter construction, the
canonical skip-tick exception tuple, the ``HANDLER_TICK_FAILURES_TOTAL``
increment, and the single per-handler skip-tick warning. Since #469 it
also pins the scheduler heartbeat: ``HANDLER_LAST_TICK_TIMESTAMP`` is
stamped at the end of EVERY invocation (success, idle, caught failure).
This file pins that ownership in one place; the per-wrapper behavior
(each handler delegating through the runner) stays covered by the —
unmodified — ``tests/test_timer_wrapper_failures.py`` and the
per-handler suites.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import pytest
from kubernetes.client import ApiException
from kubernetes.config import ConfigException
from prometheus_client import REGISTRY
from urllib3.exceptions import LocationValueError

from openstudio_operator._oscm_handlers import SKIP_TICK_EXCEPTIONS, run_oscm_tick
from openstudio_operator.config import OperatorConfigError
from openstudio_operator.openstudio_client import OpenStudioApiError
from openstudio_operator.redis_client import RedisClientError
from openstudio_operator.status_store import StatusStoreError

NAMESPACE = "openstudio-server"
NAME = "oscm"

SPEC = {
    "serverUrl": "http://web.test",
    "redisUrl": "redis://:pw@queue.test:6379",
    "dryRun": True,
}

IDLE_SPEC = {
    "serverUrl": "",
    "redisUrl": "redis://:pw@queue.test:6379",
    "dryRun": True,
}


def _counter(error_type: str) -> float:
    """Read the HANDLER_TICK_FAILURES_TOTAL sample for the probe module label."""
    return (
        REGISTRY.get_sample_value(
            "openstudio_operator_handler_tick_failures_total",
            {
                "namespace": NAMESPACE,
                "name": NAME,
                "module": "tick_runner_probe",
                "error_type": error_type,
            },
        )
        or 0.0
    )


def _heartbeat() -> float | None:
    """Read the HANDLER_LAST_TICK_TIMESTAMP sample for the probe module label."""
    return REGISTRY.get_sample_value(
        "openstudio_operator_handler_last_tick_timestamp",
        {"module": "tick_runner_probe"},
    )


def _run(
    tick,
    *,
    spec: dict = SPEC,
    logger: logging.Logger | None = None,
    wire=None,
):
    """Invoke the runner with the probe module label and no-op wiring.

    ``wire`` defaults to a no-op closure; the #493 wiring-failure tests
    pass a raising one.
    """
    return run_oscm_tick(
        spec=spec,
        body={"metadata": {"name": NAME, "namespace": NAMESPACE}},
        namespace=NAMESPACE,
        name=NAME,
        logger=logger or logging.getLogger("test"),
        module="tick_runner_probe",
        tick_label="tick runner probe",
        idle_label="tick runner probe",
        custom_objects_api=lambda: object(),
        wire=wire or (lambda config: object()),
        tick=tick,
    )


def test_run_oscm_tick_owns_failure_counter_and_skip_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #473: the tick-runner is the ONE site that counts + logs a skipped tick.

    A tick closure raising an in-tuple exception must (a) increment
    ``HANDLER_TICK_FAILURES_TOTAL`` with all four labels exactly once,
    (b) log the single skip-tick warning carrying the handler label and
    the error type, and (c) return ``None`` (D12: retry next poll). The
    canonical tuple is pinned as the UNION of the four historical
    per-wrapper tuples, so no handler silently lost a catch in the
    unification — evaluated at RUNTIME: issue #475 made the
    ``OperatorConfigError`` membership explicit because it used to ride
    into the historical wrappers' ``except RedisClientError`` catches via
    subclassing.
    """
    assert SKIP_TICK_EXCEPTIONS == (
        OpenStudioApiError,
        StatusStoreError,
        ApiException,
        RedisClientError,
        OperatorConfigError,
        ConfigException,
        LocationValueError,
    ), (
        "the canonical tuple must stay the runtime-effective union of the four "
        "historical wrapper tuples (OperatorConfigError explicitly, per #475) "
        "plus the #493 wiring/construction-failure members (ConfigException, "
        "LocationValueError)"
    )

    def wire(config: object) -> object:
        return object()  # client wiring is per-module; the runner only forwards it

    def tick(*, config, store, emit, deps, now: datetime) -> object:
        raise ApiException(status=500, reason="boom")

    before = _counter("ApiException")
    with caplog.at_level(logging.WARNING):
        result = run_oscm_tick(
            spec=SPEC,
            body={"metadata": {"name": NAME, "namespace": NAMESPACE}},
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
            module="tick_runner_probe",
            tick_label="tick runner probe",
            idle_label="tick runner probe",
            custom_objects_api=lambda: object(),
            wire=wire,
            tick=tick,
        )

    assert result is None, "runner must swallow the in-tuple exception and return None (D12)"
    assert _counter("ApiException") - before == 1.0, (
        "HANDLER_TICK_FAILURES_TOTAL{module=tick_runner_probe, error_type=ApiException} "
        "must increment by exactly 1 at the shared site"
    )
    assert "tick runner probe tick skipped, retrying next poll (ApiException" in caplog.text, (
        "the shared site must own the skip-tick warning wording"
    )


def test_run_oscm_tick_stamps_heartbeat_on_success() -> None:
    """Issue #469: a successful tick stamps the per-module heartbeat gauge.

    The heartbeat is the one /metrics signal that advances when the
    scheduler is merely INVOKING the timer — set at the END of every
    ``run_oscm_tick`` invocation so a flat gauge (dead scheduler) is
    distinguishable from a failing one (its own counter).
    """

    def tick(*, config, store, emit, deps, now: datetime) -> str:
        return "ran"

    before = time.time()
    result = _run(tick)
    after = time.time()

    assert result == "ran"
    stamped = _heartbeat()
    assert stamped is not None, "success path must stamp the heartbeat gauge"
    assert before - 1.0 <= stamped <= after + 1.0, (
        "heartbeat must be set to time.time() at the end of the invocation"
    )


def test_run_oscm_tick_stamps_heartbeat_on_caught_failure() -> None:
    """Issue #469: a caught skip-tuple failure STILL stamps the heartbeat.

    A failing-but-scheduled tick means the scheduler is alive (D12 retry
    next poll); only a flat gauge means the timers stopped being invoked
    entirely. The stamp must happen on the caught-failure path too.
    """

    def tick(*, config, store, emit, deps, now: datetime) -> object:
        raise RedisClientError("redis gone")

    before = time.time()
    result = _run(tick)
    after = time.time()

    assert result is None
    stamped = _heartbeat()
    assert stamped is not None, "caught-failure path must stamp the heartbeat gauge"
    assert before - 1.0 <= stamped <= after + 1.0, (
        "heartbeat must be set even when the tick was skipped (scheduler alive)"
    )


def test_run_oscm_tick_stamps_heartbeat_on_idle() -> None:
    """Issue #469: the empty-serverUrl idle return ALSO stamps the heartbeat.

    The idle branch is an incomplete-CR posture, not a scheduling
    failure — the timer is still being invoked, so the heartbeat
    advances (a flat gauge must remain reserved for a dead scheduler).
    """

    def tick(*, config, store, emit, deps, now: datetime) -> object:
        raise AssertionError("idle CR must never reach the tick closure")

    before = time.time()
    result = _run(tick, spec=IDLE_SPEC)
    after = time.time()

    assert result is None
    stamped = _heartbeat()
    assert stamped is not None, "idle path must stamp the heartbeat gauge"
    assert before - 1.0 <= stamped <= after + 1.0, (
        "heartbeat must be set on the idle return (timer invoked = scheduler alive)"
    )


def test_run_oscm_tick_skips_on_operator_config_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #475: ``OperatorConfigError`` (TLS / key-layout misconfiguration)
    is an explicit ``SKIP_TICK_EXCEPTIONS`` member.

    Pre-#475 the class subclassed ``RedisClientError`` and was caught inside
    the historical wrappers via that parentage; the move to config.py severs
    the link, so the tuple carries it explicitly to preserve the D12 posture:
    a wiring/config failure skips the tick (counter bumped with
    ``error_type=OperatorConfigError``, single skip log) and retries on the
    next poll — where a fixed Secret, CR spec, or re-mounted CA bundle is
    picked up live — instead of propagating as an uncaught kopf error.
    """
    def wire(config: object) -> object:
        return object()  # client wiring is per-module; the runner only forwards it

    def tick(*, config, store, emit, deps, now: datetime) -> object:
        raise OperatorConfigError("synthetic TLS CA-bundle misconfiguration (#475)")

    before = _counter("OperatorConfigError")
    with caplog.at_level(logging.WARNING):
        result = run_oscm_tick(
            spec=SPEC,
            body={"metadata": {"name": NAME, "namespace": NAMESPACE}},
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
            module="tick_runner_probe",
            tick_label="tick runner probe",
            idle_label="tick runner probe",
            custom_objects_api=lambda: object(),
            wire=wire,
            tick=tick,
        )

    assert result is None, "runner must skip the tick on OperatorConfigError (D12)"
    assert _counter("OperatorConfigError") - before == 1.0, (
        "HANDLER_TICK_FAILURES_TOTAL{module=tick_runner_probe, "
        "error_type=OperatorConfigError} must increment by exactly 1"
    )
    assert (
        "tick runner probe tick skipped, retrying next poll (OperatorConfigError" in caplog.text
    ), "the shared site must log the OperatorConfigError skip-tick warning"


# --- Issue #493 — wiring/construction failures get the D12 skip treatment ------


def test_run_oscm_tick_skips_on_wiring_config_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #493: a ``wire`` closure raising ``ConfigException`` skips the tick.

    The kubeconfig-load failure class (what
    ``load_operator_kube_config`` / the strict ``operator_custom_objects_api``
    factory propagate) is an explicit ``SKIP_TICK_EXCEPTIONS`` member and the
    ``wire`` call runs INSIDE the guarded region — so a client-construction
    failure produces the standard warning log plus a
    ``HANDLER_TICK_FAILURES_TOTAL`` bump with ``error_type=ConfigException``
    and a clean ``None`` return, not an exception escaping the wrapper.

    Also pins the #469 heartbeat interaction: a wiring-failing-but-scheduled
    operator must read as ALIVE on ``HANDLER_LAST_TICK_TIMESTAMP`` (the
    outer ``finally`` still stamps it) while its failure counter climbs —
    the sustained-outage signature SREs alert on.
    """

    def wire(config: object) -> object:
        raise ConfigException("no kubeconfig anywhere (#493)")

    def tick(*, config, store, emit, deps, now: datetime) -> object:
        raise AssertionError("a wire failure must never reach the tick closure")

    before = _counter("ConfigException")
    stamped_before = _heartbeat()
    with caplog.at_level(logging.WARNING):
        result = _run(tick, wire=wire)

    assert result is None, "runner must skip the tick on a wiring ConfigException (D12)"
    assert _counter("ConfigException") - before == 1.0, (
        "HANDLER_TICK_FAILURES_TOTAL{module=tick_runner_probe, "
        "error_type=ConfigException} must increment by exactly 1 on a wiring "
        "failure"
    )
    assert (
        "tick runner probe tick skipped, retrying next poll (ConfigException" in caplog.text
    ), "the shared site must log the wiring-failure skip-tick warning"
    stamped = _heartbeat()
    assert stamped is not None, "wire-failure path must stamp the heartbeat gauge"
    assert stamped >= (stamped_before or 0.0), (
        "heartbeat must advance even when the tick was skipped on a wiring "
        "failure (scheduler alive; counter climbs instead)"
    )


def test_run_oscm_tick_skips_on_wiring_location_value_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #493: a ``wire`` closure raising ``LocationValueError`` skips the tick.

    ``urllib3.exceptions.LocationValueError`` ("No host specified.") is what
    the kubernetes client's transport raises through when an API object built
    against an uninitialised default ``Configuration`` makes its first real
    call (the lenient ``operator_*_api`` placeholder path — proven live in
    #66's kind validation). Pre-#493 it escaped the wrapper as an uncaught
    kopf handler error; now it is an explicit tuple member and gets the
    counted skip.
    """

    def wire(config: object) -> object:
        raise LocationValueError("No host specified.")

    def tick(*, config, store, emit, deps, now: datetime) -> object:
        raise AssertionError("a wire failure must never reach the tick closure")

    before = _counter("LocationValueError")
    with caplog.at_level(logging.WARNING):
        result = _run(tick, wire=wire)

    assert result is None, "runner must skip the tick on a wiring LocationValueError (D12)"
    assert _counter("LocationValueError") - before == 1.0, (
        "HANDLER_TICK_FAILURES_TOTAL{module=tick_runner_probe, "
        "error_type=LocationValueError} must increment by exactly 1 on a "
        "wiring failure"
    )
    assert (
        "tick runner probe tick skipped, retrying next poll (LocationValueError"
        in caplog.text
    ), "the shared site must log the wiring-failure skip-tick warning"


def test_run_oscm_tick_propagates_non_tuple_wiring_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #493 negative: a NON-tuple construction error still propagates (fail-closed).

    The tuple widening is deliberately narrow — only ``ConfigException`` and
    ``LocationValueError`` joined. A random ``RuntimeError`` from a ``wire``
    closure must NOT be silently skipped: it propagates out of the runner as
    an uncaught kopf handler error and the failure counter stays flat
    (pinning the #249 fail-closed posture at the wiring seam).

    The heartbeat still stamps — the outer ``finally`` covers the
    propagating path too (#469), so the scheduler-alive signal survives
    even a crash-looping construction bug.
    """

    def wire(config: object) -> object:
        raise RuntimeError("unexpected construction bug (#493 negative)")

    def tick(*, config, store, emit, deps, now: datetime) -> object:
        raise AssertionError("a wire failure must never reach the tick closure")

    before = _counter("RuntimeError")
    stamped_before = _heartbeat()
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(RuntimeError, match="unexpected construction bug"),
    ):
        _run(tick, wire=wire)

    assert _counter("RuntimeError") - before == 0.0, (
        "HANDLER_TICK_FAILURES_TOTAL must NOT increment for an out-of-tuple "
        "wiring error; observed delta "
        f"{_counter('RuntimeError') - before}. See issue #249 — fail-closed."
    )
    assert "tick skipped, retrying next poll" not in caplog.text, (
        "no skip-tick warning may be logged for an out-of-tuple wiring error"
    )
    stamped = _heartbeat()
    assert stamped is not None, "propagating path must still stamp the heartbeat gauge"
    assert stamped >= (stamped_before or 0.0), (
        "heartbeat must advance even on a propagating wiring error (#469: "
        "the scheduler invoked the timer = alive)"
    )