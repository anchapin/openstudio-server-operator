"""Read-only Redis client for the OpenStudio Server queue fabric (D07, issue #12).

The queue fabric behind OpenStudio Server is Redis (Service ``queue`` :6379) carrying
the Resque queues ``simulations`` and ``requeued`` (workers consume
``QUEUES=requeued,simulations``). The operator reads it directly and STRICTLY
read-only — this client is a shared dependency of Module 5 (#13) and the HPA-floor
adjuster (#18). No write command may exist in this module; enforcement is layered:

* every Redis call funnels through ``_execute()``, which asserts the command against
  ``READ_ONLY_COMMANDS`` before dispatching (runtime guard raising
  ``WriteCommandForbidden``);
* the module never accesses ``self._redis`` outside that single chokepoint
  (``getattr(self._redis, ...)`` appears exactly once);
* tests introspect this module's source and a recorded fakeredis command log to prove
  every issued command is in the allowlist (grep-proof by construction — no write
  command name appears in this file at all).

Why each read is present (and nothing else):
    LLEN      queue depth for ``simulations``/``requeued`` — backlog signal for the
              watchdog and the HPA floor
    SMEMBERS  Resque worker-registry discovery; the registry lives at one fixed key,
              so no KEYS/SCAN traversal is needed or permitted
    GET       per-worker heartbeat fetch

Key layout (per the verified contract / issue #12; centralized as constants so the
kind-cluster validation pass (#19) can adjust prefixes in one place if the live Resque
version differs):
    resque:workers               SET of registered worker ids
    resque:workers:{worker_id}   heartbeat — unix-epoch float (Resque's Time.now.to_f)
                                 stored as a string

Liveness design decision: ``worker_heartbeats()`` returns the RAW epoch floats
(worker id -> float, or None when a registered worker has no heartbeat key) so callers
can apply their own policy, and ``stale_workers(threshold_seconds)`` adds the judgment.
The threshold is a policy value passed by the caller (CRD spec / ``config.py`` at the
wiring site), never hardcoded here (AGENTS.md).

Credentials come ONLY from ``redis_url`` (``redis://:password@host:port`` is parsed by
``redis.Redis.from_url``); the client accepts no auth parameters of its own.

Error discipline: no in-client retry — Redis reads ride the same ~30s poll cadence as
the REST client, so a failed tick is skipped and the next poll retries naturally.
``redis.RedisError`` is wrapped in ``RedisClientError`` for callers.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import redis

READ_ONLY_COMMANDS: frozenset[str] = frozenset({"LLEN", "SMEMBERS", "GET"})

SIMULATIONS_QUEUE = "simulations"
REQUEUED_QUEUE = "requeued"
WORKER_REGISTRY_KEY = "resque:workers"


def _heartbeat_key(worker_id: str) -> str:
    return f"{WORKER_REGISTRY_KEY}:{worker_id}"


class RedisClientError(RuntimeError):
    """Raised when a read against the Redis queue fabric fails or returns garbage."""


class WriteCommandForbidden(RuntimeError):
    """Raised when a command outside ``READ_ONLY_COMMANDS`` reaches the dispatch chokepoint.

    Unreachable through the public API by construction; exists so that a future method
    attempting a write trips tests (and the guard) instead of the cluster.
    """


class ReadOnlyRedisClient:
    """Read-only view of the Resque/Redis queue fabric.

    ``redis_url`` is the only credential source (parsed by ``redis.Redis.from_url``).
    ``connection`` injects a pre-built client (fakeredis in tests); it MUST be created
    with ``decode_responses=True`` like the URL path. ``now_fn`` supplies the epoch
    clock for staleness judgment (injected for deterministic tests).
    """

    def __init__(
        self,
        redis_url: str,
        *,
        socket_timeout_seconds: float = 5.0,
        connection: redis.Redis | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._redis = connection or redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=socket_timeout_seconds,
            socket_connect_timeout=socket_timeout_seconds,
        )
        self._now = now_fn

    def _execute(self, command: str, *args: str) -> object:
        """Single dispatch chokepoint: allowlist assert, then call, then error wrap."""
        if command.upper() not in READ_ONLY_COMMANDS:
            raise WriteCommandForbidden(
                f"{command!r} is not in the read-only allowlist {sorted(READ_ONLY_COMMANDS)}"
            )
        method = getattr(self._redis, command.lower())
        try:
            return method(*args)
        except redis.RedisError as exc:
            raise RedisClientError(f"{command.upper()} failed: {exc}") from exc

    # --- Queue depths (LLEN) ----------------------------------------------

    def queue_depth(self, queue: str) -> int:
        """LLEN of a Resque queue key — 0 for unknown/empty queues."""
        return int(self._execute("llen", queue))

    def queue_depths(self) -> dict[str, int]:
        """LLEN of both managed Resque queues: ``{simulations: n, requeued: n}``."""
        return {
            SIMULATIONS_QUEUE: self.queue_depth(SIMULATIONS_QUEUE),
            REQUEUED_QUEUE: self.queue_depth(REQUEUED_QUEUE),
        }

    # --- Worker liveness (SMEMBERS + GET) ---------------------------------

    def worker_heartbeats(self) -> dict[str, float | None]:
        """Raw Resque worker heartbeats: worker id -> unix-epoch float (sorted by id).

        Reads the worker registry (SMEMBERS ``resque:workers``) then each heartbeat value
        (GET ``resque:workers:{id}``). A registered worker whose heartbeat key is
        missing maps to None — treated as maximally stale by ``stale_workers``. An
        unparseable heartbeat value raises ``RedisClientError`` (registry garbage
        should be loud, never silently fresh).
        """
        worker_ids = self._execute("smembers", WORKER_REGISTRY_KEY)
        heartbeats: dict[str, float | None] = {}
        for worker_id in sorted(worker_ids):
            raw = self._execute("get", _heartbeat_key(worker_id))
            if raw is None:
                heartbeats[worker_id] = None
                continue
            try:
                heartbeats[worker_id] = float(raw)
            except ValueError as exc:
                raise RedisClientError(
                    f"unparseable heartbeat for worker {worker_id!r}: {raw!r}"
                ) from exc
        return heartbeats

    def stale_workers(self, threshold_seconds: float) -> set[str]:
        """Worker ids whose heartbeat is missing or older than ``threshold_seconds``.

        A heartbeat exactly ``threshold_seconds`` old is NOT stale (strict inequality).
        The threshold is caller policy (CRD spec at the wiring site), never a default
        stored here.
        """
        now = self._now()
        return {
            worker_id
            for worker_id, heartbeat in self.worker_heartbeats().items()
            if heartbeat is None or (now - heartbeat) > threshold_seconds
        }
