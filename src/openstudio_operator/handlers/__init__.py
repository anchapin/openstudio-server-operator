"""Kopf handlers, one module per operator component in the plan doc."""

from openstudio_operator.handlers import (  # noqa: F401
    analysis_sla,
    datapoint_watchdog,
    storage_pruner,
    web_background_monitor,
    worker_recycler,
)
from openstudio_operator.metrics import start_metrics_server

# Operator startup wiring: ``kopf run --module openstudio_operator.handlers``
# imports this package exactly once, making this import path the operator's
# entrypoint — so the Prometheus /metrics server is started here (issue #17).
# Idempotent and failure-tolerant; see openstudio_operator/metrics.py.
start_metrics_server()
