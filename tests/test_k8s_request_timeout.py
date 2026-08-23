"""Tests for the bounded Kubernetes API request timeout (issue #579).

The four ``operator_*_api`` client construction paths (status RMW
patches, singleton-guard CR lists, Deployment reads/patches, the #463
Secret read — plus the prune CronJob's Jobs/Events via the same
factories) historically set NO request timeout. kubernetes-python
(``>=29.3,<37``) has no ``Configuration.timeout`` wiring — the generated
``*V1Api`` methods honor only a per-call ``_request_timeout`` and
``rest.py`` defaults the urllib3 timeout to ``None`` — so a black-holed
apiserver connection (kube-proxy stall, NAT idle drop without RST)
blocked the calling timer tick forever while the pod stayed ``Running``
and /metrics kept answering liveness.

Issue #579 fixes it at the single shared construction path:
:func:`openstudio_operator.singleton._cached_k8s_api` runs every built
client through :func:`openstudio_operator._k8s.apply_request_timeout`,
which installs a :class:`openstudio_operator._k8s.BoundedK8sRequest`
wrapper as the client's ``rest_client.request``. This module pins:

* **Application** — every client built through the four public
  ``operator_*_api`` factories carries the wrapper with the
  ``K8S_REQUEST_TIMEOUT_SECONDS`` default (the public-path version of
  the issue's "assert the configuration is actually applied to the
  constructed clients"), and re-application is idempotent.
* **Propagation** — an omitted ``_request_timeout`` is injected with the
  constant and, through the REAL ``rest.RESTClientObject``, reaches
  urllib3 as ``Timeout(total=15)`` (the unit-level "read timeout would
  fire" proof); an explicit per-call value passes through untouched.
* **D12 posture** — the urllib3 timeout error the bound produces is
  translated into an in-``SKIP_TICK_EXCEPTIONS`` ``ApiException``
  (non-timeout transport errors stay untranslated), and the counted
  skip path of ``run_oscm_tick`` absorbs it: a hung apiserver call is a
  counted skip-and-retry-next-poll, never a wedged tick.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import kubernetes.client
import kubernetes.config as kube_config
import pytest
import urllib3
import urllib3.exceptions
from kubernetes.client import ApiException
from prometheus_client import REGISTRY

from openstudio_operator import singleton
from openstudio_operator._constants import K8S_REQUEST_TIMEOUT_SECONDS
from openstudio_operator._k8s import BoundedK8sRequest, apply_request_timeout
from openstudio_operator._oscm_handlers import run_oscm_tick

NAMESPACE = "openstudio-server"
NAME = "oscm"

#: Mirrors ``tests/test_timer_wrapper_failures.py``'s SPEC shape: a
#: non-empty ``serverUrl`` (so ``run_oscm_tick`` does not idle-return)
#: and ``dryRun=True`` (so the EventEmitter is a no-op without a kopf
#: handler context).
SPEC = {
    "serverUrl": "http://web.test",
    "redisUrl": "redis://:pw@queue.test:6379",
    "dryRun": True,
}

#: The four public factories issue #579 covers — the same set the
#: ``_V1_API_FACTORIES`` AST gate in ``tests/test_singleton_registry_coverage.py``
#: pins to ``singleton.py`` as the sole construction site.
ALL_FACTORY_NAMES = (
    "operator_custom_objects_api",
    "operator_apps_api",
    "operator_batch_api",
    "operator_core_api",
)


@pytest.fixture
def _seeded_k8s_config(monkeypatch: pytest.MonkeyPatch):
    """Patch both kubeconfig loaders to seed a default Configuration.

    Mirrors the isolation contract of ``tests/test_k8s_clients.py``: CI
    lacks both in-cluster service-account env vars and ``~/.kube/config``,
    so both loaders are stubbed to install a default
    ``kubernetes.client.Configuration`` with a sentinel host — enough for
    the factories to construct REAL clients (the only way to observe the
    wrapper through the public path). Also snapshots/restores
    ``Configuration._default`` so the sentinel host cannot leak into
    sibling tests. The conftest autouse fixture already resets the
    factory caches before/after each test (issues #158 + #251).
    """
    prev_default = kubernetes.client.Configuration._default

    def fake_load_incluster() -> None:
        cfg = kubernetes.client.Configuration()
        cfg.host = "https://apiserver.incluster.example:6443"
        kubernetes.client.Configuration.set_default(cfg)

    monkeypatch.setattr(kube_config, "load_incluster_config", fake_load_incluster)
    monkeypatch.setattr(kube_config, "load_kube_config", fake_load_incluster)
    yield
    kubernetes.client.Configuration._default = prev_default


class _RecordingOriginal:
    """Fake ``rest.RESTClientObject.request`` capturing kwargs, returning a sentinel."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.exc = exc

    def __call__(self, method: str, url: str, **kwargs: object) -> object:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(status=200)


# --- application through the public factory path (issue #579a) -----------------


@pytest.mark.parametrize("factory_name", ALL_FACTORY_NAMES)
def test_every_factory_installs_bounded_request_wrapper(
    _seeded_k8s_config: None, factory_name: str
) -> None:
    """Each public ``operator_*_api`` factory returns a wrapped client.

    The issue-#579 acceptance criterion: the bounded request timeout is
    actually applied to the constructed clients. Asserted through the
    PUBLIC path — the factory with only the kubeconfig loaders stubbed —
    so a regression that stops calling ``apply_request_timeout`` at the
    shared ``_cached_k8s_api`` site (or applies it to only some client
    types) fails here, per factory.
    """
    client = getattr(singleton, factory_name)()

    request = client.api_client.rest_client.request
    assert isinstance(request, BoundedK8sRequest), (
        f"{factory_name}() must install a BoundedK8sRequest as the "
        f"client's rest_client.request (issue #579); got {type(request).__name__}. "
        f"See openstudio_operator.singleton._cached_k8s_api."
    )
    assert request.default_timeout_seconds == K8S_REQUEST_TIMEOUT_SECONDS, (
        f"The wrapper's default must be K8S_REQUEST_TIMEOUT_SECONDS "
        f"({K8S_REQUEST_TIMEOUT_SECONDS}s); got {request.default_timeout_seconds}s. "
        f"See openstudio_operator._constants.K8S_REQUEST_TIMEOUT_SECONDS."
    )


def test_apply_request_timeout_is_idempotent(_seeded_k8s_config: None) -> None:
    """A second ``apply_request_timeout`` call must not double-wrap.

    The factories cache for the process lifetime, but the D05 guard's
    rebuild path (``singleton._get_guard``) resets the caches — a
    re-built client could in principle pass through the wrapper install
    twice. A double wrap would be harmless-but-wasteful at best and a
    misleading stack frame at worst; the idempotence guard keeps the
    installed ``request`` attribute stable.
    """
    client = singleton.operator_core_api()
    first = client.api_client.rest_client.request
    assert isinstance(first, BoundedK8sRequest)

    returned = apply_request_timeout(client)

    assert returned is client
    assert client.api_client.rest_client.request is first


# --- propagation: default injection + per-call passthrough ----------------------


def test_bounded_request_injects_default_timeout_when_caller_omits_it() -> None:
    """An omitted ``_request_timeout`` becomes ``K8S_REQUEST_TIMEOUT_SECONDS``.

    This is the load-bearing default: every generated ``*V1Api`` method
    passes ``_request_timeout=local_var_params.get('_request_timeout')``
    — ``None`` unless the caller set it — so pre-#579 ``rest.py``
    converted it to ``timeout=None`` (urllib3: wait forever). The wrapper
    intercepts exactly that ``None`` and substitutes the bound.
    """
    original = _RecordingOriginal()
    bounded = BoundedK8sRequest(original)

    result = bounded("GET", "/api/v1/namespaces/openstudio-server/pods")

    assert result.status == 200
    assert original.calls[0]["_request_timeout"] == K8S_REQUEST_TIMEOUT_SECONDS, (
        "The wrapper must inject K8S_REQUEST_TIMEOUT_SECONDS when the "
        "caller omits _request_timeout (issue #579); got "
        f"{original.calls[0].get('_request_timeout')!r}."
    )


@pytest.mark.parametrize(
    "explicit",
    (
        pytest.param(5.0, id="scalar"),
        pytest.param((3.0, 7.0), id="connect-read-tuple"),
    ),
)
def test_bounded_request_preserves_explicit_per_call_timeout(explicit: object) -> None:
    """An explicit per-call ``_request_timeout`` wins over the default.

    urllib3's tuple form ``(connect, read)`` is the reason the wrapper
    must pass explicit values through verbatim: a call site that wants a
    longer connect timeout on a slow first TLS handshake, or a tighter
    read timeout on a chatty list, keeps that control post-#579.
    """
    original = _RecordingOriginal()
    bounded = BoundedK8sRequest(original)

    bounded("GET", "/api/v1/namespaces/openstudio-server/pods", _request_timeout=explicit)

    assert original.calls[0]["_request_timeout"] == explicit


def test_injected_default_reaches_urllib3_as_total_timeout(
    _seeded_k8s_config: None,
) -> None:
    """The injected bound reaches urllib3 as ``Timeout(total=15)`` on a REAL client.

    The unit-level "urllib3's read timeout would fire" proof: a client
    built through the public factory, its pool manager stubbed at the
    urllib3 boundary, still runs the REAL ``rest.RESTClientObject.request``
    under the installed wrapper — so what the pool receives is exactly
    what urllib3 would enforce: ``urllib3.Timeout(total=15)``. This pins
    the full chain wrapper → ``rest.py`` → urllib3 without opening a
    socket.
    """
    client = singleton.operator_core_api()
    rest_client = client.api_client.rest_client
    captured: dict[str, Any] = {}

    def fake_pool_request(method: str, url: str, **kwargs: object) -> object:
        captured["method"] = method
        captured["url"] = url
        captured["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(status=200)

    rest_client.pool_manager.request = fake_pool_request  # type: ignore[method-assign]

    response = rest_client.request(
        "GET",
        "https://apiserver.incluster.example:6443/api/v1/namespaces/openstudio-server/pods",
        _preload_content=False,
    )

    assert response.status == 200
    timeout = captured["timeout"]
    assert isinstance(timeout, urllib3.Timeout), (
        f"rest.py must hand urllib3 a Timeout object; got {type(timeout).__name__}."
    )
    assert timeout.total == K8S_REQUEST_TIMEOUT_SECONDS, (
        "The wrapper-injected default must reach urllib3 as "
        f"Timeout(total={K8S_REQUEST_TIMEOUT_SECONDS}); got {timeout.total!r}. "
        "Without this, a black-holed apiserver connection waits forever (issue #579)."
    )


# --- translation: urllib3 timeout -> in-skip-tuple ApiException -----------------


def test_urllib3_read_timeout_translates_to_api_exception() -> None:
    """A read timeout past the bound raises ``ApiException(status=0)``.

    ``ApiException`` is a ``SKIP_TICK_EXCEPTIONS`` member; ``status=0``
    matches kubernetes' own "no HTTP response" convention (``rest.py``
    raises ``ApiException(status=0)`` for SSL failures). The original
    exception is chained (``__cause__``) so the stack trace keeps the
    urllib3 evidence for triage.
    """
    hang = urllib3.exceptions.ReadTimeoutError("pool", "/api/v1/pods", "read timed out")
    bounded = BoundedK8sRequest(_RecordingOriginal(exc=hang))

    with pytest.raises(ApiException) as excinfo:
        bounded("GET", "/api/v1/namespaces/openstudio-server/pods")

    assert excinfo.value.status == 0
    assert "timed out" in (excinfo.value.reason or "")
    assert "issue #579" in (excinfo.value.reason or "")
    assert excinfo.value.__cause__ is hang


def test_max_retry_error_with_timeout_reason_translates_to_api_exception() -> None:
    """A ``MaxRetryError`` wrapping a timeout translates too; others do not.

    When the connection pool applies retries, urllib3 surfaces the read
    timeout as ``MaxRetryError`` with the timeout as ``.reason`` — that
    is a hang past the bound and must reach the same counted skip. A
    ``MaxRetryError`` wrapping a NON-timeout reason (connection reset)
    is not a hang and stays untranslated (fail-closed posture).
    """
    hang = urllib3.exceptions.ReadTimeoutError("pool", "/api/v1/pods", "read timed out")
    timeout_retry = urllib3.exceptions.MaxRetryError("pool", "/api/v1/pods", reason=hang)
    bounded = BoundedK8sRequest(_RecordingOriginal(exc=timeout_retry))

    with pytest.raises(ApiException) as excinfo:
        bounded("GET", "/api/v1/namespaces/openstudio-server/pods")

    assert excinfo.value.status == 0
    assert excinfo.value.__cause__ is timeout_retry


def test_non_timeout_transport_error_passes_through_untranslated() -> None:
    """Non-timeout transport errors keep their type through the wrapper.

    The wrapper widens the D12 catch set for HANGS only. A connection
    reset is an immediate failure the operator's existing fail-closed
    posture already governs — translating it would silently widen the
    skip-tick surface beyond what issue #579 accepted.
    """
    reset = urllib3.exceptions.ProtocolError("connection reset by peer")
    bounded = BoundedK8sRequest(_RecordingOriginal(exc=reset))

    with pytest.raises(urllib3.exceptions.ProtocolError) as excinfo:
        bounded("GET", "/api/v1/namespaces/openstudio-server/pods")

    assert excinfo.value is reset

    reset_retry = urllib3.exceptions.MaxRetryError(
        "pool", "/api/v1/pods", reason=urllib3.exceptions.ProtocolError("reset")
    )
    bounded_retry = BoundedK8sRequest(_RecordingOriginal(exc=reset_retry))

    with pytest.raises(urllib3.exceptions.MaxRetryError) as retryinfo:
        bounded_retry("GET", "/api/v1/namespaces/openstudio-server/pods")

    assert retryinfo.value is reset_retry


# --- D12 posture: the translated timeout lands in the counted skip path ---------


def _tick_failures(module: str, error_type: str) -> float:
    """Read the ``HANDLER_TICK_FAILURES_TOTAL`` labelled sample for a case."""
    return (
        REGISTRY.get_sample_value(
            "openstudio_operator_handler_tick_failures_total",
            {
                "namespace": NAMESPACE,
                "name": NAME,
                "module": module,
                "error_type": error_type,
            },
        )
        or 0.0
    )


def test_translated_timeout_lands_in_counted_skip_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #579 acceptance: a hung apiserver call becomes a COUNTED skip-tick.

    Simulates the black-holed connection end-to-end at the unit level:
    the tick's apiserver call goes through a production
    ``BoundedK8sRequest`` whose underlying request raises
    ``ReadTimeoutError`` past the bound (what urllib3 does when
    ``Timeout(total=15)`` fires). The wrapper's ``ApiException``
    translation must then be absorbed by ``run_oscm_tick``'s
    ``SKIP_TICK_EXCEPTIONS`` catch: return ``None`` (skip, retry next
    poll per D12), increment ``HANDLER_TICK_FAILURES_TOTAL`` exactly
    once with ``error_type="ApiException"``, and log the single skip
    warning — never propagate as an uncaught kopf handler error, never
    wedge the tick forever.
    """
    hang = urllib3.exceptions.ReadTimeoutError(
        "pool", "/apis/energy.nrel.gov/v1alpha1/namespaces/openstudio-server/openstudioclustermanagers",
        "read timed out",
    )
    bounded = BoundedK8sRequest(_RecordingOriginal(exc=hang))

    def tick(**_: object) -> object:
        # The simulated apiserver call: the wrapper's translation must
        # raise here, inside run_oscm_tick's guarded region.
        bounded(
            "GET",
            "/apis/energy.nrel.gov/v1alpha1/namespaces/openstudio-server/"
            "openstudioclustermanagers",
        )
        raise AssertionError("unreachable — the bounded call must raise first")

    before = _tick_failures("analysis_sla", "ApiException")
    with caplog.at_level(logging.WARNING):
        result = run_oscm_tick(
            spec=SPEC,
            body={"metadata": {"name": NAME, "namespace": NAMESPACE, "uid": "uid-579"}},
            namespace=NAMESPACE,
            name=NAME,
            logger=logging.getLogger("test"),
            module="analysis_sla",
            tick_label="analysis SLA",
            idle_label="analysis SLA monitor",
            custom_objects_api=lambda: object(),
            wire=lambda _config: None,
            tick=tick,
        )
    after = _tick_failures("analysis_sla", "ApiException")

    assert result is None, (
        "run_oscm_tick must swallow the timeout-induced ApiException and "
        f"return None (D12: skip the tick, retry next poll); got {result!r}"
    )
    assert after - before == 1.0, (
        "HANDLER_TICK_FAILURES_TOTAL{error_type='ApiException'} must increment "
        f"by exactly 1 on the timeout-induced skip; observed delta {after - before}. "
        "See issue #579 — the hang must be a COUNTED skip, not a wedged tick."
    )
    assert "analysis SLA tick skipped, retrying next poll (ApiException" in caplog.text, (
        f"The skip-tick warning must name the translated error type; got {caplog.text!r}"
    )
