"""Unit tests for OpenStudioClient against the verified v3.11.0 REST contract."""

from datetime import UTC, datetime

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
def test_get_analysis_page_data_normalizes_sla_anchor(client):
    responses.get(
        f"{BASE}/analyses/a1/page_data.json",
        json={
            "analysis": {
                "status": "started",
                "start_time": "2026-08-18T08:00:00-06:00",
                "run_flag": True,
            }
        },
    )
    page = client.get_analysis_page_data("a1")
    assert page["analysis"]["start_time"] == datetime(2026, 8, 18, 14, 0, 0, tzinfo=UTC)
    assert page["analysis"]["status"] == "started"


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
def test_get_datapoints_full_returns_ip_address_and_utc_times(client):
    responses.get(
        f"{BASE}/data_points.json",
        json=[
            {
                "_id": "dp1",
                "status": "started",
                "ip_address": "10.244.0.17",
                "run_start_time": "2026-08-18T08:00:00-06:00",
                "updated_at": "2026-08-18T09:30:00Z",
            }
        ],
    )
    docs = client.get_datapoints_full()
    assert docs[0]["ip_address"] == "10.244.0.17"
    assert docs[0]["run_start_time"] == datetime(2026, 8, 18, 14, 0, 0, tzinfo=UTC)
    assert docs[0]["updated_at"] == datetime(2026, 8, 18, 9, 30, 0, tzinfo=UTC)
    assert "status=" not in responses.calls[0].request.url


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
    responses.get(f"{BASE}/analyses/a1/page_data.json", status=404, body="{}")
    with pytest.raises(OpenStudioApiError, match="404"):
        client.get_analysis_page_data("a1")
    assert len(responses.calls) == 1
    assert sleeps == []


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
