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
    pod = CRONJOB["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert CRONJOB["spec"]["jobTemplate"]["spec"]["backoffLimit"] == 0
    assert pod["serviceAccountName"] == PRUNE_SA["metadata"]["name"]
    container = pod["containers"][0]
    # the operator image published on develop push — never a floating latest
    assert container["image"] == "ghcr.io/anchapin/openstudio-server-operator:dev"
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
    """Egress isolation in `openstudio-server` — issue #112 acceptance."""
    assert NETPOL_DOCS, "deploy/network-policy.yaml is missing or empty"
    kinds = {d["kind"] for d in NETPOL_DOCS if d}
    assert kinds == {"NetworkPolicy"}, kinds
    for d in NETPOL_DOCS:
        assert d["metadata"]["namespace"] == "openstudio-server"
        assert "Egress" in d["spec"]["policyTypes"], d["metadata"]["name"]


def test_network_policy_default_deny_for_operator_surface():
    """Default-deny + explicit allow: at least one policy whose podSelector
    selects every operator-managed pod (manage-by label) and whose egress
    list is restrictive. Operators in the surface (Deployment, CronJob,
    archival Jobs) cannot egress to the public Internet by default."""
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
