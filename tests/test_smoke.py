"""Structural smoke tests — keep CI green until Phase 1 logic lands."""

import openstudio_operator


def test_version():
    assert openstudio_operator.__version__ == "0.1.0"


def test_config_defaults_match_crd():
    from openstudio_operator.config import DEFAULT_REDIS_URL, OperatorConfig

    cfg = OperatorConfig()
    assert cfg.redis_url == DEFAULT_REDIS_URL
    assert DEFAULT_REDIS_URL == "redis://:openstudio@queue.openstudio-server.svc.cluster.local:6379"
    assert cfg.dry_run is False
    assert cfg.analysis_policy.max_duration_minutes == 180
    assert cfg.analysis_policy.graceful_stop_timeout_minutes == 15
    assert cfg.analysis_policy.force_delete_on_escalation is False
    assert cfg.datapoint_policy.max_datapoint_runtime_minutes == 45
    assert cfg.datapoint_policy.max_auto_requeues == 3
    assert cfg.worker_policy.recycle_worker_interval_hours == 12
    assert cfg.worker_policy.min_recycle_interval_minutes == 30
    assert cfg.web_background_policy.stall_window_minutes == 10
    assert cfg.storage_policy.archive_to_s3 is False
    assert cfg.storage_policy.retention_days == 7
    assert cfg.storage_policy.purge_completed_nfs_files is True


def test_config_from_spec_camelcase():
    from openstudio_operator.config import OperatorConfig

    cfg = OperatorConfig.from_spec(
        {
            "serverUrl": "http://web.openstudio.svc.cluster.local",
            "targetWorkerDeployment": "openstudio-worker",
            "dryRun": True,
            "analysisPolicy": {"maxDurationMinutes": 60},
            "storagePolicy": {
                "archiveToS3": True,
                "backend": "s3",
                "bucket": "os-archives",
                "secretRef": "os-archive-creds",
                "retentionDays": 0,
            },
        }
    )
    assert cfg.server_url == "http://web.openstudio.svc.cluster.local"
    assert cfg.target_worker_deployment == "openstudio-worker"
    assert cfg.dry_run is True
    assert cfg.analysis_policy.max_duration_minutes == 60
    assert cfg.storage_policy.archive_to_s3 is True
    assert cfg.storage_policy.backend == "s3"
    assert cfg.storage_policy.bucket == "os-archives"
    assert cfg.storage_policy.secret_ref == "os-archive-creds"
    assert cfg.storage_policy.retention_days == 0


def test_metrics_importable():
    from openstudio_operator import metrics

    assert metrics.SOFT_STOPS_TOTAL is not None
    assert metrics.DATAPOINTS_REQUEUED_TOTAL is not None
