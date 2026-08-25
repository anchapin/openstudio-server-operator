"""Tests for the centralised K8s client factory (issue #158) — expanded coverage (issue #801).

The factory :func:`openstudio_operator.singleton.operator_custom_objects_api`
is the operator's SINGLE ``CustomObjectsApi()`` construction point: every
handler that talks to the K8s API server for the OSCM custom object
imports the factory instead of instantiating ``CustomObjectsApi`` inline.

This module pins the factory's load+cache behaviour with contract tests
for all four public factories:

* ``test_factory_returns_singleton_instance`` — two calls share the same
  instance (process-wide cache).
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

FACTORIES = (
    ("operator_custom_objects_api", singleton.operator_custom_objects_api, True),
    ("operator_apps_api", singleton.operator_apps_api, False),
    ("operator_batch_api", singleton.operator_batch_api, False),
    ("operator_core_api", singleton.operator_core_api, False),
)


@pytest.fixture(autouse=True)
def _reset_k8s_client_cache():
    """Clear the factory's module-level cache before AND after each test.

    The production factory caches each :class:`kubernetes.client.Api` for the
    operator's lifetime; tests that mock the K8s config loaders need a
    clean slate so the first call to any ``operator_*_api`` re-runs
    the load path with the freshly patched loader. Pre + post
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


@pytest.mark.parametrize("factory_name,factory_fn,is_strict", FACTORIES)
def test_factory_returns_singleton_instance(factory_name: str, factory_fn, is_strict: bool) -> None:
    """Two calls to the same factory return the same object.

    Pins the factory's caching contract: every handler in the operator
    that talks to the K8s API server ends up sharing the same
    :class:`kubernetes.client.Api` instance, so the underlying
    :class:`kubernetes.client.ApiClient` (HTTP connection pool, retry
    config) is reused across every operator tick. The cache lives at
    module level and survives for the operator's lifetime; tests reset
    it via the autouse fixture above.
    """
    import kubernetes.client
    import kubernetes.config as kube_config

    def fake_load_incluster() -> None:
        cfg = kubernetes.client.Configuration()
        cfg.host = "https://apiserver.incluster.example:6443"
        kubernetes.client.Configuration.set_default(cfg)

    def fake_load_kube() -> None:
        fake_load_incluster()

    original_inc = kube_config.load_incluster_config
    original_kube = kube_config.load_kube_config
    kube_config.load_incluster_config = fake_load_incluster  # type: ignore[assignment]
    kube_config.load_kube_config = fake_load_kube  # type: ignore[assignment]
    try:
        first = factory_fn()
        second = factory_fn()
    finally:
        kube_config.load_incluster_config = original_inc  # type: ignore[assignment]
        kube_config.load_kube_config = original_kube  # type: ignore[assignment]

    assert first is second, (
        f"{factory_name}() must return the SAME instance on "
        f"every call (the cached process-wide client). Got two distinct "
        f"objects — the factory's caching regressed. See issue #158."
    )


