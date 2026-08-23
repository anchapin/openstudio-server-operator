"""Unit tests for the read-only Redis client (issue #12, D07).

Layers of read-only proof exercised here:
    1. source introspection — single ``self._redis`` chokepoint, every dispatched
       command string is in the allowlist, no write-command call syntax anywhere;
    2. the allowlist itself is checked against a canonical write denylist;
    3. a recording proxy around fakeredis logs every command issued by the full
       public API and asserts they are all reads;
    4. the runtime guard rejects non-allowlisted commands even when invoked directly.
"""

import inspect
import logging
import re
from datetime import UTC, datetime

import fakeredis
import pytest
import redis
from prometheus_client import REGISTRY

import openstudio_operator.handlers as handlers_pkg
from openstudio_operator import metrics as _metrics_module
from openstudio_operator import redis_client
from openstudio_operator.config import RedisSecretRef
from openstudio_operator.handlers import _check_redis_key_layout_for_cr
from openstudio_operator.redis_client import (
    READ_ONLY_COMMANDS,
    REQUEUED_QUEUE,
    SIMULATIONS_QUEUE,
    OperatorConfigError,
    ReadOnlyRedisClient,
    RedisClientError,
    WriteCommandForbidden,
)

NOW = 1_800_000_000.0

WRITE_COMMANDS = {
    "APPEND",
    "CONFIG",
    "DECR",
    "DEL",
    "EVAL",
    "EXEC",
    "EXPIRE",
    "FLUSHALL",
    "FLUSHDB",
    "GETDEL",
    "GETSET",
    "HDEL",
    "HMSET",
    "HSET",
    "INCR",
    "LPUSH",
    "LPOP",
    "LREM",
    "MOVE",
    "MULTI",
    "MSET",
    "PERSIST",
    "PUBLISH",
    "RENAME",
    "RPUSH",
    "RPOP",
    "SADD",
    "SCRIPT",
    "SETEX",
    "SETNX",
    "SET",
    "SPOP",
    "SREM",
    "UNLINK",
    "ZADD",
}


class RecordingRedis:
    """Proxy around a redis client that logs every dispatched method name (uppercase)."""

    def __init__(self, inner):
        self._inner = inner
        self.commands: list[str] = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.commands.append(name.upper())
            return getattr(self._inner, name)(*args, **kwargs)

        return _record


@pytest.fixture()
def fake():
    return fakeredis.FakeStrictRedis(decode_responses=True)


@pytest.fixture()
def client(fake):
    return ReadOnlyRedisClient("redis://:pw@queue.test:6379", connection=fake, now_fn=lambda: NOW)


def seed_workers(fake, heartbeats: dict[str, float | None]) -> None:
    """Seed the LIVE-VERIFIED v3.11.0 layout (issue #66): registry SET +
    heartbeat HASH (field=worker id, value=ISO8601 UTC timestamp string)."""
    for worker_id, heartbeat in heartbeats.items():
        fake.sadd("resque:workers", worker_id)
        if heartbeat is not None:
            iso = datetime.fromtimestamp(heartbeat, tz=UTC).isoformat()
            fake.hset("resque:workers:heartbeat", worker_id, iso)


# --- Queue depths --------------------------------------------------------


def test_queue_depth_reads_llen(fake, client):
    fake.rpush(SIMULATIONS_QUEUE, "job1", "job2", "job3")
    fake.rpush(REQUEUED_QUEUE, "job4")
    assert client.queue_depth(SIMULATIONS_QUEUE) == 3
    assert client.queue_depth(REQUEUED_QUEUE) == 1


def test_queue_depth_of_unknown_queue_is_zero(client):
    assert client.queue_depth("nope") == 0


def test_queue_depths_returns_both_managed_queues(fake, client):
    fake.rpush(SIMULATIONS_QUEUE, "job1")
    fake.rpush(REQUEUED_QUEUE, "job2", "job3")
    assert client.queue_depths() == {SIMULATIONS_QUEUE: 1, REQUEUED_QUEUE: 2}


# --- Worker liveness ------------------------------------------------------


def test_worker_heartbeats_returns_raw_epoch_floats(fake, client):
    seed_workers(fake, {"w1": NOW - 5.5, "w2": NOW - 600.25})
    assert client.worker_heartbeats() == {"w1": NOW - 5.5, "w2": NOW - 600.25}


def test_worker_heartbeats_missing_key_maps_to_none(fake, client):
    seed_workers(fake, {"registered-but-silent": None})
    assert client.worker_heartbeats() == {"registered-but-silent": None}


def test_worker_heartbeats_empty_registry(fake, client):
    assert client.worker_heartbeats() == {}


def test_worker_heartbeats_garbage_value_raises(fake, client):
    seed_workers(fake, {"w1": NOW})
    fake.hset("resque:workers:heartbeat", "w1", "not-a-timestamp")
    with pytest.raises(RedisClientError, match="unparseable heartbeat"):
        client.worker_heartbeats()


def test_worker_heartbeats_parses_live_iso8601_format(fake, client):
    """The live v3.11.0 hash value format, byte-for-byte as captured on kind
    (docs/kind-validation.md, issue #66 evidence): ISO8601 with UTC offset."""
    fake.sadd("resque:workers", "worker-5f49c94875-sngm2:35:requeued,simulations")
    fake.hset(
        "resque:workers:heartbeat",
        "worker-5f49c94875-sngm2:35:requeued,simulations",
        "2026-08-18T20:46:06+00:00",
    )
    beats = client.worker_heartbeats()
    (only,) = beats.values()
    assert only == datetime.fromisoformat("2026-08-18T20:46:06+00:00").timestamp()


def test_worker_heartbeats_treats_naive_timestamp_as_utc(fake, client):
    """A heartbeat string with no offset is interpreted as UTC (the Rails
    server writes UTC), never as local time."""
    fake.sadd("resque:workers", "w1")
    naive = datetime.fromtimestamp(NOW - 5, tz=UTC).replace(tzinfo=None)
    fake.hset("resque:workers:heartbeat", "w1", naive.isoformat())
    assert client.worker_heartbeats() == {"w1": NOW - 5}


def test_stale_workers_separates_fresh_from_stale(fake, client):
    seed_workers(
        fake,
        {"fresh": NOW - 5, "stale": NOW - 600, "never-beat": None},
    )
    assert client.stale_workers(threshold_seconds=300) == {"stale", "never-beat"}


def test_stale_workers_empty_when_all_fresh(fake, client):
    seed_workers(fake, {"w1": NOW - 1, "w2": NOW - 299.5})
    assert client.stale_workers(threshold_seconds=300) == set()


def test_stale_workers_boundary_exactly_at_threshold_is_not_stale(fake, client):
    seed_workers(fake, {"edge": NOW - 300})
    assert client.stale_workers(threshold_seconds=300) == set()


# --- Read-only proof: source introspection --------------------------------

SOURCE = inspect.getsource(redis_client)


def test_module_has_a_single_redis_dispatch_chokepoint():
    assert re.findall(r"self\._redis\s*\.\s*\w+", SOURCE) == []
    assert "getattr(self._redis, command.lower())" in SOURCE


