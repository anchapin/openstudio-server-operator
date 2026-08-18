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
    HGETALL   one-shot fetch of the worker-heartbeat hash (all fields at once —
              the live v3.11.0 Resque stores every worker's heartbeat as a field
              of a single HASH key, not per-worker STRING keys)
    SCAN      one-shot key-prefix probe for the startup layout validator
              (:meth:`validate_key_layout`, issue #44). SCAN is non-blocking,
              read-only, and never used outside the validator — the steady-state
              registry discovery still goes through the fixed ``SMEMBERS`` of
              ``WORKER_REGISTRY_KEY`` (no traversal).

Key layout (LIVE-VERIFIED 2026-08-18 on the kind validation cluster running
``nrel/openstudio-server:3.11.0`` — Resque 2.x, see ``docs/kind-validation.md``
"Resque layout validation checklist" for the captured redis-cli evidence):
    resque:workers                SET of registered worker ids
                                  (``{hostname}:{pid}:{queues}``)
    resque:workers:heartbeat      HASH — field = worker id, value = heartbeat
                                  timestamp as an ISO8601 string with UTC offset
                                  (e.g. ``2026-08-18T20:46:06+00:00``), rewritten
                                  by each worker roughly every 60 s
    resque:worker:{id}:started    STRING — human-readable first-registration time
                                  (informational; the operator does not read it)

NOTE the drift this fixed (issue #66): the pre-live constants assumed Resque 1.x
convention (``resque:workers:{id}`` STRING keys holding ``Time.now.to_f`` epoch
floats). Against the live v3.11.0 Redis those per-worker keys DO NOT EXIST —
``GET`` returned nil for every registered worker, which would have mapped every
worker to "maximally stale" and made the web_background stall condition's leg B
fire on a perfectly healthy fleet.

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
from datetime import UTC, datetime
from urllib.parse import urlparse

import redis

READ_ONLY_COMMANDS: frozenset[str] = frozenset({"LLEN", "SMEMBERS", "HGETALL", "SCAN"})

SIMULATIONS_QUEUE = "simulations"
REQUEUED_QUEUE = "requeued"
#: Resque worker-id registry (SET). Live-verified on kind/v3.11.0 (issue #66):
#: the v3.11.0 server (Resque 2.x) keeps the registry at this exact key.
WORKER_REGISTRY_KEY = "resque:workers"
#: Resque worker-heartbeat hash (HASH field=worker id → ISO8601 UTC timestamp
#: string, refreshed ~every 60 s per worker). Live-verified on kind/v3.11.0
#: (issue #66). Pre-live code assumed Resque 1.x per-worker STRING keys
#: (``resque:workers:{id}`` holding epoch floats) — those do not exist on the
#: live server; this hash is the single point of change.
WORKER_HEARTBEAT_HASH_KEY = "resque:workers:heartbeat"

#: Hard cap on how many keys :meth:`validate_key_layout` will SCAN before giving
#: up; protects against accidental full-DB traversal on a misconfigured prefix.
VALIDATE_SCAN_KEY_BUDGET = 1000


def _parse_heartbeat(raw: str, worker_id: str) -> float:
    """Parse a live heartbeat value (ISO8601 timestamp string) to an epoch float.

    Live v3.11.0 format: ``2026-08-18T20:46:06+00:00`` (always carries a UTC
    offset in practice). A naive string (no offset) is interpreted as UTC — the
    Rails server writes UTC. Anything unparseable raises :class:`RedisClientError`
    (registry garbage should be loud, never silently fresh).
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise RedisClientError(
            f"unparseable heartbeat for worker {worker_id!r}: {raw!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


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
          live Resque version uses a different registry prefix;
        * the registry is present but ``WORKER_HEARTBEAT_HASH_KEY`` is absent —
          the live Resque version stores heartbeats somewhere else (pre-live
          code assumed Resque 1.x per-worker keys; the live v3.11.0 layout is
          the heartbeat HASH — live-verified issue #66).

        Steady state (Resque workers present and heartbeating) passes trivially:
        the registry SET and the heartbeat HASH are both present whenever the
        queue fabric is live. A cold start before workers register will trip
        this; that's the intended LOUD behavior — fix the configuration, don't
        paper over it.
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
        missing: list[str] = [
            key
            for key in (WORKER_REGISTRY_KEY, WORKER_HEARTBEAT_HASH_KEY)
            if key not in seen
        ]
        if missing:
            sample = sorted(seen)[:5]
            raise OperatorConfigError(
                f"Expected Resque key(s) {missing} not found at "
                f"{self._redis_target}; observed prefix keys: "
                f"{sample}{'...' if len(seen) > 5 else ''}. The centralized "
                f"constants in src/openstudio_operator/redis_client.py "
                f"(WORKER_REGISTRY_KEY / WORKER_HEARTBEAT_HASH_KEY) do not match "
                f"the live layout — see the live-capture procedure in "
                f"docs/kind-validation.md (#44/#66)."
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

        Reads the worker registry (SMEMBERS ``resque:workers``) then the heartbeat
        hash (HGETALL ``resque:workers:heartbeat`` — live-verified v3.11.0 layout,
        one HASH field per worker). A registered worker with no hash field maps to
        None — treated as maximally stale by ``stale_workers``. An unparseable
        heartbeat value raises ``RedisClientError`` (registry garbage should be
        loud, never silently fresh).
        """
        worker_ids = self._execute("smembers", WORKER_REGISTRY_KEY)
        if not worker_ids:
            return {}
        beats: dict[str, str] = self._execute("hgetall", WORKER_HEARTBEAT_HASH_KEY)
        heartbeats: dict[str, float | None] = {}
        for worker_id in sorted(worker_ids):
            raw = beats.get(worker_id)
            if raw is None:
                heartbeats[worker_id] = None
                continue
            heartbeats[worker_id] = _parse_heartbeat(raw, worker_id)
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