@pytest.mark.parametrize("factory_name,factory_fn,is_strict", FACTORIES)
def test_factory_loads_incluster_config_once(factory_name: str, factory_fn, is_strict: bool) -> None:
    """The first call runs ``load_incluster_config`` exactly once; later calls skip it.

    Pins the factory's lazy-load contract: a single
    ``kubernetes.config.load_incluster_config`` call populates the
    process-wide ``Configuration`` default, and subsequent calls reuse the
    cached :class:`kubernetes.client.Api` instead of reloading config on every
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
        cfg = kubernetes.client.Configuration()
        cfg.host = "https://apiserver.incluster.example:6443"
        kubernetes.client.Configuration.set_default(cfg)

    kube_config.load_incluster_config = fake_load_incluster  # type: ignore[assignment]

    try:
        first = factory_fn()
        second = factory_fn()
    finally:
        kube_config.load_incluster_config = original_load  # type: ignore[assignment]

    assert load_calls == ["load_incluster_config"], (
        f"Expected load_incluster_config to be called exactly once "
        f"(cached after that); got {load_calls!r}. See issue #158 — the "
        f"factory's load-once-then-cache contract regressed."
    )
    assert first is second, (
        f"The second {factory_name}() call must return the cached instance "
        f"(load_calls == ['load_incluster_config'] proves the loader "
        f"fired only once). See issue #158."
    )


def test_apps_api_provides_deployment_manager_protocol() -> None:
    """``operator_apps_api`` returns an object satisfying DeploymentManager.

    The AppsV1Api client is used by worker_recycler (patch) and
    web_background_monitor (read+patch) via the DeploymentManager Protocol.
    This test verifies the client exposes the required method signatures.
    """
    import kubernetes.client
    import kubernetes.config as kube_config

    def fake_load_incluster() -> None:
        cfg = kubernetes.client.Configuration()
        cfg.host = "https://apiserver.incluster.example:6443"
        kubernetes.client.Configuration.set_default(cfg)

    original_inc = kube_config.load_incluster_config
    kube_config.load_incluster_config = fake_load_incluster  # type: ignore[assignment]
    try:
        client = singleton.operator_apps_api()
    finally:
        kube_config.load_incluster_config = original_inc  # type: ignore[assignment]

    assert hasattr(client, "read_namespaced_deployment"), (
        "AppsV1Api must expose read_namespaced_deployment (DeploymentReader Protocol)"
    )
    assert hasattr(client, "patch_namespaced_deployment"), (
        "AppsV1Api must expose patch_namespaced_deployment (DeploymentManager Protocol)"
    )


def test_batch_api_provides_job_surface() -> None:
    """``operator_batch_api`` returns a BatchV1Api with job create/read/delete.

    The BatchV1Api client is used by the retention pipeline for archival
    Job lifecycle management.
    """
    import kubernetes.client
    import kubernetes.config as kube_config

    def fake_load_incluster() -> None:
        cfg = kubernetes.client.Configuration()
        cfg.host = "https://apiserver.incluster.example:6443"
        kubernetes.client.Configuration.set_default(cfg)

    original_inc = kube_config.load_incluster_config
    kube_config.load_incluster_config = fake_load_incluster  # type: ignore[assignment]
    try:
        client = singleton.operator_batch_api()
    finally:
        kube_config.load_incluster_config = original_inc  # type: ignore[assignment]

    assert hasattr(client, "create_namespaced_job"), (
        "BatchV1Api must expose create_namespaced_job"
    )
    assert hasattr(client, "delete_namespaced_job"), (
        "BatchV1Api must expose delete_namespaced_job"
    )
    assert hasattr(client, "list_namespaced_job"), (
        "BatchV1Api must expose list_namespaced_job"
    )


def test_core_api_provides_pod_lister_protocol() -> None:
    """``operator_core_api`` returns a CoreV1Api satisfying PodLister.

    The CoreV1Api client is used by analysis_sla (pod delete) and
    web_background_monitor (pod list) via the PodLister Protocol.
    """
    import kubernetes.client
    import kubernetes.config as kube_config

    def fake_load_incluster() -> None:
        cfg = kubernetes.client.Configuration()
        cfg.host = "https://apiserver.incluster.example:6443"
        kubernetes.client.Configuration.set_default(cfg)

    original_inc = kube_config.load_incluster_config
    kube_config.load_incluster_config = fake_load_incluster  # type: ignore[assignment]
    try:
        client = singleton.operator_core_api()
    finally:
        kube_config.load_incluster_config = original_inc  # type: ignore[assignment]

    assert hasattr(client, "list_namespaced_pod"), (
        "CoreV1Api must expose list_namespaced_pod (PodLister Protocol)"
    )


def test_apps_api_lenient_on_missing_config() -> None:
    """``operator_apps_api`` returns a placeholder client when no kubeconfig is present.

    Unlike operator_custom_objects_api (strict), the *V1Api factories are
    lenient: a ConfigException is swallowed with a WARNING and a placeholder
    client is returned. The placeholder's API calls fail at call time (fail-closed
    via HANDLER_TICK_FAILURES_TOTAL), not at construction time.
    """
    import kubernetes.config as kube_config
    from kubernetes.config import ConfigException

    original_inc = kube_config.load_incluster_config
    original_kube = kube_config.load_kube_config

    def raise_config_exception() -> None:
        raise ConfigException("no config")

    kube_config.load_incluster_config = raise_config_exception  # type: ignore[assignment]
    kube_config.load_kube_config = raise_config_exception  # type: ignore[assignment]
    try:
        client = singleton.operator_apps_api()
    finally:
        kube_config.load_incluster_config = original_inc  # type: ignore[assignment]
        kube_config.load_kube_config = original_kube  # type: ignore[assignment]

    assert client is not None, (
        "operator_apps_api must return a client even when config is unavailable (lenient)"
    )


def test_batch_api_lenient_on_missing_config() -> None:
    """``operator_batch_api`` returns a placeholder client when no kubeconfig is present."""
    import kubernetes.config as kube_config
    from kubernetes.config import ConfigException

    original_inc = kube_config.load_incluster_config
    original_kube = kube_config.load_kube_config

    def raise_config_exception() -> None:
        raise ConfigException("no config")

    kube_config.load_incluster_config = raise_config_exception  # type: ignore[assignment]
    kube_config.load_kube_config = raise_config_exception  # type: ignore[assignment]
    try:
        client = singleton.operator_batch_api()
    finally:
        kube_config.load_incluster_config = original_inc  # type: ignore[assignment]
        kube_config.load_kube_config = original_kube  # type: ignore[assignment]

    assert client is not None, (
        "operator_batch_api must return a client even when config is unavailable (lenient)"
    )


def test_core_api_lenient_on_missing_config() -> None:
    """``operator_core_api`` returns a placeholder client when no kubeconfig is present."""
    import kubernetes.config as kube_config
    from kubernetes.config import ConfigException

    original_inc = kube_config.load_incluster_config
    original_kube = kube_config.load_kube_config

    def raise_config_exception() -> None:
        raise ConfigException("no config")

    kube_config.load_incluster_config = raise_config_exception  # type: ignore[assignment]
    kube_config.load_kube_config = raise_config_exception  # type: ignore[assignment]
    try:
        client = singleton.operator_core_api()
    finally:
        kube_config.load_incluster_config = original_inc  # type: ignore[assignment]
        kube_config.load_kube_config = original_kube  # type: ignore[assignment]

    assert client is not None, (
        "operator_core_api must return a client even when config is unavailable (lenient)"
    )


def test_custom_objects_api_strict_on_missing_config() -> None:
    """``operator_custom_objects_api`` raises ConfigException when no kubeconfig is present.

    Unlike the *V1Api factories (lenient), CustomObjectsApi is strict: a
    ConfigException propagates so the tick wrapper's fail-closed skip path
    (HANDLER_TICK_FAILURES_TOTAL + retry next poll) activates.
    """
    import kubernetes.config as kube_config
    from kubernetes.config import ConfigException

    original_inc = kube_config.load_incluster_config
    original_kube = kube_config.load_kube_config

    def raise_config_exception() -> None:
        raise ConfigException("no config")

    kube_config.load_incluster_config = raise_config_exception  # type: ignore[assignment]
    kube_config.load_kube_config = raise_config_exception  # type: ignore[assignment]
    try:
        with pytest.raises(ConfigException):
            singleton.operator_custom_objects_api()
    finally:
        kube_config.load_incluster_config = original_inc  # type: ignore[assignment]
        kube_config.load_kube_config = original_kube  # type: ignore[assignment]