def test_every_dispatched_command_string_is_in_the_allowlist():
    dispatched = re.findall(r'self\._execute\(\s*"(\w+)"', SOURCE)
    assert dispatched, "no _execute calls found — introspection pattern drifted"
    assert {command.upper() for command in dispatched} <= READ_ONLY_COMMANDS


def test_module_source_contains_no_write_command_call_syntax():
    """Guard: no Redis write command is ever invoked through any path.

    The grep deliberately excludes ``append`` — too generic; the only
    ``append`` in this module (added in #83 D2 by
    ``workers_for_analysis``) operates on a Python list of matches, not
    on a Redis client. Every other write command on the list is Redis-only.
    """
    write_call = re.compile(
        r"\.\s*(?:set|hset|hmset|hdel|lpush|rpush|lpop|rpop|lrem|sadd|srem|spop|zadd|"
        r"delete|unlink|expire|persist|pexpire|rename|move|setex|setnx|getset|getdel|"
        r"incr|decr|incrby|decrby|publish|flushall|flushdb|mset|eval|"
        r"script_load|config_set|client_setname|execute_command|pipeline)\s*\(",
        re.IGNORECASE,
    )
    assert write_call.findall(SOURCE) == []


def test_allowlist_is_exactly_the_reads_and_intersects_no_write_command():
    """Issue #83 D2 added GET for the per-worker record read; assert the
    canonical allowlist is exactly the read-only set."""
    assert READ_ONLY_COMMANDS == {"LLEN", "SMEMBERS", "HGETALL", "GET", "SCAN"}
    assert READ_ONLY_COMMANDS & WRITE_COMMANDS == set()


# --- Key layout validation (issue #44) ---------------------------------


def test_validate_key_layout_passes_when_registry_and_heartbeats_present(fake, client):
    """The full LIVE-VERIFIED v3.11.0 layout (issue #66): registry SET +
    heartbeat HASH + Resque 2.x worker:{id}:started strings."""
    fake.sadd("resque:workers", "w1", "w2")
    fake.hset("resque:workers:heartbeat", "w1", "2026-08-18T20:46:06+00:00")
    fake.hset("resque:workers:heartbeat", "w2", "2026-08-18T20:46:16+00:00")
    fake.set("resque:worker:w1:started", "2026-08-18 20:44:16 +0000")
    # Should not raise
    client.validate_key_layout()


def test_validate_key_layout_raises_when_heartbeat_hash_missing(fake, client):
    """Registry present but no heartbeat HASH — the pre-#66 failure mode: the
    live layout stores heartbeats in the HASH, so its absence means the
    centralized constants no longer match the server's Resque version."""
    fake.sadd("resque:workers", "w1")
    fake.set("resque:worker:w1:started", "2026-08-18 20:44:16 +0000")
    with pytest.raises(OperatorConfigError, match="not found"):
        client.validate_key_layout()


def test_validate_key_layout_raises_when_no_resque_keys_present(fake, client):
    fake.set("some-other-key", "x")
    with pytest.raises(OperatorConfigError, match="No Resque keys found"):
        client.validate_key_layout()


def test_validate_key_layout_raises_when_registry_key_missing(fake, client):
    """A different prefix — the live layout diverges from centralized constants."""
    # Write some resque:* keys under a different prefix so the probe finds
    # SOMETHING, but the centralized WORKER_REGISTRY_KEY is absent — proves
    # the per-key check is wired.
    fake.sadd("resque:other_thing", "w1")
    fake.set("resque:other_thing:w1", "12345.6")
    with pytest.raises(OperatorConfigError, match="not found"):
        client.validate_key_layout()


def test_operator_config_error_is_config_reexport_and_not_a_redis_error():
    """Issue #475: ``OperatorConfigError`` lives in ``config.py`` (neutral
    home); the ``redis_client`` name is a compatibility re-export of the
    SAME class, and it is no longer a ``RedisClientError`` subclass —
    callers that need it must catch it explicitly."""
    from openstudio_operator.config import OperatorConfigError as Canonical

    assert redis_client.OperatorConfigError is Canonical, (
        "redis_client.OperatorConfigError must re-export config.OperatorConfigError (#475)"
    )
    assert not issubclass(OperatorConfigError, RedisClientError), (
        "OperatorConfigError must not be catchable via except RedisClientError (#475)"
    )


def test_validate_key_layout_does_not_use_write_commands():
    """The probe must go through SCAN, never KEYS (a write-adjacent fallback)."""
    from openstudio_operator.redis_client import _redis_target_for_diagnostics

    # No password leakage: diagnostics only carry scheme://host:port/db.
    safe = _redis_target_for_diagnostics("redis://:supersecret@host:6379/2")
    assert "supersecret" not in safe
    assert "host:6379" in safe and "/2" in safe


def test_validate_key_layout_uses_only_read_only_commands(fake, client):
    """Record every command issued by validate_key_layout; assert they're all reads."""
    recorder = RecordingRedis(fake)
    validating = ReadOnlyRedisClient(
        "redis://:pw@queue.test:6379",
        connection=recorder,
        now_fn=lambda: NOW,
    )
    fake.sadd("resque:workers", "w1")
    fake.hset("resque:workers:heartbeat", "w1", "2026-08-18T20:46:06+00:00")

    validating.validate_key_layout()

    assert recorder.commands, "validate_key_layout issued no commands"
    assert set(recorder.commands) <= READ_ONLY_COMMANDS
    # SCAN is the only allowed traversal command; no KEYS, no DBSIZE, etc.
    assert "KEYS" not in recorder.commands
    assert "DBSIZE" not in recorder.commands


def test_runtime_guard_rejects_non_allowlisted_command(client):
    with pytest.raises(WriteCommandForbidden, match="read-only allowlist"):
        client._execute("set", "resque:workers", "1")


def test_recorded_command_log_of_full_public_api_is_read_only(fake):
    recorder = RecordingRedis(fake)
    client = ReadOnlyRedisClient(
        "redis://:pw@queue.test:6379", connection=recorder, now_fn=lambda: NOW
    )
    seed_workers(fake, {"fresh": NOW - 1, "stale": NOW - 600, "silent": None})
    fake.rpush(SIMULATIONS_QUEUE, "job1")

    depths = client.queue_depths()
    heartbeats = client.worker_heartbeats()
    stale = client.stale_workers(threshold_seconds=300)

    assert depths == {SIMULATIONS_QUEUE: 1, REQUEUED_QUEUE: 0}
    assert heartbeats["fresh"] == NOW - 1
    assert stale == {"stale", "silent"}
    assert set(recorder.commands) <= READ_ONLY_COMMANDS


# --- Credentials & error discipline ---------------------------------------


def test_credentials_come_only_from_redis_url():
    url = "redis://:openstudio@queue.openstudio-server.svc.cluster.local:6379"
    client = ReadOnlyRedisClient(url)
    kwargs = client._redis.connection_pool.connection_kwargs
    assert kwargs["password"] == "openstudio"
    assert kwargs["host"] == "queue.openstudio-server.svc.cluster.local"
    assert kwargs["port"] == 6379


# --- Secret-sourced URL validation (issue #463) ----------------------------


