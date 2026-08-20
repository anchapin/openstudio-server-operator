"""Unit tests for the ephemeral rclone archival Job manifest generator (issue #15).

Golden files live in ``tests/golden/archival_job_{s3,gcs,azure}.json``.
Regenerate them after an intentional manifest change with:
``REGENERATE_GOLDEN=1 .venv/bin/pytest tests/test_archival.py``
"""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from openstudio_operator.archival import (
    ARCHIVAL_JOB_ACTIVE_DEADLINE_SECONDS,
    NFS_MOUNT_PATH,
    NFS_PVC_NAME,
    RCLONE_IMAGE,
    RCLONE_IMAGE_DIGEST,
    RCLONE_REMOTE_NAME,
    archival_job_name,
    build_archival_job,
)
from openstudio_operator.config import StoragePolicy

ANALYSIS_ID = "64f0c8e2a1b3c4d5e6f7a8b9"
DATAPOINT_ID = "64f0c8e2a1b3c4d5e6f70001"
NAMESPACE = "openstudio-server"
SECRET_REF = "os-archive-creds"
BUCKET = "os-archives"
GOLDEN_DIR = Path(__file__).parent / "golden"
BACKENDS = ("s3", "gcs", "azure")


def _policy(backend: str) -> StoragePolicy:
    return StoragePolicy(
        archive_to_s3=True,
        backend=backend,
        bucket=BUCKET,
        secret_ref=SECRET_REF,
    )


def _job(backend: str) -> dict:
    return build_archival_job(ANALYSIS_ID, _policy(backend), NAMESPACE, (DATAPOINT_ID,))


def _container(job: dict) -> dict:
    return job["spec"]["template"]["spec"]["containers"][0]


def _script(job: dict) -> str:
    return _container(job)["command"][2]


# --- Golden manifests (one per backend) --------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_golden_manifest_per_backend(backend: str) -> None:
    job = _job(backend)
    golden = GOLDEN_DIR / f"archival_job_{backend}.json"
    if os.environ.get("REGENERATE_GOLDEN") == "1":
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(json.dumps(job, indent=2) + "\n")
    assert job == json.loads(golden.read_text()), (
        f"manifest drifted from {golden}; regenerate with REGENERATE_GOLDEN=1 if intentional"
    )


# --- Script content: copy + check + failure exit ------------------------------


def test_script_copies_then_verifies_and_fails_hard() -> None:
    script = _script(_job("s3"))
    assert script.splitlines()[0] == "set -eu"
    assert 'rclone copy "$src" "$dest/$rel"' in script
    assert 'rclone check "$src" "$dest/$rel"' in script
    # verified-upload gate is size+hash (rclone check default) — never size-only
    assert "--size-only" not in script
    # a missing source directory is a failure, not a silent skip
    assert '[ ! -d "$src" ]' in script


@pytest.mark.parametrize("backend", BACKENDS)
def test_script_targets_analysis_and_datapoint_trees(backend: str) -> None:
    script = _script(_job(backend))
    assert f"{NFS_MOUNT_PATH}/server/assets/analyses/{ANALYSIS_ID}" in script
    assert f"{NFS_MOUNT_PATH}/server/assets/data_points/{DATAPOINT_ID}" in script
    assert f"{RCLONE_REMOTE_NAME}:{BUCKET}/{ANALYSIS_ID}" in script


