"""Shared Kubernetes API helpers — generic surface outside any single handler's domain (issue #236).

The operator ships several handler modules under :mod:`openstudio_operator.handlers`
(``analysis_sla``, ``web_background_monitor``, ``datapoint_watchdog``,
``worker_recycler``). Cross-module dependencies between them were historically a
source of fragility:

* reordering the import block or removing one file would silently break the
  other (``from openstudio_operator.handlers.X import Y`` in module
  ``Z``),
* a third handler that wanted the same Kubernetes helper had no neutral home —
  only copy-paste, a sibling-module deep import, or a refactor-first offer,
* bug fixes for shared helpers (e.g. the issue #44 ``matchExpressions`` gap)
  had to land in two places to keep behavior consistent.

This module is the neutral home for the Kubernetes object-surface helpers that
none of the four handler modules owns. Today it hosts:

* :class:`DeploymentReader` — the structural ``AppsV1Api`` slice the operator
  needs (read-only lookup), so handlers can type-hint the same Protocol and
  tests can fake exactly one shape,
* :class:`PodLister` — the structural ``CoreV1Api`` slice for read-only pod
  listing, shared by the web_background stall monitor (leg-C liveness
  corroboration) and — via the ``WorkerPodApi`` extension in
  ``handlers/analysis_sla.py`` — the SLA escalation path (issue #505),
* :class:`DeploymentManager` — :class:`DeploymentReader` extended with
  ``patch_namespaced_deployment`` for the rolling-restart/recycle paths,
  imported by both ``worker_recycler`` and ``web_background_monitor``
  (issue #505),
* :func:`deployment_label_selector` — build a Kubernetes ``label_selector=``
  query string from a Deployment's own ``spec.selector`` (honors BOTH
  ``matchLabels`` AND ``matchExpressions``; the narrow-fallback-on-unsupported
  operator behavior established by issue #44 was preserved verbatim — see the
  function's docstring for the rationale),
* :func:`rolling_restart_deployment` — perform a rolling restart of a
  Deployment by patching the pod template's ``restartedAt`` annotation,
  plus the single-site constants :data:`RESTARTED_AT_ANNOTATION`,
  :data:`DEFAULT_WORKER_DEPLOYMENT`, and :data:`MERGE_PATCH_CONTENT_TYPE`
  the worker recycler, web_background stall monitor, and status store
  all import (issues #395 and #585).
* :func:`load_operator_kube_config` — the SINGLE public loader for the
  operator's kubeconfig (in-cluster first, ``kube_config`` fallback). The
  ``singleton.operator_*_api`` factories and
  :mod:`openstudio_operator.prune_entrypoint` call it directly (issue
  #405 removed the former ``_load_k8s_config`` / ``_load_kube_config``
  thin wrappers). A future
  loader change (kubeconfig Secret reference, network-proxy client, custom
  CA bundle) applies at this single site; the AST gate in
  ``tests/test_singleton_registry_coverage.py::test_only_one_kubeconfig_loader_call_site``
  rejects any inline ``load_incluster_config(`` / ``load_kube_config(`` call
  outside this module so a partial update fails CI loudly (issue #305).
* :class:`BoundedK8sRequest` + :func:`apply_request_timeout` — the
  issue #579 bounded-request wrapper installed on every client built
  through the shared ``singleton._cached_k8s_api`` factory path: defaults
  each request's ``_request_timeout`` to
  :data:`openstudio_operator._constants.K8S_REQUEST_TIMEOUT_SECONDS` and
  translates the resulting urllib3 timeout error into an
  in-``SKIP_TICK_EXCEPTIONS`` ``ApiException`` (the D12 counted skip).
  See the class docstring for why ``Configuration.timeout`` CANNOT be
  the mechanism on kubernetes-python 29.x–36.x.

Why ``_k8s`` and not ``k8s_helpers`` / ``k8s_api_helpers``? Mirrors the
``_constants`` / ``_time`` convention in this package (operator-internal
utilities prefixed with ``_`` and kept out of the public surface). The leading
underscore is the convention the AGENTS.md module table uses to mark
shared-internal modules.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

import urllib3.exceptions
from kubernetes.client import ApiException

from openstudio_operator._constants import K8S_REQUEST_TIMEOUT_SECONDS
from openstudio_operator.metrics import KUBE_API_REQUEST_DURATION_SECONDS, observe_duration

logger = logging.getLogger(__name__)


class DeploymentReader(Protocol):
    """Structural type of ``AppsV1Api`` for read-only Deployment lookup.

    Tests fake exactly this — passing any object with a
    ``read_namespaced_deployment(name, namespace)`` callable satisfies the
    Protocol. The wider ``AppsV1Api`` shape (deployments, stateful sets, etc.)
    is intentionally NOT captured here: handlers that need additional methods
    should declare their own Protocol that EXTENDS a ``_k8s`` Protocol and
    adds only the genuinely handler-specific surface, rather than
    type-hinting against the full ``AppsV1Api`` and forcing every test fake
    to reproduce the same broad interface. Since #505 the shared slices live
    here (``DeploymentReader``/``DeploymentManager``/``PodLister``); the
    handler-local exemption is reserved for genuine extensions only — e.g.
    ``analysis_sla.WorkerPodApi`` extending :class:`PodLister` with the
    eviction-path ``delete_namespaced_pod``.

    Issue #236 surfaced a second Protocol (``WorkerDeploymentApi`` in
    :mod:`openstudio_operator.handlers.web_background_monitor`) that wraps
    ``DeploymentReader`` with a ``patch_namespaced_deployment`` method for the
    rolling-restart path. Issue #505 moved that superset here as
    :class:`DeploymentManager` (its worker_recycler subset
    ``DeploymentPatcher`` was folded into the same import) —
    :func:`deployment_label_selector` still needs only the read-only slice.
    """

    def read_namespaced_deployment(self, name: str, namespace: str, **_: object) -> object: ...


class DeploymentManager(DeploymentReader, Protocol):
    """Structural type of ``AppsV1Api`` for Deployment read + patch (issue #505).

    ``worker_recycler``'s ``DeploymentPatcher`` (patch-only) and
    ``web_background_monitor``'s ``WorkerDeploymentApi`` (read + patch) were
    two handler-local views of the same method pair — a future signature
    change would have had to be mirrored in both or the two handlers' views
    of ``AppsV1Api`` would silently diverge. The superset now lives here;
    both handlers import it. The recycle path only exercises the patch
    method, but Protocols are structural: consumers may use a subset of the
    declared surface, and the real ``AppsV1Api`` satisfies the whole
    Protocol either way.

    Tests fake exactly the methods they exercise — a fake with only
    ``patch_namespaced_deployment`` satisfies every ``worker_recycler`` call
    site; ``web_background_monitor`` call sites additionally read the
    Deployment through :func:`deployment_label_selector`.
    """

    def patch_namespaced_deployment(
        self, name: str, namespace: str, body: dict, **_: object
    ) -> object: ...


class PodLister(Protocol):
    """Structural type of ``CoreV1Api`` for read-only pod listing (issue #505).

    ``analysis_sla``'s ``WorkerPodApi`` and ``web_background_monitor``'s
    handler-local ``PodLister`` both declared ``list_namespaced_pod`` with
    identical signatures — two structural types for one K8s method meant
    test fakes typed against one did not document compatibility with the
    other. The shared slice now lives here; ``web_background_monitor``
    imports it directly, and ``analysis_sla``'s ``WorkerPodApi`` extends it
    with the eviction-path ``delete_namespaced_pod`` (a genuine surface
    extension — that module is the only pod-deleting consumer).

    Tests fake exactly this — passing any object with a
    ``list_namespaced_pod(namespace)`` callable satisfies the Protocol.
    """

    def list_namespaced_pod(self, namespace: str, **_: object) -> object: ...


def deployment_label_selector(
    apps_api: DeploymentReader, deployment: str, namespace: str
) -> str | None:
    """Build a Kubernetes label-selector string from a Deployment's own ``spec.selector``.

    Honors BOTH ``spec.selector.matchLabels`` AND ``spec.selector.matchExpressions``
    — issue #44 gap fix. A ``matchExpressions``-only selector previously
    silently fell back to an empty selector (the helper read ``match_labels``
    only), which the label-selector API treats as "list every pod in the
    namespace" — under the web_background_monitor's "worker fleet looks
    fine" check (#13 leg C) that would falsely TRIP the deployment-wide
    pod set, and under the SLA escalation (#9) it would broaden the IP
    matching to non-worker pods.

    Intersection semantics: when both ``matchLabels`` and
    ``matchExpressions`` are set on the Deployment, the Kubernetes label
    selector grammar requires the intersection (AND), which is what the
    comma-separated ``label_selector=`` argument implements — every term
    must match.

    Supported ``matchExpressions`` operators: ``In``, ``NotIn``, ``Exists``,
    ``DoesNotExist``. Anything exotic (e.g. ``Gt``, ``Lt`` — non-string
    operators the label selector grammar does not cover) logs a WARNING
    and falls back to ``matchLabels`` only — the Deployment is then
    discovered with a deliberately narrower selector, which is the
    conservative direction (the worst case is a missed-eviction, not a
    false-eviction). Returns ``None`` only when neither is set.
    """
    # Issue #488 — time the apiserver Deployment read; the selector
    # building below is pure computation.
    with observe_duration(KUBE_API_REQUEST_DURATION_SECONDS, verb="get"):
        dep = apps_api.read_namespaced_deployment(deployment, namespace)
    selector = getattr(getattr(dep, "spec", None), "selector", None)
    match_labels = getattr(selector, "match_labels", None) or {}
    match_expressions = list(getattr(selector, "match_expressions", None) or [])

    terms: list[str] = [f"{key}={value}" for key, value in sorted(match_labels.items())]

    for expr in match_expressions:
        key = getattr(expr, "key", None)
        operator = getattr(expr, "operator", None)
        values = list(getattr(expr, "values", None) or [])
        if not key or not operator:
            continue
        op = str(operator)
        if op == "In":
            terms.append(f"{key} in ({','.join(values)})")
        elif op == "NotIn":
            terms.append(f"{key} notin ({','.join(values)})")
        elif op == "Exists":
            terms.append(key)
        elif op == "DoesNotExist":
            terms.append(f"!{key}")
        else:
            # Unsupported in the label-selector grammar (e.g. Gt/Lt on numeric
            # values). Conservative direction: fall back to matchLabels only
            # and warn — a narrower selector cannot false-evict.
            logger.warning(
                "worker Deployment %s/%s declares matchExpressions operator %r "
                "on key %r; the Kubernetes label-selector grammar does not "
                "support this operator — falling back to matchLabels=%r "
                "(#44: narrower selector = conservative direction)",
                namespace,
                deployment,
                op,
                key,
                match_labels,
            )
            terms = [f"{key}={value}" for key, value in sorted(match_labels.items())]
            break

    if not terms:
        return None
    return ",".join(terms)


# Shared rolling-restart helper (issue #395).
#
# Two handler modules (worker_recycler.py and web_background_monitor.py)
# had byte-equivalent blocks that constructed the same patch_body dict,
# called apps_api.patch_namespaced_deployment(...), appended the dry-run
# suppression message, and emitted a counter increment. The constants
# RESTARTED_AT_ANNOTATION and DEFAULT_WORKER_DEPLOYMENT were also
# duplicated. The constants below are now the single source of truth —
# both handler modules import them from here (issue #585 moved
# MERGE_PATCH_CONTENT_TYPE here as well: every production consumer —
# this module's Deployment patch and status_store's CR .status
# subresource patch — is a Kubernetes API patch caller, and the old
# utility-imports-from-domain direction ``_k8s`` → ``status_store``
# violated layering; ``status_store`` now imports it FROM here).

#: Pod-template annotation driving the rolling restart — same key/values as
#: ``kubectl rollout restart``; only the value changing triggers a rollout.
RESTARTED_AT_ANNOTATION = "kubectl.kubernetes.io/restartedAt"

#: Fallback when ``spec.targetWorkerDeployment`` is empty: the helm
#: ``develop`` chart's fixed worker Deployment name (AGENTS.md identifiers).
#: Also the liveness-corroboration fleet name for the web_background
#: stall monitor's leg C.
DEFAULT_WORKER_DEPLOYMENT = "worker"

#: RFC 7386 media type for every Kubernetes API PATCH the operator issues
#: (Deployment pod-template annotation patches here, the CR ``.status``
#: subresource patch in :mod:`openstudio_operator.status_store`). The
#: generated client's default selection for Deployment patches is
#: json-patch (ops array), which a dict body is not; merge preserves
#: sibling annotations.
MERGE_PATCH_CONTENT_TYPE = "application/merge-patch+json"


def rolling_restart_deployment(
    apps_api: DeploymentManager,
    *,
    deployment: str,
    namespace: str,
    now: datetime,
    restart_annotation: str = RESTARTED_AT_ANNOTATION,
    content_type: str = MERGE_PATCH_CONTENT_TYPE,
) -> str:
    """Perform a rolling restart of a Deployment by patching the pod template annotation.

    The caller owns the D11 dry-run gate: suppress the call entirely when
    ``spec.dryRun`` is set (the suppression message and Event/counter
    emission stay at the handler call sites, where the Event text differs
    per module). The explicit merge-patch content type (RFC 7386) is
    applied here: the generated client's default selection for Deployment
    patches is json-patch (ops array), which a dict body is not, and merge
    preserves sibling annotations.

    Args:
        apps_api: Kubernetes client typed against the
            :class:`DeploymentManager` Protocol (issue #592) — the real
            ``AppsV1Api`` and any structural fake providing
            ``patch_namespaced_deployment`` satisfy it alike.
        deployment: Name of the Deployment to restart.
        namespace: Namespace of the Deployment.
        now: Timestamp to use for the restart annotation value (UTC).
        restart_annotation: Annotation key to patch (default is the kubectl
            standard :data:`RESTARTED_AT_ANNOTATION`).
        content_type: Content-Type header for the PATCH request; defaults
            to :data:`MERGE_PATCH_CONTENT_TYPE` (RFC 7386, canonical home
            here since issue #585) to preserve sibling annotations.

    Returns:
        The ISO-format restart timestamp that was written to the annotation.
    """
    restart_value = now.astimezone(UTC).isoformat()
    patch_body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {restart_annotation: restart_value}
                }
            }
        }
    }
    # Issue #488 — time the apiserver PATCH itself (the network surface);
    # a slow-but-successful apiserver is the blind spot the 409 counters
    # leave open. Observed on failure too — duration is duration.
    with observe_duration(KUBE_API_REQUEST_DURATION_SECONDS, verb="patch"):
        apps_api.patch_namespaced_deployment(
            deployment,
            namespace,
            body=patch_body,
            _content_type=content_type,
        )
    return restart_value


def load_operator_kube_config() -> None:
    """Load the operator pod's kubeconfig: in-cluster first, ``kube_config`` fallback.

    SINGLE public loader for the operator's Kubernetes configuration
    (issues #158, #251, #305). The upstream gate every K8s API call
    shares: the in-cluster ``KUBERNETES_SERVICE_HOST`` /
    ``KUBERNETES_SERVICE_PORT`` envs plus the pod's service-account
    token mount; on ``ConfigException`` (bare ``kopf run`` dev sessions,
    no service-account env vars) falls back to ``~/.kube/config``.

    Why here rather than in :mod:`openstudio_operator.singleton` where
    the loader previously lived: ``singleton`` originally owned the
    factory (``operator_custom_objects_api``) and the loader was a
    private helper beside it. The companion ``_load_kube_config`` in
    :mod:`openstudio_operator.prune_entrypoint` was a verbatim copy of
    the same try/except, and any future loader change (kubeconfig
    Secret reference, network-proxy client, custom CA bundle) had to
    land in two places. The factories' same-file loader was the source
    of the duplication problem; the cross-handler ``_k8s`` module is the
    neutral home (its module docstring already enumerates the helpers
    it hosts, so the loader fits there), and the AST gate in
    ``tests/test_singleton_registry_coverage.py::test_only_one_kubeconfig_loader_call_site``
    rejects any inline ``load_incluster_config(`` /
    ``load_kube_config(`` call outside this module so a partial update
    fails CI loudly (issue #305).

    Behaviour:

    * In-cluster first via ``kubernetes.config.load_incluster_config``;
      on ``ConfigException`` falls back to
      ``kubernetes.config.load_kube_config``. Same posture as the
      original ``_load_k8s_config`` (issue #79) — kopf >=1.44 never
      initializes client-python's default ``Configuration``, so a bare
      ``*V1Api()`` with no loaded config raises ``LocationValueError``
      on every call (proven live in #66's kind validation).
    * Idempotent in production — the loader is idempotent on the same
      source and the factories check their cache before calling. The
      ``singleton.operator_*_api`` factories and
      :func:`openstudio_operator.prune_entrypoint.main` call this
      loader directly (issue #405 removed the thin wrappers).
    * On config-loading failure (both ``load_incluster_config`` and
      ``load_kube_config`` raise ``ConfigException``), propagates the
      second exception — callers fail closed (skip the tick, retry next
      poll per D12).

    Tests that mock ``kubernetes.config.load_incluster_config`` use
    :func:`openstudio_operator.singleton.reset_operator_k8s_client` to
    drop the cache between cases so the next call to any
    ``operator_*_api()`` factory re-runs the load path with the freshly
    patched loader. The local import inside the function binds the
    module-level patched function object, so monkeypatch-style test
    replacements of ``kubernetes.config.load_incluster_config`` are
    honored on every call.
    """
    from kubernetes.config import (
        ConfigException,
        load_incluster_config,
        load_kube_config,
    )

    try:
        load_incluster_config()
    except ConfigException:
        load_kube_config()


class _RestClientHolder(Protocol):
    """Structural slice shared by every ``kubernetes.client`` API object.

    ``CustomObjectsApi`` / ``*V1Api`` instances all carry the process-wide
    :class:`kubernetes.client.ApiClient` as ``api_client`` (whose
    ``rest_client`` is the :class:`kubernetes.client.rest.RESTClientObject`
    this module's timeout wrapper replaces — see
    :class:`BoundedK8sRequest`). Declaring the slice as a Protocol keeps
    :func:`apply_request_timeout` typed against the structural shape
    instead of any single concrete client class, mirroring the
    ``DeploymentReader`` / ``PodLister`` convention above.
    """

    api_client: Any


_K8sApiT = TypeVar("_K8sApiT", bound=_RestClientHolder)


class BoundedK8sRequest:
    """Issue #579 — bound every Kubernetes API request to a finite timeout.

    Installed as the ``request`` attribute of the constructed client's
    ``rest_client`` by :func:`apply_request_timeout` (called once at the
    shared ``singleton._cached_k8s_api`` factory path, so all four client
    types — and the prune CronJob via the same factories — get it for
    free). Behaviour:

    * An omitted (``None``) ``_request_timeout`` is defaulted to
      :data:`openstudio_operator._constants.K8S_REQUEST_TIMEOUT_SECONDS`
      (15 s); an explicit per-call value (scalar or ``(connect, read)``
      tuple) passes through untouched — a call site that knows better
      can still bound itself.
    * A urllib3 timeout error raised past the bound
      (``urllib3.exceptions.TimeoutError`` and its ``Read``/``Connect``/
      ``WriteTimeoutError`` subclasses, or a ``MaxRetryError`` whose
      ``reason`` is one of those) is translated into
      ``ApiException(status=0, reason=...)`` — ``status=0`` matching
      kubernetes' own "no HTTP response" convention (``rest.py`` raises
      ``ApiException(status=0)`` for SSL failures). ``ApiException`` is
      a :data:`openstudio_operator._oscm_handlers.SKIP_TICK_EXCEPTIONS`
      member, so the timer wrapper converts the hang into a COUNTED
      skip-tick (``HANDLER_TICK_FAILURES_TOTAL`` + retry next poll) per
      D12 — instead of the pre-#579 forever-blocked tick.
    * Non-timeout transport errors propagate untranslated (fail-closed
      posture unchanged).

    Why a request wrapper and NOT ``Configuration.timeout``:
    kubernetes-python (the supported ``>=29.3,<37`` range) has no
    ``Configuration.timeout`` attribute at all — nothing in the package
    consults one; the generated ``*V1Api`` methods pass only a per-call
    ``_request_timeout`` (``local_var_params.get('_request_timeout')``)
    and ``rest.RESTClientObject.request`` defaults the urllib3 timeout
    to ``None`` (wait forever). A pool-level urllib3 default does not
    work either: ``rest.py`` passes ``timeout=None`` EXPLICITLY, which
    overrides any pool default. Wrapping ``rest_client.request`` at the
    single construction path is the only one-site mechanism that bounds
    every call — and it is the layer where the urllib3 timeout error
    surfaces first, so the ``ApiException`` translation lives here too.
    """

    def __init__(
        self,
        original: Callable[..., object],
        *,
        default_timeout_seconds: float = K8S_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.original = original
        self.default_timeout_seconds = default_timeout_seconds

    def __call__(self, method: str, url: str, **kwargs: object) -> object:
        per_call = kwargs.pop("_request_timeout", None)
        effective = per_call if per_call is not None else self.default_timeout_seconds
        try:
            return self.original(method, url, _request_timeout=effective, **kwargs)
        except urllib3.exceptions.TimeoutError as exc:
            raise ApiException(
                status=0,
                reason=(
                    f"K8s API request timed out (bounded at {effective}s by "
                    f"K8S_REQUEST_TIMEOUT_SECONDS, issue #579): "
                    f"{type(exc).__name__}: {exc}"
                ),
            ) from exc
        except urllib3.exceptions.MaxRetryError as exc:
            if isinstance(exc.reason, urllib3.exceptions.TimeoutError):
                raise ApiException(
                    status=0,
                    reason=(
                        f"K8s API request timed out after retries (bounded at "
                        f"{effective}s by K8S_REQUEST_TIMEOUT_SECONDS, issue "
                        f"#579): {type(exc.reason).__name__}: {exc.reason}"
                    ),
                ) from exc
            raise


def apply_request_timeout(client: _K8sApiT) -> _K8sApiT:
    """Install :class:`BoundedK8sRequest` as the client's ``rest_client.request``.

    Called by :func:`openstudio_operator.singleton._cached_k8s_api` right
    after the ``build()`` thunk constructs each client — the ONE site all
    four ``operator_*_api`` factories (and through them the prune
    CronJob and the #463 Secret read) share, so every Kubernetes API
    request the operator process makes is bounded by
    :data:`openstudio_operator._constants.K8S_REQUEST_TIMEOUT_SECONDS`.
    The bare no-arg ``XApi()`` construction calls stay byte-identical in
    ``singleton.py`` (the #158 / #251 AST gates pin that shape).

    Idempotent: if the client's ``rest_client.request`` is already a
    ``BoundedK8sRequest`` the client is returned unchanged (no
    double-wrap), so re-applying after a guard rebuild is safe.
    """
    rest_client = client.api_client.rest_client
    if isinstance(rest_client.request, BoundedK8sRequest):
        return client
    rest_client.request = BoundedK8sRequest(rest_client.request)
    return client
