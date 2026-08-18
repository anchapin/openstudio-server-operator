"""Prometheus metrics for the operator (plan Phase 4).

Counters are declared now so handler stubs and tests can import them; the
/metrics HTTP endpoint is wired up in Phase 4.
"""

from prometheus_client import Counter

SOFT_STOPS_TOTAL = Counter(
    "openstudio_operator_soft_stops_total",
    "Analyses soft-stopped by the SLA monitor",
)

DATAPOINTS_REQUEUED_TOTAL = Counter(
    "openstudio_operator_datapoints_requeued_total",
    "Zombie datapoints automatically requeued",
)

WORKERS_RECYCLED_TOTAL = Counter(
    "openstudio_operator_workers_recycled_total",
    "Worker deployment recycles performed",
)

STORAGE_FREED_BYTES = Counter(
    "openstudio_operator_storage_freed_bytes",
    "NFS bytes reclaimed after archival/pruning",
)
