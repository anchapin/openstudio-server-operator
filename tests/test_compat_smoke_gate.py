"""Issue #679: the committed image digest must stay compatible with deploy/.

The pinned digest in ``deploy/operator-deployment.yaml`` is re-pinned by
release.yml on every develop push, but between publishes the sibling
manifests (RBAC verbs / #606 ``resourceNames``, VAP semantics, CRD fields
like the #463 ``spec.redisCredentials.secretRef``) can drift ahead of the
last published dev image — a directory-level ``kubectl apply -f deploy/``
then ships a self-incompatible snapshot. The compatibility smoke gate
(.github/workflows/compat-smoke-gate.yml running
scripts/compat-smoke-gate.sh) boots the committed digest against the
current deploy/ set on a disposable kind cluster and fails loudly on the
two live-observed failure signatures.

These tests are YAML-structural / script-shape assertions (same shape as
tests/test_cosign_verification_ordering.py and
tests/test_rclone_image_ci_gate.py — ``yaml.safe_load`` the workflow and
assert on the parsed structure, never on comment text). They NEVER boot
kind; the gate itself runs only in its scheduled/dispatched workflow.
"""

from __future__ import annotations

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "compat-smoke-gate.yml"
SCRIPT = ROOT / "scripts" / "compat-smoke-gate.sh"


def load_workflow() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def gate_steps(doc: dict) -> list[dict]:
    steps = doc["jobs"]["compat-smoke-gate"]["steps"]
    assert isinstance(steps, list)
    return steps


# ---------------------------------------------------------------------------
# Trigger shape: scheduled + dispatch only — never PR/push.
# ---------------------------------------------------------------------------


def test_gate_workflow_triggers_are_schedule_and_dispatch_only() -> None:
    """The gate fires weekly + on demand, and deliberately NOT on
    pull_request/push: a PR's digest only exists after publish (the
    committed pin is what the gate boots), and a kind cluster + GHCR
    digest pull per PR would dominate PR runner minutes. A post-publish
    run in release.yml would test a coherent-by-construction pair — the
    scheduled run is what covers the between-publishes drift window."""
    triggers = load_workflow()[True]  # YAML parses `on:` as True
    assert "schedule" in triggers, "the gate must run on a weekly schedule"
    assert isinstance(triggers["schedule"], list) and triggers["schedule"]
    assert "workflow_dispatch" in triggers, (
        "the gate must support workflow_dispatch for on-demand repin "
        "verification after RBAC/VAP/CRD semantics changes"
    )
    assert "pull_request" not in triggers, (
        "the gate must not run on pull_request (issue #679: PR-minute guard)"
    )
    assert "push" not in triggers, (
        "the gate must not run on push — release.yml's post-publish moment "
        "tests a coherent-by-construction pair; keep the gate on the "
        "between-publishes drift window instead"
    )


# ---------------------------------------------------------------------------
# The job runs the script, bounded by a timeout.
# ---------------------------------------------------------------------------


def test_gate_job_runs_the_script_with_a_sane_timeout() -> None:
    """The single job exists, invokes scripts/compat-smoke-gate.sh, and is
    bounded by timeout-minutes: 10 — kind create + digest pull + the
    observe window + teardown is ~4-5 min nominal; 10 min bounds the worst
    case without burning runner minutes."""
    doc = load_workflow()
    job = doc["jobs"]["compat-smoke-gate"]
    assert job["timeout-minutes"] == 10, (
        "the gate job must carry timeout-minutes: 10 (issue #679)"
    )
    run_steps = [s for s in gate_steps(doc) if isinstance(s.get("run"), str)]
    assert any("scripts/compat-smoke-gate.sh" in s["run"] for s in run_steps), (
        "the gate job must run scripts/compat-smoke-gate.sh"
    )


def test_gate_job_checkout_is_sha_pinned() -> None:
    """The gate's only third-party action is the repo's standard SHA-pinned
    checkout (the #389 rule) — kind/kubectl install via pinned curl, not
    mutable-tag actions."""
    for step in gate_steps(load_workflow()):
        uses = step.get("uses")
        if isinstance(uses, str):
            assert uses == (
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
            ), f"unexpected/mutable action pin in the gate workflow: {uses}"


# ---------------------------------------------------------------------------
# Script shape: the two failure signatures + the pinned-digest rule.
# ---------------------------------------------------------------------------


def test_gate_script_asserts_both_failure_signatures() -> None:
    """The script carries the two regression greps from the issue's
    acceptance criterion: the `empty spec.redisUrl` warning (the #116
    fence must stay silent when the #463 secretRef resolves) and the
    main-resource PATCH 403/Forbidden shape (the #228 RBAC contract)."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "empty spec.redisUrl" in text, (
        "the gate must grep for the #116/#463 failure signature"
    )
    assert "403" in text and "orbidden" in text, (
        "the gate must grep for the #228 main-resource PATCH 403 signature"
    )


def test_gate_script_boots_the_committed_digest_never_overrides_it() -> None:
    """The script applies deploy/operator-deployment.yaml as committed and
    never overrides the image — the pinned digest is the thing under
    test. A `kind load`/`image:` override would silently test something
    else and re-open the exact skew #679 closed."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "kubectl apply -f \"$REPO_ROOT/deploy/operator-deployment.yaml\"" in text
    assert "set image" not in text, "the gate must not override the Deployment image"
    assert "kind load" not in text, (
        "the gate must not load a local image — the kubelet pulls the "
        "committed digest itself (same posture as scripts/kind-config.yaml)"
    )


def test_gate_script_tears_the_cluster_down() -> None:
    """Disposable by construction: the EXIT trap deletes the kind cluster
    unless COMPAT_SMOKE_KEEP_CLUSTER=1 (debug escape hatch)."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "trap teardown EXIT" in text
    assert "kind delete cluster" in text
