"""Shared test fakes for the OSCM suite (issue #474).

Canonical home for the K8s CustomObjectsApi stand-in, the RFC 7386 merge-patch
mirror, and the small CR/event/metric helpers that used to be duplicated
byte-identical across a dozen test modules. Underscore-prefixed so pytest does
not collect it (same convention as ``tests/_metrics_inventory.py``).

Consumers import via pytest's rootdir path insertion — ``from _fakes import
FakeCustomObjectsApi`` — exactly like the existing ``from _metrics_inventory
import ...`` style. This module must never import from a ``test_*`` module.

Issue #531 added ``FakeAppsV1Api`` (the deployment-patch surface the worker
recycler, web_background stall monitor, and ``_k8s`` helper tests share) —
previously triplicated across three test modules with a divergent
apply-semantics fourth copy.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import responses
from kubernetes.client import ApiException
from prometheus_client import REGISTRY

from openstudio_operator.status_store import GROUP, PLURAL, VERSION

NAME = "oscm"
NAMESPACE = "openstudio-server"


def make_cr(
    spec: dict | None = None,
    status: dict | None = None,
    *,
    name: str = NAME,
    namespace: str = NAMESPACE,
    default_spec: dict | None = None,
) -> dict:
    """Minimal OSCM custom-resource body.

    Per-module default specs differ, so consumers bind theirs with
    ``functools.partial(make_cr, default_spec=SPEC)`` — the body is otherwise
    byte-identical to the helpers this replaces.
    """
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "OpenStudioClusterManager",
        "metadata": {"name": name, "namespace": namespace},
        "spec": copy.deepcopy(spec if spec is not None else (default_spec or {})),
        "status": copy.deepcopy(status if status is not None else {}),
    }


def _merge_patch(target: dict, patch: dict) -> None:
    """RFC 7386 merge-patch — the exact RMW semantics ``StatusStore`` depends on."""
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_patch(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class FakeCustomObjectsApi:
    """In-memory CustomObjectsApi stand-in with RFC 7386 merge-patch + synthetic 409s.

    Union of the per-module variants duplicated before #474:

    - ``obj`` — the CR body served by get/patch_namespaced_custom_object_status
      (the handler/status-store surface).
    - ``items`` — the CR list served by list_namespaced_custom_object (the
      singleton-guard resolution surface; mutable in place so tests can
      append/clear between calls).
    - ``patch_conflicts`` — number of leading status patches that raise a
      synthetic 409 ``ApiException`` before the merge is applied (StatusStore
      RMW-retry tests). ``patch_calls``/``patches`` record EVERY attempt,
      including the conflicting ones.
    """

    def __init__(
        self,
        obj: dict | None = None,
        items: list | None = None,
        *,
        patch_conflicts: int = 0,
    ) -> None:
        self.obj = copy.deepcopy(obj) if obj is not None else {}
        self.items = copy.deepcopy(items) if items is not None else []
        self.remaining_conflicts = patch_conflicts
        self.conflicts_seen = 0
        self.get_calls = 0
        self.patch_calls = 0
        self.list_calls = 0
        self.patches: list[tuple[dict, str | None]] = []

    def list_namespaced_custom_object(self, group, version, namespace, plural, **_kw):
        assert (group, version, plural) == (GROUP, VERSION, PLURAL)
        assert namespace == NAMESPACE
        self.list_calls += 1
        return {"items": copy.deepcopy(self.items)}

    def get_namespaced_custom_object_status(self, group, version, namespace, plural, name):
        self.get_calls += 1
        return copy.deepcopy(self.obj)

    def patch_namespaced_custom_object_status(
        self, group, version, namespace, plural, name, body, _content_type=None
    ):
        self.patch_calls += 1
        self.patches.append((copy.deepcopy(body), _content_type))
        if self.remaining_conflicts > 0:
            self.remaining_conflicts -= 1
            self.conflicts_seen += 1
            raise ApiException(status=409, reason="Conflict")
        _merge_patch(self.obj, body)
        return copy.deepcopy(self.obj)


#: The helm ``develop`` worker Deployment's selector — the default the shared
#: ``FakeAppsV1Api`` serves for ``read_namespaced_deployment``. Both handler
#: tick suites (worker recycler, web_background stall monitor) construct the
#: fake empty and rely on this default (AGENTS.md fixed identifiers).
_WORKER_MATCH_LABELS = {"app.kubernetes.io/name": "openstudio-server", "component": "worker"}


class FakeAppsV1Api:
    """In-memory AppsV1Api stand-in: read-selector server + merge-patch-applying Deployment store.

    Consolidates the four per-module copies that existed before #531
    (test_analysis_sla / test_web_background_monitor / test_worker_recycler
    each carried a record-only variant; test_k8s_rolling_restart carried the
    apply-semantics variant) into one union surface:

    - ``read_namespaced_deployment`` — serves the ``match_labels`` /
      ``match_expressions`` constructor args (the ``deployment_label_selector``
      surface) and records ``reads`` as ``(name, namespace)`` tuples. Defaults
      to the worker selector.
    - ``patch_namespaced_deployment`` — records EVERY attempt (including
      raising ones) in ``patches`` as ``{"name", "namespace", "body",
      "kwargs"}``, then applies the patch to ``obj`` with the shared RFC 7386
      mirror (``_merge_patch``) and returns the patched object. The record
      shape is byte-identical to the pre-#531 record-only copies, so
      call-count/body assertions keep working; applying server-side
      additionally lets merge-semantics assertions (sibling-annotation
      preservation) run against real state, not just the outgoing body.
    - ``fail_with`` — exception the NEXT patch raises before touching ``obj``
      (ApiException propagation tests).
    - No delete method at all — the operator's AppsV1Api surface is
      read + rolling-restart patch only (the pre-#531 worker-recycler copy
      pinned the same absence deliberately).
    """

    def __init__(
        self,
        deployment_obj: dict | None = None,
        match_labels: dict | None = None,
        *,
        match_expressions: list | None = None,
    ) -> None:
        self.obj = copy.deepcopy(deployment_obj or {})
        self.match_labels = dict(
            match_labels if match_labels is not None else _WORKER_MATCH_LABELS
        )
        self.match_expressions = list(match_expressions if match_expressions is not None else [])
        self.patches: list[dict] = []
        self.reads: list[tuple[str, str]] = []
        self.fail_with: ApiException | None = None

    def read_namespaced_deployment(self, name, namespace, **kwargs):
        self.reads.append((name, namespace))
        return SimpleNamespace(
            spec=SimpleNamespace(
                selector=SimpleNamespace(
                    match_labels=dict(self.match_labels),
                    match_expressions=list(self.match_expressions),
                )
            )
        )

    def patch_namespaced_deployment(self, name, namespace, body, **kwargs):
        self.patches.append(
            {"name": name, "namespace": namespace, "body": copy.deepcopy(body), "kwargs": kwargs}
        )
        if self.fail_with is not None:
            exc, self.fail_with = self.fail_with, None
            raise exc
        _merge_patch(self.obj, body)
        return copy.deepcopy(self.obj)


def make_emit():
    events: list[tuple[str, str, str]] = []

    def emit(event_type: str, reason: str, message: str) -> None:
        events.append((event_type, reason, message))

    return events, emit


def calls_to(suffix: str) -> int:
    return sum(1 for call in responses.calls if call.request.url.endswith(suffix))


def tick_failures_total(namespace: str, name: str, module: str, error_type: str) -> float:
    """Read the labelled HANDLER_TICK_FAILURES_TOTAL sample (issue #117 shape)."""
    return (
        REGISTRY.get_sample_value(
            "openstudio_operator_handler_tick_failures_total",
            {"namespace": namespace, "name": name, "module": module, "error_type": error_type},
        )
        or 0.0
    )
