"""Shared test fakes for the OSCM suite (issue #474).

Canonical home for the K8s CustomObjectsApi stand-in, the RFC 7386 merge-patch
mirror, and the small CR/event/metric helpers that used to be duplicated
byte-identical across a dozen test modules. Underscore-prefixed so pytest does
not collect it (same convention as ``tests/_metrics_inventory.py``).

Consumers import via pytest's rootdir path insertion — ``from _fakes import
FakeCustomObjectsApi`` — exactly like the existing ``from _metrics_inventory
import ...`` style. This module must never import from a ``test_*`` module.
"""

from __future__ import annotations

import copy

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
