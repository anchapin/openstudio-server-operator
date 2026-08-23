"""Issue #588: cosign verification must be ordered and independent.

Three holes in the provenance chain, fenced here as YAML-structural
assertions over the two committed workflows (same shape as
tests/test_ci_workflow_hygiene.py — ``yaml.safe_load`` the workflow and
assert on the parsed structure, never on comment text):

* **(a) race.** ci.yml's ``cosign-verify-dev-image`` used to trigger on
  the same ``push`` to develop that release.yml handles, racing its
  ``Sign + attest`` step — it could verify (and fail against) the fresh
  unsigned digest or vacuously pass against the PREVIOUS image, and a
  release that failed before publishing still yielded a green
  verification against the stale artifact. The job is now
  ``workflow_run``-gated on the Release workflow's SUCCESSFUL
  completion, so it deterministically verifies the just-signed digest.
* **(b) pre-sign digest pin.** release.yml's digest-pin commit ran
  BEFORE ``cosign sign/attest`` — a sign failure left develop pinned to
  an unsigned, unverified digest. The pin step is now the LAST step of
  ``publish-dev``, after ``Sign + attest`` and an in-workflow
  digest-pinned verify (the same placement rationale as the #479/#481
  pre-pin content gates).
* **(c) tag blind spot.** ``v*`` tag images had only release.yml's
  in-workflow self-verification — the exact weakness issue #159 called
  out for ``:dev``. ci.yml now carries a push-tags-triggered
  ``cosign-verify-tag-image`` job that verifies the tag's attestation
  with the #169 release identity, in a separate workflow from the one
  that signed.

The #574 injection fence (github.* never inside ``run:``) and the #577
pinned-pip fence are enforced repo-wide by test_ci_workflow_hygiene.py;
the tests here additionally pin the env-indirection shape of the NEW
untrusted interpolations (``workflow_run`` context, ``ref_name``).
"""

from __future__ import annotations

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = ROOT / ".github" / "workflows"

DEV_IDENTITY = (
    "https://github.com/anchapin/openstudio-server-operator"
    "/.github/workflows/release.yml@refs/heads/develop"
)
TAG_IDENTITY = (
    "https://github.com/anchapin/openstudio-server-operator"
    "/.github/workflows/release.yml@refs/tags/v*"
)


