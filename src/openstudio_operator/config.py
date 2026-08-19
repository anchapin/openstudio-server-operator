"""Typed mapping of the OpenStudioClusterManager CRD spec (energy.nrel.gov/v1alpha1).

Field names and defaults mirror ``deploy/crd.yaml`` exactly. Policy values are
configuration, never hardcoded constants in handlers (per AGENTS.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Issue #116 — empty by design. The historical default
#: ``redis://:openstudio@queue...`` baked the kind-recipe password
#: ``openstudio`` into every published CRD, leaking it through every
#: ``kubectl get oscrm -o yaml`` of an unconfigured CR. The operator now
#: refuses to operate when ``spec.redisUrl`` is empty (handlers/__init__.py
#: startup guard); helm-chart users must set it explicitly or read the
#: URL from the Redis Secret via a templating step.
DEFAULT_REDIS_URL = ""

#: Module 5 (#13): heartbeat-staleness threshold (seconds) passed to
#: ``ReadOnlyRedisClient.stale_workers`` when judging "no worker is
#: processing". Resque workers heartbeat every ~5 s while alive, so 300 s
#: with no fresh heartbeat from ANY worker unambiguously means "nobody is
#: processing" while staying well under the default 10-minute stall window
#: (the sustained window, not this threshold, dominates reaction time).
#: NOT a CRD field — the v1alpha1 schema is fixed (#4) — so it lives here as
#: a documented wiring default (same convention as DEFAULT_REDIS_URL).
DEFAULT_WORKER_HEARTBEAT_STALE_SECONDS = 300.0


@dataclass(frozen=True)
class AnalysisPolicy:
    max_duration_minutes: int = 180
    graceful_stop_timeout_minutes: int = 15
    auto_soft_stop: bool = True
    force_delete_on_escalation: bool = False


@dataclass(frozen=True)
class DatapointPolicy:
    max_datapoint_runtime_minutes: int = 45
    max_auto_requeues: int = 3


@dataclass(frozen=True)
class WorkerPolicy:
    recycle_worker_interval_hours: int = 12
    recycle_after_analysis: bool = True
    min_recycle_interval_minutes: int = 30


@dataclass(frozen=True)
class WebBackgroundPolicy:
    stall_window_minutes: int = 10


@dataclass(frozen=True)
class StoragePolicy:
    archive_to_s3: bool = False
    backend: str | None = None
    bucket: str | None = None
    secret_ref: str | None = None
    retention_days: int = 7
    purge_completed_nfs_files: bool = True


@dataclass(frozen=True)
class OperatorConfig:
    server_url: str = ""
    redis_url: str = DEFAULT_REDIS_URL
    dry_run: bool = False
    target_worker_deployment: str = ""
    target_web_background_deployment: str = ""
    analysis_policy: AnalysisPolicy = field(default_factory=AnalysisPolicy)
    datapoint_policy: DatapointPolicy = field(default_factory=DatapointPolicy)
    worker_policy: WorkerPolicy = field(default_factory=WorkerPolicy)
    web_background_policy: WebBackgroundPolicy = field(default_factory=WebBackgroundPolicy)
    storage_policy: StoragePolicy = field(default_factory=StoragePolicy)

    @classmethod
    def from_spec(cls, spec: dict) -> OperatorConfig:
        """Build config from a CR ``spec`` dict (camelCase keys per the CRD)."""
        analysis = spec.get("analysisPolicy", {})
        datapoint = spec.get("datapointPolicy", {})
        worker = spec.get("workerPolicy", {})
        web_background = spec.get("webBackgroundPolicy", {})
        storage = spec.get("storagePolicy", {})
        return cls(
            server_url=spec.get("serverUrl", ""),
            redis_url=spec.get("redisUrl", DEFAULT_REDIS_URL),
            dry_run=spec.get("dryRun", False),
            target_worker_deployment=spec.get("targetWorkerDeployment", ""),
            target_web_background_deployment=spec.get("targetWebBackgroundDeployment", ""),
            analysis_policy=AnalysisPolicy(
                max_duration_minutes=analysis.get("maxDurationMinutes", 180),
                graceful_stop_timeout_minutes=analysis.get("gracefulStopTimeoutMinutes", 15),
                auto_soft_stop=analysis.get("autoSoftStop", True),
                force_delete_on_escalation=analysis.get("forceDeleteOnEscalation", False),
            ),
            datapoint_policy=DatapointPolicy(
                max_datapoint_runtime_minutes=datapoint.get("maxDatapointRuntimeMinutes", 45),
                max_auto_requeues=datapoint.get("maxAutoRequeues", 3),
            ),
            worker_policy=WorkerPolicy(
                recycle_worker_interval_hours=worker.get("recycleWorkerIntervalHours", 12),
                recycle_after_analysis=worker.get("recycleAfterAnalysis", True),
                min_recycle_interval_minutes=worker.get("minRecycleIntervalMinutes", 30),
            ),
            web_background_policy=WebBackgroundPolicy(
                stall_window_minutes=web_background.get("stallWindowMinutes", 10),
            ),
            storage_policy=StoragePolicy(
                archive_to_s3=storage.get("archiveToS3", False),
                backend=storage.get("backend"),
                bucket=storage.get("bucket"),
                secret_ref=storage.get("secretRef"),
                retention_days=storage.get("retentionDays", 7),
                purge_completed_nfs_files=storage.get("purgeCompletedNFSFiles", True),
            ),
        )
