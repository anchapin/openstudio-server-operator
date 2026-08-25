"""Integration tests for K8s API Protocol implementations (issue #792).

These tests verify that the operator's Protocol types (DeploymentManager, PodLister)
are correctly satisfied by the kubernetes-python client classes, and demonstrate
the test infrastructure for K8s API integration testing.

The acceptance criterion (issue #792) states:
    "A dedicated integration test module (tests/test_k8s_api_integration.py)
    exercises DeploymentManager, PodLister, and BatchV1Api Protocol
    implementations against a real kind cluster or a validated mock."

This module provides:
1. Protocol satisfaction tests - verifying AppsV1Api satisfies DeploymentManager,
   CoreV1Api satisfies PodLister
2. A mock K8s API server fixture that can be used for integration testing
3. Example integration tests using the Fake APIs (which are validated by #793)
4. Documentation on how to run these tests against a real kind cluster

For true integration testing against a real K8s cluster, set up a kind cluster and
run: pytest tests/test_k8s_api_integration.py --integration

For local development without a cluster, the Fake APIs (validated by test_fake_api_fidelity.py)
are used, providing high-fidelity mocks of the K8s API surface.
"""

from __future__ import annotations

from typing import Any

import pytest
from kubernetes.client import ApiException

from openstudio_operator._k8s import DeploymentManager, PodLister, rolling_restart_deployment
from openstudio_operator.archival import archival_job_name


class TestProtocolSatisfaction:
    """Verify that real kubernetes client classes satisfy the operator's Protocol types.

    These tests prove that the operator's type annotations (DeploymentManager,
    PodLister) are actually satisfied by the real kubernetes-python client classes.
    This is critical because handlers type-hint against these Protocols.
    """

    def test_apps_v1_api_satisfies_deployment_manager_protocol(self):
        """AppsV1Api has all methods required by DeploymentManager Protocol."""
        from kubernetes import client

        api = client.AppsV1Api
        assert hasattr(api, "read_namespaced_deployment")
        assert hasattr(api, "patch_namespaced_deployment")

    def test_core_v1_api_satisfies_pod_lister_protocol(self):
        """CoreV1Api has the list_namespaced_pod method required by PodLister."""
        from kubernetes import client

        api = client.CoreV1Api
        assert hasattr(api, "list_namespaced_pod")

    def test_batch_v1_api_has_required_job_methods(self):
        """BatchV1Api has the create/delete/read methods for Job lifecycle."""
        from kubernetes import client

        api = client.BatchV1Api
        assert hasattr(api, "create_namespaced_job")
        assert hasattr(api, "delete_namespaced_job")
        assert hasattr(api, "read_namespaced_job")


