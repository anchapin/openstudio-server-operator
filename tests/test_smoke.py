"""Smoke tests + CRD/config parity tests.

Parity approach (issue #5): the hardcoded assertions below keep the original
explicit style (readable failures), while the structural tests parse
``deploy/crd.yaml`` and verify the mapping/defaults against it, so a CRD change
without a matching ``config.py`` change (or vice versa) fails CI.
"""

from pathlib import Path

import yaml

import openstudio_operator
from openstudio_operator.config import DEFAULT_REDIS_URL, OperatorConfig

CRD_YAML = Path(__file__).resolve().parents[1] / "deploy" / "crd.yaml"

# camelCase CRD spec path -> snake_case OperatorConfig attribute path.
# Test coverage contract: every leaf spec property declared in deploy/crd.yaml
# appears here exactly once, and every mapped attribute resolves on a bare
# OperatorConfig. yaml is a transitive dependency of kopf (no new dep added).
CRD_SPEC_FIELD_MAP = {
    "serverUrl": "server_url",
    "redisUrl": "redis_url",
    "dryRun": "dry_run",
    "targetWorkerDeployment": "target_worker_deployment",
    "targetWebBackgroundDeployment": "target_web_background_deployment",
    "analysisPolicy.maxDurationMinutes": "analysis_policy.max_duration_minutes",
    "analysisPolicy.gracefulStopTimeoutMinutes": (
        "analysis_policy.graceful_stop_timeout_minutes"
    ),
    "analysisPolicy.autoSoftStop": "analysis_policy.auto_soft_stop",
    "analysisPolicy.forceDeleteOnEscalation": "analysis_policy.force_delete_on_escalation",
    "datapointPolicy.maxDatapointRuntimeMinutes": (
        "datapoint_policy.max_datapoint_runtime_minutes"
    ),
    "datapointPolicy.maxAutoRequeues": "datapoint_policy.max_auto_requeues",
    "workerPolicy.recycleWorkerIntervalHours": "worker_policy.recycle_worker_interval_hours",
    "workerPolicy.recycleAfterAnalysis": "worker_policy.recycle_after_analysis",
    "workerPolicy.minRecycleIntervalMinutes": "worker_policy.min_recycle_interval_minutes",
    "webBackgroundPolicy.stallWindowMinutes": "web_background_policy.stall_window_minutes",
    "storagePolicy.archiveToS3": "storage_policy.archive_to_s3",
    "storagePolicy.backend": "storage_policy.backend",
    "storagePolicy.bucket": "storage_policy.bucket",
    "storagePolicy.secretRef": "storage_policy.secret_ref",
    "storagePolicy.retentionDays": "storage_policy.retention_days",
    "storagePolicy.purgeCompletedNFSFiles": "storage_policy.purge_completed_nfs_files",
}


def _crd_spec_leaves() -> dict[str, dict]:
    """Flatten the CRD's ``spec`` schema into ``{dotted.path: leaf schema}``."""
    doc = yaml.safe_load(CRD_YAML.read_text())
    version = doc["spec"]["versions"][0]
    spec_schema = version["schema"]["openAPIV3Schema"]["properties"]["spec"]
    leaves: dict[str, dict] = {}

    def walk(schema: dict, prefix: str) -> None:
        properties = schema.get("properties")
        if properties is None:
            leaves[prefix] = schema
            return
        for name, sub in properties.items():
            walk(sub, f"{prefix}.{name}" if prefix else name)

    walk(spec_schema, "")
    return leaves


def _get_attr(obj: object, dotted: str) -> object:
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def test_version():
    assert openstudio_operator.__version__ == "0.1.0"


def test_config_defaults_match_crd():
    cfg = OperatorConfig()
    assert cfg.server_url == ""
    # Issue #116 — DEFAULT_REDIS_URL is now empty by design (the
    # historical `redis://:openstudio@queue....` baked the kind-recipe
    # password into every published CRD; see AGENTS.md / #116 for the
    # rationale + the Redis-URL-guard Event the operator emits when
    # this stays empty).
    assert cfg.redis_url == ""
    assert DEFAULT_REDIS_URL == ""
    assert cfg.dry_run is False
    assert cfg.target_worker_deployment == ""
    assert cfg.target_web_background_deployment == ""
    assert cfg.analysis_policy.max_duration_minutes == 180
    assert cfg.analysis_policy.graceful_stop_timeout_minutes == 15
    assert cfg.analysis_policy.auto_soft_stop is True
    assert cfg.analysis_policy.force_delete_on_escalation is False
    assert cfg.datapoint_policy.max_datapoint_runtime_minutes == 45
    assert cfg.datapoint_policy.max_auto_requeues == 3
    assert cfg.worker_policy.recycle_worker_interval_hours == 12
    assert cfg.worker_policy.recycle_after_analysis is True
    assert cfg.worker_policy.min_recycle_interval_minutes == 30
    assert cfg.web_background_policy.stall_window_minutes == 10
    assert cfg.storage_policy.archive_to_s3 is False
    assert cfg.storage_policy.backend is None
    assert cfg.storage_policy.bucket is None
    assert cfg.storage_policy.secret_ref is None
    assert cfg.storage_policy.retention_days == 7
    assert cfg.storage_policy.purge_completed_nfs_files is True


