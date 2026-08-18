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
    SCAN      one-shot key-prefix probe for the startup layout validator
              (:meth:`validate_key_layout`, issue #44). SCAN is non-blocking,
              read-only, and never used outside the validator — the steady-state
              registry discovery still goes through the fixed ``SMEMBERS`` of
              ``WORKER_REGISTRY_KEY`` (no traversal).

Key layout (per the verified contract / issue #12; centralized as constants so the
kind-cluster validation pass (#44) can adjust prefixes in one place if the live Resque
version differs — DEFAULT IS RESQUE CONVENTION; adjust only after live evidence):
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
``redis.RedisError`` is wrapped in ``RedisClientError`` for callers. Operator
configuration mismatches (wrong DB, wrong prefix — what ``validate_key_layout`` is
designed to catch) raise :class:`OperatorConfigError` instead, which is a
subclass so existing callers that catch ``RedisClientError`` continue to handle it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import urlparse

import redis

READ_ONLY_COMMANDS: frozenset[str] = frozenset({"LLEN", "SMEMBERS", "GET", "SCAN"})

SIMULATIONS_QUEUE = "simulations"
REQUEUED_QUEUE = "requeued"
#: Resque convention key for the worker-id set. Adjust here after live v3.11.0
#: validation (issue #44) — this is the single point of change.
WORKER_REGISTRY_KEY = "resque:workers"

#: Hard cap on how many keys :meth:`validate_key_layout` will SCAN before giving
#: up; protects against accidental full-DB traversal on a misconfigured prefix.
VALIDATE_SCAN_KEY_BUDGET = 1000


def _heartbeat_key(worker_id: str) -> str:
    return f"{WORKER_REGISTRY_KEY}:{worker_id}"


def _redis_target_for_diagnostics(redis_url: str) -> str:
    """Strip credentials from ``redis://:password@host:port/db`` for error messages.

    The operator never logs the URL itself (it carries the password in plain text);
    diagnostics surface only ``scheme://host:port/db`` — enough for an operator to
    tell which cluster/DB a misconfiguration is talking to, never enough to leak a
    credential.
    """
    parsed = urlparse(redis_url)
    host = parsed.hostname or "<unknown>"
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    path = parsed.path or ""
    return f"{parsed.scheme}://{netloc}{path}"


class RedisClientError(RuntimeError):
    """Raised when a read against the Redis queue fabric fails or returns garbage."""


class OperatorConfigError(RedisClientError):
    """Raised when the operator's configured key layout does not match the live Redis.

    Issue #44: the centralized key constants (``WORKER_REGISTRY_KEY`` etc.) are
    inferred from Resque convention and may not match the real v3.11.0 layout.
    :meth:`ReadOnlyRedisClient.validate_key_layout` raises this on the first call
    so a misconfiguration fails LOUD at operator startup, not silently at the
    first stall-condition evaluation. Subclasses :class:`RedisClientError` so the
    existing ``except RedisClientError:`` call sites continue to handle it.
    """


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
        self._redis_url = redis_url
        self._redis_target = _redis_target_for_diagnostics(redis_url)
        self._redis = connection or redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=socket_timeout_seconds,
            socket_connect_timeout=socket_timeout_seconds,
        )
        self._now = now_fn

    def _execute(self, command: str, *args: str, **kwargs: object) -> object:
        """Single dispatch chokepoint: allowlist assert, then call, then error wrap."""
        if command.upper() not in READ_ONLY_COMMANDS:
            raise WriteCommandForbidden(
                f"{command!r} is not in the read-only allowlist {sorted(READ_ONLY_COMMANDS)}"
            )
        method = getattr(self._redis, command.lower())
        try:
            return method(*args, **kwargs)
        except redis.RedisError as exc:
            raise RedisClientError(f"{command.upper()} failed: {exc}") from exc

    # --- Key layout validation (issue #44) ---------------------------------

    def validate_key_layout(self) -> None:
        """One-shot probe of the Resque key layout — call at operator startup.

        Uses ``SCAN MATCH resque:*`` to walk the keyspace without blocking the
        Redis server, then asserts the centralized constants are consistent with
        what the live v3.11.0 server actually writes. Raises
        :class:`OperatorConfigError` (which subclasses :class:`RedisClientError`)
        when the layout diverges, so a misconfiguration fails LOUD at boot
        rather than silently at the first stall-condition evaluation.

        Designed to be called ONCE per operator process; subsequent calls are
        idempotent (re-probe, no caching). The probe is capped at
        :data:`VALIDATE_SCAN_KEY_BUDGET` keys so a misconfigured prefix cannot
        turn the startup probe into a full-keyspace traversal.

        Failure modes (each → :class:`OperatorConfigError`):

        * zero ``resque:*`` keys exist — the DB is empty or the operator is
          pointing at the wrong Redis DB (``redis_url`` selects a different
          logical DB than the server writes; the most common footgun);
        * ``resque:*`` keys exist but ``WORKER_REGISTRY_KEY`` is absent — the
          live Resque version uses a different prefix; update the constant.

        Empty-but-non-broken steady state (Resque workers present and
        heartbeating) will trivially pass this check: the registry SET and at
        least one heartbeat key are always present when the queue fabric is
        live. A cold start before workers register will trip this; that's the
        intended LOUD behavior — fix the configuration, don't paper over it.
        """
        seen: set[str] = set()
        cursor: int | str = 0
        try:
            while True:
                cursor, batch = self._execute(
                    "scan",
                    cursor,
                    match=f"{WORKER_REGISTRY_KEY.rsplit(':', 1)[0]}:*",
                    count=100,
                )
                seen.update(batch)
                if int(cursor) == 0 or len(seen) >= VALIDATE_SCAN_KEY_BUDGET:
                    break
        except redis.RedisError as exc:
            raise RedisClientError(f"SCAN failed: {exc}") from exc

        if not seen:
            raise OperatorConfigError(
                f"No Resque keys found at {self._redis_target} matching "
                f"prefix '{WORKER_REGISTRY_KEY.rsplit(':', 1)[0]}:*'. "
                f"The operator's redis_url may select a different logical DB than "
                f"the server writes to, or the live v3.11.0 Resque uses a different "
                f"prefix. Verify against the live Redis with "
                f"`redis-cli -u <redis_url> KEYS 'resque:*'` and update "
                f"WORKER_REGISTRY_KEY in src/openstudio_operator/redis_client.py."
            )
        if WORKER_REGISTRY_KEY not in seen:
            sample = sorted(seen)[:5]
            raise OperatorConfigError(
                f"Expected Resque worker registry key {WORKER_REGISTRY_KEY!r} "
                f"not found at {self._redis_target}; observed prefix keys: "
                f"{sample}{'...' if len(seen) > 5 else ''}. Update "
                f"WORKER_REGISTRY_KEY in src/openstudio_operator/redis_client.py "
                f"to match the live v3.11.0 Resque layout (#44)."
            )

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
