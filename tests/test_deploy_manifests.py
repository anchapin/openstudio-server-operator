"""Structural tests for the #78 storage-workload manifests and RBAC shift.

Quantifies the issue #78 acceptance criterion "Operator RBAC footprint
reduced" at CI level: the operator Role must no longer hold ANY batch
permission (the whole jobs rule moved to the prune CronJob's own
least-privilege Role), and the new Role must keep the house style —
namespaced, enumerated verbs, no wildcards, no secrets/volume inspection.
"""

import ast
import copy
import re
import sys
from pathlib import Path

import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"

OPERATOR_RBAC_DOCS = list(yaml.safe_load_all((DEPLOY / "rbac.yaml").read_text()))
STORAGE_DOCS = list(yaml.safe_load_all((DEPLOY / "storage-cronjob.yaml").read_text()))

CRONJOB = next(d for d in STORAGE_DOCS if d["kind"] == "CronJob")
PRUNE_ROLE = next(d for d in STORAGE_DOCS if d["kind"] == "Role")
PRUNE_ROLEBINDING = next(d for d in STORAGE_DOCS if d["kind"] == "RoleBinding")
PRUNE_SA = next(d for d in STORAGE_DOCS if d["kind"] == "ServiceAccount")
OPERATOR_ROLE = next(d for d in OPERATOR_RBAC_DOCS if d["kind"] == "Role")

# Issue #293 — defense-in-depth admission policy for the operator's
# pods/delete verb. Native RBAC has no label-selector slot, so the
# constraint lives in a ValidatingAdmissionPolicy declared in
# deploy/pod-delete-admission-policy.yaml. Loading the docs at module
# import keeps the test functions focused on assertions; a YAML parse
# error here surfaces in the first test that touches the constant.
_POD_DELETE_ADMISSION_PATH = DEPLOY / "pod-delete-admission-policy.yaml"
POD_DELETE_ADMISSION_DOCS = list(yaml.safe_load_all(_POD_DELETE_ADMISSION_PATH.read_text()))
_OPERATOR_SA_NAME = "openstudio-operator-sa"
_OPERATOR_SA_FULL = (
    f"system:serviceaccount:openstudio-server:{_OPERATOR_SA_NAME}"
)
_OPERATOR_NS = "openstudio-server"


def test_operator_role_no_longer_holds_any_batch_permission():
    """The #78 reduction: the entire batch/jobs rule is gone from the operator.

    Before #78: 6 rules including batch/jobs [get,list,watch,create,delete].
    After: 5 rules, no batch apiGroup at all — the operator can no longer
    create, read or delete Jobs; that credential moved to the prune SA.
    """
    assert all("batch" not in rule["apiGroups"] for rule in OPERATOR_ROLE["rules"])
    jobs_rules = [
        rule
        for rule in OPERATOR_ROLE["rules"]
        for res in rule["resources"]
        if res == "jobs"
    ]
    assert jobs_rules == []


def test_operator_role_no_longer_holds_any_hpa_permission():
    """The #77 reduction: the autoscaling/horizontalpodautoscalers rule is gone.

    Before #77: the operator patched ``worker-hpa`` ``spec.minReplicas`` from
    Redis backlog (Phase 4 HPA-floor adjuster, #18). The custom loop was
    removed in favor of a standard KEDA ScaledObject
    (deploy/keda-scaledobject.yaml) — KEDA owns the autoscaling surface and
    runs under its own ServiceAccount, so the operator no longer needs ANY
    HPA verbs. This is the same RBAC-shrink pattern as #78: operator
    footprint strictly smaller, dedicated workload SA picks up the powers.
    """
    assert all("autoscaling" not in rule["apiGroups"] for rule in OPERATOR_ROLE["rules"])
    hpa_rules = [
        rule
        for rule in OPERATOR_ROLE["rules"]
        for res in rule["resources"]
        if res == "horizontalpodautoscalers"
    ]
    assert hpa_rules == []


def test_operator_role_still_namespaced_enumerated_style():
    """House invariants kept: namespaced Role/RoleBinding only, no ClusterRole."""
    kinds = {doc["kind"] for doc in OPERATOR_RBAC_DOCS}
    assert "ClusterRole" not in kinds
    assert kinds == {"ServiceAccount", "Role", "RoleBinding"}


def test_operator_role_no_wildcard_verbs_on_oscm():
    """Issue #228 acceptance: `verbs: ["*"]` MUST NOT appear anywhere in
    the operator Role — the wildcard includes delete/deletecollection/
    create/bind, none of which the operator process ever invokes, and a
    compromised operator pod could otherwise delete the OSCM CR
    wholesale and bypass the singleton guard's passive policing.

    AGENTS.md Working rule: 'Least-privilege RBAC — namespaced Role
    only, never a ClusterRole. Verbs are enumerated; keep it that way.'
    This is the regression fence that makes the rule self-enforcing."""
    for rule in OPERATOR_ROLE["rules"]:
        assert "*" not in rule["verbs"], (
            f"rule grants wildcard verbs {rule['verbs']!r} on "
            f"{rule['resources']!r}; enumerate the verbs instead "
            "(issue #228)"
        )


def test_operator_role_oscm_verbs_are_enumerated_subset():
    """Issue #228 acceptance #2: the OSCM CR + status-subresource rules
    enumerate a least-privilege subset of {get, list, watch, patch,
    update}. The CR itself is read-only from the operator (singleton
    guard polls list_namespaced_custom_object) — get+list+watch is
    sufficient. The status subresource additionally needs patch+update
    for the StatusStore read-modify-write helper
    (get_namespaced_custom_object_status +
    patch_namespaced_custom_object_status)."""
    allowed_for_cr = {"get", "list", "watch"}
    allowed_for_status = {"get", "list", "watch", "patch", "update"}
    offenders = []
    for rule in OPERATOR_ROLE["rules"]:
        if "energy.nrel.gov" not in rule["apiGroups"]:
            continue
        for resource in rule["resources"]:
            verbs = set(rule["verbs"])
            allowed = (
                allowed_for_status if resource.endswith("/status")
                else allowed_for_cr
            )
            excess = verbs - allowed
            if excess:
                offenders.append((resource, sorted(excess), sorted(verbs)))
    assert not offenders, (
        "OSCM rules grant verbs outside the least-privilege subset "
        f"(issue #228): {offenders}"
    )


# ---- Issue #293: ValidatingAdmissionPolicy narrows the operator's
# pods/delete blast radius --------------------------------------
#
# Native RBAC has no label-selector slot on a Role rule, so the
# `pods/delete` verb in deploy/rbac.yaml cannot be label-restricted
# in-Role. The fix is a ValidatingAdmissionPolicy
# (admissionregistration.k8s.io/v1) — GA in k8s 1.30; the kind
# validation cluster runs 1.31 — that intercepts every DELETE pod
# admission request in `openstudio-server` and rejects any request
# from the operator ServiceAccount unless the target pod carries the
# `app=worker` label (the helm chart's worker Deployment pod
# selector — scripts/manifests/06-worker.yaml:39-40). All other
# actors (humans via kubectl, the prune CronJob SA, etc.) are NOT
# restricted — the CEL short-circuits on a non-matching userInfo.
#
# These tests pin the structural acceptance criteria: the manifest
# exists, parses, scopes to the operator namespace, targets
# DELETE on pods, hard-closes on policy failure (`failurePolicy:
# Fail`), carries the operator SA username + worker-label check in
# CEL, and is bound into the namespace by a matching Binding. A
# regression that re-introduces the wildcard delete blast radius
# trips at least one assertion below.


def _pod_delete_admission_policy():
    """Return the ValidatingAdmissionPolicy doc for #293."""
    matches = [
        d for d in POD_DELETE_ADMISSION_DOCS
        if d.get("kind") == "ValidatingAdmissionPolicy"
    ]
    assert matches, (
        "deploy/pod-delete-admission-policy.yaml is missing a "
        "ValidatingAdmissionPolicy — the operator's pods/delete verb "
        "is unconstrained at the admission layer (issue #293)"
    )
    return matches[0]


def _pod_delete_admission_binding():
    """Return the ValidatingAdmissionPolicyBinding doc for #293."""
    matches = [
        d for d in POD_DELETE_ADMISSION_DOCS
        if d.get("kind") == "ValidatingAdmissionPolicyBinding"
    ]
    assert matches, (
        "deploy/pod-delete-admission-policy.yaml is missing a "
        "ValidatingAdmissionPolicyBinding — the policy is "
        "cluster-scoped and has no RBAC for itself; the binding is "
        "what scopes evaluation to `openstudio-server` (issue #293)"
    )
    return matches[0]


def test_pod_delete_admission_manifest_exists_and_parses():
    """Issue #293 acceptance #1: the manifest file exists, parses as YAML,
    and contains exactly the two admissionregistration resources — no
    other resources (ConfigMap, ServiceAccount, etc.) belong here.
    """
    assert _POD_DELETE_ADMISSION_PATH.exists(), (
        f"{_POD_DELETE_ADMISSION_PATH} is missing — the operator's "
        "pods/delete verb is unconstrained at the admission layer "
        "(issue #293)"
    )
    kinds = {d["kind"] for d in POD_DELETE_ADMISSION_DOCS if d}
    assert kinds == {
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
    }, (
        f"deploy/pod-delete-admission-policy.yaml must declare exactly "
        f"a ValidatingAdmissionPolicy + Binding, got {kinds!r}"
    )


def test_pod_delete_admission_policy_targets_delete_on_pods():
    """Issue #293 acceptance #2: the policy's matchConstraints
    resourceRules target DELETE operations on core/v1 pods ONLY. CREATE
    / UPDATE / PATCH on pods are not operator surfaces (the operator
    patches Deployments, which transitively restart pods — see
    worker_recycler.py). Over-broad match would falsely reject pod
    lifecycle operations that other actors legitimately perform.
    """
    policy = _pod_delete_admission_policy()
    rules = policy["spec"]["matchConstraints"]["resourceRules"]
    assert len(rules) == 1, (
        f"pod-delete policy must have exactly one resourceRule "
        f"(DELETE on pods), got {rules!r}"
    )
    rule = rules[0]
    assert rule["apiGroups"] == [""], rule
    assert rule["apiVersions"] == ["v1"], rule
    assert rule["operations"] == ["DELETE"], rule
    assert rule["resources"] == ["pods"], rule


def test_pod_delete_admission_policy_scopes_to_openstudio_server():
    """The policy must be namespace-scoped via matchConstraints. A
    cluster-scoped policy (no namespaceSelector) would evaluate
    pod deletes in EVERY namespace — narrowing the operator blast
    radius in `openstudio-server` while widening it everywhere else
    is the wrong trade.
    """
    policy = _pod_delete_admission_policy()
    ns_selector = policy["spec"]["matchConstraints"].get("namespaceSelector")
    assert ns_selector, (
        "pod-delete policy must scope via matchConstraints."
        "namespaceSelector to openstudio-server; a cluster-scoped "
        "policy would evaluate pod deletes in every namespace "
        "(issue #293)"
    )
    match_labels = ns_selector.get("matchLabels") or {}
    assert match_labels.get("kubernetes.io/metadata.name") == _OPERATOR_NS, (
        f"namespaceSelector must target {(_OPERATOR_NS)!r}, got "
        f"{match_labels!r}"
    )


def test_pod_delete_admission_policy_validations_check_operator_sa_and_worker_label():
    """Issue #293 acceptance #3: the CEL validations must encode BOTH
    halves of the constraint:

    * `request.userInfo.username != '<operator SA>' || ...` —
      narrows the surface to ONLY the operator SA. Humans via
      kubectl, the prune CronJob SA, and any other principal are
      NOT restricted by this policy.
    * `object.metadata.labels['app'] == 'worker'` — narrows the
      pod-delete target to the helm chart's worker Deployment pod
      selector. A bare `pods/delete` RBAC rule on a compromised
      operator pod could otherwise reach `web`, `web-background`,
      `mongo`, `redis`, `queue`, NFS, or the operator's own pod.

    Both checks MUST be present in a single `||`-chained CEL
    expression (the short-circuit is what keeps non-operator
    actors unconstrained). A regression that drops either check
    silently re-opens the gap.
    """
    policy = _pod_delete_admission_policy()
    validations = policy["spec"].get("validations") or []
    assert validations, (
        "pod-delete policy must declare at least one CEL validation; "
        "an empty validations list means the policy does nothing "
        "(issue #293)"
    )
    matched = [
        v for v in validations
        if _OPERATOR_SA_FULL in v.get("expression", "")
        and "metadata.labels" in v["expression"]
        and "'app'" in v["expression"]
        and "'worker'" in v["expression"]
    ]
    assert matched, (
        "pod-delete policy CEL must check BOTH the operator SA "
        f"username ({_OPERATOR_SA_FULL!r}) AND the worker's "
        "`app=worker` label; a regression that drops either check "
        "silently re-opens issue #293. Current validations: "
        f"{[v.get('expression') for v in validations]!r}"
    )
    # The expression must short-circuit on non-matching userInfo so
    # other actors are not constrained. The CEL `||` operator is
    # present iff the policy author wrote a disjunction rather than
    # two separate `validations`. We require the disjunction shape
    # because it expresses the constraint as a single human-readable
    # rule; the k8s CEL compiler evaluates `||` left-to-right and
    # short-circuits on truth.
    expr = matched[0]["expression"]
    assert "||" in expr, (
        f"pod-delete policy CEL must short-circuit on userInfo so "
        f"non-operator actors keep unrestricted pod-delete access; "
        f"expected a `||`-chained expression, got {expr!r}"
    )


def test_pod_delete_admission_policy_message_cites_issue_293():
    """The validation message must reference issue #293 so an operator
    who hits the policy at apply time can find the rationale + the
    doc-string narrative without grepping source."""
    policy = _pod_delete_admission_policy()
    validations = policy["spec"]["validations"]
    messages = [v.get("message", "") for v in validations]
    assert any("#293" in m for m in messages), (
        f"pod-delete policy validation message must cite issue #293; "
        f"got {messages!r}"
    )


def test_pod_delete_admission_policy_failure_policy_is_fail():
    """`failurePolicy: Fail` (the default, but pinned here) means a
    broken admission webhook REJECTS the request rather than
    failing open. A regression to `Warn` would silently re-open the
    blast radius when the admission evaluation itself is broken
    (CEL compile error, missing object field, etc.)."""
    policy = _pod_delete_admission_policy()
    assert policy["spec"].get("failurePolicy") == "Fail", (
        f"pod-delete policy must have failurePolicy: Fail so a broken "
        f"admission evaluation REJECTS the request rather than "
        f"failing open; got failurePolicy="
        f"{policy['spec'].get('failurePolicy')!r}"
    )


def test_pod_delete_admission_binding_binds_policy_to_openstudio_server():
    """The Binding must (a) reference the policy by name and (b) scope
    to `openstudio-server` via namespaceSelector. The policy is
    cluster-scoped at the API level — the Binding is what restricts
    evaluation to the operator's namespace. A missing or
    incorrectly-targeted Binding silently turns the policy into a
    no-op."""
    binding = _pod_delete_admission_binding()
    assert binding["spec"]["policyName"] == "openstudio-operator-pod-delete-scope", (
        f"Binding.policyName must reference the operator policy, got "
        f"{binding['spec'].get('policyName')!r}"
    )
    ns_selector = binding["spec"].get("matchResources", {}).get("namespaceSelector")
    assert ns_selector, (
        "Binding must scope via matchResources.namespaceSelector to "
        f"{_OPERATOR_NS!r}; a missing selector leaves the "
        "cluster-scoped policy evaluating every namespace"
    )
    assert ns_selector.get("matchLabels", {}).get(
        "kubernetes.io/metadata.name"
    ) == _OPERATOR_NS, (
        f"Binding.namespaceSelector must target {_OPERATOR_NS!r}, got "
        f"{ns_selector!r}"
    )


# ---- Issue #565: schema-valid VAP/Binding shape across ALL of deploy/ ----
#
# All four admission documents (2 VAPs + 2 Bindings across
# pod-delete-admission-policy.yaml and storage-cronjob.yaml) were
# REJECTED by a live Kubernetes 1.31 API server under
# `kubectl apply --dry-run=server` before #565: the webhook-only
# `spec.admissionReviewVersions` on the pod-delete VAP (strict-decoding
# unknown field), the omitted required `spec.validationActions` on both
# Bindings ("at least one validation action is required"), a multi-line
# CEL `message` ("message must not contain line breaks"), and the
# nonexistent Binding `spec.selector` field (unknown-field reject).
# Dead manifests mean dead defense-in-depth: the #293/#294/#398
# narrowings silently vanish at apply time. The fence below re-derives
# the inventory from a glob of deploy/ so a newly added admission doc
# cannot dodge it by living in a new file, and pins the schema rules
# the API server enforces — a regression fails CI instead of failing
# silently at apply time.

_ADMISSION_POLICY_KIND = "ValidatingAdmissionPolicy"
_ADMISSION_BINDING_KIND = "ValidatingAdmissionPolicyBinding"
# Issue #572 added a third admission file (secret-read-admission-policy.yaml)
# and #573 a fourth (deployment-patch-admission-policy.yaml);
# the #565 fence below re-derives the doc inventory from a deploy/ glob, so
# the new file must be consciously listed here (same discipline as
# tests/_metrics_inventory.py, issue #406).
_EXPECTED_VAP_FILES = {
    "deployment-patch-admission-policy.yaml",
    "pod-delete-admission-policy.yaml",
    "secret-read-admission-policy.yaml",
    "storage-cronjob.yaml",
}


