"""Smoke tests for the Prometheus /metrics endpoint (issue #17)."""

import re
import socket
import subprocess
import sys
import textwrap
from contextlib import closing
from importlib.metadata import PackageNotFoundError, version

import pytest
import requests
from prometheus_client import Counter, Gauge, Histogram, generate_latest

#: Canonical source: ``tests/_metrics_inventory.py`` (issue #406). The
#: import below also binds the three tuples as module attributes of this
#: file — ``tests/test_metrics_family_prose_claim`` reaches them via
#: ``import test_metrics_endpoint``. Update the tuples THERE, not here.
from _metrics_inventory import (
    EXPECTED_COUNTER_FAMILIES,
    EXPECTED_GAUGE_FAMILIES,
    EXPECTED_HISTOGRAM_FAMILIES,
)
from openstudio_operator import metrics
from openstudio_operator.metrics import start_metrics_server


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
    # `# TYPE` family line after the first observation. Issue #472 makes
    # ANALYSIS_DATAPOINT_COUNT a labelled Histogram (``view``), so the
    # pre-touch below carries the label — same pattern as the #308
    # labelled histograms. Keeps the family-existence assertion
    # self-contained (mirrors the labelled-counter pattern from #117).
    metrics.ANALYSIS_DATAPOINT_COUNT.labels(view="analyses_per_tick").observe(1)
    # Issue #308 — labelled Histograms need at least one labelled
    # observation before the family line is exposed. Pre-touch each
    # labelled histogram so the family-existence assertion is
    # self-contained for every declared family — same pattern as
    # issue #117's labelled-counter pre-touch.
    metrics.HANDLER_TICK_DURATION_SECONDS.labels(
        module="__metrics_test_sentinel__"
    ).observe(0.1)
    metrics.REST_REQUEST_DURATION_SECONDS.labels(
        method="GET", outcome="200"
    ).observe(0.1)
    # Issue #488 — same pre-touch for the two dependency-latency
    # histograms (operation / verb label sets).
    metrics.REDIS_REQUEST_DURATION_SECONDS.labels(
        operation="__metrics_test_sentinel__"
    ).observe(0.1)
    metrics.KUBE_API_REQUEST_DURATION_SECONDS.labels(
        verb="__metrics_test_sentinel__"
    ).observe(0.1)
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
    # Issue #783 — same pattern for the consecutive-failure streak counter.
    metrics.HANDLER_CONSECUTIVE_FAILURE_STREAK_TOTAL.labels(
        namespace=SENTINEL_NAMESPACE,
        name=SENTINEL_NAME,
        module="__metrics_test_sentinel__",
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
    # Issue #403 — pre-touch the per-tick singleton-guard loser skip
    # counter. Labelled by (module, namespace, name); the module
    # vocabulary is the wrapped handler's __name__ (the four OSCM timer
    # module names, same as handler_tick_failures_total).
    metrics.SINGLETON_LOSER_SKIPS_TOTAL.labels(
        module="__metrics_test_sentinel__",
        namespace=SENTINEL_NAMESPACE,
        name=SENTINEL_NAME,
    ).inc()
    # Issue #255 — pre-touch the kopf.event emission-failure counter.
    # ``reason`` label vocabulary matches ``events_emitted_total`` and,
    # post-#311, the (namespace, name) labels carry the same CR identity.
    metrics.EVENTS_EMIT_FAILURES_TOTAL.labels(
        namespace=SENTINEL_NAMESPACE,
        name=SENTINEL_NAME,
        reason="__metrics_test_sentinel__",
    ).inc()
    # Issue #306 — pre-touch the storage-prune CronJob's skip-tick failure
    # counter. The three-value vocabulary (cr_list_failure |
    # runtime_failure | redis_url_empty) mirrors the three bump sites in
    # prune_entrypoint.main() (two skip-tick branches + the #392 exit-3
    # guard); the
    # CronJob pod exposes the same exposition format on port 9090.
    metrics.PRUNE_TICK_FAILURES_TOTAL.labels(reason="__metrics_test_sentinel__").inc()
    # Issue #471 — pre-touch the REST retry-attempt counter. Labelled by
    # ``method`` only (GET | POST | DELETE — same vocabulary as the #308
    # duration histogram; no CR-identity labels, the client is
    # CR-agnostic).
    metrics.REST_RETRIES_TOTAL.labels(method="GET").inc()
    # Issue #309 — pre-touch the four action counters that gained
    # outcome/trigger labels in this PR. Each call creates a labelled
    # series so the family-existence assertion below is self-contained
    # (mirrors the #117 / #171 / #237 / #239 / #255 pattern).
    metrics.SOFT_STOPS_TOTAL.labels(outcome="__metrics_test_sentinel__").inc()
    metrics.STOP_STOPS_TOTAL.labels(outcome="__metrics_test_sentinel__").inc()
    metrics.WORKERS_RECYCLED_TOTAL.labels(trigger="__metrics_test_sentinel__").inc()
    metrics.WORKER_PODS_EVICTED_TOTAL.labels(outcome="__metrics_test_sentinel__").inc()
    metrics.ANALYSES_DELETED_TOTAL.labels(outcome="__metrics_test_sentinel__").inc()
    # Issue #782 — pre-touch the archival job failure counter. Labelled by
    # ``namespace``; cardinality is bounded by the one-namespace-per-deploy
    # model (D05) so the series vocabulary stays bounded.
    metrics.ARCHIVAL_JOBS_FAILED_TOTAL.labels(namespace=SENTINEL_NAMESPACE).inc()
    # Issue #784 — pre-touch the cross-handler composite outage counter.
    # Labelled by ``module_set`` (a hyphenated string of 2+ failing modules,
    # e.g. ``"analysis_sla-datapoint_watchdog"``); cardinality is bounded
    # by the 4 OSCM handler modules (≤15 non-empty subsets).
    metrics.HANDLER_CROSS_HANDLER_OUTAGE_TOTAL.labels(module_set="__metrics_test_sentinel__").inc()
    # Issue #238 — pre-touch the labelled ``resque_queue_depth`` Gauge
    # so the family line is exposed alongside the unlabelled #44, #253,
    # #254 gauges. The labelled form (``{queue="..."}``) is then asserted
    # below alongside the bare-form unlabelled gauges.
    metrics.RESQUE_QUEUE_DEPTH.labels(queue="__metrics_test_sentinel__").set(0)
    # Issue #469 — pre-touch the labelled scheduler-heartbeat Gauge so
    # the family's sample series is exposed. Labelled by ``module``
    # (the four OSCM timer module names, same vocabulary as
    # ``handler_tick_failures_total`` / the #308 duration histogram).
    metrics.HANDLER_LAST_TICK_TIMESTAMP.labels(
        module="__metrics_test_sentinel__"
    ).set(0)
    # Issue #492 — pre-touch the four config-state posture gauges so
    # each family's sample series is exposed. All four are labelled by
    # (namespace, name) — CR identity, the #311 convention; cardinality
    # is bounded by the singleton guard (D05).
    metrics.DRY_RUN_ACTIVE.labels(
        namespace=SENTINEL_NAMESPACE, name=SENTINEL_NAME
    ).set(0)
    metrics.SERVER_URL_SET.labels(
        namespace=SENTINEL_NAMESPACE, name=SENTINEL_NAME
    ).set(0)
    metrics.REDIS_URL_SET.labels(
        namespace=SENTINEL_NAMESPACE, name=SENTINEL_NAME
    ).set(0)
    metrics.AUTO_SOFT_STOP_ENABLED.labels(
        namespace=SENTINEL_NAMESPACE, name=SENTINEL_NAME
    ).set(0)
    # Issue #489 — pre-touch the labelled status-map size Gauge so the
    # family's sample series is exposed. Labelled by (namespace, name,
    # map_name) — CR identity (the #311 convention) + the four .status
    # map keys (same vocabulary as status_map_caps_total).
    metrics.STATUS_MAP_ENTRIES.labels(
        namespace=SENTINEL_NAMESPACE,
        name=SENTINEL_NAME,
        map_name="__metrics_test_sentinel__",
    ).set(0)
    # Issue #504 — pre-touch the fleet-identity gauge's sentinel series
    # (the #117 convention). The REAL series is already set at metrics
    # import time — importlib.metadata resolves against the editable
    # install in the dev/CI venv — so the family is exposed regardless;
    # the sentinel keeps this test self-contained in a bare interpreter
    # and the real series' label-key shape is pinned below via regex
    # (its label VALUES are environment-dependent: distribution version
    # + interpreter).
    metrics.BUILD_INFO.labels(
        version="__metrics_test_sentinel__",
        python_version="__metrics_test_sentinel__",
    ).set(1)
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
        elif name == "openstudio_operator_handler_consecutive_failure_streak_total":
            assert (
                'openstudio_operator_handler_consecutive_failure_streak_total{'
                f'module="__metrics_test_sentinel__",'
                f'name="{SENTINEL_NAME}",'
                f'namespace="{SENTINEL_NAMESPACE}"'
                "}"
                in response.text
            )
        elif name == "openstudio_operator_handler_cross_handler_outage_total":
            assert (
                'openstudio_operator_handler_cross_handler_outage_total{'
                'module_set="__metrics_test_sentinel__"}'
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
        elif name == "openstudio_operator_singleton_loser_skips_total":
            # Issue #403 — labelled by (module, namespace, name);
            # exposition writes labels alphabetically by key.
            assert (
                'openstudio_operator_singleton_loser_skips_total{module="__metrics_test_sentinel__",'
                f'name="{SENTINEL_NAME}",'
                f'namespace="{SENTINEL_NAMESPACE}"'
                "}"
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
        elif name == "openstudio_operator_prune_tick_failures_total":
            # Issue #306 — labelled by `reason` (cr_list_failure | runtime_failure).
            assert (
                'openstudio_operator_prune_tick_failures_total{reason="__metrics_test_sentinel__"}'
                in response.text
            )
        elif name == "openstudio_operator_soft_stops_total":
            assert (
                'openstudio_operator_soft_stops_total{outcome="__metrics_test_sentinel__"}'
                in response.text
            )
        elif name == "openstudio_operator_stops_total":
            assert (
                'openstudio_operator_stops_total{outcome="__metrics_test_sentinel__"}'
                in response.text
            )
        elif name == "openstudio_operator_workers_recycled_total":
            assert (
                'openstudio_operator_workers_recycled_total{trigger="__metrics_test_sentinel__"}'
                in response.text
            )
        elif name == "openstudio_operator_worker_pods_evicted_total":
            assert (
                'openstudio_operator_worker_pods_evicted_total{outcome="__metrics_test_sentinel__"}'
                in response.text
            )
        elif name == "openstudio_operator_analyses_deleted_total":
            assert (
                'openstudio_operator_analyses_deleted_total{outcome="__metrics_test_sentinel__"}'
                in response.text
            )
        elif name == "openstudio_operator_archival_jobs_failed_total":
            # Issue #782 — labelled by namespace; cardinality bounded by D05.
            assert (
                'openstudio_operator_archival_jobs_failed_total{namespace="__metrics_test_sentinel_ns__"}'
                in response.text
            )
        elif name == "openstudio_operator_rest_retries_total":
            assert (
                'openstudio_operator_rest_retries_total{method="GET"}'
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
        elif name == "openstudio_operator_metrics_server_bound":
            # Issue #393 — labelled by (addr, port); the label VALUES depend
            # on which bind attempt was first in this pytest process (this
            # test's own start, or an earlier handlers-package import that
            # bound 0.0.0.0:9090), and the VALUE is 1.0 unless that first
            # attempt failed (e.g. port 9090 occupied on a dev laptop). Pin
            # the label KEYS (alphabetical: addr < port) + a 0.0/1.0 value
            # via regex; the deterministic success/failure paths live in the
            # subprocess tests below.
            assert re.search(
                r'openstudio_operator_metrics_server_bound'
                r'\{addr="[^"]+",port="[0-9]+"\} [01]\.0',
                response.text,
            )
        elif name == "openstudio_operator_handler_last_tick_timestamp":
            # Issue #469 — labelled by ``module`` (the four OSCM timer
            # module names, same vocabulary as the failure counter).
            assert (
                'openstudio_operator_handler_last_tick_timestamp'
                '{module="__metrics_test_sentinel__"}'
                in response.text
            )
        elif name in {
            # Issue #492 — the four config-state posture gauges share
            # one shape: labelled by (namespace, name), exposition
            # writes labels alphabetically (name < namespace).
            "openstudio_operator_dry_run_active",
            "openstudio_operator_server_url_set",
            "openstudio_operator_redis_url_set",
            "openstudio_operator_auto_soft_stop_enabled",
        }:
            assert (
                f'{name}{{name="{SENTINEL_NAME}",'
                f'namespace="{SENTINEL_NAMESPACE}"}}'
                in response.text
            )
        elif name == "openstudio_operator_status_map_entries":
            # Issue #489 — labelled by (namespace, name, map_name);
            # exposition writes labels alphabetically
            # (map_name < name < namespace).
            assert (
                'openstudio_operator_status_map_entries{map_name="__metrics_test_sentinel__",'
                f'name="{SENTINEL_NAME}",'
                f'namespace="{SENTINEL_NAMESPACE}"'
                "}"
                in response.text
            )
        elif name == "openstudio_operator_build_info":
            # Issue #504 — fleet-identity gauge; the real series is set
            # at metrics import time with environment-dependent label
            # values (distribution version + interpreter), so pin the
            # label KEYS (alphabetical: python_version < version) and
            # the constant 1.0 via regex — the deterministic label-VALUE
            # test lives in
            # test_build_info_gauge_version_label_non_empty_when_installed.
            assert re.search(
                r"openstudio_operator_build_info"
                r'\{python_version="[^"]+",version="[^"]+"\} 1\.0',
                response.text,
            )
        else:
            assert f"\n{name} " in response.text
    # Issue #179: histogram exposed alongside counters and the gauge. The
    # pre-touch above creates the sentinel SERIES for the labelled counter.
    # Issue #472 makes this histogram labelled (``view``) — it also only
    # emits its `# TYPE` line after the first labelled observation.
    # Pre-touch it here so the family-existence assertion is self-contained
    # (mirrors the labelled-counter pattern).
    metrics.ANALYSIS_DATAPOINT_COUNT.labels(view="analyses_per_tick").observe(1)
    # Issue #308 — labelled Histograms (handler_tick_duration_seconds,
    # rest_request_duration_seconds) need at least one labelled observation
    # before the family line is exposed. Pre-touch each labelled histogram
    # so the family-existence assertion is self-contained — same pattern
    # as issue #117's labelled-counter pre-touch. The labelled form is
    # asserted below alongside the bare-form unlabelled histogram.
    metrics.HANDLER_TICK_DURATION_SECONDS.labels(
        module="__metrics_test_sentinel__"
    ).observe(0.1)
    metrics.REST_REQUEST_DURATION_SECONDS.labels(
        method="GET", outcome="200"
    ).observe(0.1)
    # Issue #488 — pre-touch the two dependency-latency histograms so
    # their labelled family lines are exposed (same #117 pattern).
    metrics.REDIS_REQUEST_DURATION_SECONDS.labels(
        operation="__metrics_test_sentinel__"
    ).observe(0.1)
    metrics.KUBE_API_REQUEST_DURATION_SECONDS.labels(
        verb="__metrics_test_sentinel__"
    ).observe(0.1)
    response = requests.get(f"http://127.0.0.1:{port}/metrics", timeout=5)
    for name in _declared_histogram_families():
        assert f"# TYPE {name} histogram" in response.text
        if name == "openstudio_operator_handler_tick_duration_seconds":
            # prometheus_client emits labels in alphabetical order
            # (``le`` < ``module``); pin that shape so a future
            # client-side change to label ordering is caught here.
            assert (
                'openstudio_operator_handler_tick_duration_seconds_bucket{le="0.1",module="__metrics_test_sentinel__"}'
                in response.text
            )
        elif name == "openstudio_operator_analysis_datapoint_count":
            # Issue #472 — labelled by ``view``; prometheus_client emits
            # labels alphabetically (``le`` < ``view``). The observe(1)
            # pre-touch above lands in the le="5" bucket.
            assert (
                'openstudio_operator_analysis_datapoint_count_bucket'
                '{le="5.0",view="analyses_per_tick"}'
                in response.text
            )
        elif name == "openstudio_operator_rest_request_duration_seconds":
            assert (
                'openstudio_operator_rest_request_duration_seconds_bucket{le="0.1",method="GET",outcome="200"}'
                in response.text
            )
        elif name == "openstudio_operator_redis_request_duration_seconds":
            # Issue #488 — labelled by ``operation``; labels are
            # alphabetical (``le`` < ``operation``).
            assert (
                'openstudio_operator_redis_request_duration_seconds_bucket'
                '{le="0.1",operation="__metrics_test_sentinel__"}'
                in response.text
            )
        elif name == "openstudio_operator_kube_api_request_duration_seconds":
            # Issue #488 — labelled by ``verb``; labels are alphabetical
            # (``le`` < ``verb``).
            assert (
                'openstudio_operator_kube_api_request_duration_seconds_bucket'
                '{le="0.1",verb="__metrics_test_sentinel__"}'
                in response.text
            )

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


# --- Issue #309 — action-counter outcome / trigger labels -----------------------


def test_workers_recycled_counter_increments_per_trigger():
    """Issue #309 acceptance: ``openstudio_operator_workers_recycled_total``
    counter increments per trigger at the recycle site in
    ``run_recycler_tick``. The vocabulary is the two-string return of
    ``_armed_trigger`` — pinned here so a future refactor that drops the
    label (or that adds a third trigger without expanding the pin) is
    caught at CI rather than at the on-call's Grafana board. Mirrors the
    labelled-counter pattern from #117 / #171 / #237 / #239 / #255.
    """
    from openstudio_operator.handlers.worker_recycler import (
        TRIGGER_ANALYSIS_COMPLETED,
        TRIGGER_INTERVAL_ELAPSED,
    )

    counter = metrics.WORKERS_RECYCLED_TOTAL
    baseline = _counter_total(counter)
    for trigger in (TRIGGER_ANALYSIS_COMPLETED, TRIGGER_INTERVAL_ELAPSED):
        counter.labels(trigger=trigger).inc()
    after = _counter_total(counter)
    assert after - baseline == 2.0


def test_workers_recycled_counter_exposition_uses_trigger_label():
    """Issue #309 — verified exposition shape: the ``trigger`` label is
    present at the /metrics surface. Pinning the label key here means a
    future refactor that silently renames the label (e.g. to
    ``recycle_trigger``) is caught at CI rather than when the on-call
    rewrites a dashboard panel to match."""
    counter = metrics.WORKERS_RECYCLED_TOTAL
    counter.labels(trigger="ExpositionShapeProbe").inc()
    exposition = generate_latest().decode()
    assert (
        'openstudio_operator_workers_recycled_total{trigger="ExpositionShapeProbe"}'
        in exposition
    )


def test_worker_pods_evicted_counter_increments_per_outcome():
    """Issue #309 acceptance: ``openstudio_operator_worker_pods_evicted_total``
    counter increments per outcome at the escalation site in
    ``_escalate_analysis``. The vocabulary is the four-slot
    ``escalationOutcome`` enum (evicted | evicted-partial | no-matching-pods
    | dry-run) — pinned here so a future refactor that drops the label (or
    that introduces a new outcome without expanding the pin) is caught at
    CI. Mirrors the labelled-counter pattern from #117 / #171 / #237 / #239
    / #255.
    """
    from openstudio_operator.handlers.analysis_sla import (
        ESCALATION_DRY_RUN,
        ESCALATION_EVICTED,
        ESCALATION_EVICTED_PARTIAL,
        ESCALATION_NO_MATCH,
    )

    counter = metrics.WORKER_PODS_EVICTED_TOTAL
    baseline = _counter_total(counter)
    for outcome in (
        ESCALATION_EVICTED,
        ESCALATION_EVICTED_PARTIAL,
        ESCALATION_NO_MATCH,
        ESCALATION_DRY_RUN,
    ):
        counter.labels(outcome=outcome).inc()
    after = _counter_total(counter)
    assert after - baseline == 4.0


def test_worker_pods_evicted_counter_exposition_uses_outcome_label():
    """Issue #309 — verified exposition shape: the ``outcome`` label is
    present at the /metrics surface. Pinning the label key here means a
    future refactor that silently renames the label is caught at CI."""
    counter = metrics.WORKER_PODS_EVICTED_TOTAL
    counter.labels(outcome="ExpositionShapeProbe").inc()
    exposition = generate_latest().decode()
    assert (
        'openstudio_operator_worker_pods_evicted_total{outcome="ExpositionShapeProbe"}'
        in exposition
    )


def test_soft_stops_counter_increments_per_outcome():
    """Issue #309 acceptance: ``openstudio_operator_soft_stops_total``
    counter increments per outcome at the soft-stop branch in
    ``run_sla_tick``. The vocabulary is the two-slot soft-stop outcome
    (issued | dry-run) — pinned here so a future refactor that drops the
    label (or that introduces a new outcome without expanding the pin)
    is caught at CI. Mirrors the labelled-counter pattern from #117 /
    #171 / #237 / #239 / #255.
    """
    from openstudio_operator.handlers.analysis_sla import (
        _OUTCOME_DRY_RUN,
        _OUTCOME_ISSUED,
    )

    counter = metrics.SOFT_STOPS_TOTAL
    baseline = _counter_total(counter)
    for outcome in (_OUTCOME_ISSUED, _OUTCOME_DRY_RUN):
        counter.labels(outcome=outcome).inc()
    after = _counter_total(counter)
    assert after - baseline == 2.0


def test_soft_stops_counter_exposition_uses_outcome_label():
    """Issue #309 — verified exposition shape: the ``outcome`` label is
    present at the /metrics surface. Pinning the label key here means a
    future refactor that silently renames the label is caught at CI."""
    counter = metrics.SOFT_STOPS_TOTAL
    counter.labels(outcome="ExpositionShapeProbe").inc()
    exposition = generate_latest().decode()
    assert (
        'openstudio_operator_soft_stops_total{outcome="ExpositionShapeProbe"}'
        in exposition
    )


def test_analyses_deleted_counter_increments_per_outcome():
    """Issue #309 acceptance: ``openstudio_operator_analyses_deleted_total``
    counter increments per outcome at the delete-commit site in
    ``retention.delete_verified_analysis``. Current code only exercises
    the ``deleted`` outcome (the only branch that performs the REST
    cascade) — pinned here so the existing label is not silently dropped
    by a future refactor. Mirrors the labelled-counter pattern from
    #117 / #171 / #237 / #239 / #255.
    """
    counter = metrics.ANALYSES_DELETED_TOTAL
    baseline = _counter_total(counter)
    counter.labels(outcome="deleted").inc()
    after = _counter_total(counter)
    assert after - baseline == 1.0


def test_analyses_deleted_counter_exposition_uses_outcome_label():
    """Issue #309 — verified exposition shape: the ``outcome`` label is
    present at the /metrics surface. Pinning the label key here means a
    future refactor that silently renames the label is caught at CI."""
    counter = metrics.ANALYSES_DELETED_TOTAL
    counter.labels(outcome="ExpositionShapeProbe").inc()
    exposition = generate_latest().decode()
    assert (
        'openstudio_operator_analyses_deleted_total{outcome="ExpositionShapeProbe"}'
        in exposition
    )


# --- Issue #308 — handler tick + REST round-trip Histograms ---------------------


def test_handler_tick_duration_histogram_exposes_per_module_series():
    """Issue #308 acceptance: ``openstudio_operator_handler_tick_duration_seconds``
    Histogram exposes one labelled series per module — same vocabulary as
    ``handler_tick_failures_total`` so a dashboard can correlate latency
    with failure rate on the same dimension. Driving ``.observe(...)``
    directly via ``labels(...).observe(...)`` mirrors the labelled-counter
    pattern from #117 and the labelled-histogram pattern this issue
    introduces; the call-site integration is covered end-to-end by
    ``tests/test_handler_boundaries.py::test_handler_wrapper_observe_tick_duration``.
    """
    histogram = metrics.HANDLER_TICK_DURATION_SECONDS
    for module in (
        "analysis_sla",
        "datapoint_watchdog",
        "worker_recycler",
        "web_background_monitor",
    ):
        histogram.labels(module=module).observe(0.1)
    exposition = generate_latest().decode()
    for module in (
        "analysis_sla",
        "datapoint_watchdog",
        "worker_recycler",
        "web_background_monitor",
    ):
        # Each labelled series must register its own _count + _bucket line.
        # The le="0.1" bucket line is the canonical proof the observation
        # landed on the right module's series — pinning the label key here
        # catches a future refactor that drops or renames the ``module``
        # label. prometheus_client emits labels in alphabetical order
        # (``le`` < ``module``) — pin that shape too.
        assert (
            f'openstudio_operator_handler_tick_duration_seconds_count{{module="{module}"}}'
            in exposition
        )
        assert (
            f'openstudio_operator_handler_tick_duration_seconds_bucket{{le="0.1",module="{module}"}}'
            in exposition
        )


def test_rest_request_duration_histogram_exposes_per_method_outcome_series():
    """Issue #308 acceptance: ``openstudio_operator_rest_request_duration_seconds``
    Histogram exposes one labelled series per ``(method, outcome)`` pair.
    The ``outcome`` label vocabulary is ``"200"`` (any successful 2xx/3xx)
    or ``"exception"`` (any raised ``OpenStudioApiError``); the ``method``
    label is the verb the operator actually uses (GET | POST | DELETE).
    A sustained non-zero rate on ``outcome="exception"`` is the canonical
    REST-degraded alert. Driving ``.observe(...)`` directly mirrors the
    labelled-counter pattern from #117 and pins the label cardinality so
    a future refactor that drops the ``outcome`` label is caught at CI.
    """
    histogram = metrics.REST_REQUEST_DURATION_SECONDS
    for method, outcome in (
        ("GET", "200"),
        ("GET", "exception"),
        ("POST", "200"),
        ("POST", "exception"),
        ("DELETE", "200"),
        ("DELETE", "exception"),
    ):
        histogram.labels(method=method, outcome=outcome).observe(0.1)
    exposition = generate_latest().decode()
    for method, outcome in (
        ("GET", "200"),
        ("GET", "exception"),
        ("POST", "200"),
        ("POST", "exception"),
        ("DELETE", "200"),
        ("DELETE", "exception"),
    ):
        # Labels are alphabetical: ``le`` < ``method`` < ``outcome``.
        assert (
            f'openstudio_operator_rest_request_duration_seconds_count{{method="{method}",outcome="{outcome}"}}'
            in exposition
        )
        assert (
            f'openstudio_operator_rest_request_duration_seconds_bucket{{le="0.1",method="{method}",outcome="{outcome}"}}'
            in exposition
        )


def test_rest_request_duration_histogram_uses_issue_308_bucket_set():
    """Issue #308 — verified exposition shape: the histogram bucket list
    must be the canonical ``(0.05, 0.1, 0.5, 1, 2, 5)`` set. Pinning the
    bucket boundaries catches a future refactor that silently broadens or
    narrows the resolution at the healthy band — a coarser bucket set
    hides a 100 ms → 500 ms degradation; a finer set is just cardinality
    growth on the right tail."""
    histogram = metrics.REST_REQUEST_DURATION_SECONDS
    # Drive a single observation so the bucket lines are emitted, then
    # assert the ``le`` boundaries match the issue body's canonical set.
    histogram.labels(method="GET", outcome="200").observe(0.1)
    exposition = generate_latest().decode()
    for le in ("0.05", "0.1", "0.5", "1.0", "2.0", "5.0"):
        assert (
            f'openstudio_operator_rest_request_duration_seconds_bucket{{le="{le}",method="GET",outcome="200"}}'
            in exposition
        )
    # The ``+Inf`` bucket is implicit in prometheus_client — it is always
    # present, even with no observations. Confirm it too so a future
    # refactor that strips the default bucket is caught at CI.
    assert (
        'openstudio_operator_rest_request_duration_seconds_bucket{le="+Inf",method="GET",outcome="200"}'
        in exposition
    )


def test_handler_tick_duration_histogram_uses_issue_308_bucket_set():
    """Issue #308 — verified exposition shape: the histogram bucket list
    must be the canonical ``(0.05, 0.1, 0.5, 1, 2, 5, 10, 30)`` set.
    Pinning the bucket boundaries catches a future refactor that clips
    the right tail (a degraded poll that masks the retry window is the
    exact data point the 30 s cap preserves)."""
    histogram = metrics.HANDLER_TICK_DURATION_SECONDS
    histogram.labels(module="analysis_sla").observe(0.1)
    exposition = generate_latest().decode()
    for le in ("0.05", "0.1", "0.5", "1.0", "2.0", "5.0", "10.0", "30.0"):
        assert (
            f'openstudio_operator_handler_tick_duration_seconds_bucket{{le="{le}",module="analysis_sla"}}'
            in exposition
        )
    assert (
        'openstudio_operator_handler_tick_duration_seconds_bucket{le="+Inf",module="analysis_sla"}'
        in exposition
    )


# --- Issue #488 — Redis + kube-api dependency duration Histograms ----------------


def test_redis_request_duration_histogram_exposes_per_operation_series():
    """Issue #488 acceptance: ``openstudio_operator_redis_request_duration_
    seconds`` Histogram exposes one labelled series per ``operation``. The
    vocabulary is the pinned four-value set (llen | smembers | scan |
    exists — exists added by issue #688 for the layout validator's
    O(1) verdict probes) — pinned here so a future refactor that drops
    the label (or invents a fifth value without expanding the pin) is
    caught at CI. The call-site integration (fakeredis happy paths
    asserting real observation) lives in ``tests/test_redis_client.py``."""
    histogram = metrics.REDIS_REQUEST_DURATION_SECONDS
    for operation in ("llen", "smembers", "scan", "exists"):
        histogram.labels(operation=operation).observe(0.1)
    exposition = generate_latest().decode()
    for operation in ("llen", "smembers", "scan", "exists"):
        # Labels are alphabetical (``le`` < ``operation``); pin the shape
        # so a label rename is caught here, not on the on-call's board.
        assert (
            f'openstudio_operator_redis_request_duration_seconds_count{{operation="{operation}"}}'
            in exposition
        )
        assert (
            f'openstudio_operator_redis_request_duration_seconds_bucket{{le="0.1",operation="{operation}"}}'
            in exposition
        )


def test_kube_api_request_duration_histogram_exposes_per_verb_series():
    """Issue #488 acceptance: ``openstudio_operator_kube_api_request_
    duration_seconds`` Histogram exposes one labelled series per ``verb``.
    The vocabulary is the four Kubernetes verbs the wrapped chokepoints
    issue (get | patch | delete | list). The call-site integrations
    (status_store RMW get+patch, rolling_restart patch) live in
    ``tests/test_status_store.py`` / ``tests/test_k8s_rolling_restart.py``."""
    histogram = metrics.KUBE_API_REQUEST_DURATION_SECONDS
    for verb in ("get", "patch", "delete", "list"):
        histogram.labels(verb=verb).observe(0.1)
    exposition = generate_latest().decode()
    for verb in ("get", "patch", "delete", "list"):
        assert (
            f'openstudio_operator_kube_api_request_duration_seconds_count{{verb="{verb}"}}'
            in exposition
        )
        assert (
            f'openstudio_operator_kube_api_request_duration_seconds_bucket{{le="0.1",verb="{verb}"}}'
            in exposition
        )


def test_dependency_duration_histograms_use_issue_308_bucket_set():
    """Issue #488 — both dependency histograms share the canonical #308
    REST bucket set ``(0.05, 0.1, 0.5, 1, 2, 5)``: Redis reads are
    sub-ms-to-ms and kube calls ms-to-s on a healthy cluster, so one
    spread keeps resolution at the healthy band for both AND makes the
    three per-dependency histograms directly comparable on one dashboard
    axis (the issue's attribution ask). Pinning the boundaries catches a
    future refactor that broadens or narrows the resolution silently."""
    for histogram, label_kw in (
        (metrics.REDIS_REQUEST_DURATION_SECONDS, {"operation": "llen"}),
        (metrics.KUBE_API_REQUEST_DURATION_SECONDS, {"verb": "get"}),
    ):
        histogram.labels(**label_kw).observe(0.1)
    exposition = generate_latest().decode()
    for le in ("0.05", "0.1", "0.5", "1.0", "2.0", "5.0"):
        assert (
            f'openstudio_operator_redis_request_duration_seconds_bucket{{le="{le}",operation="llen"}}'
            in exposition
        )
        assert (
            f'openstudio_operator_kube_api_request_duration_seconds_bucket{{le="{le}",verb="get"}}'
            in exposition
        )
    assert (
        'openstudio_operator_redis_request_duration_seconds_bucket{le="+Inf",operation="llen"}'
        in exposition
    )
    assert (
        'openstudio_operator_kube_api_request_duration_seconds_bucket{le="+Inf",verb="get"}'
        in exposition
    )


# --- Issue #393 — metrics-server bind-outcome Gauge ------------------------------


def test_metrics_server_bound_gauge_success_path():
    """Issue #393 acceptance: a successful bind advances the gauge to 1.0
    with the configured ``(addr, port)`` labels, and the series appears in
    the LIVE /metrics scrape. Runs in a subprocess (the same pattern as
    ``test_handlers_import_starts_metrics_server``): ``start_metrics_server``
    is process-idempotent, so the first-attempt semantics can only be
    exercised deterministically in a fresh process."""
    code = textwrap.dedent(
        """
        import socket
        from contextlib import closing

        import requests
        from openstudio_operator.metrics import start_metrics_server

        with closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            free_port = sock.getsockname()[1]
        port = start_metrics_server(port=free_port, addr="127.0.0.1")
        assert port == free_port, (port, free_port)

        response = requests.get(f"http://127.0.0.1:{port}/metrics", timeout=5)
        assert response.status_code == 200, response.status_code
        assert "# TYPE openstudio_operator_metrics_server_bound gauge" in response.text
        # Labels are alphabetical (addr < port); pin the exact series + value.
        assert (
            f'openstudio_operator_metrics_server_bound{{addr="127.0.0.1",'
            f'port="{free_port}"}} 1.0' in response.text
        )
        """
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_metrics_server_bound_gauge_bind_failure_path():
    """Issue #393 acceptance: a bind failure (port already in use) sets the
    gauge to 0.0 with the attempted ``(addr, port)`` labels AND the WARNING
    log still fires. The 0.0 is asserted from the default REGISTRY
    exposition — the durable record for the post-mortem, since a dead bind
    means THIS pod's /metrics is unscrapeable (the self-referential edge
    documented in the README row)."""
    code = textwrap.dedent(
        """
        import logging
        import socket
        from contextlib import closing

        from prometheus_client import generate_latest
        from openstudio_operator.metrics import start_metrics_server

        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        capture = _Capture(level=logging.WARNING)
        metrics_logger = logging.getLogger("openstudio_operator.metrics")
        metrics_logger.addHandler(capture)
        metrics_logger.setLevel(logging.WARNING)

        with closing(socket.socket()) as blocker:
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            busy_port = blocker.getsockname()[1]

            result = start_metrics_server(port=busy_port, addr="127.0.0.1")
            assert result is None, result

        exposition = generate_latest().decode()
        assert (
            f'openstudio_operator_metrics_server_bound{{addr="127.0.0.1",'
            f'port="{busy_port}"}} 0.0' in exposition
        )
        # The WARNING log still fires (the gauge supplements the log, it
        # does not replace it).
        warnings = [
            r for r in records if r.levelno == logging.WARNING
        ]
        assert any(
            "Cannot serve /metrics" in r.getMessage() for r in warnings
        ), [r.getMessage() for r in warnings]
        """
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_metrics_server_bound_gauge_never_retouched_after_first_attempt():
    """Issue #393 acceptance: the gauge records the FIRST bind attempt only.
    A failed first attempt (port in use → 0.0) followed by a successful
    retry on a different port must NOT flip the gauge to 1.0 nor add a
    second labelled series — first-attempt semantics, per the issue body."""
    code = textwrap.dedent(
        """
        import socket
        from contextlib import closing

        from prometheus_client import generate_latest
        from openstudio_operator.metrics import start_metrics_server

        with closing(socket.socket()) as blocker:
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            busy_port = blocker.getsockname()[1]
            assert start_metrics_server(port=busy_port, addr="127.0.0.1") is None

        with closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            free_port = sock.getsockname()[1]
        # Retry on a free port binds successfully...
        assert start_metrics_server(port=free_port, addr="127.0.0.1") == free_port

        exposition = generate_latest().decode()
        series = [
            line
            for line in exposition.splitlines()
            if line.startswith("openstudio_operator_metrics_server_bound{")
        ]
        # ...but the gauge still records the FIRST attempt only: exactly one
        # series, the failed one, at 0.0 — no 1.0 series for the retry.
        assert len(series) == 1, series
        assert (
            f'openstudio_operator_metrics_server_bound{{addr="127.0.0.1",'
            f'port="{busy_port}"}} 0.0' in series[0]
        )
        """
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# --- Issue #504 — build_info fleet-identity Gauge --------------------------------


def test_build_info_gauge_version_label_non_empty_when_installed():
    """Issue #504 acceptance: ``openstudio_operator_build_info`` carries a
    non-empty ``version`` label WHEN the distribution is installed — the
    dev/CI venv installs the package editable, so ``importlib.metadata``
    resolves at test time and the import-time ``.labels(...).set(1)`` in
    ``metrics.py`` stamps the real version into the exposition. The
    ``unknown`` fallback only fires in a bare interpreter where the
    distribution is absent; skipped there so this assertion is exactly
    the issue's "non-empty WHEN installed" criterion."""
    try:
        installed = version("openstudio-server-operator")
    except PackageNotFoundError:
        pytest.skip("openstudio-server-operator distribution not installed")
    assert installed != ""
    exposition = generate_latest().decode()
    # Labels are alphabetical (python_version < version); the python
    # label is sys.version.split()[0] — read it the same way metrics.py
    # does so the two cannot drift apart.
    assert (
        "openstudio_operator_build_info"
        f'{{python_version="{sys.version.split()[0]}",version="{installed}"}} 1.0'
        in exposition
    )


# --- Issue #491 — singleton-guard wrap-count Gauge --------------------------------


def test_singleton_wrapped_handlers_gauge_round_trip():
    """Issue #491 acceptance: ``openstudio_operator_singleton_wrapped_
    handlers`` Gauge accepts arbitrary values (the real set site is the
    end of ``singleton.install_singleton_guard``, asserted end-to-end in
    ``tests/test_singleton_guard.py`` — this test pins the exposition
    shape). Unlabelled Gauge — one series, no cardinality growth; the
    bare ``<name> <value>`` line with no ``{label}`` suffix, mirroring
    the #253 / #254 / #312 unlabelled-gauge conventions. The 0.0 reset
    is the alert semantics: ``== 0`` on a booted operator that expects
    timers is the silent-unwrap failure mode (kopf internals shifted so
    the gate wrapped nothing — D05 enforcement disabled while the
    operator appears healthy)."""
    metrics.SINGLETON_WRAPPED_HANDLERS.set(4.0)
    assert metrics.SINGLETON_WRAPPED_HANDLERS._value.get() == 4.0
    metrics.SINGLETON_WRAPPED_HANDLERS.set(0.0)
    assert metrics.SINGLETON_WRAPPED_HANDLERS._value.get() == 0.0
    exposition = generate_latest().decode()
    assert "# TYPE openstudio_operator_singleton_wrapped_handlers gauge" in exposition
    assert "\nopenstudio_operator_singleton_wrapped_handlers 0.0" in exposition
