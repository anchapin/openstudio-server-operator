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


def test_server_url_rejects_two_label_public_hostnames_575():
    """Issue #575: a free two-label host is indistinguishable from a public
    TLD domain — ``http://evil.com`` matched the pre-#575 grammar because
    the optional second label was unconstrained. Every multi-label host
    must now terminate in ``.svc`` or ``.svc.cluster.local``; with and
    without ports, and the retired bare service.namespace form, are all
    rejected at apply time."""
    schema = _spec_field("serverUrl")
    for bad in (
        "http://evil.com",
        "https://attacker.dev",
        "http://exfil.io:8080",
        "https://web.openstudio-server",  # retired bare namespace form (#575)
        "http://a.b.c.d.e",
    ):
        assert not _matches_pattern(bad, schema), (
            f"spec.serverUrl: two-label public hostname {bad!r} must be "
            f"rejected (issue #575)"
        )


def test_server_url_accepts_in_cluster_forms():
    """The pattern must still accept every URL shape a real deployment
    uses: the bare Service name, the ``.svc`` short forms, the full
    ``svc.cluster.local`` FQDN, and an explicit port/path. Since #575 the
    bare two-label service.namespace form is NO longer accepted — the
    ``.svc``-suffixed spelling replaces it."""
    schema = _spec_field("serverUrl")
    for url in (
        "http://web",
        "http://web:80",
        "http://web.svc",
        "http://web.svc.cluster.local",
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


# ---------------------------------------------------------------------------
# Issue #390: ``spec.redisUrl`` must constrain the URL to an in-cluster Redis
# Service, mirroring the ``serverUrl`` hardening from #160. An unconstrained
# ``redisUrl`` let a CR-write user redirect the operator's
# ``ReadOnlyRedisClient`` (``src/openstudio_operator/redis_client.py``) at any
# reachable host and coerce the Resque keyspace probe to talk to it
# (SSRF / hostile-Redis exfiltration). The empty-default escape hatch (#116)
# must be preserved — the pattern's ``|^$`` branch is what does it.
#
# jsonschema/CEL are not executed here — the API server owns that. These tests
# apply the declared ``pattern`` with ``re`` (same ECMA-262-compatible subset
# the API server uses) and assert the CEL rules are structurally present.
# ---------------------------------------------------------------------------


_REDIS_URL_BAD = (
    # Issue #390 headline case: a multi-label public hostname that the
    # pre-#390 unconstrained field could reach.
    "redis://:password@attacker.example.com:6379",
    # Public hostname without auth — same exfiltration surface, just less
    # obvious to a human reviewer.
    "redis://attacker.example.com",
    # Wrong scheme: ftp:// is not a Redis listener.
    "ftp://queue:6379",
    # Wrong scheme: http:// would let a CR-write user route the probe to a
    # REST responder that returns crafted JSON to influence queue-depth
    # reasoning.
    "http://queue:6379",
    # Uppercase service name: K8s Service names are lowercase; the API
    # server can never resolve an uppercase name and accepting it would mask
    # the misconfiguration as a runtime 404 instead of an apply-time
    # rejection.
    "redis://QUEUE:6379",
    # Bare scheme, no host: no Service to talk to, no admission.
    "redis://",
    # Issue #476: the TLS scheme inherits the #390 SSRF fence — accepting a
    # public-host rediss:// would re-open the exfiltration surface the
    # pattern exists to close.
    "rediss://attacker.example.com:6379",
    "rediss://:password@attacker.example.com:6379",
)
# Issue #463: URLs with EMBEDDED CREDENTIALS — the helm-recipe shapes that
# were the pre-#463 pattern's headline acceptance cases are now REJECTED.
# The CR spec is stored plaintext in etcd, returned verbatim to every
# get/list principal, and mirrored into GitOps repos; the password belongs
# in a Secret referenced via spec.redisCredentials.secretRef.
_REDIS_URL_EMBEDDED_CREDS = (
    # Empty-user + password (the helm-recipe REDIS_URL shape).
    "redis://:openstudio-rotated@queue:6379",
    "redis://:openstudio-rotated@queue.openstudio-server.svc.cluster.local:6379",
    # user:password form.
    "redis://user:password@queue:6379",
    # Credentialed FQDN with a db selector (the pre-#463
    # test_config_from_spec_camelcase value).
    "redis://:secret@queue.openstudio-server.svc.cluster.local:6379/1",
    # Issue #476: the TLS twin must inherit the #463 no-inline-credentials
    # rule — switching scheme must not become a side door for passwords.
    "rediss://:openstudio-rotated@queue:6379",
    "rediss://user:password@queue:6379",
)
_REDIS_URL_GOOD = (
    # Credential-free bare Service label with port — the post-#463 inline
    # shape (auth-less dev cluster), also used by ``test_prune_entrypoint``
    # and other test fixtures.
    "redis://queue:6379",
    # Two-label short form ending in .svc — the #575 replacement for the
    # retired bare service.namespace spelling.
    "redis://queue.svc:6379",
    # Four-label short FQDN ending in .svc.cluster.local.
    "redis://queue.svc.cluster.local:6379",
    # Three-label namespaced Service form ending in .svc.
    "redis://queue.openstudio-server.svc:6379",
    # Five-label full FQDN ending in .svc.cluster.local — the
    # credential-free twin of the form ``singleton.py``'s #116 guidance
    # documents.
    "redis://queue.openstudio-server.svc.cluster.local:6379",
    # Full FQDN with a Redis db-number selector, credential-free.
    "redis://queue.openstudio-server.svc.cluster.local:6379/1",
)
_REDIS_URL_EMPTY = ""  # documented empty-default escape hatch (#116)

# Issue #476: credential-free TLS shapes — every in-cluster DNS form the
# plaintext tuple covers, with the ``rediss://`` scheme.
_REDIS_URL_TLS_GOOD = (
    "rediss://queue:6379",
    "rediss://queue.svc:6379",
    "rediss://queue.openstudio-server.svc:6379",
    "rediss://queue.openstudio-server.svc.cluster.local:6379",
    "rediss://queue.openstudio-server.svc.cluster.local:6379/1",
)


def test_redis_url_rejects_off_cluster_host():
    """Issue #390 acceptance (a): ``redis://:password@attacker.example.com:6379``
    must be rejected. An unconstrained ``redisUrl`` is an SSRF /
    hostile-Redis exfiltration surface — the operator's
    ``ReadOnlyRedisClient`` would happily poll an attacker-controlled Redis
    and reveal operator timing / scrape the Workload identity / be
    blocked-by-design to mask a real outage."""
    schema = _spec_field("redisUrl")
    assert schema.get("pattern"), "spec.redisUrl is missing a `pattern` constraint (issue #390)"
    for bad in _REDIS_URL_BAD:
        assert not _matches_pattern(bad, schema), (
            f"spec.redisUrl: {bad!r} must be rejected by the in-cluster DNS pattern"
        )


def test_redis_url_rejects_two_label_public_hostnames_575():
    """Issue #575: ``redis://evil.com:6379`` matched the pre-#575 grammar
    (the optional second label was unconstrained), re-opening the #390
    exfiltration surface for two-label public domains. With and without
    ports, both schemes, and the retired bare service.namespace form are
    all rejected at apply time."""
    schema = _spec_field("redisUrl")
    for bad in (
        "redis://evil.com",
        "redis://exfil.io:6379",
        "rediss://attacker.dev:6379",
        "redis://queue.openstudio-server:6379",  # retired bare form (#575)
        "redis://a.b.c.d.e:6379",
    ):
        assert not _matches_pattern(bad, schema), (
            f"spec.redisUrl: two-label public hostname {bad!r} must be "
            f"rejected (issue #575)"
        )


def test_url_fence_host_grammar_shared_across_three_sites_575():
    """Issue #575 anti-drift fence: the two CRD patterns and the Python
    ``SECRET_REDIS_URL_PATTERN`` (the Secret-resolved Redis URL fence in
    ``redis_client.py``, reached via ``client_factory._resolve_redis_url``)
    must agree on the SAME host grammar — accept and reject — so the three
    sites cannot drift apart. Each pattern is probed with its own scheme /
    credential decorations around the identical host matrix."""
    from openstudio_operator.redis_client import SECRET_REDIS_URL_PATTERN

    server_schema = _spec_field("serverUrl")
    redis_schema = _spec_field("redisUrl")
    legal_hosts = (
        "web",
        "web.svc",
        "web.svc.cluster.local",
        "web.openstudio-server.svc",
        "web.openstudio-server.svc.cluster.local",
    )
    illegal_hosts = (
        "evil.com",
        "exfil.io",
        "attacker.dev",
        "web.openstudio-server",  # retired bare namespace form (#575)
        "a.b.c.d.e",
        "web.evil.com",
    )
    for host in legal_hosts:
        assert _matches_pattern(f"http://{host}", server_schema), host
        assert _matches_pattern(f"redis://{host}:6379", redis_schema), host
        assert SECRET_REDIS_URL_PATTERN.match(f"redis://:pw@{host}:6379"), host
    for host in illegal_hosts:
        assert not _matches_pattern(f"http://{host}", server_schema), host
        assert not _matches_pattern(f"redis://{host}:6379", redis_schema), host
        assert not SECRET_REDIS_URL_PATTERN.match(f"redis://:pw@{host}:6379"), host


def test_redis_url_accepts_in_cluster_forms():
    """Issue #390 acceptance (b), post-#463 form: every CREDENTIAL-FREE
    in-cluster DNS shape the helm-recipe + tests use must pass. The pattern
    must not be so strict that it rejects the legitimately-managed URLs —
    but since #463 "legitimate" no longer includes embedded credentials
    (see ``test_redis_url_rejects_embedded_credentials_463``)."""
    schema = _spec_field("redisUrl")
    for good in _REDIS_URL_GOOD:
        assert _matches_pattern(good, schema), (
            f"spec.redisUrl: in-cluster URL {good!r} must be accepted by the pattern"
        )


def test_redis_url_accepts_empty_default_per_116():
    """Issue #390 acceptance (c): ``redisUrl: ""`` (the documented #116
    empty-default escape hatch) must still pass. The pattern's ``|^$``
    alternation branch is what preserves this — without it the API server
    would reject every CR that omitted the field or used the documented
    empty default."""
    schema = _spec_field("redisUrl")
    assert _matches_pattern(_REDIS_URL_EMPTY, schema), (
        "spec.redisUrl: empty string is the #116 documented escape hatch "
        "and must be accepted by the pattern"
    )


def test_redis_url_empty_default_is_preserved():
    """The CRD must still declare ``default: ""`` on ``spec.redisUrl`` —
    the empty default is the entire point of the #116 escape hatch
    (re-introducing a non-empty default would re-introduce the public-facing
    password that #116 deleted). Issue #390 inherits this invariant; a
    future edit that "fills in" the default would be a regression."""
    schema = _spec_field("redisUrl")
    assert schema.get("default") == "", (
        f"spec.redisUrl.default must remain the empty string (issue #116); "
        f"got {schema.get('default')!r}"
    )


def test_redis_url_has_cel_redis_scheme_rule():
    """Issue #390 acceptance: a CEL rule reinforces the ``redis://`` scheme
    with a readable apply-time message (the raw pattern mismatch is opaque
    to operators). The rule also short-circuits to true for the #116
    empty-default escape hatch."""
    rules = _cel_rules(_spec_field("redisUrl"))
    assert any("redis://" in r["rule"] for r in rules), (
        f"spec.redisUrl: no CEL rule asserting the redis:// scheme; got: {rules!r}"
    )
    assert any("issue #390" in r["message"].lower() for r in rules), (
        f"spec.redisUrl: CEL rule message must reference 'issue #390'; got: {rules!r}"
    )


def test_redis_url_accepts_rediss_tls_scheme_476():
    """Issue #476 acceptance: every credential-free in-cluster DNS shape the
    plaintext tuple covers must ALSO pass with the ``rediss://`` TLS scheme —
    TLS-only Redis (Azure Cache / Memorystore / ElastiCache) deployments
    point the operator at the same in-cluster Service shape, just encrypted.
    The ``rediss?`` alternation must not loosen anything else (host fence,
    port/db shape, #116 empty branch — covered by their own tests)."""
    schema = _spec_field("redisUrl")
    for good in _REDIS_URL_TLS_GOOD:
        assert _matches_pattern(good, schema), (
            f"spec.redisUrl: TLS in-cluster URL {good!r} must be accepted "
            f"by the pattern (issue #476)"
        )


def test_redis_url_scheme_alternation_is_exactly_redis_and_rediss_476():
    """Issue #476: the alternation is ``rediss?`` — exactly ``redis://`` and
    ``rediss://``. Degenerate spellings (extra s, missing slash) must NOT
    sneak through the alternation."""
    schema = _spec_field("redisUrl")
    for bad in ("redissss://queue:6379", "redi://queue:6379", "rediss:/queue:6379"):
        assert not _matches_pattern(bad, schema), (
            f"spec.redisUrl: {bad!r} is not a legal scheme spelling and must "
            f"be rejected (issue #476)"
        )


def test_redis_url_cel_scheme_rule_accepts_rediss_476():
    """Issue #476: the CEL scheme rule must carry an explicit
    ``startsWith('rediss://')`` branch — ``rediss://queue`` does NOT start
    with ``redis://`` (the 7th char is ``s``, not ``:``), so without the
    branch the CEL layer would reject what the pattern accepts."""
    rules = _cel_rules(_spec_field("redisUrl"))
    scheme_rules = [r for r in rules if "startsWith" in r["rule"]]
    assert any("startsWith('rediss://')" in r["rule"] for r in scheme_rules), (
        f"spec.redisUrl: no CEL scheme rule covering the rediss:// TLS "
        f"scheme (issue #476); got: {rules!r}"
    )
    assert any(
        "self == '' ||" in r["rule"] and "rediss://" in r["rule"] for r in scheme_rules
    ), (
        "spec.redisUrl: the CEL scheme rule must keep the #116 empty-string "
        "short-circuit while accepting both schemes (issue #476); got: "
        f"{rules!r}"
    )


# ---------------------------------------------------------------------------
# Issue #463: ``spec.redisUrl`` must REJECT embedded credentials — the CR
# spec is stored plaintext in etcd, returned verbatim to any principal with
# get/list on the CR, and typically committed to GitOps repos, so an inline
# password leaks in three persistent places. Credentials move to a Secret
# named by the new ``spec.redisCredentials.secretRef`` (full-URL semantics).
# ---------------------------------------------------------------------------


def test_redis_url_rejects_embedded_credentials_463():
    """Issue #463 acceptance: the tightened pattern rejects every URL shape
    carrying ``@``-userinfo — the empty-user helm-recipe form AND the
    ``user:password`` form. A CR with an embedded credential fails CRD
    validation at apply time (pattern applied here with ``re``, the same
    ECMA-262-compatible subset the API server uses)."""
    schema = _spec_field("redisUrl")
    for bad in _REDIS_URL_EMBEDDED_CREDS:
        assert not _matches_pattern(bad, schema), (
            f"spec.redisUrl: {bad!r} carries embedded credentials and must be "
            f"rejected by the pattern (issue #463)"
        )


def test_redis_url_has_cel_no_credentials_rule_463():
    """Issue #463: a CEL rule rejects ``@`` with a readable apply-time
    message pointing at the secretRef path (the raw pattern mismatch is
    opaque about WHY credentialed URLs are now rejected)."""
    rules = _cel_rules(_spec_field("redisUrl"))
    assert any(
        "@" in r["rule"] and "contains" in r["rule"] for r in rules
    ), f"spec.redisUrl: no CEL rule rejecting '@' userinfo; got: {rules!r}"
    assert any("issue #463" in r["message"].lower() for r in rules), (
        f"spec.redisUrl: CEL rule message must reference 'issue #463'; got: {rules!r}"
    )


def test_redis_credentials_secret_ref_name_declares_convention_pattern():
    """Issue #463 acceptance: ``spec.redisCredentials.secretRef.name`` is
    pattern-locked to the ``openstudio-redis*`` convention — the #240-style
    fence that keeps a CR-write principal from pointing the operator's one
    Secret read at arbitrary Secrets (TLS, registry, operator-managed
    credentials). ``openstudio-redis`` (deploy/redis-credentials-secret.yaml)
    and suffixed rotations must pass; every other real Secret name must be
    rejected."""
    schema = (
        SPEC_SCHEMA["properties"]["redisCredentials"]["properties"]["secretRef"]
    )

    name_schema = schema["properties"]["name"]
    pattern = name_schema.get("pattern")
    assert pattern == r"^openstudio-redis[a-z0-9-]*$", (
        f"secretRef.name must declare the openstudio-redis* convention "
        f"(issue #463); got {pattern!r}"
    )
    for good in ("openstudio-redis", "openstudio-redis-url", "openstudio-redis-2"):
        assert _matches_pattern(good, name_schema), (
            f"secretRef.name: {good!r} is a documented valid name and must be accepted"
        )
    for bad in ("tls-cert", "docker-pull-secret", "os-archive-creds", "WEB"):
        assert not _matches_pattern(bad, name_schema), (
            f"secretRef.name: {bad!r} must be rejected by the pattern (issue #463)"
        )

    rules = _cel_rules(name_schema)
    assert any(
        r["rule"] == "self.startsWith('openstudio-redis')" for r in rules
    ), f"secretRef.name: no CEL rule asserting the openstudio-redis prefix; got: {rules!r}"
    assert any("issue #463" in r["message"].lower() for r in rules), (
        f"secretRef.name: CEL rule message must reference 'issue #463'; got: {rules!r}"
    )


def test_redis_credentials_secret_ref_name_description_documents_rbac_bound():
    """Issue #606: the ``secretRef.name`` pattern deliberately stays WIDER
    than the enforcement — the default operator Role grants secrets:get
    only on the canonical name(s) via RBAC resourceNames, and a custom
    ``openstudio-redis-*`` name is apply-legal here but fails at read time
    (403). The field's description must SAY that (kubectl explain is the
    CR author's surface): custom names require the deploy/rbac.yaml
    resourceNames widening. The pattern itself stays untouched — pinning
    it to one fixed name would make the field pointless and break
    existing CRs at their next spec update."""
    name_schema = (
        SPEC_SCHEMA["properties"]["redisCredentials"]["properties"]["secretRef"]
    )["properties"]["name"]
    description = str(name_schema.get("description") or "")
    assert "resourceNames" in description, (
        "secretRef.name description must document the RBAC resourceNames "
        "bound (issue #606); got: "
        f"{description!r}"
    )
    assert "rbac.yaml" in description, (
        "secretRef.name description must point at deploy/rbac.yaml as the "
        f"widening surface (issue #606); got: {description!r}"
    )
    assert "openstudio-redis" in description, (
        "secretRef.name description must name the default granted Secret "
        f"(issue #606); got: {description!r}"
    )


def test_redis_credentials_secret_ref_requires_name_and_key():
    """Issue #463: the secretRef object declares ``required: [name, key]`` —
    a half-configured reference must fail apply-time validation instead of
    surfacing as a runtime resolution error. The key field is a plain
    Secret-key-shaped string (full-URL semantics: the key holds the whole
    ``redis://...`` URL, e.g. ``redis-url``)."""
    schema = (
        SPEC_SCHEMA["properties"]["redisCredentials"]["properties"]["secretRef"]
    )
    assert schema.get("required") == ["name", "key"], (
        f"secretRef must require both name and key (issue #463); "
        f"got {schema.get('required')!r}"
    )
    key_schema = schema["properties"]["key"]
    assert key_schema.get("pattern"), "secretRef.key is missing a `pattern` (issue #463)"
    assert _matches_pattern("redis-url", key_schema)
    assert _matches_pattern("password", key_schema)


# ---------------------------------------------------------------------------
# Issue #240: ``storagePolicy.secretRef`` must constrain archival Job envFrom
# to the documented ``os-archive-<suffix>`` naming convention. An
# unconstrained secretRef let a CR-write user mount any Secret in the
# namespace (openstudio-redis password, TLS Secrets, docker-registry Secrets,
# operator-managed credentials); the rclone process reads every key as an
# env var and could exfiltrate it via ``rclone config``. The DNS-1035-style
# pattern enforces the convention; the CEL rule gives a readable apply-time
# message for the same constraint.
# ---------------------------------------------------------------------------


_SECRET_REF_BAD_NAMES = (
    # Real Secret names in the openstudio-server namespace (#150):
    "openstudio-redis",        # KEDA TriggerAuthentication password (#116/#150)
    "openstudio-rotated",      # current default Redis Secret literal
    "openstudio-redis-creds",  # plausible alternative spelling
    # Other Secrets a CR-write user could pivot at:
    "tls-cert",                # any TLS Secret
    "docker-pull-secret",      # imagePullSecrets
    "kube-system/coredns",     # namespaced path (slash)
    "WEB",                     # uppercase — K8s names are lowercase
)
_SECRET_REF_GOOD_NAME = "os-archive-creds"  # used by test_archival.py + golden fixtures


def test_crd_spec_validations():
    """Issue #240 acceptance: ``spec.storagePolicy.secretRef`` carries both a
    ``pattern`` constraint matching the documented ``os-archive-<suffix>``
    naming convention AND an ``x-kubernetes-validations`` rule that surfaces
    the same constraint with a readable apply-time message.

    Pre-#240 the field was plain ``type: string`` with no constraint, so a
    CR-write user could set it to ``openstudio-redis`` and have rclone mount
    the KEDA TriggerAuthentication password as env vars — the rclone
    process reads every key as an env var and could exfiltrate the value via
    ``rclone config`` (which reads ``RCLONE_CONFIG_*`` env vars)."""
    schema = _field("storagePolicy", "secretRef")

    # Pattern must be present and equal the documented convention.
    pattern = schema.get("pattern")
    assert pattern == r"^os-archive-[a-z0-9-]+$", (
        f"spec.storagePolicy.secretRef must declare the os-archive- pattern "
        f"(issue #240); got {pattern!r}"
    )

    # The legitimate value used by every test fixture and runbook must pass.
    assert _matches_pattern(_SECRET_REF_GOOD_NAME, schema), (
        f"spec.storagePolicy.secretRef: {_SECRET_REF_GOOD_NAME!r} is the "
        f"documented valid value and must be accepted by the pattern"
    )

    # Every real-world Secret name from the issue body must be rejected —
    # these are the names the pre-#240 unconstrained field could reach.
    for bad in _SECRET_REF_BAD_NAMES:
        assert not _matches_pattern(bad, schema), (
            f"spec.storagePolicy.secretRef: {bad!r} must be rejected by the "
            f"pattern (issue #240)"
        )

    # CEL x-kubernetes-validations rule must be present, assert the
    # os-archive- prefix, and surface 'issue #240' in its apply-time message.
    rules = _cel_rules(schema)
    assert any(
        r["rule"] == "self.startsWith('os-archive-')"
        for r in rules
    ), (
        f"spec.storagePolicy.secretRef: no CEL rule asserting the "
        f"os-archive- prefix; got {rules!r}"
    )
    assert any("issue #240" in r["message"].lower() for r in rules), (
        f"spec.storagePolicy.secretRef: CEL rule message must reference "
        f"'issue #240'; got {rules!r}"
    )
