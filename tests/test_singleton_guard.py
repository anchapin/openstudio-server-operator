"""Unit tests for the passive singleton guard (D05 — issue #14).

The K8s API surface is faked with an in-memory ``CustomObjectsApi`` stand-in
(``list_namespaced_custom_object`` only — the guard never mutates anything),
and Events are captured via a recorder sink instead of ``kopf.event`` (which
needs a live operator context). No live cluster, no extra dependencies beyond
the ``[dev]`` extra — same approach as test_worker_recycler.py.
"""

import copy
import logging

import kopf
import kubernetes.client
import kubernetes.config
import pytest
from kubernetes.client import ApiException, CustomObjectsApi
from kubernetes.config import ConfigException

from openstudio_operator import singleton
from openstudio_operator.singleton import (
    SINGLETON_ACTIVE_EVENT,
    SINGLETON_CONFLICT_EVENT,
    SingletonGuard,
    SingletonGuardError,
    install_singleton_guard,
    is_active_cr,
    resolve_active_cr,
)
from openstudio_operator.status_store import GROUP, PLURAL

NAMESPACE = "openstudio-server"

OLD_TS = "2026-08-10T08:00:00Z"
NEW_TS = "2026-08-11T08:00:00Z"

LOGGER = logging.getLogger("singleton-test")


def make_cr(name: str, created: str, uid: str | None = None, spec: dict | None = None) -> dict:
    meta: dict = {"name": name, "namespace": NAMESPACE, "creationTimestamp": created}
    if uid is not None:
        meta["uid"] = uid
    return {
        "apiVersion": "energy.nrel.gov/v1alpha1",
        "kind": "OpenStudioClusterManager",
        "metadata": meta,
        "spec": copy.deepcopy(spec if spec is not None else {"serverUrl": "http://web.test"}),
    }


class FakeCustomObjectsApi:
    """In-memory list-only CustomObjectsApi stand-in (the guard never mutates)."""

    def __init__(self, items: list[dict]) -> None:
        self.items = copy.deepcopy(items)
        self.list_calls = 0

    def list_namespaced_custom_object(self, group, version, namespace, plural):
        assert (group, version, plural) == (GROUP, "v1alpha1", PLURAL)
        assert namespace == NAMESPACE
        self.list_calls += 1
        return {"items": copy.deepcopy(self.items)}


class ExplodingCustomObjectsApi(FakeCustomObjectsApi):
    def list_namespaced_custom_object(self, group, version, namespace, plural):
        raise ApiException(status=500, reason="boom")


def make_sink():
    events: list[tuple[dict, str, str, str]] = []

    def emit(obj: dict, event_type: str, reason: str, message: str) -> None:
        events.append((copy.deepcopy(obj), event_type, reason, message))

    return events, emit


def event_triples(events):
    return [(obj["metadata"]["name"], etype, reason) for obj, etype, reason, _ in events]


@pytest.fixture
def guard_two_crs():
    """Oldest CR 'alpha' (spec A) + newer CR 'beta' (spec B)."""
    return SingletonGuard(
        FakeCustomObjectsApi(
            [
                make_cr("alpha", OLD_TS, uid="uid-alpha", spec={"serverUrl": "http://a.test"}),
                make_cr("beta", NEW_TS, uid="uid-beta", spec={"serverUrl": "http://b.test"}),
            ]
        )
    )


@pytest.fixture
def log(caplog):
    """The shared test logger, at INFO so caplog reliably captures its records."""
    LOGGER.setLevel(logging.INFO)
    return LOGGER


# --- resolution ---------------------------------------------------------------


def test_resolve_active_cr_picks_the_oldest():
    items = [
        make_cr("beta", NEW_TS),
        make_cr("alpha", OLD_TS),
    ]
    assert resolve_active_cr(items)["metadata"]["name"] == "alpha"


def test_resolve_active_cr_empty_is_none():
    assert resolve_active_cr([]) is None


