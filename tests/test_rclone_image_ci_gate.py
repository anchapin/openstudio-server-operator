"""Issue #642: the rclone sidecar image must be trivy-gated in CI, single-source.

The ci.yml ``audit`` job scans the locally-built operator image (#481) but
the digest-pinned rclone sidecar (``archival.RCLONE_IMAGE``, pinned by
#124/#417) was invisible to every gate — Dependabot's docker ecosystem sees
Dockerfile ``FROM`` lines only, and a digest pin freezes the artifact, not
its CVE record. The fix scans the rclone reference at the same CRITICAL,HIGH
floor with the shared ``.trivyignore`` triage rules.

These tests are YAML-structural assertions over the committed workflow
(same shape as tests/test_cosign_verification_ordering.py —
``yaml.safe_load`` the workflow and assert on the parsed structure, never
on comment text). The load-bearing fence is single-source (#417): the
scanned reference must be EMITTED from ``openstudio_operator.archival``
via the Python emit step — hardcoding the digest in the workflow would
create a second pin site that drifts independently of archival.py and of
the #641 CEL cross-fence in deploy/storage-cronjob.yaml.
"""

from __future__ import annotations

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"

EMIT_STEP_ID = "emit-rclone-image"

# The trivy-action commit SHA the #481 operator-image scan is pinned to —
# the rclone scan must reuse the exact same pin (one trivy version per
# workflow, so gate behavior cannot drift between the two scans).
TRIVY_ACTION_PIN = "aquasecurity/trivy-action@ed142fd0673e97e23eac54620cfb913e5ce36c25"


def load_ci() -> dict:
    doc = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def audit_steps(doc: dict) -> list[dict]:
    steps = doc["jobs"]["audit"]["steps"]
    assert isinstance(steps, list)
    return steps


def test_audit_job_emits_rclone_image_from_archival_module() -> None:
    """The audit job carries the #642 emit step, and it imports the pin from
    ``openstudio_operator.archival`` — the single-source rule (#417): the
    workflow must never carry its own copy of the digest."""
    emit = next(
        (s for s in audit_steps(load_ci()) if s.get("id") == EMIT_STEP_ID),
        None,
    )
    assert emit is not None, (
        "ci.yml audit job must carry the rclone emit step "
        f"(id: {EMIT_STEP_ID}) that resolves RCLONE_IMAGE from "
        "openstudio_operator.archival (issue #642)"
    )
    run = emit.get("run", "")
    assert "from openstudio_operator.archival import RCLONE_IMAGE" in run, (
        "the emit step must import RCLONE_IMAGE from "
        "openstudio_operator.archival so the pin stays single-source (#417)"
    )


def test_audit_job_trivy_scans_the_emitted_rclone_reference() -> None:
    """A trivy step scans exactly the emit step's output at the #481 floor:
    CRITICAL,HIGH severity, exit-code 1, the shared .trivyignore triage
    file, ignore-unfixed, and the same trivy-action commit pin as the
    operator-image scan."""
    scan = next(
        (
            s
            for s in audit_steps(load_ci())
            if isinstance(s.get("uses"), str)
            and s["uses"].startswith("aquasecurity/trivy-action@")
            and s.get("with", {}).get("image-ref")
            == "${{ steps." + EMIT_STEP_ID + ".outputs.rclone-image }}"
        ),
        None,
    )
    assert scan is not None, (
        "ci.yml audit job must trivy-scan the emitted rclone reference via "
        f"`image-ref: ${{ steps.{EMIT_STEP_ID}.outputs.rclone-image }}` "
        "(issue #642)"
    )
    assert scan["uses"] == TRIVY_ACTION_PIN, (
        "the rclone scan must reuse the exact trivy-action commit pin the "
        "#481 operator-image scan uses — two pins would let gate behavior "
        "drift between the two scans"
    )
    with_block = scan.get("with", {})
    assert with_block.get("severity") == "CRITICAL,HIGH"
    assert with_block.get("exit-code") == 1
    assert with_block.get("trivyignores") == ".trivyignore"
    assert with_block.get("ignore-unfixed") is True


def test_no_rclone_digest_hardcoded_in_the_workflow() -> None:
    """No step of the audit job feeds trivy a hardcoded ``@sha256:`` image
    reference — the scanned ``image-ref`` must be the emit-step output
    expression, never a literal digest (a second pin site would drift
    independently of archival.py and of the #641 CEL cross-fence). Only the
    action inputs are checked: the emit step's ``run:`` block legitimately
    mentions ``@sha256:`` inside its shape-validation regex."""
    for step in audit_steps(load_ci()):
        with_block = step.get("with", {})
        assert isinstance(with_block, dict)
        for key, value in with_block.items():
            assert "@sha256:" not in str(value), (
                f"ci.yml audit job step input '{key}' must not hardcode an "
                "@sha256: image digest — emit it from "
                "openstudio_operator.archival instead (issues #642 / #417 "
                "single-source rule)"
            )
