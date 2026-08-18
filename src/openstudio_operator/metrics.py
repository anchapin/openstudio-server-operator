"""Prometheus metrics for the operator (plan Phase 4, issue #17).

Counters are module-level singletons on prometheus_client's default REGISTRY.
The /metrics endpoint is served by ``start_metrics_server()`` via
``prometheus_client.start_http_server`` — a plain WSGI server on a daemon
thread. Mechanism choice (D10): no web framework, no extra dependencies, and
the thread dies with the process. It is started at operator startup from the
handlers package import (``kopf run --module openstudio_operator.handlers``
imports that package exactly once), which is the operator's de-facto
entrypoint — see ``openstudio_operator/handlers/__init__.py``.

Port choice: 9090, the conventional Prometheus port, as a single module
constant that must stay in sync with the containerPort in
deploy/operator-deployment.yaml.
"""

import logging
import threading

import prometheus_client
from prometheus_client import Counter

logger = logging.getLogger(__name__)

SOFT_STOPS_TOTAL = Counter(
    "openstudio_operator_soft_stops_total",
    "Analyses soft-stopped by the SLA monitor",
)

DATAPOINTS_REQUEUED_TOTAL = Counter(
    "openstudio_operator_datapoints_requeued_total",
    "Zombie datapoints automatically requeued",
)

DATAPOINTS_REQUEUE_EXHAUSTED_TOTAL = Counter(
    "openstudio_operator_datapoints_requeue_exhausted_total",
    "Zombie datapoints abandoned after exceeding maxRequeues "
    "(incremented by the datapoint watchdog, #10)",
)

WORKERS_RECYCLED_TOTAL = Counter(
    "openstudio_operator_workers_recycled_total",
    "Worker deployment recycles performed",
)

STORAGE_FREED_BYTES = Counter(
    "openstudio_operator_storage_freed_bytes",
    "NFS bytes reclaimed after archival/pruning",
)

DEFAULT_METRICS_PORT = 9090

_start_lock = threading.Lock()
_started = False


def start_metrics_server(port: int | None = None, addr: str = "0.0.0.0") -> int | None:
    """Serve /metrics from a daemon thread (idempotent).

    Returns the bound port, or ``None`` if the server was already started in
    this process or the port could not be bound (logged as a warning — losing
    metrics must never take the operator down).
    """
    global _started
    with _start_lock:
        if _started:
            return None
        bound_port = DEFAULT_METRICS_PORT if port is None else port
        try:
            server, thread = prometheus_client.start_http_server(bound_port, addr=addr)
        except OSError as exc:
            logger.warning("Cannot serve /metrics on %s:%s: %s", addr, bound_port, exc)
            return None
        _started = True
        logger.info(
            "Serving /metrics on %s:%s (daemon thread: %s)", addr, bound_port, thread.daemon
        )
        return server.server_address[1]


def is_metrics_server_started() -> bool:
    """Whether :func:`start_metrics_server` has successfully run in this process."""
    return _started
