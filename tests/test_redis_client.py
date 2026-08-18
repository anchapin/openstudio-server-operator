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
import re

import fakeredis
import pytest
import redis

from openstudio_operator import redis_client
from openstudio_operator.redis_client import (
    READ_ONLY_COMMANDS,
    ReadOnlyRedisClient,
    RedisClientError,
    WriteCommandForbidden,
)

NOW = 1_800_000_000.0

WRITE_COMMANDS = {
    "APPEND", "CONFIG", "DECR", "DEL", "EVAL", "EXEC", "EXPIRE", "FLUSHALL", "FLUSHDB",
    "GETDEL", "GETSET", "HDEL", "HMSET", "HSET", "INCR", "LPUSH", "LPOP", "LREM",
    "MOVE", "MULTI", "MSET", "PERSIST", "PUBLISH", "RENAME", "RPUSH", "RPOP", "SADD",
    "SCRIPT", "SETEX", "SETNX", "SET", "SPOP", "SREM", "UNLINK", "ZADD",
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
    for worker_id, heartbeat in heartbeats.items():
        fake.sadd("resque:workers", worker_id)
        if heartbeat is not None:
            fake.set(f"resque:workers:{worker_id}", str(heartbeat))


# --- Queue depths --------------------------------------------------------


def test_queue_depth_reads_llen(fake, client):
    fake.rpush("simulations", "job1", "job2", "job3")
    fake.rpush("requeued", "job4")
    assert client.queue_depth("simulations") == 3
    assert client.queue_depth("requeued") == 1


def test_queue_depth_of_unknown_queue_is_zero(client):
    assert client.queue_depth("nope") == 0


def test_queue_depths_returns_both_managed_queues(fake, client):
    fake.rpush("simulations", "job1")
    fake.rpush("requeued", "job2", "job3")
    assert client.queue_depths() == {"simulations": 1, "requeued": 2}


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
    fake.set("resque:workers:w1", "not-a-float")
    with pytest.raises(RedisClientError, match="unparseable heartbeat"):
        client.worker_heartbeats()


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
    write_call = re.compile(
        r"\.\s*(?:set|hset|hmset|hdel|lpush|rpush|lpop|rpop|lrem|sadd|srem|spop|zadd|"
        r"delete|unlink|expire|persist|pexpire|rename|move|setex|setnx|getset|getdel|"
        r"append|incr|decr|incrby|decrby|publish|flushall|flushdb|mset|eval|"
        r"script_load|config_set|client_setname|execute_command|pipeline)\s*\(",
        re.IGNORECASE,
    )
    assert write_call.findall(SOURCE) == []


def test_allowlist_is_exactly_the_three_reads_and_intersects_no_write_command():
    assert READ_ONLY_COMMANDS == {"LLEN", "SMEMBERS", "GET"}
    assert READ_ONLY_COMMANDS & WRITE_COMMANDS == set()


def test_runtime_guard_rejects_non_allowlisted_command(client):
    with pytest.raises(WriteCommandForbidden, match="read-only allowlist"):
        client._execute("set", "resque:workers", "1")


def test_recorded_command_log_of_full_public_api_is_read_only(fake):
    recorder = RecordingRedis(fake)
    client = ReadOnlyRedisClient(
        "redis://:pw@queue.test:6379", connection=recorder, now_fn=lambda: NOW
    )
    seed_workers(fake, {"fresh": NOW - 1, "stale": NOW - 600, "silent": None})
    fake.rpush("simulations", "job1")

    depths = client.queue_depths()
    heartbeats = client.worker_heartbeats()
    stale = client.stale_workers(threshold_seconds=300)

    assert depths == {"simulations": 1, "requeued": 0}
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


def test_redis_errors_are_wrapped_as_client_errors(fake, client, monkeypatch):
    def _boom(*args, **kwargs):
        raise redis.exceptions.ConnectionError("queue fabric unreachable")

    monkeypatch.setattr(fake, "smembers", _boom)
    with pytest.raises(RedisClientError, match="SMEMBERS failed"):
        client.worker_heartbeats()
