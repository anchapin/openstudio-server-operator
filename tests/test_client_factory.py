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
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from kubernetes.client import ApiException

from _fakes import FakeSecretsCoreV1Api
from openstudio_operator import client_factory, singleton
from openstudio_operator.client_factory import (
    CLIENT_CACHE_MAXSIZE,
    REDIS_CLIENT_CACHE_MAXSIZE,
    get_openstudio_client,
    get_read_only_redis_client,
)
from openstudio_operator.config import RedisSecretRef
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.redis_client import (
    ReadOnlyRedisClient,
    RedisCredentialResolutionError,
)

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
    the boot-time layout probe's. The import is now a single symbol. Since
    #584 the boot-time layout probe lives in ``handlers.redis_layout_check``
    (moved out of the package init), so that module is the probe's home in
    this tuple.
    """
    from openstudio_operator.handlers import (
        analysis_sla,
        redis_layout_check,
        web_background_monitor,
    )

    modules = (redis_layout_check, analysis_sla, web_background_monitor)
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


# --- Issue #463 — Secret-sourced Redis credentials ----------------------------
#
# ``spec.redisCredentials.secretRef`` names a Secret key holding the FULL
# ``redis://...`` URL; when set it is PREFERRED over the inline
# ``spec.redisUrl`` (the acceptance criterion). Resolution happens inside
# the factory (the operator's ONE bounded Secret-read exception) and flows
# through the same ``lru_cache`` — the secretRef identity is part of the
# cache key. These cases fake the ``CoreV1`` Secret read through the
# ``singleton._operator_core_api`` seam; construction stays lazy so nothing
# touches the network.

SECRET_URL = "redis://:rotated-pw@queue.openstudio-server.svc.cluster.local:6379"
INLINE_CRED_FREE = "redis://queue:6379"
REF = RedisSecretRef(name="openstudio-redis", key="redis-url")
OTHER_KEY_REF = RedisSecretRef(name="openstudio-redis", key="url")
NAMESPACE = "openstudio-server"


class FakeCoreV1Api:
    """Just enough ``CoreV1Api`` for the factory's one Secret read."""

    def __init__(self, data: dict[str, str] | None = None, exc: Exception | None = None):
        self._data = data or {}
        self._exc = exc
        self.calls: list[tuple[str, str]] = []

    def read_namespaced_secret(self, name: str, namespace: str):
        self.calls.append((name, namespace))
        if self._exc is not None:
            raise self._exc
        return SimpleNamespace(data=self._data)


def _encoded(url: str) -> str:
    # The API server returns Secret ``data`` base64-encoded; the client
    # library does NOT decode it.
    return base64.b64encode(url.encode()).decode()


def _install_secret_api(monkeypatch, fake: FakeCoreV1Api) -> None:
    monkeypatch.setattr(singleton, "_operator_core_api", fake)


def test_secret_ref_resolves_url_from_secret(monkeypatch):
    """The client is built from the Secret-held URL (base64 ``data`` decoded)."""
    fake = FakeCoreV1Api({"redis-url": _encoded(SECRET_URL)})
    _install_secret_api(monkeypatch, fake)

    client = get_read_only_redis_client("", secret_ref=REF, namespace=NAMESPACE)

    assert client._redis_url == SECRET_URL
    assert fake.calls == [("openstudio-redis", NAMESPACE)]


def test_secret_ref_is_preferred_over_inline_url(monkeypatch):
    """Issue #463 acceptance: secretRef WINS when both are present — a
    grandfathered CR carrying a pre-#463 inline credential (or any inline
    URL) is superseded by the Secret value."""
    fake = FakeCoreV1Api({"redis-url": _encoded(SECRET_URL)})
    _install_secret_api(monkeypatch, fake)

    client = get_read_only_redis_client(
        "redis://:stale-inline-pw@queue:6379", secret_ref=REF, namespace=NAMESPACE
    )

    assert client._redis_url == SECRET_URL


def test_secret_ref_missing_secret_raises_and_is_not_cached(monkeypatch):
    """A missing Secret (404) raises RedisCredentialResolutionError and —
    because ``lru_cache`` never memoizes exceptions — the NEXT call re-reads
    the API, so a Secret created after the first attempt is picked up on the
    very next tick with no eviction hook."""
    fake = FakeCoreV1Api(exc=ApiException(status=404, reason="Not Found"))
    _install_secret_api(monkeypatch, fake)

    with pytest.raises(RedisCredentialResolutionError, match="openstudio-redis"):
        get_read_only_redis_client("", secret_ref=REF, namespace=NAMESPACE)

    assert get_read_only_redis_client.cache_info().currsize == 0
    assert len(fake.calls) == 1

    # Second attempt re-resolves (fresh API read, fresh raise).
    with pytest.raises(RedisCredentialResolutionError):
        get_read_only_redis_client("", secret_ref=REF, namespace=NAMESPACE)
    assert len(fake.calls) == 2


