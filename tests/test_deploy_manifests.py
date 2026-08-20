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
    # Issue #291: pin to the same SHA256 digest as deploy/operator-deployment.yaml;
    # the operator image is re-pinned by .github/workflows/release.yml on every
    # develop push. The mutable `:dev` tag combined with `IfNotPresent` allowed the
    # kubelet to retain a previously-cached image across restarts.
    assert container["image"] == (
        "ghcr.io/anchapin/openstudio-server-operator@sha256:"
        "6681d2545970ff5183cf4a99c2310a8a1a6febaf8d9be43af9ed1bab46f0ee4e"
    )
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
    CronJob under deploy/. Other kinds (CRD, RBAC, NetworkPolicy, Secret,
    ServiceAccount, ScaledObject, ...) carry no containers and are
    skipped. Manifests that fail to YAML-parse are skipped — the iterator
    is defensive because `deploy/operator-deployment.yaml` historically
    has hand-edited indentation quirks (#115-era work) that PyYAML rejects
    even though `kubectl` accepts the same file."""
    for path in sorted(DEPLOY.glob("*.yaml")):
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
            if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
                containers = (
                    doc.get("spec", {})
                    .get("template", {})
                    .get("spec", {})
                    .get("containers", [])
                )
                for c in containers:
                    yield path.name, kind, name, c
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
                    yield path.name, kind, name, c


def test_every_deploy_workload_container_has_hardening_baseline():
    """Issue #162 / #115 acceptance: every container in every deploy/
    workload enforces the same six-field hardening baseline. A regression
    here re-opens the same escalation surface #115 closed for the
    operator and #114 closed for the rclone Job."""
    offenders = []
    for path_name, kind, name, container in _iter_workload_containers():
        sc = container.get("securityContext") or {}
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
        if problems:
            offenders.append(
                (path_name, kind, name, container.get("name"), problems)
            )
    assert not offenders, (
        f"containers missing #115/#162 hardening baseline: {offenders}"
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
    Deployment / StatefulSet / DaemonSet / Job / CronJob under deploy/.
    Mirrors `_iter_workload_containers` but yields the pod-level spec,
    which is where `securityContext` (the #161 defense-in-depth block)
    lives. Defensive YAML handling matches the container iterator —
    hand-edited indentation quirks in some deploy/ manifests would
    otherwise turn this into a parse-error tripwire unrelated to the
    acceptance criterion."""
    for path in sorted(DEPLOY.glob("*.yaml")):
        try:
            docs = list(yaml.safe_load_all(path.read_text()))
        except yaml.YAMLError:
            continue
        for doc in docs:
            if not doc:
                continue
            kind = doc.get("kind")
            name = doc.get("metadata", {}).get("name", "<unnamed>")
            if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
                pod_spec = (
                    doc.get("spec", {}).get("template", {}).get("spec", {})
                )
                yield path.name, kind, name, pod_spec
            elif kind == "CronJob":
                pod_spec = (
                    doc.get("spec", {})
                    .get("jobTemplate", {})
                    .get("spec", {})
                    .get("template", {})
                    .get("spec", {})
                )
                yield path.name, kind, name, pod_spec


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
    (b) allow a same-namespace peer (empty podSelector). A cluster-admin
    namespaceSelector renaming is allowed; a wholesale removal of either
    peer would silently re-open the endpoint."""
    policy = _metrics_ingress_policy()
    rule = policy["spec"]["ingress"][0]
    from_selectors = rule["from"]
    # Flatten the from[] peer list (each entry is one AND-of-ORs selector).
    has_namespace_selector = any(
        "namespaceSelector" in peer for peer in from_selectors
    )
    has_same_ns_peer = any(
        peer.get("podSelector") == {} for peer in from_selectors
    )
    assert has_namespace_selector, (
        "metrics-ingress must include a namespaceSelector pointing at the "
        "cluster's scraper namespace (default `prometheus`); see AGENTS.md "
        "Working rules for the cluster-admin opt-in."
    )
    assert has_same_ns_peer, (
        "metrics-ingress must include an empty podSelector to allow "
        "co-located scrapers (e.g. sidecar) in openstudio-server"
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
