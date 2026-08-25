"""Unit tests for ``OperatorConfig.from_spec`` parsing (issue #482).

test_smoke.py owns the *structural* contract — defaults parity with
``deploy/crd.yaml`` (``test_config_defaults_parity_with_crd_yaml``), one
full-camelCase-spec walk (``test_config_from_spec_camelcase``), and the
#463 secretRef happy/absent/malformed shapes. This module complements it
with the *parse-semantics* contract of ``config.py`` — the single config
path feeding all four handlers (issue #3): per-field partial overrides,
missing sub-dicts, type pass-through (no coercion), enum pass-through,
and the ``maxAutoRequeues: 0`` warn-only value. Where a behavior is
admission-gated by the CRD schema (``type: integer``, the backend
``enum``), these tests pin what ``from_spec`` does with a hand-crafted
dict that already slipped past admission — the defensive layer tests.

All tests take plain dicts; no K8s fakes needed.
"""

from __future__ import annotations

import dataclasses

import pytest

from openstudio_operator.config import (
    AnalysisPolicy,
    DatapointPolicy,
    OperatorConfig,
    RedisCredentials,
    RedisSecretRef,
    StoragePolicy,
    WebBackgroundPolicy,
    WorkerPolicy,
)

# Every camelCase CRD key from_spec reads, with a value distinct from both
# the CRD default and test_smoke.py's sentinels — the per-key walk pins each
# mapping independently (a typo'd dict key in config.py would silently fall
# back to the default and fail here).
FULL_SPEC: dict = {
    "serverUrl": "http://web.openstudio-server.svc.cluster.local:80",
    "redisUrl": "redis://queue.openstudio-server.svc.cluster.local:6379/1",
    "redisCredentials": {"secretRef": {"name": "redis-creds", "key": "url"}},
    "dryRun": True,
    "targetWorkerDeployment": "worker-custom",
    "targetWebBackgroundDeployment": "web-background-custom",
    "analysisPolicy": {
        "maxDurationMinutes": 240,
        "gracefulStopTimeoutMinutes": 20,
        "autoSoftStop": False,
        "forceDeleteOnEscalation": True,
    },
    "datapointPolicy": {
        "maxDatapointRuntimeMinutes": 90,
        "maxAutoRequeues": 5,
    },
    "workerPolicy": {
        "recycleWorkerIntervalHours": 24,
        "recycleAfterAnalysis": False,
        "minRecycleIntervalMinutes": 60,
    },
    "webBackgroundPolicy": {"stallWindowMinutes": 30},
    "storagePolicy": {
        "archiveToS3": True,
        "backend": "azure",
        "bucket": "archive-bucket",
        "secretRef": "archive-creds",
        "retentionDays": 14,
        "purgeCompletedNFSFiles": False,
    },
}


def test_from_spec_full_spec_populates_every_field() -> None:
    """Every camelCase key present → every snake_case field set (all mappings)."""
    cfg = OperatorConfig.from_spec(FULL_SPEC)
    assert cfg.server_url == "http://web.openstudio-server.svc.cluster.local:80"
    assert cfg.redis_url == "redis://queue.openstudio-server.svc.cluster.local:6379/1"
    assert cfg.redis_credentials == RedisCredentials(
        secret_ref=RedisSecretRef(name="redis-creds", key="url")
    )
    assert cfg.dry_run is True
    assert cfg.target_worker_deployment == "worker-custom"
    assert cfg.target_web_background_deployment == "web-background-custom"
    assert cfg.analysis_policy == AnalysisPolicy(
        max_duration_minutes=240,
        graceful_stop_timeout_minutes=20,
        auto_soft_stop=False,
        force_delete_on_escalation=True,
    )
    assert cfg.datapoint_policy == DatapointPolicy(
        max_datapoint_runtime_minutes=90,
        max_auto_requeues=5,
    )
    assert cfg.worker_policy == WorkerPolicy(
        recycle_worker_interval_hours=24,
        recycle_after_analysis=False,
        min_recycle_interval_minutes=60,
    )
    assert cfg.web_background_policy == WebBackgroundPolicy(stall_window_minutes=30)
    assert cfg.storage_policy == StoragePolicy(
        archive_to_s3=True,
        backend="azure",
        bucket="archive-bucket",
        secret_ref="archive-creds",
        retention_days=14,
        purge_completed_nfs_files=False,
    )