def test_secret_ref_403_message_names_rbac_remedy(monkeypatch):
    """Issue #606 — a 403 is NOT a generic resolution failure: it means the
    RBAC resourceNames fence bit (the CR names a pattern-legal Secret the
    default Role does not grant). The raised message must name the RBAC
    cause and both remedies — widen deploy/rbac.yaml resourceNames or
    point the secretRef at a granted Secret — so every surface carrying it
    (key-layout "unreachable" log, counted tick-skip log, the singleton
    guard's Warning Event) tells the SRE exactly what to do."""
    custom_ref = RedisSecretRef(name="openstudio-redis-url", key="redis-url")
    fake = FakeCoreV1Api(exc=ApiException(status=403, reason="Forbidden"))
    _install_secret_api(monkeypatch, fake)

    with pytest.raises(RedisCredentialResolutionError) as excinfo:
        get_read_only_redis_client("", secret_ref=custom_ref, namespace=NAMESPACE)

    message = str(excinfo.value)
    assert "403 Forbidden" in message
    assert "openstudio-redis-url" in message
    assert "resourceNames" in message and "rbac.yaml" in message, (
        "the 403 message must name the RBAC resourceNames cause "
        "(deploy/rbac.yaml), not just the status code (issue #606); got: "
        f"{message!r}"
    )
    assert "#606" in message
    # The 404 path (companion test above) must NOT grow the RBAC suffix —
    # only the RBAC-denied case names the remedy.
    fake_404 = FakeCoreV1Api(exc=ApiException(status=404, reason="Not Found"))
    _install_secret_api(monkeypatch, fake_404)
    with pytest.raises(RedisCredentialResolutionError) as excinfo_404:
        get_read_only_redis_client("", secret_ref=custom_ref, namespace=NAMESPACE)
    assert "resourceNames" not in str(excinfo_404.value), (
        "a 404 is a missing-Secret failure, not the RBAC fence — the "
        "remedy suffix is 403-only (issue #606)"
    )


def test_secret_ref_missing_key_raises(monkeypatch):
    fake = FakeCoreV1Api({"password": _encoded("rotated-pw")})  # wrong key
    _install_secret_api(monkeypatch, fake)

    with pytest.raises(RedisCredentialResolutionError, match="redis-url"):
        get_read_only_redis_client("", secret_ref=REF, namespace=NAMESPACE)


def test_secret_ref_invalid_url_value_raises(monkeypatch):
    """The Secret VALUE is fence-checked too: the #390 in-cluster constraint
    must survive the move into the Secret (no off-cluster side door)."""
    fake = FakeCoreV1Api({"redis-url": _encoded("redis://:pw@attacker.example.com:6379")})
    _install_secret_api(monkeypatch, fake)

    with pytest.raises(RedisCredentialResolutionError, match="attacker.example.com"):
        get_read_only_redis_client("", secret_ref=REF, namespace=NAMESPACE)


def test_secret_ref_without_namespace_raises(monkeypatch):
    """Resolution needs the CR's namespace; refusing loudly beats guessing."""
    fake = FakeCoreV1Api({"redis-url": _encoded(SECRET_URL)})
    _install_secret_api(monkeypatch, fake)

    with pytest.raises(RedisCredentialResolutionError, match="namespace"):
        get_read_only_redis_client("", secret_ref=REF, namespace="")

    assert fake.calls == []  # refused before any API call


def test_secret_ref_identity_is_part_of_the_cache_key(monkeypatch):
    """Issue #463 cache semantics: mutating the secretRef name/key in the
    spec is a different cache key ⇒ a fresh client (same
    invalidation-by-key property as a mutated URL). Same tuple ⇒ same
    client object (one API read, one connection pool)."""
    _install_secret_api(
        monkeypatch,
        FakeCoreV1Api({"redis-url": _encoded(SECRET_URL), "url": _encoded(OTHER_REDIS_URL)}),
    )

    first = get_read_only_redis_client(INLINE_CRED_FREE, secret_ref=REF, namespace=NAMESPACE)
    same = get_read_only_redis_client(INLINE_CRED_FREE, secret_ref=REF, namespace=NAMESPACE)
    other_key = get_read_only_redis_client(
        INLINE_CRED_FREE, secret_ref=OTHER_KEY_REF, namespace=NAMESPACE
    )

    assert first is same
    assert first is not other_key
    assert other_key._redis_url == OTHER_REDIS_URL


def test_no_secret_ref_keeps_inline_path_byte_for_byte(monkeypatch):
    """``secret_ref=None`` (every pre-#463 call site) never touches the
    Secrets API — the inline URL is used unchanged."""
    fake = FakeCoreV1Api({})
    _install_secret_api(monkeypatch, fake)

    client = get_read_only_redis_client(INLINE_CRED_FREE)

    assert client._redis_url == INLINE_CRED_FREE
    assert fake.calls == []