def test_secret_url_validation_accepts_credentialed_in_cluster_url():
    """Issue #463: the Secret key holds the FULL URL — credentials are the
    point of the Secret, so both the empty-user helm-recipe shape and the
    ``user:password`` shape pass the in-cluster fence."""
    from openstudio_operator.redis_client import redis_url_from_secret_value

    for good in (
        "redis://:rotated-pw@queue:6379",
        "redis://user:pass@queue.openstudio-server.svc.cluster.local:6379/1",
        "redis://queue:6379",  # credential-free is legal too (no-auth dev)
    ):
        assert redis_url_from_secret_value(good, secret_name="s", secret_key="k") == good


def test_secret_url_validation_rejects_off_cluster_and_non_redis():
    """The #390 SSRF fence is preserved on the Secret path: moving the URL
    out of the CRD-validated spec into a Secret must not become a side door
    to off-cluster hosts or non-Redis schemes."""
    from openstudio_operator.redis_client import (
        RedisCredentialResolutionError,
        redis_url_from_secret_value,
    )

    for bad in (
        "redis://:pw@attacker.example.com:6379",
        "http://queue:6379",
        "not-a-url",
        "",
    ):
        with pytest.raises(RedisCredentialResolutionError, match="openstudio-redis"):
            redis_url_from_secret_value(
                bad, secret_name="openstudio-redis", secret_key="redis-url"
            )


def test_secret_url_validation_error_names_the_secret_and_key():
    """The failure message must point the operator at the exact Secret/key
    to fix — and must NOT carry the credential itself (only the diagnostic
    scheme://host:port shape)."""
    from openstudio_operator.redis_client import (
        RedisCredentialResolutionError,
        redis_url_from_secret_value,
    )

    with pytest.raises(RedisCredentialResolutionError) as excinfo:
        redis_url_from_secret_value(
            "redis://:supersecret@attacker.example.com:6379",
            secret_name="openstudio-redis",
            secret_key="redis-url",
        )
    message = str(excinfo.value)
    assert "openstudio-redis" in message and "redis-url" in message
    assert "supersecret" not in message
    assert "attacker.example.com" in message  # host diagnostics stay visible


def test_secret_url_validation_rejects_two_label_public_hostnames_575():
    """Issue #575 headline case: ``redis://:pw@evil.com:6379`` matched the
    pre-#575 :data:`SECRET_REDIS_URL_PATTERN` because the optional second
    host label was unconstrained — the #463 Secret-sourced path would hand
    the queue password to any attacker-controlled two-label domain. With
    and without ``@creds``, with and without ports, both schemes, and the
    retired bare service.namespace form are all rejected."""
    from openstudio_operator.redis_client import (
        RedisCredentialResolutionError,
        redis_url_from_secret_value,
    )

    for bad in (
        "redis://:pw@evil.com:6379",
        "redis://evil.com",
        "redis://exfil.io:6379",
        "redis://user:pass@attacker.dev:6379",
        "rediss://:pw@evil.com:6379",
        "rediss://evil.com",
        # Retired pre-#575 legal shape: bare service.namespace, no .svc.
        "redis://:pw@queue.openstudio-server:6379",
        # Non-svc multi-label chains.
        "redis://:pw@a.b.c.d.e:6379",
    ):
        with pytest.raises(RedisCredentialResolutionError):
            redis_url_from_secret_value(bad, secret_name="s", secret_key="k")


def test_secret_url_validation_accepts_svc_short_forms_575():
    """Issue #575: the tightened grammar keeps every ``.svc``-terminated
    in-cluster shape the helm recipe and docs use — the short two-label
    ``service.svc`` form, the short FQDN ``service.svc.cluster.local``,
    and the namespaced ``service.namespace.svc[.cluster.local]`` chain,
    credentialed (the point of the Secret) and credential-free, returned
    unchanged."""
    from openstudio_operator.redis_client import redis_url_from_secret_value

    for good in (
        "redis://:pw@queue.svc:6379",
        "redis://:pw@queue.svc.cluster.local:6379",
        "redis://:pw@queue.openstudio-server.svc:6379",
        "redis://user:pass@queue.openstudio-server.svc.cluster.local:6379/1",
        "rediss://:pw@queue.svc:6379",
        "redis://queue.svc:6379",  # credential-free stays legal (no-auth dev)
    ):
        assert redis_url_from_secret_value(good, secret_name="s", secret_key="k") == good


def test_redis_credential_resolution_error_subclasses_client_error():
    """Existing ``except RedisClientError:`` call sites keep handling the
    Secret-path failures (D12 posture: skip the tick, retry next poll)."""
    from openstudio_operator.redis_client import RedisCredentialResolutionError

    assert issubclass(RedisCredentialResolutionError, RedisClientError)


# --- TLS (rediss://) support — issue #476 ------------------------------------


def test_secret_url_validation_accepts_rediss_tls_urls_476():
    """Issue #476: the #463 Secret fence accepts the TLS twin — credentialed
    (the point of the Secret) and credential-free — against the same
    in-cluster Service host shapes, returned unchanged."""
    from openstudio_operator.redis_client import redis_url_from_secret_value

    for good in (
        "rediss://:rotated-pw@queue:6379",
        "rediss://user:pass@queue.openstudio-server.svc.cluster.local:6379/1",
        "rediss://queue:6379",
    ):
        assert redis_url_from_secret_value(good, secret_name="s", secret_key="k") == good


def test_secret_url_validation_rejects_rediss_off_cluster_476():
    """The #390 SSRF fence is scheme-independent: a ``rediss://`` URL to an
    off-cluster host must be rejected exactly like the plaintext twin —
    adding TLS must not add an exfiltration side door on the Secret path."""
    from openstudio_operator.redis_client import (
        RedisCredentialResolutionError,
        redis_url_from_secret_value,
    )

    for bad in (
        "rediss://:pw@attacker.example.com:6379",
        "rediss://attacker.example.com",
        "https://queue:6379",
    ):
        with pytest.raises(RedisCredentialResolutionError):
            redis_url_from_secret_value(bad, secret_name="s", secret_key="k")


def test_plaintext_url_builds_plain_connection(monkeypatch):
    """The plaintext path is unchanged by #476: a ``redis://`` URL builds the
    plain TCP connection class with no ssl kwargs — and never even reads the
    REDIS_TLS_CA_BUNDLE env var (a garbage value there must not break a
    plaintext cluster)."""
    from redis.connection import Connection

    monkeypatch.setenv("REDIS_TLS_CA_BUNDLE", "/nonexistent/garbage.pem")
    client = ReadOnlyRedisClient("redis://queue:6379")
    pool = client._redis.connection_pool
    assert pool.connection_class is Connection
    assert not [k for k in pool.connection_kwargs if k.startswith("ssl_")]


def test_rediss_url_builds_tls_connection_with_system_cas(monkeypatch):
    """Issue #476 acceptance: a ``rediss://`` URL builds the SSL connection
    class with certificate verification REQUIRED. With no explicit bundle
    configured, no ``ssl_ca_certs`` kwarg is injected — redis-py then wraps
    the socket via ``ssl.create_default_context()`` (system trust store),
    and the operator never weakens ``ssl_cert_reqs``/hostname checking."""
    from redis.connection import SSLConnection

    monkeypatch.delenv("REDIS_TLS_CA_BUNDLE", raising=False)
    client = ReadOnlyRedisClient("rediss://queue:6379")
    pool = client._redis.connection_pool
    assert pool.connection_class is SSLConnection
    assert pool.connection_kwargs.get("host") == "queue"
    assert pool.connection_kwargs.get("port") == 6379
    assert "ssl_ca_certs" not in pool.connection_kwargs
    # Verification is never downgraded by the operator's wiring.
    assert pool.connection_kwargs.get("ssl_cert_reqs", "required") == "required"


