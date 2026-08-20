"""Smoke tests for the Prometheus /metrics endpoint (issue #17)."""

import socket
import subprocess
import sys
import textwrap
from contextlib import closing

import requests
from prometheus_client import Counter, Gauge, Histogram, generate_latest

from openstudio_operator import metrics
from openstudio_operator.metrics import start_metrics_server

EXPECTED_COUNTER_FAMILIES = (
    "openstudio_operator_soft_stops_total",
    "openstudio_operator_datapoints_requeued_total",
    "openstudio_operator_datapoints_requeue_exhausted_total",
    "openstudio_operator_workers_recycled_total",
    "openstudio_operator_worker_pods_evicted_total",
    "openstudio_operator_web_background_restarts_total",
    "openstudio_operator_analyses_archived_total",
    "openstudio_operator_analyses_deleted_total",
    # Issue #119 — status-store 409 retry observability surface.
    "openstudio_operator_status_conflicts_total",
    "openstudio_operator_status_conflict_retries_exhausted_total",
    # Issue #117 — handler tick failures (per module + error_type).
    "openstudio_operator_handler_tick_failures_total",
    # Issue #171 — defensive cap evictions on the CR .status maps.
    "openstudio_operator_status_map_caps_total",
    # Issue #237 — EventEmitter (#164) dry-run gate Prometheus surface
    # (per-reason labelled). Mirrors the warning-event reason vocabulary
    # so dashboards can tell WHICH handler path the dry-run gate
    # intercepted. Emitted companion below lets the dry-run ratio be
    # derived via Prometheus arithmetic without log scraping.
    "openstudio_operator_events_dry_run_suppressed_total",
    "openstudio_operator_events_emitted_total",
    # Issue #239 — singleton-guard election outcomes (per outcome).
    "openstudio_operator_singleton_election_total",
    # Issue #255 — kopf.event emission failures (per reason).
    "openstudio_operator_events_emit_failures_total",
    # Issue #310 — QueuedKopfEventSink backpressure drop counter. The
    # sink rejects defer_to_next_tick calls once the queue hits
    # MAX_DEFERRED_WARNING_EVENTS (1000); the Counter increments on each
    # rejection with reason="queue_full" so the loss is observable on
    # /metrics (no longer silent). Initial vocabulary is one reason; the
    # label leaves room for a future per-reason-cap branch without a
    # Counter rename.
    "openstudio_operator_warnings_deferred_dropped_total",
)

#: Issue #44 — Resque key-layout leg-2 non-vacuity safeguard. Since #87
#: the gauge is emitted every poll regardless of queue depth, so
#: `resque_workers_seen_max == 0` with a reachable Redis means no workers
#: are registered; under load that signature additionally marks a
#: centralized-constants / live v3.11.0 layout mismatch (the original #44
#: alert combined it with `AND queue depth > 0`).
#:
#: Issue #238 — ``resque_queue_depth{queue="..."}`` exposes the operator's
#: authoritative LLEN reads on every sensing tick (the same value KEDA's
#: external metrics API exposes — the operator's view is the cross-check
#: that surfaces a centralized-constants drift).
#:
#: Issue #253 — ``redis_key_layout_status`` Gauge (1.0=ok, 0.0=any-other)
#: surfaces the boot-time validator outcome as a cluster-wide latest-
#: observation signal so an SRE can alert on `== 0` without log scraping.
#:
#: Issue #254 — ``stall_window_elapsed_seconds`` Gauge tracks the
#: ``StallWindowTracker`` state between the first sustained observation
#: and the eventual ``web_background_restarts_total`` increment — a heads-
#: up display that gives SREs time to react before the gate trips.
#:
#: Issue #312 — paired freshness timestamp gauges for ``resque_queue_
#: depth`` and ``stall_window_elapsed_seconds``. Set to ``time.time()``
#: on every successful read/update so dashboards can compute staleness
#: (``time() - fresh``) and alert on a sustained gap. The data gauges
#: advance on success but are NOT touched on the exception path (Redis
#: unreachable, ApiException from ``_stall_condition_holds``, etc.) —
#: without the freshness pair a prior tick's value masquerades as a
#: live reading while the operator has in fact lost visibility.
#:
#: Issue #310 — ``warnings_deferred_queue_depth`` Gauge surfaces the
#: in-process QueuedKopfEventSink queue depth on every defer / flush
#: call. Unlabelled (the queue is process-wide, not per-CR), so
#: cardinality stays bounded regardless of CR count. Sustained nonzero
#: values mean the apiserver watch stream is stalled and Warning
#: Events are piling up — a companion to the
#: ``warnings_deferred_dropped_total`` Counter which fires when the
#: cap (MAX_DEFERRED_WARNING_EVENTS = 1000) is exceeded.
EXPECTED_GAUGE_FAMILIES = (
    "openstudio_operator_resque_workers_seen_max",
    "openstudio_operator_resque_queue_depth",
    "openstudio_operator_redis_key_layout_status",
    "openstudio_operator_stall_window_elapsed_seconds",
    "openstudio_operator_resque_queue_depth_fresh",
    "openstudio_operator_stall_window_fresh",
    "openstudio_operator_warnings_deferred_queue_depth",
)

