"""Tests for the centralized :mod:`openstudio_operator.client_factory` (issues #168, #235).

Covers the three behaviours the four former per-handler ``_client_cache``
blocks used to provide independently: cache hit (same URL ⇒ same object),
invalidation on ``spec.serverUrl`` mutation / per-CR isolation (different URL
⇒ different object), and an explicit ``cache_clear()`` escape hatch.

Issue #235 extends the same contract to
:func:`~openstudio_operator.client_factory.get_read_only_redis_client`, which
replaced three divergent Redis construction sites
(``web_background_monitor._get_redis_client``'s module-level dict,
``analysis_sla._default_redis_client``'s fresh-per-tick client, and the inline
``ReadOnlyRedisClient(redis_url)`` in the boot-time key-layout check). The AST
gate ``test_only_one_read_only_redis_client_construction_point`` pins the
"exactly one construction site" invariant at the source level, mirroring
``tests/test_singleton_registry_coverage.py::test_only_one_custom_objects_api_construction_point``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from openstudio_operator import client_factory
from openstudio_operator.client_factory import (
    CLIENT_CACHE_MAXSIZE,
    REDIS_CLIENT_CACHE_MAXSIZE,
    get_openstudio_client,
    get_read_only_redis_client,
)
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.redis_client import ReadOnlyRedisClient

BASE = "http://web.openstudio-server.svc.cluster.local"
OTHER = "http://web-2.openstudio-server.svc.cluster.local"

REDIS_URL = "redis://:pw@queue.openstudio-server.svc.cluster.local:6379"
OTHER_REDIS_URL = "redis://:pw@queue-2.openstudio-server.svc.cluster.local:6379"


@pytest.fixture(autouse=True)
def _clear_cache():
    """Every case starts and ends with an empty process-level cache."""
    get_openstudio_client.cache_clear()
    get_read_only_redis_client.cache_clear()
    yield
    get_openstudio_client.cache_clear()
    get_read_only_redis_client.cache_clear()


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


# --- Issue #235 — the ReadOnlyRedisClient half of the factory ------------------
#
# Before #235 the Redis client had drifted into three shapes:
# ``web_background_monitor._get_redis_client`` (module-level dict cache),
# ``analysis_sla._default_redis_client`` (fresh client — and therefore a fresh
# connection pool — on every SLA tick), and an inline
# ``ReadOnlyRedisClient(redis_url)`` in ``handlers/__init__.py`` for the
# boot-time Resque key-layout probe (#163). The cases below pin the same
# contract the REST half already had, plus the AST "one construction site"
# gate at the bottom.
#
# Construction is safe in tests without a live Redis: ``redis.Redis.from_url``
# is lazy (no socket until the first command), so none of these cases touch
# the network.


def test_returns_read_only_redis_client():
    assert isinstance(get_read_only_redis_client(REDIS_URL), ReadOnlyRedisClient)


def test_redis_cache_hit_returns_same_instance():
    """Repeated calls with the same URL reuse one client (and its connection pool).

    This is the behaviour ``analysis_sla._default_redis_client`` lacked: it
    built a new ``redis.Redis`` — and therefore a new connection pool — on
    every SLA tick.
    """
    first = get_read_only_redis_client(REDIS_URL)
    second = get_read_only_redis_client(REDIS_URL)

    assert first is second
    assert get_read_only_redis_client.cache_info().hits == 1


def test_redis_cache_miss_creates_new_instance():
    """A mutated ``spec.redisUrl`` is a new cache key ⇒ a fresh client.

    Same invalidation path as the REST half: no stale client is handed out
    for a Redis URL the CR no longer points at (which matters more here —
    the URL carries the password, so a rotated credential must not keep
    resolving to the old connection).
    """
    first = get_read_only_redis_client(REDIS_URL)
    second = get_read_only_redis_client(OTHER_REDIS_URL)

    assert first is not second
    assert get_read_only_redis_client.cache_info().misses == 2


def test_redis_cache_clear_works():
    """``cache_clear()`` drops every cached client — the test-only escape hatch."""
    first = get_read_only_redis_client(REDIS_URL)

    get_read_only_redis_client.cache_clear()
    second = get_read_only_redis_client(REDIS_URL)

    assert first is not second
    assert get_read_only_redis_client.cache_info().currsize == 1


def test_redis_cache_is_bounded():
    """The LRU is bounded — a churning Redis URL cannot grow the cache forever."""
    for index in range(REDIS_CLIENT_CACHE_MAXSIZE + 3):
        get_read_only_redis_client(f"{REDIS_URL}/{index}")

    assert get_read_only_redis_client.cache_info().currsize == REDIS_CLIENT_CACHE_MAXSIZE


def test_redis_shared_across_handler_modules():
    """Every Redis callsite resolves the same object for the same URL.

    Before #235 a running operator held (at least) three distinct clients per
    Redis URL: the stall detector's cached one, a fresh one per SLA tick, and
    the boot-time layout probe's. The import is now a single symbol.
    """
    from openstudio_operator import handlers
    from openstudio_operator.handlers import analysis_sla, web_background_monitor

    modules = (handlers, analysis_sla, web_background_monitor)
    for module in modules:
        assert module.get_read_only_redis_client is get_read_only_redis_client

    clients = {id(module.get_read_only_redis_client(REDIS_URL)) for module in modules}
    assert len(clients) == 1


def test_empty_redis_url_is_not_cached():
    """The #116 empty-``spec.redisUrl`` refusal keeps raising on every call.

    ``lru_cache`` does not memoize exceptions, so wrapping the constructor in
    a cache cannot turn the empty-URL ``ValueError`` into a one-shot failure
    that later silently returns a half-built client.
    """
    with pytest.raises(ValueError):
        get_read_only_redis_client("")
    with pytest.raises(ValueError):
        get_read_only_redis_client("")

    assert get_read_only_redis_client.cache_info().currsize == 0


# --- Issue #235 — exactly one ``ReadOnlyRedisClient()`` construction site ------
#
# The AST analogue of
# ``tests/test_singleton_registry_coverage.py::test_only_one_custom_objects_api_construction_point``.
# Bypassing the factory means the callsite gets its own connection pool and
# is silently left behind by any future change to the factory (per-command
# timeout, a latency metric, a cache-eviction policy). This gate fails loudly
# with the offending file + line so the maintainer can fix it in one read.


def _find_read_only_redis_client_constructions() -> list[tuple[str, int]]:
    """Return ``(relative_path, lineno)`` for every ``ReadOnlyRedisClient(...)`` call.

    Walks the operator's production source tree
    (``src/openstudio_operator/``), parses each ``.py`` file with :mod:`ast`,
    and locates ``Call`` nodes whose function is a bare
    ``ReadOnlyRedisClient`` name. Line numbers come from the parsed AST so
    they survive future source edits (the alternative — regex on raw bytes —
    would silently miss ``ReadOnlyRedisClient ( )`` and other whitespace
    variations).

    Unlike the ``CustomObjectsApi()`` scan, arg-bearing calls are NOT skipped:
    the factory itself passes ``redis_url``, and so does every inline
    construction worth catching, so filtering on the signature would blind
    the gate to exactly the regression it exists to catch. Bare-name matching
    also means a qualified ``redis_client.ReadOnlyRedisClient(...)`` would
    slip through — that spelling never appears in this codebase, and the
    import-site convention (``from ... import ReadOnlyRedisClient``) is what
    the whole package uses.

    Tests are out of scope by construction (they live under ``tests/``, not
    under the scanned ``src/`` root): fixtures legitimately build clients
    with injected ``connection=``/``now_fn=`` fakes.
    """
    src_root = Path(client_factory.__file__).parent
    found: list[tuple[str, int]] = []
    for py in sorted(src_root.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Name) or func.id != "ReadOnlyRedisClient":
                continue
            found.append((str(py.relative_to(src_root.parent)), node.lineno))
    return found


def test_only_one_read_only_redis_client_construction_point() -> None:
    """Issue #235: ``ReadOnlyRedisClient(...)`` is constructed in EXACTLY one place.

    :func:`~openstudio_operator.client_factory.get_read_only_redis_client` is
    the operator's only legitimate construction site. Any inline
    ``ReadOnlyRedisClient(redis_url)`` outside the factory is a regression:
    the callsite gets a private connection pool, and a future change to the
    factory (per-command timeout, latency metric, cache-eviction policy)
    silently leaves it behind — the exact three-way drift #235 collapsed.
    """
    found = _find_read_only_redis_client_constructions()

    assert len(found) == 1, (
        f"Expected exactly ONE ReadOnlyRedisClient(...) construction in "
        f"src/openstudio_operator/; found {len(found)}: {found}. Every "
        f"callsite must use "
        f"openstudio_operator.client_factory.get_read_only_redis_client() "
        f"instead of constructing the client inline. See issue #235."
    )

    path, lineno = found[0]
    assert path.endswith("openstudio_operator/client_factory.py"), (
        f"ReadOnlyRedisClient(...) must be constructed only in "
        f"client_factory.py (the factory); found it at {path}:{lineno}. "
        f"See issue #235."
    )
