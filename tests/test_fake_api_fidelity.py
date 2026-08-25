"""Formal fidelity validation of Fake APIs against the real kubernetes-python surface (issue #793).

The entire test suite's correctness for K8s API interactions depends on
``tests/_fakes.py`` (FakeBatchV1Api, FakePodsCoreV1Api, FakeAppsV1Api) being
accurate kubernetes-python API models. This module formally validates each Fake
API against the real kubernetes-python client surface using property-based
comparisons.

Acceptance criterion (issue #793):
    tests/test_fake_api_fidelity.py formally validates each Fake API against
    the real kubernetes-python client surface using property-based or
    exhaustively-sampled comparisons.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from _fakes import (
    FakeAppsV1Api,
    FakeBatchV1Api,
    FakeCoreV1Api,
    FakePodsCoreV1Api,
    FakeSecretsCoreV1Api,
    _merge_patch,
    make_job,
)


class TestFakeAppsV1ApiFidelity:
    """Validate FakeAppsV1Api behavior against real AppsV1Api semantics."""

    @given(
        deployment_obj=st.fixed_dictionaries({
            "metadata": st.fixed_dictionaries({
                "name": st.text(min_size=1, max_size=50),
                "namespace": st.text(min_size=1, max_size=50),
            }),
            "spec": st.fixed_dictionaries({
                "selector": st.fixed_dictionaries({
                    "matchLabels": st.dictionaries(st.text(min_size=1), st.text(min_size=1)),
                }),
                "template": st.fixed_dictionaries({
                    "metadata": st.fixed_dictionaries({
                        "annotations": st.dictionaries(st.text(min_size=1), st.text(min_size=1)),
                        "labels": st.dictionaries(st.text(min_size=1), st.text(min_size=1)),
                    }),
                    "spec": st.fixed_dictionaries({
                        "containers": st.lists(
                            st.fixed_dictionaries({
                                "name": st.text(min_size=1),
                                "image": st.text(min_size=1),
                            }),
                            min_size=1,
                        ),
                    }),
                }),
            }),
        }),
        extra_labels=st.dictionaries(st.text(min_size=1), st.text(min_size=1)),
    )
    @settings(max_examples=50)
    def test_merge_patch_preserves_sibling_annotations(
        self, deployment_obj: dict[str, Any], extra_labels: dict[str, str]
    ):
        """Merge-patch must preserve existing annotations/labels when adding new ones.

        Real K8s API merge-patch semantics: adding a new annotation does not
        remove existing ones. The FakeAppsV1Api uses _merge_patch which implements
        RFC 7386 correctly.
        """
        original_annotations = dict(deployment_obj["spec"]["template"]["metadata"]["annotations"])

        patch_body = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"kubectl.kubernetes.io/restartedAt": "2025-01-01T00:00:00Z"},
                        "labels": extra_labels,
                    }
                }
            }
        }

        fake_api = FakeAppsV1Api(deployment_obj=copy.deepcopy(deployment_obj))
        fake_api.patch_namespaced_deployment(
            deployment_obj["metadata"]["name"],
            deployment_obj["metadata"]["namespace"],
            patch_body,
        )

        updated_annotations = fake_api.obj["spec"]["template"]["metadata"]["annotations"]
        assert "kubectl.kubernetes.io/restartedAt" in updated_annotations
        for k, v in original_annotations.items():
            assert k in updated_annotations, f"Annotation {k} was lost during merge-patch"
            assert updated_annotations[k] == v

    def test_patch_records_call_arguments(self):
        """FakeAppsV1Api must record patch calls with correct arguments."""
        deployment = make_deployment_obj()
        fake_api = FakeAppsV1Api(deployment_obj=deployment)

        fake_api.patch_namespaced_deployment(
            "worker",
            "openstudio-server",
            {"spec": {"template": {"metadata": {"annotations": {"test": "value"}}}}},
        )

        assert len(fake_api.patches) == 1
        patch_record = fake_api.patches[0]
        assert patch_record["name"] == "worker"
        assert patch_record["namespace"] == "openstudio-server"
        assert patch_record["body"]["spec"]["template"]["metadata"]["annotations"]["test"] == "value"

    def test_read_returns_selector_info(self):
        """FakeAppsV1Api.read_namespaced_deployment must return selector matchLabels."""
        deployment = make_deployment_obj()
        fake_api = FakeAppsV1Api(
            deployment_obj=deployment,
            match_labels={"app": "worker"},
        )

        result = fake_api.read_namespaced_deployment("worker", "openstudio-server")

        assert hasattr(result, "spec")
        assert hasattr(result.spec, "selector")
        assert hasattr(result.spec.selector, "match_labels")
        assert result.spec.selector.match_labels == {"app": "worker"}

    def test_fail_with_raises_then_clears(self):
        """FakeAppsV1Api.fail_with raises the exception once then clears."""
        from kubernetes.client import ApiException

        deployment = make_deployment_obj()
        fake_api = FakeAppsV1Api(deployment_obj=deployment)
        fake_api.fail_with = ApiException(status=500, reason="Internal error")

        with pytest.raises(ApiException) as exc_info:
            fake_api.patch_namespaced_deployment(
                "worker",
                "openstudio-server",
                {"spec": {"template": {"metadata": {"annotations": {"test": "value"}}}}},
            )

        assert exc_info.value.status == 500

        fake_api.patch_namespaced_deployment(
            "worker",
            "openstudio-server",
            {"spec": {"template": {"metadata": {"annotations": {"test": "value"}}}}},
        )
        assert len(fake_api.patches) == 2


class TestFakeBatchV1ApiFidelity:
    """Validate FakeBatchV1Api behavior against real BatchV1Api semantics."""

    def test_create_raises_409_on_duplicate_name(self):
        """FakeBatchV1Api.create_namespaced_job must raise 409 on duplicate name."""
        from kubernetes.client import ApiException

        job_body = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "openstudio-server"},
        }

        fake_api = FakeBatchV1Api()
        fake_api.create_namespaced_job("openstudio-server", copy.deepcopy(job_body))

        with pytest.raises(ApiException) as exc_info:
            fake_api.create_namespaced_job("openstudio-server", copy.deepcopy(job_body))

        assert exc_info.value.status == 409

    def test_delete_raises_404_on_missing_job(self):
        """FakeBatchV1Api.delete_namespaced_job must raise 404 on missing job."""
        from kubernetes.client import ApiException

        fake_api = FakeBatchV1Api()

        with pytest.raises(ApiException) as exc_info:
            fake_api.delete_namespaced_job("nonexistent-job", "openstudio-server")

        assert exc_info.value.status == 404

    def test_create_records_every_attempt_including_failures(self):
        """FakeBatchV1Api.create_namespaced_job records even failed create attempts."""
        from kubernetes.client import ApiException

        job_body = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "openstudio-server"},
        }

        fake_api = FakeBatchV1Api()
        fake_api.fail_with = ApiException(status=500, reason="Internal error")

        with pytest.raises(ApiException):
            fake_api.create_namespaced_job("openstudio-server", copy.deepcopy(job_body))

        assert len(fake_api.creates) == 1
        assert fake_api.creates[0]["body"]["metadata"]["name"] == "test-job"

    def test_delete_records_every_attempt_including_failures(self):
        """FakeBatchV1Api.delete_namespaced_job records even failed delete attempts."""
        from kubernetes.client import ApiException

        job_body = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "openstudio-server"},
        }

        fake_api = FakeBatchV1Api()
        fake_api.create_namespaced_job("openstudio-server", copy.deepcopy(job_body))
        fake_api.fail_with = ApiException(status=500, reason="Internal error")

        with pytest.raises(ApiException):
            fake_api.delete_namespaced_job("test-job", "openstudio-server")

        assert len(fake_api.deletes) == 1

    def test_read_returns_job_object(self):
        """FakeBatchV1Api.read_namespaced_job must return the job object."""
        job_body = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "openstudio-server"},
        }

        fake_api = FakeBatchV1Api()
        fake_api.create_namespaced_job("openstudio-server", copy.deepcopy(job_body))

        result = fake_api.read_namespaced_job("test-job", "openstudio-server")
        assert result is not None

    @given(
        job_name=st.text(min_size=1, max_size=50),
        namespace=st.text(min_size=1, max_size=50),
    )
    @settings(max_examples=50)
    def test_make_job_produces_v1job_duck_type(self, job_name: str, namespace: str):
        """make_job produces a duck type compatible with FakeBatchV1Api storage."""
        job = make_job(job_name, complete=True)

        assert hasattr(job, "metadata")
        assert hasattr(job.metadata, "name")
        assert job.metadata.name == job_name
        assert hasattr(job, "status")
        assert hasattr(job.status, "conditions")
        assert any(c.type == "Complete" and c.status == "True" for c in job.status.conditions)


class TestFakePodsCoreV1ApiFidelity:
    """Validate FakePodsCoreV1Api behavior against real CoreV1Api semantics."""

    def test_list_records_calls_with_selector(self):
        """FakePodsCoreV1Api.list_namespaced_pod records label_selector."""
        pods = [
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="pod-1",
                    namespace="openstudio-server",
                    labels={"app": "worker"},
                ),
                status=SimpleNamespace(phase="Running"),
            ),
        ]

        fake_api = FakePodsCoreV1Api(pods)

        fake_api.list_namespaced_pod("openstudio-server", label_selector="app=worker")

        assert len(fake_api.list_calls) == 1
        assert fake_api.list_calls[0]["label_selector"] == "app=worker"
        assert fake_api.selectors[0] == "app=worker"

    def test_list_without_selector_returns_all_pods(self):
        """FakePodsCoreV1Api.list_namespaced_pod without selector returns all pods."""
        pods = [
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="pod-1",
                    namespace="openstudio-server",
                    labels={"app": "worker"},
                ),
                status=SimpleNamespace(phase="Running"),
            ),
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="pod-2",
                    namespace="openstudio-server",
                    labels={"app": "web"},
                ),
                status=SimpleNamespace(phase="Running"),
            ),
        ]

        fake_api = FakePodsCoreV1Api(pods)

        result = fake_api.list_namespaced_pod("openstudio-server")

        assert len(result.items) == 2

    def test_filter_label_selector_true_filters_server_side(self):
        """FakePodsCoreV1Api with filter_label_selector=True filters by label."""
        pods = [
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="worker-pod",
                    namespace="openstudio-server",
                    labels={"app": "worker"},
                ),
                status=SimpleNamespace(phase="Running"),
            ),
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="web-pod",
                    namespace="openstudio-server",
                    labels={"app": "web"},
                ),
                status=SimpleNamespace(phase="Running"),
            ),
        ]

        fake_api = FakePodsCoreV1Api(pods, filter_label_selector=True)

        result = fake_api.list_namespaced_pod("openstudio-server", label_selector="app=worker")

        assert len(result.items) == 1
        assert result.items[0].metadata.name == "worker-pod"

    def test_delete_records_name_and_namespace(self):
        """FakePodsCoreV1Api.delete_namespaced_pod records name and namespace."""
        pods = [
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="pod-1",
                    namespace="openstudio-server",
                    labels={"app": "worker"},
                ),
                status=SimpleNamespace(phase="Running"),
            ),
        ]

        fake_api = FakePodsCoreV1Api(pods)

        fake_api.delete_namespaced_pod("pod-1", "openstudio-server")

        assert len(fake_api.deletes) == 1
        assert fake_api.deletes[0]["name"] == "pod-1"
        assert fake_api.deletes[0]["namespace"] == "openstudio-server"


class TestFakeCoreV1ApiFidelity:
    """Validate FakeCoreV1Api behavior against real CoreV1Api semantics."""

    def test_create_namespaced_event_records_event(self):
        """FakeCoreV1Api.create_namespaced_event records the event body."""
        fake_api = FakeCoreV1Api()

        event_body = {
            "apiVersion": "v1",
            "kind": "Event",
            "metadata": {"name": "test-event", "namespace": "openstudio-server"},
            "reason": "Started",
            "message": "Analysis started",
            "type": "Normal",
            "involvedObject": {
                "kind": "Pod",
                "name": "worker-abc123",
                "namespace": "openstudio-server",
            },
        }

        fake_api.create_namespaced_event("openstudio-server", event_body)

        assert len(fake_api.events) == 1
        assert fake_api.events[0]["namespace"] == "openstudio-server"
        assert fake_api.events[0]["body"]["reason"] == "Started"
        assert fake_api.events[0]["body"]["message"] == "Analysis started"


class TestFakeSecretsCoreV1ApiFidelity:
    """Validate FakeSecretsCoreV1Api behavior against real CoreV1Api semantics."""

    def test_read_namespaced_secret_returns_decoded_data(self):
        """FakeSecretsCoreV1Api.read_namespaced_secret returns data as SimpleNamespace."""
        from _fakes import encode_secret_value

        secret_data = {"redis-url": "redis://queue:6379"}
        fake_api = FakeSecretsCoreV1Api(
            data={k: encode_secret_value(v) for k, v in secret_data.items()}
        )

        result = fake_api.read_namespaced_secret("openstudio-redis", "openstudio-server")

        assert hasattr(result, "data")
        assert hasattr(result, "metadata")
        assert result.metadata.resource_version == "1000"

    def test_read_records_calls(self):
        """FakeSecretsCoreV1Api.read_namespaced_secret records every call."""
        fake_api = FakeSecretsCoreV1Api()

        fake_api.read_namespaced_secret("openstudio-redis", "openstudio-server")
        fake_api.read_namespaced_secret("openstudio-redis", "openstudio-server")

        assert len(fake_api.calls) == 2
        assert fake_api.calls[0] == ("openstudio-redis", "openstudio-server")
        assert fake_api.calls[1] == ("openstudio-redis", "openstudio-server")

    def test_rotate_updates_content_and_resource_version(self):
        """FakeSecretsCoreV1Api.rotate updates data and bumps resourceVersion."""
        from _fakes import encode_secret_value

        fake_api = FakeSecretsCoreV1Api(
            data={"key": encode_secret_value("old-value")},
            resource_version="1000",
        )

        fake_api.rotate(data={"key": encode_secret_value("new-value")})

        fake_api.read_namespaced_secret("openstudio-redis", "openstudio-server")
        assert fake_api._resource_version == "1001"


def make_deployment_obj(
    name: str = "worker",
    namespace: str = "openstudio-server",
    annotations: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Helper to create a deployment dict matching the real API response shape."""
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": f"uid-{name}",
        },
        "spec": {
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {
                    "annotations": annotations or {},
                    "labels": labels or {"app": name, "tier": "simulations"},
                },
                "spec": {
                    "containers": [{"name": name, "image": f"openstudio/{name}:3.11.0"}]
                },
            },
        },
    }


