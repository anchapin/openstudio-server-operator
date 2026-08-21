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
class RedisSecretRef:
    """Names the Secret key holding the FULL ``redis://...`` URL (issue #463).

    ``spec.redisCredentials.secretRef: {name, key}`` points at one Secret
    key whose value is the complete connection URL
    (``redis://:password@queue:6379``) — NOT the bare password. Full-URL
    semantics avoid URL-reconstruction logic in the operator (no userinfo
    surgery to inject a password into a credential-free URL) and match the
    value the helm recipe already templates into the web / worker
    ``REDIS_URL`` env vars. The operator resolves it via
    :func:`openstudio_operator.client_factory.get_read_only_redis_client`
    (the bounded "operator never reads secrets" exception, #463).
    """

    name: str
    key: str


@dataclass(frozen=True)
class RedisCredentials:
    """``spec.redisCredentials`` — Secret-sourced Redis credentials (#463).

    ``secret_ref`` is ``None`` when the CR does not set it; the operator
    then falls back to the inline ``spec.redisUrl`` (which the #463-tightened
    CRD pattern forbids carrying credentials, so credential-free inline URLs
    remain valid for no-auth dev clusters). When BOTH are present the
    secretRef wins — the resolution preference is issue #463's acceptance
    criterion.
    """

    secret_ref: RedisSecretRef | None = None


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


def _redis_secret_ref_from_spec(raw: object) -> RedisSecretRef | None:
    """Parse ``spec.redisCredentials.secretRef`` (#463); malformed → loud.

    Returns ``None`` when the CR does not set the field (the default —
    inline ``spec.redisUrl`` remains the active credential source). A
    present-but-malformed value raises ``ValueError``: credential
    misconfiguration must be loud, never silently treated as absent (the
    CRD's ``required: [name, key]`` makes this unreachable through the API
    server; the raise is the defensive assertion for hand-crafted specs).
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        # ValueError (not TypeError) on purpose: one exception type is the
        # public contract of this parser — callers catch ValueError for every
        # malformed-secretRef shape alike.
        raise ValueError(  # noqa: TRY004 — uniform parse-error type
            f"spec.redisCredentials.secretRef must be an object with 'name' and "
            f"'key' (issue #463); got {type(raw).__name__}: {raw!r}"
        )
    name = raw.get("name")
    key = raw.get("key")
    if not isinstance(name, str) or not name or not isinstance(key, str) or not key:
        raise ValueError(
            f"spec.redisCredentials.secretRef requires non-empty string 'name' and "
            f"'key' (issue #463); got name={name!r}, key={key!r}"
        )
    return RedisSecretRef(name=name, key=key)


@dataclass(frozen=True)
class OperatorConfig:
    server_url: str = ""
    redis_url: str = DEFAULT_REDIS_URL
    redis_credentials: RedisCredentials = field(default_factory=RedisCredentials)
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
        redis_credentials = spec.get("redisCredentials") or {}
        return cls(
            server_url=spec.get("serverUrl", ""),
            redis_url=spec.get("redisUrl", DEFAULT_REDIS_URL),
            redis_credentials=RedisCredentials(
                secret_ref=_redis_secret_ref_from_spec(redis_credentials.get("secretRef")),
            ),
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