def test_equal_creation_timestamps_tiebreak_is_lexicographic_name():
    items = [
        make_cr("zulu", "2026-08-10T08:00:00Z"),
        make_cr("alfa", "2026-08-10T08:00:00Z"),
        make_cr("mike", "2026-08-10T08:00:00Z"),
    ]
    assert resolve_active_cr(items)["metadata"]["name"] == "alfa"


def test_missing_creation_timestamp_sorts_youngest():
    items = [
        {"metadata": {"name": "synthetic"}},
        make_cr("real", OLD_TS),
    ]
    assert resolve_active_cr(items)["metadata"]["name"] == "real"


def test_malformed_creation_timestamp_raises():
    items = [make_cr("broken", "not-a-timestamp")]
    with pytest.raises(SingletonGuardError):
        resolve_active_cr(items)


# --- is_active: only the oldest CR's spec drives behavior ----------------------


def test_is_active_true_for_oldest_false_for_newer(guard_two_crs):
    alpha = guard_two_crs.list_crs(NAMESPACE)[0]
    beta = guard_two_crs.list_crs(NAMESPACE)[1]
    assert guard_two_crs.is_active(alpha, NAMESPACE) is True
    assert guard_two_crs.is_active(beta, NAMESPACE) is False


def test_is_active_zero_crs_is_false():
    guard = SingletonGuard(FakeCustomObjectsApi([]))
    assert guard.is_active(make_cr("anyone", OLD_TS), NAMESPACE) is False


def test_is_active_cr_module_convenience(monkeypatch, guard_two_crs):
    monkeypatch.setattr(singleton, "_process_guard", guard_two_crs)
    assert is_active_cr(guard_two_crs.list_crs(NAMESPACE)[0], NAMESPACE) is True
    assert is_active_cr(guard_two_crs.list_crs(NAMESPACE)[1], NAMESPACE) is False


# --- enforce: the loud half ----------------------------------------------------


def test_enforce_conflict_error_log_and_warning_events(caplog, log, guard_two_crs):
    events, emit = make_sink()
    guard_two_crs.enforce(guard_two_crs.list_crs(NAMESPACE), logger=log, emit=emit)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "alpha" in message and "beta" in message  # roster names every CR + the winner

    assert event_triples(events) == [
        ("beta", "Warning", SINGLETON_CONFLICT_EVENT),
        ("alpha", "Normal", SINGLETON_ACTIVE_EVENT),
    ]
    assert "alpha" in events[0][3]  # loser message points at the winner


def test_enforce_is_silent_when_state_unchanged(caplog, log, guard_two_crs):
    events, emit = make_sink()
    items = guard_two_crs.list_crs(NAMESPACE)
    guard_two_crs.enforce(items, logger=log, emit=emit)
    guard_two_crs.enforce(items, logger=log, emit=emit)
    guard_two_crs.enforce(items, logger=log, emit=emit)
    assert len(events) == 2  # one Warning + one Normal, exactly once
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_enforce_single_cr_one_info_log_no_events(caplog, log):
    guard = SingletonGuard(FakeCustomObjectsApi([make_cr("solo", OLD_TS)]))
    events, emit = make_sink()
    guard.enforce(guard.list_crs(NAMESPACE), logger=log, emit=emit)
    guard.enforce(guard.list_crs(NAMESPACE), logger=log, emit=emit)
    assert events == []
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert "solo" in infos[0].getMessage()


def test_enforce_zero_crs_single_idle_log_no_events(caplog, log):
    guard = SingletonGuard(FakeCustomObjectsApi([]))
    events, emit = make_sink()
    guard.enforce([], logger=log, emit=emit)
    guard.enforce([], logger=log, emit=emit)
    assert events == []
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert "idle" in infos[0].getMessage()


