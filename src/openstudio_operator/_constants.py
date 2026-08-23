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
# CRD identity — the canonical group/version/plural wiring (issue #495)
# -----------------------------------------------------------------------------

#: The OSCM custom-resource identity. Every ``@kopf.timer`` /
#: ``@kopf.on.event`` decorator and every CustomObjectsApi call site in the
#: operator consumes these constants — no raw identity strings anywhere
#: else. Rationale: the wiring used to be reassembled in six places (the
#: status_store constants, four per-module ``_SPEC`` dict literals, and two
#: hardcoded decorators in handlers/__init__.py); a typo or a version bump
#: edited in only one place would silently detach a watch handler from the
#: watched resource — the handler simply never fires. The CI fence
#: ``tests/test_singleton_registry_coverage.py::test_crd_identity_literals_live_only_in_constants``
#: fails the build if a raw literal reappears outside this module.
CRD_GROUP = "energy.nrel.gov"
CRD_VERSION = "v1alpha1"
CRD_PLURAL = "openstudioclustermanagers"

#: Uniform decorator-consumption form:
#: ``@kopf.on.event(**CRD_SPEC)`` / ``@kopf.timer(**CRD_SPEC, interval=...)``.
#: kopf's resource selectors accept ``group``/``version``/``plural`` as
#: keyword arguments, so passing the canonical object as kwargs makes an
#: argument-order swap (a silent mis-wiring) impossible. The dict literal
#: exists exactly once — here.
CRD_SPEC = {"group": CRD_GROUP, "version": CRD_VERSION, "plural": CRD_PLURAL}

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

#: Issue #490 — periodic Redis key-layout revalidation cadence. The #163
#: boot-time check (``handlers.redis_layout_check._check_redis_key_layout_for_cr`` behind the
#: ``@kopf.on.event`` watch) only re-runs on OSCM watch events — the boot
#: listing and CR edits — so a steady-state cluster generates none and a
#: mid-flight Resque layout drift (helm chart upgrade to a different
#: prefix, queue backend swap) leaves the key-layout status gauge holding
#: its boot value indefinitely. The web_background stall tick re-runs the
#: check on this cadence (``web_background_monitor``'s #490 rider). 5
#: minutes — half the default ``stallWindowMinutes`` (10), so drift is
#: revalidated and surfaced on ``redis_key_layout_status{,_fresh}``
#: before the FIRST vacuous restart window can complete; matches the
#: worker-recycler 300 s precedent for a low-frequency Redis-side scan
#: cadence (per-run cost bounded by ``VALIDATE_SCAN_KEY_BUDGET``).
REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL: timedelta = timedelta(minutes=5)

# -----------------------------------------------------------------------------
# Kubernetes API client transport (issue #579)
# -----------------------------------------------------------------------------

#: Issue #579 — bounded request timeout for EVERY Kubernetes API call the
#: operator makes (status RMW patches, singleton-guard CR lists, Deployment
#: reads/patches, the #463 Secret read, and the prune CronJob's Jobs/Events
#: through the same factories). Applied once at the shared construction path
#: (:func:`openstudio_operator.singleton._cached_k8s_api` via
#: :func:`openstudio_operator._k8s.apply_request_timeout`): a
#: :class:`openstudio_operator._k8s.BoundedK8sRequest` wrapper over the
#: constructed client's ``rest_client.request`` — kubernetes-python
#: (``>=29.3,<37``) has NO ``Configuration.timeout`` wiring (the generated
#: ``*V1Api`` methods honor only a per-call ``_request_timeout``, and
#: ``rest.py`` defaults the urllib3 timeout to ``None`` = wait forever), so
#: without the wrapper a black-holed apiserver connection (kube-proxy stall,
#: NAT idle drop without RST) blocks the calling timer tick forever while
#: the pod stays ``Running`` and /metrics keeps answering liveness.
#:
#: 15 s rationale: strictly above the REST client's 10 s per-request
#: timeout and the Redis client's 5 s socket timeouts (an apiserver slower
#: than the OpenStudio REST dependency it fronts would be an outage worth
#: surfacing, not a timeout to cut early), strictly below the shortest
#: timer cadence (``SLA_POLL_INTERVAL_SECONDS`` = 30 s) so the D12
#: skip-tick lands and the next poll retries within one interval, and
#: inside the issue's 10–30 s guidance band. A transport timeout is
#: operator BEHAVIOR, not cluster policy — it stays out of the CRD
#: ``spec`` / :class:`~openstudio_operator.config.OperatorConfig` boundary
#: this module's docstring draws.
K8S_REQUEST_TIMEOUT_SECONDS: float = 15.0

# -----------------------------------------------------------------------------
# Metrics HTTP server
# -----------------------------------------------------------------------------

#: Conventional Prometheus port for the operator's plaintext ``/metrics``
#: endpoint (metrics.py). The port is exposed by ``deploy/operator-deployment.yaml``
#: ``containerPort: 9090`` and gated by ``deploy/network-policy.yaml``
#: (``openstudio-operator-metrics-ingress`` allow-list, issue #166).
METRICS_PORT: int = 9090