def test_secret_ref_resolves_rediss_tls_url_end_to_end_476(monkeypatch):
    """Issue #476: a Secret holding a ``rediss://`` (TLS) URL resolves
    through the SAME factory path — fence-checked by
    ``redis_url_from_secret_value`` and built as a TLS connection (SSL
    connection class from ``redis.Redis.from_url``). Proves the cache key /
    secretRef resolution pass the scheme through untouched."""
    from redis.connection import SSLConnection

    tls_url = "rediss://:rotated-pw@queue.openstudio-server.svc.cluster.local:6379"
    fake = FakeCoreV1Api({"redis-url": _encoded(tls_url)})
    _install_secret_api(monkeypatch, fake)

    client = get_read_only_redis_client("", secret_ref=REF, namespace=NAMESPACE)

    assert client._redis_url == tls_url
    assert client._redis.connection_pool.connection_class is SSLConnection


# --- Issue #568 — Secret-content rotation is picked up in-band ------------------
#
# Pre-#568 the cache key was ``(redis_url, secret_ref, namespace)``: the
# Secret's CONTENT was not part of the key, so rotating the password in
# place (exactly what ``scripts/rotate_redis_password.sh`` does) left the
# cached client authenticating with the OLD password — WRONGPASS →
# ``RedisClientError`` → counted skip-ticks, Resque-dependent features
# dark — until someone bounced the operator pod. The factory now
# re-resolves the Secret on EVERY call and keys the client LRU by the
# resolved URL, so a rotation is a new key ⇒ a fresh client on the very
# next tick. These cases use the shared ``FakeSecretsCoreV1Api`` (extended
# with ``resource_version`` + ``rotate()`` for #568) rather than the local
# ``FakeCoreV1Api`` above.

ROTATED_SECRET_URL = "redis://:new-rotated-pw@queue.openstudio-server.svc.cluster.local:6379"


def _install_shared_secret_api(monkeypatch, fake: FakeSecretsCoreV1Api) -> None:
    monkeypatch.setattr(singleton, "_operator_core_api", fake)


def test_secret_rotation_yields_fresh_client_568(monkeypatch):
    """Same secretRef + namespace, rotated Secret content ⇒ a NEW client.

    The stale entry is never handed back out: the rotated password changes
    the resolved URL, the URL is the cache key, and the very next call
    builds from the new credential — the in-band fix #568 demands (no
    operator restart, picked up within one tick)."""
    fake = FakeSecretsCoreV1Api({"redis-url": _encoded(SECRET_URL)})
    _install_shared_secret_api(monkeypatch, fake)

    stale = get_read_only_redis_client("", secret_ref=REF, namespace=NAMESPACE)

    fake.rotate({"redis-url": _encoded(ROTATED_SECRET_URL)})

    fresh = get_read_only_redis_client("", secret_ref=REF, namespace=NAMESPACE)

    assert fresh is not stale
    assert fresh._redis_url == ROTATED_SECRET_URL
    assert stale._redis_url == SECRET_URL
    assert fake.calls == [("openstudio-redis", NAMESPACE), ("openstudio-redis", NAMESPACE)]
    assert fake.versions_read == ["1000", "1001"]  # the rotation bumped the rv


def test_unchanged_secret_keeps_cached_client_and_pool_568(monkeypatch):
    """Unchanged Secret content ⇒ the SAME client object (and connection
    pool). The rotation probe costs one bounded ``secrets: get`` per call,
    but the pool only churns when the credential actually changes."""
    fake = FakeSecretsCoreV1Api({"redis-url": _encoded(SECRET_URL)})
    _install_shared_secret_api(monkeypatch, fake)

    first = get_read_only_redis_client("", secret_ref=REF, namespace=NAMESPACE)
    second = get_read_only_redis_client("", secret_ref=REF, namespace=NAMESPACE)

    assert first is second
    assert first._redis is second._redis
    assert len(fake.calls) == 2  # re-resolved every call — the #568 rotation probe


def test_resource_version_bump_without_content_change_keeps_client_568(monkeypatch):
    """A Secret update that bumps ``resourceVersion`` but leaves the URL
    byte-identical keeps the cached client.

    Keying the LRU on the resolved URL (not the bare resourceVersion) is
    deliberate: any credential rotation changes the URL, while a
    content-less metadata touch must not churn the connection pool."""
    fake = FakeSecretsCoreV1Api({"redis-url": _encoded(SECRET_URL)})
    _install_shared_secret_api(monkeypatch, fake)

    first = get_read_only_redis_client("", secret_ref=REF, namespace=NAMESPACE)

    fake.rotate({"redis-url": _encoded(SECRET_URL)})  # same content, new rv

    second = get_read_only_redis_client("", secret_ref=REF, namespace=NAMESPACE)

    assert first is second
