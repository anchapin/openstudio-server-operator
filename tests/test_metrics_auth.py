"""Issue #401 — optional bearer-token authN on the /metrics endpoint.

Each test runs the server in a subprocess (the same pattern as
``test_metrics_endpoint.py::test_handlers_import_starts_metrics_server``):
``start_metrics_server`` is process-idempotent (the first call binds; later
calls are reads), so auth-mode and open-plaintext-mode servers cannot coexist
in one pytest process. The child asserts against the live endpoint with
``requests``; ``check=True`` surfaces any child assertion failure as a
parent-side test failure with the child traceback.
"""

import os
import subprocess
import sys
import tempfile
import textwrap


def _run_child(code: str, env_extra: dict[str, str] | None = None) -> None:
    """Run ``python -c code``; fail the test if the child asserts."""
    env = os.environ.copy()
    # A token env var leaking in from the harness would silently flip the
    # open-plaintext tests into auth mode — pop it unless a test sets it.
    env.pop("OPENSTUDIO_METRICS_TOKEN_FILE", None)
    if env_extra:
        env.update(env_extra)
    subprocess.run([sys.executable, "-c", textwrap.dedent(code)], check=True, env=env)


def test_metrics_bearer_token_missing_or_invalid_returns_401():
    """Acceptance: with a token file configured, a request with NO token and a
    request with a WRONG token both return 401 — and the 401 body must not
    leak any exposition. Alternate paths must not bypass the gate (the bare
    prometheus_client app serves metrics for ANY path; the auth wrapper 404s
    them)."""
    _run_child(
        """
        import tempfile
        import requests
        from openstudio_operator.metrics import start_metrics_server

        with tempfile.NamedTemporaryFile(
            "w", suffix=".token", delete=False
        ) as handle:
            handle.write("sekrit-token\\n")  # trailing newline: kubectl bakes it in
            token_path = handle.name
        port = start_metrics_server(port=0, addr="127.0.0.1", token_file=token_path)
        assert port is not None and port > 0, port
        base = f"http://127.0.0.1:{port}"

        response = requests.get(f"{base}/metrics", timeout=5)
        assert response.status_code == 401, response.status_code
        assert "# TYPE" not in response.text, "401 body must not leak exposition"
        assert response.headers.get("WWW-Authenticate", "").startswith("Bearer")

        response = requests.get(
            f"{base}/metrics", headers={"Authorization": "Bearer wrong"}, timeout=5
        )
        assert response.status_code == 401, response.status_code
        assert "# TYPE" not in response.text

        # No alternate-path leak: /, arbitrary paths, /metrics/ (trailing
        # slash) — none of them serve the exposition around the gate.
        for path in ("/", "/anything", "/metrics/"):
            response = requests.get(f"{base}{path}", timeout=5)
            assert response.status_code == 404, (path, response.status_code)
            assert "# TYPE" not in response.text
        """
    )


def test_metrics_bearer_token_valid_returns_200():
    """Acceptance: a valid ``Authorization: Bearer <token>`` header returns
    200 with the full exposition (token file carries a trailing newline —
    ``strip()`` must absorb it). ``/healthz`` returns a bare 200 so the
    kubelet probes have a Bearer-free target when auth is enabled."""
    _run_child(
        """
        import tempfile
        import requests
        from openstudio_operator.metrics import start_metrics_server

        with tempfile.NamedTemporaryFile("w", suffix=".token", delete=False) as handle:
            handle.write("sekrit-token\\n")
            token_path = handle.name
        port = start_metrics_server(port=0, addr="127.0.0.1", token_file=token_path)
        assert port is not None and port > 0, port
        base = f"http://127.0.0.1:{port}"

        response = requests.get(
            f"{base}/metrics",
            headers={"Authorization": "Bearer sekrit-token"},
            timeout=5,
        )
        assert response.status_code == 200, response.status_code
        assert "# TYPE " in response.text

        # Probe path: 200, no auth, no exposition data.
        response = requests.get(f"{base}/healthz", timeout=5)
        assert response.status_code == 200, response.status_code
        assert response.content == b"ok\\n"
        assert "# TYPE" not in response.text
        """
    )


def test_metrics_no_token_configured_open_plaintext():
    """Acceptance: no token file (arg + env both unset) = the pre-#401 open
    plaintext behavior — a bare GET serves the exposition, and a stray
    Authorization header changes nothing (no auth layer is active)."""
    _run_child(
        """
        import requests
        from openstudio_operator.metrics import start_metrics_server

        port = start_metrics_server(port=0, addr="127.0.0.1")
        assert port is not None and port > 0, port
        base = f"http://127.0.0.1:{port}"

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


def test_metrics_token_file_env_var_honored():
    """The env var is the deployment-facing config surface (the manifest
    ships ``OPENSTUDIO_METRICS_TOKEN_FILE`` with an empty default): a
    ``start_metrics_server()`` call with NO explicit token_file must pick
    it up from the environment."""
    with tempfile.NamedTemporaryFile("w", suffix=".token", delete=False) as handle:
        handle.write("env-token\n")
        token_path = handle.name
    try:
        _run_child(
            """
            import requests
            from openstudio_operator.metrics import start_metrics_server

            port = start_metrics_server(port=0, addr="127.0.0.1")
            assert port is not None and port > 0, port
            base = f"http://127.0.0.1:{port}"

            response = requests.get(f"{base}/metrics", timeout=5)
            assert response.status_code == 401, response.status_code
            response = requests.get(
                f"{base}/metrics",
                headers={"Authorization": "Bearer env-token"},
                timeout=5,
            )
            assert response.status_code == 200, response.status_code
            assert "# TYPE " in response.text
            """,
            env_extra={"OPENSTUDIO_METRICS_TOKEN_FILE": token_path},
        )
    finally:
        os.unlink(token_path)


def test_metrics_token_rotation_and_fail_closed():
    """The token file is re-read per request: rotating the Secret's content
    flips auth to the new token with NO restart, and deleting the file
    (unreadable volume) fails CLOSED — every request 401s; the endpoint
    never falls back to open plaintext."""
    _run_child(
        """
        import os
        import tempfile
        import requests
        from openstudio_operator.metrics import start_metrics_server

        with tempfile.NamedTemporaryFile("w", suffix=".token", delete=False) as handle:
            handle.write("token-a\\n")
            token_path = handle.name
        port = start_metrics_server(port=0, addr="127.0.0.1", token_file=token_path)
        assert port is not None and port > 0, port
        base = f"http://127.0.0.1:{port}"

        def get(token):
            return requests.get(
                f"{base}/metrics",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )

        assert get("token-a").status_code == 200
        with open(token_path, "w") as handle:
            handle.write("token-b\\n")
        assert get("token-a").status_code == 401  # old token revoked
        assert get("token-b").status_code == 200  # new token live
        os.unlink(token_path)
        assert get("token-b").status_code == 401  # fail closed, not open
        """
    )