#: Issue #179 — per-CR datapoint-budget Histogram. The SLA monitor and the
#: datapoint watchdog each observe the count off the OpenStudio REST
#: analysis payload (or the equivalent summary endpoint) once per tick:
#: the SLA records ``len(analyses)`` from ``/analyses.json``; the watchdog
#: records ``len(started_ids)`` from ``/data_points/status?status=1&jobs=
#: started``. No labels — one observation per tick, bounded-cardinality
#: at the histogram level rather than per analysis.
EXPECTED_HISTOGRAM_FAMILIES = ("openstudio_operator_analysis_datapoint_count",)


def _declared_counter_families():
    """Exposition family name for every Counter declared in metrics.py."""
    return [
        f"{value._name}_total" for value in vars(metrics).values() if isinstance(value, Counter)
    ]


def _declared_gauge_families():
    """Exposition family name for every Gauge declared in metrics.py (issue #44)."""
    return [value._name for value in vars(metrics).values() if isinstance(value, Gauge)]


def _declared_histogram_families():
    """Exposition family name for every Histogram declared in metrics.py (issue #179)."""
    return [value._name for value in vars(metrics).values() if isinstance(value, Histogram)]


def test_declared_counters_match_expected_set():
    assert sorted(_declared_counter_families()) == sorted(EXPECTED_COUNTER_FAMILIES)


def test_declared_gauges_match_expected_set():
    assert sorted(_declared_gauge_families()) == sorted(EXPECTED_GAUGE_FAMILIES)


def test_declared_histograms_match_expected_set():
    assert sorted(_declared_histogram_families()) == sorted(EXPECTED_HISTOGRAM_FAMILIES)


def test_every_declared_counter_family_in_registry_exposition():
    exposition = generate_latest().decode()
    for name in _declared_counter_families():
        assert f"# TYPE {name} counter" in exposition


def test_every_declared_gauge_family_in_registry_exposition():
    exposition = generate_latest().decode()
    for name in _declared_gauge_families():
        assert f"# TYPE {name} gauge" in exposition


def test_every_declared_histogram_family_in_registry_exposition():
    # Issue #179 — Histogram, like a labelled Counter, only exposes its
    # `# TYPE` family line after the first observation. The pre-touch
    # below keeps the family-existence assertion self-contained
    # (mirrors the labelled-counter pattern from #117).
    metrics.ANALYSIS_DATAPOINT_COUNT.observe(1)
    exposition = generate_latest().decode()
    for name in _declared_histogram_families():
        assert f"# TYPE {name} histogram" in exposition


