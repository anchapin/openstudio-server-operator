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

Issue #581 adds the second invariant, scoped to the #469 scheduler
heartbeat alert: the expr's ``module="..."`` selector set must be
set-equal to the OSCM timer population (source of truth
``_oscm_handlers.REGISTRY``, issue #250) and each clause's threshold must
be 3x the module's poll interval — so a fifth timer added by the routine
onboarding 5-step pattern, or an interval change in ``_constants.py``,
fails CI until the alert is extended/retuned (the same doc-drift-guard
discipline as the metrics inventory).

Issue #645 adds the third invariant, scoped to the #569 prune
absence-of-success alert: the expr must fullmatch the canonical
CronJob-recency two-arm shape (recency on
``kube_cronjob_status_last_successful_time`` + a never-succeeded
``unless`` bootstrap arm) and must not accumulate
``kube_job_status_succeeded`` — retained Job history (``successfulJobs
HistoryLimit: 3``) pins that sum at >= 1 forever, deadening the alert in
exactly the no-event modes it exists for. A scenario simulation evaluates
the parsed arms, including the #645 regression ("3 retained successes,
no new Job for 1h → MUST be armed").

Scope guard (#485): these tests deliberately do NOT pin the full deploy/
file inventory — the deploy-inventory CI guard is owned by #485. Only the
two #468 artifacts and their referenced families are covered here.
"""

from __future__ import annotations

import ast
import importlib
import inspect
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


# ---------------------------------------------------------------------------
# Issue #581 — heartbeat alert module-set + threshold drift gate.
#
# The #469 alert ``OpenStudioOperatorHandlerHeartbeatStale`` hardcodes one
# ``time() - openstudio_operator_handler_last_tick_timestamp{module="X"} > T``
# clause per OSCM timer. Nothing else ties that clause set to the actual
# timer population or the thresholds to the poll constants: a fifth timer
# (routine per the onboarding 5-step pattern) would have NO staleness
# alert and CI stayed green, and an interval change in ``_constants.py``
# silently desynchronised the 3x math (too tight → flappy pages; too loose
# → late page). The tests below fence both directions against
# ``_oscm_handlers.REGISTRY`` (#250) and the handler modules' poll
# constants.
# ---------------------------------------------------------------------------

#: Name of the #469 scheduler-heartbeat alert in deploy/prometheustrule.yaml.
_HEARTBEAT_ALERT_NAME = "OpenStudioOperatorHandlerHeartbeatStale"

#: One ``or``-leg of the heartbeat expr: ``time() - <family>{module="X"} > T``.
#: Used with ``fullmatch`` so a hand-edited leg that drifts from the
#: staleness shape (extra arithmetic, renamed family, moved threshold)
#: fails structurally instead of parsing to a wrong-but-plausible clause.
_HEARTBEAT_CLAUSE_RE = re.compile(
    r"time\(\) - openstudio_operator_handler_last_tick_timestamp"
    r'\{module="(?P<module>[^"]+)"\} > (?P<threshold>[0-9]+(?:\.[0-9]+)?)'
)

#: The alert's threshold multiplier, transcribed from the manifest's own
#: comment ("Thresholds are 3x each module's interval") and the #469
#: docstring in metrics.py.
_HEARTBEAT_THRESHOLD_MULTIPLIER = 3


def _heartbeat_alert_expr() -> str:
    """Return the expr of the (unique) heartbeat alert rule."""
    matches = [
        alert["expr"]
        for alert in _prometheusrule_alerts()
        if alert.get("alert") == _HEARTBEAT_ALERT_NAME
    ]
    assert len(matches) == 1, (
        f"expected exactly one {_HEARTBEAT_ALERT_NAME} rule in "
        f"{PROMETHEUSRULE_PATH.name}, found {len(matches)}"
    )
    return matches[0]


def _heartbeat_alert_clauses() -> dict[str, float]:
    """Parse the heartbeat expr into ``{module_label: threshold_seconds}``.

    Every ``or``-separated leg must ``fullmatch`` the canonical staleness
    shape, so the parse doubles as a structure check: an edited expr that
    no longer consists solely of per-module staleness legs fails here.
    """
    clauses: dict[str, float] = {}
    for leg in _heartbeat_alert_expr().split(" or "):
        match = _HEARTBEAT_CLAUSE_RE.fullmatch(leg.strip())
        assert match, (
            f"heartbeat alert leg does not match the canonical "
            f"per-module staleness shape "
            f"`time() - ...handler_last_tick_timestamp{{module=\"X\"}} > N`: {leg!r}"
        )
        clauses[match["module"]] = float(match["threshold"])
    return clauses


#: The production handlers-package prefix. ``_oscm_handlers.REGISTRY`` is
#: process-wide, and some existing tests (e.g. ``tests/test_lenient_api_
#: factories.py``) register fake timers into it WITHOUT cleanup — entries
#: whose ``fn.__module__`` is not under this prefix are test fakes, not
#: the production population the alert fences.
_PRODUCTION_HANDLERS_PREFIX = "openstudio_operator.handlers."


def _oscm_timer_module_intervals() -> dict[str, float]:
    """Derive ``{module_label: poll_interval_seconds}`` for every OSCM timer.

    Source of truth: ``_oscm_handlers.REGISTRY`` (#250) — the same
    declarative registry the singleton guard cross-checks against the kopf
    registry at boot and that
    ``test_singleton_registry_coverage.py::test_python_registry_includes_all_oscm_spawning_handlers``
    pins. For each registered handler:

    * the heartbeat **module label** is AST-extracted from the handler
      module's own ``run_oscm_tick(..., module="...", ...)`` call — the
      exact literal that stamps ``HANDLER_LAST_TICK_TIMESTAMP`` (the
      series the alert selects on; no uniform REGISTRY-key→label
      transformation exists — ``analysis_sla_monitor`` stamps
      ``analysis_sla``, ``zombie_datapoint_watchdog`` stamps
      ``datapoint_watchdog``, the other two are identity — so the call
      site IS the coupling);
    * the **interval** is the handler module's ``POLL_INTERVAL_SECONDS``
      — the ``_constants.py`` constant (issue #165) its
      ``@kopf.timer(interval=...)`` consumes, i.e. the cadence that
      actually runs.

    No hand-maintained mapping table: a fifth timer that registers (#250)
    and wires through ``run_oscm_tick`` joins the expected set
    automatically; one that does NOT (custom wrapper) is caught by the
    assertion below with instructions to extend the fence and the alert.
    """
    from openstudio_operator import _oscm_handlers

    if not _oscm_handlers.REGISTRY:
        # tests/conftest.py imports openstudio_operator.handlers at module
        # level (populating REGISTRY before any test runs); guard the
        # direct-invocation case anyway.
        import openstudio_operator.handlers  # noqa: F401  (import side effect)

    intervals: dict[str, float] = {}
    production_entries = {
        handler_id: fn
        for handler_id, fn in _oscm_handlers.REGISTRY.items()
        if fn.__module__.startswith(_PRODUCTION_HANDLERS_PREFIX)
    }
    assert production_entries, (
        "no production OSCM handlers found in _oscm_handlers.REGISTRY — "
        "the openstudio_operator.handlers import in this helper's guard "
        "did not populate the registry (issue #581)."
    )
    for handler_id, fn in sorted(production_entries.items()):
        handler_module = importlib.import_module(fn.__module__)
        tree = ast.parse(inspect.getsource(handler_module))
        labels = [
            keyword.value.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_oscm_tick"
            for keyword in node.keywords
            if keyword.arg == "module" and isinstance(keyword.value, ast.Constant)
        ]
        assert labels, (
            f"OSCM handler {handler_id!r} ({fn.__module__}) declares no "
            f"run_oscm_tick(module=...) call to derive its heartbeat module "
            f"label from — extend _oscm_timer_module_intervals and the "
            f"{_HEARTBEAT_ALERT_NAME} expr (issue #581), or route the timer "
            f"through run_oscm_tick like the four existing handlers."
        )
        interval = float(handler_module.POLL_INTERVAL_SECONDS)
        for label in labels:
            intervals[label] = interval
    return intervals


def test_heartbeat_alert_module_set_matches_oscm_registry():
    """#581: the heartbeat expr covers exactly the OSCM timer population.

    Set-equality in both directions against the module labels derived from
    ``_oscm_handlers.REGISTRY``: a fifth timer with no alert clause fails
    (the exact scenario #469 was built for — a silently-wedged new timer
    would otherwise have NO staleness alert while CI stays green), and a
    stale clause for a removed/renamed timer fails (the selector would
    match no series, deadening that leg of the alert).
    """
    alert_modules = set(_heartbeat_alert_clauses())
    expected_modules = set(_oscm_timer_module_intervals())
    missing = expected_modules - alert_modules
    stale = alert_modules - expected_modules
    assert not missing, (
        f"OSCM timer module(s) with no heartbeat clause in "
        f"{_HEARTBEAT_ALERT_NAME} (issue #581): {sorted(missing)}. A timer "
        f"without a clause has NO staleness alert — extend the expr with "
        f"`or time() - openstudio_operator_handler_last_tick_timestamp"
        f'{{module="{min(missing)}"}} > '
        f"{_HEARTBEAT_THRESHOLD_MULTIPLIER}x its POLL_INTERVAL_SECONDS`."
    )
    assert not stale, (
        f"heartbeat clause(s) in {_HEARTBEAT_ALERT_NAME} reference module(s) "
        f"not in the OSCM registry (issue #581): {sorted(stale)} — the "
        f"selector matches no series and that leg is dead. Remove or "
        f"re-key the clause."
    )


def test_heartbeat_alert_thresholds_are_three_x_poll_intervals():
    """#581: every heartbeat clause threshold is 3x the module's interval.

    The 3x multiplier is the alert's designed tolerance (three missed
    polls before paging). If a module's ``POLL_INTERVAL_SECONDS`` changes
    in ``_constants.py`` without retuning the expr, the math silently
    desynchronises — too tight → flappy pages, too loose → late page.
    """
    clauses = _heartbeat_alert_clauses()
    intervals = _oscm_timer_module_intervals()
    assert set(clauses) == set(intervals), (
        "module-set mismatch — see "
        "test_heartbeat_alert_module_set_matches_oscm_registry"
    )
    for module in sorted(clauses):
        threshold = clauses[module]
        expected = _HEARTBEAT_THRESHOLD_MULTIPLIER * intervals[module]
        assert threshold == expected, (
            f"heartbeat threshold for module {module!r} is {threshold:g}s but "
            f"{_HEARTBEAT_THRESHOLD_MULTIPLIER}x its POLL_INTERVAL_SECONDS "
            f"({_HEARTBEAT_THRESHOLD_MULTIPLIER} x {intervals[module]:g}) is "
            f"{expected:g}s — retune the {module} clause in "
            f"{_HEARTBEAT_ALERT_NAME} (issue #581)."
        )


# ---------------------------------------------------------------------------
# Issue #645 — PruneJobNoSuccess CronJob-recency drift gate.
#
# The #569 absence-of-success expr accumulated Job objects:
# ``sum(max_over_time(kube_job_status_succeeded[...])) or vector(0) < 1``.
# But storage-cronjob.yaml sets ``successfulJobsHistoryLimit: 3``, KSM
# keeps exporting succeeded=1 for every RETAINED Job object, and the
# CronJob controller only GCs history when it creates a new Job — after
# the first three successes the sum is permanently >= 1 and the alert
# can NEVER fire, in exactly the no-event modes (#569: suspend, wrong
# schedule, controller outage) it was built for. The gate below pins the
# #645 rekey onto CronJob-level recency: the two-arm canonical shape
# (fullmatch, so any hand-edited drift fails structurally — the same
# discipline as the #581 heartbeat clause regex) plus a scenario
# simulation that evaluates the parsed arms against synthetic series,
# including the #645 regression ("3 retained successes, nothing new for
# 1h") and the never-succeeded bootstrap edge case.
# ---------------------------------------------------------------------------

#: Name of the absence-of-success prune alert in deploy/prometheusrule.yaml.
_PRUNE_NO_SUCCESS_ALERT_NAME = "OpenStudioOperatorPruneJobNoSuccess"

#: The canonical two-arm #645 shape: recency on last_successful_time,
#: ``or`` the never-succeeded bootstrap (schedule stale unless a success
#: exists). Used with ``fullmatch`` — thresholds are captured so the
#: scenario simulation derives its semantics from the manifest itself.
_PRUNE_NO_SUCCESS_EXPR_RE = re.compile(
    r"\(time\(\) - kube_cronjob_status_last_successful_time"
    r'\{namespace="openstudio-server", cronjob="openstudio-storage-pruner"\}'
    r" > (?P<success_threshold>[0-9]+)\)"
    r" or \(\(time\(\) - kube_cronjob_status_last_schedule_time"
    r'\{namespace="openstudio-server", cronjob="openstudio-storage-pruner"\}'
    r" > (?P<schedule_threshold>[0-9]+)\)"
    r" unless kube_cronjob_status_last_successful_time"
    r'\{namespace="openstudio-server", cronjob="openstudio-storage-pruner"\}\)'
)

#: 1h = 6 missed */10 schedules (storage-cronjob.yaml), the #569 window.
_PRUNE_NO_SUCCESS_WINDOW_SECONDS = 3600


def _prune_no_success_expr() -> re.Match:
    """Return the fullmatch of the (unique) absence-of-success alert expr."""
    matches = [
        alert["expr"]
        for alert in _prometheusrule_alerts()
        if alert.get("alert") == _PRUNE_NO_SUCCESS_ALERT_NAME
    ]
    assert len(matches) == 1, (
        f"expected exactly one {_PRUNE_NO_SUCCESS_ALERT_NAME} rule in "
        f"{PROMETHEUSRULE_PATH.name}, found {len(matches)}"
    )
    parsed = _PRUNE_NO_SUCCESS_EXPR_RE.fullmatch(matches[0])
    assert parsed, (
        f"{_PRUNE_NO_SUCCESS_ALERT_NAME} expr does not fullmatch the "
        f"canonical #645 CronJob-recency shape "
        f"`(time() - kube_cronjob_status_last_successful_time{{...}} > N) "
        f"or ((time() - kube_cronjob_status_last_schedule_time{{...}} > N) "
        f"unless kube_cronjob_status_last_successful_time{{...}})`: "
        f"{matches[0]!r}"
    )
    return parsed


def _simulate_prune_no_success(
    parsed: re.Match,
    *,
    last_successful_age: float | None,
    last_schedule_age: float | None,
) -> bool:
    """Evaluate the parsed arms; ``None`` means the series is ABSENT.

    Models PromQL semantics faithfully for the two arms: a ``time() - X``
    comparison over an absent series produces no data (False), and the
    ``unless`` RHS suppresses the bootstrap arm whenever a success series
    exists — retained ``kube_job_status_succeeded`` Job history is
    deliberately NOT an input, which is the whole point of #645.
    """
    arm_recency = (
        last_successful_age is not None
        and last_successful_age > float(parsed["success_threshold"])
    )
    arm_bootstrap = (
        last_schedule_age is not None
        and last_schedule_age > float(parsed["schedule_threshold"])
        and last_successful_age is None
    )
    return arm_recency or arm_bootstrap


def test_prune_no_success_expr_matches_cronjob_recency_canonical_shape():
    """#645: the expr is the two-arm CronJob-recency shape, no Job accumulation.

    ``kube_job_status_succeeded`` accumulation is the regression: retained
    history pins that sum at >= 1 forever (the CronJob controller only GCs
    on new-Job creation), so the fullmatch — which admits no variant
    spelling — doubles as the "no Job-object accumulation" fence. Both
    thresholds must equal the #569 1h window / 6 missed */10 schedules.
    """
    parsed = _prune_no_success_expr()
    expr = parsed.group(0)
    assert "kube_job_status_succeeded" not in expr, (
        "PruneJobNoSuccess must not accumulate kube_job_status_succeeded "
        "(issue #645): successfulJobsHistoryLimit (3) retains succeeded "
        "Jobs in the no-event failure modes, pinning the sum at >= 1 so "
        "the alert can never fire"
    )
    assert int(parsed["success_threshold"]) == _PRUNE_NO_SUCCESS_WINDOW_SECONDS
    assert int(parsed["schedule_threshold"]) == _PRUNE_NO_SUCCESS_WINDOW_SECONDS, (
        "both arms must use the 1h window (6 missed */10 schedules, "
        "#569); a shorter bootstrap window would false-fire on fresh "
        "installs, a longer one delays the suspend/no-event page"
    )


def test_prune_no_success_regression_three_retained_successes_still_arms():
    """#645 acceptance criterion: 3 historical successes retained
    (``successfulJobsHistoryLimit: 3``), no new Job scheduled for 1h —
    the alert MUST be armed.

    This is the scenario the old
    ``sum(max_over_time(kube_job_status_succeeded[1h])) or vector(0) < 1``
    could never fire in (the three retained successes pinned the sum at
    >= 1). Under the CronJob-recency rekey the retained Job history is
    not an input at all; the stale ``last_successful_time`` arms the
    alert. The sibling states must NOT arm: healthy cadence, and the 1h
    grace after a success (suspend-with-recent-success must not page
    early).
    """
    parsed = _prune_no_success_expr()
    # THE #645 regression: successes exist (3 retained Job objects keep
    # kube_job_status_succeeded=1 exported forever), last success and last
    # schedule are both >1h stale (suspended / controller outage).
    assert _simulate_prune_no_success(
        parsed, last_successful_age=2 * _PRUNE_NO_SUCCESS_WINDOW_SECONDS,
        last_schedule_age=2 * _PRUNE_NO_SUCCESS_WINDOW_SECONDS,
    ), "3 retained successes + nothing new for >1h must arm the alert (#645)"
    # Healthy cadence: success 5 minutes ago (a */10 schedule that just ran).
    assert not _simulate_prune_no_success(
        parsed, last_successful_age=300, last_schedule_age=300,
    ), "healthy */10 cadence must not arm the alert"
    # Grace: suspended 30 minutes after a success — the 1h window has not
    # elapsed; firing here would flap on every schedule boundary.
    assert not _simulate_prune_no_success(
        parsed, last_successful_age=0.5 * _PRUNE_NO_SUCCESS_WINDOW_SECONDS,
        last_schedule_age=0.5 * _PRUNE_NO_SUCCESS_WINDOW_SECONDS,
    ), "success inside the 1h window must not arm the alert"


def test_prune_no_success_bootstrap_arm_covers_never_succeeded():
    """#645 edge case: ``kube_cronjob_status_last_successful_time`` is
    ABSENT until the CronJob's first success — ``time() - X`` over an
    absent series is no-data, so without the ``unless`` bootstrap arm the
    alert would silently never fire on a fresh install that never
    succeeded (#569 precedent: TRUE absence must fire).

    The bootstrap arm arms on schedule-stale + never-succeeded (suspend
    from day one, wrong schedule, controller outage). It must stay quiet
    on a fresh install still inside its first window, and the ``unless``
    must hand semantics to the recency arm once any success exists (a
    recent success suppresses the bootstrap arm even with a stale
    schedule — the 1h grace).
    """
    parsed = _prune_no_success_expr()
    # Never succeeded, scheduling stopped >1h ago (suspend / outage from
    # day one): bootstrap arm must fire.
    assert _simulate_prune_no_success(
        parsed, last_successful_age=None,
        last_schedule_age=2 * _PRUNE_NO_SUCCESS_WINDOW_SECONDS,
    ), "never-succeeded + schedule stale >1h must arm (bootstrap arm, #645)"
    # Fresh install inside its first window, never succeeded yet: quiet.
    assert not _simulate_prune_no_success(
        parsed, last_successful_age=None, last_schedule_age=300,
    ), "first-hour grace must hold on a never-succeeded fresh install"
    # Success exists but is recent while the schedule is stale (suspended
    # right after a success): the unless suppresses the bootstrap arm and
    # the recency arm honours the grace — no fire until the 1h elapses.
    assert not _simulate_prune_no_success(
        parsed, last_successful_age=300,
        last_schedule_age=2 * _PRUNE_NO_SUCCESS_WINDOW_SECONDS,
    ), "recent success + stale schedule must not arm (unless keeps arms disjoint)"


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
