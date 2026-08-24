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

Issue #653 finished the #531/#567 leftovers: ``FakeBatchV1Api`` (the
retention/prune Job surface, previously two copies with silently divergent
delete semantics), ``FakePodsCoreV1Api`` (the pod-list surface, previously
duplicated across the SLA / stall-monitor / walkthrough suites), ``make_job``
(the V1Job duck type the batch fake builds), and ``make_cr`` gained the
``uid``/``created`` params that absorb the singleton-guard / lenient-factories
/ prune-entrypoint local copies.

Issue #718 added ``FakeCoreV1Api`` (the prune-CronJob's
``create_namespaced_event`` surface — the only CoreV1Api method
``prune_entrypoint.main`` actually uses; previously a local copy in
``tests/test_prune_entrypoint.py`` that was the last K8s-API surface
not in this shared module).
"""

from __future__ import annotations

import base64
import copy
from types import SimpleNamespace

import responses
from kubernetes.client import ApiException
from prometheus_client import REGISTRY

from openstudio_operator.status_store import GROUP, PLURAL, VERSION

NAME = "oscm"
NAMESPACE = "openstudio-server"


def encode_secret_value(value: str) -> str:
    """Base64-encode a Secret ``data`` value the way the API server returns it.

    The kubernetes client library does NOT decode Secret ``data``; the #463
    resolution path in ``client_factory._resolve_redis_url`` does its own
    ``base64.b64decode``. Fakes that seed a Secret therefore store the
    encoded form.
    """
    return base64.b64encode(value.encode()).decode()


def make_cr(
    spec: dict | None = None,
    status: dict | None = None,
    *,
    name: str = NAME,
    namespace: str = NAMESPACE,
    default_spec: dict | None = None,
    uid: str | None = None,
    created: str | None = None,
) -> dict:
    """Minimal OSCM custom-resource body.

    Per-module default specs differ, so consumers bind theirs with
    ``functools.partial(make_cr, default_spec=SPEC)`` — the body is otherwise
    byte-identical to the helpers this replaces.

    ``uid``/``created`` (#653) populate ``metadata.uid`` /
    ``metadata.creationTimestamp`` when given — the singleton guard's
    ``_same_cr`` identity needs both, so the three modules that used to carry
    local ``make_cr`` copies hardcoding the CRD identity strings bind them
    here instead.
    """
    meta: dict = {"name": name, "namespace": namespace}
    if uid is not None:
        meta["uid"] = uid
    if created is not None:
        meta["creationTimestamp"] = created
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "OpenStudioClusterManager",
        "metadata": meta,
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


def make_job(name: str, *, complete: bool = False, failed: bool = False, running: bool = True):
    """Generated-client Job shape (attribute-style V1Job duck type).

    ``running`` with a non-terminal condition present but not True exercises
    the condition filter (a Job that exists but has not terminated). Moved
    from test_retention.py (#653) because the shared ``FakeBatchV1Api`` builds
    its created Jobs with it.
    """
    conditions = []
    if complete:
        conditions.append(SimpleNamespace(type="Complete", status="True"))
    if failed:
        conditions.append(SimpleNamespace(type="Failed", status="True"))
    if running and not complete and not failed:
        conditions.append(SimpleNamespace(type="Complete", status="False"))
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(conditions=conditions or None, succeeded=0, failed=0),
    )


class FakeBatchV1Api:
    """BatchV1Api stand-in: in-memory Jobs with faithful 404/409 semantics (issue #653).

    Consolidates the two pre-#653 near-verbatim copies (test_retention.py /
    test_prune_entrypoint.py). Their one silent divergence — delete of a
    MISSING Job — is reconciled the strict, real-API-faithful way: 404, like
    the retention copy (and like a real API server); the prune copy's lax
    ``pop(name, None)`` swallow is gone, so a call path that deletes a
    vanished Job now fails loudly in tests instead of passing silently.

    - ``read_namespaced_job`` — records every attempt in ``reads`` as
      ``{"name", "namespace"}`` dicts; raises 404 on missing.
    - ``create_namespaced_job`` — records EVERY attempt (including raising
      ones) in ``creates`` as ``{"namespace", "body"}`` (body deep-copied),
      raises 409 on a duplicate name, then stores and returns a
      :func:`make_job`-shaped Job.
    - ``delete_namespaced_job`` — records every attempt (including raising
      ones) in ``deletes`` as ``{"name", "namespace", "kwargs"}`` (retention
      asserts ``propagation_policy`` through it), raises 404 on missing,
      then removes the Job.
    - ``fail_with`` — exception the NEXT create/delete raises after being
      recorded, one-shot (ApiException propagation tests; ``FakeAppsV1Api``'s
      #531 precedent).

    The create/read/delete exercised here are the ``batch/jobs`` verbs from
    deploy/storage-cronjob.yaml (the prune CronJob's Role, #78).
    """

    def __init__(self, jobs: list | None = None) -> None:
        self.jobs = {job.metadata.name: job for job in (jobs or [])}
        self.reads: list[dict] = []
        self.creates: list[dict] = []
        self.deletes: list[dict] = []
        self.fail_with: ApiException | None = None

    def read_namespaced_job(self, name, namespace, **kwargs):
        self.reads.append({"name": name, "namespace": namespace})
        if name not in self.jobs:
            raise ApiException(status=404, reason="Not Found")
        return self.jobs[name]

    def create_namespaced_job(self, namespace, body, **kwargs):
        name = body["metadata"]["name"]
        self.creates.append({"namespace": namespace, "body": copy.deepcopy(body)})
        if self.fail_with is not None:
            exc, self.fail_with = self.fail_with, None
            raise exc
        if name in self.jobs:
            raise ApiException(status=409, reason="Conflict")
        job = make_job(name)
        self.jobs[name] = job
        return job

    def delete_namespaced_job(self, name, namespace, **kwargs):
        self.deletes.append({"name": name, "namespace": namespace, "kwargs": kwargs})
        if self.fail_with is not None:
            exc, self.fail_with = self.fail_with, None
            raise exc
        if name not in self.jobs:
            raise ApiException(status=404, reason="Not Found")
        del self.jobs[name]
        return {}


class FakePodsCoreV1Api:
    """CoreV1Api pod stand-in: fixed pod list + recorded deletes (issue #653).

    Consolidates the pre-#653 pod-list copies: test_analysis_sla's
    list+delete recorder (the #83 D2 escalation surface),
    test_web_background_monitor's selector recorder, and the walkthrough's
    filtering variant (``FakePodApi``).

    - ``list_namespaced_pod`` — serves ``pods`` as-is, recording every call
      in ``list_calls`` as ``{"namespace", "label_selector", "kwargs"}`` and
      every selector in ``selectors``. With ``filter_label_selector=True``
      the list is filtered server-side (comma-separated equality ``k=v``
      pairs — real-API semantics; the walkthrough variant). Default False:
      the stall-monitor's pods are deliberately label-free duck types, so
      the default serves every pod exactly like both pre-#653 copies.
    - ``delete_namespaced_pod`` — records every attempt in ``deletes`` as
      ``{"name", "namespace", "kwargs"}`` (the escalation eviction path).
    """

    def __init__(self, pods: list, *, filter_label_selector: bool = False) -> None:
        self.pods = list(pods)
        self.filter_label_selector = filter_label_selector
        self.list_calls: list[dict] = []
        self.selectors: list[str | None] = []
        self.deletes: list[dict] = []

    def list_namespaced_pod(self, namespace, label_selector=None, **kwargs):
        self.list_calls.append(
            {"namespace": namespace, "label_selector": label_selector, "kwargs": kwargs}
        )
        self.selectors.append(label_selector)
        items = list(self.pods)
        if label_selector and self.filter_label_selector:
            wanted = label_selector.split(",")
            items = [
                pod
                for pod in items
                if all(f"{k}={v}" in wanted for k, v in pod.metadata.labels.items())
            ]
        return SimpleNamespace(items=items)

    def delete_namespaced_pod(self, name, namespace, **kwargs):
        self.deletes.append({"name": name, "namespace": namespace, "kwargs": kwargs})
        return {}


def make_emit():
    events: list[tuple[str, str, str]] = []

    def emit(event_type: str, reason: str, message: str) -> None:
        events.append((event_type, reason, message))

    return events, emit


class FakeSecretsCoreV1Api:
    """Just enough ``CoreV1Api`` for the #463 Secret read (issue #567).

    Serves ``read_namespaced_secret`` only — the single bounded Secret
    access ``client_factory._resolve_redis_url`` performs. Mirrors the
    local copy in ``tests/test_client_factory.py`` (which predates the
    sharing and is left local — refactoring that file is out of #567's
    scope); the handler-wrapper tests added by #567 import this one so
    the surface is defined once going forward. Records every read as a
    ``(name, namespace)`` tuple in ``.calls`` so tests can assert the
    factory asked for exactly the referenced Secret in the CR's
    namespace.

    Issue #568 adds the rotation surface: reads serve
    ``metadata.resource_version`` (tracked in ``.versions_read``), and
    :meth:`rotate` models an in-place Secret update — new content plus a
    resourceVersion bump — so tests can pin the factory's re-resolution
    behaviour without a live API server.
    """

    def __init__(
        self,
        data: dict[str, str] | None = None,
        exc: Exception | None = None,
        resource_version: str = "1000",
    ):
        self._data = dict(data or {})
        self._exc = exc
        self._resource_version = resource_version
        self.calls: list[tuple[str, str]] = []
        self.versions_read: list[str] = []

    def rotate(
        self,
        data: dict[str, str],
        resource_version: str | None = None,
    ) -> None:
        """Model an in-place Secret update (#568): swap content, bump rv."""
        self._data = dict(data)
        self._resource_version = resource_version or str(int(self._resource_version) + 1)

    def read_namespaced_secret(self, name: str, namespace: str):
        self.calls.append((name, namespace))
        if self._exc is not None:
            raise self._exc
        self.versions_read.append(self._resource_version)
        return SimpleNamespace(
            data=dict(self._data),
            metadata=SimpleNamespace(resource_version=self._resource_version),
        )


class FakeCoreV1Api:
    """Just enough ``CoreV1Api`` for the prune-CronJob's Event emission.

    Issue #718. The CronJob platform has no kopf, so ``prune_entrypoint.main``
    calls ``core_v1.create_namespaced_event(namespace, body)`` directly via
    ``build_event_emitter``. Records every event as a
    ``{"namespace", "body"}`` dict in ``.events`` so tests can assert the
    Event reason / message / involvedObject without a live API server.

    Consumes only the ``create_namespaced_event`` surface — the same
    minimum the prior local copy in ``tests/test_prune_entrypoint.py``
    served (and the same minimum the real CronJob pod exercises in
    production).
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def create_namespaced_event(self, namespace, body, **_kw):
        self.events.append({"namespace": namespace, "body": body})
        return body


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