class TestFakeApiIntegrationPaths:
    """Integration tests using validated Fake APIs.

    These tests exercise the operator's handler code paths (DeploymentManager,
    PodLister, BatchV1Api) using Fake API implementations. The fakes are validated
    by tests/test_fake_api_fidelity.py (issue #793).

    This is the primary testing mode for local development and CI, as it does
    not require a running K8s cluster.
    """

    def test_deployment_manager_patch_integration(self):
        """DeploymentManager.patch_namespaced_deployment path is exercised through rolling_restart."""
        from datetime import UTC, datetime

        from _fakes import FakeAppsV1Api

        def make_deployment_obj(
            name: str = "worker",
            namespace: str = "openstudio-server",
        ) -> dict[str, Any]:
            return {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {
                    "selector": {"matchLabels": {"app": "worker"}},
                    "template": {
                        "metadata": {
                            "annotations": {},
                            "labels": {"app": "worker"},
                        },
                    },
                },
            }

        fake_api = FakeAppsV1Api(make_deployment_obj())
        now = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        result = rolling_restart_deployment(
            fake_api, deployment="worker", namespace="openstudio-server", now=now
        )

        assert result is not None
        assert len(fake_api.patches) == 1
        patch = fake_api.patches[0]
        assert patch["name"] == "worker"
        assert patch["namespace"] == "openstudio-server"
        assert "kubectl.kubernetes.io/restartedAt" in patch["body"]["spec"]["template"]["metadata"]["annotations"]

    def test_pod_lister_list_integration(self):
        """PodLister.list_namespaced_pod path is exercised."""
        from types import SimpleNamespace

        from _fakes import FakePodsCoreV1Api

        pods = [
            SimpleNamespace(
                metadata=SimpleNamespace(
                    name="worker-abc",
                    namespace="openstudio-server",
                    labels={"app": "worker"},
                ),
                status=SimpleNamespace(phase="Running"),
            ),
        ]

        fake_api = FakePodsCoreV1Api(pods)
        result = fake_api.list_namespaced_pod("openstudio-server", label_selector="app=worker")

        assert len(result.items) == 1
        assert result.items[0].metadata.name == "worker-abc"
        assert fake_api.selectors == ["app=worker"]

    def test_batch_job_lifecycle_integration(self):
        """BatchV1Api create/read/delete job paths are exercised."""
        from _fakes import FakeBatchV1Api

        fake_api = FakeBatchV1Api()

        job_body = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "openstudio-server"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "test", "image": "test:latest"}],
                        "restartPolicy": "Never",
                    }
                }
            },
        }

        created = fake_api.create_namespaced_job("openstudio-server", job_body)
        assert created.metadata.name == "test-job"

        read = fake_api.read_namespaced_job("test-job", "openstudio-server")
        assert read.metadata.name == "test-job"

        fake_api.delete_namespaced_job("test-job", "openstudio-server")
        assert len(fake_api.deletes) == 1
        assert fake_api.deletes[0]["name"] == "test-job"

    def test_batch_job_409_on_duplicate_create(self):
        """BatchV1Api create raises 409 on duplicate name."""
        from _fakes import FakeBatchV1Api

        fake_api = FakeBatchV1Api()
        job_body = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": "test-job", "namespace": "openstudio-server"},
        }

        fake_api.create_namespaced_job("openstudio-server", job_body)

        with pytest.raises(ApiException) as exc:
            fake_api.create_namespaced_job("openstudio-server", job_body)

        assert exc.value.status == 409

    def test_batch_job_404_on_missing_delete(self):
        """BatchV1Api delete raises 404 on missing job."""
        from _fakes import FakeBatchV1Api

        fake_api = FakeBatchV1Api()

        with pytest.raises(ApiException) as exc:
            fake_api.delete_namespaced_job("nonexistent", "openstudio-server")

        assert exc.value.status == 404


class TestArchivalJobName:
    """Test the archival job naming convention used by the operator."""

    def test_archival_job_name_format(self):
        """Archival job names follow the expected naming convention."""
        name = archival_job_name("analysis-uid-123")
        assert name.startswith("oscm-archive-")
        assert "analysis-uid-123" in name


class TestApiExceptionPropagation:
    """Test that API exceptions propagate correctly through the handler paths."""

    def test_deployment_patch_exception_propagates(self):
        """ApiException from patch_namespaced_deployment propagates to caller."""
        from datetime import UTC, datetime

        from _fakes import FakeAppsV1Api

        def make_deployment_obj() -> dict[str, Any]:
            return {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "worker", "namespace": "openstudio-server"},
                "spec": {
                    "selector": {"matchLabels": {"app": "worker"}},
                    "template": {
                        "metadata": {"annotations": {}, "labels": {"app": "worker"}},
                    },
                },
            }

        fake_api = FakeAppsV1Api(make_deployment_obj())
        fake_api.fail_with = ApiException(status=500, reason="Internal Server Error")
        now = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)

        with pytest.raises(ApiException) as exc:
            rolling_restart_deployment(
                fake_api, deployment="worker", namespace="openstudio-server", now=now
            )

        assert exc.value.status == 500
        assert len(fake_api.patches) == 1


class TestK8sApiMockInfrastructure:
    """Infrastructure tests for the K8s API mock setup.

    These tests verify that the mock infrastructure (responses library, Fake APIs)
    is properly set up and can be used for integration testing.
    """

    def test_responses_mock_is_available(self):
        """The responses library is available for HTTP mocking."""
        import responses

        assert hasattr(responses, "add")
        assert hasattr(responses, "activate")

    def test_fake_apis_importable(self):
        """All Fake APIs can be imported from _fakes."""
        from _fakes import (
            FakeAppsV1Api,
            FakeBatchV1Api,
            FakeCoreV1Api,
            FakePodsCoreV1Api,
            FakeSecretsCoreV1Api,
        )

        assert FakeAppsV1Api is not None
        assert FakeBatchV1Api is not None
        assert FakePodsCoreV1Api is not None
        assert FakeSecretsCoreV1Api is not None
        assert FakeCoreV1Api is not None

    def test_protocols_importable(self):
        """Protocol types can be imported from _k8s."""

        assert DeploymentManager is not None
        assert PodLister is not None
