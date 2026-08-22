"""Direct tests for the shared ``_k8s`` helper surfaces (issue #483).

``rolling_restart_deployment`` (the #395 shared helper serving BOTH the worker
recycler and the web_background stall monitor) and ``load_operator_kube_config``
(the #305 single public kubeconfig loader) were previously exercised only
indirectly — via the handler tick tests in ``test_worker_recycler.py`` /
``test_web_background_monitor.py`` and the factory tests in
``test_k8s_clients.py``. A regression in the shared helper would break both
handlers at once, so this module pins its contracts directly:

* the merge-patch body shape (RFC 7386 content type, ``restartedAt`` annotation
  keyed ``kubectl.kubernetes.io/restartedAt``, sibling annotations/labels on the
  server-side Deployment survive — merge-patch adds, never replaces),
* the returned timestamp is tz-aware UTC and round-trips through
  ``openstudio_operator._time.parse_utc`` (the repo boundary rule),
* ``ApiException`` from the patch call (500 server error, 409 conflict)
  propagates unhandled — the helper neither retries nor swallows; the D12
  skip-tick-and-retry-next-poll posture lives at the handler call sites,
* the loader's incluster-first / kubeconfig-fallback order, and that the
  SECOND exception propagates when both sources fail (fail closed).

The shared ``FakeAppsV1Api`` from ``tests/_fakes.py`` (#531) applies the RFC
7386 merge-patch mirror (``_merge_patch``) onto an in-memory Deployment, so
annotation-preservation is asserted against real merge semantics, not just the
outgoing body dict.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from kubernetes.client import ApiException
from kubernetes.config import ConfigException

from _fakes import FakeAppsV1Api
from openstudio_operator._k8s import (
    RESTARTED_AT_ANNOTATION,
    load_operator_kube_config,
    rolling_restart_deployment,
)
from openstudio_operator._time import parse_iso_utc
from openstudio_operator.status_store import MERGE_PATCH_CONTENT_TYPE

NAMESPACE = "openstudio-server"
DEPLOYMENT = "worker"


def make_deployment_obj() -> dict:
    """Server-side Deployment with sibling pod-template annotations + labels.

    The sibling annotation/label keys are what the merge-patch must NOT
    touch — an annotations-replacing patch (e.g. json-patch semantics or a
    whole-metadata replace) would drop them.
    """
    return {
        "metadata": {"name": DEPLOYMENT, "namespace": NAMESPACE},
        "spec": {
            "selector": {"matchLabels": {"app": "worker"}},
            "template": {
                "metadata": {
                    "annotations": {
                        "prometheus.io/scrape": "true",
                        "nrel.openstudio.org/owner": "helm-chart",
                    },
                    "labels": {"app": "worker", "tier": "simulations"},
                }
            },
        },
    }


def test_rolling_restart_merge_patch_body_and_sibling_preservation() -> None:
    """Patch carries ONLY the restartedAt annotation under merge-patch content type.

    The outgoing body's annotations dict contains exactly one key — the
    kubectl-standard ``kubectl.kubernetes.io/restartedAt`` — and the request
    is a merge-patch (RFC 7386). Applying it to a Deployment that already
    carries sibling pod-template annotations and labels leaves every sibling
    untouched: merge-patch adds the restart annotation, never replaces the
    surrounding metadata. An annotations-replacing regression here would wipe
    chart-managed annotations on BOTH the worker and web_background restart
    paths (#395 shared helper).
    """
    fake = FakeAppsV1Api(make_deployment_obj())
    now = datetime(2026, 8, 22, 12, 30, 45, tzinfo=UTC)

    returned = rolling_restart_deployment(fake, deployment=DEPLOYMENT, namespace=NAMESPACE, now=now)

    assert len(fake.patches) == 1
    patch = fake.patches[0]
    assert patch["name"] == DEPLOYMENT
    assert patch["namespace"] == NAMESPACE
    assert patch["kwargs"]["_content_type"] == MERGE_PATCH_CONTENT_TYPE
    assert patch["kwargs"]["_content_type"] == "application/merge-patch+json"

    annotations = patch["body"]["spec"]["template"]["metadata"]["annotations"]
    assert list(annotations) == [RESTARTED_AT_ANNOTATION]
    assert annotations[RESTARTED_AT_ANNOTATION] == returned
    assert annotations[RESTARTED_AT_ANNOTATION] == "2026-08-22T12:30:45+00:00"

    served = fake.obj["spec"]["template"]["metadata"]
    assert served["annotations"][RESTARTED_AT_ANNOTATION] == returned
    assert served["annotations"]["prometheus.io/scrape"] == "true"
    assert served["annotations"]["nrel.openstudio.org/owner"] == "helm-chart"
    assert served["labels"] == {"app": "worker", "tier": "simulations"}


def test_rolling_restart_timestamp_is_tz_aware_utc_and_parseable() -> None:
    """The annotation value round-trips ``parse_utc`` as tz-aware UTC regardless of input tz.

    The helper normalises ``now`` via ``astimezone(UTC)`` — a caller passing a
    naive-unfriendly local-zone datetime still gets a ``+00:00``-suffixed ISO
    stamp. Pins the repo rule that every timestamp at a boundary is tz-aware
    UTC (a naive value here would make the annotation string ambiguous and
    break the next restart's elapsed-time comparison).
    """
    fake = FakeAppsV1Api(make_deployment_obj())
    local_tz = timezone(timedelta(hours=-7))
    now = datetime(2026, 8, 22, 5, 30, 45, tzinfo=local_tz)

    returned = rolling_restart_deployment(fake, deployment=DEPLOYMENT, namespace=NAMESPACE, now=now)

    assert returned.endswith("+00:00")
    parsed = parse_iso_utc(returned)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == datetime(2026, 8, 22, 12, 30, 45, tzinfo=UTC)


def test_rolling_restart_patch_api_exception_500_propagates() -> None:
    """A 500 ApiException from the patch call propagates — no retry, no swallow.

    The helper calls ``patch_namespaced_deployment`` exactly once and lets any
    ``ApiException`` raise: the D12 skip-tick-and-retry-next-poll posture is
    caller-side (the handlers' ``run_oscm_tick`` wrapper catches it and bumps
    ``HANDLER_TICK_FAILURES_TOTAL``). Retrying inside the helper would stack a
    second in-flight restart on top of the handlers' own next-poll retry.
    """
    fake = FakeAppsV1Api(make_deployment_obj())
    fake.fail_with = ApiException(status=500, reason="Internal Server Error")

    with pytest.raises(ApiException) as excinfo:
        rolling_restart_deployment(
            fake, deployment=DEPLOYMENT, namespace=NAMESPACE, now=datetime.now(UTC)
        )

    assert excinfo.value.status == 500
    assert len(fake.patches) == 1, "helper must not retry the patch on its own"


def test_rolling_restart_patch_api_exception_409_propagates() -> None:
    """A 409 conflict from the patch also propagates unhandled.

    Unlike ``StatusStore``'s CR status RMW (which retries 409s internally),
    the rolling restart has no read-modify-write window to lose — a 409 here
    means the API server rejected the patch, and the helper pins that
    decision to the caller rather than guessing a retry policy.
    """
    fake = FakeAppsV1Api(make_deployment_obj())
    fake.fail_with = ApiException(status=409, reason="Conflict")

    with pytest.raises(ApiException) as excinfo:
        rolling_restart_deployment(
            fake, deployment=DEPLOYMENT, namespace=NAMESPACE, now=datetime.now(UTC)
        )

    assert excinfo.value.status == 409
    assert len(fake.patches) == 1, "helper must not retry the patch on its own"
    assert RESTARTED_AT_ANNOTATION not in fake.obj["spec"]["template"]["metadata"]["annotations"]


def test_rolling_restart_observes_kube_api_duration_histogram() -> None:
    """Issue #488 acceptance: the rolling-restart patch observes the kube-api
    request-duration histogram under ``verb="patch"`` — the Deployment patch
    is one of the kube chokepoints whose latency was previously invisible
    (only the #119 status-store 409 counters covered any kube path)."""
    from openstudio_operator import metrics

    fake = FakeAppsV1Api(make_deployment_obj())
    histogram = metrics.KUBE_API_REQUEST_DURATION_SECONDS
    child = histogram.labels(verb="patch")
    before = float(next(s.value for s in child._child_samples() if s.name == "_count"))

    rolling_restart_deployment(
        fake, deployment=DEPLOYMENT, namespace=NAMESPACE, now=datetime.now(UTC)
    )

    after = float(next(s.value for s in child._child_samples() if s.name == "_count"))
    assert after - before >= 1


def test_loader_falls_back_to_kubeconfig_on_config_exception() -> None:
    """Incluster ``ConfigException`` → ``load_kube_config`` runs, in that order.

    The bare-``kopf run`` dev-session shape: no service-account env vars, so
    the in-cluster loader raises and the ``~/.kube/config`` fallback takes
    over. Pins the incluster-FIRST order (#405 single loader; the local
    import inside the function rebinds the patched module attribute on every
    call, so the manual assignment patching style of ``test_k8s_clients.py``
    is honored).
    """
    import kubernetes.config as kube_config

    calls: list[str] = []

    def fake_incluster() -> None:
        calls.append("load_incluster_config")
        raise ConfigException("no in-cluster service-account env vars")

    def fake_kube() -> None:
        calls.append("load_kube_config")

    original_inc = kube_config.load_incluster_config
    original_kube = kube_config.load_kube_config
    kube_config.load_incluster_config = fake_incluster  # type: ignore[assignment]
    kube_config.load_kube_config = fake_kube  # type: ignore[assignment]
    try:
        load_operator_kube_config()
    finally:
        kube_config.load_incluster_config = original_inc  # type: ignore[assignment]
        kube_config.load_kube_config = original_kube  # type: ignore[assignment]

    assert calls == ["load_incluster_config", "load_kube_config"]


def test_loader_skips_fallback_when_incluster_succeeds() -> None:
    """A successful in-cluster load never touches ``load_kube_config``."""
    import kubernetes.config as kube_config

    calls: list[str] = []

    def fake_incluster() -> None:
        calls.append("load_incluster_config")

    def fake_kube() -> None:
        calls.append("load_kube_config")

    original_inc = kube_config.load_incluster_config
    original_kube = kube_config.load_kube_config
    kube_config.load_incluster_config = fake_incluster  # type: ignore[assignment]
    kube_config.load_kube_config = fake_kube  # type: ignore[assignment]
    try:
        load_operator_kube_config()
    finally:
        kube_config.load_incluster_config = original_inc  # type: ignore[assignment]
        kube_config.load_kube_config = original_kube  # type: ignore[assignment]

    assert calls == ["load_incluster_config"]


def test_loader_propagates_second_exception_when_both_fail() -> None:
    """Both loaders fail → the kubeconfig-fallback exception propagates (fail closed).

    Callers (the ``operator_*_api`` factories, ``prune_entrypoint.main``) get
    the SECOND exception, matching the docstring's fail-closed contract: the
    operator skips the tick and retries on the next poll per D12 rather than
    running against a half-initialised configuration.
    """
    import kubernetes.config as kube_config

    def fake_incluster() -> None:
        raise ConfigException("incluster failed")

    def fake_kube() -> None:
        raise ConfigException("kubeconfig file not found")

    original_inc = kube_config.load_incluster_config
    original_kube = kube_config.load_kube_config
    kube_config.load_incluster_config = fake_incluster  # type: ignore[assignment]
    kube_config.load_kube_config = fake_kube  # type: ignore[assignment]
    try:
        with pytest.raises(ConfigException) as excinfo:
            load_operator_kube_config()
    finally:
        kube_config.load_incluster_config = original_inc  # type: ignore[assignment]
        kube_config.load_kube_config = original_kube  # type: ignore[assignment]

    assert "kubeconfig" in str(excinfo.value)


def test_loader_non_config_exception_does_not_fall_back() -> None:
    """Only ``ConfigException`` triggers the fallback; anything else raises straight out.

    A non-``ConfigException`` from the in-cluster loader (e.g. a corrupt
    token file surfacing as another error type) must not be masked by a
    silent kubeconfig fallback — that would hide a broken service-account
    mount behind a dev-config success.
    """
    import kubernetes.config as kube_config

    calls: list[str] = []

    def fake_incluster() -> None:
        calls.append("load_incluster_config")
        raise RuntimeError("corrupt service-account token")

    def fake_kube() -> None:
        calls.append("load_kube_config")

    original_inc = kube_config.load_incluster_config
    original_kube = kube_config.load_kube_config
    kube_config.load_incluster_config = fake_incluster  # type: ignore[assignment]
    kube_config.load_kube_config = fake_kube  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="corrupt service-account token"):
            load_operator_kube_config()
    finally:
        kube_config.load_incluster_config = original_inc  # type: ignore[assignment]
        kube_config.load_kube_config = original_kube  # type: ignore[assignment]

    assert calls == ["load_incluster_config"]
