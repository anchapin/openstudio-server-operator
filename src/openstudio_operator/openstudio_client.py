"""REST client for the OpenStudio Server API, verified against NREL/OpenStudio-server v3.11.0.

Ground truth: `docs/contracts/openstudio-server-v3.11.0-rest.md`.

Reads:
    GET    /analyses.json                               raw Mongoid docs (status, run_flag, created_at, updated_at)
    GET    /analyses/{id}/status.json                   derived view: status + dp counts (the SLA clock anchor post-#83)
    GET    /analyses/{id}/page_data.json                derived `start_time` — the historical SLA clock anchor
                                                        (no longer authoritative on v3.11.0; status is absent
                                                        on a started analysis until the first job, see #83 D1)
    GET    /data_points/status?status=1&jobs=started    light watchdog poll (no timestamps)
    GET    /data_points.json                            full docs, heavy — escalation-only (ip_address; v3.11.0
                                                        always-null on K8s, see #83 D2 — escalation re-sources
                                                        to Resque-worker-identity)

Actions:
    GET    /analyses/{id}/soft_stop                     cooperative stop, does NOT wait for in-flight runs
    POST   /analyses/{id}/action                        body param `analysis_action` ∈ start|stop (no PUT route)
    POST   /data_points/{id}/requeue                    204; re-enqueues onto the `requeued` Resque queue
    DELETE /analyses/{id}                               server-side cascade frees NFS + Mongo

Non-existent / legacy (do NOT use): `GET /cluster.json`, `kill`/`hard_stop` actions,
`PUT /analyses/{id}/action` (route is POST), `/compute_nodes.json` on K8s.

Client discipline (D12): every timestamp is normalized to timezone-aware UTC at this boundary;
transient failures (5xx, connection errors, timeouts) are retried with jittered exponential
backoff (~1s/2s/4s) before `OpenStudioApiError` is raised — callers skip the tick and retry
naturally on the next poll.
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from typing import Any

import requests

from ._time import parse_iso_utc

_TRANSIENT_EXCEPTIONS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# Single source of truth for ISO-8601 → tz-aware UTC parsing (D12 boundary,
# issue #174). Re-exported under the historical name so the existing
# ``tests/test_openstudio_client.py`` direct import keeps working unchanged.
def _parse_timestamp(value: str | None) -> datetime | None:
    """Backward-compatible alias for :func:`openstudio_operator._time.parse_iso_utc`."""
    return parse_iso_utc(value)


def _normalize_timestamps(obj: Any) -> Any:
    """Recursively convert string values whose key ends in ``_at``/``_time`` to tz-aware UTC.

    Covers the Mongoid/derived fields the operator reads: ``created_at``, ``updated_at``,
    ``start_time``, ``end_time``, ``run_start_time``. All other values pass through.
    """
    if isinstance(obj, dict):
        return {
            key: parse_iso_utc(val)
            if isinstance(val, str) and key.endswith(("_at", "_time"))
            else _normalize_timestamps(val)
            for key, val in obj.items()
        }
    if isinstance(obj, list):
        return [_normalize_timestamps(item) for item in obj]
    return obj


class OpenStudioApiError(RuntimeError):
    """Raised when the OpenStudio REST API fails or returns an unexpected response."""


class OpenStudioClient:
    """REST client for OpenStudio Server v3.11.0.

    One initial attempt plus up to ``max_retries`` retries on transient failures (HTTP 5xx,
    connection errors, timeouts) with jittered exponential backoff: sleep before retry N is
    ``backoff_base_seconds * 2**(N-1) scaled by a random 0.5–1.5 jitter (~1s/2s/4s by
    default), then ``OpenStudioApiError`` is raised. HTTP 4xx raises immediately (not
    transient). Timestamps in returned documents are normalized to timezone-aware UTC.
    """

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff_base_seconds = backoff_base_seconds
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def _jittered_backoff(self, retry: int) -> float:
        return random.uniform(0.5, 1.5) * self._backoff_base_seconds * 2 ** (retry - 1)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> requests.Response:
        url = f"{self._base}{path}"
        last_error = ""
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            if attempt:
                _sleep(self._jittered_backoff(attempt))
            try:
                response = self._session.request(
                    method, url, params=params, data=data, timeout=self._timeout
                )
            except _TRANSIENT_EXCEPTIONS as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"
                last_exc = exc
                continue
            if response.status_code < 400:
                return response
            if response.status_code < 500:
                raise OpenStudioApiError(
                    f"{method} {path} returned HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )
            last_error = f"HTTP {response.status_code}"
            last_exc = None
        raise OpenStudioApiError(
            f"{method} {path} failed after {self._max_retries + 1} attempts: {last_error}"
        ) from last_exc

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        response = self._request(method, path, params=params, data=data)
        try:
            return _normalize_timestamps(response.json())
        except requests.exceptions.JSONDecodeError as exc:
            raise OpenStudioApiError(f"{method} {path}: invalid JSON in response") from exc
        except ValueError as exc:
            raise OpenStudioApiError(f"{method} {path}: {exc}") from exc

    # --- Reads -----------------------------------------------------------

    def list_analyses(self) -> list[dict]:
        """GET /analyses.json — full list of analysis documents (id, name, status, etc.).

        Raw docs omit nil fields on a fresh analysis (live v3.11.0 contract §2).
        For per-analysis derived fields (start_time, point counts, output variables),
        use ``get_analysis_status`` — it is the live-verified source-of-truth for
        status and reliably reflects the analysis lifecycle.
        """
        return self._request_json("GET", "/analyses.json")

    def get_analysis_status(self, analysis_id: str) -> dict:
        """GET /analyses/{id}/status.json — derived view, the SLA clock anchor (#83 D1).

        Live-verified v3.11.0: the only endpoint that reports the real analysis
        status. Returns ``{analysis: {...}}`` for exactly one match (count-based
        wrapping — see contract §7); ``{analyses: [...]}`` for zero or many; the
        ``analysis`` payload carries ``status``, ``run_flag``, ``jobs`` (per-job
        status), and ``data_points`` (counts by status).

        Uniqueness-of-the-1-match wrapper is the data the SLA clock needs: a
        started analysis yields ``{analysis: {status: "started", jobs: [...],
        ...}}`` and the operator-observed first sight of ``status == "started"``
        becomes the CR ``.status.softStops[aid].issuedAt`` anchor (D04). The
        ``jobs`` list and per-dp counts surface here too, but the SLA clock only
        reads the top-level ``status``.

        Unknown ids answer 200 ``{analyses: []}`` (the ``where()`` path never
        raises — contract §1); the SLA flow treats that body as "no candidate".
        """
        return self._request_json("GET", f"/analyses/{analysis_id}/status.json")

    def list_started_datapoints(self) -> list[dict]:
        """GET /data_points/status?status=1&jobs=started — light view, returns the inner list.

        Shape: ``{data_points: [{_id, id, analysis_id, status, status_message}]}``. Rails
        quirk: the presence of the ``status`` param gates filtering; the filter value is
        read from ``jobs``. No timestamps — pair with the operator-tracked ``startedSince``
        clock.
        """
        payload = self._request_json(
            "GET", "/data_points/status", params={"status": 1, "jobs": "started"}
        )
        return payload.get("data_points", []) if isinstance(payload, dict) else []

    # --- Actions ---------------------------------------------------------

    def soft_stop_analysis(self, analysis_id: str) -> None:
        """GET /analyses/{id}/soft_stop — cooperative stop that does NOT wait for in-flight
        runs (semantics roughly inverted vs ``stop``).
        """
        self._request("GET", f"/analyses/{analysis_id}/soft_stop")

    def stop_analysis(self, analysis_id: str) -> None:
        """POST /analyses/{id}/action with body param ``analysis_action=stop``.

        Verified semantics: set ``run_flag`` false and wait for in-flight runs. Chosen over
        ``GET /analyses/{id}/stop`` ("stop waiting for last submitted run") because the
        action endpoint carries the explicit run_flag/wait semantics the operator needs.
        The route is POST only — ``PUT /analyses/{id}/action`` does not exist in v3.11.0,
        and no ``kill``/``hard_stop`` action exists anywhere; escalation is Kubernetes-side
        pod eviction.

        RESERVED (issue #49): the method has zero call sites. The SLA flow uses
        ``soft_stop_analysis`` (does not wait) and ``delete_analysis`` owns the
        NFS-cascade cleanup path, so this waiting-variant stop is not currently wired.

        Do NOT invoke from a handler without:

        1. ``spec.dryRun`` gating (D11) — see
           ``docs/audit-dryrun-idempotency.md`` §1.1 row R2.
        2. A status-anchor idempotency design (D04) — a ``stoppedAt`` /
           ``runFlagClearedAt`` record on the CR so a restarted operator does not
           re-issue the POST and so flipping ``dryRun`` off cannot double-fire.

        If you wire this method, add the mutation to the audit doc's table in the
        same PR (replace DORMANT status with GATED and cite the gate line).
        """
        self._request("POST", f"/analyses/{analysis_id}/action", data={"analysis_action": "stop"})

    def requeue_datapoint(self, datapoint_id: str) -> None:
        """POST /data_points/{id}/requeue — 204 No Content.

        Destroys the existing Resque job on the ``requeued``/``simulations`` queues and
        re-enqueues on the ``requeued`` queue. Does NOT kill a wedged worker process; that
        escalation is Kubernetes-side.
        """
        self._request("POST", f"/data_points/{datapoint_id}/requeue")

    def delete_analysis(self, analysis_id: str) -> None:
        """DELETE /analyses/{id} — server-side cascade.

        ``data_points dependent: :destroy`` (each dp ``after_destroy`` rm-rf's its NFS
        asset dir) plus ``before_destroy :queue_delete_files``: this IS the NFS cleanup for
        the asset tree and frees the Mongo documents.
        """
        self._request("DELETE", f"/analyses/{analysis_id}")
