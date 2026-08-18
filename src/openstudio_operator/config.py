"""Typed mapping of the OpenStudioClusterManager CRD spec (energy.nrel.gov/v1alpha1).

Field names and defaults mirror ``deploy/crd.yaml`` exactly. Policy values are
configuration, never hardcoded constants in handlers (per AGENTS.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AnalysisPolicy:
    max_duration_minutes: int = 180
    graceful_stop_timeout_minutes: int = 15
    auto_soft_stop: bool = True


@dataclass(frozen=True)
class DatapointPolicy:
    max_datapoint_runtime_minutes: int = 45
    max_auto_requeues: int = 3


@dataclass(frozen=True)
class WorkerPolicy:
    recycle_worker_interval_hours: int = 12
    recycle_after_analysis: bool = True


@dataclass(frozen=True)
class StoragePolicy:
    archive_to_s3: bool = False
    s3_bucket_name: str | None = None
    purge_completed_nfs_files: bool = True


@dataclass(frozen=True)
class OperatorConfig:
    server_url: str = ""
    target_worker_deployment: str = ""
    target_web_background_deployment: str = ""
    analysis_policy: AnalysisPolicy = field(default_factory=AnalysisPolicy)
    datapoint_policy: DatapointPolicy = field(default_factory=DatapointPolicy)
    worker_policy: WorkerPolicy = field(default_factory=WorkerPolicy)
    storage_policy: StoragePolicy = field(default_factory=StoragePolicy)

    @classmethod
    def from_spec(cls, spec: dict) -> OperatorConfig:
        """Build config from a CR ``spec`` dict (camelCase keys per the CRD)."""
        analysis = spec.get("analysisPolicy", {})
        datapoint = spec.get("datapointPolicy", {})
        worker = spec.get("workerPolicy", {})
        storage = spec.get("storagePolicy", {})
        return cls(
            server_url=spec.get("serverUrl", ""),
            target_worker_deployment=spec.get("targetWorkerDeployment", ""),
            target_web_background_deployment=spec.get("targetWebBackgroundDeployment", ""),
            analysis_policy=AnalysisPolicy(
                max_duration_minutes=analysis.get("maxDurationMinutes", 180),
                graceful_stop_timeout_minutes=analysis.get("gracefulStopTimeoutMinutes", 15),
                auto_soft_stop=analysis.get("autoSoftStop", True),
            ),
            datapoint_policy=DatapointPolicy(
                max_datapoint_runtime_minutes=datapoint.get("maxDatapointRuntimeMinutes", 45),
                max_auto_requeues=datapoint.get("maxAutoRequeues", 3),
            ),
            worker_policy=WorkerPolicy(
                recycle_worker_interval_hours=worker.get("recycleWorkerIntervalHours", 12),
                recycle_after_analysis=worker.get("recycleAfterAnalysis", True),
            ),
            storage_policy=StoragePolicy(
                archive_to_s3=storage.get("archiveToS3", False),
                s3_bucket_name=storage.get("s3BucketName"),
                purge_completed_nfs_files=storage.get("purgeCompletedNFSFiles", True),
            ),
        )
