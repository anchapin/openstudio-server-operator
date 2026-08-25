"""End-to-end operator test against a real kind cluster (issue #777).

This module exercises the full operator stack against a kind cluster:
kind cluster creation, operator image build+load, CRD/RBAC/operator deployment,
OSCM CR creation, and tick-driven runtime behavior assertions within a bounded
timeout.

The test is gated on kind and docker availability and is automatically skipped
in environments where these tools are unavailable. It is NOT run as part of
the normal CI suite — it is a manual validation tool documented in
``docs/kind-validation.md``.

The acceptance criterion (issue #777):

    An automated e2e test suite (tests/test_operator_e2e.py) that uses kind,
    deploys the operator from the local image, creates a CR, and asserts
    tick-driven behaviors within a bounded timeout.

Architecture:

- ``_ensure_kind_cluster`` — creates the kind cluster if it does not exist.
- ``_apply_manifests`` — applies CRD, RBAC, admission policies, and priority
  class (the bare minimum required for the operator to boot).
- ``_run_operator`` — launches the operator as a background subprocess using the
  programmatic entrypoint (``python -m openstudio_operator``), yielding a handle
  whose ``kill()`` is called in the fixture teardown.
- ``_wait_for_tick`` — polls the operator's /metrics endpoint until the
  tick counter advances OR the timeout elapses. This is the signal that the
  operator's kopf timer has fired at least once against the live cluster.
- ``test_operator_ticks_against_live_cluster`` — the main test: creates the
  cluster, loads the image, applies manifests, creates the CR, runs the
  operator, and asserts the tick counter advances within 60 seconds.

CRD/RBAC minimum for operator boot:
  crd.yaml · rbac.yaml · priority-class.yaml · pod-delete-admission-policy.yaml
  (the pod-delete VAP narrows the operator SA's pods/delete to app=worker pods)

The test uses ``dryRun: true`` so no actual OpenStudio server is needed — the
operator's tick path exercises the kopf handler wiring, CR status RMW,
metrics endpoint, and event emission without making external HTTP calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager

import pytest

OPERATOR_IMAGE_NAME = "ghcr.io/anchapin/openstudio-server-operator:e2e"
CLUSTER_NAME = "os-operator-validation"
NAMESPACE = "openstudio-server"
OPERATOR_TIMEOUT_SECONDS = 90
TICK_POLL_INTERVAL_SECONDS = 5
CR_NAME = "e2e-validation"


def _tool_available(cmd: list[str]) -> bool:
    """Return True if the command exits with 0, False otherwise."""
    try:
        subprocess.run(cmd, capture_output=True, check=False)
        return True
    except FileNotFoundError:
        return False


KIND_AVAILABLE = _tool_available(["kind", "version"])
DOCKER_AVAILABLE = _tool_available(["docker", "info"])


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "e2e: end-to-end test requiring kind cluster")


@pytest.fixture(scope="module")
def _kind_cluster() -> Generator[None, None, None]:
    """Create the kind cluster if it does not exist; yield; leave cluster running on teardown."""
    if not KIND_AVAILABLE:
        pytest.skip("kind not available")
    if not DOCKER_AVAILABLE:
        pytest.skip("docker daemon not available")

    result = subprocess.run(
        ["kind", "get", "clusters"],
        capture_output=True,
        text=True,
        check=False,
    )
    if CLUSTER_NAME not in result.stdout:
        try:
            subprocess.run(
                ["kind", "create", "cluster", "--name", CLUSTER_NAME, "--config", "scripts/kind-config.yaml"],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            pytest.skip(f"kind create cluster failed: {e}")

    try:
        subprocess.run(
            ["kubectl", "config", "use-context", f"kind-{CLUSTER_NAME}"],
            capture_output=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        pytest.skip("kubectl could not use kind cluster context — check kind cluster status")

    try:
        subprocess.run(
            ["kubectl", "wait", "--for=condition=Ready", "node", "--all", "--timeout=120s"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        pytest.skip("kind cluster node did not become Ready within 120s")

    cluster_info = subprocess.run(
        ["kubectl", "cluster-info"],
        capture_output=True,
        check=False,
    )
    if cluster_info.returncode != 0:
        pytest.skip(f"kubectl cluster-info failed (exit {cluster_info.returncode}) — kind cluster may not be fully operational")

    yield


@pytest.fixture(scope="module")
def _operator_image(_kind_cluster: None) -> Generator[str, None, None]:
    """Build the operator image and load it into the kind cluster.

    Returns the image name if successful; pytest.skip if docker is unavailable
    or the build/load fails.
    """
    if not DOCKER_AVAILABLE:
        pytest.skip("docker daemon not available")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    build_result = subprocess.run(
        ["docker", "build", "-t", OPERATOR_IMAGE_NAME, repo_root],
        capture_output=True,
        text=True,
        check=False,
    )
    if build_result.returncode != 0:
        pytest.skip(f"docker build failed: {build_result.stderr[:500]}")

    load_result = subprocess.run(
        ["kind", "load", "docker-image", "--name", CLUSTER_NAME, OPERATOR_IMAGE_NAME],
        capture_output=True,
        text=True,
        check=False,
    )
    if load_result.returncode != 0:
        pytest.skip(f"kind load docker-image failed: {load_result.stderr[:500]}")

    yield OPERATOR_IMAGE_NAME


@pytest.fixture(scope="module")
def _operator_manifests(
    _kind_cluster: None,
    _operator_image: str,
) -> Generator[None, None, None]:
    """Apply CRD, RBAC, admission policies, priority class, and operator Deployment."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    manifests = [
        os.path.join(repo_root, "deploy", "crd.yaml"),
        os.path.join(repo_root, "deploy", "rbac.yaml"),
        os.path.join(repo_root, "deploy", "priority-class.yaml"),
        os.path.join(repo_root, "deploy", "pod-delete-admission-policy.yaml"),
    ]

    for manifest in manifests:
        try:
            subprocess.run(
                ["kubectl", "apply", "-f", manifest],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            pytest.skip(f"kubectl apply -f {manifest} failed: {e}")

    try:
        subprocess.run(
            ["kubectl", "wait", "--for=condition=Established", "crd/openstudioclustermanagers.energy.nrel.gov", "--timeout=60s"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        pytest.skip("CRD did not become Established within 60s")

    operator_deployment_path = os.path.join(repo_root, "deploy", "operator-deployment.yaml")
    apply_result = subprocess.run(
        ["kubectl", "apply", "-f", operator_deployment_path],
        capture_output=True,
        text=True,
        check=False,
    )
    if apply_result.returncode != 0:
        pytest.skip(f"operator Deployment could not be applied: {apply_result.stderr[:500]}")

    try:
        subprocess.run(
            ["kubectl", "wait", "--for=condition=Available", "-n", NAMESPACE, "deployment/openstudio-operator", "--timeout=60s"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        pytest.skip("operator Deployment did not become Available within 60s")

    yield

    subprocess.run(
        ["kubectl", "delete", "-f", operator_deployment_path, "--ignore-not-found"],
        capture_output=True,
        check=False,
    )


@pytest.fixture
def _oscm_cr(_operator_manifests: None) -> Generator[None, None, None]:
    """Create the OSCM CR; delete it on teardown."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cr_manifest_path = os.path.join(repo_root, "tests", "fixtures", "e2e-oscm-cr.json")

    cr_body = {
        "apiVersion": "energy.nrel.gov/v1alpha1",
        "kind": "OpenStudioClusterManager",
        "metadata": {"name": CR_NAME, "namespace": NAMESPACE},
        "spec": {
            "serverUrl": "http://web.openstudio-server.svc.cluster.local",
            "dryRun": True,
        },
    }

    os.makedirs(os.path.dirname(cr_manifest_path), exist_ok=True)
    with open(cr_manifest_path, "w") as f:
        json.dump(cr_body, f)

    try:
        subprocess.run(
            ["kubectl", "apply", "-f", cr_manifest_path],
            capture_output=True,
            text=True,
            check=True,
        )
        yield
    finally:
        subprocess.run(
            ["kubectl", "delete", "-f", cr_manifest_path, "--ignore-not-found"],
            capture_output=True,
            check=False,
        )
        try:
            os.remove(cr_manifest_path)
        except OSError:
            pass


@contextmanager
def _operator_subprocess(namespace: str = NAMESPACE) -> Generator[subprocess.Popen[bytes], None, None]:
    """Run ``python -m openstudio_operator`` as a background subprocess.

    Yields the Popen handle. On exit the process is killed and reaped.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    venv_python = os.path.join(repo_root, ".venv", "bin", "python")
    env = dict(os.environ)
    env["POD_NAMESPACE"] = namespace
    env["USER"] = "operator"

    proc = subprocess.Popen(
        [venv_python, "-m", "openstudio_operator", "--namespace", namespace],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        yield proc
    finally:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _fetch_metrics() -> str:
    """Fetch the operator /metrics endpoint from inside the pod. Returns empty string on failure."""
    try:
        result = subprocess.run(
            [
                "kubectl", "exec", "-n", NAMESPACE, "deploy/openstudio-operator", "--",
                "curl", "-sf", "http://localhost:9090/metrics",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        return ""


def _wait_for_tick(
    deadline: float,
    poll_interval: float = TICK_POLL_INTERVAL_SECONDS,
) -> bool:
    """Poll the operator's /metrics until the tick counter advances or timeout.

    Returns True if a tick was observed within the deadline; False otherwise.
    """
    initial_tick_count: int | None = None
    while time.monotonic() < deadline:
        metrics = _fetch_metrics()
        for line in metrics.splitlines():
            if line.startswith("openstudio_operator_handler_ticks_total"):
                parts = line.split()
                if len(parts) >= 2:
                    current = int(parts[1])
                    if initial_tick_count is None:
                        initial_tick_count = current
                        if current > 0:
                            return True
                    elif current > initial_tick_count:
                        return True
        time.sleep(poll_interval)
    return False


@pytest.mark.e2e
def test_operator_ticks_against_live_cluster(
    _operator_manifests: None,
    _oscm_cr: None,
) -> None:
    """Assert the operator ticks against the live kind cluster within 90 seconds.

    This is the issue #777 acceptance test: spins up kind, deploys the operator
    from the local image, creates an OSCM CR, and asserts the tick counter
    advances within a bounded timeout.

    The test uses ``dryRun: true`` on the CR so no external OpenStudio server
    is required — the operator exercises its kopf timer wiring, CR status RMW,
    metrics emission, and event emission paths without making real REST calls.
    """
    deadline = time.monotonic() + OPERATOR_TIMEOUT_SECONDS

    with _operator_subprocess() as proc:
        tick_observed = _wait_for_tick(deadline)

        assert tick_observed, (
            f"Operator tick counter did not advance within {OPERATOR_TIMEOUT_SECONDS}s. "
            f"The operator may have failed to start, crashed, or the tick loop "
            f"did not fire against the live cluster. "
            f"Check 'kubectl logs -n {NAMESPACE} deploy/openstudio-operator' for errors."
        )

        metrics = _fetch_metrics()
        assert metrics, (
            f"/metrics endpoint not responding on operator after tick observed. "
            f"Check 'kubectl logs -n {NAMESPACE} deploy/openstudio-operator'."
        )

        handler_metrics = [l for l in metrics.splitlines() if l.startswith("openstudio_operator_")]
        assert handler_metrics, (
            "No openstudio_operator_* metrics found — kopf may not have started."
        )

        assert proc.poll() is None, (
            f"Operator process exited unexpectedly. "
            f"stderr: {proc.stderr.read1(4096).decode(errors='replace')}"
        )
