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
* :func:`deployment_label_selector` — build a Kubernetes ``label_selector=``
  query string from a Deployment's own ``spec.selector`` (honors BOTH
  ``matchLabels`` AND ``matchExpressions``; the narrow-fallback-on-unsupported
  operator behavior established by issue #44 was preserved verbatim — see the
  function's docstring for the rationale),
* :func:`rolling_restart_deployment` — perform a rolling restart of a
  Deployment by patching the pod template's ``restartedAt`` annotation,
  plus the single-site constants :data:`RESTARTED_AT_ANNOTATION` and
  :data:`DEFAULT_WORKER_DEPLOYMENT` the worker recycler and web_background
  stall monitor both import (issue #395).
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

Why ``_k8s`` and not ``k8s_helpers`` / ``k8s_api_helpers``? Mirrors the
``_constants`` / ``_time`` convention in this package (operator-internal
utilities prefixed with ``_`` and kept out of the public surface). The leading
underscore is the convention the AGENTS.md module table uses to mark
shared-internal modules.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from kubernetes.client import AppsV1Api

from openstudio_operator.status_store import MERGE_PATCH_CONTENT_TYPE

logger = logging.getLogger(__name__)


class DeploymentReader(Protocol):
    """Structural type of ``AppsV1Api`` for read-only Deployment lookup.

    Tests fake exactly this — passing any object with a
    ``read_namespaced_deployment(name, namespace)`` callable satisfies the
    Protocol. The wider ``AppsV1Api`` shape (deployments, stateful sets, etc.)
    is intentionally NOT captured here: handlers that need additional methods
    should declare their own narrow Protocol and extend the surface
    deliberately, rather than type-hinting against the full ``AppsV1Api`` and
    forcing every test fake to reproduce the same broad interface.

    Issue #236 surfaced a second Protocol (``WorkerDeploymentApi`` in
    :mod:`openstudio_operator.handlers.web_background_monitor`) that wraps
    ``DeploymentReader`` with a ``patch_namespaced_deployment`` method for the
    rolling-restart path. That Protocol is handler-local and stays put —
    :func:`deployment_label_selector` only needs the read-only slice.
    """

    def read_namespaced_deployment(self, name: str, namespace: str, **_: object) -> object: ...


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
# both handler modules import them from here (and re-export for their
# test files' import compatibility).

#: Pod-template annotation driving the rolling restart — same key/values as
#: ``kubectl rollout restart``; only the value changing triggers a rollout.
RESTARTED_AT_ANNOTATION = "kubectl.kubernetes.io/restartedAt"

#: Fallback when ``spec.targetWorkerDeployment`` is empty: the helm
#: ``develop`` chart's fixed worker Deployment name (AGENTS.md identifiers).
#: Also the liveness-corroboration fleet name for the web_background
#: stall monitor's leg C.
DEFAULT_WORKER_DEPLOYMENT = "worker"


def rolling_restart_deployment(
    apps_api: AppsV1Api,
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
        apps_api: Kubernetes AppsV1Api client (or a structural fake with
            ``patch_namespaced_deployment``).
        deployment: Name of the Deployment to restart.
        namespace: Namespace of the Deployment.
        now: Timestamp to use for the restart annotation value (UTC).
        restart_annotation: Annotation key to patch (default is the kubectl
            standard :data:`RESTARTED_AT_ANNOTATION`).
        content_type: Content-Type header for the PATCH request; defaults
            to :data:`openstudio_operator.status_store.MERGE_PATCH_CONTENT_TYPE`
            per RFC 7386 to preserve sibling annotations.

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
