"""Operator-behavior constants — single source of truth (issue #165).

These are the DEFAULTS used when a handler module's ``@kopf.timer(..., interval=...)``
registration needs a polling cadence or when an internal sub-system (the metrics
HTTP server, the Resque-key-layout leg-2 safeguard) needs a wiring value that
is NOT user-facing cluster policy. Each module used to declare its own copy
of these constants; a future maintainer who wanted to add a global "all polling
intervals should respect a ``pausePolling: bool`` CR field" had to edit six
files in lockstep. After #165 they live here and every handler imports them.

**Policy values do NOT belong here** — they live in the CRD ``spec`` and
:class:`openstudio_operator.config.OperatorConfig`. ``stallWindowMinutes``
(default 10), ``maxAutoRequeues`` (default 3), ``retentionDays`` (default 7),
and the worker recycle cadence knobs are cluster-policy fields the cluster
admin sets in the CR; their defaults live as ``OperatorConfig`` dataclass
defaults (covered by ``tests/test_smoke.py::test_config_defaults_parity_with_crd_yaml``).
"""

from __future__ import annotations

from datetime import timedelta

# -----------------------------------------------------------------------------
# Module 1 — analysis SLA polling
# -----------------------------------------------------------------------------

#: Tick cadence for the analysis SLA / soft-stop monitor (handlers/analysis_sla.py).
#: Matches the plan doc's declared cadence and the operator's reaction-time budget
#: for completed-but-overdue analyses (D06).
SLA_POLL_INTERVAL_SECONDS: float = 30.0

# -----------------------------------------------------------------------------
# Module 2 — datapoint watchdog polling
# -----------------------------------------------------------------------------

#: Tick cadence for the zombie-datapoint requeue watchdog (handlers/datapoint_watchdog.py).
#: Per-tick REST cost is bounded by ``GET /data_points/status?status=1&jobs=started``,
#: so a 60 s cadence is well within the v3.11.0 server's per-minute budget.
DATAPOINT_POLL_INTERVAL_SECONDS: float = 60.0

# -----------------------------------------------------------------------------
# Module 3 — worker recycler polling
# -----------------------------------------------------------------------------

#: Tick cadence for worker recycle detection / trigger evaluation
#: (handlers/worker_recycler.py). 5 minutes balances "react quickly after a
#: completed analysis" against "don't hammer the K8s API per-CR".
WORKER_RECYCLE_POLL_INTERVAL_SECONDS: float = 300.0

# -----------------------------------------------------------------------------
# Module 4 — web_background queue-stall monitoring
# -----------------------------------------------------------------------------

#: Tick cadence for the web_background queue-stall monitor
#: (handlers/web_background_monitor.py). Matches the datapoint watchdog so
#: both modules observe Redis at the same rate.
WEB_BACKGROUND_POLL_INTERVAL_SECONDS: float = 60.0

#: Issue #44 — Resque key-layout leg-2 non-vacuity safeguard. How long the
#: operator tolerates an empty worker registry with no prior heartbeat
#: observation before concluding the Resque layout is wrong and emitting
#: :data:`RESQUE_KEY_LAYOUT_UNKNOWN_EVENT`. 60 s — comfortably more than one
#: Resque heartbeat (5 s) but short enough that a misconfig surfaces well
#: within the first stall window.
LAYOUT_WARNING_GRACE_SECONDS: timedelta = timedelta(seconds=60)

# -----------------------------------------------------------------------------
# Metrics HTTP server
# -----------------------------------------------------------------------------

#: Conventional Prometheus port for the operator's plaintext ``/metrics``
#: endpoint (metrics.py). The port is exposed by ``deploy/operator-deployment.yaml``
#: ``containerPort: 9090`` and gated by ``deploy/network-policy.yaml``
#: (``openstudio-operator-metrics-ingress`` allow-list, issue #166).
METRICS_PORT: int = 9090