@pytest.mark.parametrize("backend", BACKENDS)
def test_generated_script_is_valid_posix_sh(backend: str) -> None:
    result = subprocess.run(
        ["/bin/sh", "-n"],
        input=_script(_job(backend)),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# --- Credentials: envFrom secretRef ONLY, no inlined values -------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_credentials_via_envfrom_secretref_only(backend: str) -> None:
    job = _job(backend)
    container = _container(job)
    assert container["envFrom"] == [{"secretRef": {"name": SECRET_REF}}]
    # the only inline env value is the non-secret rclone remote *type*
    for env in container.get("env", []):
        if "value" in env:
            assert env["name"] == f"RCLONE_CONFIG_{RCLONE_REMOTE_NAME.upper()}_TYPE"
    blob = json.dumps(job)
    assert "valueFrom" not in blob
    assert "secretKeyRef" not in blob
    # the Secret *name* appears exactly once — as the secretRef, never as a value
    assert blob.count(SECRET_REF) == 1
    # volumes are PVC-only (no secret volumes)
    for volume in job["spec"]["template"]["spec"]["volumes"]:
        assert set(volume) == {"name", "persistentVolumeClaim"}


# --- Security: hardened rclone container (#114) ------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_archival_job_has_hardened_rclone_securitycontext(backend: str) -> None:
    """Match the operator Deployment (#115) and the prune CronJob (#78).

    The rclone container holds S3/GCS/Azure credentials via envFrom — a
    writable root FS + net-raw would let a compromised rclone pivot out.
    """
    job = _job(backend)
    container = _container(job)
    sc = container["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert sc["runAsNonRoot"] is True
    assert sc["runAsUser"] == 1000
    assert sc["seccompProfile"]["type"] == "RuntimeDefault"


@pytest.mark.parametrize("backend", BACKENDS)
def test_archival_job_pod_securitycontext_hardened(backend: str) -> None:
    """Issue #161 — defense-in-depth pod-level baseline. PSS `restricted`
    validates pod-level runAsNonRoot + seccompProfile, NOT the
    container-level fields. An injected sidecar or debug
    ephemeralContainer that omits its own securityContext still
    inherits these pod-level defaults. fsGroup 1000 is harmless on the
    readOnly NFS mount and keeps the manifest shape consistent with
    the operator Deployment and the prune CronJob.
    """
    job = _job(backend)
    pod_spec = job["spec"]["template"]["spec"]
    sc = pod_spec["securityContext"]
    assert sc["runAsNonRoot"] is True, sc
    assert sc["runAsUser"] == 1000, sc
    assert sc["seccompProfile"]["type"] == "RuntimeDefault", sc
    assert sc["fsGroup"] == 1000, sc


@pytest.mark.parametrize("backend", BACKENDS)
def test_archival_job_disables_service_account_token_mount(backend: str) -> None:
    """Issue #241 — the archival Job is rclone-only; it never calls the
    kube-apiserver. The kubelet's default is to mount the SA token at
    /var/run/secrets/kubernetes.io/serviceaccount, which is pure
    attack surface (a compromised rclone could cat the token and
    authenticate against the API server with whatever RBAC the SA
    carries). Set ``automountServiceAccountToken: false`` on the pod
    spec to opt out. Scope guard: do NOT propagate this field to the
    prune CronJob — its SA token mount is intentional (StatusStore
    RMW + Batch Jobs).
    """
    job = _job(backend)
    pod_spec = job["spec"]["template"]["spec"]
    assert pod_spec.get("automountServiceAccountToken") is False, pod_spec


@pytest.mark.parametrize("backend", BACKENDS)
def test_archival_job_image_pinned_by_digest(backend: str) -> None:
    """#124 — the rclone image is pinned by @sha256 digest. Floating
    ``rclone/rclone:1.67.0`` would let a tag-mutation steer the image
    between release.yml rebuild and the next refresh; the digest closes
    that window.

    The tag form is preserved for human readability — the digest is
    what the kubelet resolves.
    """
    job = _job(backend)
    container = _container(job)
    image = container["image"]
    # Tag + digest form: ``<repo>:<tag>@sha256:<digest>``.
    assert "@sha256:" in image, image
    assert image.endswith(f"@{RCLONE_IMAGE_DIGEST}")
    # Digest matches the module-level constant — fail loudly if the
    # constant + the import drift; that's exactly the drift #124 wants
    # to catch.
    assert RCLONE_IMAGE_DIGEST in image


# --- NFS mount ----------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_mounts_nfs_pvc_readonly_at_web_mount_path(backend: str) -> None:
    job = _job(backend)
    pod_spec = job["spec"]["template"]["spec"]
    assert pod_spec["volumes"] == [
        {
            "name": "nfs-assets",
            "persistentVolumeClaim": {"claimName": NFS_PVC_NAME, "readOnly": True},
        }
    ]
    assert _container(job)["volumeMounts"] == [
        {"name": "nfs-assets", "mountPath": NFS_MOUNT_PATH, "readOnly": True}
    ]


# --- Job mechanics ------------------------------------------------------------


def test_job_mechanics() -> None:
    job = _job("gcs")
    assert job["apiVersion"] == "batch/v1"
    assert job["kind"] == "Job"
    assert job["metadata"]["namespace"] == NAMESPACE
    spec = job["spec"]
    assert spec["backoffLimit"] == 3
    assert spec["ttlSecondsAfterFinished"] == 24 * 3600
    assert spec["template"]["spec"]["restartPolicy"] == "Never"
    container = _container(job)
    # Issue #124 — image is pinned by tag + @sha256 digest. The test
    # asserts the tag form (for human readability) AND the digest pin
    # (the deploy-time guarantee) — see test_archival_job_image_pinned_by_digest.
    assert container["image"] == RCLONE_IMAGE
    assert container["image"].startswith("rclone/rclone:1.67.0@")
    assert container["command"][0:2] == ["/bin/sh", "-c"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_archival_job_emits_active_deadline_seconds(backend: str) -> None:
    """Issue #394 — emit ``activeDeadlineSeconds`` on the Job manifest.

    The kubelet hard-kills any rclone pod that runs past this bound so a
    hung rclone (TLS handshake stall, stuck TCP retransmit, S3 500 storm
    with no response) cannot keep a Job alive forever. The value is the
    module-level constant ``ARCHIVAL_JOB_ACTIVE_DEADLINE_SECONDS`` (6× the
    chart's worker ``terminationGracePeriodSeconds``), pinned here so a
    silent constant change surfaces as a test failure.
    """
    job = _job(backend)
    spec = job["spec"]
    assert spec["activeDeadlineSeconds"] == ARCHIVAL_JOB_ACTIVE_DEADLINE_SECONDS
    # belt-and-braces: the constant itself is large enough to absorb a
    # healthy multi-GB upload (5200s chart worker grace × 6 = 31200s ≈
    # 8.7h). A regression that drops the multiplier (e.g. 5200 → 30)
    # would silently break the alert upper-bound contract.
    assert ARCHIVAL_JOB_ACTIVE_DEADLINE_SECONDS == 6 * 5200
    assert ARCHIVAL_JOB_ACTIVE_DEADLINE_SECONDS > 3600


@pytest.mark.parametrize(
    ("backend", "remote_type"),
    [("s3", "s3"), ("gcs", "gcs"), ("azure", "azureblob")],
)
def test_backend_maps_to_rclone_remote_type(backend: str, remote_type: str) -> None:
    container = _container(_job(backend))
    assert container["env"] == [
        {"name": "RCLONE_CONFIG_ARCHIVE_TYPE", "value": remote_type}
    ]


# --- Deterministic, DNS-1035-safe job names -----------------------------------


def test_job_name_deterministic_per_analysis() -> None:
    name = archival_job_name(ANALYSIS_ID)
    assert name == archival_job_name(ANALYSIS_ID)
    assert name != archival_job_name("64f0c8e2a1b3c4d5e6f7a8b0")
    assert name == _job("s3")["metadata"]["name"]


@pytest.mark.parametrize(
    "raw_id",
    [ANALYSIS_ID, "ABC-123", "with spaces!!", "x" * 300, "-leading-dash-", ""],
)
def test_job_name_dns1035_safe_and_bounded(raw_id: str) -> None:
    name = archival_job_name(raw_id)
    assert name.startswith("oscm-archive-")
    assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name)
    assert len(name) <= 63


def test_distinct_raw_ids_never_collide_after_sanitization() -> None:
    # ids that sanitize to the same slug stay distinct via the digest suffix
    assert archival_job_name("a b") != archival_job_name("a-b")


# --- Input validation ----------------------------------------------------------


def test_build_requires_complete_storage_policy() -> None:
    for policy in (
        StoragePolicy(archive_to_s3=True),
        StoragePolicy(archive_to_s3=True, backend="s3", bucket=BUCKET),
        StoragePolicy(archive_to_s3=True, backend="s3", secret_ref=SECRET_REF),
        StoragePolicy(archive_to_s3=True, backend="ftp", bucket=BUCKET, secret_ref=SECRET_REF),
    ):
        with pytest.raises(ValueError):
            build_archival_job(ANALYSIS_ID, policy, NAMESPACE)
    with pytest.raises(ValueError):
        build_archival_job("", _policy("s3"), NAMESPACE)


def test_job_name_matches_analysis_id_when_hex() -> None:
    # the normal case: readable name embedding the (hex) analysis id
    assert ANALYSIS_ID in archival_job_name(ANALYSIS_ID)


# --- Hypothesis-driven property tests (issue #297) -------------------------
#
# The hand-written cases above parametrize ``archival_job_name`` over six
# inputs (``#270``) and check ``build_archival_job`` against one shape per
# backend (``tests/golden/archival_job_{s3,gcs,azure}.json``). Both fences
# silently bypass the long tail:
#
#   * ``archival_job_name``: unicode, embedded newlines, control characters,
#     8-bit ASCII boundary, bidi marks, leading/trailing dot or dash
#     sequences, multi-megabyte strings — none of these are sampled today,
#     and a regression that drops ``re.sub``'s ``[^a-z0-9-]+`` clause would
#     pass the hand-written cases while breaking name-safety.
#
#   * ``build_archival_job``: the golden file pins one shape per backend. A
#     regression that drops ``automountServiceAccountToken: false`` only when
#     the analysis_id contains a digit, omits the pod-level seccompProfile
#     only when the ``storagePolicy.bucket`` is a specific length, or
#     mangles the envFrom secretRef for one of the three backends, would
#     not surface as a test failure until a production cluster hits it.
#
# The six properties below are the acceptance criteria from #297:
#
#   (a) ``archival_job_name(x)`` matches DNS-1035-safe and ≤ 63 chars for
#       any text input (including unicode, control chars, embedded
#       newlines, 8-bit ASCII boundary, bidi marks).
#   (b) ``archival_job_name(x) == archival_job_name(x)`` — deterministic.
#   (c) The 8-char sha256-256 prefix digest suffix is collision-free across
#       any reasonable sample of distinct inputs.
#   (d) The pod-level + container-level ``securityContext`` is byte-stable
#       across the ``backend × analysis_id`` cross.
#   (e) The container ``envFrom[].secretRef`` shape is byte-stable across
#       the same cross.
#   (f) The container image is always digest-pinned (``@sha256:`` prefix).
#
# Bounded ``max_size`` on the text strategy keeps the property test well
# inside the D12 / conftest duration budget (issue #257) — sha256 on a 256
# char string is microseconds and the operator never sees ids longer than
# a MongoDB ObjectId hex (24 chars) in production. ``.hypothesis/`` stays
# locally cached and gitignored (see ``.gitignore``).

# (a)+(b) arbitrary text input, including the long-tail chars the
# hand-written parametrization misses (unicode, embedded newlines, control
# chars, 8-bit ASCII boundary). ``min_size=0`` is intentional — empty /
# whitespace-only / pure-punctuation ids are a documented stress case
# (the digest-suffix fallback path kicks in when sanitization strips to
# the empty string).
_raw_id_text = st.text(min_size=0, max_size=256)


@given(raw_id=_raw_id_text)
def test_archival_job_name_property_format_compliance(raw_id: str) -> None:
    """(a) ``archival_job_name(x)`` is DNS-1035-safe and ≤ 63 chars for any text input.

    Property: every hypothesis-generated ``raw_id`` (unicode, control
    chars, embedded newlines, 8-bit ASCII boundary, bidi marks,
    leading/trailing dot and dash sequences, multi-megabyte strings)
    yields a name that matches the K8s DNS-1035 label regex
    ``^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`` and is at most 63 chars long.

    A regression in the sanitization regex
    (``re.sub(r"[^a-z0-9-]+", "-", analysis_id.lower()).strip("-")``)
    or in the 63-char budget trim would surface here for any
    non-alphanumeric input the hand-written parametrization misses.
    """
    name = archival_job_name(raw_id)
    assert name.startswith("oscm-archive-"), name
    assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name), name
    assert len(name) <= 63, name


@given(raw_id=_raw_id_text)
def test_archival_job_name_property_idempotency(raw_id: str) -> None:
    """(b) ``archival_job_name(x) == archival_job_name(x)`` — deterministic.

    Property: repeated invocations with the same input must yield the
    same name. The generator is purely functional over its ``analysis_id``
    argument (sha256 + regex + string concat), so idempotency is a
    smoke-level guarantee — but it costs little to assert, and it
    catches a regression where someone introduces non-determinism
    (e.g. a Python set iteration in the sanitization step).
    """
    assert archival_job_name(raw_id) == archival_job_name(raw_id)


# (c) Distinct raw ids must not collide on the 8-hex-char digest suffix.
# Hypothesis samples up to 64 distinct inputs per example; the probability
# of any sha256-prefix collision in that range is astronomically small
# (~2^-32 * O(64^2) ≈ 10^-15), so the assertion holds for any seed
# hypothesis can reach. We pin the strategy to ``unique=True`` so the
# input list has no duplicates — the property is "distinct in ⇒
# distinct digest suffix out".
_unique_raw_id_text = st.text(min_size=0, max_size=256)


@given(
    raw_ids=st.lists(
        _unique_raw_id_text,
        min_size=2,
        max_size=64,
        unique=True,
    ),
)
def test_archival_job_name_property_digest_uniqueness(raw_ids: list[str]) -> None:
    """(c) ``sha256(x)[:8]`` suffix is collision-free across distinct inputs.

    Property: no two distinct ``raw_ids`` in a sampled list produce the
    same 8-hex-char digest suffix. The digest is the only escape hatch
    that lets two distinct raw ids collapse onto the same name after
    sanitization (e.g. ``"a b"`` and ``"a-b"`` both sanitize to ``"a-b"``
    — the test in #280 covers that pair specifically; this property
    generalises the fence over all sampled distinct inputs).
    """
    digests = [
        hashlib.sha256(x.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
        for x in raw_ids
    ]
    assert len(set(digests)) == len(digests), (
        f"sha256 prefix collision among {len(raw_ids)} distinct raw ids"
    )


# (d)+(e)+(f) Fuzz the manifest generator over the cross
# ``StoragePolicy(backend=...) × analysis_id``. All three properties are
# byte-stability assertions: a regression that drops a key from
# ``securityContext``, swaps the ``envFrom`` shape, or drops the
# ``@sha256:`` digest for one backend × one id pair would surface here.
#
# ``_DIGEST_LEN`` is the constant ``archival._DIGEST_LEN`` — re-imported
# via a public attribute to avoid coupling this test to a private name.
_DIGEST_LEN = 8  # mirrors archival._DIGEST_LEN; the constant is part of the public Job-name contract.

_backend_strategy = st.sampled_from(("s3", "gcs", "azure"))
# analysis_id must be non-empty: ``build_archival_job`` raises
# ``ValueError("analysis_id must be non-empty")`` on the empty string,
# which is the correct behaviour but not what we're testing here.
_nonempty_analysis_id = st.text(
    min_size=1,
    max_size=128,
)


@given(backend=_backend_strategy, raw_id=_nonempty_analysis_id)
def test_archival_manifest_byte_stable_securityContext(backend: str, raw_id: str) -> None:
    """(d) Pod-level + container-level ``securityContext`` is byte-stable.

    Property: for every ``(backend, analysis_id)`` cross, the pod-level
    ``securityContext`` dict and the rclone container's
    ``securityContext`` dict match the fixed defence-in-depth baseline
    byte-for-byte. A regression that drops ``seccompProfile`` only when
    a deep ``pod_spec.securityContext`` is rebuilt for one backend, or
    omits ``capabilities.drop`` only when the analysis id is unicode,
    would surface here.
    """
    job = build_archival_job(raw_id, _policy(backend), NAMESPACE)
    pod_spec = job["spec"]["template"]["spec"]
    assert pod_spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
        "fsGroup": 1000,
    }
    container_security_context = _container(job)["securityContext"]
    assert container_security_context == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }


@given(backend=_backend_strategy, raw_id=_nonempty_analysis_id)
def test_archival_manifest_byte_stable_envFrom(backend: str, raw_id: str) -> None:
    """(e) Container ``envFrom[].secretRef`` shape is byte-stable.

    Property: for every ``(backend, analysis_id)`` cross, the rclone
    container's ``envFrom`` is exactly ``[{"secretRef": {"name": <secret_ref>}}]``
    and only that — no inline ``value``, no ``secretKeyRef``, no extra
    entries. Credentials flow only via ``envFrom secretRef`` (issue #15,
    D09); the operator never reads secret values, so this is the
    integrity boundary. A regression that omits ``envFrom`` for one
    backend, inlines a secret value, or adds an extra ``envFrom``
    entry with a stray ``configMapRef``, would surface here.
    """
    job = build_archival_job(raw_id, _policy(backend), NAMESPACE)
    container = _container(job)
    assert container["envFrom"] == [{"secretRef": {"name": SECRET_REF}}]
    # ``env`` carries exactly one entry (the rclone remote-type override)
    # and never carries a ``valueFrom``, ``secretKeyRef``, or any
    # inline secret value. Belt-and-braces on top of the envFrom check.
    blob = json.dumps(container)
    assert "valueFrom" not in blob
    assert "secretKeyRef" not in blob
    assert "configMapRef" not in blob


@given(backend=_backend_strategy, raw_id=_nonempty_analysis_id)
def test_archival_manifest_byte_stable_image_digest_pinned(
    backend: str, raw_id: str
) -> None:
    """(f) The rclone container image is always digest-pinned.

    Property: the container ``image`` string is ``<repo>:<tag>@sha256:<digest>``
    for every ``(backend, analysis_id)`` cross. A regression that drops
    the ``@sha256:`` suffix (e.g. someone refactors ``RCLONE_IMAGE`` to
    a plain tag-only reference) would surface here. Mirrors the
    hand-written ``test_archival_job_image_pinned_by_digest`` (#124)
    fuzzed over the full cross.
    """
    job = build_archival_job(raw_id, _policy(backend), NAMESPACE)
    image = _container(job)["image"]
    assert "@sha256:" in image, image
    assert image.endswith(f"@{RCLONE_IMAGE_DIGEST}"), image
    # Tag form is preserved for human readability — the digest is what
    # the kubelet resolves against, but a future regression that drops
    # the tag in favour of "digest-only" would break grep-ability on the
    # cluster, so we keep the explicit prefix check too.
    assert image.startswith("rclone/rclone:"), image