def test_rediss_url_honors_ca_bundle_env(monkeypatch, tmp_path):
    """Issue #476 acceptance: a valid ``REDIS_TLS_CA_BUNDLE`` (readable PEM
    file) is forwarded as ``ssl_ca_certs`` so the TLS handshake validates
    against THAT bundle instead of the system trust store — the mirror of
    the REST client's ``OPENSTUDIO_TLS_CA_BUNDLE`` (issue #296)."""
    from redis.connection import SSLConnection

    bundle = tmp_path / "ca.pem"
    pem = "-----BEGIN CERTIFICATE-----\nnot-a-real-cert\n-----END CERTIFICATE-----\n"
    bundle.write_text(pem)
    monkeypatch.setenv("REDIS_TLS_CA_BUNDLE", str(bundle))

    client = ReadOnlyRedisClient("rediss://queue:6379")

    assert client._redis.connection_pool.connection_class is SSLConnection
    assert client._redis.connection_pool.connection_kwargs["ssl_ca_certs"] == str(bundle)


def test_rediss_url_empty_ca_bundle_env_means_system_cas(monkeypatch):
    """An EMPTY ``REDIS_TLS_CA_BUNDLE`` value means the system trust store —
    the same truthy-string refusal the REST client applies (#296): a
    templated boolean flag must not become a bogus CA path."""
    monkeypatch.setenv("REDIS_TLS_CA_BUNDLE", "")
    client = ReadOnlyRedisClient("rediss://queue:6379")
    assert "ssl_ca_certs" not in client._redis.connection_pool.connection_kwargs


def test_rediss_url_missing_ca_bundle_path_raises_config_error(monkeypatch):
    """Issue #476 acceptance: a ``REDIS_TLS_CA_BUNDLE`` that names no
    existing file raises ``OperatorConfigError`` from its #475 neutral home
    (``openstudio_operator.config``) — and, per the #475 hierarchy, it is
    NOT a ``RedisClientError`` (so ``except RedisClientError`` cannot
    silently swallow a TLS misconfiguration)."""
    from openstudio_operator.config import OperatorConfigError as ConfigError

    monkeypatch.setenv("REDIS_TLS_CA_BUNDLE", "/nonexistent/ca-bundle.pem")
    with pytest.raises(ConfigError, match="REDIS_TLS_CA_BUNDLE") as excinfo:
        ReadOnlyRedisClient("rediss://queue:6379")
    assert not isinstance(excinfo.value, RedisClientError)


def test_rediss_url_non_pem_ca_bundle_raises_config_error(monkeypatch, tmp_path):
    """A file that exists but carries no PEM marker is refused too (the #296
    shape) — pinning the env value to a real bundle instead of trusting any
    readable path."""
    from openstudio_operator.config import OperatorConfigError as ConfigError

    bundle = tmp_path / "not-pem.txt"
    bundle.write_text("definitely not a certificate bundle\n")
    monkeypatch.setenv("REDIS_TLS_CA_BUNDLE", str(bundle))
    with pytest.raises(ConfigError, match="PEM marker"):
        ReadOnlyRedisClient("rediss://queue:6379")


def test_redis_errors_are_wrapped_as_client_errors(fake, client, monkeypatch):
    def _boom(*args, **kwargs):
        raise redis.exceptions.ConnectionError("queue fabric unreachable")

    monkeypatch.setattr(fake, "smembers", _boom)
    with pytest.raises(RedisClientError, match="SMEMBERS failed"):
        client.worker_heartbeats()


# --- Issue #83 D2: workers_for_analysis + pod_name_for_worker -----------------


def _seed_worker_payload(fake, worker_id: str, payload_args: list | None) -> None:
    """Seed ``resque:worker:{worker_id}`` with a JSON payload.

    The live Resque 2.x layout stores the worker record as a JSON STRING
    whose shape is ``{host, pid, queues, payload, run_at}`` (issue #83 D2
    verified on kind/v3.11.0). When ``payload_args`` is None the worker
    is recorded as idle (no current job); the operator's
    ``workers_for_analysis`` must not match it.
    """
    import json as _json

    if payload_args is None:
        record = {"host": worker_id.split(":")[0], "pid": 0, "queues": []}
    else:
        record = {
            "host": worker_id.split(":")[0],
            "pid": 1,
            "queues": ["requeued", "simulations"],
            "payload": {"class": "RunSimulateDataPoint", "args": payload_args},
            "run_at": "2026-08-18T20:00:00+00:00",
        }
    fake.set(f"resque:worker:{worker_id}", _json.dumps(record))


def test_workers_for_analysis_matches_payload_args_0(fake, client):
    """Live Resque convention: ``RunSimulateDataPoint`` job passes
    ``[analysis_id, datapoint_id, ...]`` — args[0] is the analysis id."""
    fake.sadd("resque:workers", "worker-a:1:requeued,simulations", "worker-b:2:requeued,simulations")
    _seed_worker_payload(fake, "worker-a:1:requeued,simulations", ["a1", "dp1"])
    _seed_worker_payload(fake, "worker-b:2:requeued,simulations", ["a2", "dp2"])

    matches = client.workers_for_analysis("a1")

    assert matches == ["worker-a:1:requeued,simulations"]


def test_workers_for_analysis_matches_any_arg_position(fake, client):
    """Defensive: a future job class may rearrange args — the helper
    matches ``analysis_id`` in any position, not just ``args[0]``."""
    fake.sadd("resque:workers", "worker-x:1:requeued,simulations")
    _seed_worker_payload(
        fake, "worker-x:1:requeued,simulations", ["dp1", "a1", "options-hash"]
    )

    matches = client.workers_for_analysis("a1")

    assert matches == ["worker-x:1:requeued,simulations"]


def test_workers_for_analysis_skips_idle_workers(fake, client):
    """A worker with no current payload (``payload: null``) is never a match."""
    fake.sadd("resque:workers", "worker-idle:1:requeued,simulations", "worker-busy:2:requeued,simulations")
    _seed_worker_payload(fake, "worker-idle:1:requeued,simulations", None)
    _seed_worker_payload(fake, "worker-busy:2:requeued,simulations", ["a1"])

    matches = client.workers_for_analysis("a1")

    assert matches == ["worker-busy:2:requeued,simulations"]


def test_workers_for_analysis_skips_workers_with_no_record(fake, client):
    """A registered worker whose ``resque:worker:{id}`` STRING is absent
    is treated as idle (not an error). The helper skips it gracefully —
    fakeredis returns ``None`` for an unset key, and we accept that as
    "no payload, no match"."""
    fake.sadd("resque:workers", "worker-ghost:1:requeued,simulations")
    # No _seed_worker_payload call for worker-ghost.

    matches = client.workers_for_analysis("a1")

    assert matches == []


