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

import re
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


# ---------------------------------------------------------------------------
# Issue #160: string-field validation on the Deployment-target fields and
# serverUrl. These fields were plain ``type: string`` with no constraint, so
# a CR-write user could redirect the worker recycler / web_background restart
# at an arbitrary Deployment, or point the REST client at an off-cluster
# endpoint. The CRD now declares a DNS-1035 ``pattern`` plus a CEL enum on the
# target fields, and an in-cluster-URL ``pattern`` on ``serverUrl``.
#
# jsonschema/CEL are not executed here — the API server owns that. These tests
# apply the declared ``pattern`` with ``re`` (the same ECMA-262-compatible
# subset the API server uses for these expressions) and assert the CEL rules
# are structurally present.
# ---------------------------------------------------------------------------

TARGET_FIELDS = ("targetWorkerDeployment", "targetWebBackgroundDeployment")
ALLOWED_TARGETS = ("worker", "web-background")


def _spec_field(field: str) -> dict:
    """Return the JSON Schema for a top-level ``spec.<field>``."""
    return SPEC_SCHEMA["properties"][field]


def _matches_pattern(value: str, field_schema: dict) -> bool:
    """Return True if ``value`` satisfies the field's declared ``pattern``.
    A field with no pattern accepts everything — which is exactly the
    pre-#160 bug, so the absence of a pattern is itself a failure."""
    pattern = field_schema.get("pattern")
    assert pattern, "field is missing a `pattern` constraint (issue #160)"
    return re.search(pattern, value) is not None


def _cel_rules(field_schema: dict) -> list[dict]:
    rules = field_schema.get("x-kubernetes-validations")
    assert rules, "field is missing x-kubernetes-validations (issue #160)"
    assert isinstance(rules, list) and rules, "x-kubernetes-validations must be a non-empty list"
    return rules


def test_target_deployment_fields_declare_dns1035_pattern():
    """Issue #160 acceptance: both Deployment-target fields carry the
    DNS-1035 pattern. Without it the API server accepts any string,
    including a namespaced path like ``kube-system/coredns``."""
    for field in TARGET_FIELDS:
        schema = _spec_field(field)
        assert schema.get("pattern") == r"^[a-z]([-a-z0-9]*[a-z0-9])?$", (
            f"spec.{field} must declare the DNS-1035 pattern (issue #160); "
            f"got {schema.get('pattern')!r}"
        )


def test_target_deployment_rejects_namespaced_path():
    """``kube-system/coredns`` is the headline escalation from the issue: a
    slash-bearing value that names a Deployment outside the operator's
    intended blast radius. DNS-1035 has no slash, so the pattern rejects it."""
    for field in TARGET_FIELDS:
        schema = _spec_field(field)
        assert not _matches_pattern("kube-system/coredns", schema), (
            f"spec.{field}: 'kube-system/coredns' must be rejected by the pattern"
        )


def test_target_deployment_rejects_uppercase():
    """``WEB`` is not a legal DNS-1035 label — Kubernetes object names are
    lowercase. Accepting it would produce a patch call against a name the
    API server can never resolve, masking the misconfiguration as a
    runtime 404 instead of an apply-time rejection."""
    for field in TARGET_FIELDS:
        schema = _spec_field(field)
        assert not _matches_pattern("WEB", schema), (
            f"spec.{field}: 'WEB' must be rejected by the DNS-1035 pattern"
        )


def test_target_deployment_accepts_the_two_managed_deployments():
    """The pattern must not be so strict that it rejects the legitimate
    values — ``worker`` and ``web-background`` are the Deployments the
    operator actually manages."""
    for field in TARGET_FIELDS:
        schema = _spec_field(field)
        for allowed in ALLOWED_TARGETS:
            assert _matches_pattern(allowed, schema), (
                f"spec.{field}: {allowed!r} is a managed Deployment and must be accepted"
            )


def test_target_deployment_has_cel_enum_rule():
    """Issue #160 acceptance: a CEL rule pins the value to the two managed
    Deployments. The pattern alone still allows e.g. ``coredns`` — the CEL
    enum is what closes the redirect. jsonschema does not execute CEL, so
    this verifies the rule is structurally present and names both values."""
    for field in TARGET_FIELDS:
        rules = _cel_rules(_spec_field(field))
        assert any(
            "self in" in r["rule"]
            and "'worker'" in r["rule"]
            and "'web-background'" in r["rule"]
            for r in rules
        ), f"spec.{field}: no CEL enum rule pinning to worker/web-background; got {rules!r}"


def test_target_deployment_cel_message_cites_issue_160():
    """A rejected ``kubectl apply`` must give the operator a searchable
    breadcrumb back to the originating design discussion."""
    for field in TARGET_FIELDS:
        rules = _cel_rules(_spec_field(field))
        assert any("issue #160" in r["message"].lower() for r in rules), (
            f"spec.{field}: CEL rule message must reference 'issue #160'; got {rules!r}"
        )


def test_server_url_rejects_non_http_scheme():
    """Issue #160: ``ftp://web`` must be rejected. The REST client speaks
    HTTP only; any other scheme is either a typo or an attempt to steer the
    operator at a non-HTTP listener."""
    schema = _spec_field("serverUrl")
    assert not _matches_pattern("ftp://web", schema), (
        "spec.serverUrl: 'ftp://web' must be rejected — http(s) only"
    )


def test_server_url_rejects_off_cluster_host():
    """Issue #160: ``https://evil.example.com/attack`` must be rejected.
    An unconstrained serverUrl is an SSRF/exfiltration surface — the
    operator would happily poll an attacker-controlled endpoint and act on
    whatever analysis documents it returned."""
    schema = _spec_field("serverUrl")
    assert not _matches_pattern("https://evil.example.com/attack", schema), (
        "spec.serverUrl: off-cluster host 'evil.example.com' must be rejected"
    )


def test_server_url_accepts_in_cluster_forms():
    """The pattern must still accept every URL shape a real deployment
    uses: the bare Service name, the namespaced form, the full
    ``svc.cluster.local`` FQDN, and an explicit port/path."""
    schema = _spec_field("serverUrl")
    for url in (
        "http://web",
        "http://web:80",
        "https://web.openstudio-server",
        "http://web.openstudio-server.svc",
        "http://web.openstudio-server.svc.cluster.local",
        "http://web.openstudio-server.svc.cluster.local:80/analyses.json",
    ):
        assert _matches_pattern(url, schema), (
            f"spec.serverUrl: in-cluster URL {url!r} must be accepted"
        )


def test_server_url_has_cel_scheme_rule():
    """A CEL rule reinforces the http(s)-only contract with a readable
    apply-time message (the raw pattern mismatch is opaque to operators)."""
    rules = _cel_rules(_spec_field("serverUrl"))
    assert any("http" in r["rule"] for r in rules), (
        f"spec.serverUrl: no CEL rule asserting an http(s) scheme; got {rules!r}"
    )
    assert any("issue #160" in r["message"].lower() for r in rules), (
        f"spec.serverUrl: CEL rule message must reference 'issue #160'; got {rules!r}"
    )
