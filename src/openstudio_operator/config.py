"""Typed mapping of the OpenStudioClusterManager CRD spec (energy.nrel.gov/v1alpha1).

Field names and defaults mirror ``deploy/crd.yaml`` exactly. Policy values are
configuration, never hardcoded constants in handlers (per AGENTS.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_REDIS_URL = "redis://:openstudio@queue.openstudio-server.svc.cluster.local:6379"

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


# --- HPA-floor adjuster (plan Phase 4, issue #18, decision D10) ----------------
#
# D10: no KEDA, no chart fork — the adjuster patches only the chart's
# existing CPU HPA ``worker-hpa`` ``spec.minReplicas`` from Redis backlog
# (the chart's HPA is unconditional; two autoscalers on one Deployment
# would fight). These are wiring defaults, NOT CRD fields — the v1alpha1
# schema is fixed (#4); CRD fields are a follow-up if tuning demands (same
# convention as DEFAULT_REDIS_URL / DEFAULT_WORKER_HEARTBEAT_STALE_SECONDS).
#
# Decision D10a (issue #46 — chart-derived baseline): the runtime decay
# floor is the HPA's ``spec.minReplicas`` captured at operator startup,
# NOT a hardcoded constant. The chart already encodes the designed floor
# for the worker fleet (NatLabRockies production: 2; #19 kind manifest:
# 1); a hardcoded ``DEFAULT_HPA_BASELINE_MIN_REPLICAS = 1`` would let
# decay undercut the production chart's intentional floor — quietly
# fighting the deployment. ``DEFAULT_HPA_BASELINE_MIN_REPLICAS`` therefore
# is the FALLBACK only — used when the HPA isn't observable at startup
# (early boot, RBAC denied, transient API error). The handler captures
# the HPA's current value once per namespace and caches it for the
# lifetime of the operator process (handlers/hpa_floor.py
# ``resolve_baseline_min_replicas``) — re-reading on every tick would
# defeat the safety property by allowing a concurrent chart edit to
# silently lower the decay floor. Chart production HPA bounds are 2–20,
# so the deepest default floor (10) stays inside them; the handler
# additionally never raises a floor above the HPA's own maxReplicas
# (read-only respect — maxReplicas is never patched).
#
# Tier semantics: a backlog (simulations + requeued depths, summed) at or
# above a tier's threshold maps to that tier's floor; below every tier maps
# to the baseline. Highest matching tier wins.

#: Fallback floor when the HPA is not observable at operator startup.
#: The runtime decay floor is the chart's HPA ``minReplicas`` captured at
#: startup (issue #46, see module-level decision note above) — this
#: constant is only consulted if the HPA read fails. Matches the #19
#: kind-cluster manifest (scripts/manifests/06-worker.yaml
#: ``minReplicas: 1``); production chart baseline is 2, which is
#: captured live and therefore wins over this fallback when the HPA is
#: observable.
DEFAULT_HPA_BASELINE_MIN_REPLICAS = 1

#: Minimum time between ANY two adjustments (anti-flap), seconds.
DEFAULT_HPA_FLOOR_COOLDOWN_SECONDS = 300.0

#: (backlog_threshold, min_replicas) tiers, descending by threshold.
DEFAULT_HPA_FLOOR_TIERS: tuple[tuple[int, int], ...] = (
    (500, 10),
    (250, 8),
    (120, 6),
    (60, 4),
    (25, 3),
    (10, 2),
)


@dataclass(frozen=True)
class HpaFloorPolicy:
    """Backlog-to-floor mapping table plus baseline and cooldown (issue #18).

    Pure policy, held at module level as ``DEFAULT_HPA_FLOOR_POLICY`` so the
    handler contains no mapping numbers of its own (AGENTS.md: policy is
    configuration). ``floor_for`` is the single blessed lookup.
    """

    tiers: tuple[tuple[int, int], ...] = DEFAULT_HPA_FLOOR_TIERS
    baseline_min_replicas: int = DEFAULT_HPA_BASELINE_MIN_REPLICAS
    cooldown_seconds: float = DEFAULT_HPA_FLOOR_COOLDOWN_SECONDS

    def __post_init__(self) -> None:
        thresholds = [threshold for threshold, _ in self.tiers]
        if len(set(thresholds)) != len(thresholds):
            raise ValueError(f"duplicate tier thresholds in {self.tiers}")
        for threshold, min_replicas in self.tiers:
            if threshold < 1:
                raise ValueError(f"tier threshold {threshold} must be >= 1")
            if min_replicas < 1:
                raise ValueError(f"tier min_replicas {min_replicas} must be >= 1")
        if self.baseline_min_replicas < 1:
            raise ValueError(f"baseline {self.baseline_min_replicas} must be >= 1")
        if self.cooldown_seconds <= 0:
            raise ValueError(f"cooldown {self.cooldown_seconds} must be > 0")

    def floor_for(self, backlog: int, *, baseline: int | None = None) -> int:
        """Floor for a backlog: highest tier whose threshold it meets, else baseline.

        ``baseline`` overrides :attr:`baseline_min_replicas` for this single
        lookup when supplied — used by the handler to substitute the
        chart-derived baseline (issue #46, ``resolve_baseline_min_replicas``)
        without rebuilding the frozen policy object on every tick.
        """
        for threshold, min_replicas in sorted(self.tiers, reverse=True):
            if backlog >= threshold:
                return min_replicas
        return self.baseline_min_replicas if baseline is None else baseline


DEFAULT_HPA_FLOOR_POLICY = HpaFloorPolicy()