def test_workers_for_analysis_raises_on_garbage_record(fake, client):
    """Unparseable JSON in a worker's record is loud, never silently absent.

    The escalation path is safety-critical; registry garbage must surface
    as :class:`RedisClientError` so the SLA tick skips the tick (D12) and
    the next poll retries naturally. A swallowed failure here would
    silently no-match and never escalate."""

    fake.sadd("resque:workers", "worker-bad:1:requeued,simulations")
    fake.set("resque:worker:worker-bad:1:requeued,simulations", "not-valid-json{")

    with pytest.raises(RedisClientError, match="unparseable worker record"):
        client.workers_for_analysis("a1")


def test_workers_for_analysis_empty_registry(fake, client):
    assert client.workers_for_analysis("a1") == []


def test_pod_name_for_worker_extracts_hostname_segment():
    """Live Resque 2.x: ``{hostname}:{pid}:{queues}`` — first segment IS the
    K8s pod name (pods default ``hostname`` to the pod name)."""
    from openstudio_operator.redis_client import ReadOnlyRedisClient

    assert (
        ReadOnlyRedisClient.pod_name_for_worker(
            ReadOnlyRedisClient, "worker-5f49c94875-sngm2:35:requeued,simulations"
        )
        == "worker-5f49c94875-sngm2"
    )


def test_pod_name_for_worker_returns_none_for_malformed_id():
    """Defensive: a worker id with fewer than 3 colon segments (or an
    empty first segment) returns ``None`` — the caller must skip, not
    mistakenly try to delete a pod named ``''`` or ``':'``."""
    from openstudio_operator.redis_client import ReadOnlyRedisClient

    assert ReadOnlyRedisClient.pod_name_for_worker(ReadOnlyRedisClient, "too-few-segments") is None
    assert ReadOnlyRedisClient.pod_name_for_worker(ReadOnlyRedisClient, ":1:queues") is None
    assert ReadOnlyRedisClient.pod_name_for_worker(ReadOnlyRedisClient, "") is None


def test_workers_for_analysis_uses_only_read_only_commands(fake):
    """The D2 helper must dispatch only allowlisted commands. SMEMBERS
    reads the registry; GET reads each per-worker record. Both are
    strictly reads — never a write command."""
    recorder = RecordingRedis(fake)
    client = ReadOnlyRedisClient(
        "redis://:pw@queue.test:6379", connection=recorder, now_fn=lambda: NOW
    )
    fake.sadd("resque:workers", "worker-a:1:requeued,simulations")
    _seed_worker_payload(fake, "worker-a:1:requeued,simulations", ["a1"])

    client.workers_for_analysis("a1")

    assert set(recorder.commands) <= READ_ONLY_COMMANDS
    assert "SMEMBERS" in recorder.commands
    assert "GET" in recorder.commands


# --- allowlist change (issue #83 D2 adds GET) --------------------------------


def test_allowlist_includes_get_for_worker_records():
    """Issue #83 D2 added GET to the read-only allowlist so the per-worker
    record read at ``resque:worker:{worker_id}`` can be served by the
    dispatch chokepoint. This test is a regression guard for the
    allowlist mutation."""
    assert "GET" in READ_ONLY_COMMANDS
    assert "GET" not in WRITE_COMMANDS  # GET is strictly a read command


# --- Issue #163 — boot-time validate_key_layout() structured log lines -------
#
# The operator's entrypoint (handlers/__init__.py:_check_redis_key_layout_for_cr)
# runs ``validate_key_layout()`` per CR at boot and emits a structured log
# line ``redis_key_layout=ok|degraded|unreachable``. This is the loud-fail
# invariant the issue set out to fix: a Redis layout drift (helm chart
# upgrade to a Resque-2.x-with-different-prefix layout, or a different queue
# backend entirely) would otherwise only be noticed downstream when the
# worker-registry gauges go silent. The tests below pin the log line on each
# branch so a regression (no log line, wrong status, no try/except) fails
# the CI gate. They exercise the operator's wiring (not the redis_client
# itself), but live here because the underlying contract is the redis
# layout's behavior and the surrounding log fixture is keyed off
# ``ReadOnlyRedisClient``.


def _patched_client_factory(fake, monkeypatch):
    """Return a factory that bypasses the live Redis URL and uses ``fake``.

    ``_check_redis_key_layout_for_cr`` closes over
    ``get_read_only_redis_client`` at import time (issue #235 — the boot-time
    check no longer constructs ``ReadOnlyRedisClient`` inline). Patching the
    factory symbol at ``openstudio_operator.handlers`` (the import site)
    redirects the construction to a stub that uses the test ``fake``
    fakeredis client — the same pattern kopf tests use in the wider codebase.
    """

    def _factory(redis_url: str, **_kwargs: object) -> ReadOnlyRedisClient:
        return ReadOnlyRedisClient(
            redis_url, connection=fake, now_fn=lambda: NOW
        )

    monkeypatch.setattr(
        "openstudio_operator.handlers.get_read_only_redis_client", _factory
    )
    return _factory


