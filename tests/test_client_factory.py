"""Tests for the centralized :mod:`openstudio_operator.client_factory` (issue #168).

Covers the three behaviours the four former per-handler ``_client_cache``
blocks used to provide independently: cache hit (same URL ⇒ same object),
invalidation on ``spec.serverUrl`` mutation / per-CR isolation (different URL
⇒ different object), and an explicit ``cache_clear()`` escape hatch.
"""

from __future__ import annotations

import pytest

from openstudio_operator.client_factory import (
    CLIENT_CACHE_MAXSIZE,
    get_openstudio_client,
)
from openstudio_operator.openstudio_client import OpenStudioClient

BASE = "http://web.openstudio-server.svc.cluster.local"
OTHER = "http://web-2.openstudio-server.svc.cluster.local"


@pytest.fixture(autouse=True)
def _clear_cache():
    """Every case starts and ends with an empty process-level cache."""
    get_openstudio_client.cache_clear()
    yield
    get_openstudio_client.cache_clear()


def test_returns_openstudio_client():
    assert isinstance(get_openstudio_client(BASE), OpenStudioClient)


def test_cache_hit_returns_same_instance():
    """Repeated calls with the same URL reuse one client (and its session)."""
    first = get_openstudio_client(BASE)
    second = get_openstudio_client(BASE)

    assert first is second
    assert get_openstudio_client.cache_info().hits == 1


def test_cache_miss_creates_new_instance():
    """A mutated ``spec.serverUrl`` is a new cache key ⇒ a fresh client.

    This is the invalidation path: no stale client is ever handed out for a
    URL the CR no longer points at. It is also the per-CR isolation
    guarantee — two CRs on different servers never share a client.
    """
    first = get_openstudio_client(BASE)
    second = get_openstudio_client(OTHER)

    assert first is not second
    assert get_openstudio_client.cache_info().misses == 2


def test_cache_clear_works():
    """``cache_clear()`` drops every cached client — the test-only escape hatch."""
    first = get_openstudio_client(BASE)

    get_openstudio_client.cache_clear()
    second = get_openstudio_client(BASE)

    assert first is not second
    assert get_openstudio_client.cache_info().currsize == 1


def test_shared_across_handler_modules():
    """All handler modules resolve the same object for the same server URL.

    Before #168 each handler kept its own ``_client_cache``, so four separate
    clients existed per server. The import is now a single symbol.
    """
    from openstudio_operator import prune_entrypoint
    from openstudio_operator.handlers import (
        analysis_sla,
        datapoint_watchdog,
        worker_recycler,
    )

    modules = (analysis_sla, datapoint_watchdog, worker_recycler, prune_entrypoint)
    for module in modules:
        assert module.get_openstudio_client is get_openstudio_client

    clients = {id(module.get_openstudio_client(BASE)) for module in modules}
    assert len(clients) == 1


def test_cache_is_bounded():
    """The LRU is bounded — a churning URL cannot grow the cache without limit."""
    for index in range(CLIENT_CACHE_MAXSIZE + 3):
        get_openstudio_client(f"{BASE}:{index}")

    assert get_openstudio_client.cache_info().currsize == CLIENT_CACHE_MAXSIZE