def test_from_spec_partial_sub_dict_keeps_sibling_defaults() -> None:
    """One key set inside a policy sub-dict → only that field moves, siblings default.

    Per-key fallback: ``analysis.get("maxDurationMinutes", 180)`` style reads
    mean a partially-populated sub-dict (common in hand-edited CRs) must not
    disturb the untouched fields.
    """
    cfg = OperatorConfig.from_spec({"serverUrl": "http://web", "analysisPolicy": {"maxDurationMinutes": 90}})
    assert cfg.analysis_policy.max_duration_minutes == 90
    assert cfg.analysis_policy.graceful_stop_timeout_minutes == 15
    assert cfg.analysis_policy.auto_soft_stop is True
    assert cfg.analysis_policy.force_delete_on_escalation is False

    cfg = OperatorConfig.from_spec({"serverUrl": "http://web", "storagePolicy": {"backend": "gcs"}})
    assert cfg.storage_policy.backend == "gcs"
    assert cfg.storage_policy.archive_to_s3 is False
    assert cfg.storage_policy.bucket is None
    assert cfg.storage_policy.retention_days == 7
    assert cfg.storage_policy.purge_completed_nfs_files is True


def test_from_spec_missing_policy_sub_dicts_use_module_defaults() -> None:
    """No ``analysisPolicy`` / ``storagePolicy`` / … keys → ``spec.get(k, {})`` defaults.

    A spec carrying only the top-level scalars yields the exact default
    policy instances — the same values the CRD schema would default-fill.
    """
    cfg = OperatorConfig.from_spec({"serverUrl": "http://web", "dryRun": True})
    assert cfg.server_url == "http://web"
    assert cfg.dry_run is True
    assert cfg.analysis_policy == AnalysisPolicy()
    assert cfg.datapoint_policy == DatapointPolicy()
    assert cfg.worker_policy == WorkerPolicy()
    assert cfg.web_background_policy == WebBackgroundPolicy()
    assert cfg.storage_policy == StoragePolicy()
    assert cfg.redis_credentials == RedisCredentials()


def test_from_spec_empty_policy_sub_dict_equals_missing() -> None:
    """``analysisPolicy: {}`` is equivalent to the key being absent."""
    base = {"serverUrl": "http://web"}
    with_key = OperatorConfig.from_spec({**base, "analysisPolicy": {}})
    without_key = OperatorConfig.from_spec(base)
    assert with_key == without_key


def test_from_spec_does_not_coerce_string_integers() -> None:
    """``maxDurationMinutes: "180"`` passes through as ``str`` — no coercion, no raise.

    Pinned CURRENT behavior (#482): ``from_spec`` is a plain ``.get`` with a
    typed default — the dataclass annotation is documentation, not a runtime
    check, so a string survives into ``analysis_policy.max_duration_minutes``.
    The real gate is CRD admission (``type: integer`` in deploy/crd.yaml,
    enforced by the API server); a string reaching this parser means a
    hand-crafted spec already slipped past it. Downstream hazard if it does:
    ``timedelta(minutes="180")`` raises ``TypeError`` inside the handler tick.
    """
    cfg = OperatorConfig.from_spec({"serverUrl": "http://web", "analysisPolicy": {"maxDurationMinutes": "180"}})
    assert cfg.analysis_policy.max_duration_minutes == "180"
    assert isinstance(cfg.analysis_policy.max_duration_minutes, str)


def test_from_spec_invalid_backend_passes_through_unvalidated() -> None:
    """``storagePolicy.backend: "ftp"`` is stored verbatim — validation is NOT here.

    Pinned CURRENT behavior (#482): from_spec performs no enum validation;
    the invalid value lands in ``storage_policy.backend`` untouched. The two
    real gates are (a) the CRD ``enum: [s3, gcs, azure]`` at admission and
    (b) ``archival.build_archival_job``, which raises ``ValueError`` at
    archival-Job build time for an unknown backend. Config-level pass-through
    means the failure surfaces when archival is attempted, not at spec parse.
    """
    cfg = OperatorConfig.from_spec({"serverUrl": "http://web", "storagePolicy": {"backend": "ftp"}})
    assert cfg.storage_policy.backend == "ftp"
    for valid in ("s3", "gcs", "azure"):
        parsed = OperatorConfig.from_spec({"serverUrl": "http://web", "storagePolicy": {"backend": valid}})
        assert parsed.storage_policy.backend == valid


