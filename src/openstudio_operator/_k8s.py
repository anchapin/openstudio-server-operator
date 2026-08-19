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
  function's docstring for the rationale).

Why ``_k8s`` and not ``k8s_helpers`` / ``k8s_api_helpers``? Mirrors the
``_constants`` / ``_time`` convention in this package (operator-internal
utilities prefixed with ``_`` and kept out of the public surface). The leading
underscore is the convention the AGENTS.md module table uses to mark
shared-internal modules.
"""

from __future__ import annotations

import logging
from typing import Protocol

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
