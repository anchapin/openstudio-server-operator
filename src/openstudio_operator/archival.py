"""Ephemeral rclone archival Job generator (Module 4 part 1 — issue #15, decision D09).

Pure manifest generation with **no caller yet**: the storage-pruner
orchestration (retention gating, Job spawn, verified-upload-then-
``DELETE /analyses/{id}`` pipeline) is issue #16.

Design (D09):
- One Kubernetes Job per archived analysis. The Job mounts the helm chart's
  NFS PVC (``nfs-pvc`` — the same ``/mnt/openstudio`` mount path the ``web``
  Deployment uses) **read-only** and copies the analysis's asset tree
  (``server/assets/analyses/{id}`` plus optional ``server/assets/data_points/{dp}``
  trees) to object storage with rclone, then verifies each tree with
  ``rclone check`` (size + hash, rclone's default). ``set -eu`` makes ANY
  failure — missing source directory, failed copy, any check mismatch — exit
  nonzero, so a Completed Job is the "verified upload" gate; only then may the
  caller trigger the server-side delete.
- Backend-agnostic: the only backend knowledge here is the rclone remote
  *type* mapping (``s3``→``s3``, ``gcs``→``gcs``, ``azure``→``azureblob``)
  plus the destination path ``archive:{bucket}/{analysis_id}``. All
  credentials flow exclusively from the Secret named by
  ``storagePolicy.secretRef`` via ``envFrom`` — the operator never reads
  secret values (and holds no secrets RBAC). The Secret is expected to carry
  whatever rclone env vars the chosen backend needs; rclone's uniform
  ``RCLONE_CONFIG_ARCHIVE_<OPTION>`` convention covers every backend (e.g.
  ``RCLONE_CONFIG_ARCHIVE_ACCESS_KEY_ID`` for s3,
  ``RCLONE_CONFIG_ARCHIVE_SERVICE_ACCOUNT_CREDENTIALS`` for gcs,
  ``RCLONE_CONFIG_ARCHIVE_ACCOUNT`` / ``RCLONE_CONFIG_ARCHIVE_KEY`` for
  azureblob).
- Idempotent spawn: the Job name is a pure function of the analysis id
  (DNS-1035-safe sanitized id + sha256-8 digest suffix), so regenerating the
  manifest for the same analysis yields the same name and a re-apply is a
  no-op; distinct ids never collapse onto the same name.

Transport-mechanics constants (documented module constants, not CR policy —
no CRD field exists for these and none is invented here; issue #4 owns the
CRD surface):
- ``RESTART_POLICY = "Never"`` with Job-level ``ARCHIVAL_BACKOFF_LIMIT = 3``:
  Never keeps a failed pod's logs available for forensics instead of
  retrying in place; retries happen as fresh pods under the Job's backoff
  budget, which caps total attempts at 4.
- ``TTL_SECONDS_AFTER_FINISHED = 86400``: finished Jobs self-delete after
  24h (the "ephemeral" half) while leaving a post-mortem window for failed
  uploads.
- ``RCLONE_IMAGE``: rclone release pinned by tag — never ``latest``.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from collections.abc import Sequence

from openstudio_operator.config import StoragePolicy

# --- Managed-object identifiers (AGENTS.md "fixed identifiers") -------------

NFS_PVC_NAME = "nfs-pvc"
NFS_MOUNT_PATH = "/mnt/openstudio"
ASSET_ROOT = "server/assets"

# --- rclone transport mechanics (documented constants, not CR policy) -------

RCLONE_IMAGE = "rclone/rclone:1.67.0"
RCLONE_REMOTE_NAME = "archive"
RESTART_POLICY = "Never"
ARCHIVAL_BACKOFF_LIMIT = 3
TTL_SECONDS_AFTER_FINISHED = 24 * 3600

# CRD storagePolicy.backend enum → rclone remote type. The only backend
# knowledge in this module.
_RCLONE_BACKEND_TYPES = {"s3": "s3", "gcs": "gcs", "azure": "azureblob"}

# DNS-1035 label constraints for the generated Job name (single label ≤ 63
# chars is valid for every K8s object-name style).
_NAME_PREFIX = "oscm-archive-"
_MAX_NAME_LEN = 63
_DIGEST_LEN = 8


def archival_job_name(analysis_id: str) -> str:
    """Deterministic, DNS-1035-safe Job name for an analysis id.

    ``oscm-archive-<sanitized-id>-<sha256-8>``: same id → same name (idempotent
    spawn); the digest suffix keeps distinct raw ids from colliding after
    sanitization.
    """
    digest = hashlib.sha256(analysis_id.encode("utf-8")).hexdigest()[:_DIGEST_LEN]
    sanitized = re.sub(r"[^a-z0-9-]+", "-", analysis_id.lower()).strip("-")
    if not sanitized:
        return f"{_NAME_PREFIX}{digest}"
    budget = _MAX_NAME_LEN - len(_NAME_PREFIX) - _DIGEST_LEN - 1
    sanitized = sanitized[:budget].rstrip("-")
    return f"{_NAME_PREFIX}{sanitized}-{digest}"


def _archive_script(analysis_id: str, bucket: str, datapoint_ids: Sequence[str]) -> str:
    """POSIX sh script: rclone copy each asset tree, then rclone check (size+hash).

    ``set -eu`` aborts the script — and therefore the Job, nonzero — on any
    missing source directory, failed copy, or check mismatch.
    """
    dest_root = f"{RCLONE_REMOTE_NAME}:{bucket}/{analysis_id}"
    sources: list[tuple[str, str]] = [
        (f"{NFS_MOUNT_PATH}/{ASSET_ROOT}/analyses/{analysis_id}", f"analyses/{analysis_id}"),
    ]
    sources.extend(
        (f"{NFS_MOUNT_PATH}/{ASSET_ROOT}/data_points/{dp_id}", f"data_points/{dp_id}")
        for dp_id in datapoint_ids
    )

    lines = [
        "set -eu",
        "",
        "# Generated by openstudio-server-operator (issue #15, D09) — do not edit.",
        "# Copies one analysis's NFS asset trees with rclone, then verifies each",
        "# with `rclone check` (size + hash). ANY mismatch exits nonzero: a",
        "# Completed Job is the verified-upload gate.",
        f"dest={shlex.quote(dest_root)}",
        "",
        "copy_check() {",
        '    src="$1"',
        '    rel="$2"',
        '    if [ ! -d "$src" ]; then',
        '        echo "archival: ERROR: source directory missing on NFS: $src" >&2',
        "        return 1",
        "    fi",
        '    echo "archival: copy $src -> $dest/$rel"',
        '    rclone copy "$src" "$dest/$rel"',
        '    echo "archival: verify (size+hash) $src <=> $dest/$rel"',
        '    rclone check "$src" "$dest/$rel"',
        "}",
        "",
    ]
    for src, rel in sources:
        lines.append(f"copy_check {shlex.quote(src)} {shlex.quote(rel)}")
    lines += ["", 'echo "archival: all asset trees copied and verified (size+hash)"', ""]
    return "\n".join(lines)


def build_archival_job(
    analysis_id: str,
    storage_policy: StoragePolicy,
    namespace: str,
    datapoint_ids: Sequence[str] = (),
) -> dict:
    """Build the ephemeral rclone archival Job manifest for one analysis.

    Returns a ``batch/v1`` Job dict ready for ``kopf`` or dynamic-client apply.
    Pure function of its arguments — no cluster access, no secret access.

    Args:
        analysis_id: OpenStudio analysis id (Mongo ObjectId hex in practice).
        storage_policy: typed ``storagePolicy`` from the CR spec — supplies
            ``backend`` (s3|gcs|azure), ``bucket`` and ``secretRef``.
        namespace: namespace hosting the OSCM CR and the NFS PVC.
        datapoint_ids: datapoint ids whose ``data_points/{id}`` trees are
            archived alongside ``analyses/{analysis_id}``.

    Raises:
        ValueError: if ``analysis_id`` is empty or the storage policy is
            missing backend/bucket/secretRef or has an unknown backend.
    """
    if not analysis_id:
        raise ValueError("analysis_id must be non-empty")
    backend = storage_policy.backend
    if backend not in _RCLONE_BACKEND_TYPES:
        raise ValueError(
            f"storagePolicy.backend must be one of {sorted(_RCLONE_BACKEND_TYPES)}, got {backend!r}"
        )
    if not storage_policy.bucket:
        raise ValueError("storagePolicy.bucket is required to generate an archival Job")
    if not storage_policy.secret_ref:
        raise ValueError("storagePolicy.secretRef is required to generate an archival Job")

    script = _archive_script(analysis_id, storage_policy.bucket, datapoint_ids)
    remote_type = _RCLONE_BACKEND_TYPES[backend]
    labels = {
        "app.kubernetes.io/managed-by": "openstudio-operator",
        "app.kubernetes.io/component": "archival",
        "energy.nrel.gov/archive-backend": backend,
    }

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": archival_job_name(analysis_id),
            "namespace": namespace,
            "labels": labels,
            # Annotations (unlike label values) accept any characters — the
            # raw ids and destination are recorded here for forensics.
            "annotations": {
                "energy.nrel.gov/analysis-id": analysis_id,
                "energy.nrel.gov/archive-destination": (
                    f"{RCLONE_REMOTE_NAME}:{storage_policy.bucket}/{analysis_id}"
                ),
            },
        },
        "spec": {
            "backoffLimit": ARCHIVAL_BACKOFF_LIMIT,
            "ttlSecondsAfterFinished": TTL_SECONDS_AFTER_FINISHED,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": RESTART_POLICY,
                    "containers": [
                        {
                            "name": "rclone",
                            "image": RCLONE_IMAGE,
                            "command": ["/bin/sh", "-c", script],
                            # Issue #114 — match the operator Deployment's
                            # hardening (#115) and the prune CronJob's
                            # (#78). The rclone container holds the S3/GCS/Azure
                            # credentials via envFrom secretRef (#218); all-caps
                            # net-raw + a writable root FS would let a
                            # compromised rclone pivot out, so deny it.
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                                "runAsNonRoot": True,
                                "runAsUser": 1000,
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "env": [
                                {
                                    "name": f"RCLONE_CONFIG_{RCLONE_REMOTE_NAME.upper()}_TYPE",
                                    "value": remote_type,
                                }
                            ],
                            "envFrom": [{"secretRef": {"name": storage_policy.secret_ref}}],
                            "volumeMounts": [
                                {
                                    "name": "nfs-assets",
                                    "mountPath": NFS_MOUNT_PATH,
                                    "readOnly": True,
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "nfs-assets",
                            "persistentVolumeClaim": {
                                "claimName": NFS_PVC_NAME,
                                "readOnly": True,
                            },
                        }
                    ],
                },
            },
        },
    }
