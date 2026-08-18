"""Kopf handlers, one module per operator component in the plan doc."""

from openstudio_operator.handlers import (  # noqa: F401
    analysis_sla,
    datapoint_watchdog,
    storage_pruner,
    web_background_monitor,
    worker_recycler,
)