def test_enforce_newer_cr_appears_winner_unchanged(caplog, log):
    api = FakeCustomObjectsApi([make_cr("alpha", OLD_TS, uid="uid-alpha")])
    guard = SingletonGuard(api)
    events, emit = make_sink()
    guard.enforce(guard.list_crs(NAMESPACE), logger=log, emit=emit)
    assert events == []  # single CR: quiet

    api.items.append(make_cr("beta", NEW_TS, uid="uid-beta"))
    guard.enforce(guard.list_crs(NAMESPACE), logger=log, emit=emit)

    assert event_triples(events) == [
        ("beta", "Warning", SINGLETON_CONFLICT_EVENT),
        ("alpha", "Normal", SINGLETON_ACTIVE_EVENT),
    ]
    assert guard.is_active(api.items[0], NAMESPACE) is True
    assert guard.is_active(api.items[1], NAMESPACE) is False


def test_enforce_older_cr_appears_and_usurps_winner(caplog, log):
    api = FakeCustomObjectsApi([make_cr("beta", NEW_TS, uid="uid-beta")])
    guard = SingletonGuard(api)
    events, emit = make_sink()
    guard.enforce(guard.list_crs(NAMESPACE), logger=log, emit=emit)
    assert guard.is_active(api.items[0], NAMESPACE) is True

    api.items.append(make_cr("alpha", OLD_TS, uid="uid-alpha"))
    guard.enforce(guard.list_crs(NAMESPACE), logger=log, emit=emit)

    # oldest always wins: alpha takes over, beta demoted to loser with events
    assert event_triples(events) == [
        ("beta", "Warning", SINGLETON_CONFLICT_EVENT),
        ("alpha", "Normal", SINGLETON_ACTIVE_EVENT),
    ]
    assert guard.is_active(api.items[1], NAMESPACE) is True
    assert guard.is_active(api.items[0], NAMESPACE) is False


def test_enforce_transition_to_zero_after_deletion(caplog, log):
    api = FakeCustomObjectsApi([make_cr("alpha", OLD_TS)])
    guard = SingletonGuard(api)
    events, emit = make_sink()
    guard.enforce(guard.list_crs(NAMESPACE), logger=log, emit=emit)
    caplog.clear()

    api.items.clear()
    guard.enforce([], logger=log, emit=emit)
    guard.enforce([], logger=log, emit=emit)

    assert events == []
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert "idle" in infos[0].getMessage()


# --- central gate: registry surgery ---------------------------------------------


def make_registry_with_handlers() -> kopf.OperatorRegistry:
    registry = kopf.OperatorRegistry()

    @kopf.timer(GROUP, "v1alpha1", PLURAL, interval=30.0, registry=registry)
    def oscm_timer(body: dict, spec: dict, **_: object):
        return ("served", spec.get("serverUrl"))

    @kopf.timer("apps", "v1", "deployments", interval=30.0, registry=registry)
    def deployment_timer(body: dict, **_: object):
        return "deployments-are-not-gated"

    return registry


def oscm_handlers(registry):
    return [h for h in registry._spawning._handlers if singleton._selector_matches_oscms(h)]


def test_install_wraps_only_oscm_spawning_handlers():
    registry = make_registry_with_handlers()
    assert install_singleton_guard(registry=registry) == 1

    spawners = registry._spawning._handlers
    assert len(spawners) == 2
    oscm = next(h for h in spawners if singleton._selector_matches_oscms(h))
    deployment = next(h for h in spawners if not singleton._selector_matches_oscms(h))
    assert oscm.id.endswith("oscm_timer")
    assert getattr(oscm.fn, singleton.GUARD_MARKER, False) is True
    assert oscm.fn.__name__ == "oscm_timer"  # functools.wraps: id/name preserved
    assert getattr(deployment.fn, singleton.GUARD_MARKER, False) is False

    # idempotent: a second install must not double-wrap
    assert install_singleton_guard(registry=registry) == 0


def test_install_does_not_touch_watching_handlers():
    registry = kopf.OperatorRegistry()

    @kopf.on.event(GROUP, "v1alpha1", PLURAL, registry=registry)
    def evt(body: dict, **_: object): ...

    assert install_singleton_guard(registry=registry) == 0
    assert registry._watching._handlers[0].fn is evt