def test_from_spec_max_auto_requeues_zero_parses_to_warn_only_budget() -> None:
    """``maxAutoRequeues: 0`` parses to ``0`` — the supported warn-only mode.

    The watchdog requeues while ``requeues[dp].count < max_auto_requeues``
    (datapoint_watchdog); with 0 the comparison ``0 < 0`` is False, so an
    over-runtime datapoint is never requeued and only the exhaustion Warning
    fires. This test pins the config-level contract (the value parses to int
    0, distinct from the default 3 and from absence); the behavioral test is
    ``tests/test_datapoint_watchdog.py::test_max_auto_requeues_zero_is_warn_only_mode``.
    """
    cfg = OperatorConfig.from_spec({"serverUrl": "http://web", "datapointPolicy": {"maxAutoRequeues": 0}})
    max_requeues = cfg.datapoint_policy.max_auto_requeues
    assert max_requeues == 0
    assert isinstance(max_requeues, int) and not isinstance(max_requeues, bool)
    assert not (0 < max_requeues)  # the warn-only gate: budget starts exhausted
    # Absent key keeps the default budget of 3 (requeue up to 3 times).
    assert OperatorConfig.from_spec({"serverUrl": "http://web"}).datapoint_policy.max_auto_requeues == 3


def test_from_spec_redis_credentials_null_tolerated() -> None:
    """``redisCredentials: None`` → ``secret_ref is None`` (the ``or {}`` guard).

    ``spec.get("redisCredentials") or {}`` treats an explicit null the same
    as absence — inline ``spec.redisUrl`` remains the credential source
    (#463). Complements test_smoke.py's absent/``{}``/malformed shapes.
    """
    cfg = OperatorConfig.from_spec({"serverUrl": "http://web", "redisCredentials": None})
    assert cfg.redis_credentials.secret_ref is None


def test_from_spec_null_policy_sub_dict_raises_attribute_error() -> None:
    """Pinned CURRENT behavior: ``analysisPolicy: None`` → ``AttributeError``.

    The five policy sub-dicts use ``spec.get(k, {})`` — the default only
    fires when the key is *missing*, so an explicit null yields ``None`` and
    the first ``.get`` on it crashes with ``AttributeError`` (not the
    uniform ``ValueError`` the #463 secretRef parser raises for malformed
    input, and unlike redisCredentials' null-tolerating ``or {}`` guard).
    Unreachable through the API server (the CRD types these as non-nullable
    objects), so this is a defensive pin of what IS, not an endorsement —
    flagged as a follow-up in the #482 report.
    """
    with pytest.raises(AttributeError, match="'NoneType' object has no attribute 'get'"):
        OperatorConfig.from_spec({"serverUrl": "http://web", "analysisPolicy": None})
    with pytest.raises(AttributeError, match="'NoneType' object has no attribute 'get'"):
        OperatorConfig.from_spec({"serverUrl": "http://web", "storagePolicy": None})


def test_from_spec_ignores_unknown_keys() -> None:
    """Unknown top-level and sub-dict keys are dropped (forward compatibility).

    from_spec reads only the v1alpha1 keys; anything else in the dict is
    discarded rather than rejected, so a CR written against a newer schema
    still parses under this operator.
    """
    cfg = OperatorConfig.from_spec(
        {
            "serverUrl": "http://web",
            "futureTopLevel": {"nested": 1},
            "analysisPolicy": {"maxDurationMinutes": 60, "futureKey": True},
        }
    )
    assert cfg.server_url == "http://web"
    assert cfg.analysis_policy == AnalysisPolicy(max_duration_minutes=60)
    assert not hasattr(cfg, "future_top_level")


def test_config_objects_are_frozen() -> None:
    """Parsed config is immutable — assignment raises ``FrozenInstanceError``.

    Handlers receive the config as a read-only value; a policy tweak must go
    through the CR spec (single config path, issue #3), never by mutating the
    parsed object in flight.
    """
    cfg = OperatorConfig.from_spec(FULL_SPEC)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.dry_run = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.storage_policy.backend = "s3"  # type: ignore[misc]
