"""Schema validation tests for the OpenStudioClusterManager CRD (issue #170).

Quantifies the issue #170 acceptance: every numeric policy field must carry
a JSON Schema ``minimum`` constraint consistent with its semantic floor, and
the analysis-level CEL rule must reject ``gracefulStopTimeoutMinutes >=
maxDurationMinutes``.

The CRD is the contract enforced by the API server at ``kubectl apply
--validate=true`` time. ``minimum`` is the OpenAPI/JSON Schema keyword that
makes the API server reject values below the declared floor — its presence
on the field is what makes a CR with ``maxDurationMinutes: 0`` fail to
apply. The CEL rule on ``analysisPolicy`` is the corresponding cross-field
guard (grace period must be strictly less than the SLA budget); jsonschema
does not execute CEL, so we verify the rule is structurally present.

These tests are structural rather than running a full JSON Schema
validator: the contract is the schema itself, and the only thing that
matters is that the constraints are declared with the right values.
"""

from pathlib import Path

import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
CRD = yaml.safe_load((DEPLOY / "crd.yaml").read_text())

# The spec schema — the one ``kubectl apply --validate=true`` runs every CR
# against. ``.metadata``, ``.status`` and the CRD envelope are mechanical
# K8s plumbing and not a useful target for business-rule assertions.
SPEC_SCHEMA = (
    CRD["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
)

# Field paths under spec.* and the minimum value the CRD must enforce.
# Issue #170: floors are semantic, not always >= 1. ``maxAutoRequeues`` and
# ``retentionDays`` are deliberately 0 because the documented sentinels
# ("no auto-requeue", "archive immediately") are the lowest valid settings.
MINIMUM_CONSTRAINTS: dict[tuple[str, str], int] = {
    ("analysisPolicy", "maxDurationMinutes"): 1,
    ("analysisPolicy", "gracefulStopTimeoutMinutes"): 1,
    ("datapointPolicy", "maxDatapointRuntimeMinutes"): 1,
    ("datapointPolicy", "maxAutoRequeues"): 0,
    ("workerPolicy", "recycleWorkerIntervalHours"): 1,
    ("workerPolicy", "minRecycleIntervalMinutes"): 1,
    ("webBackgroundPolicy", "stallWindowMinutes"): 1,
    ("storagePolicy", "retentionDays"): 0,
}


def _field(policy: str, field: str) -> dict:
    """Return the JSON Schema for ``spec.<policy>.<field>``."""
    return SPEC_SCHEMA["properties"][policy]["properties"][field]


def _is_rejected_by_minimum(value: int, field_schema: dict) -> bool:
    """Return True if ``value`` would be rejected by the schema's minimum
    constraint. This is the exact check the API server performs: any integer
    below ``minimum`` is rejected by ``kubectl apply --validate=true``."""
    return "minimum" in field_schema and value < field_schema["minimum"]


# ---------------------------------------------------------------------------
# Structural: minimum constraints exist and equal the documented floor
# ---------------------------------------------------------------------------


def test_every_numeric_policy_field_has_minimum_constraint():
    """Issue #170 acceptance: every numeric policy field has a JSON Schema
    ``minimum`` matching the field's semantic floor. The previous CRD left
    these unset, which let a CR-write user set ``maxDurationMinutes: 0``,
    ``retentionDays: -1``, etc. — see the issue body for the data-loss paths."""
    for (policy, field), expected in MINIMUM_CONSTRAINTS.items():
        schema = _field(policy, field)
        assert "minimum" in schema, (
            f"{policy}.{field} is missing a `minimum` constraint (issue #170)"
        )
        assert schema["minimum"] == expected, (
            f"{policy}.{field} minimum={schema['minimum']}, expected {expected}"
        )


# ---------------------------------------------------------------------------
# Rejection semantics: the declared minimum is what makes the API server
# reject documents below the floor. The tests below assert the schema
# contract that underlies that rejection.
# ---------------------------------------------------------------------------


def test_max_duration_minutes_zero_is_rejected():
    """Issue #170: ``maxDurationMinutes: 0`` makes the SLA monitor soft-stop
    every analysis on its first sighting. Floor is 1 minute. The API server
    rejects this because the schema declares ``minimum: 1``."""
    schema = _field("analysisPolicy", "maxDurationMinutes")
    assert _is_rejected_by_minimum(0, schema), (
        "maxDurationMinutes: 0 must be rejected by the declared minimum"
    )


def test_graceful_stop_timeout_negative_is_rejected():
    """Issue #170: ``gracefulStopTimeoutMinutes: -1`` must be rejected. A
    negative grace period is meaningless (no real-world worker honors a
    "negative seconds" SIGTERM) and most likely indicates a typo or hostile
    CR mutation. Floor is 1 minute."""
    schema = _field("analysisPolicy", "gracefulStopTimeoutMinutes")
    assert _is_rejected_by_minimum(-1, schema), (
        "gracefulStopTimeoutMinutes: -1 must be rejected by the declared minimum"
    )


def test_max_auto_requeues_negative_is_rejected_zero_is_allowed():
    """``maxAutoRequeues: 0`` is a documented valid setting (operators may
    opt to disable auto-requeue entirely); negative would be a bug. The
    floor is 0, not 1."""
    schema = _field("datapointPolicy", "maxAutoRequeues")
    # Zero is the documented floor and must still be accepted
    assert not _is_rejected_by_minimum(0, schema), (
        "maxAutoRequeues: 0 is the documented valid floor; must not be rejected"
    )
    # Negative is rejected
    assert _is_rejected_by_minimum(-1, schema), (
        "maxAutoRequeues: -1 must be rejected by the declared minimum"
    )


def test_retention_days_zero_is_allowed_negative_is_rejected():
    """Issue #170: ``retentionDays: 0`` is the documented "archive
    immediately" sentinel (deliberate — see CRD field description). A
    negative retention widens the eligibility window to the past and creates
    a one-way data-loss path. Floor is 0, not 1."""
    schema = _field("storagePolicy", "retentionDays")
    # Zero is the documented sentinel and must still be accepted
    assert not _is_rejected_by_minimum(0, schema), (
        "retentionDays: 0 is the documented 'archive immediately' sentinel; "
        "must not be rejected"
    )
    # Negative is rejected
    assert _is_rejected_by_minimum(-1, schema), (
        "retentionDays: -1 must be rejected by the declared minimum"
    )


def test_min_recycle_interval_minutes_zero_is_rejected():
    """Issue #170: ``minRecycleIntervalMinutes: 0`` would let the worker
    recycler fire continuously (zero spacing between recycles). Floor is 1."""
    schema = _field("workerPolicy", "minRecycleIntervalMinutes")
    assert _is_rejected_by_minimum(0, schema), (
        "minRecycleIntervalMinutes: 0 must be rejected by the declared minimum"
    )


def test_recycle_worker_interval_hours_zero_is_rejected():
    """Issue #170: ``recycleWorkerIntervalHours: 0`` would force a constant
    worker restart loop. Floor is 1."""
    schema = _field("workerPolicy", "recycleWorkerIntervalHours")
    assert _is_rejected_by_minimum(0, schema), (
        "recycleWorkerIntervalHours: 0 must be rejected by the declared minimum"
    )


def test_stall_window_minutes_zero_is_rejected():
    """Issue #170: ``stallWindowMinutes: 0`` would restart web_background on
    every poll. Floor is 1."""
    schema = _field("webBackgroundPolicy", "stallWindowMinutes")
    assert _is_rejected_by_minimum(0, schema), (
        "stallWindowMinutes: 0 must be rejected by the declared minimum"
    )


def test_max_datapoint_runtime_minutes_zero_is_rejected():
    """Issue #170: ``maxDatapointRuntimeMinutes: 0`` would mark every
    newly-started datapoint as over-runtime on its first sighting. Floor
    is 1 minute."""
    schema = _field("datapointPolicy", "maxDatapointRuntimeMinutes")
    assert _is_rejected_by_minimum(0, schema), (
        "maxDatapointRuntimeMinutes: 0 must be rejected by the declared minimum"
    )


# ---------------------------------------------------------------------------
# CEL x-kubernetes-validations: gracefulStopTimeoutMinutes < maxDurationMinutes
# ---------------------------------------------------------------------------


def test_analysis_policy_has_cel_validation_rule():
    """Issue #170: a CEL ``x-kubernetes-validations`` rule on
    ``analysisPolicy`` must assert that the grace period is strictly less
    than the SLA budget. Without this, escalation is unreachable (the
    analysis is already past the SLA before the grace period starts).

    jsonschema does not execute CEL — that needs the API server. So this
    test only verifies the rule is structurally present: the rule string
    must reference both fields and express the strict-less-than constraint.
    A future CRD edit that silently drops the rule will fail this test."""
    analysis_policy = SPEC_SCHEMA["properties"]["analysisPolicy"]
    rules = analysis_policy.get("x-kubernetes-validations")
    assert rules, "analysisPolicy is missing x-kubernetes-validations (issue #170)"
    assert isinstance(rules, list) and rules, "x-kubernetes-validations must be a non-empty list"

    expected_rule = "self.gracefulStopTimeoutMinutes < self.maxDurationMinutes"
    assert any(
        expected_rule in r["rule"]
        and "gracefulStopTimeoutMinutes" in r["rule"]
        and "maxDurationMinutes" in r["rule"]
        for r in rules
    ), f"no CEL rule asserting {expected_rule!r} found in: {rules!r}"


def test_cel_rule_message_cites_issue_170():
    """The user-facing message MUST cite the issue number so a rejected
    ``kubectl apply`` gives the operator a searchable breadcrumb back to
    the originating design discussion."""
    rules = SPEC_SCHEMA["properties"]["analysisPolicy"].get("x-kubernetes-validations", [])
    expected_rule = "self.gracefulStopTimeoutMinutes < self.maxDurationMinutes"
    matched = [
        r for r in rules
        if expected_rule in r["rule"]
    ]
    assert matched, "no SLA-vs-grace CEL rule present"
    assert any("issue #170" in r["message"].lower() for r in matched), (
        f"CEL rule message must reference 'issue #170'; got: {matched[0]['message']!r}"
    )