def load_workflow(name: str) -> dict:
    doc = yaml.safe_load((WORKFLOWS_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


def job_steps(doc: dict, job_id: str) -> list[dict]:
    steps = doc["jobs"][job_id]["steps"]
    assert isinstance(steps, list)
    return steps


def step_index(steps: list[dict], step_name: str) -> int:
    """Index of the step whose ``name:`` starts with ``step_name``.

    Prefix match because the workflow's step names embed bare ``#NNN``
    issue refs — unquoted YAML scalars, where `` #`` starts a comment,
    so the parsed name truncates right before the issue number.
    """
    for index, step in enumerate(steps):
        name = step.get("name")
        if isinstance(name, str) and name.startswith(step_name):
            return index
    raise AssertionError(f"no step named {step_name!r} in {[s.get('name') for s in steps]}")


def step_by_name(steps: list[dict], step_name: str) -> dict:
    return steps[step_index(steps, step_name)]


# ---------------------------------------------------------------------------
# Hole (b): the digest-pin commit is the LAST step of publish-dev, after
# sign + the digest-pinned in-workflow verify.
# ---------------------------------------------------------------------------


def test_publish_dev_pins_after_sign_and_verify() -> None:
    """Indices prove the #588 order: Sign + attest → Verify provenance →
    Pin — a deliberately-failed sign fails the job before the pin step,
    so no digest-pin commit can be produced for an unsigned image."""
    steps = job_steps(load_workflow("release.yml"), "publish-dev")
    sign_at = step_index(steps, "Sign + attest the dev image")
    verify_at = step_index(steps, "Verify provenance (digest-pinned)")
    pin_at = step_index(steps, "Pin operator image by SHA256 digest")
    assert sign_at < verify_at < pin_at
    # the pre-pin content gates keep their historical placement (#479/#481)
    assert step_index(steps, "Assert no dev deps in the runtime image") < sign_at
    assert step_index(steps, "Trivy scan the pushed image") < sign_at
    # ...and the pin step is the last step of the job, period.
    assert pin_at == len(steps) - 1


def test_publish_dev_verify_targets_signed_digest_not_tag() -> None:
    """The in-workflow verify proves THIS build's artifact: it reads the
    build step's digest and verifies ``@${DIGEST}`` under the #169
    develop identity — never the mutable ``:dev`` tag (which during a
    rerun race could resolve to a stale artifact)."""
    steps = job_steps(load_workflow("release.yml"), "publish-dev")
    run = step_by_name(steps, "Verify provenance (digest-pinned)")["run"]
    assert 'steps.build.outputs.digest' in run
    assert 'operator@${DIGEST}' in run
    assert ':dev"' not in run and ":dev'" not in run
    assert DEV_IDENTITY in run
    assert "--type slsaprovenance" in run


def test_publish_dev_pin_step_preserves_output_contract() -> None:
    """The relocated pin step keeps ``id: pin`` and the job-level
    ``image-digest`` output still points at it — the #149 contract
    downstream tooling reads; reordering must not silently drop it."""
    doc = load_workflow("release.yml")
    steps = job_steps(doc, "publish-dev")
    pin = step_by_name(steps, "Pin operator image by SHA256 digest")
    assert pin.get("id") == "pin"
    assert doc["jobs"]["publish-dev"]["outputs"]["image-digest"] == (
        "${{ steps.pin.outputs.image-digest }}"
    )


# ---------------------------------------------------------------------------
# Hole (a): ci.yml's :dev verifier is workflow_run-gated on the Release
# workflow's successful completion.
# ---------------------------------------------------------------------------


def test_ci_triggers_include_release_workflow_run_gate() -> None:
    """ci.yml's ``on:`` carries a ``workflow_run`` trigger for the Release
    workflow (completed) — the mechanism that ends the same-push race —
    plus the tag push filter for hole (c). The historical push/PR
    triggers (guard-branch-pairing, lint, test) are untouched."""
    triggers = load_workflow("ci.yml")[True]  # YAML parses `on:` as True
    assert "Release" in triggers["workflow_run"]["workflows"]
    assert "completed" in triggers["workflow_run"]["types"]
    assert triggers["push"]["branches"] == ["develop"]
    assert "v*" in triggers["push"]["tags"]
    assert triggers["pull_request"]["branches"] == ["develop", "main"]


def test_ci_dev_verifier_is_workflow_run_gated() -> None:
    """The verifier's ``if:`` requires a workflow_run event from the
    Release workflow, on the develop branch, with a SUCCESSFUL
    conclusion — a release whose sign step failed never produces a green
    verification against the stale artifact."""
    doc = load_workflow("ci.yml")
    job = doc["jobs"]["cosign-verify-dev-image"]
    condition = job["if"]
    assert "github.event_name == 'workflow_run'" in condition
    assert "github.event.workflow_run.name == 'Release'" in condition
    assert "github.event.workflow_run.head_branch == 'develop'" in condition
    assert "github.event.workflow_run.conclusion == 'success'" in condition
    # the OLD same-push shape (pre-#588) is gone for good
    assert "github.ref == 'refs/heads/develop'" not in condition


def test_ci_dev_verifier_verifies_resolved_digest() -> None:
    """The gated verifier resolves the ``:dev`` digest from the registry
    descriptor (#459) and verifies ``@${DIGEST}`` under the pinned
    develop identity — the same digest the Release run just signed."""
    run = None
    for step in job_steps(load_workflow("ci.yml"), "cosign-verify-dev-image"):
        candidate = step.get("run")
        if isinstance(candidate, str) and "verify-attestation" in candidate:
            run = candidate
            break
    assert run is not None, "cosign-verify-dev-image has no verify-attestation step"
    assert "imagetools inspect" in run
    assert "operator@${DIGEST}" in run
    assert DEV_IDENTITY in run


def test_ci_verifiers_use_env_indirection_for_untrusted_values() -> None:
    """The new untrusted interpolations never touch ``run:`` directly
    (#574, fenced repo-wide by test_ci_workflow_hygiene.py): the
    workflow_run context arrives via RELEASE_RUN_ID/RELEASE_HEAD_SHA and
    the tag name via TAG_NAME, each declared in ``env:`` and read as
    ``"$VAR"`` in the script."""
    doc = load_workflow("ci.yml")
    dev_steps = job_steps(doc, "cosign-verify-dev-image")
    dev_verify = next(s for s in dev_steps if "verify-attestation" in str(s.get("run", "")))
    assert dev_verify["env"]["RELEASE_RUN_ID"] == "${{ github.event.workflow_run.id }}"
    assert dev_verify["env"]["RELEASE_HEAD_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    assert "${RELEASE_RUN_ID}" in dev_verify["run"] and "${RELEASE_HEAD_SHA}" in dev_verify["run"]

    tag_steps = job_steps(doc, "cosign-verify-tag-image")
    tag_verify = next(s for s in tag_steps if "verify-attestation" in str(s.get("run", "")))
    assert tag_verify["env"]["TAG_NAME"] == "${{ github.ref_name }}"
    assert "${TAG_NAME}" in tag_verify["run"]


# ---------------------------------------------------------------------------
# Hole (c): the independent v* tag verification job.
# ---------------------------------------------------------------------------


def test_ci_tag_verify_job_exists_and_targets_release_identity() -> None:
    """A push-tags-triggered job verifies the tagged image's attestation
    with the #169 tag identity (release.yml@refs/tags/v*) in a SEPARATE
    workflow from the one that signed — closing the #159 self-verification
    blind spot on the release path."""
    doc = load_workflow("ci.yml")
    job = doc["jobs"]["cosign-verify-tag-image"]
    assert "startsWith(github.ref, 'refs/tags/v')" in job["if"]
    assert "github.event_name == 'push'" in job["if"]
    run = next(
        s["run"] for s in job["steps"] if "verify-attestation" in str(s.get("run", ""))
    )
    assert TAG_IDENTITY in run
    assert "operator@${DIGEST}" in run
    # the Release workflow signs the tag concurrently: the job must wait
    # (bounded retry), never vacuously pass or transiently fail on the
    # unsigned fresh digest — absent provenance exhausts the loop.
    assert "seq 1 30" in run
    assert "exit 1" in run


def test_ci_core_jobs_do_not_rerun_on_workflow_run() -> None:
    """lint/test/audit skip workflow_run events: the trigger exists solely
    for the provenance verifiers, and re-running the full CI surface on
    every Release completion would double the runs on every develop push
    (on top of the pin-commit push the release itself makes)."""
    doc = load_workflow("ci.yml")
    for job_id in ("lint", "test", "audit"):
        assert doc["jobs"][job_id].get("if") == (
            "github.event_name != 'workflow_run'"
        ), f"ci.yml job '{job_id}' must skip workflow_run events (issue #588)"
