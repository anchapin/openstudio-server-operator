"""Issue #715: narrower Role+Binding pair carries the operator's only
``secrets: get`` grant — RBAC structural fence that fails loudly if a
future PR re-merges a ``secrets`` rule into the cross-cutting
``openstudio-operator-role``.

Background
----------

Pre-#715 the operator's ``secrets: get`` lived in the broad
``openstudio-operator-role`` (the cross-cutting namespaced Role carrying
every operator surface — OSCM, deployments, pods, events), bounded only
by ``resourceNames: ["openstudio-redis"]``. Issue #606 closed the
namespace-wide read by adding that constraint, but the ``secrets`` verb
was still one of seven rules in a single Role — a future PR that adds
another Secret name to ``resourceNames`` (e.g. for a new credential
backend), or that loosens the rule any other way, would silently widen
the read without anyone touching the dedicated surface.

#715 splits that rule into its own narrower Role+Binding pair. The
operator SA's effective ``secrets`` verbs now come ONLY from
``openstudio-redis-secret-reader-role`` — the broad
``openstudio-operator-role`` carries no ``secrets`` rule at all. The
threat model the AGENTS.md "never reads secrets" rule describes
("a compromised operator pod could read ANY Secret in
``openstudio-server``") is closed by RBAC shape alone: any future PR
that re-merges a ``secrets`` verb into ``openstudio-operator-role``
fails at least one of the four tests below.

The four-layer fence that justified the single in-Role rule pre-#715
carries over verbatim; only WHERE the RBAC ``secrets`` rule lives
changed. The operational behavior is identical, the audit footprint is
smaller:

1. RBAC exact-name (``resourceNames: ["openstudio-redis"]``, issue #606)
2. CRD pattern on ``spec.redisCredentials.secretRef.name`` (issue #463)
3. Admission mutation-fence (issue #572 — VAP cannot intercept GET)
4. Single AST-gated construction site
   (``client_factory._resolve_redis_url``)
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
RBAC_PATH = DEPLOY / "rbac.yaml"

_OPERATOR_SA_NAME = "openstudio-operator-sa"
_OPERATOR_NS = "openstudio-server"
_NARROWER_ROLE_NAME = "openstudio-redis-secret-reader-role"
_NARROWER_RB_NAME = "openstudio-redis-secret-reader-rb"
_OPERATOR_ROLE_NAME = "openstudio-operator-role"

_RBAC_DOCS = list(yaml.safe_load_all(RBAC_PATH.read_text()))


def _roles() -> list[dict]:
    return [d for d in _RBAC_DOCS if d.get("kind") == "Role"]


def _rolebindings() -> list[dict]:
    return [d for d in _RBAC_DOCS if d.get("kind") == "RoleBinding"]


def _role_by_name(name: str) -> dict:
    matches = [r for r in _roles() if r["metadata"]["name"] == name]
    assert len(matches) == 1, (
        f"expected exactly one Role {name!r} in deploy/rbac.yaml, "
        f"found {len(matches)} (issue #715)"
    )
    return matches[0]


def _rolebinding_by_name(name: str) -> dict:
    matches = [rb for rb in _rolebindings() if rb["metadata"]["name"] == name]
    assert len(matches) == 1, (
        f"expected exactly one RoleBinding {name!r} in deploy/rbac.yaml, "
        f"found {len(matches)} (issue #715)"
    )
    return matches[0]


def _operator_sa_secrets_verbs_and_contributors() -> tuple[set[str], set[str]]:
    """Walk every RoleBinding targeting the operator SA; for each bound
    Role, accumulate the ``secrets`` verbs it grants and the Role's name.

    Returns (effective_secrets_verbs, contributing_role_names). A future
    PR that re-merges a ``secrets`` rule into the broader namespaced
    Role shows up as a SECOND entry in ``contributing_role_names`` and
    fails ``test_operator_sa_secrets_verbs_only_flow_through_narrower_role``."""
    effective_verbs: set[str] = set()
    contributors: set[str] = set()
    for rb in _rolebindings():
        subjects = rb.get("subjects") or []
        targets_operator_sa = any(
            s.get("kind") == "ServiceAccount"
            and s.get("name") == _OPERATOR_SA_NAME
            and s.get("namespace") == _OPERATOR_NS
            for s in subjects
        )
        if not targets_operator_sa:
            continue
        bound_role_name = rb["roleRef"]["name"]
        role = _role_by_name(bound_role_name)
        for rule in role["rules"]:
            if "secrets" in rule.get("resources", []):
                effective_verbs.update(rule.get("verbs", []))
                contributors.add(bound_role_name)
    return effective_verbs, contributors


def test_narrower_role_exists_with_exact_rule_shape():
    """Issue #715 acceptance #1: the narrower ``openstudio-redis-secret-
    reader-role`` exists and carries exactly one rule — the
    ``secrets: get`` grant on ``resourceNames: ["openstudio-redis"]``.
    Any broader shape (more verbs, more resource names, additional
    resources, additional apiGroups, multiple rules) silently re-opens
    the cross-cutting secrets surface and fails this fence."""
    role = _role_by_name(_NARROWER_ROLE_NAME)
    assert role["metadata"]["namespace"] == _OPERATOR_NS, (
        f"narrower Role must live in {_OPERATOR_NS!r} (where the operator "
        f"SA runs), got {role['metadata'].get('namespace')!r}"
    )
    assert len(role["rules"]) == 1, (
        f"narrower Role must have exactly ONE rule — the secrets:get "
        f"grant. Adding more rules silently widens the read surface "
        f"(issue #715); got {role['rules']!r}"
    )
    rule = role["rules"][0]
    expected = {
        "apiGroups": [""],
        "resources": ["secrets"],
        "verbs": ["get"],
        "resourceNames": ["openstudio-redis"],
    }
    assert rule == expected, (
        f"narrower Role rule must be exactly {expected!r} — the #463 "
        f"bounded exception, fenced by #606 RBAC resourceNames. Any "
        f"deviation (additional verbs like list/watch/create/update/"
        f"delete/deletecollection, broader apiGroups, additional "
        f"resources, additional resourceNames) silently widens the "
        f"surface this narrower Role exists to constrain. Got {rule!r} "
        f"(issue #715 / #606)"
    )


def test_narrower_rolebinding_wires_narrower_role_to_operator_sa():
    """Issue #715 acceptance #2: the narrower ``openstudio-redis-secret-
    reader-rb`` RoleBinding references the narrower Role by name AND
    binds it to the operator ServiceAccount — both halves of the wiring.
    A Role that no RoleBinding references is structurally correct but
    operationally dead; this test pins the roleRef + subject triple as
    one atomic check."""
    rb = _rolebinding_by_name(_NARROWER_RB_NAME)
    assert rb["metadata"]["namespace"] == _OPERATOR_NS, (
        f"narrower RoleBinding must live in {_OPERATOR_NS!r} (same "
        f"namespace as the SA it targets), got "
        f"{rb['metadata'].get('namespace')!r}"
    )
    assert rb["roleRef"]["kind"] == "Role", rb["roleRef"]
    assert rb["roleRef"]["name"] == _NARROWER_ROLE_NAME, (
        f"narrower RoleBinding must reference the narrower Role "
        f"{_NARROWER_ROLE_NAME!r}, got {rb['roleRef']['name']!r}"
    )
    assert rb["roleRef"]["apiGroup"] == "rbac.authorization.k8s.io", (
        f"roleRef.apiGroup must be 'rbac.authorization.k8s.io' "
        f"(issue #715); got {rb['roleRef'].get('apiGroup')!r}"
    )
    subjects = rb.get("subjects") or []
    sa_subjects = [
        s for s in subjects
        if s.get("kind") == "ServiceAccount"
        and s.get("name") == _OPERATOR_SA_NAME
        and s.get("namespace") == _OPERATOR_NS
    ]
    assert len(sa_subjects) == 1, (
        f"narrower RoleBinding must have exactly one subject referencing "
        f"the operator SA ({_OPERATOR_SA_NAME!r} in {_OPERATOR_NS!r}), "
        f"got {subjects!r}"
    )


def test_operator_role_has_no_secrets_rule():
    """Issue #715 acceptance #3: the broader ``openstudio-operator-role``
    no longer carries ANY rule granting the ``secrets`` resource — the
    narrower Role+Binding pair is the operator SA's ONLY path to that
    verb. A future PR that re-merges a secrets rule here fails loudly."""
    role = _role_by_name(_OPERATOR_ROLE_NAME)
    secrets_rules = [
        rule for rule in role["rules"]
        if "secrets" in (rule.get("resources") or [])
    ]
    assert secrets_rules == [], (
        f"openstudio-operator-role must NOT carry ANY secrets rule — "
        f"issue #715 split the `secrets: get` grant into the narrower "
        f"Role {_NARROWER_ROLE_NAME!r}; a regression that re-merges it "
        f"here silently widens the operator's read surface (the threat "
        f"model the AGENTS.md 'never reads secrets' rule describes). "
        f"Found {secrets_rules!r}"
    )


def test_operator_sa_secrets_verbs_only_flow_through_narrower_role():
    """Issue #715 acceptance #4 (the regression fence): the operator
    SA's effective ``secrets`` verbs come ONLY from
    ``openstudio-redis-secret-reader-role`` — and the set is exactly
    ``{"get"}``.

    This walks every RoleBinding in ``deploy/rbac.yaml`` targeting the
    operator SA, resolves the bound Role, and accumulates the
    ``secrets`` verbs each grants. A future PR that:

    * re-merges a broader secrets rule into ``openstudio-operator-role``,
    * widens the narrower Role's rule (extra verbs, extra
      resourceNames),
    * or adds a NEW Role/Binding pair that ALSO grants ``secrets`` to
      the operator SA,

    trips at least one of the two assertions below. This is the fence
    the issue body calls for — "RBAC regression test that fails if
    ``deploy/rbac.yaml`` re-merges the secrets verb into a cross-cutting
    namespaced Role."""
    effective_verbs, contributors = _operator_sa_secrets_verbs_and_contributors()
    assert effective_verbs == {"get"}, (
        f"operator SA's effective secrets verbs must be exactly "
        f"{{'get'}} — broader verbs (list/watch/create/update/delete/"
        f"deletecollection) silently widen the read surface; an empty "
        f"set means client_factory._resolve_redis_url (#463) would 403 "
        f"at read time. Got {sorted(effective_verbs)!r} "
        f"(issue #715 / #606)"
    )
    assert contributors == {_NARROWER_ROLE_NAME}, (
        f"operator SA's secrets verbs must flow ONLY through the "
        f"narrower Role {_NARROWER_ROLE_NAME!r} — a second contributor "
        f"means the broader Role gained a secrets rule, OR a NEW "
        f"Role+Binding pair was added that ALSO grants secrets. The "
        f"audit footprint shrinks by exactly one contributor: "
        f"the broader Role loses its secrets rule. Got contributors "
        f"{sorted(contributors)!r} (issue #715)"
    )