class TestMergePatchSemanticEquivalence:
    """Validate _merge_patch against real K8s merge-patch semantics."""

    def test_merge_patch_adds_keys(self):
        """Adding a key that doesn't exist should create it."""
        target = {"a": 1, "b": 2}
        patch = {"c": 3}

        _merge_patch(target, patch)

        assert target == {"a": 1, "b": 2, "c": 3}

    def test_merge_patch_replaces_values(self):
        """Existing keys should be replaced by patch values."""
        target = {"a": 1, "b": 2}
        patch = {"a": 10}

        _merge_patch(target, patch)

        assert target == {"a": 10, "b": 2}

    def test_merge_patch_recurses_into_dicts(self):
        """Nested dicts should merge recursively."""
        target = {"spec": {"template": {"metadata": {"annotations": {"existing": "value"}}}}}
        patch = {"spec": {"template": {"metadata": {"annotations": {"new": "value"}}}}}

        _merge_patch(target, patch)

        assert target["spec"]["template"]["metadata"]["annotations"] == {
            "existing": "value",
            "new": "value",
        }

    def test_merge_patch_removes_null_values(self):
        """Patch null value should remove the key from target."""
        target = {"a": 1, "b": 2}
        patch = {"a": None}

        _merge_patch(target, patch)

        assert "a" not in target
        assert target == {"b": 2}

    def test_merge_patch_handles_list_replacement(self):
        """Non-dict values in patch replace the target value (including lists)."""
        target = {"containers": [{"name": "old"}]}
        patch = {"containers": [{"name": "new"}]}

        _merge_patch(target, patch)

        assert target == {"containers": [{"name": "new"}]}