def test_gated_wrapper_serves_only_oldest(monkeypatch, log, guard_two_crs):
    registry = make_registry_with_handlers()
    assert install_singleton_guard(registry=registry) == 1
    gated = oscm_handlers(registry)[0].fn
    monkeypatch.setattr(singleton, "_process_guard", guard_two_crs)

    # loser: the handler body must not run at all — beta's spec never drives behavior
    assert (
        gated(
            body=make_cr("beta", NEW_TS, uid="uid-beta"),
            spec={"serverUrl": "http://b.test"},
            namespace=NAMESPACE,
            name="beta",
            logger=log,
        )
        is None
    )

    # winner: the original handler runs and returns its value (alpha's spec)
    assert (
        gated(
            body=make_cr("alpha", OLD_TS, uid="uid-alpha"),
            spec={"serverUrl": "http://a.test"},
            namespace=NAMESPACE,
            name="alpha",
            logger=log,
        )
        == ("served", "http://a.test")
    )


def test_gated_wrapper_accepts_non_dict_mapping_body(monkeypatch, log, guard_two_crs):
    """kopf >=1.4x delivers `body` as Body (a MappingView, NOT a dict subclass).

    Regression (live-found in-cluster during issue #67's walkthrough, the same
    dead-operator signature class as #79): a dict-only isinstance check made
    the gate skip EVERY in-cluster tick. The gate must accept any Mapping.
    types.MappingProxyType reproduces the shape: a Mapping, not a dict.
    """
    import types

    registry = make_registry_with_handlers()
    assert install_singleton_guard(registry=registry) == 1
    gated = oscm_handlers(registry)[0].fn
    monkeypatch.setattr(singleton, "_process_guard", guard_two_crs)

    winner_view = types.MappingProxyType(make_cr("alpha", OLD_TS, uid="uid-alpha"))
    assert not isinstance(winner_view, dict)  # the shape this test exists for
    assert (
        gated(
            body=winner_view,
            spec={"serverUrl": "http://a.test"},
            namespace=NAMESPACE,
            name="alpha",
            logger=log,
        )
        == ("served", "http://a.test")
    )


def test_gated_wrapper_fails_closed_on_api_error(caplog, log, monkeypatch):
    registry = make_registry_with_handlers()
    install_singleton_guard(registry=registry)
    gated = oscm_handlers(registry)[0].fn
    monkeypatch.setattr(singleton, "_process_guard", SingletonGuard(ExplodingCustomObjectsApi([])))

    result = gated(
        body=make_cr("alpha", OLD_TS), spec={}, namespace=NAMESPACE, name="alpha", logger=log
    )
    assert result is None  # tick skipped — never served on the assumption of being active
    assert any("singleton guard" in r.getMessage() for r in caplog.records)


# --- kopf wrappers --------------------------------------------------------------


def test_event_wrapper_enforces_on_conflict(caplog, log, monkeypatch, guard_two_crs):
    monkeypatch.setattr(singleton, "_process_guard", guard_two_crs)
    events, emit = make_sink()
    monkeypatch.setattr(singleton, "_emit_kopf_event", emit)

    singleton.singleton_guard_event(
        body=make_cr("beta", NEW_TS),
        namespace=NAMESPACE,
        name="beta",
        logger=log,
        patch={},
        type="ADDED",
    )
    assert event_triples(events) == [
        ("beta", "Warning", SINGLETON_CONFLICT_EVENT),
        ("alpha", "Normal", SINGLETON_ACTIVE_EVENT),
    ]


def test_event_wrapper_survives_api_failure(caplog, log, monkeypatch):
    monkeypatch.setattr(singleton, "_process_guard", SingletonGuard(ExplodingCustomObjectsApi([])))
    events, emit = make_sink()
    monkeypatch.setattr(singleton, "_emit_kopf_event", emit)

    singleton.singleton_guard_event(
        body=make_cr("alpha", OLD_TS),
        namespace=NAMESPACE,
        name="alpha",
        logger=log,
        patch={},
        type="ADDED",
    )
    assert events == []
    assert any("could not list OSCM CRs" in r.getMessage() for r in caplog.records)


