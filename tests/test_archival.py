"""Unit tests for the ephemeral rclone archival Job manifest generator (issue #15).

Golden files live in ``tests/golden/archival_job_{s3,gcs,azure}.json``.
Regenerate them after an intentional manifest change with:
``REGENERATE_GOLDEN=1 .venv/bin/pytest tests/test_archival.py``
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from openstudio_operator.archival import (
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