def test_config_from_spec_camelcase():
    cfg = OperatorConfig.from_spec(
        {
            "serverUrl": "http://web.openstudio-server.svc.cluster.local",
            "redisUrl": "redis://:secret@queue.openstudio-server.svc.cluster.local:6379/1",
            "dryRun": True,
            "targetWorkerDeployment": "worker",
            "targetWebBackgroundDeployment": "web-background",
            "analysisPolicy": {
                "maxDurationMinutes": 60,
                "gracefulStopTimeoutMinutes": 5,
                "autoSoftStop": False,
                "forceDeleteOnEscalation": True,
            },
            "datapointPolicy": {
                "maxDatapointRuntimeMinutes": 20,
                "maxAutoRequeues": 1,
            },
            "workerPolicy": {
                "recycleWorkerIntervalHours": 4,
                "recycleAfterAnalysis": False,
                "minRecycleIntervalMinutes": 15,
            },
            "webBackgroundPolicy": {"stallWindowMinutes": 25},
            "storagePolicy": {
                "archiveToS3": True,
                "backend": "gcs",
                "bucket": "os-archives",
                "secretRef": "os-archive-creds",
                "retentionDays": 0,
                "purgeCompletedNFSFiles": False,
            },
        }
    )
    assert cfg.server_url == "http://web.openstudio-server.svc.cluster.local"
    assert cfg.redis_url == "redis://:secret@queue.openstudio-server.svc.cluster.local:6379/1"
    assert cfg.dry_run is True
    assert cfg.target_worker_deployment == "worker"
    assert cfg.target_web_background_deployment == "web-background"
    assert cfg.analysis_policy.max_duration_minutes == 60
    assert cfg.analysis_policy.graceful_stop_timeout_minutes == 5
    assert cfg.analysis_policy.auto_soft_stop is False
    assert cfg.analysis_policy.force_delete_on_escalation is True
    assert cfg.datapoint_policy.max_datapoint_runtime_minutes == 20
    assert cfg.datapoint_policy.max_auto_requeues == 1
    assert cfg.worker_policy.recycle_worker_interval_hours == 4
    assert cfg.worker_policy.recycle_after_analysis is False
    assert cfg.worker_policy.min_recycle_interval_minutes == 15
    assert cfg.web_background_policy.stall_window_minutes == 25
    assert cfg.storage_policy.archive_to_s3 is True
    assert cfg.storage_policy.backend == "gcs"
    assert cfg.storage_policy.bucket == "os-archives"
    assert cfg.storage_policy.secret_ref == "os-archive-creds"
    assert cfg.storage_policy.retention_days == 0
    assert cfg.storage_policy.purge_completed_nfs_files is False


def test_config_from_spec_empty_equals_defaults():
    assert OperatorConfig.from_spec({}) == OperatorConfig()


def test_crd_spec_field_map_complete():
    """Every leaf spec property in deploy/crd.yaml maps to a real config attribute."""
    leaves = _crd_spec_leaves()
    assert set(leaves) == set(CRD_SPEC_FIELD_MAP)
    cfg = OperatorConfig()
    for attr_path in CRD_SPEC_FIELD_MAP.values():
        _get_attr(cfg, attr_path)  # raises AttributeError on a stale mapping


def test_config_defaults_parity_with_crd_yaml():
    """Bare OperatorConfig defaults equal every default declared in deploy/crd.yaml."""
    cfg = OperatorConfig()
    for crd_path, schema in _crd_spec_leaves().items():
        if "default" not in schema:
            continue
        assert _get_attr(cfg, CRD_SPEC_FIELD_MAP[crd_path]) == schema["default"], crd_path


def test_config_from_spec_round_trips_every_crd_field():
    """from_spec reads every leaf CRD field: sentinel values survive the round trip."""
    spec: dict = {}
    sentinels: dict[str, object] = {}
    for crd_path, schema in _crd_spec_leaves().items():
        if schema["type"] == "boolean":
            value = not schema.get("default", False)
        elif schema["type"] == "integer":
            value = schema.get("default", 0) + 1
        else:
            value = f"sentinel-{crd_path}"
        sentinels[crd_path] = value
        node = spec
        parts = crd_path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    cfg = OperatorConfig.from_spec(spec)
    for crd_path, value in sentinels.items():
        assert _get_attr(cfg, CRD_SPEC_FIELD_MAP[crd_path]) == value, crd_path


def test_metrics_importable():
    from openstudio_operator import metrics

    assert metrics.SOFT_STOPS_TOTAL is not None
    assert metrics.DATAPOINTS_REQUEUED_TOTAL is not None
