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
the #116 empty-``spec.redisUrl`` path keeps raising on every call. The same
holds for issue #463's ``RedisCredentialResolutionError`` — a missing Secret
or key re-resolves (and re-raises) on every call, so a Secret created after
the first attempt is picked up on the very next tick with no eviction hook.

Issue #463 — Secret-sourced credentials:
---------------------------------------
``get_read_only_redis_client`` accepts an optional ``secret_ref``
(:class:`~openstudio_operator.config.RedisSecretRef`) plus ``namespace``.
When set, the FULL ``redis://...`` URL is resolved from that Secret key and
**preferred over the inline ``redis_url``** (the acceptance criterion:
secretRef wins when both are present — a grandfathered CR carrying a
pre-#463 inline credential is silently superseded by the Secret value).
``None`` keeps the legacy inline-URL behavior byte-for-byte, so every
existing call site is unaffected.

This is the operator's ONE bounded exception to "never reads Secrets": a
``CoreV1`` ``get`` of exactly the one named Secret, for connection info the
acceptance criterion explicitly demands the operator resolve in-process
(rclone ``envFrom`` mounting is not available here — the Redis client needs
the URL as a constructor argument). The grant is namespaced ``get``-only in
``deploy/rbac.yaml``; the CRD pins the Secret name to the
``openstudio-redis*`` convention (#240-style fence) so a CR-write principal
cannot point the operator at arbitrary Secrets.

Cache semantics on the Secret path: the cache key is
``(redis_url, secret_ref, namespace)`` — mutating the secretRef name/key in
the spec is a different key and therefore a fresh client (same
invalidation-by-key property as a mutated URL). The Secret's CONTENT is not
part of the key, so an in-place password rotation leaves the cached client
on the old URL until the operator restarts or the spec's secretRef changes
— the same runbook step the inline path needs (rotating the password also
requires updating every REDIS_URL env consumer); see
``scripts/rotate_redis_password.sh``.

Tests call :func:`get_openstudio_client.cache_clear` /
:func:`get_read_only_redis_client.cache_clear` to get a clean process.
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache

from kubernetes.client import ApiException

from openstudio_operator.config import RedisSecretRef
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.redis_client import (
    ReadOnlyRedisClient,
    RedisCredentialResolutionError,
    redis_url_from_secret_value,
)
from openstudio_operator.singleton import operator_core_api

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
def get_read_only_redis_client(
    redis_url: str,
    secret_ref: RedisSecretRef | None = None,
    namespace: str = "",
) -> ReadOnlyRedisClient:
    """Return the cached :class:`ReadOnlyRedisClient` for the resolved URL (issue #235).

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

    Issue #463 — when ``secret_ref`` is set, the URL is resolved from that
    Secret key (full-URL semantics; validated against the in-cluster
    pattern with credentials allowed) and PREFERRED over ``redis_url``.
    The cache key is ``(redis_url, secret_ref, namespace)``, so a mutated
    secretRef is a different key and therefore a fresh client; resolution
    failures raise :class:`RedisCredentialResolutionError` and are not
    cached (a Secret created later is picked up on the next call).
    """
    return ReadOnlyRedisClient(_resolve_redis_url(redis_url, secret_ref, namespace))


def _resolve_redis_url(
    redis_url: str, secret_ref: RedisSecretRef | None, namespace: str
) -> str:
    """Resolve the effective Redis URL — Secret-sourced wins over inline (#463).

    Preference order (the acceptance criterion):

    1. ``secret_ref`` set → one ``CoreV1`` ``get`` of exactly the named
       Secret; the value at ``secret_ref.key`` (base64-decoded, as the API
       server returns it) must be a full in-cluster ``redis://`` URL
       (:func:`openstudio_operator.redis_client.redis_url_from_secret_value`
       enforces the #390 fence). Any failure raises
       :class:`RedisCredentialResolutionError`.
    2. otherwise → the inline ``redis_url`` unchanged (legacy path; the
       #116 empty-URL refusal keeps firing inside
       :class:`ReadOnlyRedisClient`).

    NOTE on "the operator never reads Secrets": this single named-``get``
    is the bounded exception issue #463's acceptance criterion demands —
    the client needs the URL as a constructor argument, so the rclone-style
    ``envFrom`` hand-off is not available for this consumer. The RBAC grant
    is namespaced ``get``-only on Secrets (``deploy/rbac.yaml``), and the
    CRD pins the Secret name to the ``openstudio-redis*`` convention so a
    CR-write principal cannot make the operator read arbitrary Secrets.
    """
    if secret_ref is None:
        return redis_url
    if not namespace:
        raise RedisCredentialResolutionError(
            "spec.redisCredentials.secretRef is set but no namespace was "
            "supplied to resolve Secret "
            f"{secret_ref.name!r} (issue #463); pass the CR's namespace."
        )
    try:
        secret = operator_core_api().read_namespaced_secret(secret_ref.name, namespace)
    except ApiException as exc:
        raise RedisCredentialResolutionError(
            f"cannot read Secret {namespace}/{secret_ref.name} (key "
            f"{secret_ref.key!r}, spec.redisCredentials.secretRef, issue #463): "
            f"{exc.status} {exc.reason}"
        ) from exc
    data = getattr(secret, "data", None) or {}
    raw = data.get(secret_ref.key)
    if raw is None:
        raise RedisCredentialResolutionError(
            f"Secret {namespace}/{secret_ref.name} has no key {secret_ref.key!r} "
            f"(spec.redisCredentials.secretRef, issue #463); found keys: "
            f"{sorted(data) or '<none>'}."
        )
    try:
        # The API server returns Secret ``data`` base64-encoded; the client
        # library does NOT decode it.
        value = base64.b64decode(raw).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise RedisCredentialResolutionError(
            f"Secret {namespace}/{secret_ref.name} key {secret_ref.key!r} is not "
            f"valid base64 UTF-8 (issue #463): {exc}"
        ) from exc
    return redis_url_from_secret_value(
        value, secret_name=f"{namespace}/{secret_ref.name}", secret_key=secret_ref.key
    )
