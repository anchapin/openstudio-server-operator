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

from _fakes import FakeSecretsCoreV1Api, encode_secret_value
from openstudio_operator import singleton
from openstudio_operator.client_factory import get_read_only_redis_client
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


class ListOnlyFakeCustomObjectsApi:
    """In-memory list-only CustomObjectsApi stand-in (the guard never mutates).

    Kept local and deliberately NOT the shared ``_fakes.FakeCustomObjectsApi``
    (issue #474): this fake must carry NO patch/get surface at all —
    ``test_guard_never_mutates_losing_crs`` proves the guard's read-only
    contract via ``assert not hasattr(api, "patch_calls")``, which the
    shared union fake (eager patch counters) cannot satisfy.
    """

    def __init__(self, items: list[dict]) -> None:
        self.items = copy.deepcopy(items)
        self.list_calls = 0

    def list_namespaced_custom_object(self, group, version, namespace, plural):
        assert (group, version, plural) == (GROUP, "v1alpha1", PLURAL)
        assert namespace == NAMESPACE
        self.list_calls += 1
        return {"items": copy.deepcopy(self.items)}


class ExplodingCustomObjectsApi(ListOnlyFakeCustomObjectsApi):
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
        ListOnlyFakeCustomObjectsApi(
            items=[
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
    guard = SingletonGuard(ListOnlyFakeCustomObjectsApi(items=[]))
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
    guard = SingletonGuard(ListOnlyFakeCustomObjectsApi(items=[make_cr("solo", OLD_TS)]))
    events, emit = make_sink()
    guard.enforce(guard.list_crs(NAMESPACE), logger=log, emit=emit)
    guard.enforce(guard.list_crs(NAMESPACE), logger=log, emit=emit)
    assert events == []
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert "solo" in infos[0].getMessage()


def test_enforce_zero_crs_single_idle_log_no_events(caplog, log):
    guard = SingletonGuard(ListOnlyFakeCustomObjectsApi(items=[]))
    events, emit = make_sink()
    guard.enforce([], logger=log, emit=emit)
    guard.enforce([], logger=log, emit=emit)
    assert events == []
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert "idle" in infos[0].getMessage()


def test_enforce_newer_cr_appears_winner_unchanged(caplog, log):
    api = ListOnlyFakeCustomObjectsApi(items=[make_cr("alpha", OLD_TS, uid="uid-alpha")])
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
    api = ListOnlyFakeCustomObjectsApi(items=[make_cr("beta", NEW_TS, uid="uid-beta")])
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
    api = ListOnlyFakeCustomObjectsApi(items=[make_cr("alpha", OLD_TS)])
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

    # Issue #250 — the cross-check inside `install_singleton_guard`
    # requires every OSCM timer to be in the Python-level registry.
    # Pre-#250 tests constructed a fake OSCM timer inline; the test
    # factory now registers it explicitly so the gate's cross-check
    # passes. The handler id below is the kopf-assigned id (which
    # includes the ``<locals>`` qualifier for nested-scope handlers) —
    # the gate's cross-check is id-based, so a literal ``"oscm_timer"``
    # would fail to match. The non-OSCM deployment_timer below is
    # intentionally NOT registered — `_selector_matches_oscms` excludes
    # it before the cross-check runs.
    from openstudio_operator import _oscm_handlers

    kopf_id = next(
        h.id for h in registry._spawning._handlers
        if h.fn is oscm_timer
    )
    _oscm_handlers.register(kopf_id, oscm_timer)

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


# --- Issue #491 — singleton_wrapped_handlers boot gauge -------------------------


def _wrapped_handlers_gauge_value() -> float:
    """Current value of ``SINGLETON_WRAPPED_HANDLERS`` (module-global gauge)."""
    return float(singleton.SINGLETON_WRAPPED_HANDLERS._value.get())


def make_registry_with_n_oscm_timers(count: int) -> kopf.OperatorRegistry:
    """Registry with ``count`` OSCM timers (all #250-registered) + one
    non-OSCM deployment timer (never gated, never counted)."""
    registry = kopf.OperatorRegistry()
    from openstudio_operator import _oscm_handlers

    fns = []
    for _ in range(count):

        @kopf.timer(GROUP, "v1alpha1", PLURAL, interval=30.0, registry=registry)
        def oscm_timer_n(body: dict, **_: object):
            return "served"

        fns.append(oscm_timer_n)

    # Same identity-based id lookup as make_registry_with_handlers: kopf
    # dedupes colliding handler ids (identical qualnames from this loop)
    # with suffixes, so the id must be read back per fn, not assumed.
    for fn in fns:
        kopf_id = next(h.id for h in registry._spawning._handlers if h.fn is fn)
        _oscm_handlers.register(kopf_id, fn)

    @kopf.timer("apps", "v1", "deployments", interval=30.0, registry=registry)
    def deployment_timer(body: dict, **_: object):
        return "deployments-are-not-gated"

    return registry


def test_install_sets_wrapped_handlers_gauge_to_wrap_count():
    """Issue #491: after installing the guard over a registry with OSCM
    timers, ``SINGLETON_WRAPPED_HANDLERS`` equals the number of wrapped
    handlers. The idempotent re-install (0 NEW wraps) must NOT clobber
    the gauge to 0 — the gauge counts handlers CARRYING the gate marker,
    and 0 is precisely the silent-unwrap alert value."""
    registry = make_registry_with_handlers()
    assert install_singleton_guard(registry=registry) == 1
    assert _wrapped_handlers_gauge_value() == 1.0
    # idempotent re-install: returns 0 newly-wrapped, gauge keeps the
    # actual gated population.
    assert install_singleton_guard(registry=registry) == 0
    assert _wrapped_handlers_gauge_value() == 1.0


def test_wrapped_handlers_gauge_counts_every_oscm_timer():
    """Issue #491: the gauge tracks N, not just the 1-handler shape — a
    future handler module added to the import block moves the healthy
    reading, and the non-OSCM deployment timer is never counted."""
    registry = make_registry_with_n_oscm_timers(3)
    assert install_singleton_guard(registry=registry) == 3
    assert _wrapped_handlers_gauge_value() == 3.0


def test_wrapped_handlers_gauge_zero_when_no_oscm_timers():
    """Issue #491: a registry with no matching handlers leaves the gauge
    at 0 — a legitimate 0 (nothing to gate), distinct from the alert
    semantics only when the cluster EXPECTS timers (the alert's
    ``for: 5m`` + expectation context carries that distinction)."""
    registry = kopf.OperatorRegistry()

    @kopf.timer("apps", "v1", "deployments", interval=30.0, registry=registry)
    def deployment_timer(body: dict, **_: object):
        return "deployments-are-not-gated"

    assert install_singleton_guard(registry=registry) == 0
    assert _wrapped_handlers_gauge_value() == 0.0


def test_wrapped_handlers_gauge_zero_when_internals_not_as_expected(caplog):
    """Issue #491 — the silent-unwrap shape, made scrapeable: a kopf
    upgrade that moves ``registry._spawning._handlers`` (the private
    layout the gate walks — the documented kopf-pin failure mode) sends
    the gate down its internals-mismatch branch. Pre-#491 that outcome
    was a WARNING log only; the gauge must now read 0.0 so
    ``openstudio_operator_singleton_wrapped_handlers == 0`` fires."""

    class _InternalsShiftedRegistry:
        # _spawning carries no _handlers list — the post-upgrade shape
        # the gate's isinstance(handlers, list) check rejects.
        _spawning = object()

    assert install_singleton_guard(registry=_InternalsShiftedRegistry()) == 0
    assert _wrapped_handlers_gauge_value() == 0.0
    assert any(
        "registry internals not as expected" in r.getMessage() for r in caplog.records
    )


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
    monkeypatch.setattr(singleton, "_process_guard", SingletonGuard(ExplodingCustomObjectsApi(items=[])))

    result = gated(
        body=make_cr("alpha", OLD_TS), spec={}, namespace=NAMESPACE, name="alpha", logger=log
    )
    assert result is None  # tick skipped — never served on the assumption of being active
    assert any("singleton guard" in r.getMessage() for r in caplog.records)


# --- Issue #307 — singleton guard wrapper must bump HANDLER_TICK_FAILURES_TOTAL on API errors ---


class _ServiceUnavailableApi(ListOnlyFakeCustomObjectsApi):
    """list_namespaced_custom_object raises ApiException(503).

    The acceptance criterion for issue #307 — a sustained apiserver outage
    must surface on the per-module counter, not just the WARNING log line.
    The 503 / "Service Unavailable" shape matches the live apiserver
    overload signature (vs. the generic ``ExplodingCustomObjectsApi``'s
    500/boom) so the test exercises the documented failure path.
    """

    def list_namespaced_custom_object(self, group, version, namespace, plural):
        raise ApiException(status=503, reason="Service Unavailable")


def _handler_tick_failure_total(module: str, error_type: str) -> float:
    """Sum of ``handler_tick_failures_total{module=...,error_type=...}`` across all label series."""
    total = 0.0
    for metric in singleton.HANDLER_TICK_FAILURES_TOTAL.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total") and sample.labels.get("module") == module \
                    and sample.labels.get("error_type") == error_type:
                total += float(sample.value)
    return total


def test_gated_wrapper_bumps_handler_tick_failures_total_on_api_exception(
    caplog, log, monkeypatch
):
    """Issue #307 acceptance: when ``_get_guard().is_active`` raises
    ``ApiException(503)``, the gate's outer wrapper must increment
    ``HANDLER_TICK_FAILURES_TOTAL{module=<handler_id>,error_type=ApiException}``
    in addition to logging the WARNING. The pre-#307 wrapper suppressed the
    exception silently — a sustained apiserver outage produced zero per-module
    increments and SREs (alerting on the #117 counter) had no signal.

    The module label is the wrapped handler's ``__name__`` (preserved by
    ``functools.wraps`` at install time — see ``oscm_timer`` in
    ``make_registry_with_handlers``); error_type is the exception class name.
    """
    registry = make_registry_with_handlers()
    install_singleton_guard(registry=registry)
    gated = oscm_handlers(registry)[0].fn
    monkeypatch.setattr(
        singleton, "_process_guard", SingletonGuard(_ServiceUnavailableApi([]))
    )

    # Sanity: ``oscm_timer`` is the wrapped handler's preserved __name__ —
    # the counter module label must match exactly so the #117 alert can
    # group observations by handler module.
    assert gated.__name__ == "oscm_timer"

    before = _handler_tick_failure_total("oscm_timer", "ApiException")

    result = gated(
        body=make_cr("alpha", OLD_TS), spec={}, namespace=NAMESPACE, name="alpha", logger=log
    )
    assert result is None  # fail-closed posture preserved (D05 / D12)

    after = _handler_tick_failure_total("oscm_timer", "ApiException")
    assert after - before == 1.0  # exactly one increment per suppressed tick

    # The WARNING log line is the companion signal — both must fire so an
    # SRE can correlate log forwarding with the Prometheus increment.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("singleton guard" in r.getMessage() and "ApiException" in r.getMessage()
               for r in warnings)


# --- kopf wrappers --------------------------------------------------------------


def test_event_wrapper_enforces_on_conflict(caplog, log, monkeypatch, guard_two_crs):
    # Issue #304 — the URL-guard Warning Event now fires via
    # ``emit_kopf_event`` directly (no more queue/drain detour through
    # ``openstudio_operator.handlers``). Build a guard whose CRs carry an
    # explicit ``redisUrl`` so the URL-guard branch in ``_check`` is a
    # no-op for this case; the conflict Events are the only thing under
    # test. The companion ``test_event_wrapper_url_guard_fires_directly``
    # below exercises the direct URL-guard path explicitly.
    populated_guard = SingletonGuard(
        ListOnlyFakeCustomObjectsApi(
            items=[
                make_cr(
                    "alpha", OLD_TS, uid="uid-alpha",
                    spec={"serverUrl": "http://a.test", "redisUrl": "redis://queue.test:6379"},
                ),
                make_cr(
                    "beta", NEW_TS, uid="uid-beta",
                    spec={"serverUrl": "http://b.test", "redisUrl": "redis://queue.test:6379"},
                ),
            ]
        )
    )
    monkeypatch.setattr(singleton, "_process_guard", populated_guard)
    events, emit = make_sink()
    monkeypatch.setattr(singleton, "emit_kopf_event", emit)

    singleton.singleton_guard_event(
        body=make_cr(
            "beta", NEW_TS,
            spec={"serverUrl": "http://b.test", "redisUrl": "redis://queue.test:6379"},
        ),
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


def test_event_wrapper_url_guard_fires_directly(caplog, log, monkeypatch):
    """Issue #304: empty ``spec.redisUrl`` emits ``RedisUrlEmpty`` via ``emit_kopf_event`` directly.

    Pre-#304 the URL-guard Warning Event was queued and drained on the next
    OSCM watch tick by the consolidated ``_drain_queued_warning_events``
    handler in :mod:`openstudio_operator.handlers`. The deferral
    sidestepped an inverted module dependency (singleton → handlers) but
    was architecturally unnecessary: ``_check`` is only called from
    ``@kopf.on.startup`` and ``@kopf.on.event`` callbacks, where
    ``kopf.event(...)`` is callable directly. The fix in
    :func:`openstudio_operator.singleton._emit_redis_url_guard_events`
    routes the Event through the same :func:`emit_kopf_event` shim the
    SINGLETON_* conflict/active events use — so a test that monkeypatches
    that shim MUST see the ``RedisUrlEmpty`` Event on the same path as
    the conflict Events.

    The CR carries NO ``redisUrl`` (the URL-guard's trigger), so the
    singleton's loud-half (``enforce``) sees a single CR (no conflict)
    and stays quiet — only the URL-guard Event fires. The companion
    conflict test (``test_event_wrapper_enforces_on_conflict``) covers
    the conflict branch.
    """
    guard = SingletonGuard(
        ListOnlyFakeCustomObjectsApi(
            items=[
                make_cr(
                    "alpha", OLD_TS, uid="uid-alpha",
                    spec={"serverUrl": "http://a.test"},
                ),
            ]
        )
    )
    monkeypatch.setattr(singleton, "_process_guard", guard)
    events, emit = make_sink()
    monkeypatch.setattr(singleton, "emit_kopf_event", emit)
    # Each test run caches its warned set in a module-level variable;
    # clear it so the Event is re-emitted deterministically.
    monkeypatch.setattr(singleton, "_redis_url_warned", set())

    singleton.singleton_guard_event(
        body=make_cr("alpha", OLD_TS, spec={"serverUrl": "http://a.test"}),
        namespace=NAMESPACE,
        name="alpha",
        logger=log,
        patch={},
        type="ADDED",
    )
    # Exactly one Event: Warning / RedisUrlEmpty attached to the CR
    # (the singleton-conflict branch is silent — single CR, no losers).
    assert event_triples(events) == [
        ("alpha", "Warning", "RedisUrlEmpty"),
    ], (
        f"URL-guard Warning Event must fire through emit_kopf_event "
        f"directly (issue #304). Got: {event_triples(events)!r}. The "
        f"regression that re-adds a singleton→handlers import would "
        f"break this assertion: the deferred import + queue detour "
        f"captured the Event in handlers._sink, not in the patched "
        f"emit sink."
    )


def test_event_wrapper_url_guard_is_idempotent_per_cr(caplog, log, monkeypatch):
    """Issue #116/304: the URL-guard fires at most ONCE per ``(ns, name)`` per operator restart.

    The single-callback cache in :data:`openstudio_operator.singleton._redis_url_warned`
    is the noise-gate that keeps a re-evaluated singleton guard from
    re-firing the same Warning Event on every kopf watch tick. The
    guard's ``enforce`` method also deduplicates on state-change (so a
    second call to ``singleton_guard_event`` for the same CR snapshot
    is silent on the conflict side too); the URL-guard cache is
    separate and has its own invariant. This test pins the
    once-per-(ns,name) contract by calling the URL-guard helper
    twice and asserting the second call's ``events`` list is empty
    for the ``RedisUrlEmpty`` reason.
    """
    guard = SingletonGuard(
        ListOnlyFakeCustomObjectsApi(
            items=[make_cr("alpha", OLD_TS, uid="uid-alpha", spec={"serverUrl": "http://a.test"})]
        )
    )
    monkeypatch.setattr(singleton, "_process_guard", guard)
    events, emit = make_sink()
    monkeypatch.setattr(singleton, "emit_kopf_event", emit)
    monkeypatch.setattr(singleton, "_redis_url_warned", set())

    # First call: the URL-guard fires for alpha (empty redisUrl).
    singleton.singleton_guard_event(
        body=make_cr("alpha", OLD_TS, spec={"serverUrl": "http://a.test"}),
        namespace=NAMESPACE,
        name="alpha",
        logger=log,
        patch={},
        type="ADDED",
    )
    first = [t for t in event_triples(events) if t[2] == "RedisUrlEmpty"]
    assert first == [("alpha", "Warning", "RedisUrlEmpty")], (
        f"First call must fire one RedisUrlEmpty Event; got {first!r}. "
        f"See issue #116."
    )

    # Second call with a different (NEW) body delivery, same CR — the
    # cache must suppress the duplicate. The conflict branch also stays
    # silent because the snapshot is unchanged.
    events.clear()
    singleton.singleton_guard_event(
        body=make_cr("alpha", OLD_TS, spec={"serverUrl": "http://a.test"}),
        namespace=NAMESPACE,
        name="alpha",
        logger=log,
        patch={},
        type="ADDED",
    )
    second_url = [t for t in event_triples(events) if t[2] == "RedisUrlEmpty"]
    assert second_url == [], (
        f"Second call on the same CR must NOT re-fire RedisUrlEmpty "
        f"(issue #116 once-per-(ns,name) cache). Got: {second_url!r}. "
        f"The cache lives in "
        f"openstudio_operator.singleton._redis_url_warned."
    )


def test_url_guard_silent_when_secret_ref_present(caplog, log, monkeypatch):
    """Issue #463: empty ``spec.redisUrl`` + populated
    ``spec.redisCredentials.secretRef`` is the RECOMMENDED production shape
    (the URL resolves from the Secret at client-construction time), so the
    #116 ``RedisUrlEmpty`` Warning must NOT fire for it. The fence stays
    armed for an empty URL with NO secretRef (the companion tests above).

    Since #606 the guard also PROBES the secretRef resolution once per CR
    (the fail-visible RBAC fence); a resolvable Secret yields no event, so
    this test doubles as the probe's success-path silence pin — the fake
    Secret API keeps it hermetic (no live read on a kubeconfig-bearing
    host)."""
    secret_ref_spec = {
        "serverUrl": "http://a.test",
        "redisUrl": "",
        "redisCredentials": {"secretRef": {"name": "openstudio-redis", "key": "redis-url"}},
    }
    guard = SingletonGuard(
        ListOnlyFakeCustomObjectsApi(
            items=[make_cr("alpha", OLD_TS, uid="uid-alpha", spec=secret_ref_spec)]
        )
    )
    monkeypatch.setattr(singleton, "_process_guard", guard)
    fake_secret = FakeSecretsCoreV1Api(
        {"redis-url": encode_secret_value("redis://:pw@queue.openstudio-server.svc.cluster.local:6379")}
    )
    monkeypatch.setattr(singleton, "_operator_core_api", fake_secret)
    monkeypatch.setattr(singleton, "_redis_secret_ref_forbidden_warned", set())
    events, emit = make_sink()
    monkeypatch.setattr(singleton, "emit_kopf_event", emit)
    monkeypatch.setattr(singleton, "_redis_url_warned", set())

    get_read_only_redis_client.cache_clear()
    try:
        singleton.singleton_guard_event(
            body=make_cr("alpha", OLD_TS, spec=secret_ref_spec),
            namespace=NAMESPACE,
            name="alpha",
            logger=log,
            patch={},
            type="ADDED",
        )
    finally:
        get_read_only_redis_client.cache_clear()

    url_events = [t for t in event_triples(events) if t[2] == "RedisUrlEmpty"]
    assert url_events == [], (
        f"RedisUrlEmpty must not fire when spec.redisCredentials.secretRef "
        f"is populated (issue #463 — Secret-sourced URL is the recommended "
        f"shape for an empty spec.redisUrl). Got: {url_events!r}."
    )
    forbidden = [t for t in event_triples(events) if t[2] == "RedisSecretRefForbidden"]
    assert forbidden == [], (
        f"a RESOLVABLE secretRef must not fire RedisSecretRefForbidden "
        f"(issue #606 — only the RBAC-denied 403 case does); got "
        f"{forbidden!r}"
    )


def test_url_guard_fires_when_secret_ref_is_malformed(caplog, log, monkeypatch):
    """A secretRef missing name/key does NOT count as set (the tolerant
    ``_has_redis_secret_ref`` shape check) — an empty redisUrl with a
    half-configured secretRef still trips the #116 fence rather than
    silently operating with no credential source at all."""
    malformed_spec = {
        "serverUrl": "http://a.test",
        "redisUrl": "",
        "redisCredentials": {"secretRef": {"name": "openstudio-redis"}},
    }
    guard = SingletonGuard(
        ListOnlyFakeCustomObjectsApi(
            items=[make_cr("alpha", OLD_TS, uid="uid-alpha", spec=malformed_spec)]
        )
    )
    monkeypatch.setattr(singleton, "_process_guard", guard)
    events, emit = make_sink()
    monkeypatch.setattr(singleton, "emit_kopf_event", emit)
    monkeypatch.setattr(singleton, "_redis_url_warned", set())

    singleton.singleton_guard_event(
        body=make_cr("alpha", OLD_TS, spec=malformed_spec),
        namespace=NAMESPACE,
        name="alpha",
        logger=log,
        patch={},
        type="ADDED",
    )

    assert ("alpha", "Warning", "RedisUrlEmpty") in event_triples(events)

    # Second scenario in the same test (pre-existing, kept verbatim): the
    # guard's CR LIST exploding must not crash the event wrapper — the
    # #116/#606 emit paths run after the list try/except returns.
    monkeypatch.setattr(singleton, "_process_guard", SingletonGuard(ExplodingCustomObjectsApi(items=[])))
    events, emit = make_sink()
    monkeypatch.setattr(singleton, "emit_kopf_event", emit)

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


# ---- Issue #590: status-only watch events skip the guard relist ----------------
#
# The operator's own StatusStore RMW writes (soft-stop anchors, requeue
# anchors, startedSince bookkeeping, deferredEvents mirrors — several per
# SLA tick) each fire a MODIFIED watch event, and pre-#590 every one of
# them re-LISTed the namespace's CRs even though a .status patch can
# never change the election: seniority is a function of immutable
# metadata (creationTimestamp/name/uid) plus the SET of CRs, and any
# change to that set arrives as its own ADDED/DELETED event, which is
# never skipped. The skip requires: type == MODIFIED, a matching
# policy-surface fingerprint (spec + uid/labels/annotations/
# deletionTimestamp — NOT resourceVersion/managedFields/status), and a
# guard that already resolved the namespace (fail-closed until then).

_SPEC_590 = {"serverUrl": "http://a.test", "redisUrl": "redis://queue.test:6379"}


def _wire_guard_590(monkeypatch, log, items):
    """Install a fresh guard + sink + fresh one-shot caches over ``items``."""
    api = ListOnlyFakeCustomObjectsApi(items=items)
    monkeypatch.setattr(singleton, "_process_guard", SingletonGuard(api))
    events, emit = make_sink()
    monkeypatch.setattr(singleton, "emit_kopf_event", emit)
    monkeypatch.setattr(singleton, "_redis_url_warned", set())
    monkeypatch.setattr(singleton, "_redis_secret_ref_forbidden_warned", set())
    monkeypatch.setattr(singleton, "_guard_last_seen_surface", {})
    return api, events


def _fire_guard_event(event_type, body, log, **overrides):
    kwargs: dict = {
        "body": body,
        "namespace": NAMESPACE,
        "name": body.get("metadata", {}).get("name", "alpha"),
        "logger": log,
        "patch": {},
        "type": event_type,
    }
    kwargs.update(overrides)
    singleton.singleton_guard_event(**kwargs)


def _status_only_variant(cr: dict) -> dict:
    """The same CR after a StatusStore RMW write: status subresource added,
    resourceVersion/managedFields churned — everything the #590 fingerprint
    covers (uid/spec/labels/annotations) unchanged."""
    drifted = copy.deepcopy(cr)
    drifted["status"] = {"softStops": {"a-1": "2026-08-23T00:00:00+00:00"}}
    drifted["metadata"]["resourceVersion"] = "42"
    drifted["metadata"]["managedFields"] = [
        {"manager": "OpenAPI-Generator", "time": "2026-08-23T00:00:00Z"}
    ]
    return drifted


def test_guard_event_skips_relist_on_status_only_update(caplog, log, monkeypatch):
    """Issue #590 acceptance: a status-subresource-only MODIFIED event of a CR
    the guard already resolved does NOT re-list the namespace's CRs."""
    cr = make_cr("alpha", OLD_TS, uid="uid-alpha", spec=_SPEC_590)
    api, events = _wire_guard_590(monkeypatch, log, [cr])

    # Boot-shaped first event: full check, surface recorded.
    with caplog.at_level(logging.DEBUG, logger="singleton-test"):
        _fire_guard_event("ADDED", cr, log)
    assert api.list_calls == 1

    events.clear()
    with caplog.at_level(logging.DEBUG, logger="singleton-test"):
        _fire_guard_event("MODIFIED", _status_only_variant(cr), log)

    assert api.list_calls == 1, (
        f"Status-only update re-listed CRs (issue #590): list_calls went to "
        f"{api.list_calls}. A .status patch cannot change the election — the "
        f"relist is pure apiserver load."
    )
    assert events == [], "The skipped event must not emit anything either."
    assert any("#590" in r.getMessage() and "relist skipped" in r.getMessage()
               for r in caplog.records), (
        "The skip should be observable at debug level for on-call triage."
    )


def test_guard_event_relists_on_spec_change_after_status_only_skip(caplog, log, monkeypatch):
    """The skip is per-event, not sticky: after a skipped status-only event, a
    MODIFIED event whose spec changed (redisUrl flipped) forces the relist."""
    cr = make_cr("alpha", OLD_TS, uid="uid-alpha", spec=_SPEC_590)
    api, _ = _wire_guard_590(monkeypatch, log, [cr])
    _fire_guard_event("ADDED", cr, log)
    _fire_guard_event("MODIFIED", _status_only_variant(cr), log)
    assert api.list_calls == 1  # the #590 skip under test

    spec_changed = copy.deepcopy(cr)
    spec_changed["spec"]["redisUrl"] = "redis://other-queue.test:6379"
    _fire_guard_event("MODIFIED", spec_changed, log)

    assert api.list_calls == 2, (
        f"A spec change must NOT be classified status-only (issue #590 + the "
        f"#116/#606 guard events read spec). list_calls={api.list_calls}."
    )


def test_guard_event_never_skips_listing_or_added_events(caplog, log, monkeypatch):
    """The #163 boot path (initial listing, ``type is None``) and ADDED events
    always run the full check — even with an already-recorded identical
    surface — and DELETED both relists and evicts the fingerprint."""
    cr = make_cr("alpha", OLD_TS, uid="uid-alpha", spec=_SPEC_590)
    api, _ = _wire_guard_590(monkeypatch, log, [cr])

    _fire_guard_event("ADDED", cr, log)  # surface recorded here
    assert api.list_calls == 1

    _fire_guard_event(None, cr, log)  # initial-listing / resync shape
    assert api.list_calls == 2, "type is None (listing/resync) must never skip."

    _fire_guard_event("ADDED", cr, log)
    assert api.list_calls == 3, "ADDED changes the CR set — must never skip."

    _fire_guard_event("DELETED", cr, log)
    assert api.list_calls == 4, "DELETED changes the CR set — must never skip."
    assert ("openstudio-server", "alpha") not in singleton._guard_last_seen_surface, (
        "DELETED must evict the fingerprint entry (a same-name recreation "
        "starts fresh)."
    )


def test_guard_event_relists_when_uid_changes_delete_recreate(caplog, log, monkeypatch):
    """#364 delete+recreate: a recreated CR carries a new uid, so the surface
    fingerprint mismatches and the full relist runs — the skip can never mask
    a recycled name."""
    cr = make_cr("alpha", OLD_TS, uid="uid-alpha", spec=_SPEC_590)
    api, _ = _wire_guard_590(monkeypatch, log, [cr])
    _fire_guard_event("ADDED", cr, log)
    _fire_guard_event("MODIFIED", _status_only_variant(cr), log)
    assert api.list_calls == 1

    recreated = copy.deepcopy(cr)
    recreated["metadata"]["uid"] = "uid-recreated"
    recreated["metadata"]["resourceVersion"] = "43"
    _fire_guard_event("MODIFIED", recreated, log)

    assert api.list_calls == 2, (
        f"A uid change (delete+recreate, #364) must force the relist; got "
        f"list_calls={api.list_calls}."
    )


def test_guard_event_status_only_skip_fails_closed_until_state_resolved(caplog, log, monkeypatch):
    """Fail-closed: while the guard has never completed a resolution
    (``_last_state`` is None — e.g. the initial relist failed), a
    status-only event with a matching surface STILL runs the full check."""
    cr = make_cr("alpha", OLD_TS, uid="uid-alpha", spec=_SPEC_590)
    broken = SingletonGuard(ExplodingCustomObjectsApi(items=[cr]))
    monkeypatch.setattr(singleton, "_process_guard", broken)
    _events, emit = make_sink()
    monkeypatch.setattr(singleton, "emit_kopf_event", emit)
    monkeypatch.setattr(singleton, "_guard_last_seen_surface", {})

    # First event: the relist EXPLODES (swallowed warning), state unresolved,
    # but the surface IS recorded — the worst case for the skip gate.
    _fire_guard_event("ADDED", cr, log)

    working_api = ListOnlyFakeCustomObjectsApi(items=[cr])
    monkeypatch.setattr(singleton, "_process_guard", SingletonGuard(working_api))

    _fire_guard_event("MODIFIED", _status_only_variant(cr), log)

    assert working_api.list_calls == 1, (
        f"An unresolved guard (no successful enforce yet) must NOT skip the "
        f"relist (issue #590 fail-closed); list_calls={working_api.list_calls}."
    )


# ---- Issue #606: the RBAC resourceNames fence is fail-visible ----------------
#
# The default operator Role grants secrets:get only on the canonical
# Secret name(s) (deploy/rbac.yaml resourceNames — default
# 'openstudio-redis'); the CRD pattern stays wider, so a CR naming a
# custom openstudio-redis-* Secret is apply-legal and DENIED at read time
# (403). The guard probes the secretRef resolution once per CR and, on
# the 403, emits a one-time RedisSecretRefForbidden Warning naming the
# RBAC cause and both remedies (widen resourceNames / fix the secretRef).
# These tests fake the Secret read at singleton._operator_core_api (the
# same seam tests/test_client_factory.py uses) so nothing touches a live
# cluster.

_SECRET_REF_SPEC_606 = {
    "serverUrl": "http://a.test",
    "redisUrl": "",
    # Pattern-legal (CRD) but NOT in the default Role's resourceNames —
    # the exact custom-name scenario #606's fail-visible variant owns.
    "redisCredentials": {
        "secretRef": {"name": "openstudio-redis-url", "key": "redis-url"}
    },
}


def _guard_with_secret_ref_cr(fake_secret):
    """Wire ``_process_guard`` to one CR naming a custom redis Secret."""
    return SingletonGuard(
        ListOnlyFakeCustomObjectsApi(
            items=[make_cr("alpha", OLD_TS, uid="uid-alpha", spec=_SECRET_REF_SPEC_606)]
        )
    )


def _run_guard_event(monkeypatch, log, fake_secret):
    """Fire one singleton_guard_event with the #606 probe fakes installed."""
    monkeypatch.setattr(singleton, "_process_guard", _guard_with_secret_ref_cr(fake_secret))
    monkeypatch.setattr(singleton, "_operator_core_api", fake_secret)
    events, emit = make_sink()
    monkeypatch.setattr(singleton, "emit_kopf_event", emit)
    get_read_only_redis_client.cache_clear()
    try:
        singleton.singleton_guard_event(
            body=make_cr("alpha", OLD_TS, spec=_SECRET_REF_SPEC_606),
            namespace=NAMESPACE,
            name="alpha",
            logger=log,
            patch={},
            type="ADDED",
        )
    finally:
        get_read_only_redis_client.cache_clear()
    return events


def test_secret_ref_403_emits_forbidden_warning_naming_rbac_remedy(log, monkeypatch):
    """Issue #606 acceptance: a 403 on the secretRef Secret read emits a
    Warning Event attached to the CR that names the RBAC resourceNames
    cause and BOTH remedies — not a generic resolution failure."""
    fake_secret = FakeSecretsCoreV1Api(exc=ApiException(status=403, reason="Forbidden"))
    monkeypatch.setattr(singleton, "_redis_secret_ref_forbidden_warned", set())

    events = _run_guard_event(monkeypatch, log, fake_secret)

    forbidden = [e for e in events if e[2] == "RedisSecretRefForbidden"]
    assert len(forbidden) == 1, (
        f"expected exactly one RedisSecretRefForbidden Warning on a 403 "
        f"secretRef read (issue #606); got {event_triples(events)!r}"
    )
    obj, event_type, _reason, message = forbidden[0]
    assert event_type == "Warning"
    assert obj["metadata"]["name"] == "alpha"
    for token in (
        "openstudio-redis-url",
        "403 Forbidden",
        "resourceNames",
        "rbac.yaml",
        "openstudio-redis",
        "#606",
    ):
        assert token in message, (
            f"the RedisSecretRefForbidden message must name {token!r} so "
            f"the SRE knows either widening step to take (issue #606); "
            f"got: {message!r}"
        )
    # The probe read exactly the referenced Secret, in the CR's namespace.
    assert fake_secret.calls == [("openstudio-redis-url", NAMESPACE)]


def test_secret_ref_403_warning_fires_once_per_cr(log, monkeypatch):
    """The probe is at-most-once per CR per operator restart (the
    ``_redis_secret_ref_forbidden_warned`` cache, mirroring
    ``_redis_url_warned``): a re-fire on every watch event would spam the
    Event stream AND re-read a Secret the Role denies forever."""
    fake_secret = FakeSecretsCoreV1Api(exc=ApiException(status=403, reason="Forbidden"))
    monkeypatch.setattr(singleton, "_redis_secret_ref_forbidden_warned", set())

    events_first = _run_guard_event(monkeypatch, log, fake_secret)
    events_second = _run_guard_event(monkeypatch, log, fake_secret)

    assert len([e for e in events_first if e[2] == "RedisSecretRefForbidden"]) == 1
    assert [e for e in events_second if e[2] == "RedisSecretRefForbidden"] == [], (
        "second guard pass on the same CR must NOT re-fire "
        "RedisSecretRefForbidden (issue #606 once-per-(ns,name) cache in "
        "singleton._redis_secret_ref_forbidden_warned)"
    )
    assert len(fake_secret.calls) == 1, (
        "the known-403 CR must not be re-probed on subsequent watch "
        "events (issue #606)"
    )


def test_secret_ref_missing_secret_no_forbidden_event_and_reprobes(log, monkeypatch):
    """A 404 (missing Secret) is NOT the RBAC case — no event — and the CR
    stays UNMARKED so the next watch event re-probes: missing-Secret is a
    transient condition (the Secret may be created later), unlike the
    static RBAC mismatch."""
    fake_secret = FakeSecretsCoreV1Api(exc=ApiException(status=404, reason="Not Found"))
    monkeypatch.setattr(singleton, "_redis_secret_ref_forbidden_warned", set())

    events_first = _run_guard_event(monkeypatch, log, fake_secret)
    events_second = _run_guard_event(monkeypatch, log, fake_secret)

    for events in (events_first, events_second):
        assert [e for e in events if e[2] == "RedisSecretRefForbidden"] == [], (
            "a 404 is a missing-Secret failure (#567 surfaces), not the "
            "RBAC fence — RedisSecretRefForbidden must stay silent "
            "(issue #606)"
        )
    assert len(fake_secret.calls) == 2, (
        "non-403 resolution failures leave the CR unmarked so the next "
        "watch event re-probes (issue #606)"
    )


def test_startup_wrapper_zero_crs_logs_idle_once(caplog, log, monkeypatch):
    guard = SingletonGuard(ListOnlyFakeCustomObjectsApi(items=[]))
    monkeypatch.setattr(singleton, "_process_guard", guard)
    monkeypatch.setenv("POD_NAMESPACE", NAMESPACE)
    events, emit = make_sink()
    monkeypatch.setattr(singleton, "emit_kopf_event", emit)

    singleton.singleton_guard_startup(logger=log)
    singleton.singleton_guard_startup(logger=log)
    assert events == []
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1


def test_startup_wrapper_without_pod_namespace_skips(monkeypatch):
    guard = SingletonGuard(ListOnlyFakeCustomObjectsApi(items=[]))
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
