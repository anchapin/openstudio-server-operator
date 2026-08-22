"""Issue #478 — the prune CronJob's /metrics endpoint honors the same
optional bearer-token authN as the operator's (#401).

``prune_entrypoint.main()`` calls the shared
``openstudio_operator.metrics.start_metrics_server()`` with no explicit
``token_file``, so the ``OPENSTUDIO_METRICS_TOKEN_FILE`` env var the
CronJob manifest now ships (empty-by-default) is the pruner's config
surface for the fail-closed bearer gate. These tests pin that wiring at
the ENTRYPOINT level (not just ``metrics.py`` — see ``test_metrics_auth.py``
for the operator-side equivalents): if a future refactor grows a second
metrics-server call site inside the prune path, or passes an explicit
``token_file=`` that bypasses the env var, these fail.

Each test runs the entrypoint in a subprocess (the same pattern as
``test_metrics_auth.py`` / ``test_metrics_endpoint.py``):
``start_metrics_server`` is process-idempotent, and the pytest process
itself may already hold port 9090 (the prune-entrypoint unit tests call
``main()`` in-process), so the spy redirects the real bind to an
ephemeral localhost port — the same redirect as
``test_metrics_endpoint.py::test_handlers_import_starts_metrics_server``.
``POD_NAMESPACE`` is popped so ``main()`` takes the exit-2 wiring branch
AFTER starting the metrics server — no K8s clients are constructed.
"""

import os
import subprocess
import sys
import tempfile
import textwrap

#: Shared child preamble: spy-patch ``prune_entrypoint.start_metrics_server``
#: (the module imported the name by-value, so the patch must land on the
#: prune module's attribute, not on ``metrics``), run ``main()``, and assert
#: the call shape. Binds an ephemeral 127.0.0.1 port; leaves ``base`` for
#: the per-test assertions.
_PREAMBLE = """
import requests
import openstudio_operator.metrics as m
import openstudio_operator.prune_entrypoint as pe

real = m.start_metrics_server
calls = []
ports = []

def spy(*args, **kwargs):
    calls.append((args, kwargs))
    ports.append(real(port=0, addr="127.0.0.1"))
    return ports[-1]

pe.start_metrics_server = spy

code = pe.main()  # no POD_NAMESPACE -> exit-2 wiring branch, AFTER the bind
assert code == 2, code
assert ports, "prune_entrypoint.main did not invoke start_metrics_server"
assert calls == [((), {})], (
    "prune_entrypoint.main must call start_metrics_server with NO explicit "
    "token_file/port args — OPENSTUDIO_METRICS_TOKEN_FILE is the config "
    f"surface the CronJob manifest ships (#478); got {calls!r}"
)
base = f"http://127.0.0.1:{ports[0]}"
"""


def _run_prune_child(
    code: str, env_extra: dict[str, str] | None = None
) -> None:
    """Run ``python -c code``; fail the test if the child asserts."""
    env = os.environ.copy()
    # A token env var leaking in from the harness would silently flip the
    # open-plaintext test into auth mode — pop both unless a test sets
    # them. POD_NAMESPACE must be unset so main() exits at the wiring
    # check before any K8s client construction.
    env.pop("OPENSTUDIO_METRICS_TOKEN_FILE", None)
    env.pop("POD_NAMESPACE", None)
    if env_extra:
        env.update(env_extra)
    subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(_PREAMBLE) + textwrap.dedent(code),
        ],
        check=True,
        env=env,
    )


def test_prune_entrypoint_token_env_missing_or_wrong_token_returns_401():
    """Acceptance (#478): with ``OPENSTUDIO_METRICS_TOKEN_FILE`` set on the
    prune entrypoint, a request with NO token and a request with a WRONG
    token both return 401 — the 401 body must not leak any exposition."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".token", delete=False
    ) as handle:
        handle.write("prune-sekrit\n")  # trailing newline: kubectl bakes it in
        token_path = handle.name
    try:
        _run_prune_child(
            """
            response = requests.get(f"{base}/metrics", timeout=5)
            assert response.status_code == 401, response.status_code
            assert "# TYPE" not in response.text, "401 body must not leak exposition"
            assert response.headers.get("WWW-Authenticate", "").startswith("Bearer")

            response = requests.get(
                f"{base}/metrics", headers={"Authorization": "Bearer wrong"}, timeout=5
            )
            assert response.status_code == 401, response.status_code
            assert "# TYPE" not in response.text
            """,
            env_extra={"OPENSTUDIO_METRICS_TOKEN_FILE": token_path},
        )
    finally:
        os.unlink(token_path)


def test_prune_entrypoint_token_env_valid_token_returns_200():
    """Acceptance (#478): a valid ``Authorization: Bearer <token>`` header
    against the prune entrypoint's endpoint returns 200 with the full
    exposition; ``/healthz`` returns a bare 200 so any probe has a
    Bearer-free target when auth is enabled."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".token", delete=False
    ) as handle:
        handle.write("prune-sekrit\n")
        token_path = handle.name
    try:
        _run_prune_child(
            """
            response = requests.get(
                f"{base}/metrics",
                headers={"Authorization": "Bearer prune-sekrit"},
                timeout=5,
            )
            assert response.status_code == 200, response.status_code
            assert "# TYPE " in response.text

            response = requests.get(f"{base}/healthz", timeout=5)
            assert response.status_code == 200, response.status_code
            assert response.content == b"ok\\n"
            assert "# TYPE" not in response.text
            """,
            env_extra={"OPENSTUDIO_METRICS_TOKEN_FILE": token_path},
        )
    finally:
        os.unlink(token_path)


def test_prune_entrypoint_token_env_unset_open_plaintext():
    """Acceptance (#478): the env var unset = the documented open-plaintext
    default (the NetworkPolicy is then the only gate) — a bare GET serves
    the exposition, and a stray Authorization header changes nothing."""
    _run_prune_child(
        """
        response = requests.get(f"{base}/metrics", timeout=5)
        assert response.status_code == 200, response.status_code
        assert "# TYPE " in response.text

        response = requests.get(
            f"{base}/metrics", headers={"Authorization": "Bearer whatever"}, timeout=5
        )
        assert response.status_code == 200, response.status_code
        assert "# TYPE " in response.text
        """
    )