def test_redis_key_layout_check_emits_ok_log_line(
    fake, client, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boot-time check emits ``redis_key_layout=ok`` when the layout matches.

    Seeds the live-verified v3.11.0 layout (registry SET + heartbeat HASH,
    issue #66) and asserts the ``_check_redis_key_layout_for_cr`` helper
    logs ``redis_key_layout=ok namespace=... name=...`` at INFO level. The
    status string is the operator's contract for downstream log/alert
    pipelines — a regression in the message format breaks observability.
    """
    fake.sadd("resque:workers", "w1", "w2")
    fake.hset("resque:workers:heartbeat", "w1", "2026-08-18T20:46:06+00:00")
    fake.hset("resque:workers:heartbeat", "w2", "2026-08-18T20:46:16+00:00")

    _patched_client_factory(fake, monkeypatch)

    item = {
        "metadata": {"namespace": "test-ns", "name": "test-osc"},
        "spec": {"redisUrl": "redis://:pw@queue.test:6379"},
    }

    with caplog.at_level(logging.INFO, logger="openstudio_operator.handlers"):
        status = _check_redis_key_layout_for_cr(
            item, logger=logging.getLogger("openstudio_operator.handlers")
        )

    assert status == "ok", (
        f"Expected status='ok' on a valid layout; got {status!r}. "
        f"Captured: {caplog.text!r}. See issue #163."
    )
    assert "redis_key_layout=ok" in caplog.text, (
        f"Expected structured log line 'redis_key_layout=ok' not emitted. "
        f"Captured: {caplog.text!r}. See issue #163."
    )
    assert "namespace=test-ns" in caplog.text
    assert "name=test-osc" in caplog.text


def test_redis_key_layout_check_emits_degraded_log_line(
    fake, client, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boot-time check emits ``redis_key_layout=degraded`` when layout drifts.

    The synthetic Redis is empty (no Resque keys present) — the operator
    points at a Redis with a different prefix entirely. The check must
    emit ``redis_key_layout=degraded`` at WARNING level AND queue a
    ``RedisKeyLayoutDrift`` Warning Event for the next OSCM watch tick
    (drained by the consolidated ``_drain_queued_warning_events`` after #234).
    Both invariants are
    load-bearing: the log line is the operator's alert signal, the
    Warning Event is the user-facing Kubernetes signal.
    """
    # No Resque keys — the layout has drifted.
    _patched_client_factory(fake, monkeypatch)

    item = {
        "metadata": {"namespace": "test-ns", "name": "test-osc"},
        "spec": {"redisUrl": "redis://:pw@queue.test:6379"},
    }

    # Clear the queue from any prior test in this session (#234 — the
    # three module-level queues collapsed into one QueuedKopfEventSink).
    handlers_pkg._sink.clear()

    with caplog.at_level(logging.INFO, logger="openstudio_operator.handlers"):
        status = _check_redis_key_layout_for_cr(
            item, logger=logging.getLogger("openstudio_operator.handlers")
        )

    assert status == "degraded", (
        f"Expected status='degraded' on a missing-Resque-key layout; got "
        f"{status!r}. Captured: {caplog.text!r}. See issue #163."
    )
    assert "redis_key_layout=degraded" in caplog.text, (
        f"Expected structured log line 'redis_key_layout=degraded' not "
        f"emitted. Captured: {caplog.text!r}. See issue #163."
    )
    assert "reason=layout_drift" in caplog.text
    assert "namespace=test-ns" in caplog.text
    assert "name=test-osc" in caplog.text

    # Warning Event queued for the next OSCM watch tick (#163, #234).
    pending = [
        msg for msg in handlers_pkg._sink.queued
        if msg[0] == "test-ns" and msg[1] == "test-osc"
    ]
    assert len(pending) == 1, (
        f"Expected exactly one queued RedisKeyLayoutDrift event; got "
        f"{len(pending)}. The Warning Event was not queued — the "
        f"user-facing Kubernetes signal is missing. See issue #163."
    )
    _ns, _nm, reason, message = pending[0]
    assert reason == "RedisKeyLayoutDrift", (
        f"Expected reason='RedisKeyLayoutDrift'; got {reason!r}. See issue #163."
    )
    assert "issue #163" in message or "issue #44" in message, (
        f"Expected the drained message to link the operator on-call to the "
        f"issue tracker entry; got {message!r}. See issue #163."
    )


def test_redis_key_layout_check_emits_unreachable_log_line(
    fake, client, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boot-time check emits ``redis_key_layout=unreachable`` on connectivity failure.

    A Redis connectivity failure (refused, DNS, timeout) MUST NOT crash the
    boot — the operator continues in degraded mode and retries on the next
    tick. The check must emit ``redis_key_layout=unreachable`` at WARNING
    level, queue NO Warning Event (we don't know the layout drifted; we
    just couldn't reach the server), and not raise. This is the test that
    guarantees the try/except invariant — without it, a Redis outage at
    boot would prevent the operator from starting.
    """
    import redis as _redis

    def _factory(redis_url: str, **_kwargs: object) -> ReadOnlyRedisClient:
        client = ReadOnlyRedisClient(
            redis_url, connection=fake, now_fn=lambda: NOW
        )

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise _redis.exceptions.ConnectionError(
                "Connection refused at boot"
            )

        # SCAN is the very first call validate_key_layout() makes; forcing
        # it to raise with a real RedisError reproduces the "server
        # unreachable at boot" path. ReadOnlyRedisClient wraps that as
        # RedisClientError, which the check catches.
        monkeypatch.setattr(fake, "scan", _boom)
        return client

    monkeypatch.setattr(
        "openstudio_operator.handlers.get_read_only_redis_client", _factory
    )

    item = {
        "metadata": {"namespace": "test-ns", "name": "test-osc"},
        "spec": {"redisUrl": "redis://:pw@queue.test:6379"},
    }

    handlers_pkg._sink.clear()

    with caplog.at_level(logging.INFO, logger="openstudio_operator.handlers"):
        # MUST NOT raise — this is the "operator continues to boot" invariant.
        status = _check_redis_key_layout_for_cr(
            item, logger=logging.getLogger("openstudio_operator.handlers")
        )

    assert status == "unreachable", (
        f"Expected status='unreachable' on a connectivity failure; got "
        f"{status!r}. Captured: {caplog.text!r}. See issue #163."
    )
    assert "redis_key_layout=unreachable" in caplog.text, (
        f"Expected structured log line 'redis_key_layout=unreachable' not "
        f"emitted. Captured: {caplog.text!r}. See issue #163."
    )

    # No Warning Event on the connectivity branch — we don't know the layout
    # drifted; we just couldn't reach the server. Alerting on this would
    # produce noise during outages unrelated to the queue fabric.
    pending = [
        msg for msg in handlers_pkg._sink.queued
        if msg[0] == "test-ns" and msg[1] == "test-osc"
    ]
    assert not pending, (
        f"Expected NO queued event on the connectivity branch; got {pending!r}. "
        f"See issue #163 — the operator must not false-positive on outages."
    )


def test_redis_key_layout_check_skips_empty_redis_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Empty ``spec.redisUrl`` is a separate concern (#116); the check skips.

    The operator's intentional refusal to default ``spec.redisUrl`` is
    enforced elsewhere (issue #116). The boot-time check must NOT
    short-circuit on it: the empty URL is the URL-guard's territory, not
    the layout-validator's. Skipping is the correct response — calling
    ``ReadOnlyRedisClient("")`` would raise a ValueError, which the
    try/except would absorb as "unreachable", misleadingly.
    """
    item = {
        "metadata": {"namespace": "test-ns", "name": "test-osc"},
        "spec": {"redisUrl": ""},
    }

    with caplog.at_level(logging.INFO, logger="openstudio_operator.handlers"):
        status = _check_redis_key_layout_for_cr(
            item, logger=logging.getLogger("openstudio_operator.handlers")
        )

    assert status == "skipped", (
        f"Expected status='skipped' on empty redisUrl; got {status!r}. "
        f"Captured: {caplog.text!r}. See issue #163."
    )
    # Skipped is a DEBUG-level signal so the operator's logs aren't noisy on
    # unconfigured CRs (#116); the test asserts the absence of the warning
    # structured log line.
    assert "redis_key_layout=degraded" not in caplog.text
    assert "redis_key_layout=unreachable" not in caplog.text
    assert "redis_key_layout=ok" not in caplog.text


@pytest.mark.parametrize(
    ("redis_url", "spec_id"),
    [
        ("", "secret-ref-only"),
        ("redis://:stale-inline-pw@queue.test:6379", "secret-ref-wins-over-inline"),
    ],
)
def test_redis_key_layout_check_runs_via_secret_ref(
    fake,
    client,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    redis_url: str,
    spec_id: str,
) -> None:
    """Issue #567: a secretRef CR VALIDATES instead of returning ``skipped``.

    Pre-#567 the check read ``spec.redisUrl`` directly: a secretRef-only
    CR (``redisUrl`` empty — the #463 preferred production shape) was
    "skipped" forever, so the boot-time and #490 periodic layout
    validation never ran for exactly the CRs most likely to be
    misconfigured. The factory is patched (the ``_patched_client_factory``
    pattern) to a capturing stub backed by fakeredis seeded with the
    valid v3.11.0 layout, so the test asserts BOTH that validation runs
    (status ``ok``, not ``skipped``) AND that the check passes the parsed
    secretRef + the CR's namespace into the factory (the #567 wiring
    contract; the Secret resolution itself is covered in
    ``tests/test_client_factory.py``).
    """
    fake.sadd("resque:workers", "w1")
    fake.hset("resque:workers:heartbeat", "w1", "2026-08-18T20:46:06+00:00")

    seen: dict[str, object] = {}

    def _factory(redis_url_arg: str, *, secret_ref=None, namespace=""):
        seen["redis_url"] = redis_url_arg
        seen["secret_ref"] = secret_ref
        seen["namespace"] = namespace
        return ReadOnlyRedisClient(
            # The resolved URL is irrelevant to the stub — the fakeredis
            # connection is the data source; a non-empty URL keeps the
            # constructor's URL parsing happy on the secret-ref-only spec.
            redis_url_arg or "redis://queue.test:6379",
            connection=fake,
            now_fn=lambda: NOW,
        )

    monkeypatch.setattr(
        "openstudio_operator.handlers.get_read_only_redis_client", _factory
    )

    item = {
        "metadata": {"namespace": "test-ns", "name": "test-osc"},
        "spec": {
            "redisUrl": redis_url,
            "redisCredentials": {
                "secretRef": {"name": "openstudio-redis", "key": "redis-url"}
            },
        },
    }

    with caplog.at_level(logging.INFO, logger="openstudio_operator.handlers"):
        status = _check_redis_key_layout_for_cr(
            item, logger=logging.getLogger("openstudio_operator.handlers")
        )

    assert status == "ok", (
        f"Expected status='ok' ({spec_id} spec) — validation must RUN for a "
        f"secretRef CR, not return 'skipped'; got {status!r}. Captured: "
        f"{caplog.text!r}. See issue #567."
    )
    assert "redis_key_layout=ok" in caplog.text
    assert seen["secret_ref"] == RedisSecretRef(name="openstudio-redis", key="redis-url"), (
        f"the check must pass the parsed secret_ref into the factory; got "
        f"{seen['secret_ref']!r}. See issue #567."
    )
    assert seen["namespace"] == "test-ns", (
        f"the check must pass the CR's namespace for the Secret read; got "
        f"{seen['namespace']!r}. See issue #567."
    )


def test_redis_key_layout_check_secret_ref_resolution_failure_is_unreachable(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #567: a secretRef that cannot resolve lands on ``unreachable``, not a crash.

    The function must stay total (never raise — the #490 rider rides a
    stall tick on it): a resolution failure raises
    ``RedisCredentialResolutionError``, a ``RedisClientError`` subclass,
    so the existing wire-level branch absorbs it and the log line names
    the Secret to fix.
    """
    from openstudio_operator.redis_client import RedisCredentialResolutionError

    def _factory(redis_url_arg: str, *, secret_ref=None, namespace=""):
        raise RedisCredentialResolutionError(
            "cannot read Secret test-ns/openstudio-redis (issue #463): 404 Not Found"
        )

    monkeypatch.setattr(
        "openstudio_operator.handlers.get_read_only_redis_client", _factory
    )

    item = {
        "metadata": {"namespace": "test-ns", "name": "test-osc"},
        "spec": {
            "redisUrl": "",
            "redisCredentials": {
                "secretRef": {"name": "openstudio-redis", "key": "redis-url"}
            },
        },
    }

    with caplog.at_level(logging.INFO, logger="openstudio_operator.handlers"):
        # MUST NOT raise — totality is the #163/#490 invariant.
        status = _check_redis_key_layout_for_cr(
            item, logger=logging.getLogger("openstudio_operator.handlers")
        )

    assert status == "unreachable"
    assert "redis_key_layout=unreachable" in caplog.text
    assert "openstudio-redis" in caplog.text


def test_redis_key_layout_check_skips_nameless_item(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A CR-shaped object with no metadata.namespace/name must be skipped, not crash.

    Defensive: a malformed item (e.g. a partial body delivered by a future
    kopf version change) must not crash the boot. The check returns
    ``'skipped'`` and emits no structured log line.
    """
    item = {"spec": {"redisUrl": "redis://:pw@queue.test:6379"}}  # no metadata

    with caplog.at_level(logging.INFO, logger="openstudio_operator.handlers"):
        status = _check_redis_key_layout_for_cr(
            item, logger=logging.getLogger("openstudio_operator.handlers")
        )

    assert status == "skipped", (
        f"Expected status='skipped' on a nameless item; got {status!r}. "
        f"Captured: {caplog.text!r}. See issue #163."
    )


def test_consolidated_drain_handler_emits_redis_key_layout_drift_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consolidated drain handler emits the queued Warning Event and clears the queue.

    After #234 the three module-level queues + drain handlers collapsed
    into :class:`openstudio_operator.events_sinks.QueuedKopfEventSink`;
    the drain handler is now a single ``@kopf.on.event`` wrapper that
    delegates to :meth:`QueuedKopfEventSink.flush_for`. This test pins
    the invariant that the drain still produces a single ``kopf.event``
    call per queued entry and an empty queue is a no-op. Wiring
    regression here means the Warning Event is silently dropped — the
    user-facing Kubernetes signal is lost.
    """
    import openstudio_operator.events_sinks as sinks_module

    handlers_pkg._sink.clear()
    handlers_pkg._sink.defer_to_next_tick(
        namespace="test-ns", name="test-osc",
        reason="RedisKeyLayoutDrift", message="drift detected",
    )

    emitted: list[dict] = []

    def _fake_event(*args, **kwargs):
        emitted.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(sinks_module.kopf, "event", _fake_event)

    handlers_pkg._sink.flush_for(namespace="test-ns", name="test-osc")

    assert len(emitted) == 1, (
        f"Expected exactly one kopf.event call; got {len(emitted)}. "
        f"See issue #163 — the Warning Event was not emitted."
    )
    event = emitted[0]
    assert event["kwargs"]["type"] == "Warning"
    assert event["kwargs"]["reason"] == "RedisKeyLayoutDrift"
    assert event["kwargs"]["message"] == "drift detected"
    assert handlers_pkg._sink.queued == [], (
        f"Queue was not cleared after drain; still has "
        f"{handlers_pkg._sink.queued!r}. See issue #163 / #234."
    )

    # Subsequent drain is a no-op.
    handlers_pkg._sink.flush_for(namespace="test-ns", name="test-osc")
    assert len(emitted) == 1, (
        f"Second drain emitted another event: {emitted!r}. See issue #163 — "
        f"the drain must be idempotent."
    )


# --- Issue #490 — key-layout freshness gauge stamped on every run --------------
#
# The #253 status gauge carries the validator RESULT; the #490 freshness
# pair carries WHEN the validator last ran. Both are set in lockstep by
# ``handlers/_set_redis_key_layout_status`` at every terminal path of
# ``_check_redis_key_layout_for_cr`` — the tests below pin the lockstep
# on the success path and on representative failure/skip paths, so a
# future branch added without the helper fails CI instead of silently
# reintroducing the blind-holds-value risk #490 fixed.

_FRESH_SAMPLE = "openstudio_operator_redis_key_layout_status_fresh"
_STATUS_SAMPLE = "openstudio_operator_redis_key_layout_status"


def _freshness_value() -> float:
    return REGISTRY.get_sample_value(_FRESH_SAMPLE) or 0.0


def test_redis_key_layout_check_stamps_freshness_gauge_on_ok(
    fake, client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #490: the ``ok`` path stamps the freshness gauge, not just the status.

    Pre-sets the freshness gauge to a pre-epoch sentinel so the assertion
    proves THIS call re-stamped it (an untouched gauge would keep the
    sentinel), then runs the seeded-valid-layout check and asserts the
    stamp advanced to a plausible wall-clock epoch.
    """
    fake.sadd("resque:workers", "w1")
    fake.hset("resque:workers:heartbeat", "w1", "2026-08-18T20:46:06+00:00")
    _patched_client_factory(fake, monkeypatch)

    _metrics_module.REDIS_KEY_LAYOUT_STATUS_FRESH.set(1.0)  # pre-epoch sentinel
    item = {
        "metadata": {"namespace": "test-ns", "name": "test-osc"},
        "spec": {"redisUrl": "redis://:pw@queue.test:6379"},
    }
    status = _check_redis_key_layout_for_cr(
        item, logger=logging.getLogger("openstudio_operator.handlers")
    )

    assert status == "ok"
    assert REGISTRY.get_sample_value(_STATUS_SAMPLE) == 1.0
    fresh = _freshness_value()
    assert fresh > 1_000_000_000.0, (
        f"Freshness gauge not re-stamped by the ok run: {fresh!r}. The #490 "
        f"pair must advance on EVERY validation run — see "
        f"handlers/_set_redis_key_layout_status."
    )


@pytest.mark.parametrize(
    "scenario",
    ["degraded", "unreachable", "skipped"],
)
def test_redis_key_layout_check_stamps_freshness_gauge_on_non_ok_paths(
    fake, client, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """Issue #490: every non-``ok`` terminal path stamps freshness in lockstep.

    Fresh means "recently validated" — the STATUS gauge carries the result
    (0.0 on all three paths), the FRESH gauge carries that a run happened
    at all. Without the stamp on these paths, a cluster whose validation
    persistently fails would read as "stale validation" (the ``time() -
    fresh`` alert firing) when the real problem is the carried 0.0 — two
    alerts telling one story confusingly.
    """
    item = {
        "metadata": {"namespace": "test-ns", "name": "test-osc"},
        "spec": {"redisUrl": "redis://:pw@queue.test:6379"},
    }
    if scenario == "skipped":
        item = {
            "metadata": {"namespace": "test-ns", "name": "test-osc"},
            "spec": {"redisUrl": ""},
        }
    elif scenario == "unreachable":
        import redis as _redis

        def _factory(redis_url: str, **_kwargs: object) -> ReadOnlyRedisClient:
            probe_client = ReadOnlyRedisClient(
                redis_url, connection=fake, now_fn=lambda: NOW
            )

            def _boom(*_args: object, **_kwargs: object) -> None:
                raise _redis.exceptions.ConnectionError("Connection refused")

            monkeypatch.setattr(fake, "scan", _boom)
            return probe_client

        monkeypatch.setattr(
            "openstudio_operator.handlers.get_read_only_redis_client", _factory
        )
    else:  # degraded — empty fakeredis (no Resque keys) drifts the layout.
        _patched_client_factory(fake, monkeypatch)
        handlers_pkg._sink.clear()

    _metrics_module.REDIS_KEY_LAYOUT_STATUS_FRESH.set(1.0)  # pre-epoch sentinel
    with caplog.at_level(logging.INFO, logger="openstudio_operator.handlers"):
        status = _check_redis_key_layout_for_cr(
            item, logger=logging.getLogger("openstudio_operator.handlers")
        )

    assert status == scenario, f"scenario setup drift: got {status!r}"
    assert REGISTRY.get_sample_value(_STATUS_SAMPLE) == 0.0
    fresh = _freshness_value()
    assert fresh > 1_000_000_000.0, (
        f"Freshness gauge not re-stamped by the {scenario} run: {fresh!r}. "
        f"The #490 pair must advance on EVERY validation run (fresh means "
        f"recently validated; the status gauge carries the result)."
    )


# --- Issue #488 — Redis request-duration histogram -------------------------


def _sample_count(histogram, **labels) -> float:
    """Read a labelled Histogram's observation count (the ``_count`` child)."""
    child = histogram.labels(**labels)
    samples = getattr(child, "_child_samples", None)
    if samples is not None:
        return float(next(s.value for s in samples() if s.name == "_count"))
    return float(child._count.get())  # type: ignore[attr-defined]  # older client shape


def test_queue_depths_observe_llen_duration(fake, client):
    """Issue #488 acceptance: the ``queue_depths`` happy path observes the
    Redis request-duration histogram — one LLEN observation per managed
    queue (two), so the sub-ms-to-ms Redis round-trips are visible at
    /metrics instead of hiding inside an inflated tick-duration bucket."""
    from openstudio_operator import metrics

    before = _sample_count(metrics.REDIS_REQUEST_DURATION_SECONDS, operation="llen")
    client.queue_depths()
    after = _sample_count(metrics.REDIS_REQUEST_DURATION_SECONDS, operation="llen")
    assert after - before >= 2, "expected one llen observation per managed queue"


def test_worker_heartbeats_observe_smembers_duration(fake, client):
    """Issue #488 acceptance: ``worker_heartbeats`` observes under its
    dominant operation label (``smembers`` — the SMEMBERS + HGETALL pair
    is timed once, per the issue's label vocabulary). ``stale_workers``
    delegates here, so its delegation is asserted to observe too."""
    from openstudio_operator import metrics

    seed_workers(fake, {"worker-1:1:simulations": NOW - 5, "worker-2:2:simulations": NOW - 5})
    before = _sample_count(metrics.REDIS_REQUEST_DURATION_SECONDS, operation="smembers")
    assert client.worker_heartbeats() == {
        "worker-1:1:simulations": NOW - 5,
        "worker-2:2:simulations": NOW - 5,
    }
    assert client.stale_workers(threshold_seconds=60) == set()
    after = _sample_count(metrics.REDIS_REQUEST_DURATION_SECONDS, operation="smembers")
    # worker_heartbeats() + the stale_workers() delegation: >= 2 observations.
    assert after - before >= 2


def test_validate_key_layout_observe_scan_duration(fake, client):
    """Issue #488 acceptance: the ``validate_key_layout`` happy path observes
    under the ``scan`` operation label (the whole SCAN loop is the timed
    network surface)."""
    from openstudio_operator import metrics

    seed_workers(fake, {"worker-1:1:simulations": NOW - 5})
    before = _sample_count(metrics.REDIS_REQUEST_DURATION_SECONDS, operation="scan")
    client.validate_key_layout()
    after = _sample_count(metrics.REDIS_REQUEST_DURATION_SECONDS, operation="scan")
    assert after - before >= 1


def test_failed_llen_still_observes_duration():
    """Issue #488: duration is duration — the observation fires on the
    FAILURE path too (the error itself is counted by the tick-failure
    counters; this histogram only times)."""
    from openstudio_operator import metrics

    class _BrokenLlen:
        def llen(self, *args, **kwargs):
            raise redis.ConnectionError("socket timeout")

        def __getattr__(self, name):
            raise AttributeError(name)

    client = ReadOnlyRedisClient(
        "redis://:pw@queue.test:6379", connection=_BrokenLlen(), now_fn=lambda: NOW
    )
    before = _sample_count(metrics.REDIS_REQUEST_DURATION_SECONDS, operation="llen")
    with pytest.raises(RedisClientError):
        client.queue_depth(SIMULATIONS_QUEUE)
    after = _sample_count(metrics.REDIS_REQUEST_DURATION_SECONDS, operation="llen")
    assert after - before == 1