def test_startup_wrapper_zero_crs_logs_idle_once(caplog, log, monkeypatch):
    guard = SingletonGuard(FakeCustomObjectsApi([]))
    monkeypatch.setattr(singleton, "_process_guard", guard)
    monkeypatch.setenv("POD_NAMESPACE", NAMESPACE)
    events, emit = make_sink()
    monkeypatch.setattr(singleton, "_emit_kopf_event", emit)

    singleton.singleton_guard_startup(logger=log)
    singleton.singleton_guard_startup(logger=log)
    assert events == []
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1


def test_startup_wrapper_without_pod_namespace_skips(monkeypatch):
    guard = SingletonGuard(FakeCustomObjectsApi([]))
    monkeypatch.setattr(singleton, "_process_guard", guard)
    monkeypatch.delenv("POD_NAMESPACE", raising=False)

    singleton.singleton_guard_startup(logger=LOGGER)
    assert guard._custom_api.list_calls == 0


# --- passive guarantee -----------------------------------------------------------


def test_guard_never_mutates_losing_crs(guard_two_crs):
    api = guard_two_crs._custom_api
    before = copy.deepcopy(api.items)
    events, emit = make_sink()
    guard_two_crs.enforce(guard_two_crs.list_crs(NAMESPACE), logger=LOGGER, emit=emit)
    assert len(events) == 2  # noise only
    guard_two_crs.is_active(make_cr("beta", NEW_TS), NAMESPACE)
    assert not hasattr(api, "patch_calls")  # read-only fake has nothing to mutate with
    assert api.items == before  # losing CR untouched: passive policing (D05)


# --- guard construction: k8s config before the client (#79) ----------------------


@pytest.fixture
def default_configuration_restored():
    """Snapshot/restore client-python's global default ``Configuration``.

    The fake in-cluster loader below mirrors the side effect of the real
    ``load_incluster_config`` that matters here: installing a default
    ``Configuration`` carrying a host, which a subsequently constructed
    ``CustomObjectsApi`` picks up. Without the restore, that global would leak
    across tests.
    """
    prev = kubernetes.client.Configuration._default
    yield
    kubernetes.client.Configuration._default = prev


def patch_config_loaders(monkeypatch, *, incluster_error: Exception | None = None):
    """Spy on the k8s config loaders plus the guard's client construction.

    Returns ``(calls, clients, sentinel_host)`` where ``calls`` records the
    ordered ``load_incluster_config`` / ``load_kube_config`` /
    ``CustomObjectsApi()`` invocations — that ordering is the #79 contract:
    config first, client second.
    """
    calls: list[str] = []
    clients: list[CustomObjectsApi] = []
    sentinel_host = "https://apiserver.incluster.example:6443"

    def fake_incluster() -> None:
        calls.append("load_incluster_config")
        if incluster_error is not None:
            raise incluster_error
        cfg = kubernetes.client.Configuration()
        cfg.host = sentinel_host
        kubernetes.client.Configuration.set_default(cfg)

    def fake_kubeconfig(*args: object, **kwargs: object) -> None:
        calls.append("load_kube_config")

    def fake_custom_objects_api() -> CustomObjectsApi:
        calls.append("CustomObjectsApi()")
        client = CustomObjectsApi()  # the REAL class — picks up the loaded default config
        clients.append(client)
        return client

    monkeypatch.setattr(kubernetes.config, "load_incluster_config", fake_incluster)
    monkeypatch.setattr(kubernetes.config, "load_kube_config", fake_kubeconfig)
    monkeypatch.setattr(singleton, "CustomObjectsApi", fake_custom_objects_api)
    return calls, clients, sentinel_host


def test_get_guard_loads_config_before_building_client(monkeypatch, default_configuration_restored):
    """The #79 regression: the guard's client is constructed only AFTER a
    Kubernetes config is loaded. Before the fix, ``_get_guard`` built a bare
    ``CustomObjectsApi()`` — kr8s-based kopf (1.44+) never initializes
    client-python's default ``Configuration`` (``host == ''``), so every guard
    call raised LocationValueError and the kopf ``on.startup`` check never
    completed: pod Running, operator functionally dead.
    """
    calls, clients, sentinel_host = patch_config_loaders(monkeypatch)
    monkeypatch.setattr(singleton, "_process_guard", None)

    guard = singleton._get_guard()

    assert calls == ["load_incluster_config", "CustomObjectsApi()"]
    assert guard._custom_api is clients[0]
    assert guard._custom_api.api_client.configuration.host == sentinel_host


