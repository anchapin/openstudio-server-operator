"""Drift gates for the #468 monitoring artifacts (PrometheusRule + Grafana).

Issue #468 shipped the machine-readable alerting/dashboard surface for the
/metrics endpoint: ``deploy/prometheustrule.yaml`` (canonical alert
expressions transcribed from the ``metrics.py`` docstrings and the README
metrics table) and ``deploy/grafana-dashboard.json`` (the four OSCM timers'
action counters, tick-duration histograms, Resque queue gauges + freshness).

The invariant these tests enforce: **every metric family referenced by the
shipped rules / dashboard panels exists in the canonical inventory**
(``tests/_metrics_inventory.py``, issue #406) — the same single source of
truth ``test_metrics_endpoint.py`` and ``test_walk_metrics_registry.py``
compare the live registry against. A metrics-family rename therefore fails
CI here (the alert/dashboard still names the old family) instead of
silently shipping a rule that never matches a series.

Scope guard (#485): these tests deliberately do NOT pin the full deploy/
file inventory — the deploy-inventory CI guard is owned by #485. Only the
two #468 artifacts and their referenced families are covered here.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

# Import the canonical family inventory the same way
# tests/test_metrics_family_prose_claim.py reaches the endpoint module:
# put the tests dir on sys.path and import by module name, so the drift
# gate reuses the exact tuples the registry-walk tests compare against
# (issue #406 — one tuple to update, one failure message).
_TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_DIR))
_metrics_inventory = pytest.importorskip(
    "_metrics_inventory",
    reason="tests/_metrics_inventory.py must be importable for the #468 artifact gate",
)
EXPECTED_COUNTER_FAMILIES = _metrics_inventory.EXPECTED_COUNTER_FAMILIES
EXPECTED_GAUGE_FAMILIES = _metrics_inventory.EXPECTED_GAUGE_FAMILIES
EXPECTED_HISTOGRAM_FAMILIES = _metrics_inventory.EXPECTED_HISTOGRAM_FAMILIES

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"
PROMETHEUSRULE_PATH = DEPLOY / "prometheustrule.yaml"
DASHBOARD_PATH = DEPLOY / "grafana-dashboard.json"

#: Every family the inventory knows about — the membership set for both
#: artifacts' extracted references.
ALL_KNOWN_FAMILIES = frozenset(
    EXPECTED_COUNTER_FAMILIES + EXPECTED_GAUGE_FAMILIES + EXPECTED_HISTOGRAM_FAMILIES
)

#: PromQL family identifier shape: ``openstudio_operator_`` + lowercase
#: words/digits. Stops at ``{`` / ``[`` / ``}`` / whitespace / parens, so a
#: labelled selector or range vector is split correctly.
_FAMILY_RE = re.compile(r"openstudio_operator_[a-z0-9_]+")

#: Histogram series suffixes the exposition adds beyond the base family
#: (``<family>_count`` / ``<family>_bucket`` / ``<family>_sum``). The
#: inventory pins the BASE families, so extracted references are normalised
#: by stripping these before the membership check.
_HISTOGRAM_SUFFIXES = ("_count", "_bucket", "_sum")


def _normalise_family(raw: str) -> str:
    for suffix in _HISTOGRAM_SUFFIXES:
        if raw.endswith(suffix):
            return raw[: -len(suffix)]
    return raw


def _referenced_families(*texts: str) -> set[str]:
    """Extract + normalise every family name mentioned in the given texts."""
    found: set[str] = set()
    for text in texts:
        found.update(_normalise_family(m.group(0)) for m in _FAMILY_RE.finditer(text))
    return found


def _load_prometheusrule() -> dict:
    docs = list(yaml.safe_load_all(PROMETHEUSRULE_PATH.read_text()))
    return next(doc for doc in docs if doc and doc.get("kind") == "PrometheusRule")


def _prometheusrule_alerts() -> list[dict]:
    rule = _load_prometheusrule()
    alerts: list[dict] = []
    for group in rule["spec"]["groups"]:
        for entry in group["rules"]:
            if "alert" in entry:
                alerts.append(entry)
    return alerts


def _load_dashboard() -> dict:
    return json.loads(DASHBOARD_PATH.read_text())


def _dashboard_exprs(dashboard: dict) -> list[str]:
    exprs: list[str] = []
    for panel in dashboard.get("panels", []):
        for target in panel.get("targets", []):
            expr = target.get("expr")
            if expr:
                exprs.append(expr)
    return exprs


def test_prometheustrule_parses_with_expected_shape():
    """The manifest is a well-formed monitoring.coreos.com/v1 PrometheusRule.

    Namespace matches the operator's (rules live beside the scrape target),
    and the metadata carries the kube-prometheus-stack default-pickup label
    documented in the manifest header — the two things a silent mis-apply
    would get wrong.
    """
    rule = _load_prometheusrule()
    assert rule["apiVersion"] == "monitoring.coreos.com/v1"
    assert rule["kind"] == "PrometheusRule"
    metadata = rule["metadata"]
    assert metadata["namespace"] == "openstudio-server"
    labels = metadata["labels"]
    assert labels.get("release") == "prometheus", (
        "kube-prometheus-stack default ruleSelector pickup label missing — "
        "see the manifest header for the rename-your-release note"
    )
    groups = rule["spec"]["groups"]
    assert len(groups) >= 1
    assert all("name" in group and "rules" in group for group in groups)


def test_prometheusrule_referenced_families_exist_in_inventory():
    """Every family named in any rule expr exists in _metrics_inventory.

    This is the #468 acceptance-criterion CI half: rename a family in
    metrics.py + the inventory without updating the shipped rules and this
    fails, instead of shipping an alert that matches no series.
    """
    alerts = _prometheusrule_alerts()
    assert alerts, "PrometheusRule shipped with zero alert rules"
    referenced = _referenced_families(*(alert["expr"] for alert in alerts))
    assert referenced, "no openstudio_operator_* family referenced by any rule expr"
    unknown = referenced - ALL_KNOWN_FAMILIES
    assert not unknown, (
        f"PrometheusRule exprs reference families missing from "
        f"tests/_metrics_inventory.py: {sorted(unknown)}"
    )


def test_prometheustrule_covers_the_issue_468_minimum_alert_set():
    """The issue's minimum alert coverage is present, family by family.

    Handler tick failures, REST outcome=exception (via the histogram's
    _count series), singleton conflict, metrics-server bind, deferred-drop
    rate, key-layout status, stall-window accumulation, and both #312
    freshness gauges. The #469 heartbeat gauge joins the minimum set (the
    dead-scheduler alert); the #306 prune counter LEFT it — the #470
    follow-up (landed with #469) rekeyed that alert onto kube-state-metrics
    ``kube_job_status_failed`` because ``rate()`` over the prune pod's
    per-pod-lifetime counter is mathematically meaningless (see the group
    comment in the manifest).
    """
    alerts = _prometheusrule_alerts()
    referenced = _referenced_families(*(alert["expr"] for alert in alerts))
    required = {
        "openstudio_operator_handler_tick_failures_total",
        "openstudio_operator_rest_request_duration_seconds",
        "openstudio_operator_singleton_election_total",
        "openstudio_operator_singleton_loser_skips_total",
        "openstudio_operator_metrics_server_bound",
        "openstudio_operator_warnings_deferred_dropped_total",
        "openstudio_operator_redis_key_layout_status",
        "openstudio_operator_stall_window_elapsed_seconds",
        # NOT openstudio_operator_prune_tick_failures_total — the prune
        # alert was rekeyed onto kube_job_status_failed (#470 follow-up).
        "openstudio_operator_resque_queue_depth_fresh",
        "openstudio_operator_stall_window_fresh",
        "openstudio_operator_handler_last_tick_timestamp",
    }
    missing = required - referenced
    assert not missing, f"required #468 alert families not covered: {sorted(missing)}"
    # The REST-degraded alert must specifically key on outcome="exception".
    rest_alerts = [
        alert
        for alert in alerts
        if "openstudio_operator_rest_request_duration_seconds_count" in alert["expr"]
    ]
    assert rest_alerts, "no alert consumes the REST histogram _count series"
    assert any(
        'outcome="exception"' in alert["expr"] for alert in rest_alerts
    ), 'REST alert must select outcome="exception"'


def test_prometheusrule_freshness_alerts_use_time_minus_idiom():
    """Both #312 staleness alerts + the #469 heartbeat use ``time() - x``.

    The docstrings pin the idiom; a hand-rolled variant (e.g. ``abs(...)``)
    would pass the family check while alerting on the wrong arithmetic.
    The #469 scheduler-heartbeat alert joins the same shape — per-module
    thresholds (3x each module's interval) but identical staleness
    arithmetic.
    """
    exprs = [alert["expr"] for alert in _prometheusrule_alerts()]
    assert any(
        expr.startswith("time() - openstudio_operator_resque_queue_depth_fresh")
        for expr in exprs
    ), "queue-depth freshness alert must be `time() - resque_queue_depth_fresh > ...`"
    assert any(
        expr.startswith("time() - openstudio_operator_stall_window_fresh")
        for expr in exprs
    ), "stall-window freshness alert must be `time() - stall_window_fresh > ...`"
    assert any(
        expr.startswith("time() - openstudio_operator_handler_last_tick_timestamp")
        for expr in exprs
    ), "heartbeat staleness alert must be `time() - handler_last_tick_timestamp ... > ...`"


def test_grafana_dashboard_parses_with_templated_datasource():
    """The dashboard JSON parses, holds 8-14 panels, and is DS-templated.

    ``${DS_PROMETHEUS}`` + a matching ``__inputs`` entry is the standard
    import-time datasource binding; per-panel uids must agree with it so
    the dashboard works on any Grafana without hardcoding a datasource uid.
    """
    dashboard = _load_dashboard()
    panels = dashboard.get("panels", [])
    assert 8 <= len(panels) <= 14, f"panel count {len(panels)} outside the 8-14 band"
    inputs = {entry.get("name") for entry in dashboard.get("__inputs", [])}
    assert "DS_PROMETHEUS" in inputs, "__inputs must declare DS_PROMETHEUS"
    for panel in panels:
        uid = panel.get("datasource", {}).get("uid")
        assert uid == "${DS_PROMETHEUS}", (
            f"panel {panel.get('title')!r} datasource uid {uid!r} is not "
            f"${{DS_PROMETHEUS}}"
        )
        assert panel.get("title"), "every panel needs a title"
        assert panel.get("type"), "every panel needs a type"


def test_grafana_dashboard_references_action_counter_families():
    """The four OSCM timers' action counters all render on the dashboard.

    soft_stops / workers_recycled / worker_pods_evicted (labelled by their
    outcome/trigger counters) + analyses_deleted — the issue's explicit
    dashboard minimum.
    """
    dashboard = _load_dashboard()
    referenced = _referenced_families(*_dashboard_exprs(dashboard))
    action_counters = {
        "openstudio_operator_soft_stops_total",
        "openstudio_operator_workers_recycled_total",
        "openstudio_operator_worker_pods_evicted_total",
        "openstudio_operator_analyses_deleted_total",
    }
    missing = action_counters - referenced
    assert not missing, f"dashboard misses action-counter families: {sorted(missing)}"
    # The labelled action counters must be split by their label dimension
    # (the #309 outcome/trigger dashboards), not rendered as one flat sum.
    exprs = _dashboard_exprs(dashboard)
    assert any(
        "sum by (outcome)" in expr and "soft_stops_total" in expr for expr in exprs
    ), "soft-stops panel must break out the outcome label"
    assert any(
        "sum by (trigger)" in expr and "workers_recycled_total" in expr for expr in exprs
    ), "recycles panel must break out the trigger label"


def test_grafana_dashboard_referenced_families_exist_in_inventory():
    """Dashboard panels only reference canonical inventory families.

    Same drift gate as the PrometheusRule side: a family rename that misses
    the dashboard fails here instead of rendering empty panels in prod.
    """
    dashboard = _load_dashboard()
    exprs = _dashboard_exprs(dashboard)
    assert exprs, "dashboard references no Prometheus expressions at all"
    referenced = _referenced_families(*exprs)
    assert referenced, "no openstudio_operator_* family referenced by any panel"
    unknown = referenced - ALL_KNOWN_FAMILIES
    assert not unknown, (
        f"dashboard exprs reference families missing from "
        f"tests/_metrics_inventory.py: {sorted(unknown)}"
    )


def test_operator_rbac_grants_no_prometheusrules_verbs():
    """The operator Role must NOT gain prometheusrules verbs (#468 scope).

    The PrometheusRule is applied by the cluster admin, never by the
    operator: granting the operator's namespaced Role
    monitoring.coreos.com/prometheusrules verbs would let a compromised
    operator rewrite its own alerting. This pins that boundary at CI level
    (defense against a future "helpful" RBAC widening).
    """
    docs = list(yaml.safe_load_all((DEPLOY / "rbac.yaml").read_text()))
    role = next(doc for doc in docs if doc and doc.get("kind") == "Role")
    for rule in role["rules"]:
        assert "monitoring.coreos.com" not in rule.get("apiGroups", []), (
            "operator Role must not touch monitoring.coreos.com"
        )
        assert "prometheusrules" not in rule.get("resources", []), (
            "operator Role must not hold prometheusrules verbs (#468)"
        )