def _free_port():
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_metrics_http_server_serves_all_declared_counters():
    port = start_metrics_server(port=_free_port(), addr="127.0.0.1")
    assert port is not None
    assert metrics.is_metrics_server_started()
    # Issue #311 — namespace/name CR-identity labels are added to the
    # four status-store/conflict counters and the four action counters;
    # the pre-touch labels below must carry the full label set so the
    # sentinel series exists in the exposition. The ``namespace`` /
    # ``name`` values use the ``__metrics_test_sentinel__`` placeholder
    # convention so the assert lines below can pin the exact exposition
    # shape (no production-value leakage, easy to grep / fixture).
    SENTINEL_NAMESPACE = "__metrics_test_sentinel_ns__"
    SENTINEL_NAME = "__metrics_test_sentinel__"
    # Issue #117 — pre-touch the labelled counter so the exposition
    # registers at least one series. A labelled Counter with no
    # observations does not expose the family until `.labels(...).inc()`
    # has been called once, so guarding the family-existence assertion
    # on a touch keeps this test self-contained. (Note: `inc(0)` is
    # a no-op in prometheus_client 0.26.x — `inc()` alone is what
    # actually creates the series.)
    metrics.HANDLER_TICK_FAILURES_TOTAL.labels(
        namespace=SENTINEL_NAMESPACE,
        name=SENTINEL_NAME,
        module="__metrics_test_sentinel__",
        error_type="OpenStudioApiError",
    ).inc()
    # Issue #119 / #171 — same pattern for the status-store counters.
    # Issue #311 adds (namespace, name) labels so the family existence
    # check must include the full label set.
    metrics.STATUS_CONFLICTS_TOTAL.labels(
        namespace=SENTINEL_NAMESPACE, name=SENTINEL_NAME
    ).inc()
    metrics.STATUS_CONFLICT_RETRIES_EXHAUSTED_TOTAL.labels(
        namespace=SENTINEL_NAMESPACE, name=SENTINEL_NAME
    ).inc()
    # Issue #171 — same pattern for the defensive-cap eviction counter.
    # Issue #311 adds (namespace, name) to the existing (map_name) set.
    metrics.STATUS_MAP_CAPS_TOTAL.labels(
        namespace=SENTINEL_NAMESPACE,
        name=SENTINEL_NAME,
        map_name="__metrics_test_sentinel__",
    ).inc()
    # Issue #237 — same pattern for the two EventEmitter (#164)
    # counters. ``events_dry_run_suppressed_total`` and
    # ``events_emitted_total`` are labelled by (namespace, name, reason);
    # pre-touch both so the family-existence assertion is self-contained
    # for the labelled-counter branch.
    metrics.EVENTS_DRY_RUN_SUPPRESSED_TOTAL.labels(
        namespace=SENTINEL_NAMESPACE,
        name=SENTINEL_NAME,
        reason="__metrics_test_sentinel__",
    ).inc()
    metrics.EVENTS_EMITTED_TOTAL.labels(
        namespace=SENTINEL_NAMESPACE,
        name=SENTINEL_NAME,
        reason="__metrics_test_sentinel__",
    ).inc()
    # Issue #239 — pre-touch the singleton-guard election outcome
    # counter so its labelled family is exposed at the exposition
    # surface. ``outcome`` label vocabulary mirrors the enforce()'s three
    # branches (idle | active | conflict). Issue #311 leaves this
    # counter CR-unlabelled (the singleton guard is namespace-scoped,
    # not CR-scoped — the guard itself is the multi-CR protection).
    metrics.SINGLETON_ELECTION_TOTAL.labels(outcome="__metrics_test_sentinel__").inc()
    # Issue #255 — pre-touch the kopf.event emission-failure counter.
    # ``reason`` label vocabulary matches ``events_emitted_total`` and,
    # post-#311, the (namespace, name) labels carry the same CR identity.
    metrics.EVENTS_EMIT_FAILURES_TOTAL.labels(
        namespace=SENTINEL_NAMESPACE,
        name=SENTINEL_NAME,
        reason="__metrics_test_sentinel__",
    ).inc()
    # Issue #238 — pre-touch the labelled ``resque_queue_depth`` Gauge
    # so the family line is exposed alongside the unlabelled #44, #253,
    # #254 gauges. The labelled form (``{queue="..."}``) is then asserted
    # below alongside the bare-form unlabelled gauges.
    metrics.RESQUE_QUEUE_DEPTH.labels(queue="__metrics_test_sentinel__").set(0)
    # Issue #310 — pre-touch the labelled drop Counter so the family
    # line is exposed. ``reason`` label vocabulary currently includes
    # ``queue_full`` (the only drop path today); a future second reason
    # adds a series, not a family rename.
    metrics.WARNINGS_DEFERRED_DROPPED_TOTAL.labels(
        reason="__metrics_test_sentinel__"
    ).inc()

    response = requests.get(f"http://127.0.0.1:{port}/metrics", timeout=5)
    assert response.status_code == 200
    for name in _declared_counter_families():
        assert f"# TYPE {name} counter" in response.text
        # Labelled counters (issue #117's ``handler_tick_failures_total``,
        # issue #119's ``status_conflicts_total`` /
        # ``status_conflict_retries_exhausted_total``, issue #171's
        # ``status_map_caps_total``, issue #237's
        # ``events_dry_run_suppressed_total`` / ``events_emitted_total``,
        # issue #239's ``singleton_election_total``, issue #255's
        # ``events_emit_failures_total``) emit ``<name>{<labels>} value``;
        # non-labelled emit ``<name> value``. The sentinel increments above
        # pre-touch the labelled series; check the labelled form for them,
        # the bare form for the rest. Issue #311 — the exposition label
        # order is the order prometheus_client writes the labels (sorted
        # alphabetically by key); the assert strings below use that order.
        if name == "openstudio_operator_handler_tick_failures_total":
            assert (
                'openstudio_operator_handler_tick_failures_total{error_type="OpenStudioApiError",'
                'module="__metrics_test_sentinel__",'
                f'name="{SENTINEL_NAME}",'
                f'namespace="{SENTINEL_NAMESPACE}"'
                "}"
                in response.text
            )
        elif name == "openstudio_operator_status_conflicts_total":
            assert (
                f'openstudio_operator_status_conflicts_total{{name="{SENTINEL_NAME}",'
                f'namespace="{SENTINEL_NAMESPACE}"}}'
                in response.text
            )
        elif name == "openstudio_operator_status_conflict_retries_exhausted_total":
            assert (
                f'openstudio_operator_status_conflict_retries_exhausted_total{{name="{SENTINEL_NAME}",'
                f'namespace="{SENTINEL_NAMESPACE}"}}'
                in response.text
            )
        elif name == "openstudio_operator_status_map_caps_total":
            assert (
                'openstudio_operator_status_map_caps_total{map_name="__metrics_test_sentinel__",'
                f'name="{SENTINEL_NAME}",'
                f'namespace="{SENTINEL_NAMESPACE}"'
                "}"
                in response.text
            )
        elif name == "openstudio_operator_events_dry_run_suppressed_total":
            assert (
                f'openstudio_operator_events_dry_run_suppressed_total{{name="{SENTINEL_NAME}",'
                f'namespace="{SENTINEL_NAMESPACE}",'
                'reason="__metrics_test_sentinel__"'
                "}"
                in response.text
            )
        elif name == "openstudio_operator_events_emitted_total":
            assert (
                f'openstudio_operator_events_emitted_total{{name="{SENTINEL_NAME}",'
                f'namespace="{SENTINEL_NAMESPACE}",'
                'reason="__metrics_test_sentinel__"'
                "}"
                in response.text
            )
        elif name == "openstudio_operator_singleton_election_total":
            assert (
                'openstudio_operator_singleton_election_total{outcome="__metrics_test_sentinel__"}'
                in response.text
            )
        elif name == "openstudio_operator_events_emit_failures_total":
            assert (
                f'openstudio_operator_events_emit_failures_total{{name="{SENTINEL_NAME}",'
                f'namespace="{SENTINEL_NAMESPACE}",'
                'reason="__metrics_test_sentinel__"'
                "}"
                in response.text
            )
        elif name == "openstudio_operator_warnings_deferred_dropped_total":
            assert (
                'openstudio_operator_warnings_deferred_dropped_total{reason="__metrics_test_sentinel__"}'
                in response.text
            )
        else:
            assert f"\n{name} " in response.text
    # Issue #44: gauge exposed alongside counters.
    for name in _declared_gauge_families():
        assert f"# TYPE {name} gauge" in response.text
        if name == "openstudio_operator_resque_queue_depth":
            # Labelled gauge — check the labelled form (issue #238).
            assert (
                'openstudio_operator_resque_queue_depth{queue="__metrics_test_sentinel__"}'
                in response.text
            )
        else:
            assert f"\n{name} " in response.text
    # Issue #179: histogram exposed alongside counters and the gauge. The
    # pre-touch above creates the sentinel SERIES for the labelled counter;
    # the histogram is unlabelled, but it ALSO only emits its `# TYPE` line
    # after the first observation. Pre-touch it here so the family-existence
    # assertion is self-contained (mirrors the labelled-counter pattern).
    metrics.ANALYSIS_DATAPOINT_COUNT.observe(1)
    response = requests.get(f"http://127.0.0.1:{port}/metrics", timeout=5)
    for name in _declared_histogram_families():
        assert f"# TYPE {name} histogram" in response.text

    # idempotent: a second call must not start another server — it returns
    # the already-active port instead
    assert start_metrics_server(port=_free_port(), addr="127.0.0.1") == port


