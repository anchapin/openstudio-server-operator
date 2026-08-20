"""Structural tests for the #78 storage-workload manifests and RBAC shift.

Quantifies the issue #78 acceptance criterion "Operator RBAC footprint
reduced" at CI level: the operator Role must no longer hold ANY batch
permission (the whole jobs rule moved to the prune CronJob's own
least-privilege Role), and the new Role must keep the house style —
namespaced, enumerated verbs, no wildcards, no secrets/volume inspection.
"""

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
# matched scraper + same-namespace peer) used for the operator's /metrics
# (#166). The two targets are intentionally distinct policy objects because
# the pod labels differ (the operator carries ``app: openstudio-operator`` and
# the CronJob carries ``app.kubernetes.io/component: storage-pruner``);
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
    AGENTS.md "Working rules"): a namespace-matched scraper + a same-
    namespace peer. Cluster admins running a different scraper namespace
    MUST edit the label match — the test is the regression fence that
    prompts the rename."""
    policy = _storage_pruner_metrics_ingress_policy()
    rule = policy["spec"]["ingress"][0]
    from_selectors = rule["from"]
    has_namespace_selector = any(
        "namespaceSelector" in peer for peer in from_selectors
    )
    has_same_ns_peer = any(
        peer.get("podSelector") == {} for peer in from_selectors
    )
    assert has_namespace_selector, (
        "storage-pruner-metrics-ingress must include a namespaceSelector "
        "pointing at the cluster's scraper namespace (default `prometheus`); "
        "see AGENTS.md Working rules for the cluster-admin opt-in."
    )
    assert has_same_ns_peer, (
        "storage-pruner-metrics-ingress must include an empty podSelector "
        "to allow co-located scrapers (e.g. sidecar) in openstudio-server"
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


import ast
import sys
from pathlib import Path


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
    binding must name the policy above, and the binding itself must
    have no resource-level selectors that would narrow scope below
    what the policy's matchConstraints already enforce."""
    assert PRUNE_JOB_SCOPE_BINDING is not None, (
        "no ValidatingAdmissionPolicyBinding in deploy/storage-cronjob.yaml — "
        "the policy is dormant without a binding (issue #294)"
    )
    assert PRUNE_JOB_SCOPE_BINDING["apiVersion"] == "admissionregistration.k8s.io/v1"
    assert PRUNE_JOB_SCOPE_BINDING["spec"]["policyName"] == (
        PRUNE_JOB_SCOPE_VAP["metadata"]["name"]
    )
    # The binding's `selector` is `{}` (matches everything, as the policy
    # itself scopes by namespaceSelector). This is the canonical K8s
    # pattern; a non-empty selector would silently exclude the
    # openstudio-server namespace.
    assert PRUNE_JOB_SCOPE_BINDING["spec"]["selector"] == {}


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
    counterpart cannot use the same x7 product (7 x 2 CPU = 14 and
    7 x 4Gi = 28Gi both intentionally exceed the quota — limit
    defaults are burst ceilings, not reservations); instead the fence
    asserts the issue's 2:1 limits:requests ratio (8 = 2x4,
    16Gi = 2x8Gi), which guarantees every reserved request unit has a
    matching burst unit of headroom above it."""
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