def test_get_guard_bare_client_dies_guard_client_does_not(
    monkeypatch, default_configuration_restored
):
    """Paired proof of the failure mode: with no config loaded, a bare
    ``CustomObjectsApi`` dies with ``LocationValueError('No host specified')``
    on its very first list call (the live in-pod symptom from #66/#79 — this
    assert is the pre-fix ``_get_guard`` behavior), while the guard's client,
    built after ``load_incluster_config``, carries the loaded host.
    """
    with pytest.raises(ValueError, match="No host specified"):
        kubernetes.client.CustomObjectsApi().list_namespaced_custom_object(
            GROUP, "v1alpha1", NAMESPACE, PLURAL
        )

    calls, _, sentinel_host = patch_config_loaders(monkeypatch)
    monkeypatch.setattr(singleton, "_process_guard", None)
    guard = singleton._get_guard()

    assert calls == ["load_incluster_config", "CustomObjectsApi()"]
    assert guard._custom_api.api_client.configuration.host == sentinel_host


def test_get_guard_falls_back_to_kubeconfig_out_of_cluster(monkeypatch):
    """In-cluster load fails (bare ``kopf run`` dev session) → kubeconfig path."""
    calls, clients, _ = patch_config_loaders(
        monkeypatch, incluster_error=ConfigException("host env not set")
    )
    monkeypatch.setattr(singleton, "_process_guard", None)

    guard = singleton._get_guard()

    assert calls == ["load_incluster_config", "load_kube_config", "CustomObjectsApi()"]
    assert guard._custom_api is clients[0]


def test_get_guard_builds_once_per_process(monkeypatch, default_configuration_restored):
    """The lazy process-wide guard loads config exactly once — later ticks reuse
    the built client instead of re-loading config on every call."""
    calls, _, _ = patch_config_loaders(monkeypatch)
    monkeypatch.setattr(singleton, "_process_guard", None)

    first = singleton._get_guard()
    again = singleton._get_guard()

    assert again is first
    assert calls == ["load_incluster_config", "CustomObjectsApi()"]


def no_config_loaders(monkeypatch):
    def no_config(*args: object, **kwargs: object) -> None:
        raise ConfigException("no kubeconfig anywhere")

    monkeypatch.setattr(kubernetes.config, "load_incluster_config", no_config)
    monkeypatch.setattr(kubernetes.config, "load_kube_config", no_config)
    monkeypatch.setattr(singleton, "_process_guard", None)


def test_gated_wrapper_skips_tick_when_no_config_loads(monkeypatch, caplog, log):
    """Fail-closed on total config failure: the gated handler skips the tick
    (Warning log, handler body never runs) instead of propagating
    ConfigException — the tick is retried on the next poll."""
    registry = make_registry_with_handlers()
    install_singleton_guard(registry=registry)
    gated = oscm_handlers(registry)[0].fn
    no_config_loaders(monkeypatch)

    assert (
        gated(
            body=make_cr("alpha", OLD_TS),
            spec={},
            namespace=NAMESPACE,
            name="alpha",
            logger=log,
        )
        is None
    )
    assert any("singleton guard" in r.getMessage() for r in caplog.records)


def test_startup_wrapper_survives_config_failure(caplog, log, monkeypatch):
    """The #79 symptom class: a guard config failure in the startup path must
    not prevent kopf from finishing startup — log a Warning and move on."""
    no_config_loaders(monkeypatch)
    monkeypatch.setenv("POD_NAMESPACE", NAMESPACE)

    singleton.singleton_guard_startup(logger=log)  # must not raise

    assert any("could not list OSCM CRs" in r.getMessage() for r in caplog.records)
