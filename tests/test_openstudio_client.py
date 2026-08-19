"""Unit tests for OpenStudioClient against the verified v3.11.0 REST contract."""

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests
import responses
from responses import matchers

from openstudio_operator.openstudio_client import (
    OpenStudioApiError,
    OpenStudioClient,
    _parse_timestamp,
)

BASE = "http://web.test"


@pytest.fixture()
def client():
    return OpenStudioClient(BASE)


@pytest.fixture()
def sleeps(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr("openstudio_operator.openstudio_client._sleep", recorded.append)
    return recorded


# --- Timestamp boundary (D12) ------------------------------------------


def test_parse_timestamp_converts_zone_offset_to_utc():
    assert _parse_timestamp("2026-08-18T08:00:00-06:00") == datetime(
        2026, 8, 18, 14, 0, 0, tzinfo=UTC
    )


def test_parse_timestamp_accepts_z_suffix():
    assert _parse_timestamp("2026-08-18T12:34:56Z") == datetime(
        2026, 8, 18, 12, 34, 56, tzinfo=UTC
    )


def test_parse_timestamp_handles_fractional_seconds():
    parsed = _parse_timestamp("2026-08-18T12:34:56.789+00:00")
    assert parsed == datetime(2026, 8, 18, 12, 34, 56, 789000, tzinfo=UTC)


def test_parse_timestamp_naive_input_assumed_utc():
    parsed = _parse_timestamp("2026-08-18T12:34:56")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_parse_timestamp_invalid_raises_valueerror():
    with pytest.raises(ValueError, match="unparseable timestamp"):
        _parse_timestamp("not-a-timestamp")


# --- Reads --------------------------------------------------------------


@responses.activate
def test_list_analyses_returns_raw_docs_with_utc_timestamps(client):
    responses.get(
        f"{BASE}/analyses.json",
        json=[
            {
                "_id": "a1",
                "status": "started",
                "run_flag": True,
                "created_at": "2026-08-18T08:00:00-06:00",
                "updated_at": "2026-08-18T09:00:00Z",
            }
        ],
    )
    docs = client.list_analyses()
    assert docs[0]["status"] == "started"
    assert docs[0]["run_flag"] is True
    assert docs[0]["created_at"] == datetime(2026, 8, 18, 14, 0, 0, tzinfo=UTC)
    assert docs[0]["updated_at"] == datetime(2026, 8, 18, 9, 0, 0, tzinfo=UTC)
    assert responses.calls[0].request.method == "GET"


@responses.activate
def test_get_analysis_status_returns_singular_wrapper(client):
    """Issue #83 D1: ``/status.json`` is the new SLA clock anchor source.

    Live v3.11.0: the response wraps a single match as
    ``{analysis: {…}}``. The operator's ``_status_is_started`` helper
    reads this wrapper.
    """
    responses.get(
        f"{BASE}/analyses/a1/status.json",
        json={"analysis": {"_id": "a1", "id": "a1", "status": "started"}},
    )
    payload = client.get_analysis_status("a1")
    assert payload["analysis"]["status"] == "started"
    assert payload["analysis"]["_id"] == "a1"


@responses.activate
def test_get_analysis_status_returns_plural_wrapper(client):
    """Count-based wrapping (live-verified #66): zero or many matches →
    ``{analyses: [...]}``."""
    responses.get(
        f"{BASE}/analyses/a1/status.json",
        json={"analyses": [{"_id": "a1", "id": "a1", "status": "started"}]},
    )
    payload = client.get_analysis_status("a1")
    assert payload["analyses"][0]["status"] == "started"


@responses.activate
def test_get_analysis_status_normalizes_timestamps(client):
    """Timestamps in the status payload (e.g. ``run_at``) are tz-aware UTC."""
    responses.get(
        f"{BASE}/analyses/a1/status.json",
        json={
            "analysis": {
                "_id": "a1",
                "id": "a1",
                "status": "started",
                "run_flag": True,
                "jobs": [
                    {
                        "index": 0,
                        "analysis_type": "batch_run",
                        "status": "completed",
                        "status_message": "completed normal",
                    }
                ],
                "data_points": [
                    {
                        "_id": "dp1",
                        "id": "dp1",
                        "name": "dp",
                        "analysis_id": "a1",
                        "status": "started",
                        "status_message": "",
                        "run_start_time": "2026-08-18T08:00:00-06:00",
                    }
                ],
            }
        },
    )
    payload = client.get_analysis_status("a1")
    assert (
        payload["analysis"]["data_points"][0]["run_start_time"]
        == datetime(2026, 8, 18, 14, 0, 0, tzinfo=UTC)
    )


@responses.activate
def test_get_analysis_status_retries_5xx_then_succeeds(client, sleeps):
    """D12: status.json reads ride the same 3x retry/jitter envelope as
    every other REST method — the SLA tick's N+1 polls are protected."""
    responses.get(f"{BASE}/analyses/a1/status.json", status=503)
    responses.get(
        f"{BASE}/analyses/a1/status.json",
        json={"analysis": {"_id": "a1", "id": "a1", "status": "started"}},
    )
    payload = client.get_analysis_status("a1")
    assert payload["analysis"]["status"] == "started"
    assert len(responses.calls) == 2
    assert len(sleeps) == 1
    assert 0.5 <= sleeps[0] <= 1.5


@responses.activate
def test_list_started_datapoints_uses_status_and_jobs_params(client):
    responses.get(
        f"{BASE}/data_points/status",
        match=[matchers.query_param_matcher({"status": "1", "jobs": "started"})],
        json={
            "data_points": [
                {
                    "_id": "dp1",
                    "id": "dp1",
                    "analysis_id": "a1",
                    "status": "started",
                    "status_message": "started individual simulation",
                }
            ]
        },
    )
    dps = client.list_started_datapoints()
    assert dps == [
        {
            "_id": "dp1",
            "id": "dp1",
            "analysis_id": "a1",
            "status": "started",
            "status_message": "started individual simulation",
        }
    ]


@responses.activate
# --- Actions ------------------------------------------------------------


@responses.activate
def test_soft_stop_analysis_is_get(client):
    responses.get(f"{BASE}/analyses/a1/soft_stop", status=200, json={"result": "accepted"})
    assert client.soft_stop_analysis("a1") is None
    assert responses.calls[0].request.method == "GET"


@responses.activate
def test_stop_analysis_posts_analysis_action_param(client):
    responses.post(f"{BASE}/analyses/a1/action", status=200, json={"result": "accepted"})
    client.stop_analysis("a1")
    request = responses.calls[0].request
    assert request.method == "POST"
    assert request.body == "analysis_action=stop"
    assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")


@responses.activate
def test_requeue_datapoint_posts_and_expects_204(client):
    responses.post(f"{BASE}/data_points/dp1/requeue", status=204, body="")
    assert client.requeue_datapoint("dp1") is None
    assert responses.calls[0].request.method == "POST"


@responses.activate
def test_delete_analysis_uses_delete_verb(client):
    responses.delete(f"{BASE}/analyses/a1", status=204, body="")
    client.delete_analysis("a1")
    assert responses.calls[0].request.method == "DELETE"


# --- Session default Accept header (issue #227) -----------------------


@responses.activate
def test_all_requests_carry_accept_application_json_header(client):
    """Issue #227: every request must carry ``Accept: application/json``.

    The v3.11.0 contract (`docs/contracts/openstudio-server-v3.11.0-rest.md`)
    §4 documents that the mutating endpoints
    ``DELETE /analyses/{id}``, ``POST /analyses/{id}/action``, and
    ``POST /data_points/{id}/requeue`` only return 204/200 JSON with
    ``Accept: application/json``; without it they return 302 HTML
    (or 500 for the jobless-requeue edge case). Setting the header on
    the session is the single fix — no per-call override required.
    """
    responses.get(f"{BASE}/analyses.json", json=[])
    responses.get(f"{BASE}/analyses/a1/status.json", json={"analysis": {"status": "na"}})
    responses.get(f"{BASE}/data_points/status", json={"data_points": []})
    responses.get(f"{BASE}/analyses/a1/soft_stop", status=200, json={"result": "accepted"})
    responses.post(f"{BASE}/analyses/a1/action", status=200, json={"result": "accepted"})
    responses.post(f"{BASE}/data_points/dp1/requeue", status=204, body="")
    responses.delete(f"{BASE}/analyses/a1", status=204, body="")

    client.list_analyses()
    client.get_analysis_status("a1")
    client.list_started_datapoints()
    client.soft_stop_analysis("a1")
    client.stop_analysis("a1")
    client.requeue_datapoint("dp1")
    client.delete_analysis("a1")

    assert len(responses.calls) == 7
    for call in responses.calls:
        accept = call.request.headers.get("Accept")
        assert accept == "application/json", (
            f"{call.request.method} {call.request.url} sent Accept={accept!r}; "
            "OpenStudioClient must set a session-level "
            "Accept: application/json header (issue #227)"
        )


# --- Retry / backoff (D12) ----------------------------------------------


@responses.activate
def test_retries_5xx_then_succeeds(client, sleeps):
    responses.get(f"{BASE}/analyses.json", status=503)
    responses.get(f"{BASE}/analyses.json", status=200, json=[])
    assert client.list_analyses() == []
    assert len(responses.calls) == 2
    assert len(sleeps) == 1
    assert 0.5 <= sleeps[0] <= 1.5


@responses.activate
def test_retries_connection_error_then_succeeds(client, sleeps):
    responses.get(f"{BASE}/analyses.json", body=requests.exceptions.ConnectionError("boom"))
    responses.get(f"{BASE}/analyses.json", status=200, json=[])
    assert client.list_analyses() == []
    assert len(responses.calls) == 2
    assert len(sleeps) == 1


@responses.activate
def test_exhausts_retries_with_backoff_schedule_then_raises(client, sleeps):
    for _ in range(4):
        responses.get(f"{BASE}/analyses.json", status=500)
    with pytest.raises(OpenStudioApiError, match="failed after 4 attempts"):
        client.list_analyses()
    assert len(responses.calls) == 4
    assert len(sleeps) == 3
    assert 0.5 <= sleeps[0] <= 1.5
    assert 1.0 <= sleeps[1] <= 3.0
    assert 2.0 <= sleeps[2] <= 6.0


@responses.activate
def test_4xx_raises_immediately_without_retry(client, sleeps):
    responses.get(f"{BASE}/analyses/a1/status.json", status=404, body="{}")
    with pytest.raises(OpenStudioApiError, match="404"):
        client.get_analysis_status("a1")
    assert len(responses.calls) == 1
    assert sleeps == []


# --- Non-GET 5xx must NOT retry (issue #226) ---------------------------


@responses.activate
def test_post_5xx_does_not_retry(client, sleeps):
    """Issue #226: ``POST /data_points/{id}/requeue`` returning 504 must NOT be retried.

    RFC 9110 §9.2.2 makes POST non-idempotent by default, and the v3.11.0 contract only
    documents GET as safely-retryable. Re-firing on a 504 that follows a server-side
    commit would double-burn ``status.requeues[dp].count`` past ``maxAutoRequeues``. A
    single attempt is made; the 504 propagates immediately as ``OpenStudioApiError``.
    """
    responses.post(f"{BASE}/data_points/dp1/requeue", status=504, body="gateway timeout")
    with pytest.raises(OpenStudioApiError, match="504") as excinfo:
        client.requeue_datapoint("dp1")
    assert "non-GET verb is non-idempotent" in str(excinfo.value)
    assert len(responses.calls) == 1
    assert responses.calls[0].request.method == "POST"
    assert sleeps == []


@responses.activate
def test_delete_5xx_does_not_retry(client, sleeps):
    """Issue #226: ``DELETE /analyses/{id}`` returning 500 must NOT be retried either.

    DELETE is idempotent in practice (RFC 9110 §9.2.2), but a 504/500 mid-cascade is risky:
    the server may have partially executed ``before_destroy :queue_delete_files`` +
    ``data_points dependent: :destroy`` before the failure. Re-firing could leave the
    server in a state where the operator's accounting diverges from reality. Single
    attempt; the 5xx propagates immediately.
    """
    responses.delete(f"{BASE}/analyses/a1", status=500, body="internal error")
    with pytest.raises(OpenStudioApiError, match="500") as excinfo:
        client.delete_analysis("a1")
    assert "non-GET verb is non-idempotent" in str(excinfo.value)
    assert len(responses.calls) == 1
    assert responses.calls[0].request.method == "DELETE"
    assert sleeps == []


@responses.activate
def test_put_5xx_does_not_retry(client, sleeps):
    """Issue #226: PUT is also non-idempotent by default and must not retry on 5xx.

    The v3.11.0 contract does not expose a PUT route today, but the retry policy is
    verb-based; this locks the asymmetry for any future PUT-shaped action the operator
    might add.
    """
    responses.put(f"{BASE}/analyses/a1/action", status=503, body="unavailable")
    with pytest.raises(OpenStudioApiError, match="503"):
        client._request("PUT", "/analyses/a1/action")
    assert len(responses.calls) == 1
    assert responses.calls[0].request.method == "PUT"
    assert sleeps == []


@responses.activate
def test_get_5xx_still_retries(client, sleeps):
    """Issue #226 regression fence: GET retry behavior is preserved (D12 envelope).

    The change is verb-asymmetric — only the 5xx retry is dropped for non-GET. GET keeps
    its full 3x retry / jitter envelope; this test pins the asymmetry so a future
    "always retry" refactor cannot regress the SLA-clock polls that ride this path.
    """
    responses.get(f"{BASE}/analyses.json", status=502)
    responses.get(f"{BASE}/analyses.json", status=200, json=[])
    assert client.list_analyses() == []
    assert len(responses.calls) == 2
    assert len(sleeps) == 1


@responses.activate
def test_invalid_json_raises_api_error(client):
    responses.get(f"{BASE}/analyses.json", body="<html>gateway</html>", status=200)
    with pytest.raises(OpenStudioApiError, match="invalid JSON"):
        client.list_analyses()


@responses.activate
def test_unparseable_timestamp_in_doc_raises_api_error(client):
    responses.get(f"{BASE}/analyses.json", json=[{"_id": "a1", "created_at": "not-a-date"}])
    with pytest.raises(OpenStudioApiError, match="unparseable timestamp"):
        client.list_analyses()


# --- TLS verification (issue #242) -------------------------------------


def test_session_default_verify_is_pinned_true(monkeypatch):
    """Issue #242: ``verify=True`` must be set explicitly on the session.

    ``requests.Session()`` defaults to ``verify=True`` already, but the
    acceptance criterion pins it explicitly so a future ``Session()``
    subclass or transport swap cannot silently downgrade the operator's
    TLS posture (e.g. by inheriting ``verify=False`` from an
    ``HTTPAdapter`` mount).
    """
    monkeypatch.delenv("OPENSTUDIO_TLS_CA_BUNDLE", raising=False)
    client = OpenStudioClient(BASE)
    assert client._session.verify is True


def test_session_verify_honors_ca_bundle_env_var(monkeypatch):
    """Issue #242: when ``OPENSTUDIO_TLS_CA_BUNDLE`` is set, the session
    uses the bundle path instead of the system trust store.

    Clusters fronted by a custom CA (corporate PKI, air-gapped internal
    roots) mount the bundle as a Secret volume and set this env var so
    the operator can verify the API server's certificate against the
    cluster-controlled trust anchor instead of the host's ca-certificates.
    """
    monkeypatch.setenv("OPENSTUDIO_TLS_CA_BUNDLE", "/etc/ssl/certs/custom-ca.pem")
    client = OpenStudioClient(BASE)
    assert client._session.verify == "/etc/ssl/certs/custom-ca.pem"


@responses.activate
def test_tls_error_propagates_as_api_error(client, monkeypatch, sleeps):
    """Issue #242: a TLS handshake failure (e.g. unknown CA, expired cert)
    surfaces as ``OpenStudioApiError`` after the retry envelope, not as a
    silent 200 or an unhandled ``SSLError`` that escapes the tick.

    ``responses`` cannot directly synthesize an ``SSLError``, so we patch
    the session's request method to raise one. The retry loop treats it
    as a transient error (same family as ``ConnectionError``) and the
    exhaustion raises ``OpenStudioApiError`` from the final ``last_exc`` —
    this test pins that path so an SSL misconfiguration cannot degrade
    into a hang or an unhandled exception.
    """
    monkeypatch.setattr(
        client._session,
        "request",
        lambda *a, **kw: (_ for _ in ()).throw(
            requests.exceptions.SSLError("certificate verify failed: unable to get local issuer certificate")
        ),
    )
    with pytest.raises(OpenStudioApiError, match="failed after 4 attempts") as excinfo:
        client.list_analyses()
    assert isinstance(excinfo.value.__cause__, requests.exceptions.SSLError)
    assert len(sleeps) == 3


# --- Issue #252 — public surface (``list_datapoints``) over the private seam ---

# Issue #252: ``retention.py`` reached into ``client._request_json("GET",
# "/data_points.json")`` — the private REST helper. The path was
# workaround-shaped: ``get_datapoints_full()`` was deleted in #104 and the
# retention pipeline still needed the per-analysis datapoint ID set for
# archival Job args. The fix landed a public ``OpenStudioClient.list_datapoints()``
# method that wraps the same heavy poll with the same retry / backoff /
# timestamp-normalisation envelope, and the previous section of the
# module swapped the ``retention.py`` call to the public surface.
#
# The AST test below pins the "no module outside ``openstudio_client.py``
# calls ``_request_json``" invariant at the source level: a future
# maintainer who reaches into the private method again (because the
# public surface "didn't have what they needed") fails the CI gate
# loudly and is forced to add a public method instead. The
# alternative — silently bypass the public surface — would skip the
# retry / backoff / timestamp-normalisation envelope and break the
# maintainer's caller the next time the private method signature
# changes (e.g. an issue #226 follow-up that splits retry behaviour by
# HTTP verb).
#
# Scan scope: every ``.py`` file under ``src/openstudio_operator/``;
# the ``_request_json`` method body inside ``openstudio_client.py`` is
# the ONE allowed call site (it is the method's own implementation).
# Tests are excluded by the path filter (they live under ``tests/``).
# The walker matches attribute calls (``obj._request_json(...)``) so
# subclass-style invocation is also caught; attribute shadowing in
# a non-``OpenStudioClient`` class would also be flagged.
#
# Note: the previous test surface (see e.g. ``test_retention.py``)
# asserts the public ``list_datapoints()`` envelope (timestamp
# normalisation, retry-on-transient); this AST test is the callsite
# fence that closes the loop — the private method exists only inside
# ``openstudio_client.py``.


def _find_request_json_calls() -> list[tuple[str, int]]:
    """Return ``(relative_path, lineno)`` for every ``_request_json(...)`` call.

    Walks the operator's production source tree (``src/openstudio_operator/``),
    parses each ``.py`` file with :mod:`ast`, and locates ``Call`` nodes
    whose function is an attribute reference ending in ``_request_json``
    (the only documented call shape — ``obj._request_json(...)``). The
    attribute match excludes any identically-named local helper in
    another module: a fresh maintainer who defines ``def _request_json``
    on their own class would also be flagged, which is the intended
    (conservative) behaviour.
    """
    src_root = Path(OpenStudioClient.__module__.replace(".", "/"))
    # Resolve the source root from the openstudio_client module file
    # (its parent is the operator package; the parent's parent is the
    # ``src`` directory's child operator package).
    src_root = Path(OpenStudioClient.__module__.replace(".", "/"))
    # Fall back to the singleton-derived source root if the module-path
    # resolution above ever drifts (defensive against a future maintainer
    # moving the module).
    import openstudio_operator.singleton as _singleton

    src_root = Path(_singleton.__file__).parent
    found: list[tuple[str, int]] = []
    for py in sorted(src_root.rglob("*.py")):
        rel = str(py.relative_to(src_root.parent))
        if rel.endswith("openstudio_operator/openstudio_client.py"):
            # The private method is implemented here; its own
            # ``self._request_json(...)`` calls inside ``list_analyses``
            # / ``get_analysis_status`` / ``list_started_datapoints`` /
            # ``list_datapoints`` are the ONLY allowed call sites.
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr != "_request_json":
                continue
            found.append((rel, node.lineno))
    return found


def test_request_json_not_called_outside_openstudio_client() -> None:
    """Issue #252: ``_request_json`` is private to ``OpenStudioClient`` — no module calls it.

    The public surface replacement is :meth:`OpenStudioClient.list_datapoints`
    (plus the existing ``list_analyses`` / ``get_analysis_status`` /
    ``list_started_datapoints``). A maintainer who reaches for the
    private method again — because the public surface "didn't have what
    they needed" — fails this test loudly and is forced to add a public
    method on ``OpenStudioClient`` instead. The private method is the
    retry / backoff / timestamp-normalisation envelope; bypassing it
    silently skips that envelope and breaks the caller's contract the
    next time the private method signature changes.

    The assertion message names the offending file + line so the
    maintainer can fix the regression in one read.
    """
    found = _find_request_json_calls()
    assert not found, (
        f"OpenStudioClient._request_json is private and must NOT be called "
        f"from any module outside openstudio_client.py; found inline "
        f"calls at {found}. Add a public method to OpenStudioClient "
        f"and call that instead. See issue #252."
    )
