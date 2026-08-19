"""Single source of truth for operator client construction (issues #168, #235).

Two clients live here, each behind an ``lru_cache`` keyed by its URL:
:class:`~openstudio_operator.openstudio_client.OpenStudioClient` (the REST
client, issue #168) and
:class:`~openstudio_operator.redis_client.ReadOnlyRedisClient` (the read-only
queue-fabric client, issue #235).

Before this module, ``analysis_sla``, ``datapoint_watchdog``,
``worker_recycler`` and ``retention`` each open-coded a module-level
``_client_cache`` dict plus a private ``_get_client()`` factory. The retry /
backoff / tz-aware-UTC behaviour always lived inside
:class:`~openstudio_operator.openstudio_client.OpenStudioClient` and was
reused correctly, but the *wiring* — where the cache lives, how it is keyed,
how it is invalidated — was duplicated four times. Adding a per-request
timeout, a latency metric, or a cache eviction policy meant editing four
files in lockstep.

Issue #235 closed the same gap for the Redis client, which had drifted into
*two* divergent patterns plus one direct construction:
``web_background_monitor._get_redis_client`` kept a module-level
``_redis_client_cache: dict[str, ReadOnlyRedisClient]``;
``analysis_sla._default_redis_client`` built a fresh client every tick (no
caching at all, so each SLA tick opened a new connection pool); and
``handlers/__init__.py`` constructed ``ReadOnlyRedisClient(redis_url)``
inline for the boot-time Resque key-layout check (#163). All three now route
through :func:`get_read_only_redis_client`, so the boot-time layout probe and
every handler tick share one connection pool per Redis URL. The
"exactly one construction site" invariant is pinned at the source level by
``tests/test_client_factory.py::test_only_one_read_only_redis_client_construction_point``.

Cache semantics (D04: cache-only, never operator state)
-------------------------------------------------------
Each cache is a :func:`functools.lru_cache` keyed by its URL alone
(``server_url`` for the REST client, ``redis_url`` for the Redis client):

* **Cache hit** — repeated calls with the same URL return the *same* client
  object, so the underlying ``requests.Session`` / ``redis.Redis`` connection
  pool and retry state are reused across ticks and across handler modules.
* **Invalidation on ``spec.serverUrl`` / ``spec.redisUrl`` mutation** — a
  mutated URL is simply a different cache key, so the next tick transparently
  builds a fresh client against the new host. No explicit eviction hook is
  needed, and no stale client is ever handed out for a URL the CR no longer
  points at.
* **Per-CR isolation** — two CRs (or two namespaces) pointing at different
  servers get distinct clients; two CRs pointing at the *same* server
  deliberately share one, because the clients hold no per-CR state (all
  operator memory lives in the CR ``.status`` subresource — D04). Neither
  client caches *query results*, so a shared instance cannot leak one CR's
  view of the world into another's.

``maxsize=8`` is generous for the "exactly one OSCM CR per namespace" (D05)
deployment model while still bounding growth if a URL churns.

An empty URL is never cached: ``lru_cache`` does not memoize raised
exceptions, so the ``ValueError`` that ``ReadOnlyRedisClient("")`` raises on
the #116 empty-``spec.redisUrl`` path keeps raising on every call.

Tests call :func:`get_openstudio_client.cache_clear` /
:func:`get_read_only_redis_client.cache_clear` to get a clean process.
"""

from __future__ import annotations

from functools import lru_cache

from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.redis_client import ReadOnlyRedisClient

#: Number of distinct ``server_url`` values kept alive at once.
CLIENT_CACHE_MAXSIZE = 8

#: Number of distinct ``redis_url`` values kept alive at once (issue #235).
#: Sized to match :data:`CLIENT_CACHE_MAXSIZE` — the two caches are keyed off
#: sibling CRD fields (``spec.serverUrl`` / ``spec.redisUrl``) and churn
#: together.
REDIS_CLIENT_CACHE_MAXSIZE = 8


@lru_cache(maxsize=CLIENT_CACHE_MAXSIZE)
def get_openstudio_client(server_url: str) -> OpenStudioClient:
    """Return the cached :class:`OpenStudioClient` for ``server_url``.

    Cached by ``server_url`` so repeated calls reuse the same client (and its
    connection pool / retry state). A changed ``spec.serverUrl`` yields a new
    cache key and therefore a new client — that *is* the invalidation path.
    Tests can call ``get_openstudio_client.cache_clear()`` between cases.
    """
    return OpenStudioClient(server_url)


@lru_cache(maxsize=REDIS_CLIENT_CACHE_MAXSIZE)
def get_read_only_redis_client(redis_url: str) -> ReadOnlyRedisClient:
    """Return the cached :class:`ReadOnlyRedisClient` for ``redis_url`` (issue #235).

    The operator's ONLY :class:`ReadOnlyRedisClient` construction site — the
    boot-time Resque key-layout probe (#163), the SLA monitor's escalation
    worker lookup (#83 D2) and the ``web_background`` stall detector (#13)
    all resolve through here, so one connection pool per Redis URL serves
    the whole process instead of the pre-#235 mix of one cached client, one
    fresh-per-tick client and one inline boot-time client.

    Read-only-ness is a property of the client itself (the
    ``READ_ONLY_COMMANDS`` allowlist enforced in
    :meth:`ReadOnlyRedisClient._execute`), not of this factory: sharing an
    instance cannot widen the command surface. Construction is lazy —
    ``redis.Redis.from_url`` opens no socket — so a cache miss never blocks
    on the network.

    A changed ``spec.redisUrl`` yields a new cache key and therefore a new
    client, which is the invalidation path (#116: an empty URL is refused
    upstream and never reaches this factory). Tests can call
    ``get_read_only_redis_client.cache_clear()`` between cases.
    """
    return ReadOnlyRedisClient(redis_url)
