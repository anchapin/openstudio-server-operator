"""CI drift gate (issue #411): the production runbook ``docs/validation.md``
must gate its Prerequisites on Kubernetes 1.30+ and carry both VAP apply
steps in the correct order.

Why this test exists
--------------------
The two ValidatingAdmissionPolicies that narrow the operator / prune
ServiceAccounts beyond what RBAC can express — ``deploy/
pod-delete-admission-policy.yaml`` (#293) and the policy embedded in
``deploy/storage-cronjob.yaml`` (#294) — are ``admissionregistration.k8s.io/v1``
objects, an API that is GA only on Kubernetes 1.30+. PR #413 added the two
Module-status rows and the Prerequisites apply-order bullet, but the runbook
still had no version gate and no explicit apply step in Phase A: a cluster
admin following it on a K8s 1.29 cluster would deploy successfully and then
have the operator's worker-pod eviction fail admission at the first
escalation. Issue #411 closed the gap in the prose; this test holds it shut.
Without a parser over the runbook, a doc refactor could silently drop the
gate (or reorder the apply steps past the rbac.yaml anchor) and CI would
stay green while the 1.29-cluster failure mode returned.

What is gated
-------------
- ``## Prerequisites`` contains a ``kubectl version --short`` invocation
  and a ``1.30`` minor-version claim (the gate itself), plus the
  fail-forward caveat for pre-1.30 clusters (skip BOTH policies).
- ``## Phase A`` applies ``deploy/crd.yaml`` → ``deploy/rbac.yaml`` →
  ``deploy/pod-delete-admission-policy.yaml`` in that order (the VAP must
  land AFTER ``rbac.yaml`` so the operator SA referenced in its CEL rule
  already exists at admission-evaluation time — same constraint the kind
  walkthrough documents), and notes that the prune-side VAP rides inside
  ``deploy/storage-cronjob.yaml`` with no apply step of its own.

Scope guard (issue #301)
------------------------
This test does NOT touch ``deploy/pod-delete-admission-policy.yaml`` or
``deploy/storage-cronjob.yaml`` (the manifests are gated by
``tests/test_deploy_manifests.py``) and does NOT touch
``docs/kind-validation.md`` (the kind recipe already has the apply order;
issue #411 explicitly scopes it out).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATION_RUNBOOK = REPO_ROOT / "docs" / "validation.md"


def _section(text: str, heading_pattern: str) -> str:
    """Return the body of the first ``##``-level section whose heading
    matches ``heading_pattern`` (a regex fragment), up to the next ``##``
    heading. ``###`` subsections inside the section are included — the
    K8s minor-version gate lives in a ``###`` inside ``## Prerequisites``.
    """
    for match in re.finditer(r"^## .*$", text, re.MULTILINE):
        if re.search(heading_pattern, match.group(0)):
            remainder = text[match.end() :]
            next_section = re.search(r"^## ", remainder, re.MULTILINE)
            return remainder[: next_section.start()] if next_section else remainder
    raise AssertionError(
        f"docs/validation.md has no '## ' heading matching {heading_pattern!r}"
    )


def test_prerequisites_gates_on_k8s_1_30_with_kubectl_version():
    """``## Prerequisites`` must gate on ``kubectl version --short`` and
    name the 1.30 minimum. Acceptance criterion (issue #411): "gates on
    ``kubectl version --short`` ≥ 1.30".
    """
    text = VALIDATION_RUNBOOK.read_text(encoding="utf-8")
    prerequisites = _section(text, r"^## Prerequisites$")
    assert "kubectl version --short" in prerequisites, (
        "docs/validation.md §Prerequisites no longer gates on "
        "'kubectl version --short'. Restore the '### K8s minor version gate "
        "(1.30+)' subsection with the version check command."
    )
    assert re.search(r"1\.30", prerequisites), (
        "docs/validation.md §Prerequisites mentions the version command but "
        "no longer names the 1.30 minimum. Restore the '1.30+' claim next to "
        "'kubectl version --short'."
    )


def test_prerequisites_gate_fails_forward_on_pre_1_30():
    """The gate must tell a pre-1.30 cluster admin to skip BOTH VAPs and
    continue (RBAC-only guardrails) — the 'fails forward' caveat from the
    issue's Fix section. Without it the gate reads as a hard abort with no
    remediation.
    """
    text = VALIDATION_RUNBOOK.read_text(encoding="utf-8")
    prerequisites = _section(text, r"^## Prerequisites$")
    assert re.search(r"[Pp]re-1\.30", prerequisites), (
        "docs/validation.md §Prerequisites lost the 'pre-1.30' branch of "
        "the K8s minor version gate. Restore it."
    )
    skip_match = re.search(r"[Pp]re-1\.30[^\n]*(?:\n[^\n]*){0,3}", prerequisites)
    assert skip_match and re.search(r"skip", skip_match.group(0), re.IGNORECASE), (
        "docs/validation.md §Prerequisites mentions pre-1.30 but no longer "
        "says to skip the admission policies on such clusters. Restore the "
        "fail-forward caveat (skip BOTH VAPs, RBAC-only guardrails)."
    )


def test_phase_a_applies_pod_delete_vap_after_rbac():
    """Phase A step 1 must apply the manifests in the order
    ``crd.yaml`` → ``rbac.yaml`` → ``pod-delete-admission-policy.yaml``.
    The VAP must land AFTER ``rbac.yaml`` so the operator SA referenced in
    its CEL rule already exists at admission-evaluation time (the same
    constraint ``docs/kind-validation.md`` Phase 1 documents); applying it
    before (or without) RBAC breaks the admission wiring silently.
    """
    text = VALIDATION_RUNBOOK.read_text(encoding="utf-8")
    phase_a = _section(text, r"^## Phase A")
    order = [
        "kubectl apply -f deploy/crd.yaml",
        "kubectl apply -f deploy/rbac.yaml",
        "kubectl apply -f deploy/pod-delete-admission-policy.yaml",
    ]
    positions = []
    for command in order:
        position = phase_a.find(command)
        assert position != -1, (
            f"docs/validation.md §Phase A is missing the explicit apply step "
            f"'{command}'. Restore it (issue #411 — the operator VAP must be "
            f"applied by the runbook, not left implicit)."
        )
        positions.append(position)
    assert positions == sorted(positions), (
        f"docs/validation.md §Phase A apply order regressed: commands appear "
        f"at offsets {positions}, expected ascending {sorted(positions)} "
        f"({order}). The pod-delete VAP must be applied AFTER rbac.yaml."
    )


def test_prune_vap_rides_in_storage_cronjob_manifest():
    """Phase A must carry the parallel note that the prune-side VAP (#294)
    rides inside ``deploy/storage-cronjob.yaml`` — one apply installs the
    CronJob and the VAP together, and it is the second of the 'two VAP
    apply steps in the correct order' acceptance criterion (after the
    Phase A pod-delete apply, whenever the storage CronJob is installed).
    """
    text = VALIDATION_RUNBOOK.read_text(encoding="utf-8")
    phase_a = _section(text, r"^## Phase A")
    assert "deploy/storage-cronjob.yaml" in phase_a, (
        "docs/validation.md §Phase A no longer mentions "
        "deploy/storage-cronjob.yaml. Restore the note that the prune-side "
        "VAP rides inside that manifest."
    )
    assert re.search(r"rides\s+inside", phase_a), (
        "docs/validation.md §Phase A lost the 'rides inside' phrasing that "
        "ties the prune-side VAP (#294) to deploy/storage-cronjob.yaml. "
        "Restore the parallel note (no apply step of its own)."
    )
    assert (
        "kubectl apply -f deploy/storage-cronjob.yaml" in phase_a
    ), (
        "docs/validation.md §Phase A must show the "
        "'kubectl apply -f deploy/storage-cronjob.yaml' step inside the "
        "prune-VAP note so both VAP apply steps are explicit."
    )