def _iter_admission_docs():
    """Yield (filename, doc) for every ValidatingAdmissionPolicy /
    ValidatingAdmissionPolicyBinding under deploy/, globbing all
    manifests so a new admission doc in a new file is still fenced."""
    for path in sorted(DEPLOY.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and doc.get("kind") in (
                _ADMISSION_POLICY_KIND,
                _ADMISSION_BINDING_KIND,
            ):
                yield path.name, doc


def test_all_vap_docs_have_no_admission_review_versions_and_single_line_messages():
    """Fence half 1 (VAP shape, issue #565): `spec.admissionReviewVersions`
    is a ValidatingWebhook-only field — strict decoding on the API
    server rejects it on a ValidatingAdmissionPolicy — and a CEL
    `message` containing a line break (including the trailing newline
    of a plain folded `>` block) is an outright Invalid reject. Both
    were live 1.31 rejects before #565; messages must use a strip-
    chomped `>-` folded scalar or a quoted single-line string."""
    vap_docs = [
        (fname, doc)
        for fname, doc in _iter_admission_docs()
        if doc["kind"] == _ADMISSION_POLICY_KIND
    ]
    assert {fname for fname, _ in vap_docs} == _EXPECTED_VAP_FILES, (
        "expected the two known VAP files under deploy/; a new "
        "admission doc must consciously extend this inventory (same "
        "discipline as tests/_metrics_inventory.py, issue #406)"
    )
    for fname, vap in vap_docs:
        assert "admissionReviewVersions" not in vap["spec"], (
            f"{fname}: spec.admissionReviewVersions is a webhook-only "
            "field unknown on ValidatingAdmissionPolicy — the API "
            "server rejects the whole manifest (issue #565)"
        )
        for i, validation in enumerate(vap["spec"].get("validations", [])):
            message = validation.get("message", "")
            assert message, (
                f"{fname}: validations[{i}].message is required — the "
                "generated default would not cite the issue rationale "
                "an operator hitting the policy at apply time needs"
            )
            assert "\n" not in message, (
                f"{fname}: validations[{i}].message must be a single "
                'line — the API server rejects "message must not '
                f'contain line breaks" (issue #565); got {message!r}'
            )


def test_all_vap_bindings_have_validation_actions_and_match_resources():
    """Fence half 2 (Binding shape, issue #565): `spec.validationActions`
    is REQUIRED on a ValidatingAdmissionPolicyBinding (omission is a
    hard Invalid reject) and must include `Deny` — `Warn`/`Audit`
    alone would not block the request the fence exists to stop — and
    `spec.matchResources` is the only scoping field: the Binding schema
    has NO `selector`, so the pre-#565 `selector: {}` was a
    strict-decoding unknown-field reject. Every binding must also
    reference a VAP that actually exists under deploy/."""
    admission_docs = list(_iter_admission_docs())
    binding_docs = [
        (fname, doc)
        for fname, doc in admission_docs
        if doc["kind"] == _ADMISSION_BINDING_KIND
    ]
    vap_names = {
        doc["metadata"]["name"]
        for _, doc in admission_docs
        if doc["kind"] == _ADMISSION_POLICY_KIND
    }
    assert {fname for fname, _ in binding_docs} == _EXPECTED_VAP_FILES, (
        "expected the two known Binding files under deploy/; a new "
        "admission doc must consciously extend this inventory (same "
        "discipline as tests/_metrics_inventory.py, issue #406)"
    )
    for fname, binding in binding_docs:
        spec = binding["spec"]
        assert spec.get("policyName") in vap_names, (
            f"{fname}: Binding.policyName {spec.get('policyName')!r} "
            "matches no ValidatingAdmissionPolicy under deploy/ — the "
            "bound policy is dormant (issue #565)"
        )
        actions = spec.get("validationActions")
        assert actions, (
            f"{fname}: Binding.validationActions is required by the "
            "v1 schema — an omitted/empty list is an API-server "
            "Invalid reject that kills the whole manifest apply "
            "(issue #565)"
        )
        assert "Deny" in actions, (
            f"{fname}: validationActions must include 'Deny'; got "
            f"{actions!r} — Warn/Audit alone does not block the "
            "admission request the fence exists to stop"
        )
        assert "selector" not in spec, (
            f"{fname}: Binding spec has no `selector` field in "
            "admissionregistration.k8s.io/v1 — the API server rejects "
            "the unknown field wholesale; scope via matchResources "
            "(issue #565)"
        )
        assert "matchResources" in spec, (
            f"{fname}: Binding must scope via matchResources (the "
            "namespaceSelector gate mirroring the policy's own "
            "matchConstraints); got spec keys "
            f"{sorted(spec)!r}"
        )


def test_operator_role_still_grants_pods_delete_verb():
    """The RBAC `pods/delete` verb MUST remain in deploy/rbac.yaml —
    the admission policy is an additional defense-in-depth layer,
    NOT a replacement. Removing the RBAC verb would break the
    documented call site (analysis_sla.py:542 /
    delete_namespaced_pod) before any operator could even reach the
    admission check. This regression fence pins the RBAC surface
    even as the audit/audit-dryrun narrative evolves."""
    pod_rules = [
        rule for rule in OPERATOR_ROLE["rules"]
        if rule["apiGroups"] == [""] and rule["resources"] == ["pods"]
    ]
    assert len(pod_rules) == 1, (
        f"expected exactly one pods rule in operator Role, got {pod_rules!r}"
    )
    assert "delete" in pod_rules[0]["verbs"], (
        "operator Role must retain `delete` on pods — analysis_sla."
        "py:542 calls delete_namespaced_pod and the admission policy "
        "is the defense-in-depth layer, NOT a replacement for the "
        f"RBAC verb (issue #293). Current verbs: {pod_rules[0]['verbs']!r}"
    )


def test_prune_role_is_least_privilege_enumerated():
    """The new Role: exactly one tick's needs, no wildcards, no workload access."""
    assert PRUNE_ROLE["metadata"]["namespace"] == "openstudio-server"
    by_resource = {
        (tuple(rule["apiGroups"]), rule["resources"][0]): sorted(rule["verbs"])
        for rule in PRUNE_ROLE["rules"]
    }
    assert by_resource == {
        (("energy.nrel.gov",), "openstudioclustermanagers"): ["list"],
        (("energy.nrel.gov",), "openstudioclustermanagers/status"): ["get", "patch"],
        (("batch",), "jobs"): ["create", "delete", "get"],
        (("",), "events"): ["create"],
    }
    for rule in PRUNE_ROLE["rules"]:
        assert "*" not in rule["verbs"]
    # The footprint the prune SA does NOT inherit: no Deployments, no pods,
    # no HPA, no secrets — the operator's other powers are not shifted onto it.
    all_resources = {res for rule in PRUNE_ROLE["rules"] for res in rule["resources"]}
    assert "secrets" not in all_resources
    assert "pods" not in all_resources
    assert "deployments" not in all_resources
    assert "horizontalpodautoscalers" not in all_resources


def test_prune_rolebinding_wires_sa_to_role():
    """The storage-pruner RoleBinding must reference BOTH the prune Role
    (by name) and the prune ServiceAccount (as a single subject of kind
    ServiceAccount). This is the wiring that lets the CronJob pod run
    with the least-privilege Role from
    ``test_prune_role_is_least_privilege_enumerated`` — a Role that no
    pod can assume is structurally correct but operationally dead, so
    this test pins the roleRef + subject triple as one atomic check."""
    assert PRUNE_ROLEBINDING["roleRef"]["name"] == PRUNE_ROLE["metadata"]["name"]
    subject = PRUNE_ROLEBINDING["subjects"][0]
    assert subject["name"] == PRUNE_SA["metadata"]["name"]
    assert subject["kind"] == "ServiceAccount"


def test_cronjob_schedule_and_single_flight():
    """*/10 == the 600 s cadence of the operator timer this replaces; Forbid
    guarantees never two concurrent ticks."""
    assert CRONJOB["spec"]["schedule"] == "*/10 * * * *"
    assert CRONJOB["spec"]["concurrencyPolicy"] == "Forbid"
    assert CRONJOB["metadata"]["namespace"] == "openstudio-server"


def test_cronjob_job_template_has_active_deadline_seconds():
    """Issue #569 — the prune Job template bounds one tick's wall-clock.

    A hung prune pod (the kubernetes python client sets NO read timeout —
    an apiserver/Redis stall or a black-holed TCP connection pins it in
    Running forever) wedges the whole retention pipeline under
    ``concurrencyPolicy: Forbid``: every subsequent */10 schedule is
    skipped and the Job never reaches Failed, so
    ``OpenStudioOperatorPruneJobFailed`` (keyed on kube_job_status_failed)
    never fires. ``activeDeadlineSeconds`` is the #394 archival-Job
    pattern (``archival.py::ARCHIVAL_JOB_ACTIVE_DEADLINE_SECONDS``)
    applied to the prune side, sized off the schedule cadence (600 s)
    instead of the #394 worker grace period: 1800 s = 3 full intervals —
    generous for a worst-case healthy tick (3x REST retry budget + the
    once-per-tick heavy data_points poll; the tick never blocks on
    archival Job completion, so this cannot race a legitimate upload —
    those carry their own 31200 s #394 deadline) yet bounded so the
    kubelet DeadlineExceeded-kills a wedged pod within two skipped
    schedules, transitioning the Job to Failed for the sibling alert.
    """
    job_spec = CRONJOB["spec"]["jobTemplate"]["spec"]
    assert job_spec["activeDeadlineSeconds"] == 1800, (
        "prune Job template must bound one tick's wall-clock at 1800s "
        "(3x the 600s schedule — the #394 multiple-of-a-reference-cadence "
        "pattern; issue #569): a hung pod otherwise pins the Job in "
        "Running forever and retention dies silently under Forbid"
    )


def test_prometheusrule_prune_group_pins_failure_and_absence_of_success():
    """Issues #569 + #645 — the prune alert pair: failed Jobs AND CronJob
    recency.

    The failed-Job alert is event-driven (it needs a Job to reach Failed
    — the activeDeadlineSeconds from
    ``test_cronjob_job_template_has_active_deadline_seconds`` now makes a
    hung pod do exactly that); the absence-of-success complement catches
    the modes that produce no events at all — suspended CronJob, schedule
    mutated wrong, Jobs never scheduled — where every operator-process
    signal stays green (the heartbeat and tick counters belong to the
    operator, not the pruner).

    Since #645 the absence alert keys on CronJob-level recency
    (``time() - kube_cronjob_status_last_successful_time``), NOT on
    Job-object accumulation: the #569
    ``sum(max_over_time(kube_job_status_succeeded[...]))`` shape was
    permanently pinned >= 1 by the three succeeded Jobs
    ``successfulJobsHistoryLimit`` retains (the CronJob controller only
    GCs history when it creates a NEW Job), so it could never fire in
    exactly the no-event modes it was built for. The canonical shape and
    the firing scenarios are fenced in
    ``tests/test_monitoring_artifacts.py`` (#645 drift gate); this test
    pins the structural essentials here beside the rest of the prune
    manifest checks.
    """
    docs = list(yaml.safe_load_all((DEPLOY / "prometheustrule.yaml").read_text()))
    rule = next(d for d in docs if d and d["kind"] == "PrometheusRule")
    prune_group = next(
        g for g in rule["spec"]["groups"] if g["name"] == "openstudio-operator.prune"
    )
    alerts = {entry["alert"]: entry for entry in prune_group["rules"]}
    failed = alerts.get("OpenStudioOperatorPruneJobFailed")
    assert failed is not None, "prune failed-Job alert must stay (the #470 rekey)"
    assert 'kube_job_status_failed{' in failed["expr"]
    no_success = alerts.get("OpenStudioOperatorPruneJobNoSuccess")
    assert no_success is not None, (
        "prune absence-of-success alert missing (issue #569) — the "
        "event-driven failed alert cannot see a suspended/starved CronJob"
    )
    expr = no_success["expr"]
    assert "kube_job_status_succeeded" not in expr, (
        "absence-of-success must not accumulate kube_job_status_succeeded "
        "(issue #645): successfulJobsHistoryLimit (3) retains succeeded Job "
        "objects forever in the no-event modes, pinning the sum at >= 1 so "
        "the alert can never fire"
    )
    assert 'time() - kube_cronjob_status_last_successful_time{' in expr, (
        "absence-of-success must key on CronJob-level recency "
        "(issue #645), not Job-object accumulation"
    )
    assert 'cronjob="openstudio-storage-pruner"' in expr
    assert "kube_cronjob_status_last_schedule_time" in expr and " unless " in expr, (
        "absence-of-success needs the never-succeeded bootstrap arm "
        "(schedule-stale unless successful-time-exists): "
        "kube_cronjob_status_last_successful_time is ABSENT until the "
        "first success, so time() - X alone is no-data on a fresh install "
        "and would silently never fire (#569 TRUE-absence precedent)"
    )
    assert no_success["labels"]["severity"] == "warning"


def test_cronjob_runs_entrypoint_from_pinned_operator_image():
    """The prune CronJob must (a) be wired to the least-privilege
    ``openstudio-storage-pruner-sa`` ServiceAccount and the
    ``Never``/``backoffLimit=0`` single-attempt shape, (b) pin its
    container image to the same SHA256 digest as
    ``deploy/operator-deployment.yaml`` (issue #291 regression fence —
    the mutable ``:dev`` tag combined with ``IfNotPresent`` previously
    let the kubelet retain a cached image across ticks), and (c) use
    ``imagePullPolicy: Always`` so the digest is re-pulled every tick
    (defense in depth in case the digest-pinning invariant is ever
    bypassed). The POD_NAMESPACE downward-API env var is also pinned
    because the prune entrypoint reads it for namespace-scoped
    resource lookups."""
    pod = CRONJOB["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert CRONJOB["spec"]["jobTemplate"]["spec"]["backoffLimit"] == 0
    assert pod["serviceAccountName"] == PRUNE_SA["metadata"]["name"]
    container = pod["containers"][0]
    # Issue #291: the CronJob must pin the operator image by SHA256 digest
    # (not a mutable tag) and use imagePullPolicy: Always. The exact digest
    # is intentionally NOT hard-coded here — release.yml re-pins both manifests
    # on every develop push (issue #316), so a literal would go stale on the
    # next re-pin. The load-bearing invariant "operator-deployment and
    # storage-cronjob agree on the same digest" is enforced by
    # `test_cronjob_image_digest_matches_operator_deployment` below.
    image = container["image"]
    assert "@sha256:" in image, (
        f"CronJob image must be digest-pinned (got {image!r}); a mutable "
        "tag like `:latest` or `:dev` lets the kubelet retain a stale "
        "image across restarts (issue #291)"
    )
    # No mutable tag suffix — `@sha256:...` must be the only ref component.
    # Split on `@` and check the tag slot (between `:` and `@`) is absent.
    assert ":" not in image.split("@", 1)[0], (
        f"CronJob image must not carry a mutable tag (got {image!r}); "
        "the part before `@` must have no `:` (issue #291)"
    )
    # Belt-and-braces: the historical weak tags `:dev` and `:latest` must
    # never reappear as the only image reference.
    assert not image.endswith(":dev"), image
    assert not image.endswith(":latest"), image
    # Issue #291: Always re-pulls the digest on every CronJob tick (10 min).
    # The previous IfNotPresent policy left the kubelet free to keep a stale
    # image if the digest-pinning invariant was ever bypassed.
    assert container["imagePullPolicy"] == "Always"
    assert container["command"] == ["python", "-m", "openstudio_operator.prune_entrypoint"]
    env = {e["name"]: e for e in container["env"]}
    assert env["POD_NAMESPACE"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.namespace"


def test_cronjob_pod_mounts_no_volumes_and_no_secret_refs():
    """The prune pod orchestrates only — no NFS claim, no credentials; the
    archival Jobs it spawns carry their own envFrom secretRef and PVC mounts."""
    pod = CRONJOB["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert all(v["emptyDir"] == {} for v in pod["volumes"])  # scratch /tmp only
    blob = str(CRONJOB)
    assert "persistentVolumeClaim" not in blob
    assert "secretRef" not in blob
    assert "secretKeyRef" not in blob


# ---- Issue #162: prune CronJob securityContext hardening ------------
#
# Goal #115 (operator hardening) and #114 (rclone hardening) set a
# consistent baseline across every workload the operator manages:
# `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`,
# `capabilities.drop: [ALL]`, `runAsNonRoot: true`, `runAsUser: <non-zero>`,
# and `seccompProfile.type: RuntimeDefault`. The prune CronJob originally
# carried only the first three; #162 closes the gap so every container in
# every deploy/ workload enforces the same baseline. A pod compromised via
# the prune entrypoint must not silently run as root or without seccomp.


def _iter_workload_containers():
    """Yield (manifest_name, doc_kind, doc_name, container) for every
    container in every Deployment / StatefulSet / DaemonSet / Job /
    CronJob under deploy/ AND every container in the kind-validation
    helm-chart overlay under scripts/manifests/ (issue #388 widening).
    Other kinds (CRD, RBAC, NetworkPolicy, Secret, ServiceAccount,
    ScaledObject, Namespace, PV, PVC, Service, ...) carry no containers
    and are skipped. Manifests that fail to YAML-parse are skipped — the
    iterator is defensive because `deploy/operator-deployment.yaml`
    historically has hand-edited indentation quirks (#115-era work) that
    PyYAML rejects even though `kubectl` accepts the same file.

    Issue #388 scope note: the scripts/manifests/ tree includes the
    kind-validation stand-ins for the helm chart's StatefulSet-like
    workloads (mongo, redis, NFS hostPath). Those workloads do NOT yet
    carry the PSS `restricted` baseline (they're third-party images whose
    entrypoints assume uid 0 to chown data dirs); follow-up issue #394
    owns hardening them. This iterator SKIPS them explicitly so the
    broadened glob doesn't fail on workloads the issue's scope guard
    declared out of scope. The skipped filenames are the only point of
    contact between #388 (Rails pods + namespace label) and #394
    (stateful workloads) — when #394 lands, deleting these two entries
    from the skip set is the only edit needed here.
    """
    manifests = [
        ("deploy/", DEPLOY.glob("*.yaml")),
        (
            "scripts/manifests/",
            sorted((Path(__file__).resolve().parents[1] / "scripts" / "manifests").glob("*.yaml")),
        ),
    ]
    # Filenames whose containers are out of scope for #388 (see #394).
    skipped_helm_overlay_files = {"01-mongo.yaml", "02-redis.yaml"}
    for prefix, paths in manifests:
        for path in paths:
            # Use a stable label relative to the repo root, mirroring
            # the pre-#388 path-only convention (the deploy/ iterator
            # yielded `path.name` only, so emit both shapes for the new
            # `scripts/manifests/<file>` group to keep error messages
            # self-identifying).
            if prefix == "deploy/":
                label_prefix = ""
            else:
                label_prefix = prefix
                if path.name in skipped_helm_overlay_files:
                    continue
            try:
                docs = list(yaml.safe_load_all(path.read_text()))
            except yaml.YAMLError:
                # Out-of-scope to repair an unrelated indentation bug here.
                # The targeted `test_storage_cronjob_container_securitycontext_complete`
                # and the file-level `kubectl apply --dry-run=client` walk
                # (run by docs/validation.md) catch the same shape for the
                # prune CronJob.
                continue
            for doc in docs:
                if not doc:
                    continue
                kind = doc.get("kind")
                name = doc.get("metadata", {}).get("name", "<unnamed>")
                label = f"{label_prefix}{path.name}"
                if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
                    containers = (
                        doc.get("spec", {})
                        .get("template", {})
                        .get("spec", {})
                        .get("containers", [])
                    )
                    for c in containers:
                        yield label, kind, name, c
                elif kind == "CronJob":
                    containers = (
                        doc.get("spec", {})
                        .get("jobTemplate", {})
                        .get("spec", {})
                        .get("template", {})
                        .get("spec", {})
                        .get("containers", [])
                    )
                    for c in containers:
                        yield label, kind, name, c


def test_every_deploy_workload_container_has_hardening_baseline():
    """Issue #162 / #115 acceptance: every container in every deploy/
    workload enforces the same six-field hardening baseline. A regression
    here re-opens the same escalation surface #115 closed for the
    operator and #114 closed for the rclone Job.

    The six fields enforced below match the explicit list at the head of
    the #162 section comment (`runAsNonRoot`, `runAsUser`, `seccompProfile`
    + `allowPrivilegeEscalation`, `readOnlyRootFilesystem`,
    `capabilities.drop`). The targeted ``test_storage_cronjob_container_
    securitycontext_complete`` re-asserts the same six fields on the cron
    container specifically; this test is the broad walk across every
    container the operator runs (operator Deployment + prune CronJob
    today; future deployments inherit the same baseline by routing
    through ``_iter_workload_containers``)."""
    offenders = []
    for path_name, kind, name, container in _iter_workload_containers():
        sc = container.get("securityContext") or {}
        problems = []
        if sc.get("allowPrivilegeEscalation") is not False:
            problems.append(
                f"allowPrivilegeEscalation={sc.get('allowPrivilegeEscalation')!r} "
                "(must be False)"
            )
        if sc.get("readOnlyRootFilesystem") is not True:
            problems.append(
                f"readOnlyRootFilesystem={sc.get('readOnlyRootFilesystem')!r} "
                "(must be True)"
            )
        if (sc.get("capabilities") or {}).get("drop") != ["ALL"]:
            problems.append(
                f"capabilities.drop="
                f"{(sc.get('capabilities') or {}).get('drop')!r} "
                "(must be ['ALL'])"
            )
        if sc.get("runAsNonRoot") is not True:
            problems.append(f"runAsNonRoot={sc.get('runAsNonRoot')!r}")
        run_as_user = sc.get("runAsUser")
        if not isinstance(run_as_user, int) or run_as_user == 0:
            problems.append(f"runAsUser={run_as_user!r} (must be non-zero int)")
        seccomp = sc.get("seccompProfile") or {}
        if seccomp.get("type") != "RuntimeDefault":
            problems.append(
                f"seccompProfile.type={seccomp.get('type')!r} "
                "(must be 'RuntimeDefault')"
            )
        if problems:
            offenders.append(
                (path_name, kind, name, container.get("name"), problems)
            )
    assert not offenders, (
        f"containers missing #115/#162 six-field hardening baseline: "
        f"{offenders}"
    )


def test_storage_cronjob_container_securitycontext_complete():
    """Issue #162 acceptance: the prune CronJob container carries the full
    six-field hardening baseline (matches deploy/operator-deployment.yaml
    :48-56 and the rclone Job at archival.py:225-232). The first three
    were already present; #162 closes the regression where the last three
    were silently dropped when #78 externalized the retention pipeline."""
    pod = CRONJOB["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    container = pod["containers"][0]
    sc = container["securityContext"]
    # First three — preserved from the pre-#162 shape.
    assert sc["allowPrivilegeEscalation"] is False, sc
    assert sc["readOnlyRootFilesystem"] is True, sc
    assert sc["capabilities"]["drop"] == ["ALL"], sc
    # Last three — added by #162; these are the regression-closing fields.
    assert sc["runAsNonRoot"] is True, sc
    assert sc["runAsUser"] == 1000, sc
    assert sc["seccompProfile"]["type"] == "RuntimeDefault", sc


def test_storage_cronjob_pod_securitycontext_hardened():
    """Issue #162 defense-in-depth: the pod template ALSO carries the
    baseline plus `fsGroup` so the emptyDir /tmp mount is writable by
    the non-root UID the container runs as."""
    pod = CRONJOB["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    sc = pod["securityContext"]
    assert sc["runAsNonRoot"] is True, sc
    assert sc["runAsUser"] == 1000, sc
    assert sc["seccompProfile"]["type"] == "RuntimeDefault", sc
    assert sc["fsGroup"] == 1000, sc


# ---- Issue #161: pod-level securityContext on every operator-managed
# workload ------------------------------------------------------
#
# #115 / #114 / #162 set the hardening baseline (`runAsNonRoot`,
# `runAsUser`, `seccompProfile`) at the container level, but Pod Security
# Standards `restricted` validates the POD-level fields, not the
# container-level ones. An injected sidecar (Istio ambient mesh) or a
# debug `ephemeralContainers` patch that omits its own securityContext
# inherits the pod-level defaults — so the pod-level block is defense-
# in-depth: a workload that survives the container-level check still
# falls back to the pod-level defaults. fsGroup 1000 makes any
# volume-mounted FS (the emptyDir /tmp for the operator + prune
# CronJob) writable by the non-root UID the container runs as.
OPERATOR_DOCS = list(yaml.safe_load_all((DEPLOY / "operator-deployment.yaml").read_text()))
OPERATOR_DEPLOYMENT = next(d for d in OPERATOR_DOCS if d["kind"] == "Deployment")


def test_cronjob_image_digest_matches_operator_deployment():
    """Issue #291: both manifests must pin the same digest so the CronJob and
    the operator deployment never disagree about which image is in service.
    If they drift, a one-image-update CI job that only pins operator-deployment
    would leave storage-cronjob pointing at a stale image. The release.yml
    'Pin operator image by SHA256 digest' step is expected to re-pin BOTH
    manifests in lockstep on every develop push (see issue #291 acceptance
    criterion)."""
    op_container = OPERATOR_DEPLOYMENT["spec"]["template"]["spec"]["containers"][0]
    op_image = op_container["image"]
    assert "@sha256:" in op_image, (
        f"operator-deployment image must be digest-pinned (got {op_image!r})"
    )
    cron_container = CRONJOB["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    cron_image = cron_container["image"]
    assert cron_image == op_image, (
        f"storage-cronjob image {cron_image!r} must equal operator-deployment image "
        f"{op_image!r}; release.yml must re-pin both manifests in lockstep"
    )
    assert cron_container["imagePullPolicy"] == "Always", (
        "CronJob must use imagePullPolicy: Always so the digest is re-pulled on every tick"
    )


# ---- Issue #306: storage-prune CronJob exposes /metrics on port 9090 -------
#
# The prune entrypoint now calls start_metrics_server() at the top of main()
# (matching the operator's pattern) and the CronJob pod exposes the same
# conventional Prometheus port (METRICS_PORT=9090). The endpoint is gated
# by the parallel ``openstudio-storage-pruner-metrics-ingress`` NetworkPolicy
# in deploy/network-policy.yaml — same Prometheus-style allow-list (namespace-
# matched scraper + label-scoped same-namespace peer, hardened to the #295
# bar by #478) used for the operator's /metrics (#166), plus the same
# empty-by-default OPENSTUDIO_METRICS_TOKEN_FILE bearer opt-in (#401 parity,
# #478). The two targets are intentionally distinct policy objects because
# the pod labels differ (the operator carries ``app: openstudio-operator``
# and the CronJob carries ``app.kubernetes.io/component: storage-pruner``);
# widening the operator's policy with an OR selector would silently broaden
# the surface, so the two-policy split is the regression fence.


def test_storage_cronjob_exposes_metrics_port_9090():
    """Issue #306 acceptance: the prune CronJob container exposes
    containerPort 9090 (the same METRICS_PORT as the operator). Pre-fix
    the CronJob had no `ports:` block at all — the prune entrypoint's
    ``start_metrics_server()`` call bound the port on the pod IP but no
    containerPort declaration meant a service / readiness probe could
    not see it, and the scrape pattern was implicit. The post-fix shape
    mirrors deploy/operator-deployment.yaml:88-91 (name: metrics,
    containerPort: 9090) so the two scrape targets are structurally
    identical."""
    container = CRONJOB["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    ports = container.get("ports") or []
    metrics_ports = [
        p for p in ports if p.get("containerPort") == 9090
    ]
    assert len(metrics_ports) == 1, (
        f"prune CronJob must expose exactly one containerPort 9090, "
        f"got {ports!r}"
    )
    assert metrics_ports[0].get("name") == "metrics", (
        f"prune CronJob metrics port must be named 'metrics' to mirror "
        f"deploy/operator-deployment.yaml:89, got {metrics_ports[0]!r}"
    )


def _storage_pruner_metrics_ingress_policy():
    """Locate the storage-prune CronJob's dedicated ingress policy.

    The operator's ``openstudio-operator-metrics-ingress`` policy (#166)
    is selector-scoped to ``app: openstudio-operator`` (the operator pod
    carries that label), so it does NOT select the CronJob pod — which
    carries ``app.kubernetes.io/component: storage-pruner`` instead. The
    two-policy split is the regression fence; this helper returns the
    dedicated policy added by #306."""
    matches = [
        d for d in NETPOL_DOCS
        if "storage-pruner-metrics-ingress" in d["metadata"]["name"]
    ]
    assert matches, (
        "no openstudio-storage-pruner-metrics-ingress NetworkPolicy in "
        "deploy/network-policy.yaml — the prune CronJob's /metrics on "
        "port 9090 is unauthenticated and would be readable by every "
        "in-cluster pod (#306)"
    )
    return matches[0]


def test_storage_pruner_metrics_ingress_policy_selects_cronjob_pod():
    """Issue #306 acceptance: the storage-prune CronJob's metrics-ingress
    policy targets the CronJob pod via the
    ``app.kubernetes.io/component: storage-pruner`` label — the SAME
    label the CronJob pod template carries at
    deploy/storage-cronjob.yaml:73-74 + :92-93. Mismatching the selector
    would silently leave the endpoint unauthenticated against the
    scraping allow-list."""
    policy = _storage_pruner_metrics_ingress_policy()
    assert policy["metadata"]["namespace"] == "openstudio-server"
    assert "Ingress" in policy["spec"]["policyTypes"]
    selector = policy["spec"]["podSelector"]["matchLabels"]
    assert selector.get("app.kubernetes.io/component") == "storage-pruner"
    # The operator pod does NOT carry that component label — verify the
    # split is genuinely two policies, not one with a broad OR selector.
    assert "app" not in selector or selector["app"] != "openstudio-operator", (
        "storage-pruner-metrics-ingress should NOT select the operator "
        "pod; the operator's metrics-ingress policy (#166) owns that "
        "selector. A duplication would silently double the surface."
    )


def test_storage_pruner_metrics_ingress_restricts_to_port_9090():
    """The only port allowed by the ingress rule is 9090/TCP — same
    invariant as the operator's policy (#166). No plaintext risky ports,
    no other operator surface leaks through this policy."""
    policy = _storage_pruner_metrics_ingress_policy()
    ingress_rules = policy["spec"]["ingress"]
    assert ingress_rules, "ingress rules must not be empty"
    allowed_ports = set()
    for rule in ingress_rules:
        for port in rule.get("ports", []):
            assert port["protocol"] == "TCP", (
                f"only TCP allowed on /metrics, got {port['protocol']}"
            )
            allowed_ports.add(port["port"])
    assert allowed_ports == {9090}, (
        f"storage-pruner /metrics ingress must allow only port 9090, "
        f"got {allowed_ports}"
    )


def test_storage_pruner_metrics_ingress_has_prometheus_and_peer_allow():
    """Same Prometheus-style allow-list as the operator's policy (#166 /
    #295 / AGENTS.md "Working rules"): a namespace-matched scraper + a
    same-namespace peer only when that peer opts in via the project label
    ``app.kubernetes.io/component: metrics-scraper``. An empty
    ``podSelector: {}`` is a regression (issue #478 — the pruner policy's
    original shape): every helm-chart pod in openstudio-server carries
    some ``app.kubernetes.io/component`` value (web / web-background /
    worker / db / redis / queue / nfs), but none of them is
    ``metrics-scraper``, so the unscoped allow let them scrape the
    pruner's plaintext /metrics (``analyses_archived_total``,
    ``analyses_deleted_total``, ``prune_tick_failures_total{reason}``) —
    useful reconnaissance for timing retention deletes against conflict
    storms. Cluster admins running a different scraper namespace MUST
    edit the label match — the test is the regression fence that prompts
    the rename."""
    policy = _storage_pruner_metrics_ingress_policy()
    rule = policy["spec"]["ingress"][0]
    from_selectors = rule["from"]
    has_namespace_selector = any(
        "namespaceSelector" in peer for peer in from_selectors
    )
    same_ns_peers = [
        peer for peer in from_selectors if "podSelector" in peer
    ]
    has_label_scoped_peer = any(
        peer["podSelector"].get("matchLabels", {}).get(
            "app.kubernetes.io/component"
        )
        == "metrics-scraper"
        for peer in same_ns_peers
    )
    has_empty_podselector = any(
        peer.get("podSelector") == {} for peer in same_ns_peers
    )
    assert has_namespace_selector, (
        "storage-pruner-metrics-ingress must include a namespaceSelector "
        "pointing at the cluster's scraper namespace (default `prometheus`); "
        "see AGENTS.md Working rules for the cluster-admin opt-in."
    )
    assert has_label_scoped_peer, (
        "storage-pruner-metrics-ingress must include a podSelector matching "
        "{app.kubernetes.io/component: metrics-scraper} so the same-"
        "namespace peer allow is opt-in only. See AGENTS.md /metrics "
        "ingress rule (issues #295 / #478)."
    )
    assert not has_empty_podselector, (
        "storage-pruner-metrics-ingress must NOT use `podSelector: {}` — "
        "that selector matches every pod in openstudio-server (web, "
        "web-background, worker, db, redis, queue, nfs) and would re-open "
        "the plaintext /metrics endpoint to every helm-chart pod "
        "(issue #478 — same regression as #295 fixed for the operator)."
    )


def test_storage_cronjob_carries_metrics_token_file_env():
    """Issue #478 acceptance: the prune CronJob container carries the same
    (empty-by-default) ``OPENSTUDIO_METRICS_TOKEN_FILE`` opt-in as the
    operator Deployment's #401 hook. ``prune_entrypoint.main()`` calls the
    shared ``metrics.start_metrics_server()`` with no explicit token_file,
    so this env var is the pruner's ONLY config surface for the
    fail-closed bearer gate — without the manifest entry the CronJob pod
    could never enable authN even though the code supports it. Empty value
    = the documented open-plaintext default (the NetworkPolicy is then
    the only gate); a non-empty default would silently require a Secret
    mount the stock install does not ship."""
    container = CRONJOB["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in container.get("env") or []}
    assert "OPENSTUDIO_METRICS_TOKEN_FILE" in env, (
        "storage-cronjob.yaml must ship the OPENSTUDIO_METRICS_TOKEN_FILE "
        "env var (empty-by-default opt-in, mirroring "
        "deploy/operator-deployment.yaml's #401 hook) — the pruner's "
        "/metrics bearer-token gate is unreachable without it (#478)"
    )
    assert env["OPENSTUDIO_METRICS_TOKEN_FILE"] == "", (
        "OPENSTUDIO_METRICS_TOKEN_FILE must default to empty (open "
        "plaintext gated only by the metrics-ingress NetworkPolicy); a "
        "non-empty default would require a Secret mount the stock install "
        "does not ship (#401 / #478 opt-in shape)"
    )


def test_storage_cronjob_pod_labels_match_ingress_policy_selectors():
    """Issue #306 regression fence: the CronJob pod template must carry
    every label the new ingress policy requires. The CronJob carries
    ``app.kubernetes.io/managed-by: openstudio-operator`` +
    ``app.kubernetes.io/component: storage-pruner`` at lines :91-93;
    dropping the managed-by label would silently deselect the CronJob
    from BOTH the metrics-ingress (#306) AND the storage-egress (#112)
    policies — the scrape would 0/1 and the storage egress would fall
    back to the default-deny outcome. Mirrors #224's ``app: openstudio-
    operator`` regression-fence pattern, scoped to the CronJob."""
    cron_pod_labels = CRONJOB["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    policy = _storage_pruner_metrics_ingress_policy()
    match_labels = policy["spec"]["podSelector"]["matchLabels"]
    missing = {
        key: {"selector_requires": value, "pod_has": cron_pod_labels.get(key)}
        for key, value in match_labels.items()
        if cron_pod_labels.get(key) != value
    }
    assert not missing, (
        "storage-cronjob pod template is missing labels required by "
        f"openstudio-storage-pruner-metrics-ingress (#306): {missing}"
    )


def _pod_securitycontext_problems(sc):
    """Return a list of human-readable problems with a pod-level
    securityContext dict; empty list means the baseline is satisfied.
    Shared between the operator Deployment / prune CronJob / archival
    Job structural tests so the acceptance shape is identical across
    all three operator-managed workloads."""
    if not sc:
        return ["<missing pod-level securityContext>"]
    problems = []
    if sc.get("runAsNonRoot") is not True:
        problems.append(f"runAsNonRoot={sc.get('runAsNonRoot')!r}")
    run_as_user = sc.get("runAsUser")
    if not isinstance(run_as_user, int) or run_as_user == 0:
        problems.append(f"runAsUser={run_as_user!r} (must be non-zero int)")
    seccomp = sc.get("seccompProfile") or {}
    if seccomp.get("type") != "RuntimeDefault":
        problems.append(
            f"seccompProfile.type={seccomp.get('type')!r} "
            "(must be 'RuntimeDefault')"
        )
    return problems


def _iter_deploy_workload_pod_specs():
    """Yield (manifest_name, doc_kind, doc_name, pod_spec) for every
    Deployment / StatefulSet / DaemonSet / Job / CronJob under deploy/
    AND the kind-validation helm-chart overlay under scripts/manifests/
    (issue #388 widening). Mirrors `_iter_workload_containers` but
    yields the pod-level spec, which is where `securityContext` (the
    #161 defense-in-depth block) lives. Defensive YAML handling matches
    the container iterator — hand-edited indentation quirks in some
    deploy/ manifests would otherwise turn this into a parse-error
    tripwire unrelated to the acceptance criterion.

    Issue #388 scope note: same as `_iter_workload_containers` — the
    stateful helm-chart stand-ins (mongo, redis) are explicitly skipped
    because follow-up #394 owns their hardening. See that helper's
    docstring for the #388/#394 division-of-labor rationale.
    """
    manifests = [
        ("deploy/", DEPLOY.glob("*.yaml")),
        (
            "scripts/manifests/",
            sorted((Path(__file__).resolve().parents[1] / "scripts" / "manifests").glob("*.yaml")),
        ),
    ]
    skipped_helm_overlay_files = {"01-mongo.yaml", "02-redis.yaml"}
    for prefix, paths in manifests:
        for path in paths:
            if prefix == "deploy/":
                label_prefix = ""
            else:
                label_prefix = prefix
                if path.name in skipped_helm_overlay_files:
                    continue
            try:
                docs = list(yaml.safe_load_all(path.read_text()))
            except yaml.YAMLError:
                continue
            for doc in docs:
                if not doc:
                    continue
                kind = doc.get("kind")
                name = doc.get("metadata", {}).get("name", "<unnamed>")
                label = f"{label_prefix}{path.name}"
                if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
                    pod_spec = (
                        doc.get("spec", {}).get("template", {}).get("spec", {})
                    )
                    yield label, kind, name, pod_spec
                elif kind == "CronJob":
                    pod_spec = (
                        doc.get("spec", {})
                        .get("jobTemplate", {})
                        .get("spec", {})
                        .get("template", {})
                        .get("spec", {})
                    )
                    yield label, kind, name, pod_spec


def test_all_workloads_have_pod_level_securitycontext():
    """Issue #161 acceptance: every operator-managed workload — the
    operator Deployment, the prune CronJob, AND every archival Job
    generated by archival.py — carries a non-empty pod-level
    securityContext that satisfies the PSS `restricted` baseline
    (runAsNonRoot + runAsUser + seccompProfile). The archival Job
    manifest lives in code, not deploy/, so this test exercises
    ``build_archival_job()`` to cover it."""
    offenders = []
    for path_name, kind, name, pod_spec in _iter_deploy_workload_pod_specs():
        problems = _pod_securitycontext_problems(pod_spec.get("securityContext"))
        if problems:
            offenders.append((path_name, kind, name, problems))

    # Generated archival Jobs — not in deploy/, so build one on the fly
    # and assert on its pod-level securityContext the same way.
    from openstudio_operator.archival import build_archival_job
    from openstudio_operator.config import StoragePolicy

    job = build_archival_job(
        "64f0c8e2a1b3c4d5e6f7a8b9",
        StoragePolicy(
            archive_to_s3=True,
            backend="s3",
            bucket="os-archives",
            secret_ref="os-archive-creds",
        ),
        "openstudio-server",
    )
    archival_pod_spec = job.get("spec", {}).get("template", {}).get("spec", {})
    problems = _pod_securitycontext_problems(
        archival_pod_spec.get("securityContext")
    )
    if problems:
        offenders.append(("archival.py", "Job", "<generated>", problems))

    assert not offenders, (
        "workloads missing #161 pod-level securityContext baseline: "
        f"{offenders}"
    )


def test_operator_deployment_pod_securitycontext_hardened():
    """Issue #161 targeted test: the operator Deployment's pod template
    carries the same baseline as the prune CronJob (deploy/storage-cronjob
    .yaml:97-109) — runAsNonRoot, runAsUser 1000, seccompProfile
    RuntimeDefault, fsGroup 1000 (the last one makes the emptyDir /tmp
    mount writable by UID 1000). Mirrors
    ``test_storage_cronjob_pod_securitycontext_hardened``."""
    pod = OPERATOR_DEPLOYMENT["spec"]["template"]["spec"]
    sc = pod["securityContext"]
    assert sc["runAsNonRoot"] is True, sc
    assert sc["runAsUser"] == 1000, sc
    assert sc["seccompProfile"]["type"] == "RuntimeDefault", sc
    assert sc["fsGroup"] == 1000, sc


# ---- Issue #391: operator Deployment liveness/readiness probes -------
#
# Single-replica + Recreate (no leader election, no second replica) means
# the kubelet is the ONLY restart path for a wedged operator: a deadlock,
# an indefinitely-blocking REST call inside the GET-only retry envelope,
# a GC pause, or a stuck list_crs in the singleton guard's _gated wrapper
# can stop the four @kopf.timer handlers while the pod stays `Running`.
# Both probes hit the /metrics endpoint on the named port `metrics`
# (containerPort 9090, issue #17) — the prometheus_client HTTP server is
# the only in-process surface that answers while the operator is healthy.


def test_operator_deployment_has_liveness_and_readiness_probes():
    """Issue #391 acceptance: BOTH probes exist on the operator container
    and hit the metrics endpoint. Pre-fix the container had neither — a
    wedged tick was invisible to Kubernetes (pod `Running`, kubelet never
    restarts, handlers silently stop firing)."""
    container = OPERATOR_DEPLOYMENT["spec"]["template"]["spec"]["containers"][0]
    for probe_kind in ("livenessProbe", "readinessProbe"):
        assert probe_kind in container, (
            f"operator container must declare {probe_kind} against /metrics "
            f"(issue #391); got keys {sorted(container)}"
        )
        probe = container[probe_kind]
        http_get = probe.get("httpGet")
        assert http_get is not None, (
            f"{probe_kind} must be an httpGet probe against the metrics endpoint"
        )
        assert http_get.get("path") == "/metrics", http_get
        # The named port (containerPort 9090, issue #17) is preferred over
        # a bare integer so a future port renumbering cannot desync the
        # probes from the declared `ports:` block.
        assert http_get.get("port") == "metrics", http_get
        assert probe.get("timeoutSeconds") == 5, probe
        assert probe.get("failureThreshold") == 3, probe


def test_operator_deployment_liveness_restarts_wedged_operator_within_90s():
    """Issue #391 acceptance: the kubelet restarts a wedged operator that
    has stopped answering /metrics for ~90s — failureThreshold (3) ×
    periodSeconds (30) ≥ 90. Also pins initialDelaySeconds (60) high
    enough that a slow first reconciliation (CRD list, mongo/redis
    reachability checks) cannot trip liveness on a healthy boot, and the
    readiness cadence (initialDelay 5, period 10) so boot-time readiness
    is detected promptly without flapping."""
    container = OPERATOR_DEPLOYMENT["spec"]["template"]["spec"]["containers"][0]
    liveness = container["livenessProbe"]
    assert liveness["periodSeconds"] == 30, liveness
    assert liveness["initialDelaySeconds"] == 60, liveness
    budget = liveness["failureThreshold"] * liveness["periodSeconds"]
    assert budget >= 90, (
        f"liveness failureThreshold * periodSeconds must be >= 90s so a "
        f"wedged operator is restarted within ~90s; got {budget}s "
        f"({liveness})"
    )
    readiness = container["readinessProbe"]
    assert readiness["initialDelaySeconds"] == 5, readiness
    assert readiness["periodSeconds"] == 10, readiness


# ---- Issue #396: optional tls-ca-bundle Secret volume + recipe ------
#
# OPENSTUDIO_TLS_CA_BUNDLE ships as `value: ""` (the #242 opt-in fence)
# and openstudio_client._resolve_tls_ca_bundle (#296) validates any
# non-empty value: path must exist and carry a BEGIN CERTIFICATE PEM
# marker, else every tick aborts with OperatorConfigError. Pre-#396 the
# manifest had NO matching volumeMount, no Secret recipe, and no
# convention for where the bundle lives in the pod — a corporate-PKI
# admin following the env-var comment had no documented path from Secret
# to mount to env.
#
# Design tension: a LIVE `volumes[].secret.secretName` referencing a
# Secret that does not exist blocks pod scheduling (FailedMount) — which
# would break the default no-Secret deployment. (`optional: true` on the
# secret volume would avoid that, but it changes the default manifest for
# every cluster and diverges from the repo's established opt-in shape.)
# The fix MIRRORS the #401 metrics-token pattern instead: the
# volumeMount + secret volume ship commented-out with a
# verified-parseable structure, and the default Deployment carries no
# volume reference at all. The tests below pin BOTH states: the default
# as-parsed manifest requires no Secret, and the commented enablement
# stanzas parse back to the exact pinned conventions (mount name,
# mountPath, secretName, key→path mapping).
_OPERATOR_DEPLOYMENT_TEXT = (DEPLOY / "operator-deployment.yaml").read_text()


def _commented_tls_ca_bundle_stanzas():
    """Return the uncommented YAML text of every commented
    ``- name: tls-ca-bundle`` stanza in the raw operator-deployment.yaml.

    Each stanza starts at a ``# - name: tls-ca-bundle`` line and consumes
    consecutive comment lines whose decommented content is indented (the
    continuation keys of that list item). Prose comments (single space
    after ``#``) and real manifest lines terminate the stanza."""
    stanzas = []
    lines = _OPERATOR_DEPLOYMENT_TEXT.splitlines()
    i = 0
    while i < len(lines):
        if re.match(r"^\s*#\s+- name: tls-ca-bundle\s*$", lines[i]):
            stanza = [re.sub(r"^\s*#\s?", "", lines[i])]
            j = i + 1
            while j < len(lines) and re.match(r"^\s*#\s{2,}\S", lines[j]):
                stanza.append(re.sub(r"^\s*#\s?", "", lines[j]))
                j += 1
            stanzas.append("\n".join(stanza))
            i = j
        else:
            i += 1
    return stanzas


def test_operator_deployment_default_applies_without_tls_ca_bundle_secret():
    """Issue #396 acceptance: the DEFAULT deployment (as shipped, nothing
    uncommented) references no tls-ca-bundle volume or mount, so it
    schedules on clusters where the Secret does not exist. A live
    ``volumes[].secret.secretName: openstudio-tls-ca-bundle`` without the
    Secret would block pod scheduling (FailedMount) — the exact
    default-breaks regression this test fences. The env var stays present
    with ``value: ""`` (the #242 opt-in fence: system trust store unless
    the cluster admin explicitly enables the bundle)."""
    pod = OPERATOR_DEPLOYMENT["spec"]["template"]["spec"]
    container = pod["containers"][0]
    mounts = container.get("volumeMounts") or []
    assert all(m["name"] != "tls-ca-bundle" for m in mounts), (
        "default operator-deployment must NOT declare an active tls-ca-bundle "
        "volumeMount (issue #396): without the Secret the pod never schedules"
    )
    secret_names = [
        (v.get("secret") or {}).get("secretName") for v in pod.get("volumes") or []
    ]
    assert "openstudio-tls-ca-bundle" not in secret_names, (
        "default operator-deployment must NOT declare a live "
        f"openstudio-tls-ca-bundle secret volume (issue #396); got {secret_names}"
    )
    env = {e["name"]: e.get("value") for e in container.get("env") or []}
    assert env.get("OPENSTUDIO_TLS_CA_BUNDLE") == "", (
        "OPENSTUDIO_TLS_CA_BUNDLE must ship as value: \"\" — the #242 opt-in "
        "fence (system trust store) that #396 builds its recipe on top of"
    )


def test_operator_deployment_documents_tls_ca_bundle_enablement_recipe():
    """Issue #396 acceptance: the manifest carries the documented
    enablement path for corporate-PKI cluster admins — the kubectl
    Secret-create command, the in-pod path the admin must set
    OPENSTUDIO_TLS_CA_BUNDLE to, and BOTH commented stanzas
    (volumeMount + secret volume), mirroring the #401 metrics-token
    pattern."""
    text = _OPERATOR_DEPLOYMENT_TEXT
    assert "kubectl -n openstudio-server create secret generic" in text, (
        "the env-var comment must carry the Secret-create command verbatim "
        "(issue #396 acceptance criterion)"
    )
    assert "openstudio-tls-ca-bundle --from-file=ca-bundle.crt" in text, (
        "the Secret-create command must name the pinned Secret + key "
        "'openstudio-tls-ca-bundle --from-file=ca-bundle.crt' (issue #396)"
    )
    assert "/etc/openstudio/tls/ca-bundle.crt" in text, (
        "the manifest must document the in-pod bundle path "
        "/etc/openstudio/tls/ca-bundle.crt the admin sets the env var to"
    )
    assert len(_commented_tls_ca_bundle_stanzas()) == 2, (
        "exactly two commented tls-ca-bundle stanzas must ship: the "
        "volumeMount entry and the volumes[].secret entry (issue #396, "
        "mirroring the #401 pair)"
    )


def test_operator_deployment_tls_ca_bundle_stanzas_parse_to_pinned_shape():
    """Issue #396: the commented enablement stanzas must stay valid YAML —
    a cluster admin uncommenting them blindly gets a parseable manifest
    with the exact pinned conventions: mount name `tls-ca-bundle`
    (linking the volumeMount to the volume), mountPath
    /etc/openstudio/tls, readOnly, secretName
    openstudio-tls-ca-bundle, and the key→path mapping that puts the PEM
    at /etc/openstudio/tls/ca-bundle.crt — the same path the env-var
    comment tells the admin to set."""
    stanzas = _commented_tls_ca_bundle_stanzas()
    items = [item for block in stanzas for item in yaml.safe_load(block)]
    assert len(items) == 2, (
        f"each commented stanza must parse as a one-item YAML list; got {items!r}"
    )
    mounts = [s for s in items if "mountPath" in s]
    vols = [s for s in items if "secret" in s]
    assert len(mounts) == 1 and len(vols) == 1, items
    assert mounts[0] == {
        "name": "tls-ca-bundle",
        "mountPath": "/etc/openstudio/tls",
        "readOnly": True,
    }, mounts[0]
    assert vols[0]["name"] == "tls-ca-bundle", vols[0]
    assert vols[0]["secret"]["secretName"] == "openstudio-tls-ca-bundle", vols[0]
    assert vols[0]["secret"]["items"] == [
        {"key": "ca-bundle.crt", "path": "ca-bundle.crt"}
    ], vols[0]


# ---- Issue #112: namespace NetworkPolicy ----------------------------
#
# The operator surface (operator Deployment + storage-prune CronJob +
# archival Jobs) MUST have a default-deny egress policy in
# `deploy/network-policy.yaml`. The structural test below quantifies the
# acceptance: at least one NetworkPolicy resource per actor class with
# `policyTypes: [Egress]` and a default-deny shape (empty egress list OR
# an explicit allow-list with no wider-than-needed cidrs).
NETPOL_DOCS = list(yaml.safe_load_all((DEPLOY / "network-policy.yaml").read_text()))


def test_network_policy_manifest_exists_and_is_namespaced():
    """Egress isolation in `openstudio-server` — issue #112 acceptance.
    #166 adds a single Ingress-only policy (`openstudio-operator-metrics-
    ingress`); the structural invariant is that every policy targets
    the namespace and declares one of the supported policyTypes."""
    assert NETPOL_DOCS, "deploy/network-policy.yaml is missing or empty"
    kinds = {d["kind"] for d in NETPOL_DOCS if d}
    assert kinds == {"NetworkPolicy"}, kinds
    for d in NETPOL_DOCS:
        assert d["metadata"]["namespace"] == "openstudio-server"
        # Every NetworkPolicy MUST declare at least one policyType. Egress
        # is the default for the operator surface (#112); the metrics
        # ingress policy (#166) is Ingress-only and that is intentional.
        policy_types = set(d["spec"]["policyTypes"])
        assert policy_types & {"Egress", "Ingress"}, (
            f"policy {d['metadata']['name']} must declare Egress or "
            f"Ingress in policyTypes, got {policy_types}"
        )


def test_network_policy_default_deny_for_operator_surface():
    """Default-deny shape: at least one NetworkPolicy named ``*-deny-egress``
    exists in ``deploy/network-policy.yaml`` and its ``spec.egress`` list
    is empty (the strictest restrictive shape — an explicit allow-list
    policy must be added in a sibling policy to grant egress). The
    podSelector scope (operator-managed surface only) is a separate
    invariant covered by
    ``test_deny_egress_policy_selector_is_not_empty`` (issue #154) and
    the helm-chart isolation invariant by
    ``test_deny_egress_does_not_select_helm_chart_pods``."""
    # Find a deny-all policy by name prefix
    deny_policies = [
        d for d in NETPOL_DOCS
        if "deny-egress" in d["metadata"]["name"]
    ]
    assert deny_policies, "no default-deny NetworkPolicy found in deploy/network-policy.yaml"
    deny = deny_policies[0]
    assert deny["spec"]["egress"] in ([], None), (
        f"default-deny must have empty egress list, got {deny['spec']['egress']!r}"
    )


def test_network_policy_storage_egress_is_https_only():
    """The archival / prune egress allow-list permits HTTPS (TCP 443) only.
    Plaintext IMAP/SMTP/arbitrary ports must NOT be in the allow-list — the
    storage backend is reached over TLS, period."""
    storage_policies = [
        d for d in NETPOL_DOCS
        if "storage-egress" in d["metadata"]["name"]
    ]
    assert storage_policies, "no storage-egress NetworkPolicy found"
    storage = storage_policies[0]
    ports = []
    for rule in storage["spec"]["egress"]:
        ports.extend(rule.get("ports", []))
    port_numbers = {p["port"] for p in ports}
    assert 443 in port_numbers, port_numbers
    # No plaintext risky ports anywhere in the allow-list
    plaintext_risky = {25, 110, 143, 587, 993, 995}
    assert port_numbers.isdisjoint(plaintext_risky), (
        f"plaintext risky ports present in egress allow: {port_numbers & plaintext_risky}"
    )


def test_network_policy_rfc1918_egress_excluded_for_storage():
    """Public Internet egress (TCP 443) for storage backends MUST exclude
    RFC1918 ranges — an archival Job should never egress to a private net."""
    storage_policies = [
        d for d in NETPOL_DOCS
        if "storage-egress" in d["metadata"]["name"]
    ]
    storage = storage_policies[0]
    # Walk every egress rule, every ipBlock within, and confirm
    # RFC1918 is in the `except:` list of the 0.0.0.0/0 block.
    for rule in storage["spec"]["egress"]:
        for to in rule.get("to", []):
            ip_block = to.get("ipBlock", {})
            if ip_block.get("cidr") == "0.0.0.0/0":
                excepts = set(ip_block.get("except", []))
                rfc1918 = {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"}
                assert rfc1918.issubset(excepts), (
                    f"0.0.0.0/0 egress is missing RFC1918 exceptions: {excepts}"
                )


def test_network_policy_link_local_and_cgnat_egress_excluded_for_storage():
    """Issue #477: the storage-egress ``0.0.0.0/0`` ipBlock except list MUST
    additionally exclude link-local 169.254.0.0/16 (the AWS/GCP/Azure instance
    metadata service range — a compromised archival pod holding envFrom S3/
    GCS/Azure credentials could otherwise reach IMDS to steal node IAM / kubelet
    identities) and CGNAT 100.64.0.0/10, alongside the RFC1918 entries. All
    five ranges asserted together so a future edit that drops any one of them
    fails here (mirrors the metrics-ingress regression-test style)."""
    storage_policies = [
        d for d in NETPOL_DOCS
        if "storage-egress" in d["metadata"]["name"]
    ]
    assert storage_policies, "no storage-egress NetworkPolicy found"
    storage = storage_policies[0]
    for rule in storage["spec"]["egress"]:
        for to in rule.get("to", []):
            ip_block = to.get("ipBlock", {})
            if ip_block.get("cidr") == "0.0.0.0/0":
                excepts = set(ip_block.get("except", []))
                required = {
                    "10.0.0.0/8",
                    "172.16.0.0/12",
                    "192.168.0.0/16",
                    "169.254.0.0/16",
                    "100.64.0.0/10",
                }
                assert required.issubset(excepts), (
                    f"0.0.0.0/0 egress is missing link-local/CGNAT/RFC1918 "
                    f"exceptions (issue #477): {excepts}"
                )


# ---- Issue #166: /metrics ingress is restricted -------------------
#
# The operator exposes /metrics on port 9090 in plaintext with no auth
# (metrics.py:204 + deploy/operator-deployment.yaml:43). On a cluster
# without a default-deny-ingress CNI plugin, any pod could scrape queue
# depths, eviction counts, status-conflict retries, and the
# resque_workers_seen_max gauge — useful reconnaissance for an attacker
# timing attacks against operator busy periods. The fix is a NetworkPolicy
# in deploy/network-policy.yaml that restricts ingress to port 9090 with
# a Prometheus-style namespace selector and a same-namespace peer allow.


def _metrics_ingress_policy():
    matches = [
        d for d in NETPOL_DOCS
        if "metrics-ingress" in d["metadata"]["name"]
    ]
    assert matches, (
        "no metrics-ingress NetworkPolicy in deploy/network-policy.yaml — "
        "the operator's /metrics endpoint on port 9090 is unauthenticated "
        "and would be readable by every in-cluster pod (#166)"
    )
    return matches[0]


def test_network_policy_metrics_ingress_exists():
    """Issue #166 acceptance: a dedicated ingress policy for /metrics exists,
    targets the operator pod, and declares policyTypes: [Ingress]."""
    policy = _metrics_ingress_policy()
    assert policy["metadata"]["namespace"] == "openstudio-server"
    # The policy must select the operator pod. The selector is the union of
    # `app.kubernetes.io/managed-by: openstudio-operator` (added by the
    # operator's Helm/Kustomize install path on hardened clusters) and
    # `app: openstudio-operator` (always present on the pod template at
    # deploy/operator-deployment.yaml:20). Both labels appear in
    # matchLabels — cluster admins can tighten or loosen either.
    selector = policy["spec"]["podSelector"]["matchLabels"]
    assert selector.get("app") == "openstudio-operator"
    assert "Ingress" in policy["spec"]["policyTypes"]


def test_network_policy_metrics_ingress_restricts_to_port_9090():
    """The only port allowed by the ingress rule is 9090/TCP — no plaintext
    risky ports, no other operator surface (e.g. the kube-apiserver proxy
    on 443) leaks through this policy."""
    policy = _metrics_ingress_policy()
    ingress_rules = policy["spec"]["ingress"]
    assert ingress_rules, "ingress rules must not be empty"
    allowed_ports = set()
    for rule in ingress_rules:
        for port in rule.get("ports", []):
            assert port["protocol"] == "TCP", (
                f"only TCP allowed on /metrics, got {port['protocol']}"
            )
            allowed_ports.add(port["port"])
    assert allowed_ports == {9090}, (
        f"/metrics ingress must allow only port 9090, got {allowed_ports}"
    )


def test_network_policy_metrics_ingress_has_prometheus_and_peer_allow():
    """The from-block must (a) target the cluster's scraper namespace AND
    (b) allow a same-namespace peer only when that peer opts in via the
    project label `app.kubernetes.io/component: metrics-scraper`. An empty
    `podSelector: {}` is a regression — every helm-chart pod in
    openstudio-server carries some `app.kubernetes.io/component` value
    (web / web-background / worker / db / redis / queue / nfs), but none
    of them is `metrics-scraper`, so an unscoped allow would let them
    scrape the plaintext /metrics endpoint (issue #295). A cluster-admin
    namespaceSelector renaming is allowed; a wholesale removal of either
    peer, or a return to `podSelector: {}`, would silently re-open the
    endpoint."""
    policy = _metrics_ingress_policy()
    rule = policy["spec"]["ingress"][0]
    from_selectors = rule["from"]
    # Flatten the from[] peer list (each entry is one AND-of-ORs selector).
    has_namespace_selector = any(
        "namespaceSelector" in peer for peer in from_selectors
    )
    same_ns_peers = [
        peer for peer in from_selectors if "podSelector" in peer
    ]
    has_label_scoped_peer = any(
        peer["podSelector"].get("matchLabels", {}).get(
            "app.kubernetes.io/component"
        )
        == "metrics-scraper"
        for peer in same_ns_peers
    )
    has_empty_podselector = any(
        peer.get("podSelector") == {} for peer in same_ns_peers
    )
    assert has_namespace_selector, (
        "metrics-ingress must include a namespaceSelector pointing at the "
        "cluster's scraper namespace (default `prometheus`); see AGENTS.md "
        "Working rules for the cluster-admin opt-in."
    )
    assert has_label_scoped_peer, (
        "metrics-ingress must include a podSelector matching "
        "{app.kubernetes.io/component: metrics-scraper} so the same-"
        "namespace peer allow is opt-in only. See AGENTS.md /metrics "
        "ingress rule (issue #295)."
    )
    assert not has_empty_podselector, (
        "metrics-ingress must NOT use `podSelector: {}` — that selector "
        "matches every pod in openstudio-server (web, web-background, "
        "worker, db, redis, queue, nfs) and would re-open the plaintext "
        "/metrics endpoint to every helm-chart pod (issue #295)."
    )


# ---- Issue #154: deny-egress NetworkPolicy must NOT match every pod --
#
# `openstudio-operator-deny-egress` is the operator-managed-surface default-
# deny. An empty `podSelector: {}` selects every pod in `openstudio-server`,
# which means the helm chart's `web`, `web-background`, `worker`, `queue`
# (Redis), `db` (MongoDB), and NFS pods inherit the deny BEFORE any allow-
# list can apply — silently breaking DNS + Service egress for the deployment
# the operator is supposed to manage. The fix narrows the selector to the
# operator-managed surface; the three tests below pin that narrowing.


def _deny_egress_policy():
    matches = [
        d for d in NETPOL_DOCS
        if "deny-egress" in d["metadata"]["name"]
    ]
    assert matches, (
        "no deny-egress NetworkPolicy in deploy/network-policy.yaml — "
        "issue #112 default-deny is missing"
    )
    return matches[0]


def _allow_egress_policies():
    """Every egress policy other than the deny itself — i.e. the allow-
    lists the deny must be consistent with. Excludes the metrics-ingress
    policy (Ingress-only, no egress allow to reconcile with)."""
    deny_name = _deny_egress_policy()["metadata"]["name"]
    out = []
    for d in NETPOL_DOCS:
        if d["metadata"]["name"] == deny_name:
            continue
        if d["spec"].get("policyTypes") != ["Egress"]:
            continue
        out.append(d)
    return out


def test_deny_egress_policy_selector_is_not_empty():
    """Issue #154 acceptance #1: `openstudio-operator-deny-egress`'s
    podSelector must not be empty. An empty selector (`podSelector: {}`)
    matches every pod in the namespace and silently breaks egress for the
    helm chart's `web`, `worker`, `queue`, etc. The selector MUST declare
    at least one of `matchLabels` or `matchExpressions` to qualify as
    "operator-managed surface only"."""
    deny = _deny_egress_policy()
    selector = deny["spec"]["podSelector"]
    assert selector, (
        f"deny-egress podSelector is empty ({selector!r}); an empty "
        "selector matches every pod in the namespace and breaks helm "
        "chart egress (issue #154)"
    )
    has_labels = bool(selector.get("matchLabels"))
    has_exprs = bool(selector.get("matchExpressions"))
    assert has_labels or has_exprs, (
        "deny-egress podSelector must declare at least one label or "
        f"expression, got {selector!r}"
    )


def test_deny_egress_selector_matches_allow_policies():
    """Issue #154 acceptance #2: the deny selector must be COVERED by the
    union of the allow-policy selectors — every label key the deny matches
    on must also be matched by at least one allow policy. Otherwise the
    deny would select pods that no allow covers, and those pods would
    have zero egress (the exact bug the empty selector caused for the helm
    chart pods)."""
    deny = _deny_egress_policy()
    allows = _allow_egress_policies()
    assert allows, (
        "no Egress-typed allow NetworkPolicy found in deploy/network-policy."
        "yaml — without an allow, the deny has nothing to reconcile against"
    )
    deny_match_labels = deny["spec"]["podSelector"].get("matchLabels", {})
    deny_match_expr_keys = {
        expr["key"]
        for expr in deny["spec"]["podSelector"].get("matchExpressions", [])
    }
    deny_keys = set(deny_match_labels) | deny_match_expr_keys

    # Collect, per allow policy, the set of label keys it matches on
    allow_keys_by_policy = {}
    for allow in allows:
        keys = set(allow["spec"]["podSelector"].get("matchLabels", {}))
        keys.update(
            expr["key"]
            for expr in allow["spec"]["podSelector"].get("matchExpressions", [])
        )
        allow_keys_by_policy[allow["metadata"]["name"]] = keys

    # Every deny key must appear in at least one allow selector
    for key in deny_keys:
        covered_by = [
            name for name, keys in allow_keys_by_policy.items() if key in keys
        ]
        assert covered_by, (
            f"deny-egress requires label {key!r} but no allow policy "
            f"matches on that key — pods selected by the deny alone "
            f"would lose all egress (issue #154)"
        )


def test_deny_egress_does_not_select_helm_chart_pods():
    """Issue #154 acceptance #3: the deny selector must NOT match any helm
    chart pod. The helm chart pods (web, web-background, worker, queue/
    redis, db, NFS) carry the labels actually emitted by
    scripts/manifests/* and the NFS deployment; the deny must leave them
    alone so they keep inheriting the cluster default egress."""
    deny = _deny_egress_policy()
    selector = deny["spec"]["podSelector"]

    # Helm chart pod label sets as actually emitted by scripts/manifests/
    # and the NFS pod. Each is a dict mapping the pod labels a selector
    # would have to consider. None of these carry the operator-managed
    # labels the deny selects on.
    helm_chart_pods = [
        {"app": "web"},
        {"app": "web-background"},
        {"app": "worker"},
        {"app": "redis"},          # the Resque `queue` Service
        {"app": "db"},             # MongoDB
        {"app": "nfs"},
    ]

    for pod_labels in helm_chart_pods:
        # matchLabels: AND across all required label keys
        if "matchLabels" in selector:
            matches = all(
                pod_labels.get(k) == v
                for k, v in selector["matchLabels"].items()
            )
            assert not matches, (
                f"deny-egress matches helm chart pod {pod_labels!r} via "
                f"matchLabels={selector['matchLabels']!r}; the deny would "
                f"break egress for this pod (issue #154)"
            )
        # matchExpressions: AND across all required expressions
        for expr in selector.get("matchExpressions", []):
            if expr["operator"] != "In":
                continue
            val = pod_labels.get(expr["key"])
            assert val not in expr["values"], (
                f"deny-egress expression {expr!r} matches helm chart pod "
                f"{pod_labels!r}; the deny would break egress for this "
                f"pod (issue #154)"
            )


# ---- Issue #224: operator pod template carries every NetworkPolicy
# selector's matchLabels -------------------------------------------
#
# The deny-egress (#112) and metrics-ingress (#166) NetworkPolicies in
# `deploy/network-policy.yaml` select the operator pod via the
# conjunction `app: openstudio-operator` AND `app.kubernetes.io/
# managed-by: openstudio-operator`; the allow-egress policy selects via
# `app: openstudio-operator` alone. Pre-fix, the operator Deployment
# template at deploy/operator-deployment.yaml declared ONLY `app:
# openstudio-operator` — so the deny and the metrics-ingress selectors
# failed to select the operator pod:
#
#   (a) the operator could egress to any destination on any port,
#       contradicting network-policy.yaml:8 ("operator does NOT egress
#       to public Internet");
#   (b) the plaintext Prometheus endpoint on port 9090 was reachable
#       from every pod in every namespace on clusters without a
#       default-deny-ingress CNI plugin (the #166 attack surface).
#
# The fix adds `app.kubernetes.io/managed-by: openstudio-operator` to
# the operator pod template. This test asserts the operator pod template
# carries the union of labels every matchLabels selector requires, so
# the regression cannot silently recur.
#
# Scope guard: this test is operator-only. The prune CronJob
# (deploy/storage-cronjob.yaml:91-93) and the archival Job
# (archival.py:185-189) carry `app.kubernetes.io/managed-by:
# openstudio-operator` but not `app: openstudio-operator` — they are
# owned by #112/#166 and explicitly out of scope for #224, so the test
# does not flag them.


def test_operator_pod_template_carries_union_of_network_policy_matchlabels():
    """Issue #224 acceptance: the operator pod template must carry every
    label required by every NetworkPolicy `matchLabels` block in
    deploy/network-policy.yaml. Pre-fix the operator pod had only
    `app: openstudio-operator`, which is sufficient for the allow-egress
    selector but NOT for the deny-egress + metrics-ingress selectors
    that require the conjunction — so those policies never selected the
    operator pod. Post-fix the operator pod template carries both
    labels and every matchLabels selector selects it.

    The test scopes to the operator Deployment pod template only;
    the prune CronJob + archival Job templates are owned by #112/#166
    and out of scope per the issue's scope guard."""
    operator_labels = OPERATOR_DEPLOYMENT["spec"]["template"]["metadata"]["labels"]
    offenders = []
    for policy in NETPOL_DOCS:
        # Issue #306 — the storage-pruner-metrics-ingress policy is
        # scoped to the CronJob pod (label `app.kubernetes.io/component:
        # storage-pruner`), NOT the operator pod. The operator is
        # explicitly out of scope here; the matching acceptance criterion
        # for the new policy lives in
        # `test_storage_cronjob_pod_labels_match_ingress_policy_selectors`.
        if "storage-pruner-metrics-ingress" in policy["metadata"]["name"]:
            continue
        match_labels = policy["spec"].get("podSelector", {}).get("matchLabels", {})
        if not match_labels:
            # matchExpressions-only selectors are out of scope for this
            # test (allow-dns, storage-egress). The test exercises the
            # structural matchLabels invariant #224 violates; matching
            # expressions have their own invariant in
            # `test_deny_egress_selector_matches_allow_policies`.
            continue
        missing = {
            key: {"selector_requires": value, "pod_has": operator_labels.get(key)}
            for key, value in match_labels.items()
            if operator_labels.get(key) != value
        }
        if missing:
            offenders.append((policy["metadata"]["name"], missing))
    assert not offenders, (
        "operator pod template is missing labels required by these "
        f"NetworkPolicies (#224): {offenders}"
    )


# ---- Issue #225: API-server egress must not use kube-dns placeholder --
#
# Pre-fix, the FIRST egress block in `openstudio-operator-allow-egress`
# used `namespaceSelector: kubernetes.io/metadata.name: default` +
# `podSelector: k8s-app: kube-dns` on port 443 as a "placeholder" for the
# API server. The kube-dns pods live in `kube-system`, not `default`, so
# the rule was structurally pointless AND opened a credential-exfiltration
# path: an attacker who can create a pod in the `default` namespace with
# the `k8s-app: kube-dns` label gets the operator talking to it on port
# 443. NetworkPolicy entries are additive — both the mislabeled block AND
# the correct `component: kube-apiserver` block applied.
#
# Scope guard: the legitimate DNS egress allow (`openstudio-operator-
# allow-dns`) uses the SAME `k8s-app: kube-dns` selector pair but with
# `namespaceSelector: kube-system` and ports 53 (UDP+TCP) — that allow is
# the actual legitimate target and must remain. The bug pattern is the
# SPECIFIC combination of port 443 (the API server port) + the kube-dns
# selector. We assert on that combination so the legitimate DNS allow is
# preserved but the misconfiguration cannot silently recur.


def test_no_port_443_egress_block_uses_kube_dns_placeholder_selector():
    """Issue #225 regression fence: no NetworkPolicy egress block in
    deploy/network-policy.yaml targets port 443 (the API server port) using
    `k8s-app: kube-dns` as a podSelector. Pre-fix, the FIRST egress block in
    `openstudio-operator-allow-egress` used namespaceSelector: default +
    podSelector: k8s-app: kube-dns on port 443 as a "placeholder" for the
    API server. Kube-dns pods live in kube-system, not default, so the
    rule was structurally pointless AND opened a credential-exfiltration
    path. The fix removes the mislabeled first egress block; this test
    pins that removal by catching any port-443 egress block using the
    kube-dns selector — i.e. any block pretending kube-dns is on port 443.

    Scoped to port 443 because that is the API server port and the bug
    pattern; the legitimate DNS allow on port 53 (UDP+TCP) is preserved
    and intentionally not flagged by this test."""
    offenders = []
    for policy in NETPOL_DOCS:
        egress_rules = policy["spec"].get("egress") or []
        for rule_idx, rule in enumerate(egress_rules):
            ports = {p.get("port") for p in rule.get("ports", [])}
            if 443 not in ports:
                continue
            for to_idx, to in enumerate(rule.get("to", [])):
                pod_labels = to.get("podSelector", {}).get("matchLabels", {})
                if pod_labels.get("k8s-app") == "kube-dns":
                    offenders.append(
                        (policy["metadata"]["name"], rule_idx, to_idx)
                    )
    assert not offenders, (
        f"port-443 egress blocks use k8s-app: kube-dns placeholder "
        f"selector (#225 regression): {offenders}"
    )


# ---- Issue #578: kube-apiserver egress peer must be matchable ----------
#
# Pre-fix, the first egress block of `openstudio-operator-allow-egress`
# was a bare `podSelector: {component: kube-apiserver}` — and a `to:` peer
# with ONLY a podSelector is NAMESPACE-LOCAL: it matches pods in
# `openstudio-server`, never in `kube-system` where kube-apiserver pods
# live. The rule matched zero endpoints, so on a CNI that enforces
# NetworkPolicy the `openstudio-operator-deny-egress` default-deny
# blackholed the operator's apiserver traffic (kubeconfig watches,
# StatusStore RMW). kindnet does not enforce NetworkPolicy, which is why
# the kind validation cluster never caught it.
#
# The fix is the compound peer — namespaceSelector
# {kubernetes.io/metadata.name: kube-system} + podSelector
# {component: kube-apiserver} in the SAME `to:` entry (logical AND) —
# which matches self-hosted API servers (kubeadm / kind / self-managed).
# Hosted control planes (EKS / GKE / AKS) run the API server outside the
# cluster where no pod selector can match; those environments need an
# ipBlock peer for the API endpoint CIDR, shipped commented-out with the
# per-environment guidance because no single CIDR is correct everywhere
# and a wide ipBlock would reopen the exfil surface #112 closed.


def _operator_allow_egress_policy():
    matches = [
        d for d in NETPOL_DOCS
        if d["metadata"]["name"] == "openstudio-operator-allow-egress"
    ]
    assert matches, (
        "no openstudio-operator-allow-egress NetworkPolicy in "
        "deploy/network-policy.yaml — the deny-egress (#112) would leave "
        "the operator pod with zero egress"
    )
    return matches[0]


def _apiserver_egress_rule(policy):
    """The TCP/443 egress rule of an Egress policy (the API server port)."""
    for rule in policy["spec"]["egress"]:
        ports = {p.get("port") for p in rule.get("ports", [])}
        if 443 in ports:
            return rule
    raise AssertionError(
        "no TCP/443 egress rule in openstudio-operator-allow-egress — the "
        "operator cannot reach the kube-apiserver (#578)"
    )


def test_operator_allow_egress_apiserver_peer_is_compound_kube_system():
    """Issue #578: the TCP/443 egress rule's `to:` block MUST contain the
    compound peer — namespaceSelector {kubernetes.io/metadata.name:
    kube-system} AND podSelector {component: kube-apiserver} together in
    ONE peer entry (logical AND). A bare podSelector-only peer is
    namespace-local (matches only openstudio-server pods), so the pre-fix
    rule matched zero endpoints and an enforcing CNI + deny-egress
    blackholed the operator's apiserver traffic. The bare shape is
    asserted absent so the regression cannot silently recur."""
    rule = _apiserver_egress_rule(_operator_allow_egress_policy())
    peers = rule.get("to", [])
    compound = [
        peer for peer in peers
        if peer.get("namespaceSelector", {}).get("matchLabels", {}).get(
            "kubernetes.io/metadata.name"
        ) == "kube-system"
        and peer.get("podSelector", {}).get("matchLabels", {}).get(
            "component"
        ) == "kube-apiserver"
    ]
    assert compound, (
        "the TCP/443 egress rule must carry the compound peer — "
        "namespaceSelector {kubernetes.io/metadata.name: kube-system} + "
        "podSelector {component: kube-apiserver} in the SAME `to:` entry "
        "(issue #578); a bare podSelector is namespace-local and never "
        "matches the kube-system apiserver pods"
    )
    bare_apiserver = [
        peer for peer in peers
        if peer.get("podSelector", {}).get("matchLabels", {}).get(
            "component"
        ) == "kube-apiserver"
        and "namespaceSelector" not in peer
    ]
    assert not bare_apiserver, (
        "the TCP/443 egress rule must NOT carry a podSelector-only "
        "kube-apiserver peer — namespace-local, matches zero endpoints, "
        f"blackholes apiserver traffic on enforcing CNIs (#578): {peers!r}"
    )


def test_operator_allow_egress_apiserver_peer_documents_ipblock_alternative():
    """Issue #578: the TCP/443 egress rule must carry a commented-out
    `ipBlock` alternative for hosted control planes (EKS / GKE / AKS run
    the API server outside the cluster — no pod selector can match it;
    the per-environment choice is an ipBlock for the API endpoint CIDR),
    plus the comment explaining the namespace-local-podSelector trap.
    Asserted on the RAW yaml (yaml.safe_load strips comments) within the
    openstudio-operator-allow-egress document only."""
    raw = (DEPLOY / "network-policy.yaml").read_text()
    start = raw.index("name: openstudio-operator-allow-egress")
    end = raw.index("\n---", start)
    allow_egress_doc = raw[start:end]
    commented_ipblock_lines = [
        line for line in allow_egress_doc.splitlines()
        if "ipBlock" in line and line.lstrip().startswith("#")
    ]
    assert commented_ipblock_lines, (
        "the TCP/443 egress rule must document the hosted-control-plane "
        "ipBlock alternative as a commented entry (issue #578) — hosted "
        "API servers match no pod selector"
    )
    doc_lower = allow_egress_doc.lower()
    assert "namespace-local" in doc_lower, (
        "the TCP/443 egress rule must carry a comment explaining the "
        "namespace-local-podSelector trap (issue #578 acceptance)"
    )
    assert "hosted" in doc_lower, (
        "the ipBlock alternative comment must name the hosted-control-"
        "plane environment it exists for (issue #578)"
    )


# ---- Issue #294: prune SA's batch/jobs verbs are scoped via a
# ValidatingAdmissionPolicy ---------------------------------------------
#
# RBAC `PolicyRule` does NOT support `labelSelector` and `resourceNames`
# only accepts exact strings (no globs). The prune SA's Role at
# deploy/storage-cronjob.yaml:45-47 therefore cannot express "only
# archival Jobs" in RBAC alone — its `batch/jobs` create|delete allow
# list accepts any Job, which a compromised prune pod could exploit to
# spawn an arbitrary Job with any name, any image, any service account
# (an exfiltration path the destructive-fence tests above cannot catch).
#
# The acceptance criterion (issue #294) lists three options:
#   1. RBAC `labelSelector` — NOT supported by the RBAC v1 API.
#      would silently swallow a value at import time).
#   2. Every test has a docstring. This is the cheapest fence against
#      the "silent invariant" failure: an author who cannot articulate
#      the invariant in a docstring cannot truthfully claim the
#      assertion enforces it.
#   3. Every test body has at least one ``assert`` statement OR a
#      ``raise``. Empty bodies (or ``pass``-only bodies) pass pytest
#      with a vacuous success — the #291 class of failure starts
#      exactly there.
#
# This is intentionally a coarse structural check, not a semantic
# "does the comment match the assertion" verifier. That semantic
# check requires reading the test in context and is what the #315
# audit PR body records; automating it would re-create the
# "encode-the-bug-as-a-feature" failure mode in the verifier itself.


def _collect_test_functions():
    """Walk this module's AST and yield (name, FunctionDef) for every
    top-level ``def test_*`` function. Nested defs (helpers prefixed
    ``test_``) are excluded — the convention in this file is helpers
    start with ``_`` and tests start with ``test``, so a top-level
    ``test_*`` is a real test. Nested ``def`` inside a test body
    (e.g. parametrized loops) is also excluded."""
    module = sys.modules[__name__]
    module_file = module.__file__
    if not module_file:
        raise RuntimeError(f"cannot find source file for {__name__!r}")
    source = Path(module_file).read_text(encoding="utf-8")
    tree = ast.parse(source)
    tests = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            tests.append((node.name, node))
    return tests


def test_deploy_manifests_self_collect():
    """Smoke test: ``_collect_test_functions`` finds this test plus every
    other top-level ``test_*`` in the module. The audit meta-test below
    uses the same walker, so this is the canary that the AST walk
    itself is wired up correctly."""
    found = {name for name, _ in _collect_test_functions()}
    assert "test_deploy_manifests_self_collect" in found
    # Cross-check against a hardcoded minimum so adding tests never
    # silently shrinks the audit set (e.g. by renaming them out of the
    # ``test_`` prefix). The number grows as new tests are added.
    assert len(found) >= 28, (
        f"expected at least 28 test_* functions in this file, found "
        f"{len(found)}: {sorted(found)}"
    )


def test_every_test_function_has_docstring():
    """#315 structural fence: every top-level ``test_*`` in this file
    MUST have a docstring. The docstring is the only place the author
    articulates the invariant the test enforces — without one, a
    later reviewer cannot tell whether the assertion still matches the
    comment claim, which is exactly the "asserts the bug as a feature"
    failure mode #315 audits for. A test that is hard to document is a
    test that is hiding its intent."""
    missing = []
    for name, node in _collect_test_functions():
        doc = ast.get_docstring(node)
        if not doc or not doc.strip():
            missing.append(name)
    assert not missing, (
        "tests without docstrings — every test in this file must "
        "articulate the invariant it enforces in the docstring "
        "(issue #315 audit, regression-fence): "
        f"{missing}"
    )


def test_every_test_function_has_assert_or_raise():
    """#315 structural fence: every top-level ``test_*`` MUST contain at
    least one ``assert`` statement OR ``raise`` statement in its body.
    An empty test body (or ``pass``-only body) passes pytest with
    vacuous success and is the structural shape the #291 bug-as-
    feature regression fence took. Walking the AST catches it before
    the test reaches CI."""
    offenders = []
    for name, node in _collect_test_functions():
        has_assert_or_raise = False
        for child in ast.walk(node):
            if isinstance(child, ast.Assert):
                has_assert_or_raise = True
                break
            if isinstance(child, ast.Raise):
                has_assert_or_raise = True
                break
        if not has_assert_or_raise:
            offenders.append(name)
    assert not offenders, (
        "tests with no assert or raise — an empty body is a vacuous "
        "pass and re-creates the #291 'asserts the bug as a feature' "
        "shape (issue #315 audit, regression-fence): "
        f"{offenders}"
    )
# ---- Issue #294: prune SA's batch/jobs verbs are scoped via a
# ValidatingAdmissionPolicy ---------------------------------------------
#
# RBAC `PolicyRule` does NOT support `labelSelector` and `resourceNames`
# only accepts exact strings (no globs). The prune SA's Role at
# deploy/storage-cronjob.yaml:45-47 therefore cannot express "only
# archival Jobs" in RBAC alone — its `batch/jobs` create|delete allow
# list accepts any Job, which a compromised prune pod could exploit to
# spawn an arbitrary Job with any name, any image, any service account
# (an exfiltration path the destructive-fence tests above cannot catch).
#
# The acceptance criterion (issue #294) lists three options:
#   1. RBAC `labelSelector` — NOT supported by the RBAC v1 API.
#   2. RBAC `resourceNames: [oscm-archive-*]` — globs NOT supported by
#      `resourceNames`; only exact strings.
#   3. ValidatingAdmissionPolicy — the only API that can express the
#      "must be archival-labelled" constraint declaratively.
#
# The fix ships a ValidatingAdmissionPolicy + Binding, colocated with
# the storage-cronjob.yaml manifests they constrain. The tests below
# pin the shape: the policy exists, scopes to `openstudio-server` only,
# hooks CREATE/UPDATE/DELETE on `batch/jobs`, requires the archival
# labels, and fails closed by default.

PRUNE_JOB_SCOPE_VAP = next(
    (d for d in STORAGE_DOCS if d["kind"] == "ValidatingAdmissionPolicy"),
    None,
)
PRUNE_JOB_SCOPE_BINDING = next(
    (d for d in STORAGE_DOCS if d["kind"] == "ValidatingAdmissionPolicyBinding"),
    None,
)
ARCHIVAL_LABELS = {
    "app.kubernetes.io/managed-by": "openstudio-operator",
    "app.kubernetes.io/component": "archival",
}
# Issue #565 — the principal the prune-scope VAP constrains (and the
# actor whose carve-out disjunct leads the CEL expression).
PRUNE_SA_FULL = "system:serviceaccount:openstudio-server:openstudio-storage-pruner-sa"


def _strip_cel_whitespace(expr: str) -> str:
    """Normalise a CEL expression for substring tests."""
    return " ".join(expr.split())


def test_prune_job_scope_vap_exists_and_targets_batch_jobs():
    """Issue #294 acceptance: a ValidatingAdmissionPolicy lives next to the
    storage CronJob manifests and hooks CREATE/UPDATE/DELETE on `batch/jobs`.
    Without it, the prune SA's `batch/jobs create|delete` verbs accept any
    Job name and any label set — the regression #294 closes."""
    assert PRUNE_JOB_SCOPE_VAP is not None, (
        "no ValidatingAdmissionPolicy in deploy/storage-cronjob.yaml — "
        "the prune SA's batch/jobs verbs are unscoped (issue #294)"
    )
    assert PRUNE_JOB_SCOPE_VAP["apiVersion"] == "admissionregistration.k8s.io/v1"
    spec = PRUNE_JOB_SCOPE_VAP["spec"]
    res = spec["matchConstraints"]["resourceRules"][0]
    assert res["apiGroups"] == ["batch"]
    assert res["apiVersions"] == ["v1"]
    assert res["resources"] == ["jobs"]
    # GET is intentionally NOT in the operation list — read access is not
    # an exfiltration path and constraining it would block the prune pod's
    # own watch-by-deterministic-name lookup. The CREATE/UPDATE/DELETE
    # triple is the exact set that lets the prune pod do its job AND
    # that an attacker would need to spawn arbitrary Jobs.
    assert sorted(res["operations"]) == ["CREATE", "DELETE", "UPDATE"]


def test_prune_job_scope_vap_scopes_to_openstudio_server_namespace():
    """The policy must NOT apply cluster-wide. The `namespaceSelector`
    inside `matchConstraints` is the canonical gate, and it must
    match only the `openstudio-server` namespace via the standard
    `kubernetes.io/metadata.name` label (the same pattern the
    metrics-ingress NetworkPolicy uses at network-policy.yaml:264)."""
    match = PRUNE_JOB_SCOPE_VAP["spec"]["matchConstraints"]
    selector = match["namespaceSelector"]
    assert selector == {
        "matchLabels": {"kubernetes.io/metadata.name": "openstudio-server"}
    }, (
        "policy namespaceSelector must restrict to openstudio-server only; "
        f"got {selector!r}; a cluster-wide match would reject Jobs in "
        "every namespace (issue #294)"
    )


def test_prune_job_scope_vap_validations_require_archival_labels():
    """The CEL validation must require both archival labels. The test
    pins the exact label keys and values the policy enforces — a
    regression that drops one label, relaxes the value, or types the
    wrong key must fail loudly. Both the CREATE-side (`object`) and
    DELETE-side (`oldObject`) clauses must reference the labels so
    the policy actually fires on its own operation."""
    validations = PRUNE_JOB_SCOPE_VAP["spec"]["validations"]
    assert validations, "policy must declare at least one validation"
    # Concatenate and normalise the validation expressions — K8s
    # evaluates each entry independently, so the test asserts the
    # aggregate expression references the right labels.
    full_expr = _strip_cel_whitespace(
        " ".join(v["expression"] for v in validations)
    )
    # Both object-side and oldObject-side clauses must appear — the
    # `has()` guards on either side are what makes the policy
    # operation-correct (CREATE has object, DELETE has oldObject).
    assert "has(object.metadata.labels)" in full_expr, (
        f"validation expression missing object-side has() guard: {full_expr!r}"
    )
    assert "has(oldObject.metadata.labels)" in full_expr, (
        f"validation expression missing oldObject-side has() guard: {full_expr!r}"
    )
    for key, value in ARCHIVAL_LABELS.items():
        assert key in full_expr, (
            f"validation expression missing label key {key!r}: {full_expr!r}"
        )
        assert f'"{value}"' in full_expr, (
            f"validation expression missing expected value {value!r} for "
            f"key {key!r}: {full_expr!r}"
        )


def test_prune_job_scope_vap_failure_policy_is_fail():
    """Any CEL evaluation error must reject the request — the same
    default-deny stance the network policies use. `Ignore` would let
    a malformed CEL expression silently let an attacker through."""
    assert PRUNE_JOB_SCOPE_VAP["spec"]["failurePolicy"] == "Fail", (
        "ValidatingAdmissionPolicy.failurePolicy must be 'Fail' so a "
        "CEL evaluation error blocks the request (issue #294); "
        "see network-policy.yaml for the same default-deny stance"
    )


def test_prune_job_scope_vap_binding_binds_to_policy():
    """A ValidatingAdmissionPolicy with no binding is dormant. The
    binding must name the policy above AND be schema-valid per the
    admissionregistration.k8s.io/v1 Binding shape (issue #565, verified
    against a live 1.31 API server with --dry-run=server):
    `validationActions` is REQUIRED — an omitted/empty list is a hard
    Invalid reject ("at least one validation action is required") that
    made the whole storage-cronjob.yaml apply fail after the
    Role/CronJob had already landed — and `matchResources` is the only
    scoping field: the schema has NO `selector`, so the pre-#565
    `selector: {}` was a strict-decoding unknown-field reject."""
    assert PRUNE_JOB_SCOPE_BINDING is not None, (
        "no ValidatingAdmissionPolicyBinding in deploy/storage-cronjob.yaml — "
        "the policy is dormant without a binding (issue #294)"
    )
    assert PRUNE_JOB_SCOPE_BINDING["apiVersion"] == "admissionregistration.k8s.io/v1"
    spec = PRUNE_JOB_SCOPE_BINDING["spec"]
    assert spec["policyName"] == (
        PRUNE_JOB_SCOPE_VAP["metadata"]["name"]
    )
    assert spec.get("validationActions") == ["Deny"], (
        "Binding.validationActions must be [\"Deny\"] — required by the "
        "v1 Binding schema (an omitted list is an API-server reject, "
        "issue #565) and the only action that actually blocks the "
        f"request; got {spec.get('validationActions')!r}"
    )
    assert "selector" not in spec, (
        "Binding spec has no `selector` field in "
        "admissionregistration.k8s.io/v1 — the API server rejects the "
        "unknown field wholesale; scope via matchResources instead "
        "(issue #565)"
    )
    ns_selector = spec.get("matchResources", {}).get("namespaceSelector")
    assert ns_selector and ns_selector.get("matchLabels", {}).get(
        "kubernetes.io/metadata.name"
    ) == _OPERATOR_NS, (
        "Binding must scope via matchResources.namespaceSelector to "
        f"{_OPERATOR_NS!r} (mirroring the policy's own gate); got "
        f"{ns_selector!r}"
    )


def test_prune_job_scope_vap_label_keys_match_archival_manifest():
    """Regression fence: the label KEY/VALUE pairs the VAP enforces
    must equal the labels `archival.py` actually emits at lines
    185-189. If they drift, the policy is sound but doesn't accept
    any real Job — the acceptance criterion is unmet. The values
    are referenced by their string identity in the policy, so a
    typo on either side causes simultaneous test failures here and
    a runtime reject by the policy."""
    from openstudio_operator.archival import build_archival_job
    from openstudio_operator.config import StoragePolicy

    job = build_archival_job(
        "64f0c8e2a1b3c4d5e6f7a8b9",
        StoragePolicy(
            archive_to_s3=True,
            backend="s3",
            bucket="os-archives",
            secret_ref="os-archive-creds",
        ),
        "openstudio-server",
    )
    job_labels = job["metadata"]["labels"]
    for key, value in ARCHIVAL_LABELS.items():
        assert job_labels[key] == value, (
            f"archival Job label {key}={job_labels.get(key)!r} drifts from "
            f"VAP-expected value {value!r}; VAP and archival manifest must "
            "agree on the exact key/value pair (issue #294)"
        )

    # Same check on the policy CEL expression
    full_expr = _strip_cel_whitespace(
        " ".join(v["expression"] for v in PRUNE_JOB_SCOPE_VAP["spec"]["validations"])
    )
    for key, value in ARCHIVAL_LABELS.items():
        assert key in full_expr
        assert f'"{value}"' in full_expr


# ---- Issue #398: the prune-scope VAP must also pin the Job NAME ----
#
# The #294 policy constrained the prune SA's batch/jobs verbs by LABELS
# only — but labels are spoofable by the very SA the policy constrains:
# a compromised prune pod can stamp
# app.kubernetes.io/managed-by=openstudio-operator +
# app.kubernetes.io/component=archival on ANY Job it spawns, and
# `metadata.name` was not part of the CEL rule, so a Job named
# `kube-system-cleanup` with the right labels passed admission. The
# deterministic names `archival.py::archival_job_name` generates
# (`oscm-archive-<sanitized-id>-<sha256-8>`) give the natural second
# factor: `metadata.name.startsWith("oscm-archive-")` ANDed inside BOTH
# the object clause (CREATE/UPDATE) and the oldObject clause (DELETE).
#
# The pinned dependency set has no CEL engine, so the "fails admission /
# passes admission" acceptance criterion is expressed with the minimal
# interpreter below. It covers exactly the constructs this repo's
# ValidatingAdmissionPolicies use — has(), dotted field paths, string map
# indexing, integer literals + list indexing, == / !=, && / || with CEL's
# error-absorption truth tables, !, string.startsWith()/matches(), the
# zero-arg .size() call, and the .all(var, predicate) macro — and
# evaluates the MANIFEST'S OWN expression text, not a re-implementation
# of its semantics (the encode-the-bug-as-a-feature trap #315 audits
# for).

_OSCM_ARCHIVE_NAME_PREFIX = "oscm-archive-"


class _CelError(Exception):
    """A CEL evaluation error, mirroring CEL's error values: `&&`/`||`
    absorb them per their truth tables, and any error escaping a
    validation expression rejects the request under `failurePolicy:
    Fail` — the default-deny stance test_prune_job_scope_vap_
    failure_policy_is_fail pins."""


_CEL_TOKEN = re.compile(
    r"\s*(?:(?P<op>\|\||&&|==|!=|!|[()\[\],.])"
    r"|(?P<str>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')"
    r"|(?P<num>\d+)"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_]*))"
)


def _cel_tokenize(text):
    """Split a CEL expression into (kind, value) tokens; raises
    _CelError on any construct the interpreter does not recognize."""
    tokens = []
    pos = 0
    while pos < len(text):
        match = _CEL_TOKEN.match(text, pos)
        if match is None:
            if text[pos:].strip():
                raise _CelError(f"unparsable CEL near {text[pos:pos + 20]!r}")
            break
        pos = match.end()
        if match.group("op"):
            tokens.append(("op", match.group("op")))
        elif match.group("str") is not None:
            raw = match.group("str")[1:-1]
            tokens.append(("str", re.sub(r"\\(.)", r"\1", raw)))
        elif match.group("num") is not None:
            tokens.append(("num", int(match.group("num"))))
        else:
            tokens.append(("ident", match.group("ident")))
    return tokens


class _CelParser:
    """Recursive-descent parser for the CEL subset this repo's
    ValidatingAdmissionPolicies use. Precedence: `||` < `&&` < `!` <
    comparison < postfix (field select, map index, method call)."""

    def __init__(self, tokens):
        self._tokens = tokens
        self._i = 0

    def parse(self):
        node = self._parse_or()
        if self._i != len(self._tokens):
            raise _CelError(f"trailing tokens: {self._tokens[self._i:]!r}")
        return node

    def _peek(self):
        return self._tokens[self._i] if self._i < len(self._tokens) else (None, None)

    def _take(self, kind=None, value=None):
        token = self._peek()
        if kind is not None and (
            token[0] != kind or (value is not None and token[1] != value)
        ):
            raise _CelError(f"expected {kind} {value!r}, got {token!r}")
        self._i += 1
        return token

    def _parse_or(self):
        node = self._parse_and()
        while self._peek() == ("op", "||"):
            self._take()
            node = ("or", node, self._parse_and())
        return node

    def _parse_and(self):
        node = self._parse_unary()
        while self._peek() == ("op", "&&"):
            self._take()
            node = ("and", node, self._parse_unary())
        return node

    def _parse_unary(self):
        if self._peek() == ("op", "!"):
            self._take()
            return ("not", self._parse_unary())
        return self._parse_comparison()

    def _parse_comparison(self):
        left = self._parse_postfix()
        if self._peek() in (("op", "=="), ("op", "!=")):
            operator = self._take()[1]
            right = self._parse_postfix()
            return ("eq" if operator == "==" else "ne", left, right)
        return left

    def _parse_postfix(self):
        if self._peek() == ("ident", "has") and self._tokens[self._i + 1] == ("op", "("):
            self._take()
            self._take("op", "(")
            path = self._parse_postfix()
            self._take("op", ")")
            return ("has", path)
        node = self._parse_atom()
        while True:
            if self._peek() == ("op", "."):
                self._take()
                name = self._take("ident")[1]
                if self._peek() == ("op", "("):
                    self._take()
                    args = []
                    if self._peek() != ("op", ")"):
                        args.append(self._parse_or())
                        while self._peek() == ("op", ","):
                            self._take()
                            args.append(self._parse_or())
                    self._take("op", ")")
                    node = ("call", node, name, args)
                else:
                    node = ("select", node, name)
            elif self._peek() == ("op", "["):
                self._take()
                key = self._parse_or()
                self._take("op", "]")
                node = ("index", node, key)
            else:
                return node

    def _parse_atom(self):
        kind, value = self._peek()
        if (kind, value) == ("op", "("):
            self._take()
            node = self._parse_or()
            self._take("op", ")")
            return node
        if kind == "str":
            self._take()
            return ("lit", value)
        if kind == "num":
            self._take()
            return ("lit", value)
        if kind == "ident":
            self._take()
            if value == "true":
                return ("lit", True)
            if value == "false":
                return ("lit", False)
            return ("var", value)
        raise _CelError(f"unexpected token {value!r}")


def _cel_field(receiver, key):
    """One map/object lookup step; missing keys error exactly like CEL's
    field selection on a map, which is what makes the policy's `has()`
    guards load-bearing."""
    if not isinstance(receiver, dict):
        raise _CelError(f"cannot select {key!r} from {type(receiver).__name__}")
    if key not in receiver:
        raise _CelError(f"no such key {key!r}")
    value = receiver[key]
    if value is None:
        raise _CelError(f"key {key!r} is null")
    return value


def _cel_eval(node, ctx):
    """Evaluate a parsed CEL node against {root-name: value} context.
    Missing/null roots error (CEL exposes `object`/`oldObject` as null
    for DELETE/CREATE respectively), which the `&&`/`||` truth tables
    absorb the same way the API server does."""
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "var":
        if node[1] not in ctx:
            raise _CelError(f"unknown root {node[1]!r}")
        value = ctx[node[1]]
        if value is None:
            raise _CelError(f"{node[1]!r} is null for this operation")
        return value
    if kind == "select":
        return _cel_field(_cel_eval(node[1], ctx), node[2])
    if kind == "index":
        receiver = _cel_eval(node[1], ctx)
        key = _cel_eval(node[2], ctx)
        if isinstance(receiver, list) and isinstance(key, int) and not isinstance(key, bool):
            if not 0 <= key < len(receiver):
                raise _CelError("list index out of range")
            return receiver[key]
        return _cel_field(receiver, key)
    if kind == "has":
        target = node[1]
        if target[0] in ("select", "index"):
            receiver = _cel_eval(target[1], ctx)
            key = target[2] if target[0] == "select" else _cel_eval(target[2], ctx)
            if not isinstance(receiver, dict):
                raise _CelError("has() receiver is not a map/object")
            return key in receiver
        _cel_eval(target, ctx)
        return True
    if kind == "call":
        # The all(var, predicate) macro binds the iteration variable in
        # a child context (CEL macros are not plain function calls).
        # Error semantics mirror CEL's: any False predicate short-
        # circuits to False; an error that never meets a False
        # propagates (denies under failurePolicy: Fail).
        if node[2] == "all" and len(node[3]) == 2 and node[3][0][0] == "var":
            receiver = _cel_eval(node[1], ctx)
            if not isinstance(receiver, list):
                raise _CelError("all() requires a list receiver")
            var, predicate = node[3][0][1], node[3][1]
            saw_error = False
            for element in receiver:
                try:
                    value = _cel_eval(predicate, {**ctx, var: element})
                except _CelError:
                    saw_error = True
                    continue
                if value is False:
                    return False
                if not isinstance(value, bool):
                    saw_error = True
            if saw_error:
                raise _CelError("error inside all() predicate")
            return True
        receiver = _cel_eval(node[1], ctx)
        args = [_cel_eval(arg, ctx) for arg in node[3]]
        if node[2] == "size" and not args:
            if isinstance(receiver, (list, str, dict)):
                return len(receiver)
            raise _CelError("size() requires a list/string/map receiver")
        if node[2] == "startsWith" and len(args) == 1:
            if not isinstance(receiver, str) or not isinstance(args[0], str):
                raise _CelError("startsWith requires string receiver and argument")
            return receiver.startswith(args[0])
        if node[2] == "matches" and len(args) == 1:
            if not isinstance(receiver, str) or not isinstance(args[0], str):
                raise _CelError("matches requires string receiver and argument")
            # CEL's matches() is a FULL match (RE2); the anchored #240
            # pattern carries its own ^...$ anyway.
            return re.fullmatch(args[0], receiver) is not None
        raise _CelError(f"unsupported method {node[2]!r}")
    if kind in ("eq", "ne"):
        equal = _cel_eval(node[1], ctx) == _cel_eval(node[2], ctx)
        return equal if kind == "eq" else not equal
    if kind in ("and", "or"):
        operands = []
        for child in node[1:]:
            try:
                value = _cel_eval(child, ctx)
            except _CelError:
                operands.append(None)  # sentinel: evaluation error
            else:
                operands.append(value if isinstance(value, bool) else None)
        if kind == "and":
            if any(op is False for op in operands):
                return False
            if any(op is None for op in operands):
                raise _CelError("error in && operand")
            return True
        if any(op is True for op in operands):
            return True
        if any(op is None for op in operands):
            raise _CelError("error in || operand")
        return False
    if kind == "not":
        value = _cel_eval(node[1], ctx)
        if not isinstance(value, bool):
            raise _CelError("! applied to non-bool")
        return not value
    raise _CelError(f"unknown node kind {kind!r}")


def _cel_allows(expression, *, obj, old, username=PRUNE_SA_FULL, operation="CREATE"):
    """Admission decision for one validation expression: True when the
    expression evaluates truthy (request allowed), False when it
    evaluates falsy OR errors — a CEL error denies under
    `failurePolicy: Fail`, so both paths are rejections. The context
    carries `request.userInfo.username` (default: the prune SA — the
    principal the policy constrains) and `request.operation`
    (default: CREATE) so the #565 carve-out disjunct and the #641
    operation guard evaluate the way the API server would."""
    ctx = {
        "object": obj,
        "oldObject": old,
        "request": {"userInfo": {"username": username}, "operation": operation},
    }
    tree = _CelParser(_cel_tokenize(expression)).parse()
    try:
        return _cel_eval(tree, ctx) is True
    except _CelError:
        return False


def test_prune_job_scope_vap_validations_require_oscm_archive_name_prefix():
    """Issue #398 acceptance (structural fence, same convention as the
    label tests above): the CEL must conjoin a
    `metadata.name.startsWith("oscm-archive-")` clause INSIDE both the
    object clause (CREATE/UPDATE) and the oldObject clause (DELETE) —
    not bolted on as a third disjunct, which a spoofed name could
    satisfy independently of the labels.

    Issue #565 adds a LEADING carve-out disjunct (non-prune actors are
    unrestricted — see the carve-out test below), so the top-level
    shape is now `userInfo != '<prune SA>' || object-clause ||
    oldObject-clause`: exactly two `||`, three clauses. Since #641 the
    policy carries a SECOND validation (the pod-spec fence), so this
    test selects the name/labels validation by content — the join-all
    approach would count the fence's own disjuncts."""
    name_exprs = [
        v["expression"]
        for v in PRUNE_JOB_SCOPE_VAP["spec"]["validations"]
        if "metadata.name.startsWith" in v["expression"]
    ]
    assert len(name_exprs) == 1, (
        "expected exactly one name/labels validation expression; the "
        "#641 spec fence must live in its own validations[] entry"
    )
    full_expr = _strip_cel_whitespace(name_exprs[0])
    assert full_expr.count("||") == 2, (
        "expected the #565 userInfo carve-out disjunct plus exactly "
        "one object-side/oldObject-side disjunction pair; got: "
        f"{full_expr!r}"
    )
    carve_out, object_clause, old_object_clause = full_expr.split("||")
    assert "request.userInfo.username" in carve_out and PRUNE_SA_FULL in carve_out, (
        f"leading clause must be the #565 userInfo carve-out naming "
        f"the prune SA ({PRUNE_SA_FULL!r}); got: {carve_out!r}"
    )
    for side, clause in (("object", object_clause), ("oldObject", old_object_clause)):
        assert f"{side}.metadata.name.startsWith(\"{_OSCM_ARCHIVE_NAME_PREFIX}\")" in clause, (
            f"{side}-side clause missing the startsWith name check "
            f"(issue #398): {clause!r}"
        )
        # The name clause must sit inside the same conjunction as the
        # label checks: each clause is has() && label && label && name.
        assert clause.count("&&") >= 3, (
            f"{side}-side clause must AND the name check with both label "
            f"checks (issue #398); got: {clause!r}"
        )
        assert "app.kubernetes.io/managed-by" in clause
        assert "app.kubernetes.io/component" in clause


def test_prune_job_scope_vap_rejects_spoofed_job_name_with_archival_labels():
    """Issue #398 acceptance: a Job named `kube-system-cleanup` carrying
    the CORRECT archival labels must FAIL admission on both the CREATE
    and DELETE paths. Before #398 the labels were the only gate and
    `metadata.name` was unchecked, so a compromised prune SA could
    label any Job archival and pass. The manifest's own CEL text is
    evaluated against the simulated request with the minimal
    interpreter above — an expression that is false OR errors denies
    under `failurePolicy: Fail`."""
    spoofed = {
        "metadata": {
            "name": "kube-system-cleanup",
            "labels": dict(ARCHIVAL_LABELS),
        }
    }
    expression = PRUNE_JOB_SCOPE_VAP["spec"]["validations"][0]["expression"]
    assert _cel_allows(expression, obj=spoofed, old=None) is False, (
        "CREATE of a spoof-named Job carrying the archival labels must "
        "be rejected at admission time (issue #398)"
    )
    assert _cel_allows(expression, obj=None, old=spoofed) is False, (
        "DELETE of a spoof-named Job carrying the archival labels must "
        "be rejected at admission time (issue #398)"
    )


def test_prune_job_scope_vap_accepts_real_archival_job_name():
    """Issue #398 acceptance + drift fence: an archival Job whose name
    AND labels come from the real generators (archival.py::
    build_archival_job / archival_job_name) must PASS admission on both
    the CREATE and DELETE paths. If archival.py ever changes its name
    prefix or label set without the VAP following, the retention
    pipeline stalls at its own spawn — this fails loudly, the same
    pairing discipline as
    test_prune_job_scope_vap_label_keys_match_archival_manifest."""
    from openstudio_operator.archival import archival_job_name, build_archival_job
    from openstudio_operator.config import StoragePolicy

    job = build_archival_job(
        "64f0c8e2a1b3c4d5e6f7a8b9",
        StoragePolicy(
            archive_to_s3=True,
            backend="s3",
            bucket="os-archives",
            secret_ref="os-archive-creds",
        ),
        "openstudio-server",
    )
    name = job["metadata"]["name"]
    assert name == archival_job_name("64f0c8e2a1b3c4d5e6f7a8b9")
    assert name.startswith(_OSCM_ARCHIVE_NAME_PREFIX), (
        f"archival_job_name drifted from the {_OSCM_ARCHIVE_NAME_PREFIX!r} "
        f"prefix the VAP enforces (issue #398): {name!r}"
    )
    expression = PRUNE_JOB_SCOPE_VAP["spec"]["validations"][0]["expression"]
    assert _cel_allows(expression, obj=job, old=None) is True, (
        "CREATE of a real archival Job must pass admission — the CEL "
        "name/label requirements must match what archival.py emits "
        "(issues #294, #398)"
    )
    assert _cel_allows(expression, obj=None, old=job) is True, (
        "DELETE of a real archival Job must pass admission (failed-Job "
        "cleanup path; issues #294, #398)"
    )


def test_prune_job_scope_vap_userinfo_carveout_leaves_non_prune_actors_unrestricted():
    """Issue #565 acceptance: the pre-fix CEL constrained EVERY
    principal's Job create/update/delete in the namespace — a human
    running `kubectl create job` for a legitimate non-archival Job
    would have been rejected, contradicting AGENTS.md's "humans via
    kubectl are left unrestricted" contract. The leading
    `request.userInfo.username != '<prune SA>' ||` short-circuit
    (mirroring the pod-delete VAP's operator-SA carve-out) means only
    the prune SA is constrained. The manifest's own CEL text is
    evaluated with the minimal interpreter: a non-prune actor must be
    ALLOWED on a non-archival Job (both CREATE and DELETE paths), and
    the prune SA must stay DENIED on the same Job."""
    expression = PRUNE_JOB_SCOPE_VAP["spec"]["validations"][0]["expression"]
    non_archival = {
        "metadata": {
            "name": "kube-system-cleanup",
            "labels": {"app.kubernetes.io/component": "debug"},
        }
    }
    human = "kubernetes-admin"
    assert _cel_allows(expression, obj=non_archival, old=None, username=human) is True, (
        "a non-prune actor creating a non-archival Job must NOT be "
        "constrained by the policy (issue #565 carve-out)"
    )
    assert _cel_allows(expression, obj=None, old=non_archival, username=human) is True, (
        "a non-prune actor deleting a non-archival Job must NOT be "
        "constrained by the policy (issue #565 carve-out)"
    )
    assert _cel_allows(expression, obj=non_archival, old=None) is False, (
        "the prune SA creating the same non-archival Job must still be "
        "rejected — the carve-out must not weaken the #294/#398 fence "
        "(default username is the prune SA)"
    )
    assert _cel_allows(expression, obj=None, old=non_archival) is False, (
        "the prune SA deleting the same non-archival Job must still be "
        "rejected (default username is the prune SA)"
    )


# ---- Issue #641: fence archival-Job image / command / envFrom ----
#
# The #294/#398 factors (name prefix + labels) are both spoofable by
# the very SA the policy constrains — a compromised prune pod names a
# Job `oscm-archive-evil`, stamps the two labels, and the Job's
# containers were completely unconstrained: the kubelet resolves
# `envFrom` against ANY Secret in `openstudio-server` with no RBAC
# check against the pod's SA, so the exact #294 exfiltration path
# (ship `openstudio-redis` / the Mongo credential Secret / TLS bundles
# out as env vars over the archival egress allow) stayed open. The
# fix adds a SECOND validations[] entry to openstudio-prune-job-scope:
# every container image == the RCLONE_IMAGE digest, every envFrom
# secretRef matches the #240 CRD pattern, command pinned to the
# generator's `/bin/sh -c` envelope (archival.py passes its script via
# `command` and never sets `args` — full equality is impossible
# because the script is a pure function of the CR spec), and
# initContainers/ephemeralContainers (never emitted by the generator)
# rejected outright. The tests below evaluate the manifest's OWN CEL
# text with the interpreter above — legit manifest must PASS, each
# tamper must FAIL — and cross-fence the pinned image literal against
# archival.RCLONE_IMAGE (the #573 pairing pattern).

_OS_ARCHIVE_SECRET_PATTERN = "^os-archive-[a-z0-9-]+$"


def _spec_fence_expression():
    """The #641 pod-spec fence validation (selected by content so the
    validations[] entry order is not load-bearing)."""
    exprs = [
        v["expression"]
        for v in PRUNE_JOB_SCOPE_VAP["spec"]["validations"]
        if "envFrom" in v["expression"]
    ]
    assert len(exprs) == 1, (
        "expected exactly one #641 spec-fence validation expression "
        "referencing envFrom; got "
        f"{[e[:60] for e in exprs]!r}"
    )
    return exprs[0]


def _legit_archival_job():
    """A real archival Job manifest from the actual generator — the
    only shape the fence must admit. Includes a datapoint tree so the
    multi-source script variant is exercised too."""
    from openstudio_operator.archival import build_archival_job
    from openstudio_operator.config import StoragePolicy

    return build_archival_job(
        "64f0c8e2a1b3c4d5e6f7a8b9",
        StoragePolicy(
            archive_to_s3=True,
            backend="s3",
            bucket="os-archives",
            secret_ref="os-archive-creds",
        ),
        "openstudio-server",
        datapoint_ids=("64f0c8e2a1b3c4d5e6f7a8c0",),
    )


def test_prune_job_scope_vap_spec_fence_pins_rclone_image_and_secret_pattern_literals():
    """Issue #641 cross-fence (the #573 name-pair pairing pattern): the
    image literal pinned inside the CEL must EQUAL archival.RCLONE_IMAGE
    — single-sourced per #417 — and the envFrom pattern must mirror the
    #240 CRD `spec.storagePolicy.secretRef` pattern verbatim. If
    archival.py's digest ever rotates without the CEL following (or
    vice versa), this fails loudly instead of every legit archival Job
    stalling at its own admission."""
    from openstudio_operator.archival import RCLONE_IMAGE

    expr = _strip_cel_whitespace(_spec_fence_expression())
    assert f"'{RCLONE_IMAGE}'" in expr, (
        f"the #641 spec fence must pin the archival.RCLONE_IMAGE digest "
        f"({RCLONE_IMAGE!r}) as a CEL string literal; refresh both "
        "together (docker buildx imagetools inspect rclone/rclone:<tag>)"
    )
    assert f"'{_OS_ARCHIVE_SECRET_PATTERN}'" in expr, (
        f"the #641 spec fence must mirror the #240 CRD secretRef pattern "
        f"{_OS_ARCHIVE_SECRET_PATTERN!r} in its envFrom clause"
    )
    # The fence must not be part of the #294/#398 name/labels entry —
    # K8s evaluates each validations[] entry independently, and mixing
    # them would let a spoofed name satisfy the spec factors too.
    assert "metadata.name.startsWith" not in expr


def test_prune_job_scope_vap_spec_fence_accepts_real_archival_job():
    """Issue #641 acceptance: the manifest produced by the REAL
    generator (archival.py::build_archival_job) must PASS the spec
    fence on CREATE and UPDATE — otherwise the fence is a fence that
    admits nothing and the retention pipeline stalls at its own spawn.
    DELETE carries no new object (the `request.operation` guard
    exempts it, mirroring the first validation's oldObject shape) and
    must also pass."""
    expression = _spec_fence_expression()
    job = _legit_archival_job()
    assert _cel_allows(expression, obj=job, old=None) is True, (
        "CREATE of a real archival Job must pass the #641 spec fence — "
        "the pinned image/command/envFrom factors must match exactly "
        "what archival.py emits"
    )
    assert _cel_allows(expression, obj=job, old=job, operation="UPDATE") is True, (
        "UPDATE of a real archival Job must pass the #641 spec fence"
    )
    assert (
        _cel_allows(expression, obj=None, old=job, operation="DELETE") is True
    ), (
        "DELETE of a real archival Job must pass the #641 spec fence "
        "(no new object; the operation guard exempts DELETE)"
    )


def test_prune_job_scope_vap_spec_fence_rejects_foreign_image():
    """Issue #641 factor (a): every container image must equal the
    pinned RCLONE_IMAGE digest. A wrong tag/digest on the sole
    container, a digest-stripped `rclone/rclone:1.75.0` (tag-mutation
    supply-chain window, #124), and a foreign sidecar container are
    each admission rejects — image identity is not spoofable by
    relabelling."""
    expression = _spec_fence_expression()
    wrong_tag = copy.deepcopy(_legit_archival_job())
    wrong_tag["spec"]["template"]["spec"]["containers"][0]["image"] = "alpine:latest"
    assert _cel_allows(expression, obj=wrong_tag, old=None) is False
    no_digest = copy.deepcopy(_legit_archival_job())
    no_digest["spec"]["template"]["spec"]["containers"][0]["image"] = (
        "rclone/rclone:1.75.0"
    )
    assert _cel_allows(expression, obj=no_digest, old=None) is False, (
        "the fence must require the @sha256 digest form (#124), not "
        "just the tag"
    )
    sidecar = copy.deepcopy(_legit_archival_job())
    sidecar["spec"]["template"]["spec"]["containers"].append(
        {"name": "exfil", "image": "curlimages/curl:latest", "command": ["/bin/sh", "-c", "set -eu\ncurl evil"]}
    )
    assert _cel_allows(expression, obj=sidecar, old=None) is False, (
        "the fence must check EVERY container, not just containers[0]"
    )


def test_prune_job_scope_vap_spec_fence_rejects_foreign_envfrom_secret():
    """Issue #641 factor (b) — the minimum bar per the issue: mounting a
    namespace Secret the archival flow never uses (`openstudio-redis`,
    the KEDA password — the exact #294 exfiltration path), a
    correctly-prefixed-but-foreign Secret appended as a second
    envFrom entry, and a configMapRef-only envFrom entry are each
    admission rejects. Exfiltration via envFrom fails closed."""
    expression = _spec_fence_expression()
    redis = copy.deepcopy(_legit_archival_job())
    redis["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"secretRef": {"name": "openstudio-redis"}}
    ]
    assert _cel_allows(expression, obj=redis, old=None) is False, (
        "envFrom secretRef openstudio-redis (the KEDA password Secret) "
        "must be rejected — this is the #294 exfiltration path the "
        "VAP exists to close"
    )
    sneaky = copy.deepcopy(_legit_archival_job())
    sneaky["spec"]["template"]["spec"]["containers"][0]["envFrom"].append(
        {"secretRef": {"name": "openstudio-mongo"}}
    )
    assert _cel_allows(expression, obj=sneaky, old=None) is False, (
        "a foreign secretRef APPENDED after the legit entry must still "
        "be rejected — the fence requires EVERY envFrom entry to match "
        "^os-archive-[a-z0-9-]+$"
    )
    cm_only = copy.deepcopy(_legit_archival_job())
    cm_only["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"configMapRef": {"name": "os-archive-config"}}
    ]
    assert _cel_allows(expression, obj=cm_only, old=None) is False, (
        "a configMapRef-only envFrom entry must be rejected — the "
        "fence requires has(e.secretRef)"
    )


def test_prune_job_scope_vap_spec_fence_rejects_command_override():
    """Issue #641 factor (c): `command` is pinned to the generator's
    exact envelope — a foreign argv (`curl`), a `/bin/sh -c` whose
    script is NOT the generator's `set -eu` prologue, a missing
    command, and an `args` override on an otherwise-legit container
    are each admission rejects."""
    expression = _spec_fence_expression()
    foreign_argv = copy.deepcopy(_legit_archival_job())
    foreign_argv["spec"]["template"]["spec"]["containers"][0]["command"] = [
        "curl",
        "https://evil.example",
    ]
    assert _cel_allows(expression, obj=foreign_argv, old=None) is False
    wrong_script = copy.deepcopy(_legit_archival_job())
    wrong_script["spec"]["template"]["spec"]["containers"][0]["command"] = [
        "/bin/sh",
        "-c",
        "cat /proc/self/environ > /mnt/x",
    ]
    assert _cel_allows(expression, obj=wrong_script, old=None) is False, (
        "a /bin/sh -c envelope whose script is not the generator's "
        "set -eu prologue must be rejected"
    )
    no_command = copy.deepcopy(_legit_archival_job())
    del no_command["spec"]["template"]["spec"]["containers"][0]["command"]
    assert _cel_allows(expression, obj=no_command, old=None) is False
    args_override = copy.deepcopy(_legit_archival_job())
    args_override["spec"]["template"]["spec"]["containers"][0]["args"] = ["--evil"]
    assert _cel_allows(expression, obj=args_override, old=None) is False, (
        "args overrides must be rejected — the legit manifest passes "
        "everything via command and never sets args"
    )


def test_prune_job_scope_vap_spec_fence_rejects_init_and_ephemeral_containers():
    """Issue #641 completeness: the generator never emits
    initContainers or ephemeralContainers, so ANY occurrence — even one
    carrying a pinned image — is an admission reject. This closes the
    sideload-another-image gap: a containers-only fence would let an
    unpinned initContainer/ephemeralContainer slip past the image
    check."""
    expression = _spec_fence_expression()
    init = copy.deepcopy(_legit_archival_job())
    init["spec"]["template"]["spec"]["initContainers"] = [
        {"name": "sidecar", "image": "busybox:latest"}
    ]
    assert _cel_allows(expression, obj=init, old=None) is False
    ephemeral = copy.deepcopy(_legit_archival_job())
    ephemeral["spec"]["template"]["spec"]["ephemeralContainers"] = [
        {"name": "debug", "image": "busybox:latest"}
    ]
    assert _cel_allows(expression, obj=ephemeral, old=None) is False


def test_prune_job_scope_vap_spec_fence_userinfo_carveout_leaves_non_prune_actors_unrestricted():
    """Issue #641 keeps the #565 contract: only the prune SA is
    constrained. A non-prune actor creating the SAME tampered Job
    (foreign image + foreign envFrom) must NOT be constrained by the
    spec fence, while the prune SA is denied on it."""
    expression = _spec_fence_expression()
    tampered = copy.deepcopy(_legit_archival_job())
    tampered["spec"]["template"]["spec"]["containers"][0]["image"] = "alpine:latest"
    tampered["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"secretRef": {"name": "openstudio-redis"}}
    ]
    human = "kubernetes-admin"
    assert _cel_allows(expression, obj=tampered, old=None, username=human) is True, (
        "a non-prune actor must not be constrained by the #641 fence "
        "(issue #565 carve-out contract)"
    )
    assert _cel_allows(expression, obj=tampered, old=None) is False, (
        "the prune SA creating the tampered Job must be rejected by "
        "the #641 fence (default username is the prune SA)"
    )


# ---- Issue #388: PSS `restricted` enforced at the namespace level ----
#
# The operator + prune CronJob + archival Job all declare PSS `restricted`
# at the pod level (deploy/operator-deployment.yaml:47-52,
# deploy/storage-cronjob.yaml:112-117, archival.py:225-232). Until #388
# the namespace itself carried no PSS labels, so the per-pod
# `securityContext` was defense-in-depth, not enforced: a future
# workload added to `openstudio-server` with `runAsUser: 0` and
# `privileged: true` would have passed kubelet admission. The fix adds
# the three canonical PSS labels:
#
#   * pod-security.kubernetes.io/enforce=restricted — reject at admission.
#   * pod-security.kubernetes.io/enforce-version=latest — pin to the PSS
#     profile bundled with the running cluster (the documented way to
#     stay current with new restricted-profile restrictions).
#   * pod-security.kubernetes.io/audit=restricted — record non-compliant
#     pods in the audit log without rejecting them, the "warn first"
#     staging shape.
#
# The structural test below pins all three labels and their exact values
# so a regression (typo, value drop, label removal) fails loudly at CI
# rather than silently letting a permissive namespace through. Paired
# with the helm-chart pod-level + container-level hardening covered by
# the existing `_iter_workload_containers` and `_iter_deploy_workload_pod_specs`
# walkers (which #388 widened to include scripts/manifests/*.yaml), the
# combined posture is "enforced at the namespace AND defense-in-depth on
# every pod".
NAMESPACE_MANIFEST = (
    Path(__file__).resolve().parents[1] / "scripts" / "manifests" / "00-namespace.yaml"
)
NAMESPACE_DOC = next(
    iter(yaml.safe_load_all(NAMESPACE_MANIFEST.read_text()))
)
EXPECTED_PSS_LABELS = {
    "pod-security.kubernetes.io/enforce": "restricted",
    "pod-security.kubernetes.io/enforce-version": "latest",
    "pod-security.kubernetes.io/audit": "restricted",
}


def test_namespace_has_pss_enforce_audit_labels():
    """Issue #388 acceptance #1: the `openstudio-server` namespace
    manifest carries the three Pod Security Standards labels
    (`enforce`, `enforce-version`, `audit`). A regression that drops
    any of the three turns the per-pod `securityContext` from
    defense-in-depth into the only line of defense — silent if a
    future workload forgets the pod-level block entirely."""
    assert NAMESPACE_MANIFEST.exists(), (
        f"{NAMESPACE_MANIFEST} is missing — the openstudio-server "
        "namespace manifest is required for the PSS label gate (issue #388)"
    )
    assert NAMESPACE_DOC.get("kind") == "Namespace", (
        f"scripts/manifests/00-namespace.yaml must declare a Namespace, "
        f"got kind={NAMESPACE_DOC.get('kind')!r}"
    )
    assert NAMESPACE_DOC["metadata"]["name"] == "openstudio-server", (
        f"namespace manifest must name `openstudio-server`, got "
        f"{NAMESPACE_DOC['metadata']['name']!r}"
    )
    labels = NAMESPACE_DOC["metadata"].get("labels") or {}
    missing = {
        key: {"expected": value, "got": labels.get(key)}
        for key, value in EXPECTED_PSS_LABELS.items()
        if labels.get(key) != value
    }
    assert not missing, (
        "openstudio-server namespace is missing the PSS `restricted` "
        f"labels (issue #388): {missing}. Without these labels the "
        "per-pod securityContext is defense-in-depth, not enforced — a "
        "future workload with runAsUser: 0 / privileged: true would pass "
        "kubelet admission. The labels must be: "
        f"{EXPECTED_PSS_LABELS!r}."
    )


# ---- Issue #498: PSS labels on the PRODUCTION deploy/ path ----------
#
# #388 labeled the kind recipe's namespace (scripts/manifests/
# 00-namespace.yaml) but deploy/ — the production story alongside the
# helm chart — shipped no Namespace labels or labeling step, so a
# helm-created `openstudio-server` namespace ran with whatever PSS
# default the cluster had (usually none). #498 ships
# deploy/namespace-labels.yaml: a Namespace carrying ONLY metadata so
# `kubectl apply` MERGES the PSS labels onto the existing helm-owned
# namespace instead of replacing it (three-way apply semantics leave
# labels absent from this manifest untouched). The label set is the
# #388 kind shape plus `warn` — enforce, enforce-version, audit, warn.
DEPLOY_NAMESPACE_MANIFEST = DEPLOY / "namespace-labels.yaml"
DEPLOY_NAMESPACE_DOC = next(
    iter(yaml.safe_load_all(DEPLOY_NAMESPACE_MANIFEST.read_text()))
)
EXPECTED_DEPLOY_PSS_LABELS = {
    "pod-security.kubernetes.io/enforce": "restricted",
    "pod-security.kubernetes.io/enforce-version": "latest",
    "pod-security.kubernetes.io/audit": "restricted",
    "pod-security.kubernetes.io/warn": "restricted",
}


def test_deploy_namespace_labels_manifest_exists_and_parses():
    """Issue #498 acceptance: deploy/namespace-labels.yaml exists, parses
    as YAML, and declares exactly one Namespace targeting
    `openstudio-server` — the fixed identifier every other deploy/
    manifest assumes (AGENTS.md §Fixed identifiers). A doc of any other
    kind (or name) would silently apply nothing to the production
    namespace."""
    assert DEPLOY_NAMESPACE_MANIFEST.exists(), (
        f"{DEPLOY_NAMESPACE_MANIFEST} is missing — the production deploy/ "
        "path ships no PSS labels for the openstudio-server namespace, so "
        "the per-pod securityContext stays defense-in-depth instead of "
        "enforced (issue #498)"
    )
    docs = [d for d in yaml.safe_load_all(DEPLOY_NAMESPACE_MANIFEST.read_text()) if d]
    assert len(docs) == 1, (
        f"namespace-labels.yaml must declare exactly one doc, got {len(docs)}"
    )
    assert DEPLOY_NAMESPACE_DOC.get("kind") == "Namespace", (
        f"deploy/namespace-labels.yaml must declare a Namespace, got "
        f"kind={DEPLOY_NAMESPACE_DOC.get('kind')!r}"
    )
    assert DEPLOY_NAMESPACE_DOC["metadata"]["name"] == "openstudio-server", (
        f"namespace-labels.yaml must name `openstudio-server`, got "
        f"{DEPLOY_NAMESPACE_DOC['metadata']['name']!r}"
    )


def test_deploy_namespace_labels_carry_pss_restricted_set():
    """Issue #498 acceptance: the manifest carries the full PSS label set
    — enforce=restricted + enforce-version=latest (the acceptance
    criterion's minimum), plus audit=restricted and warn=restricted (the
    #388 kind shape widened with the before-the-fact kubectl signal). A
    regression that drops or retypes any label turns admission
    enforcement back into optional defense-in-depth."""
    labels = DEPLOY_NAMESPACE_DOC["metadata"].get("labels") or {}
    missing = {
        key: {"expected": value, "got": labels.get(key)}
        for key, value in EXPECTED_DEPLOY_PSS_LABELS.items()
        if labels.get(key) != value
    }
    assert not missing, (
        "deploy/namespace-labels.yaml is missing PSS `restricted` labels "
        f"(issue #498): {missing}. The production namespace runs with no "
        "admission enforcement without them. The labels must be: "
        f"{EXPECTED_DEPLOY_PSS_LABELS!r}."
    )


def test_deploy_namespace_labels_manifest_is_merge_safe():
    """Issue #498 shape fence: the manifest declares ONLY
    `apiVersion`/`kind`/`metadata` (name + labels) — no `spec`, no
    `finalizers`, no annotations. That is what makes `kubectl apply` a
    label merge on a helm-owned namespace rather than a replacement: a
    future edit that grows a `spec:` block (or moves labels into
    annotations) would change apply semantics on an object the helm
    chart believes it owns."""
    assert set(DEPLOY_NAMESPACE_DOC) <= {"apiVersion", "kind", "metadata"}, (
        f"namespace-labels.yaml must declare only apiVersion/kind/metadata "
        f"to stay merge-safe on the helm-created namespace (issue #498); "
        f"got top-level keys {sorted(DEPLOY_NAMESPACE_DOC)}"
    )
    assert set(DEPLOY_NAMESPACE_DOC["metadata"]) <= {"name", "labels"}, (
        f"namespace-labels.yaml metadata must carry only name + labels "
        f"(issue #498); got {sorted(DEPLOY_NAMESPACE_DOC['metadata'])}"
    )


def test_deploy_namespace_labels_manifest_documents_downgrade_note():
    """Issue #498 acceptance criterion, prose half: the manifest must
    document (a) the apply-merge semantics that make it safe on a
    helm-owned namespace and (b) the downgrade note — chart pods must
    satisfy `restricted` or the labels must be deliberately downgraded
    per-environment. A future rewrite that strips the header comments
    silently loses the operator guidance the acceptance criterion
    demands; this fence trips on exactly that."""
    text = DEPLOY_NAMESPACE_MANIFEST.read_text()
    lowered = text.lower()
    assert "merge" in lowered, (
        "namespace-labels.yaml must document the kubectl apply merge "
        "semantics that make a metadata-only Namespace manifest safe on "
        "the helm-created namespace (issue #498)"
    )
    assert "downgrad" in lowered, (
        "namespace-labels.yaml must carry the downgrade note: chart pods "
        "must satisfy `restricted` or the labels must be deliberately "
        "downgraded per-environment (issue #498 acceptance criterion)"
    )
    assert "#498" in text and "#388" in text, (
        "namespace-labels.yaml header must cite both #498 (this "
        "manifest) and #388 (the kind-recipe label set it mirrors)"
    )


HELM_CHART_POD_BASELINE_FILES = (
    "scripts/manifests/04-web.yaml",
    "scripts/manifests/05-web-background.yaml",
    "scripts/manifests/06-worker.yaml",
)
HELM_CHART_POD_NAMES = {
    "scripts/manifests/04-web.yaml": "web",
    "scripts/manifests/05-web-background.yaml": "web-background",
    "scripts/manifests/06-worker.yaml": "worker",
}


def _helm_chart_pod_baseline_problems(rel_path):
    """Return a list of human-readable problems with the helm-chart
    pod manifest at `rel_path`; empty list means the manifest satisfies
    the PSS `restricted` baseline. Shared between
    ``test_helm_chart_pods_have_pod_level_securitycontext`` (pod-level)
    and ``test_helm_chart_pods_have_container_securitycontext_baseline``
    (container-level) so the two regression fences read the same way."""
    manifest_path = Path(__file__).resolve().parents[1] / rel_path
    docs = list(yaml.safe_load_all(manifest_path.read_text()))
    deployment = next(
        (d for d in docs if d and d.get("kind") == "Deployment"), None
    )
    if deployment is None:
        return [f"{rel_path}: no Deployment doc found"]
    pod_spec = deployment["spec"]["template"]["spec"]
    problems = []
    # Pod-level: defense-in-depth (mirrors operator + prune CronJob).
    pod_sc = pod_spec.get("securityContext") or {}
    if not pod_sc:
        problems.append("pod-level securityContext missing")
    else:
        if pod_sc.get("runAsNonRoot") is not True:
            problems.append(
                f"pod-level runAsNonRoot={pod_sc.get('runAsNonRoot')!r} "
                "(must be True)"
            )
        if not isinstance(pod_sc.get("runAsUser"), int) or pod_sc.get("runAsUser") == 0:
            problems.append(
                f"pod-level runAsUser={pod_sc.get('runAsUser')!r} "
                "(must be non-zero int)"
            )
        if (pod_sc.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
            problems.append(
                f"pod-level seccompProfile.type="
                f"{(pod_sc.get('seccompProfile') or {}).get('type')!r} "
                "(must be 'RuntimeDefault')"
            )
    # Container-level: six-field baseline (mirrors #115 / #162).
    containers = pod_spec.get("containers") or []
    if not containers:
        problems.append("no containers in pod spec")
    for c in containers:
        c_name = c.get("name", "<unnamed>")
        c_sc = c.get("securityContext") or {}
        if not c_sc:
            problems.append(f"container {c_name!r}: securityContext missing")
            continue
        if c_sc.get("allowPrivilegeEscalation") is not False:
            problems.append(
                f"container {c_name!r}: allowPrivilegeEscalation="
                f"{c_sc.get('allowPrivilegeEscalation')!r} (must be False)"
            )
        if c_sc.get("readOnlyRootFilesystem") is not True:
            problems.append(
                f"container {c_name!r}: readOnlyRootFilesystem="
                f"{c_sc.get('readOnlyRootFilesystem')!r} (must be True)"
            )
        if (c_sc.get("capabilities") or {}).get("drop") != ["ALL"]:
            problems.append(
                f"container {c_name!r}: capabilities.drop="
                f"{(c_sc.get('capabilities') or {}).get('drop')!r} "
                "(must be ['ALL'])"
            )
        if c_sc.get("runAsNonRoot") is not True:
            problems.append(
                f"container {c_name!r}: runAsNonRoot="
                f"{c_sc.get('runAsNonRoot')!r} (must be True)"
            )
        if not isinstance(c_sc.get("runAsUser"), int) or c_sc.get("runAsUser") == 0:
            problems.append(
                f"container {c_name!r}: runAsUser={c_sc.get('runAsUser')!r} "
                "(must be non-zero int)"
            )
        if (c_sc.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
            problems.append(
                f"container {c_name!r}: seccompProfile.type="
                f"{(c_sc.get('seccompProfile') or {}).get('type')!r} "
                "(must be 'RuntimeDefault')"
            )
    return problems


def test_helm_chart_pods_have_pod_level_securitycontext():
    """Issue #388 acceptance #2 (pod-level half): the three Rails pods
    in the kind-validation helm-chart overlay (web, web-background,
    worker) carry a non-empty pod-level `securityContext` with
    runAsNonRoot + non-zero runAsUser + seccompProfile
    RuntimeDefault — the same shape as deploy/operator-deployment.yaml
    :47-52 and deploy/storage-cronjob.yaml:112-117. Mirrors
    ``test_all_workloads_have_pod_level_securitycontext`` but pinned to
    the three issue #388 manifests so a regression that drops the pod-
    level block on any of them fails with a targeted message naming
    the exact file. (Stateful helm-chart stand-ins — mongo, redis, NFS
    — are out of scope per the issue's scope guard; #394 owns them.)"""
    offenders = {}
    for rel_path in HELM_CHART_POD_BASELINE_FILES:
        problems = _helm_chart_pod_baseline_problems(rel_path)
        # Filter to pod-level problems only — the container-level check
        # has its own dedicated test below.
        pod_only = [
            p for p in problems
            if p.startswith("pod-level")
            or p.endswith("securityContext missing")
            and "container" not in p
        ]
        if pod_only:
            offenders[rel_path] = pod_only
    assert not offenders, (
        "helm-chart pods are missing the #161/#388 pod-level "
        "securityContext baseline (issue #388): "
        f"{offenders}. The namespace label set in #388 turns this from "
        "defense-in-depth to enforced — every workload must satisfy the "
        "baseline or admission will reject it."
    )


def test_helm_chart_pods_have_container_securitycontext_baseline():
    """Issue #388 acceptance #2 (container-level half): the three
    Rails pods in the kind-validation helm-chart overlay carry the same
    six-field container-level baseline the operator + prune CronJob
    already enforce (allowPrivilegeEscalation=false,
    readOnlyRootFilesystem=true, capabilities.drop=[ALL],
    runAsNonRoot=true, runAsUser=<non-zero>, seccompProfile.type=
    RuntimeDefault). Targeted per-file regression fence: a regression
    that drops one field on one pod fails loudly with the file +
    offending field name."""
    offenders = {}
    for rel_path in HELM_CHART_POD_BASELINE_FILES:
        problems = _helm_chart_pod_baseline_problems(rel_path)
        container_only = [
            p for p in problems
            if p.startswith("container ")
            or p == "no containers in pod spec"
            or p.endswith("securityContext missing")
        ]
        if container_only:
            offenders[rel_path] = container_only
    assert not offenders, (
        "helm-chart pods are missing the #115/#162/#388 container-level "
        "hardening baseline (issue #388): "
        f"{offenders}. The same six fields the operator + prune CronJob "
        "enforce must apply here so the PSS `restricted` namespace label "
        "added by #388 does not reject these pods at admission."
    )


# ---- Issue #414: PriorityClass for operator + prune CronJob -----------
#
# The operator is single-replica + Recreate (no leader election, no
# second replica), so a node-pressure eviction mid-tick halts the ONLY
# poller — potentially with the StatusStore read-modify-write
# half-applied and a `web_background_restart_pending` flag stuck in
# `.status` (D04). The prune CronJob has the same exposure mid-
# retention-tick. Pre-fix, neither deploy/operator-deployment.yaml nor
# deploy/storage-cronjob.yaml declared `priorityClassName`, and no
# PriorityClass shipped in deploy/ at all — eviction ordering was
# whatever the cluster default happened to be (kops/eks/gke diverge).
#
# The fix ships ONE cluster-scoped PriorityClass
# (`openstudio-operator-critical`, value 1000000000 — above user
# workloads, below the system-reserved 2x10^9 band — with
# `preemptionPolicy: Never` so the operator never displaces other
# pods, and `globalDefault: false` so nothing inherits it implicitly)
# referenced by BOTH pod templates. The issue text mused about "a
# lower value for the prune CronJob" but its own scope guard is
# authoritative: "Do NOT add multiple PriorityClasses — one is enough"
# and the acceptance criterion pins BOTH manifests to
# `priorityClassName: openstudio-operator-critical`. The evict-first
# contrast is preserved the other way: the archival Jobs generated by
# archival.py set NO priorityClassName, so rclone pods stay at the
# cluster default and remain evictable before the operator surface.
PRIORITY_CLASS_PATH = DEPLOY / "priority-class.yaml"
PRIORITY_CLASS_DOCS = [
    d for d in yaml.safe_load_all(PRIORITY_CLASS_PATH.read_text()) if d
]
PRIORITY_CLASS = next(
    (d for d in PRIORITY_CLASS_DOCS if d.get("kind") == "PriorityClass"),
    None,
)
_PRIORITY_CLASS_NAME = "openstudio-operator-critical"


def test_priority_class_manifest_ships_single_critical_class():
    """Issue #414 acceptance: deploy/priority-class.yaml ships exactly
    ONE PriorityClass with the protective shape — value 1000000000
    (above every user workload, below the system-reserved 2x10^9 band
    so kube-system always wins), `preemptionPolicy: Never` (the class
    orders evictions; it must NOT let the operator preempt other pods
    on a full cluster), and `globalDefault: false` (pods that do not
    opt in are untouched). The single-doc assertion enforces the scope
    guard "Do NOT add multiple PriorityClasses" literally — a second
    class in this file fails CI. The description must cite #414 so an
    operator hitting the class in `kubectl get priorityclass` can find
    the rationale."""
    assert PRIORITY_CLASS is not None, (
        "deploy/priority-class.yaml is missing a PriorityClass — the "
        "operator + prune pods have no node-pressure eviction "
        "protection and inherit an unspecified cluster default "
        "(issue #414)"
    )
    assert len(PRIORITY_CLASS_DOCS) == 1, (
        "issue #414 scope guard: exactly ONE PriorityClass — a class "
        "shared by the operator Deployment and the prune CronJob. "
        f"Got {len(PRIORITY_CLASS_DOCS)} docs: "
        f"{[d.get('kind') for d in PRIORITY_CLASS_DOCS]!r}"
    )
    assert PRIORITY_CLASS["apiVersion"] == "scheduling.k8s.io/v1"
    assert PRIORITY_CLASS["metadata"]["name"] == _PRIORITY_CLASS_NAME
    assert PRIORITY_CLASS["value"] == 1000000000, (
        "PriorityClass value must be 1000000000 — high enough to be "
        "last-evicted among user workloads, low enough to stay below "
        f"the system-reserved band; got {PRIORITY_CLASS['value']!r}"
    )
    assert PRIORITY_CLASS["globalDefault"] is False, (
        "globalDefault must be false — a global default would silently "
        "reorder eviction for every pod in the cluster (issue #414)"
    )
    assert PRIORITY_CLASS["preemptionPolicy"] == "Never", (
        "preemptionPolicy must be Never — this class protects eviction "
        "ORDER only; preempting other pods to schedule the operator is "
        "out of scope (issue #414)"
    )
    assert "#414" in PRIORITY_CLASS.get("description", ""), (
        "PriorityClass description must cite issue #414 so the "
        "rationale is discoverable from kubectl get priorityclass"
    )


def test_operator_deployment_sets_openstudio_operator_critical_priority():
    """Issue #414 acceptance: the operator Deployment pod template
    references the `openstudio-operator-critical` PriorityClass.
    Single-replica + Recreate means an eviction mid-tick halts the
    only poller with the StatusStore RMW potentially half-applied —
    the priorityClassName is what makes this pod the LAST evicted
    under node pressure. A regression that drops the field (or
    retargets it at a different class) fails here."""
    pod_spec = OPERATOR_DEPLOYMENT["spec"]["template"]["spec"]
    assert pod_spec.get("priorityClassName") == _PRIORITY_CLASS_NAME, (
        "operator Deployment pod template must set priorityClassName: "
        f"{_PRIORITY_CLASS_NAME!r} (deploy/priority-class.yaml, issue "
        f"#414); got {pod_spec.get('priorityClassName')!r}"
    )


def test_storage_cronjob_sets_openstudio_operator_critical_priority():
    """Issue #414 acceptance: the prune CronJob pod template references
    the SAME `openstudio-operator-critical` PriorityClass. The issue
    text mused about a separate lower value for the prune CronJob, but
    its scope guard is explicit ("Do NOT add multiple PriorityClasses
    — one is enough") and the acceptance criterion pins both manifests
    to this exact class name. The evict-first contrast lives on the
    archival-Job side instead (next test)."""
    cron_pod_spec = CRONJOB["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert cron_pod_spec.get("priorityClassName") == _PRIORITY_CLASS_NAME, (
        "prune CronJob pod template must set priorityClassName: "
        f"{_PRIORITY_CLASS_NAME!r} — the SAME class as the operator "
        "Deployment; one PriorityClass covers both surfaces per the "
        "issue #414 scope guard; got "
        f"{cron_pod_spec.get('priorityClassName')!r}"
    )


def test_archival_jobs_do_not_inherit_priority_class():
    """Issue #414 complement: the archival Jobs generated by
    archival.py set NO priorityClassName. This is the evict-first half
    of the fix — the rclone archival pods stay at the cluster default
    priority so under node pressure they are evicted BEFORE the
    operator + prune surface. If archival.py ever grows a
    priorityClassName pointing at the critical class, the "archival
    Jobs still evict first" property silently disappears and this
    fence fails."""
    from openstudio_operator.archival import build_archival_job
    from openstudio_operator.config import StoragePolicy

    job = build_archival_job(
        "64f0c8e2a1b3c4d5e6f7a8b9",
        StoragePolicy(
            archive_to_s3=True,
            backend="s3",
            bucket="os-archives",
            secret_ref="os-archive-creds",
        ),
        "openstudio-server",
    )
    archival_pod_spec = job.get("spec", {}).get("template", {}).get("spec", {})
    assert "priorityClassName" not in archival_pod_spec, (
        "archival Jobs must NOT set priorityClassName — they stay at "
        "the cluster default so rclone archival pods remain evictable "
        "before the operator + prune surface under node pressure "
        f"(issue #414); got {archival_pod_spec.get('priorityClassName')!r}"
    )


# ---- Issue #415: automountServiceAccountToken must stay unset on the
# operator Deployment + prune CronJob -----------------------------------
#
# The ephemeral archival Job generated by archival.py:226 sets
# `automountServiceAccountToken: false` (#241) — rclone never calls the
# kube-apiserver, so the kubelet's default SA-token mount is pure attack
# surface there, and tests/test_archival.py::
# test_archival_job_disables_service_account_token_mount pins that
# inverse. The operator Deployment and the prune CronJob are the OPPOSITE
# case: both pods genuinely talk to the kube-apiserver (the operator via
# the single public kubeconfig loader at _k8s.load_operator_kube_config
# — #305 — and the prune entrypoint via the same loader for StatusStore
# RMW + batch Job create/delete). Neither manifest sets the field, so
# the kubelet default (`true`) is what runs — correct, but invisible to
# a manifest reader. If someone copies archival.py's hardening to either
# manifest in error (a future PR adding `automountServiceAccountToken:
# false`), the operator silently loses kube-apiserver access on first
# boot. These tests are the regression fence: the field must be absent
# OR explicitly true — NOT false.


def test_operator_deployment_does_not_disable_sa_token_mount():
    """Issue #415 regression fence: the operator Deployment pod template
    must NOT set `automountServiceAccountToken: false`. The operator pod
    genuinely needs its SA token — every OSCM timer handler reaches the
    kube-apiserver through the single public kubeconfig loader
    (_k8s.load_operator_kubeconfig, #305), and the token mount at
    /var/run/secrets/kubernetes.io/serviceaccount is the in-cluster
    credential path. Absence (the current shape, letting the kubelet
    default `true` apply) or an explicit `true` both pass; only `false`
    fails — the exact copy-the-archival-hardening mistake (#241's
    inverse) this fence exists to catch."""
    pod_spec = OPERATOR_DEPLOYMENT["spec"]["template"]["spec"]
    assert pod_spec.get("automountServiceAccountToken") is not False, (
        "operator Deployment pod template must NOT set "
        "automountServiceAccountToken: false — the operator needs its "
        "SA token to reach the kube-apiserver via the single public "
        "kubeconfig loader (#305). The automountServiceAccountToken: "
        "false hardening belongs ONLY on the ephemeral archival Job, "
        "whose rclone container never calls the API server (#241); "
        "copying it here silently breaks the operator on first boot "
        "(issue #415)."
    )


def test_storage_cronjob_does_not_disable_sa_token_mount():
    """Issue #415 regression fence: the prune CronJob pod template must
    NOT set `automountServiceAccountToken: false`. The prune entrypoint
    needs its SA token every tick — StatusStore read-modify-write on the
    OSCM CR status subresource plus batch Job create/delete all
    authenticate through the in-cluster credential path (same single
    public kubeconfig loader as the operator, #305). Absence (the
    current shape) or an explicit `true` both pass; only `false` fails.
    The archival Jobs the prune pod SPAWNS still carry
    `automountServiceAccountToken: false` (archival.py:226, #241) — the
    asymmetry is intentional and pinned on the archival side by
    tests/test_archival.py::test_archival_job_disables_service_account_
    token_mount."""
    cron_pod_spec = CRONJOB["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert cron_pod_spec.get("automountServiceAccountToken") is not False, (
        "prune CronJob pod template must NOT set "
        "automountServiceAccountToken: false — the prune SA needs its "
        "token for StatusStore RMW + batch Job create/delete every tick "
        "(#305). The automountServiceAccountToken: false hardening "
        "belongs ONLY on the ephemeral archival Job, whose rclone "
        "container never calls the API server (#241); copying it here "
        "silently breaks the retention pipeline (issue #415)."
    )


# ---- Issue #400: ResourceQuota + LimitRange bound the namespace's
# resource blast radius ----------------------------------------------
#
# Pre-fix `openstudio-server` had no ResourceQuota and no LimitRange:
# the verb surface was gated (RBAC, admission policies, network
# policies) but the RESOURCE surface was not — a misbehaving web pod
# or an attacker-controlled archival Job could claim arbitrary
# CPU/memory, evicting the operator pod on a memory-constrained node.
# The fix ships deploy/resource-quota.yaml with a ResourceQuota
# (aggregate totals) + a LimitRange (per-container defaults so pods
# that omit resources remain admissible under a quota that tracks
# limits). `kubectl apply --dry-run=server` is not available in CI
# (no cluster), so the "lint" below is a static YAML-structure walk
# in the same style as the rest of this file.
RESOURCE_QUOTA_PATH = DEPLOY / "resource-quota.yaml"
RESOURCE_QUOTA_DOCS = [
    d for d in yaml.safe_load_all(RESOURCE_QUOTA_PATH.read_text()) if d
]
RESOURCE_QUOTA = next(
    (d for d in RESOURCE_QUOTA_DOCS if d.get("kind") == "ResourceQuota"),
    None,
)
LIMIT_RANGE = next(
    (d for d in RESOURCE_QUOTA_DOCS if d.get("kind") == "LimitRange"),
    None,
)

# The LimitRange per-container defaults (issue #400's "per-pod
# default" numbers). `type: Container` is the only LimitRange type
# that can express default/defaultRequest; the constants below are
# what the admission controller injects for containers that omit
# resources.
LR_DEFAULT_REQUEST_CPU_MILLIS = 500.0
LR_DEFAULT_REQUEST_MEM_BYTES = 512 * 1024**2
LR_DEFAULT_LIMIT_CPU_MILLIS = 2000.0
LR_DEFAULT_LIMIT_MEM_BYTES = 4 * 1024**3

# Documented steady-state pod count (issue #400): 5 chart pods
# (web, web-background, worker, db, queue — the AGENTS.md
# managed-objects inventory) + 1 operator pod + 1 prune CronJob pod.
STEADY_STATE_PODS = 7

_CPU_SUFFIX_TO_CORES = {"n": 1e-9, "u": 1e-6, "m": 1e-3}
_MEM_SUFFIX_TO_BYTES = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
    "K": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "E": 10**18,
}


def _cpu_millis(quantity):
    """Parse a k8s CPU quantity ("500m", "2", "100m") to millicores.

    Raises ValueError on an unsupported suffix so an exotic unit fails
    the scan loudly instead of silently comparing as zero."""
    q = str(quantity).strip()
    for suffix, cores in _CPU_SUFFIX_TO_CORES.items():
        if q.endswith(suffix):
            return float(q[: -len(suffix)]) * cores * 1000
    try:
        return float(q) * 1000  # bare cores ("2" == 2000m)
    except ValueError as exc:
        raise ValueError(f"unsupported CPU quantity {quantity!r}") from exc


def _mem_bytes(quantity):
    """Parse a k8s memory quantity ("512Mi", "8Gi", 1024) to bytes.

    Raises ValueError on an unsupported suffix — same loud-failure
    rationale as `_cpu_millis`."""
    q = str(quantity).strip()
    for suffix, mult in sorted(
        _MEM_SUFFIX_TO_BYTES.items(), key=lambda kv: -len(kv[0])
    ):
        if q.endswith(suffix):
            return int(float(q[: -len(suffix)]) * mult)
    if q.isdigit():
        return int(q)
    raise ValueError(f"unsupported memory quantity {quantity!r}")


def test_resource_quota_manifest_ships_quota_and_limitrange():
    """Issue #400 acceptance #1: deploy/resource-quota.yaml exists,
    parses, and contains EXACTLY a ResourceQuota + a LimitRange, both
    bound to `openstudio-server` (namespaced core/v1 objects; a
    cluster admin can apply the file alongside
    scripts/manifests/00-namespace.yaml with no extra RBAC). The
    ResourceQuota must track all four totals (requests.cpu/memory +
    limits.cpu/memory) — a quota that omits one dimension leaves that
    dimension unbounded, the exact gap #400 closes."""
    assert RESOURCE_QUOTA is not None, (
        "deploy/resource-quota.yaml is missing a ResourceQuota — the "
        "namespace's resource surface is unbounded (issue #400)"
    )
    assert LIMIT_RANGE is not None, (
        "deploy/resource-quota.yaml is missing a LimitRange — pods that "
        "omit resources get no defaults and a limits-tracking quota "
        "would reject the chart's pods at admission (issue #400)"
    )
    kinds = {d["kind"] for d in RESOURCE_QUOTA_DOCS}
    assert kinds == {"ResourceQuota", "LimitRange"}, (
        f"deploy/resource-quota.yaml must declare exactly a "
        f"ResourceQuota + LimitRange, got {kinds!r}"
    )
    for doc in (RESOURCE_QUOTA, LIMIT_RANGE):
        assert doc["apiVersion"] == "v1", doc
        assert doc["metadata"]["namespace"] == "openstudio-server", doc
    hard = RESOURCE_QUOTA["spec"]["hard"]
    assert set(hard) == {
        "requests.cpu",
        "requests.memory",
        "limits.cpu",
        "limits.memory",
    }, (
        f"ResourceQuota must track all four CPU/memory totals, got "
        f"{sorted(hard)!r}"
    )


def test_limit_range_defaults_match_issue_400_numbers():
    """Issue #400 acceptance: the LimitRange carries the documented
    per-container defaults — defaultRequest cpu 500m / memory 512Mi,
    default cpu 2 / memory 4Gi — under `type: Container` (the only
    LimitRange type that can express defaults; the issue's "per-pod
    default" is realized as the per-container default injected for
    every container that omits resources). These defaults are
    load-bearing: a ResourceQuota tracking limits REJECTS pods whose
    containers omit limits, and the chart's pods declare
    limits.memory but NOT limits.cpu, so the injected default is what
    keeps them admissible."""
    assert LIMIT_RANGE is not None
    limits = LIMIT_RANGE["spec"]["limits"]
    container_limits = [l for l in limits if l.get("type") == "Container"]
    assert len(container_limits) == 1, (
        f"LimitRange must have exactly one type: Container entry, got "
        f"{limits!r}"
    )
    entry = container_limits[0]
    default_request = entry.get("defaultRequest") or {}
    default = entry.get("default") or {}
    assert _cpu_millis(default_request["cpu"]) == LR_DEFAULT_REQUEST_CPU_MILLIS
    assert _mem_bytes(default_request["memory"]) == LR_DEFAULT_REQUEST_MEM_BYTES
    assert _cpu_millis(default["cpu"]) == LR_DEFAULT_LIMIT_CPU_MILLIS
    assert _mem_bytes(default["memory"]) == LR_DEFAULT_LIMIT_MEM_BYTES


def test_resource_quota_totals_leave_room_for_steady_state():
    """Issue #400 acceptance #2: the quota totals leave room for the
    documented steady state — 5 chart pods (web, web-background,
    worker, db, queue) + 1 operator + 1 prune CronJob pod = 7 pods.
    The arithmetic this test fences (all values from the issue):

      * requests.cpu   7 x 500m  = 3.5   <= quota 4
      * requests.memory 7 x 512Mi = 3.5Gi <= quota 8Gi

    i.e. quota >= per-pod default request x documented pod count, so
    the namespace can always schedule the steady state even if every
    pod relies entirely on the LimitRange defaults. A quota shrink
    below that product strands the documented stack. The limits-side
    counterpart is fenced two ways: this test asserts the issue's
    >=2:1 limits:requests ratio (every reserved request unit keeps a
    matching burst unit of headroom above it), and the #580 companion
    below — ``test_resource_quota_covers_keda_burst_envelope`` —
    computes the real per-pod limits aggregate (LimitRange-injected
    defaults included) at KEDA maxReplicaCount and asserts the quota
    covers it. The companion is the stronger fence: the pre-#580
    8 CPU limits quota sat below even the steady-state limits
    aggregate (10.75 CPU)."""
    assert RESOURCE_QUOTA is not None
    hard = RESOURCE_QUOTA["spec"]["hard"]
    # Requests side: quota >= per-pod default x documented pod count.
    steady_cpu = STEADY_STATE_PODS * LR_DEFAULT_REQUEST_CPU_MILLIS
    steady_mem = STEADY_STATE_PODS * LR_DEFAULT_REQUEST_MEM_BYTES
    quota_req_cpu = _cpu_millis(hard["requests.cpu"])
    quota_req_mem = _mem_bytes(hard["requests.memory"])
    assert quota_req_cpu >= steady_cpu, (
        f"requests.cpu quota {_cpu_str(quota_req_cpu)} must leave room "
        f"for {STEADY_STATE_PODS} steady-state pods x 500m default = "
        f"{_cpu_str(steady_cpu)} (issue #400)"
    )
    assert quota_req_mem >= steady_mem, (
        f"requests.memory quota {_mem_str(quota_req_mem)} must leave "
        f"room for {STEADY_STATE_PODS} steady-state pods x 512Mi "
        f"default = {_mem_str(steady_mem)} (issue #400)"
    )
    # Limits side: the 2:1 limits:requests ratio (8 = 2x4, 16Gi = 2x8Gi).
    quota_lim_cpu = _cpu_millis(hard["limits.cpu"])
    quota_lim_mem = _mem_bytes(hard["limits.memory"])
    assert quota_lim_cpu >= 2 * quota_req_cpu, (
        f"limits.cpu quota {_cpu_str(quota_lim_cpu)} must be >= 2x the "
        f"requests.cpu quota {_cpu_str(quota_req_cpu)} — the issue's "
        "documented burst headroom ratio (issue #400)"
    )
    assert quota_lim_mem >= 2 * quota_req_mem, (
        f"limits.memory quota {_mem_str(quota_lim_mem)} must be >= 2x "
        f"the requests.memory quota {_mem_str(quota_req_mem)} — the "
        "issue's documented burst headroom ratio (issue #400)"
    )


def _cpu_str(millis):
    """Render millicores for assertion messages."""
    return f"{millis:.0f}m"


def _mem_str(num_bytes):
    """Render bytes as GiB (or MiB below 1 GiB) for assertion messages."""
    if num_bytes >= 1024**3:
        return f"{num_bytes / 1024**3:g}Gi"
    return f"{num_bytes / 1024**2:g}Mi"


# ---- Issue #580: quota covers the KEDA maxReplicaCount burst envelope --
#
# The steady-state fence above counts ONE worker replica; KEDA's
# ScaledObject allows maxReplicaCount worker pods. At burst the
# namespace holds 4 chart pods + maxReplicaCount workers + operator
# + prune pod, and EVERY container's requests AND limits count
# against the aggregate quota — including the LimitRange-INJECTED
# defaults for dimensions a container omits (the chart pods omit
# limits.cpu, so each injects the 2 CPU default limit, and the
# injected value counts exactly like a declared one). The envelope
# below is therefore computed from the manifests at test time —
# maxReplicaCount parsed from deploy/keda-scaledobject.yaml,
# per-pod requests/limits parsed from scripts/manifests/ + deploy/,
# no hardcoded 11-pod arithmetic — so drift in any direction (burst
# up, quota down, chart requests up, LimitRange defaults up) fails
# here. The single-replica, Recreate-strategy operator must never be
# quota-rejected while KEDA is at burst: unlike node pressure (#414),
# quota admission is NOT preempted by the PriorityClass.

SCRIPTS_MANIFESTS_DIR = (
    Path(__file__).resolve().parents[1] / "scripts" / "manifests"
)
SCALED_OBJECT_DOCS = list(
    yaml.safe_load_all((DEPLOY / "keda-scaledobject.yaml").read_text())
)
SCALED_OBJECT = next(
    (d for d in SCALED_OBJECT_DOCS if d.get("kind") == "ScaledObject"),
    None,
)


def _scaled_target_max_replicas():
    """(kind, name, maxReplicaCount) from the ScaledObject — the burst
    ceiling the quota must cover, plus the scaleTargetRef identity so
    the replica substitution below stays tied to the workload KEDA
    actually scales (issue #580)."""
    assert SCALED_OBJECT is not None, (
        "deploy/keda-scaledobject.yaml is missing a ScaledObject — the "
        "worker burst ceiling (maxReplicaCount) cannot be determined "
        "(issue #580)"
    )
    target = SCALED_OBJECT["spec"]["scaleTargetRef"]
    max_replicas = SCALED_OBJECT["spec"].get("maxReplicaCount")
    assert isinstance(max_replicas, int), (
        f"ScaledObject must declare an integer maxReplicaCount, got "
        f"{max_replicas!r} (issue #580)"
    )
    return target["kind"], target["name"], max_replicas


def _burst_workload_pod_specs():
    """Yield (source, kind, name, replicas, pod_spec) for every
    request-bearing workload the quota must cover at max burst: the
    chart overlay under scripts/manifests/ (db, queue, web,
    web-background, worker — the NFS manifest ships only PV/PVC, no
    pods) + the operator Deployment + the prune CronJob's job pod
    (intermittently present; counted as ONE pod, matching the issue's
    envelope arithmetic). The workload named by the ScaledObject
    scaleTargetRef gets maxReplicaCount replicas; everything else its
    declared replicas (default 1, matching the API server's default
    for an omitted replicas field)."""
    scaled_kind, scaled_name, max_replicas = _scaled_target_max_replicas()
    sources = [
        ("scripts/manifests/", sorted(SCRIPTS_MANIFESTS_DIR.glob("*.yaml"))),
        (
            "deploy/",
            [
                DEPLOY / "operator-deployment.yaml",
                DEPLOY / "storage-cronjob.yaml",
            ],
        ),
    ]
    for prefix, paths in sources:
        for path in paths:
            for doc in yaml.safe_load_all(path.read_text()):
                if not doc:
                    continue
                kind = doc.get("kind")
                name = doc.get("metadata", {}).get("name", "<unnamed>")
                if kind in {"Deployment", "StatefulSet", "DaemonSet"}:
                    replicas = doc.get("spec", {}).get("replicas", 1)
                    if kind == scaled_kind and name == scaled_name:
                        replicas = max_replicas
                    yield (
                        f"{prefix}{path.name}",
                        kind,
                        name,
                        replicas,
                        doc["spec"]["template"]["spec"],
                    )
                elif kind == "CronJob":
                    yield (
                        f"{prefix}{path.name}",
                        kind,
                        name,
                        1,
                        doc["spec"]["jobTemplate"]["spec"]["template"]["spec"],
                    )


def _burst_envelope():
    """The max-burst aggregate the namespace can hold, as a dict keyed
    by quota dimension (requests.cpu in millicores, requests.memory in
    bytes, limits.cpu, limits.memory), plus a per-workload breakdown
    for assertion messages. Per-container effective values = declared
    request/limit, else the LimitRange-injected default for that
    section (admission injects defaultRequest for omitted requests and
    default for omitted limits; both count against the aggregate
    quota — issue #580)."""
    container_entry = LIMIT_RANGE["spec"]["limits"][0]
    zero = {
        "requests.cpu": 0.0,
        "requests.memory": 0,
        "limits.cpu": 0.0,
        "limits.memory": 0,
    }
    totals = dict(zero)
    breakdown = []
    for source, kind, name, replicas, pod_spec in _burst_workload_pod_specs():
        per = dict(zero)
        for container in pod_spec.get("containers", []):
            resources = container.get("resources") or {}
            for section in ("requests", "limits"):
                declared = resources.get(section) or {}
                lr_key = "defaultRequest" if section == "requests" else "default"
                lr_defaults = container_entry.get(lr_key) or {}
                for dimension in ("cpu", "memory"):
                    value = declared.get(dimension)
                    if value is None:
                        value = lr_defaults[dimension]
                    key = f"{section}.{dimension}"
                    if dimension == "cpu":
                        per[key] += _cpu_millis(value)
                    else:
                        per[key] += _mem_bytes(value)
        for key in totals:
            totals[key] += replicas * per[key]
        breakdown.append((source, kind, name, replicas, per))
    return totals, breakdown


def test_resource_quota_covers_keda_burst_envelope():
    """Issue #580: the quota totals cover the namespace at KEDA MAX
    burst. The envelope is computed from the manifests (chart pods at
    their REAL declared requests — web's 2Gi memory request included —
    + maxReplicaCount workers + operator + prune pod, with the
    LimitRange-injected defaults counted for every dimension a
    container omits) and asserted <= the quota on all four
    dimensions. The pre-#580 requests quota (4/8Gi) already covered
    the real-request envelope; the pre-#580 LIMITS quota (8/16Gi) did
    NOT — the injected 2 CPU default limit per chart pod aggregates to
    18.75 CPU at maxReplicaCount=5 (and 10.75 CPU even at steady
    state), so a rescheduled chart pod or the single-replica Recreate
    operator could be quota-rejected until another pod terminated.
    Raising maxReplicaCount or the chart requests without raising the
    quota (or vice versa) fails here — the durable fence the issue's
    acceptance criterion asks for."""
    assert RESOURCE_QUOTA is not None
    hard = RESOURCE_QUOTA["spec"]["hard"]
    totals, breakdown = _burst_envelope()
    pretty = "; ".join(
        f"{kind}/{name} x{replicas} "
        f"req {_cpu_str(per['requests.cpu'])}/"
        f"{_mem_str(per['requests.memory'])} "
        f"lim {_cpu_str(per['limits.cpu'])}/"
        f"{_mem_str(per['limits.memory'])}"
        for _source, kind, name, replicas, per in breakdown
    )
    checks = [
        ("requests.cpu", _cpu_millis, _cpu_str),
        ("requests.memory", _mem_bytes, _mem_str),
        ("limits.cpu", _cpu_millis, _cpu_str),
        ("limits.memory", _mem_bytes, _mem_str),
    ]
    for key, parse, render in checks:
        quota = parse(hard[key])
        envelope = totals[key]
        assert quota >= envelope, (
            f"{key} quota {render(quota)} does not cover the KEDA "
            f"maxReplicaCount burst envelope {render(envelope)} "
            f"(issue #580). Envelope: {pretty}. Either raise the "
            f"quota in deploy/resource-quota.yaml to cover the burst "
            f"envelope or lower maxReplicaCount in deploy/"
            f"keda-scaledobject.yaml — the single-replica Recreate "
            f"operator must never be quota-rejected at burst."
        )


def test_no_deploy_container_exceeds_limit_range_defaults():
    """Issue #400 acceptance #3 (the static "lint"): every container in
    every deploy/ workload manifest must declare requests/limits at or
    below the LimitRange per-container defaults (requests <=
    500m/512Mi, limits <= 2/4Gi). Today both deploy/ workloads pass
    with room to spare — the operator container declares
    100m/128Mi requests + 500m/256Mi limits and the prune container
    50m/128Mi + 250m/256Mi (the operator's requests are pinned by the
    issue #400 scope guard and must NOT be changed). Any NEW deploy/
    manifest whose container specs exceed the defaults fails here, so
    the steady-state arithmetic in
    ``test_resource_quota_totals_leave_room_for_steady_state`` stays
    valid as deploy/ grows.

    Scope: deploy/ only (the operator-owned surface). The kind-chart
    overlay under scripts/manifests/ is deliberately NOT scanned — its
    pods legitimately declare requests above the defaultRequest (web
    requests 2Gi memory), which is fine because LimitRange defaults
    apply only to containers that OMIT resources, and the chart's
    upstream values are protected by the issue #400 scope guard.
    initContainers are out of scope: no deploy/ manifest declares any
    (the shared iterator walks `containers` only)."""
    offenders = []
    for path_name, kind, name, container in _iter_workload_containers():
        if path_name.startswith("scripts/"):
            continue  # chart overlay — out of scope (docstring above)
        resources = container.get("resources") or {}
        requests = resources.get("requests") or {}
        limits = resources.get("limits") or {}
        problems = []
        if "cpu" in requests and (
            _cpu_millis(requests["cpu"]) > LR_DEFAULT_REQUEST_CPU_MILLIS
        ):
            problems.append(
                f"requests.cpu={requests['cpu']!r} > defaultRequest "
                f"500m"
            )
        if "memory" in requests and (
            _mem_bytes(requests["memory"]) > LR_DEFAULT_REQUEST_MEM_BYTES
        ):
            problems.append(
                f"requests.memory={requests['memory']!r} > "
                f"defaultRequest 512Mi"
            )
        if "cpu" in limits and (
            _cpu_millis(limits["cpu"]) > LR_DEFAULT_LIMIT_CPU_MILLIS
        ):
            problems.append(f"limits.cpu={limits['cpu']!r} > default 2")
        if "memory" in limits and (
            _mem_bytes(limits["memory"]) > LR_DEFAULT_LIMIT_MEM_BYTES
        ):
            problems.append(
                f"limits.memory={limits['memory']!r} > default 4Gi"
            )
        if problems:
            offenders.append(
                (path_name, kind, name, container.get("name"), problems)
            )
    assert not offenders, (
        f"deploy/ containers exceed the LimitRange per-container "
        f"defaults (issue #400 — shrink the manifest or, if the "
        f"workload genuinely needs more, adjust the LimitRange "
        f"defaults and the steady-state test together): {offenders}"
    )


# ---------------------------------------------------------------------------
# Issue #462 — the deploy/ credential Secret manifests must ship ONLY the
# unusable sentinel placeholder. The pre-#462 committed value
# ``openstudio-rotated`` was a real, publicly-known credential: the
# documented apply order listed plain ``kubectl apply`` of the Secret
# manifests as a valid path, so fresh installs that skipped the rotation
# scripts got a guessable password guarding Redis (the Resque queue
# fabric) and Mongo — the exact leak class #150/#219 closed, renamed.
# The shell guards (scripts/check_redis_password_unique.sh,
# scripts/check_mongo_password_unique.sh) enforce the same rule in CI;
# this test mirrors it at pytest level.
# ---------------------------------------------------------------------------

_CREDENTIAL_SECRET_SENTINEL = "CHANGE_ME_RUN_ROTATE_SCRIPT"
_CREDENTIAL_SECRET_MANIFESTS = (
    "redis-credentials-secret.yaml",
    "mongo-credentials-secret.yaml",
)


def test_credential_secret_manifests_ship_only_sentinel_placeholder():
    """Issue #462 acceptance: both deploy/ credential Secrets carry exactly
    the unusable sentinel ``CHANGE_ME_RUN_ROTATE_SCRIPT`` as
    ``stringData.password`` — never a real (or real-looking) committed
    credential. Any other value is a CWE-798 hardcoded default because the
    manifests are public; per-cluster passwords are installed only via
    scripts/rotate_redis_password.sh / scripts/rotate_mongo_password.sh,
    which substitute at apply time so the working tree keeps the sentinel.
    Mirrors the sentinel rule the shell guards enforce, so the regression
    fails CI even when the shell gate is skipped locally."""
    offenders = []
    for basename in _CREDENTIAL_SECRET_MANIFESTS:
        docs = list(yaml.safe_load_all((DEPLOY / basename).read_text()))
        secrets = [d for d in docs if d and d.get("kind") == "Secret"]
        assert secrets, f"{basename}: no Secret doc found (manifest malformed?)"
        for secret in secrets:
            password = (secret.get("stringData") or {}).get("password")
            if password != _CREDENTIAL_SECRET_SENTINEL:
                offenders.append(
                    (basename, secret.get("metadata", {}).get("name"), password)
                )
    assert not offenders, (
        f"deploy/ credential Secrets must ship only the unusable sentinel "
        f"'{_CREDENTIAL_SECRET_SENTINEL}' as stringData.password (issue #462 "
        f"— committed real credentials are CWE-798 defaults; install "
        f"per-cluster passwords via the rotation scripts): {offenders}"
    )


# ---------------------------------------------------------------------------
# Issue #572 — the third ValidatingAdmissionPolicy: the operator SA's
# Secret surface narrowed to the `openstudio-redis*` naming convention.
#
# The #463 bounded exception grants `secrets: [get]` namespace-wide in
# deploy/rbac.yaml; only the operator's own code (one construction
# site, client_factory._resolve_redis_url) and the CRD pattern
# ``^openstudio-redis[a-z0-9-]*$`` on spec.redisCredentials.secretRef
# bound it — neither constrains a compromised operator pod holding the
# SA token. A PolicyRule has no name-PATTERN slot (resourceNames is an
# exact-name list, useless for a CR-configurable target), so the fence
# lives at the admission layer like the #293/#294 policies.
#
# Scope honesty (mirrors the manifest's own header): admission runs
# ONLY on the mutating path — operations accepts exactly
# CREATE/UPDATE/DELETE/CONNECT, a literal "GET" is an apply-time
# Invalid reject (the dead-manifest failure mode #565 closed), and RBAC
# authorization precedes admission anyway. The policy therefore fences
# the operator SA's MUTATING Secret surface to the naming convention
# (RBAC-drift insurance for any future widening of the secrets rule),
# while the read path stays bounded by RBAC get-only + the CRD
# pattern. The tests below pin that exact shape and evaluate the
# MANIFEST'S OWN CEL with the #398 interpreter.
# ---------------------------------------------------------------------------

_SECRET_READ_ADMISSION_DOCS = list(
    yaml.safe_load_all((DEPLOY / "secret-read-admission-policy.yaml").read_text())
)
_REDIS_SECRET_NAME_PREFIX = "openstudio-redis"


def _secret_read_admission_policy():
    """Return the ValidatingAdmissionPolicy doc for #572."""
    return next(
        (
            d
            for d in _SECRET_READ_ADMISSION_DOCS
            if d.get("kind") == "ValidatingAdmissionPolicy"
        ),
        None,
    )


def _secret_read_admission_binding():
    """Return the ValidatingAdmissionPolicyBinding doc for #572."""
    return next(
        (
            d
            for d in _SECRET_READ_ADMISSION_DOCS
            if d.get("kind") == "ValidatingAdmissionPolicyBinding"
        ),
        None,
    )


def _cel_secret_read_allows(expression, *, name, username=_OPERATOR_SA_FULL):
    """Admission decision for the #572 validation expression against a
    single-object Secret request identified by ``request.name`` and
    authenticated as ``username`` (default: the operator SA — the
    principal the policy constrains). Reuses the #398 minimal
    interpreter: True = allowed, False = rejected (falsy OR error — a
    CEL error denies under ``failurePolicy: Fail``)."""
    ctx = {"request": {"userInfo": {"username": username}, "name": name}}
    tree = _CelParser(_cel_tokenize(expression)).parse()
    try:
        return _cel_eval(tree, ctx) is True
    except _CelError:
        return False


def test_secret_read_admission_manifest_exists_and_parses():
    """Issue #572 acceptance #1: deploy/secret-read-admission-policy.yaml
    exists, parses, and contains exactly the two admissionregistration
    resources — a lone policy is dormant and a lone binding is
    unbound."""
    assert _SECRET_READ_ADMISSION_DOCS, (
        "deploy/secret-read-admission-policy.yaml is missing or empty — "
        "the operator SA's Secret surface is unconstrained at the "
        "admission layer (issue #572)"
    )
    kinds = sorted(d["kind"] for d in _SECRET_READ_ADMISSION_DOCS if d)
    assert kinds == [
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
    ], (
        "deploy/secret-read-admission-policy.yaml must declare exactly "
        f"a ValidatingAdmissionPolicy + Binding, got {kinds!r}"
    )


def test_secret_read_admission_policy_targets_secrets_in_openstudio_server():
    """Issue #572 acceptance #2: the policy matches core/v1 `secrets` in
    `openstudio-server` only. The operations list is the complete
    mutating set (CREATE/UPDATE/DELETE) — "GET" is not a valid
    admission operation (apply-time Invalid reject, the dead-manifest
    mode #565 closed), so a policy that tried to name it would be
    un-applyable; the read path is bounded by RBAC + the CRD pattern
    instead (see the manifest header's scope-honesty block)."""
    policy = _secret_read_admission_policy()
    assert policy is not None
    assert policy["apiVersion"] == "admissionregistration.k8s.io/v1"
    match = policy["spec"]["matchConstraints"]
    rule = match["resourceRules"][0]
    assert rule["apiGroups"] == [""]
    assert rule["apiVersions"] == ["v1"]
    assert rule["resources"] == ["secrets"]
    assert sorted(rule["operations"]) == ["CREATE", "DELETE", "UPDATE"]
    assert match["namespaceSelector"] == {
        "matchLabels": {"kubernetes.io/metadata.name": _OPERATOR_NS}
    }, (
        "policy namespaceSelector must restrict to "
        f"{_OPERATOR_NS!r} — a cluster-wide match would evaluate the "
        "rule in every namespace (issue #572)"
    )


def test_secret_read_admission_policy_cel_carveout_and_name_prefix():
    """Issue #572 acceptance #3: the CEL is the #293 carve-out shape —
    exactly one top-level `||`: the leading clause exempts every actor
    EXCEPT the operator SA (humans and other SAs are untouched), and
    the trailing clause allows only `openstudio-redis*` names. The
    prefix must match the CRD's `^openstudio-redis[a-z0-9-]*$` pattern
    on spec.redisCredentials.secretRef.name so the admission fence and
    the schema fence name the same convention."""
    policy = _secret_read_admission_policy()
    full_expr = _strip_cel_whitespace(
        " ".join(v["expression"] for v in policy["spec"]["validations"])
    )
    assert full_expr.count("||") == 1, (
        "expected exactly the userInfo carve-out disjunct plus the "
        f"name-prefix clause; got: {full_expr!r}"
    )
    carve_out, name_clause = full_expr.split("||")
    assert "request.userInfo.username" in carve_out and _OPERATOR_SA_FULL in carve_out, (
        f"leading clause must be the userInfo carve-out naming the "
        f"operator SA ({_OPERATOR_SA_FULL!r}); got: {carve_out!r}"
    )
    assert name_clause.strip() == (
        f"request.name.startsWith('{_REDIS_SECRET_NAME_PREFIX}')"
    ), (
        f"name clause must allow exactly the {_REDIS_SECRET_NAME_PREFIX!r} "
        f"prefix via request.name.startsWith; got: {name_clause!r}"
    )
    # Cross-fence: the CRD pattern naming the same convention must still
    # exist verbatim in deploy/crd.yaml (gated in depth by
    # tests/test_crd_schema.py).
    crd_text = (DEPLOY / "crd.yaml").read_text()
    assert f"^{_REDIS_SECRET_NAME_PREFIX}[a-z0-9-]*$" in crd_text, (
        "deploy/crd.yaml must keep the spec.redisCredentials.secretRef.name "
        f"pattern ^{_REDIS_SECRET_NAME_PREFIX}[a-z0-9-]*$ — the admission "
        "fence and the CRD fence must name the same convention (#572/#463)"
    )


def test_secret_read_admission_policy_failure_policy_is_fail_and_message_cites_issue():
    """Issue #572 acceptance #4: `failurePolicy: Fail` (a CEL runtime
    error or an empty request.name REJECTS — the safe direction), and
    the single-line message (the #565 line-break rule is enforced
    globally by the glob fence) cites the issue an operator hitting the
    deny needs."""
    policy = _secret_read_admission_policy()
    assert policy["spec"]["failurePolicy"] == "Fail", (
        "ValidatingAdmissionPolicy.failurePolicy must be 'Fail' so a CEL "
        "evaluation error blocks the request (issue #572); 'Ignore' would "
        "fail the #572 fence open"
    )
    message = policy["spec"]["validations"][0].get("message", "")
    assert "572" in message, (
        f"validation message must cite issue #572; got {message!r}"
    )


def test_secret_read_admission_binding_binds_policy_to_openstudio_server():
    """Issue #572 acceptance #5: the binding names the policy above and
    is schema-correct per the #565 conventions — `validationActions:
    ["Deny"]` (required; the only action that blocks), scoping via
    `matchResources` (the Binding schema has no `selector`), and the
    openstudio-server namespaceSelector mirroring the policy's gate."""
    binding = _secret_read_admission_binding()
    assert binding is not None, (
        "no ValidatingAdmissionPolicyBinding in "
        "deploy/secret-read-admission-policy.yaml — the policy is "
        "dormant without a binding (issue #572)"
    )
    assert binding["apiVersion"] == "admissionregistration.k8s.io/v1"
    spec = binding["spec"]
    policy = _secret_read_admission_policy()
    assert spec["policyName"] == policy["metadata"]["name"]
    assert spec.get("validationActions") == ["Deny"], (
        "Binding.validationActions must be [\"Deny\"] — required by the v1 "
        "Binding schema (issue #565) and the only action that blocks; got "
        f"{spec.get('validationActions')!r}"
    )
    assert "selector" not in spec, (
        "Binding spec has no `selector` field in "
        "admissionregistration.k8s.io/v1 — scope via matchResources "
        "(issue #565)"
    )
    ns_selector = spec.get("matchResources", {}).get("namespaceSelector")
    assert ns_selector and ns_selector.get("matchLabels", {}).get(
        "kubernetes.io/metadata.name"
    ) == _OPERATOR_NS, (
        "Binding must scope via matchResources.namespaceSelector to "
        f"{_OPERATOR_NS!r}; got {ns_selector!r}"
    )


def test_secret_read_admission_cel_denies_operator_sa_non_redis_secret_name():
    """Issue #572 acceptance #6 (interpreter-evaluated): the MANIFEST'S
    OWN CEL rejects the compromise scenario — the operator SA reading/
    touching a Secret OUTSIDE the naming convention (the Mongo
    credentials, the exact exfiltration target named in the issue)."""
    policy = _secret_read_admission_policy()
    expression = policy["spec"]["validations"][0]["expression"]
    for hostile_name in (
        "openstudio-mongo-credentials",
        "os-archive-creds",
        "tls-ca-bundle",
        "openstudio-redi",  # near-miss: proper prefix required
    ):
        assert _cel_secret_read_allows(
            expression, name=hostile_name, username=_OPERATOR_SA_FULL
        ) is False, (
            f"operator-SA request on Secret {hostile_name!r} must be "
            "rejected by the #572 CEL"
        )
    # An empty request.name (server-named CREATE) must fail CLOSED —
    # "".startsWith(...) is false, and failurePolicy: Fail denies.
    assert _cel_secret_read_allows(
        expression, name="", username=_OPERATOR_SA_FULL
    ) is False


def test_secret_read_admission_cel_allows_redis_names_and_other_actors():
    """Issue #572 acceptance #7 (interpreter-evaluated): the carve-out
    works — `openstudio-redis*` names are allowed for the operator SA
    (the CRD-legal secretRef targets), and EVERY other actor is
    untouched by the policy (humans via kubectl, the prune SA, helm —
    the #293-style short-circuit)."""
    policy = _secret_read_admission_policy()
    expression = policy["spec"]["validations"][0]["expression"]
    for legal_name in (
        "openstudio-redis",
        "openstudio-redis-url",
        "openstudio-redis-credentials-v2",
    ):
        assert _cel_secret_read_allows(
            expression, name=legal_name, username=_OPERATOR_SA_FULL
        ) is True, (
            f"operator-SA request on {legal_name!r} (CRD-legal "
            "spec.redisCredentials.secretRef name) must be allowed"
        )
    for other_actor in ("kubernetes-admin", PRUNE_SA_FULL, "system:serviceaccount:kube-system:helm"):
        assert _cel_secret_read_allows(
            expression, name="openstudio-mongo-credentials", username=other_actor
        ) is True, (
            f"non-operator actor {other_actor!r} must be exempt from the "
            "#572 policy (userInfo carve-out)"
        )


# ---------------------------------------------------------------------------
# Issue #606 — the operator Role's secrets:get grant is exact-name-bounded
# by RBAC resourceNames (the GET-path fence #572's admission layer could
# not provide: VAPs run on the mutating path only, and RBAC authz precedes
# admission regardless). Pre-#606 the grant was namespace-wide get, so a
# compromised operator pod could read ANY Secret in openstudio-server
# (Mongo credentials, rclone cloud creds, TLS bundles, metrics bearer
# tokens). The fence is fail-visible by design: the CRD pattern stays
# wider (any openstudio-redis* name is apply-legal), so a CR naming a
# custom Secret is denied at READ time (403 →
# RedisCredentialResolutionError → key-layout "unreachable" + counted
# skip-ticks + a one-time RedisSecretRefForbidden Warning from the
# singleton guard). A cluster admin allows a custom name by adding it to
# the rule's resourceNames — a deliberate, visible, reviewable RBAC
# change. The tests below pin the rule shape and its consistency with
# the shipped tooling + the CRD convention.
# ---------------------------------------------------------------------------

#: The canonical Secret name(s) the shipped tooling creates — the exact
#: set the default Role grants. Derived from deploy/redis-credentials-
#: secret.yaml (the committed manifest) and scripts/rotate_redis_password.
#: sh (SECRET_NAME — the live-Secret rotation path), NOT hand-invented:
#: if the ecosystem ever ships a second canonical name, both this tuple
#: and deploy/rbac.yaml must grow together (the first test fails loudly
#: on the manifest side of that drift).
_CANONICAL_REDIS_SECRET_NAMES = ["openstudio-redis"]


def _operator_role_secrets_rule():
    """Return the operator Role's single ``secrets`` rule (issue #606)."""
    matches = [
        rule
        for rule in OPERATOR_ROLE["rules"]
        if rule["apiGroups"] == [""] and rule["resources"] == ["secrets"]
    ]
    assert len(matches) == 1, (
        f"expected exactly one secrets rule in the operator Role, got "
        f"{matches!r} (issue #606)"
    )
    return matches[0]


def test_operator_role_secrets_get_bounded_to_canonical_resourcenames():
    """Issue #606 acceptance: the secrets rule is `verbs: [get]` ONLY and
    carries `resourceNames` exactly equal to the canonical set the shipped
    tooling creates — the deploy manifest's Secret name and the rotation
    script's SECRET_NAME must both be members (the grant can never be
    narrower than what we ship), and nothing else may be granted (the
    exfiltration path stays closed)."""
    rule = _operator_role_secrets_rule()
    assert rule["verbs"] == ["get"], (
        f"secrets rule must stay get-only; got {rule['verbs']!r} (issues "
        "#463/#606)"
    )
    granted = rule.get("resourceNames")
    assert granted == _CANONICAL_REDIS_SECRET_NAMES, (
        "secrets rule must carry resourceNames exactly equal to the "
        f"canonical set {_CANONICAL_REDIS_SECRET_NAMES!r} — the #606 "
        f"exact-name RBAC fence; got {granted!r}"
    )
    # The canonical set is derived, never invented: every name in it must
    # be created by the committed Secret manifest AND by the rotation
    # script's live-Secret path.
    deploy_secret_doc = next(
        d
        for d in yaml.safe_load_all((DEPLOY / "redis-credentials-secret.yaml").read_text())
        if d and d.get("kind") == "Secret"
    )
    manifest_name = deploy_secret_doc["metadata"]["name"]
    rotate_script = (
        Path(__file__).resolve().parents[1] / "scripts" / "rotate_redis_password.sh"
    ).read_text()
    for name in granted:
        assert name == manifest_name, (
            f"resourceNames entry {name!r} is not the Secret name the "
            f"committed manifest creates ({manifest_name!r}) — the grant "
            "and the shipped tooling have drifted (issue #606)"
        )
        assert f'SECRET_NAME="{name}"' in rotate_script, (
            f"resourceNames entry {name!r} is not the Secret name "
            "scripts/rotate_redis_password.sh rotates — the grant and the "
            "shipped tooling have drifted (issue #606)"
        )


def test_operator_role_secrets_resourcenames_follow_crd_convention():
    """Cross-fence consistency: every name granted via resourceNames must
    satisfy the CRD's `^openstudio-redis[a-z0-9-]*$` secretRef pattern —
    the RBAC exact-name fence and the schema fence must name the same
    convention (mirrors the #572 prefix cross-check)."""
    rule = _operator_role_secrets_rule()
    crd_text = (DEPLOY / "crd.yaml").read_text()
    assert f"^{_REDIS_SECRET_NAME_PREFIX}[a-z0-9-]*$" in crd_text, (
        "deploy/crd.yaml must keep the secretRef.name convention pattern "
        "(checked in depth by tests/test_crd_schema.py; issue #606)"
    )
    pattern = re.compile(rf"^{_REDIS_SECRET_NAME_PREFIX}[a-z0-9-]*$")
    for name in rule["resourceNames"]:
        assert pattern.fullmatch(name), (
            f"resourceNames entry {name!r} violates the "
            f"{_REDIS_SECRET_NAME_PREFIX}* convention the CRD pins — the "
            "RBAC fence would grant a name a CR could never legally "
            "reference (issue #606)"
        )


# ---------------------------------------------------------------------------
# Issue #573 — the fourth ValidatingAdmissionPolicy: the operator SA's
# Deployment mutating surface narrowed to the two managed names,
# `worker` and `web-background`.
#
# deploy/rbac.yaml grants `deployments: [get, list, watch, patch,
# update]` namespace-wide; the documented mutating surface is exactly
# two names (worker_recycler's worker scale-down and
# web_background_monitor's restartedAt restart). A compromised operator
# pod could otherwise patch the `web` Deployment — the Rails pod
# holding the Mongo credentials and the read-write NFS mount — and
# pivot to full app compromise without touching pods/delete (#293) or
# Secrets (#572).
#
# Scope note (mirrors the manifest's own header): the operations list
# is ["UPDATE"] alone because the admission chain presents every HTTP
# PATCH as operation UPDATE — the NamedRuleWithOperations enum has no
# PATCH literal, and naming one is an apply-time Invalid reject (the
# #565 dead-manifest failure mode). Both handler modules mutate
# Deployments via patch_namespaced_deployment, so UPDATE covers 100%
# of the real surface. The tests below pin that shape and evaluate the
# MANIFEST'S OWN CEL with the #398 interpreter.
# ---------------------------------------------------------------------------

_DEPLOYMENT_PATCH_ADMISSION_DOCS = list(
    yaml.safe_load_all((DEPLOY / "deployment-patch-admission-policy.yaml").read_text())
)


def _deployment_patch_admission_policy():
    """Return the ValidatingAdmissionPolicy doc for #573."""
    return next(
        (
            d
            for d in _DEPLOYMENT_PATCH_ADMISSION_DOCS
            if d.get("kind") == "ValidatingAdmissionPolicy"
        ),
        None,
    )


def _deployment_patch_admission_binding():
    """Return the ValidatingAdmissionPolicyBinding doc for #573."""
    return next(
        (
            d
            for d in _DEPLOYMENT_PATCH_ADMISSION_DOCS
            if d.get("kind") == "ValidatingAdmissionPolicyBinding"
        ),
        None,
    )


def _cel_deployment_patch_allows(expression, *, name, username=_OPERATOR_SA_FULL):
    """Admission decision for the #573 validation expression against a
    single-object Deployment request identified by ``request.name`` and
    authenticated as ``username`` (default: the operator SA — the
    principal the policy constrains). Reuses the #398 minimal
    interpreter: True = allowed, False = rejected (falsy OR error — a
    CEL error denies under ``failurePolicy: Fail``)."""
    ctx = {"request": {"userInfo": {"username": username}, "name": name}}
    tree = _CelParser(_cel_tokenize(expression)).parse()
    try:
        return _cel_eval(tree, ctx) is True
    except _CelError:
        return False


def test_deployment_patch_admission_manifest_exists_and_parses():
    """Issue #573 acceptance #1: deploy/deployment-patch-admission-policy.yaml
    exists, parses, and contains exactly the two admissionregistration
    resources — a lone policy is dormant and a lone binding is
    unbound."""
    assert _DEPLOYMENT_PATCH_ADMISSION_DOCS, (
        "deploy/deployment-patch-admission-policy.yaml is missing or empty — "
        "the operator SA's Deployment mutating surface is unconstrained at "
        "the admission layer (issue #573)"
    )
    kinds = sorted(d["kind"] for d in _DEPLOYMENT_PATCH_ADMISSION_DOCS if d)
    assert kinds == [
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
    ], (
        "deploy/deployment-patch-admission-policy.yaml must declare exactly "
        f"a ValidatingAdmissionPolicy + Binding, got {kinds!r}"
    )


def test_deployment_patch_admission_policy_targets_updates_on_apps_deployments():
    """Issue #573 acceptance #2: the policy matches apps/v1 `deployments`
    in `openstudio-server` only. The operations list is `["UPDATE"]`
    ALONE — admission presents every HTTP PATCH (the operator's only
    Deployment mutation path, patch_namespaced_deployment) as operation
    UPDATE, the operations enum has no PATCH literal, and naming one is
    an apply-time Invalid reject (the #565 dead-manifest mode). A
    literal "PATCH" here must fail this fence, not an apiserver apply."""
    policy = _deployment_patch_admission_policy()
    assert policy is not None
    assert policy["apiVersion"] == "admissionregistration.k8s.io/v1"
    match = policy["spec"]["matchConstraints"]
    rule = match["resourceRules"][0]
    assert rule["apiGroups"] == ["apps"]
    assert rule["apiVersions"] == ["v1"]
    assert rule["resources"] == ["deployments"]
    assert rule["operations"] == ["UPDATE"], (
        "operations must be [\"UPDATE\"] — the admission layer presents "
        "HTTP PATCH as UPDATE and there is no PATCH literal in the enum "
        "(a literal 'PATCH' is an apply-time Invalid reject, issue #565); "
        f"got {rule['operations']!r}"
    )
    assert match["namespaceSelector"] == {
        "matchLabels": {"kubernetes.io/metadata.name": _OPERATOR_NS}
    }, (
        "policy namespaceSelector must restrict to "
        f"{_OPERATOR_NS!r} — a cluster-wide match would evaluate the "
        "rule in every namespace (issue #573)"
    )


def test_deployment_patch_admission_policy_cel_carveout_and_managed_names():
    """Issue #573 acceptance #3: the CEL is the #293 carve-out shape —
    the leading clause exempts every actor EXCEPT the operator SA
    (humans and other SAs are untouched), and the trailing clauses
    allow exactly the two managed Deployment names. Cross-fenced
    against the Python constants the handlers actually default to and
    the CRD's #160 enum, so the admission fence, the code, and the
    schema can never drift to different name sets."""
    from openstudio_operator._k8s import DEFAULT_WORKER_DEPLOYMENT
    from openstudio_operator.handlers.web_background_monitor import (
        DEFAULT_WEB_BACKGROUND_DEPLOYMENT,
    )

    policy = _deployment_patch_admission_policy()
    full_expr = _strip_cel_whitespace(
        " ".join(v["expression"] for v in policy["spec"]["validations"])
    )
    assert full_expr.count("||") == 2, (
        "expected exactly the userInfo carve-out disjunct plus the two "
        f"name clauses; got: {full_expr!r}"
    )
    carve_out, worker_clause, web_bg_clause = full_expr.split("||")
    assert "request.userInfo.username" in carve_out and _OPERATOR_SA_FULL in carve_out, (
        f"leading clause must be the userInfo carve-out naming the "
        f"operator SA ({_OPERATOR_SA_FULL!r}); got: {carve_out!r}"
    )
    assert worker_clause.strip() == (
        f"request.name == '{DEFAULT_WORKER_DEPLOYMENT}'"
    ), (
        "first name clause must allow exactly the worker Deployment "
        f"(DEFAULT_WORKER_DEPLOYMENT == {DEFAULT_WORKER_DEPLOYMENT!r}); "
        f"got: {worker_clause!r}"
    )
    assert web_bg_clause.strip() == (
        f"request.name == '{DEFAULT_WEB_BACKGROUND_DEPLOYMENT}'"
    ), (
        "second name clause must allow exactly the web-background "
        "Deployment (DEFAULT_WEB_BACKGROUND_DEPLOYMENT == "
        f"{DEFAULT_WEB_BACKGROUND_DEPLOYMENT!r}); got: {web_bg_clause!r}"
    )
    # Cross-fence: the CRD's #160 enum admits exactly the same two names
    # for both target fields (gated in depth by tests/test_crd_schema.py).
    crd_text = (DEPLOY / "crd.yaml").read_text()
    assert crd_text.count("self in ['worker', 'web-background']") >= 2, (
        "deploy/crd.yaml must keep the #160 enum "
        "\"self in ['worker', 'web-background']\" on BOTH "
        "spec.targetWorkerDeployment and spec.targetWebBackgroundDeployment "
        "— the #573 admission fence and the CRD enum must name the same "
        "pair (worker / web-background)"
    )


def test_deployment_patch_admission_policy_failure_policy_is_fail_and_message_cites_issue():
    """Issue #573 acceptance #4: `failurePolicy: Fail` (a CEL runtime
    error or an unexpected request shape REJECTS — the safe direction),
    and the single-line message (the #565 line-break rule is enforced
    globally by the glob fence) cites the issue an operator hitting the
    deny needs."""
    policy = _deployment_patch_admission_policy()
    assert policy["spec"]["failurePolicy"] == "Fail", (
        "ValidatingAdmissionPolicy.failurePolicy must be 'Fail' so a CEL "
        "evaluation error blocks the request (issue #573); 'Ignore' would "
        "fail the #573 fence open"
    )
    message = policy["spec"]["validations"][0].get("message", "")
    assert "573" in message, (
        f"validation message must cite issue #573; got {message!r}"
    )


def test_deployment_patch_admission_binding_binds_policy_to_openstudio_server():
    """Issue #573 acceptance #5: the binding names the policy above and
    is schema-correct per the #565 conventions — `validationActions:
    ["Deny"]` (required; the only action that blocks), scoping via
    `matchResources` (the Binding schema has no `selector`), and the
    openstudio-server namespaceSelector mirroring the policy's gate."""
    binding = _deployment_patch_admission_binding()
    assert binding is not None, (
        "no ValidatingAdmissionPolicyBinding in "
        "deploy/deployment-patch-admission-policy.yaml — the policy is "
        "dormant without a binding (issue #573)"
    )
    assert binding["apiVersion"] == "admissionregistration.k8s.io/v1"
    spec = binding["spec"]
    policy = _deployment_patch_admission_policy()
    assert spec["policyName"] == policy["metadata"]["name"]
    assert spec.get("validationActions") == ["Deny"], (
        "Binding.validationActions must be [\"Deny\"] — required by the v1 "
        "Binding schema (issue #565) and the only action that blocks; got "
        f"{spec.get('validationActions')!r}"
    )
    assert "selector" not in spec, (
        "Binding spec has no `selector` field in "
        "admissionregistration.k8s.io/v1 — scope via matchResources "
        "(issue #565)"
    )
    ns_selector = spec.get("matchResources", {}).get("namespaceSelector")
    assert ns_selector and ns_selector.get("matchLabels", {}).get(
        "kubernetes.io/metadata.name"
    ) == _OPERATOR_NS, (
        "Binding must scope via matchResources.namespaceSelector to "
        f"{_OPERATOR_NS!r}; got {ns_selector!r}"
    )


def test_deployment_patch_admission_cel_denies_operator_sa_unmanaged_deployment_names():
    """Issue #573 acceptance #6 (interpreter-evaluated): the MANIFEST'S
    OWN CEL rejects the compromise scenario — the operator SA patching
    a Deployment OUTSIDE the managed pair, including the `web`
    Deployment (the Rails pod holding the Mongo credentials and the
    read-write NFS mount — the exact pivot named in the issue) and
    near-miss names."""
    policy = _deployment_patch_admission_policy()
    expression = policy["spec"]["validations"][0]["expression"]
    for hostile_name in (
        "web",  # the Rails pod: Mongo creds + read-write NFS
        "db",  # the Mongo Deployment
        "redis",
        "queue",
        "workerz",  # near-miss: exact name required
        "web-backgroundz",  # near-miss: exact name required
        "openstudio-operator",  # the operator's own Deployment
    ):
        assert _cel_deployment_patch_allows(
            expression, name=hostile_name, username=_OPERATOR_SA_FULL
        ) is False, (
            f"operator-SA request on Deployment {hostile_name!r} must be "
            "rejected by the #573 CEL"
        )
    # An empty request.name must fail CLOSED — '' == 'worker' is false,
    # '' == 'web-background' is false, and failurePolicy: Fail denies.
    assert _cel_deployment_patch_allows(
        expression, name="", username=_OPERATOR_SA_FULL
    ) is False


def test_deployment_patch_admission_cel_allows_managed_names_and_other_actors():
    """Issue #573 acceptance #7 (interpreter-evaluated): the carve-out
    works — the two managed names are allowed for the operator SA (the
    worker-recycler and web-background-restart surfaces), and EVERY
    other actor is untouched by the policy (humans via kubectl, the
    prune SA, helm — the #293-style short-circuit), including on the
    `web` Deployment the policy exists to protect."""
    policy = _deployment_patch_admission_policy()
    expression = policy["spec"]["validations"][0]["expression"]
    for legal_name in ("worker", "web-background"):
        assert _cel_deployment_patch_allows(
            expression, name=legal_name, username=_OPERATOR_SA_FULL
        ) is True, (
            f"operator-SA request on {legal_name!r} (a managed Deployment "
            "name) must be allowed"
        )
    for other_actor in ("kubernetes-admin", PRUNE_SA_FULL, "system:serviceaccount:kube-system:helm"):
        assert _cel_deployment_patch_allows(
            expression, name="web", username=other_actor
        ) is True, (
            f"non-operator actor {other_actor!r} must be exempt from the "
            "#573 policy (userInfo carve-out)"
        )


def test_dockerfile_sets_non_root_user_matching_manifests():
    """Issue #589: the Dockerfile carries its own non-root ``USER`` directive.

    The deploy manifests have pinned ``runAsUser``/``fsGroup`` 1000 since
    #115/#161, but a pod-level securityContext is a scheduler-side control
    that does not travel with the image artifact: ``docker run`` of the
    published image, the release.yml dev-dep assert (``docker run --rm
    --entrypoint python``), and any downstream embed would otherwise
    execute as root. This gate fails if the ``USER`` directive is dropped
    or drifts away from the manifests' UID (file-ownership semantics must
    stay identical).
    """
    dockerfile_text = (DEPLOY.parent / "Dockerfile").read_text()
    user_directives = re.findall(r"(?im)^\s*USER\s+(\S+)\s*$", dockerfile_text)
    assert user_directives, (
        "Dockerfile has no USER directive — the image defaults to root "
        "everywhere outside the deploy manifests (#589)."
    )
    # Dockerfile semantics: the LAST USER directive is the effective one.
    effective_user = user_directives[-1]
    assert effective_user.isdigit(), (
        f"Dockerfile USER {effective_user!r} is not a numeric UID — a name "
        "resolves against the image passwd at build time and can silently "
        "drift across base-image refreshes (#589)."
    )
    uid = int(effective_user)
    assert uid != 0, (
        "Dockerfile USER is root (UID 0) — the #589 non-root default was "
        "regressed."
    )
    assert uid == 1000, (
        f"Dockerfile USER {uid} does not match the manifests' runAsUser/"
        "fsGroup 1000 (deploy/operator-deployment.yaml:55-60, "
        "deploy/storage-cronjob.yaml:151-156) — ownership semantics must "
        "stay identical (#589)."
    )
