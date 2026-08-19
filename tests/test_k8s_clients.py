"""Tests for the centralised K8s client factory (issue #158).

The factory :func:`openstudio_operator.singleton.operator_custom_objects_api`
is the operator's SINGLE ``CustomObjectsApi()`` construction point: every
handler that talks to the K8s API server for the OSCM custom object
imports the factory instead of instantiating ``CustomObjectsApi`` inline.
This module pins the factory's load+cache behaviour with two
contract tests:

* ``test_factory_returns_singleton_instance`` — two calls share the same
  :class:`CustomObjectsApi` instance (process-wide cache).
* ``test_factory_loads_incluster_config`` — the first call runs
  ``kubernetes.config.load_incluster_config`` exactly once; subsequent calls
  skip the loader entirely.

The companion AST test in
``tests/test_singleton_registry_coverage.py::test_only_one_custom_objects_api_construction_point``
pins the "exactly one construction site" invariant at the source level so a
future regression that bypasses the factory fails the CI gate loudly.

Test isolation: the autouse fixture below calls
:func:`openstudio_operator.singleton.reset_operator_k8s_client` before AND
after each test so the factory's module-level cache is empty at the start
of the case. Without this reset, a test that asserts ``load_incluster_config``
was called would be fooled by a cached instance built in a previous test.
"""

from __future__ import annotations

import pytest

from openstudio_operator import singleton


@pytest.fixture(autouse=True)
def _reset_k8s_client_cache():
    """Clear the factory's module-level cache before AND after each test.

    The production factory caches the :class:`CustomObjectsApi` for the
    operator's lifetime; tests that mock the K8s config loaders need a
    clean slate so the first call to :func:`operator_custom_objects_api`
    re-runs the load path with the freshly patched loader. Pre + post
    resets keep the suite hermetic: a stale cache from a previous test
    cannot leak into the next case (the post-yield reset is defensive —
    if a test crashes mid-run, monkeypatch's atexit reverts don't touch
    the module-level cache).

    Also snapshots/restores ``kubernetes.client.Configuration._default``
    because the load-once test installs a sentinel host on the global
    default; without restoration, subsequent tests that assert "bare
    client raises No host specified" (e.g.
    ``test_singleton_guard::test_get_guard_bare_client_dies_guard_client_does_not``)
    would see the sentinel and make a real DNS lookup instead of raising.
    """
    import kubernetes.client

    prev_default = kubernetes.client.Configuration._default
    singleton.reset_operator_k8s_client()
    yield
    singleton.reset_operator_k8s_client()
    kubernetes.client.Configuration._default = prev_default


def test_factory_returns_singleton_instance() -> None:
    """Two calls to ``operator_custom_objects_api`` return the same object.

    Pins the factory's caching contract: every handler in the operator
    that talks to the K8s API server ends up sharing the same
    :class:`CustomObjectsApi` instance, so the underlying
    :class:`kubernetes.client.ApiClient` (HTTP connection pool, retry
    config) is reused across every operator tick. The cache lives at
    module level and survives for the operator's lifetime; tests reset
    it via the autouse fixture above.
    """
    first = singleton.operator_custom_objects_api()
    second = singleton.operator_custom_objects_api()

    assert first is second, (
        "operator_custom_objects_api() must return the SAME instance on "
        "every call (the cached process-wide client). Got two distinct "
        "objects — the factory's caching regressed. See issue #158."
    )


def test_factory_loads_incluster_config() -> None:
    """The first call runs ``load_incluster_config`` exactly once; later calls skip it.

    Pins the factory's lazy-load contract: a single
    ``kubernetes.config.load_incluster_config`` call populates the
    process-wide ``Configuration`` default, and subsequent calls reuse the
    cached :class:`CustomObjectsApi` instead of reloading config on every
    tick. Without caching, every handler tick would pay the
    ``load_incluster_config`` cost (env reads + token mount parse) and
    waste the connection pool.
    """
    import kubernetes.client
    import kubernetes.config as kube_config

    load_calls: list[str] = []
    original_load = kube_config.load_incluster_config

    def fake_load_incluster() -> None:
        load_calls.append("load_incluster_config")
        # Mirror the real loader's side effect that matters here
        # (issue #79): installing a default Configuration with a host,
        # so a subsequently constructed CustomObjectsApi carries it.
        cfg = kubernetes.client.Configuration()
        cfg.host = "https://apiserver.incluster.example:6443"
        kubernetes.client.Configuration.set_default(cfg)

    kube_config.load_incluster_config = fake_load_incluster  # type: ignore[assignment]

    try:
        first = singleton.operator_custom_objects_api()
        second = singleton.operator_custom_objects_api()
    finally:
        kube_config.load_incluster_config = original_load  # type: ignore[assignment]

    assert load_calls == ["load_incluster_config"], (
        f"Expected load_incluster_config to be called exactly once "
        f"(cached after that); got {load_calls!r}. See issue #158 — the "
        f"factory's load-once-then-cache contract regressed."
    )
    assert first is second, (
        "The second factory call must return the cached instance "
        "(load_calls == ['load_incluster_config'] proves the loader "
        "fired only once). See issue #158."
    )