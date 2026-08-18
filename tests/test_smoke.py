"""Structural smoke tests — keep CI green until Phase 1 logic lands."""

import openstudio_operator


def test_version():
    assert openstudio_operator.__version__ == "0.1.0"


def test_config_defaults_match_crd():
    from openstudio_operator.config import OperatorConfig

    cfg = OperatorConfig()
    assert cfg.analysis_policy.max_duration_minutes == 180
    assert cfg.analysis_policy.graceful_stop_timeout_minutes == 15
    assert cfg.datapoint_policy.max_datapoint_runtime_minutes == 45
    assert cfg.datapoint_policy.max_auto_requeues == 3
    assert cfg.worker_policy.recycle_worker_interval_hours == 12
    assert cfg.storage_policy.archive_to_s3 is False
    assert cfg.storage_policy.purge_completed_nfs_files is True


def test_config_from_spec_camelcase():
    from openstudio_operator.config import OperatorConfig

    cfg = OperatorConfig.from_spec(
        {
            "serverUrl": "http://web.openstudio.svc.cluster.local",
            "targetWorkerDeployment": "openstudio-worker",
            "analysisPolicy": {"maxDurationMinutes": 60},
            "storagePolicy": {"archiveToS3": True, "s3BucketName": "os-archives"},
        }
    )
    assert cfg.server_url == "http://web.openstudio.svc.cluster.local"
    assert cfg.target_worker_deployment == "openstudio-worker"
    assert cfg.analysis_policy.max_duration_minutes == 60
    assert cfg.storage_policy.archive_to_s3 is True
    assert cfg.storage_policy.s3_bucket_name == "os-archives"


def test_metrics_importable():
    from openstudio_operator import metrics

    assert metrics.SOFT_STOPS_TOTAL is not None
    assert metrics.DATAPOINTS_REQUEUED_TOTAL is not None
