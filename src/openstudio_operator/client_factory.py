"""Single source of truth for :class:`OpenStudioClient` construction (issue #168).

Before this module, ``analysis_sla``, ``datapoint_watchdog``,
``worker_recycler`` and ``retention`` each open-coded a module-level
``_client_cache`` dict plus a private ``_get_client()`` factory. The retry /
backoff / tz-aware-UTC behaviour always lived inside
:class:`~openstudio_operator.openstudio_client.OpenStudioClient` and was
reused correctly, but the *wiring* — where the cache lives, how it is keyed,
how it is invalidated — was duplicated four times. Adding a per-request
timeout, a latency metric, or a cache eviction policy meant editing four
files in lockstep.

Cache semantics (D04: cache-only, never operator state)
-------------------------------------------------------
The cache is a :func:`functools.lru_cache` keyed by ``server_url`` alone:

* **Cache hit** — repeated calls with the same URL return the *same* client
  object, so the underlying ``requests.Session`` connection pool and retry
  state are reused across ticks and across handler modules.
* **Invalidation on ``spec.serverUrl`` mutation** — a mutated URL is simply a
  different cache key, so the next tick transparently builds a fresh client
  against the new host. No explicit eviction hook is needed, and no stale
  client is ever handed out for a URL the CR no longer points at.
* **Per-CR isolation** — two CRs (or two namespaces) pointing at different
  servers get distinct clients; two CRs pointing at the *same* server
  deliberately share one, because the client holds no per-CR state (all
  operator memory lives in the CR ``.status`` subresource — D04).

``maxsize=8`` is generous for the "exactly one OSCM CR per namespace" (D05)
deployment model while still bounding growth if a URL churns.

Tests call :func:`get_openstudio_client.cache_clear` to get a clean process.
"""

from __future__ import annotations

from functools import lru_cache

from openstudio_operator.openstudio_client import OpenStudioClient

#: Number of distinct ``server_url`` values kept alive at once.
CLIENT_CACHE_MAXSIZE = 8


@lru_cache(maxsize=CLIENT_CACHE_MAXSIZE)
def get_openstudio_client(server_url: str) -> OpenStudioClient:
    """Return the cached :class:`OpenStudioClient` for ``server_url``.

    Cached by ``server_url`` so repeated calls reuse the same client (and its
    connection pool / retry state). A changed ``spec.serverUrl`` yields a new
    cache key and therefore a new client — that *is* the invalidation path.
    Tests can call ``get_openstudio_client.cache_clear()`` between cases.
    """
    return OpenStudioClient(server_url)
