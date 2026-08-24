"""Issue #719: CI gate asserting ``deploy/secret-read-admission-policy.yaml``
name set matches RBAC ``secrets`` ``resourceNames``.

Background
----------

The ValidatingAdmissionPolicy in ``deploy/secret-read-admission-policy.yaml``
(#572) restricts the operator SA's Secret MUTATIONS to names satisfying
``request.name.startsWith('openstudio-redis')``. The narrower
``openstudio-redis-secret-reader-role`` in ``deploy/rbac.yaml`` (#715) holds
the operator SA's only ``secrets: get`` grant, bounded by
``resourceNames: ["openstudio-redis"]``.

These two manifests must stay coherent: every name in RBAC
``resourceNames`` must satisfy the VAP's ``startsWith`` prefix. A future
maintainer who adds a name to RBAC without updating the VAP's prefix (or
vice versa) creates a gap where the RBAC grant covers a name the admission
policy does not. This test fails loudly in CI instead.

Scope: this test does NOT modify either manifest; it only AST-validates
that the two files' name sets are coherent.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy"
RBAC_PATH = DEPLOY / "rbac.yaml"
VAP_PATH = DEPLOY / "secret-read-admission-policy.yaml"

_RBAC_DOCS = list(yaml.safe_load_all(RBAC_PATH.read_text()))
_VAP_DOCS = list(yaml.safe_load_all(VAP_PATH.read_text()))


def _vap_expression() -> str:
    """Return the CEL expression string from the secret-read VAP.

    The VAP carries one ``validations[*].expression`` entry that contains
    ``request.name.startsWith('<prefix>')``. The full expression is a
    single-line string (strip-chomped ``>-`` scalar in the YAML source).
    """
    vap = next(
        d for d in _VAP_DOCS
        if d and d.get("kind") == "ValidatingAdmissionPolicy"
    )
    validations = vap.get("spec", {}).get("validations") or []
    assert validations, "secret-read VAP must have at least one validation"
    # Concatenate all expression lines (the ``>-`` folded scalar may split
    # across multiple YAML lines; joining preserves the original CEL syntax).
    expr = "".join(v.get("expression", "") for v in validations)
    return expr


def _vap_starts_with_prefix() -> str:
    """Extract the ``startsWith`` prefix from the VAP CEL expression.

    Matches ``request.name.startsWith('<prefix>')`` and returns ``<prefix>``.
    Fails with a clear message if the CEL shape ever changes.
    """
    expr = _vap_expression()
    # CEL: request.name.startsWith('openstudio-redis')
    # The expression is a single logical line; strip-chomping removes the
    # trailing newline but preserves the single-quoted string.
    m = re.search(r"request\.name\.startsWith\('([^']+)'\)", expr)
    assert m, (
        f"secret-read VAP expression must contain "
        f"request.name.startsWith('<prefix>'); got {expr!r}"
    )
    return m.group(1)


def _all_rbac_secrets_resource_names() -> list[str]:
    """Return every ``resourceNames`` entry from every secrets rule in
    ``deploy/rbac.yaml``."""
    names: list[str] = []
    for doc in _RBAC_DOCS:
        if not doc or doc.get("kind") != "Role":
            continue
        for rule in doc.get("rules") or []:
            if "secrets" in (rule.get("resources") or []):
                names.extend(rule.get("resourceNames") or [])
    return names


def test_secret_read_vap_resource_names_parity():
    """Issue #719: every name in RBAC ``resourceNames`` must satisfy the
    VAP's ``startsWith`` prefix.

    If a future maintainer adds a name to the RBAC ``resourceNames`` list
    without updating the VAP's ``startsWith`` prefix (or the reverse), the
    two manifests are no longer coherent — the RBAC grant could cover a
    name the admission policy does not restrict. This test fails in CI
    instead of silently opening a gap.
    """
    prefix = _vap_starts_with_prefix()
    rbac_names = _all_rbac_secrets_resource_names()
    assert rbac_names, "deploy/rbac.yaml must declare at least one secrets rule"

    offenders = [name for name in rbac_names if not name.startswith(prefix)]
    assert not offenders, (
        f"RBAC resourceNames and VAP startsWith prefix are incoherent "
        f"(issue #719): every name in RBAC resourceNames must satisfy "
        f"the VAP's startsWith prefix {prefix!r}. Offenders: {offenders!r}. "
        f"Fix: either add the name to the VAP's startsWith expression or "
        f"remove it from RBAC resourceNames. Do NOT modify this test to "
        f"paper over the gap — the incoherence is real."
    )