def test_handlers_import_starts_metrics_server():
    # The operator's entrypoint is `kopf run --module openstudio_operator.handlers`;
    # importing the package must invoke start_metrics_server() (handlers/__init__.py).
    # The spy redirects the real bind to an ephemeral localhost port so the test
    # does not depend on the default port 9090 being free on the test machine.
    code = textwrap.dedent(
        """
        import openstudio_operator.metrics as m
        calls = []
        real = m.start_metrics_server
        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return real(port=0, addr="127.0.0.1")
        m.start_metrics_server = spy
        import openstudio_operator.handlers
        assert calls, "handlers import did not invoke start_metrics_server"
        assert m.is_metrics_server_started()
        """
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# --- Issue #117 — handler tick failures counter ----------------------------------


def _counter_total(counter) -> float:
    """Read a labelled Counter's total across ALL label series.

    For unlabelled counters (\"_value\" attribute holding a MutexValue),
    read the value directly. For labelled counters (\"_metrics\" dict per
    label combo), sum every keyed sample's value. Either form is
    supported by prometheus_client 0.26.x.
    """
    raw_total = getattr(counter, "_value", None)
    if raw_total is not None:
        v = raw_total.get()
        if isinstance(v, (int, float)):
            return float(v)
    per_series = getattr(counter, "_metrics", None)
    if per_series:
        return sum(
            float(snapshot._value.get())  # type: ignore[attr-defined]
            for snapshot in per_series.values()
        )
    return 0.0


def test_handler_tick_failures_counter_increments_per_module():
    """The 4 handler modules must each report a distinct tick-failure counter
    with ``(namespace, name, module, error_type)`` labels. Driving the
    increment directly via ``labels(...).inc()`` is sufficient to verify
    the label machinery works across every module name the wrappers use
    (#117); issue #311 adds the (namespace, name) CR-identity labels so
    a multi-CR process can attribute failures to the specific CR."""
    counter = metrics.HANDLER_TICK_FAILURES_TOTAL
    baseline = _counter_total(counter)
    for module in (
        "analysis_sla",
        "datapoint_watchdog",
        "worker_recycler",
        "web_background_monitor",
    ):
        counter.labels(
            namespace="openstudio-server",
            name="oscm",
            module=module,
            error_type="OpenStudioApiError",
        ).inc()
    after = _counter_total(counter)
    # 4 modules × 1 increment each.
    assert after - baseline == 4.0


def test_handler_tick_failures_counter_distinguishes_error_types():
    """Same module, two distinct exception classes — must record separately so
    a dashboard alerting on ``error_type`` can tell an API-down from a
    store-conflict from a Redis-down storm. Issue #311 adds (namespace,
    name) CR identity so the (module, error_type) tuple is now a
    4-tuple (namespace, name, module, error_type)."""
    counter = metrics.HANDLER_TICK_FAILURES_TOTAL
    baseline = _counter_total(counter)
    counter.labels(
        namespace="openstudio-server",
        name="oscm",
        module="analysis_sla",
        error_type="OpenStudioApiError",
    ).inc()
    counter.labels(
        namespace="openstudio-server",
        name="oscm",
        module="analysis_sla",
        error_type="ApiException",
    ).inc()
    counter.labels(
        namespace="openstudio-server",
        name="oscm",
        module="analysis_sla",
        error_type="RedisClientError",
    ).inc()
    after = _counter_total(counter)
    assert after - baseline == 3.0
    # The exposition form is the verified shape — each
    # (namespace, name, module, error_type) is its own labelled series.
    exposition = generate_latest().decode()
    assert (
        'openstudio_operator_handler_tick_failures_total{error_type="OpenStudioApiError",'
        'module="analysis_sla",name="oscm",namespace="openstudio-server"}'
        in exposition
    )


# --- Issue #237 — EventEmitter dry-run gate Prometheus surface ----------------


def test_events_dry_run_suppressed_counter_increments_per_reason():
    """Issue #237 acceptance: ``events_dry_run_suppressed_total`` increments
    per-reason at the same site that bumps ``EventEmitter.suppressed_count``.

    Driving the increment directly via ``labels(...).inc()`` mirrors the
    labelled-counter pattern from #117 and #171 — the actual emission site
    (inside :meth:`EventEmitter.emit`) is covered end-to-end by
    ``tests/test_events.py``. This test pins the label cardinality so a
    future refactor that drops the label is caught at CI. Issue #311
    adds (namespace, name) CR-identity labels so the per-reason drill
    is now a 3-tuple (namespace, name, reason)."""
    counter = metrics.EVENTS_DRY_RUN_SUPPRESSED_TOTAL
    baseline = _counter_total(counter)
    for reason in (
        "AnalysisSoftStopped",
        "AnalysisEscalated",
        "DatapointRequeued",
        "DatapointRequeueExhausted",
        "WorkerRecycled",
        "WebBackgroundRestarted",
        "ResqueKeyLayoutUnknown",
    ):
        counter.labels(
            namespace="openstudio-server", name="oscm", reason=reason
        ).inc()
    after = _counter_total(counter)
    # 7 reasons × 1 increment each.
    assert after - baseline == 7.0


def test_events_emitted_counter_increments_per_reason():
    """Issue #237 acceptance: companion ``events_emitted_total`` increments
    per-reason for every successful ``kopf.event`` call from
    :class:`EventEmitter`. Together with the suppressed counter above,
    ``rate(emitted) / rate(suppressed)`` is the headline SLO for an
    audit-only install — a non-trivial suppressed rate with a zero
    emitted rate is the intended state; the test pin is for the inverse
    drift (suppressed > emitted during a non-dry-run deploy). Issue
    #311 adds (namespace, name) CR identity so the per-reason drill
    is now a 3-tuple (namespace, name, reason)."""
    counter = metrics.EVENTS_EMITTED_TOTAL
    baseline = _counter_total(counter)
    for reason in (
        "AnalysisSoftStopped",
        "AnalysisEscalated",
        "DatapointRequeued",
        "DatapointRequeueExhausted",
        "WorkerRecycled",
        "WebBackgroundRestarted",
        "ResqueKeyLayoutUnknown",
    ):
        counter.labels(
            namespace="openstudio-server", name="oscm", reason=reason
        ).inc()
    after = _counter_total(counter)
    assert after - baseline == 7.0


def test_events_counters_exposition_uses_reason_label():
    """Issue #237 — verified exposition shape: each ``reason`` value is its
    own labelled series. Same guard pattern as #117's per-(module, error_type)
    series check: a labelled Counter with at least one observation exposes
    ``<name>{<labels>} <value>`` and the family line. Pinning the label key
    here (``reason``) means a future refactor that silently renames the label
    (e.g. to ``event_reason``) is caught at CI rather than at the on-call's
    Grafana board. Issue #311 — the labels now include (namespace, name)
    so the exposition shape is the verified 3-tuple."""
    counter_suppressed = metrics.EVENTS_DRY_RUN_SUPPRESSED_TOTAL
    counter_emitted = metrics.EVENTS_EMITTED_TOTAL
    counter_suppressed.labels(
        namespace="openstudio-server", name="oscm", reason="ExpositionShapeProbe"
    ).inc()
    counter_emitted.labels(
        namespace="openstudio-server", name="oscm", reason="ExpositionShapeProbe"
    ).inc()
    exposition = generate_latest().decode()
    assert (
        'openstudio_operator_events_dry_run_suppressed_total{name="oscm",'
        'namespace="openstudio-server",reason="ExpositionShapeProbe"}'
        in exposition
    )
    assert (
        'openstudio_operator_events_emitted_total{name="oscm",'
        'namespace="openstudio-server",reason="ExpositionShapeProbe"}'
        in exposition
    )


# --- Issue #238 — Resque queue-depth Gauge --------------------------------------


def test_resque_queue_depth_gauge_exposes_both_queues():
    """Issue #238 acceptance: ``openstudio_operator_resque_queue_depth{queue}``
    Gauge is exposed with both managed Resque queues (``simulations`` +
    ``requeued``) as label values. Cardinality is bounded to the two
    queues; a future refactor that adds a third queue without updating
    this pin (or that collapses the label and emits two unlabelled
    gauges) is caught at CI rather than at the operator's /metrics
    scrape. Same labelled-counter pattern as #117 / #171 / #237 / #239
    / #255.
    """
    metrics.RESQUE_QUEUE_DEPTH.labels(queue="simulations").set(7)
    metrics.RESQUE_QUEUE_DEPTH.labels(queue="requeued").set(3)
    exposition = generate_latest().decode()
    assert (
        'openstudio_operator_resque_queue_depth{queue="simulations"} 7.0' in exposition
    )
    assert (
        'openstudio_operator_resque_queue_depth{queue="requeued"} 3.0' in exposition
    )


# --- Issue #239 — singleton-guard election outcomes -----------------------------


def test_singleton_election_counter_increments_per_outcome():
    """Issue #239 acceptance: ``openstudio_operator_singleton_election_total``
    counter increments per outcome (idle | active | conflict) at the
    three branches in :meth:`SingletonGuard.enforce`. Driving the
    increment directly via ``labels(...).inc()`` mirrors the labelled-
    counter pattern from #117 / #171 / #237 / #255 and pins the label
    cardinality so a future refactor that drops or renames the label is
    caught at CI."""
    counter = metrics.SINGLETON_ELECTION_TOTAL
    baseline = _counter_total(counter)
    for outcome in ("idle", "active", "conflict"):
        counter.labels(outcome=outcome).inc()
    after = _counter_total(counter)
    assert after - baseline == 3.0


def test_singleton_election_counter_exposition_uses_outcome_label():
    """Issue #239 — verified exposition shape: each ``outcome`` value is
    its own labelled series. Pinning the label key (``outcome``) means a
    future refactor that silently renames the label (e.g. to
    ``election_outcome``) is caught at CI rather than at the on-call's
    Grafana board."""
    counter = metrics.SINGLETON_ELECTION_TOTAL
    counter.labels(outcome="ExpositionShapeProbe").inc()
    exposition = generate_latest().decode()
    assert (
        'openstudio_operator_singleton_election_total{outcome="ExpositionShapeProbe"}'
        in exposition
    )


# --- Issue #253 — Redis key-layout status Gauge ---------------------------------


def test_redis_key_layout_status_gauge_ok_vs_other():
    """Issue #253 acceptance: ``openstudio_operator_redis_key_layout_status``
    Gauge reads ``1.0`` for the ``ok`` validator outcome and ``0.0`` for
    every other terminal status (``degraded`` | ``unreachable`` |
    ``error`` | ``skipped``). The gauge is cluster-wide latest-observation
    — no per-CR labels, cardinality stays bounded regardless of CR count.
    """
    metrics.REDIS_KEY_LAYOUT_STATUS.set(1.0)
    assert metrics.REDIS_KEY_LAYOUT_STATUS._value.get() == 1.0
    metrics.REDIS_KEY_LAYOUT_STATUS.set(0.0)
    assert metrics.REDIS_KEY_LAYOUT_STATUS._value.get() == 0.0
    # Exposition shape — unlabelled gauge, no {label} suffix.
    exposition = generate_latest().decode()
    assert "# TYPE openstudio_operator_redis_key_layout_status gauge" in exposition
    assert (
        "\nopenstudio_operator_redis_key_layout_status 0.0" in exposition
    )


# --- Issue #254 — sustained-window elapsed seconds Gauge ------------------------


def test_stall_window_elapsed_seconds_gauge_round_trip():
    """Issue #254 acceptance: ``openstudio_operator_stall_window_elapsed_seconds``
    Gauge accepts arbitrary float values (the metric is set in
    ``run_stall_tick`` after ``tracker.observe()``) and is reset to 0 on
    a broken-condition tick. Unlabelled Gauge — one series, no
    cardinality growth. The exposition shape mirrors ``resque_workers_
    seen_max``: bare ``<name> <value>`` line, no labels.
    """
    metrics.STALL_WINDOW_ELAPSED_SECONDS.set(123.5)
    assert metrics.STALL_WINDOW_ELAPSED_SECONDS._value.get() == 123.5
    metrics.STALL_WINDOW_ELAPSED_SECONDS.set(0.0)
    assert metrics.STALL_WINDOW_ELAPSED_SECONDS._value.get() == 0.0
    exposition = generate_latest().decode()
    assert "# TYPE openstudio_operator_stall_window_elapsed_seconds gauge" in exposition


# --- Issue #312 — freshness timestamp gauges -------------------------------------


def test_stall_window_fresh_gauge_round_trip():
    """Issue #312 acceptance: ``openstudio_operator_stall_window_fresh``
    Gauge accepts Unix-epoch float values (the metric is set to
    ``time.time()`` in ``run_stall_tick`` immediately after the
    ``STALL_WINDOW_ELAPSED_SECONDS.set(...)`` sequence on both the
    holding and broken paths). Unlabelled Gauge — one series, no
    cardinality growth. Mirrors the issue #254 round-trip pattern; the
    exposition shape is the bare ``<name> <value>`` line.
    """
    metrics.STALL_WINDOW_FRESH.set(1_700_000_000.5)
    assert metrics.STALL_WINDOW_FRESH._value.get() == 1_700_000_000.5
    metrics.STALL_WINDOW_FRESH.set(0.0)
    assert metrics.STALL_WINDOW_FRESH._value.get() == 0.0
    exposition = generate_latest().decode()
    assert "# TYPE openstudio_operator_stall_window_fresh gauge" in exposition


def test_resque_queue_depth_fresh_gauge_round_trip():
    """Issue #312 acceptance: ``openstudio_operator_resque_queue_depth_fresh``
    Gauge accepts Unix-epoch float values (the metric is set to
    ``time.time()`` in ``_stall_condition_holds`` immediately after every
    successful ``queue_depths()`` call). Unlabelled Gauge — one series,
    process-wide (the Resque read site is unique); bounded cardinality.
    The exposition shape mirrors :data:`STALL_WINDOW_FRESH`: bare
    ``<name> <value>`` line, no labels.
    """
    metrics.RESQUE_QUEUE_DEPTH_FRESH.set(1_700_000_001.25)
    assert metrics.RESQUE_QUEUE_DEPTH_FRESH._value.get() == 1_700_000_001.25
    metrics.RESQUE_QUEUE_DEPTH_FRESH.set(0.0)
    assert metrics.RESQUE_QUEUE_DEPTH_FRESH._value.get() == 0.0
    exposition = generate_latest().decode()
    assert "# TYPE openstudio_operator_resque_queue_depth_fresh gauge" in exposition


def test_freshness_gauges_decouple_from_data_gauges_on_failure():
    """Issue #312 acceptance: the data gauges can hold a stale prior-tick
    value while the freshness gauges carry the last-successful-update
    timestamp — that gap is exactly the staleness signal dashboards
    alert on. The data/fresh pair must NOT be coupled through a shared
    setter; each is independent and only the freshness gauges advance
    on the "successful read" path. Verified here by setting a non-zero
    data value, then a FRESH timestamp, and asserting both
    independently — the staleness computation ``time() - fresh`` does
    not depend on the data value at all.
    """
    metrics.STALL_WINDOW_ELAPSED_SECONDS.set(120.0)
    metrics.STALL_WINDOW_FRESH.set(1_700_000_000.0)
    assert metrics.STALL_WINDOW_ELAPSED_SECONDS._value.get() == 120.0
    assert metrics.STALL_WINDOW_FRESH._value.get() == 1_700_000_000.0

    # Resetting only the freshness gauge (simulating "we lost visibility")
    # leaves the data gauge holding its prior value — the exact failure
    # mode issue #312 fixes.
    metrics.STALL_WINDOW_FRESH.set(0.0)
    assert metrics.STALL_WINDOW_ELAPSED_SECONDS._value.get() == 120.0  # stale!
    assert metrics.STALL_WINDOW_FRESH._value.get() == 0.0  # fresh=0 → dashboards alert


# --- Issue #255 — kopf.event emission-failure counter --------------------------


def test_events_emit_failures_counter_increments_per_reason():
    """Issue #255 acceptance: ``openstudio_operator_events_emit_failures_total``
    counter increments per ``reason`` inside the try/except wrapping the
    ``kopf.event`` call in :meth:`EventEmitter.emit`. Same ``reason``
    vocabulary as ``events_emitted_total`` so the two can be rate-
    correlated on a dashboard. Mirrors the labelled-counter pattern
    from #117 / #171 / #237 / #239.
    """
    counter = metrics.EVENTS_EMIT_FAILURES_TOTAL
    baseline = _counter_total(counter)
    for reason in (
        "AnalysisSoftStopped",
        "AnalysisEscalated",
        "DatapointRequeued",
        "DatapointRequeueExhausted",
        "WorkerRecycled",
        "WebBackgroundRestarted",
        "ResqueKeyLayoutUnknown",
    ):
        counter.labels(namespace="test-ns", name="test-cr", reason=reason).inc()
    after = _counter_total(counter)
    assert after - baseline == 7.0


def test_events_emit_failures_counter_exposition_uses_reason_label():
    """Issue #255 — verified exposition shape: each ``reason`` value is
    its own labelled series. Pinning the label key (``reason``) means a
    future refactor that silently renames the label is caught at CI."""
    counter = metrics.EVENTS_EMIT_FAILURES_TOTAL
    counter.labels(
        namespace="test-ns", name="test-cr", reason="ExpositionShapeProbe"
    ).inc()
    exposition = generate_latest().decode()
    assert (
        'openstudio_operator_events_emit_failures_total{name="test-cr",namespace="test-ns",reason="ExpositionShapeProbe"}'
        in exposition
    )
