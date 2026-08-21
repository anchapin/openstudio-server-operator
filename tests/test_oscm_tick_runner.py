"""Focused test for the shared OSCM tick-runner (issue #473).

``run_oscm_tick`` in :mod:`openstudio_operator._oscm_handlers` owns the
wrapper wiring the four timer handlers used to duplicate: config parse,
the empty-``serverUrl`` idle branch, store/emitter construction, the
canonical skip-tick exception tuple, the ``HANDLER_TICK_FAILURES_TOTAL``
increment, and the single per-handler skip-tick warning. This file pins
that ownership in one place; the per-wrapper behavior (each handler
delegating through the runner) stays covered by the — unmodified —
``tests/test_timer_wrapper_failures.py`` and the per-handler suites.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
from kubernetes.client import ApiException
from prometheus_client import REGISTRY

from openstudio_operator._oscm_handlers import SKIP_TICK_EXCEPTIONS, run_oscm_tick
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
    unification.
    """
    assert SKIP_TICK_EXCEPTIONS == (
        OpenStudioApiError,
        StatusStoreError,
        ApiException,
        RedisClientError,
    ), "the canonical tuple must stay the union of the four historical wrapper tuples"

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
