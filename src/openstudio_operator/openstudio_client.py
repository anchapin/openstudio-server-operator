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

Client discipline (D12): every timestamp is normalized to timezone-aware UTC at this boundary.
For ``GET`` methods, transient failures (5xx, connection errors, timeouts) are retried with
jittered exponential backoff (~1s/2s/4s) before ``OpenStudioApiError`` is raised — callers
skip the tick and retry naturally on the next poll. For non-GET methods (POST/PUT/DELETE/PATCH),
a single attempt is made; a 5xx response propagates immediately to the caller (issue #226:
RFC 9110 §9.2.2 makes POST non-idempotent by default, and the v3.11.0 contract documents GET
as the only safely-retryable verb — re-firing a POST on a 504 that followed a server-side
commit would double-burn the operator's accounting).
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime
from typing import Any

import requests

from openstudio_operator.metrics import REST_REQUEST_DURATION_SECONDS, REST_RETRIES_TOTAL

from ._retry import _sleep
from ._time import parse_iso_utc
from .redis_client import OperatorConfigError

_TRANSIENT_EXCEPTIONS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)


_PEM_BEGIN_MARKER = "-----BEGIN CERTIFICATE-----"


def _resolve_tls_ca_bundle() -> bool | str:
    """Validate ``OPENSTUDIO_TLS_CA_BUNDLE`` and return the value ``requests.Session.verify`` expects.

    Issue #296: the truthy-string fallthrough ``ca_bundle if ca_bundle else True``
    silently accepted arbitrary non-empty env values (``"True"``, ``"1"``,
    ``"yes"``, ``"on"``) and passed them to ``requests`` as CA-bundle paths. An
    attacker who controls the operator pod env (or a misconfigured helm chart
    that templated a boolean flag into the env var) could substitute a CA
    bundle of their choosing and MITM the in-cluster REST traffic. The
    ``BEGIN CERTIFICATE`` substring check pins the value to a real PEM bundle.

    Returns ``True`` when the env var is unset or empty (system trust store —
    the pinned regression fence from issue #242). Returns the bundle path on
    success. Raises :class:`OperatorConfigError` on any validation failure so
    the operator refuses to start the tick rather than degrading TLS
    verification into an obscure ``requests`` ``SSLError`` at the first REST
    call.
    """
    ca_bundle = os.environ.get("OPENSTUDIO_TLS_CA_BUNDLE")
    if not ca_bundle:
        return True
    if not os.path.isfile(ca_bundle):
        raise OperatorConfigError(
            f"OPENSTUDIO_TLS_CA_BUNDLE={ca_bundle!r} does not name an existing "
            f"file; refusing to start the tick with an unverifiable CA bundle "
            f"(issue #296). Mount the PEM bundle as a Secret volume and set "
            f"the env var to its in-pod path."
        )
    try:
        with open(ca_bundle, encoding="utf-8") as bundle_file:
            head = bundle_file.read(4096)
    except OSError as exc:
        raise OperatorConfigError(
            f"OPENSTUDIO_TLS_CA_BUNDLE={ca_bundle!r} is not readable: {exc}; "
            f"refusing to start the tick (issue #296)."
        ) from exc
    if _PEM_BEGIN_MARKER not in head:
        raise OperatorConfigError(
            f"OPENSTUDIO_TLS_CA_BUNDLE={ca_bundle!r} does not contain a "
            f"{_PEM_BEGIN_MARKER!r} PEM marker in the first 4 KiB; refusing to "
            f"treat it as a CA bundle (issue #296)."
        )
    return ca_bundle


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

    For ``GET`` requests, one initial attempt plus up to ``max_retries`` retries on transient
    failures (HTTP 5xx, connection errors, timeouts) with jittered exponential backoff: sleep
    before retry N is ``backoff_base_seconds * 2**(N-1)`` scaled by a random 0.5–1.5 jitter
    (~1s/2s/4s by default), then ``OpenStudioApiError`` is raised. HTTP 4xx raises immediately
    (not transient). Timestamps in returned documents are normalized to timezone-aware UTC.

    For non-GET requests (POST/PUT/DELETE/PATCH), a single attempt is made. Any non-2xx
    response — including 5xx — raises ``OpenStudioApiError`` immediately (issue #226). The
    v3.11.0 contract documents GET as the only safely-retryable verb; a 504 returned AFTER the
    server processed a requeue / stop / delete may have left the server-side cascade
    partially executed, so re-firing risks double-burning operator accounting. Call sites that
    need retry-safe mutating calls must opt in with their own idempotency-key design.
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
        # Issue #242: pin ``verify=True`` explicitly (requests default) AND honor
        # ``OPENSTUDIO_TLS_CA_BUNDLE`` for clusters whose API server / inter-service
        # traffic is fronted by a custom CA (corporate PKI roots, air-gapped
        # clusters with their own internal CA). When the env var is unset or
        # empty the session falls through to the system trust store — never to
        # ``False``. The acceptance criterion calls for
        # ``os.environ.get('OPENSTUDIO_TLS_CA_BUNDLE', True)`` semantics; the
        # empty-string branch below preserves that without ruff's PLW1508
        # (``True`` default on ``os.environ.get`` is a string-returning API).
        # Issue #296: the truthy-string path above accepted arbitrary non-empty
        # values (``"True"``, ``"1"``, ``"yes"``) and handed them to ``requests``
        # as a CA bundle path. ``_resolve_tls_ca_bundle`` validates the path
        # exists and contains a ``BEGIN CERTIFICATE`` PEM marker; on failure it
        # raises ``OperatorConfigError`` so the operator refuses to start the
        # tick rather than degrading TLS verification to a runtime ``SSLError``.
        self._session.verify = _resolve_tls_ca_bundle()

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
        # Issue #308 — observe wall-clock duration of every ``_request`` call,
        # including the GET-only 3x retry envelope. The histogram is observed
        # once at the END of the function (success or raised exception) so
        # the entire retry-window wall-clock is part of the observation —
        # exactly the data point an on-call needs when correlating slow
        # ticks with REST degredation. ``outcome`` mirrors what the caller
        # would see: ``"200"`` for any 2xx/3xx return, ``"exception"`` for
        # any raised ``OpenStudioApiError`` (4xx immediately, 5xx retries
        # exhausted, or non-GET 5xx per issue #226). ``method`` is the verb
        # the operator actually uses (GET | POST | DELETE) so a per-verb
        # split can tell the on-call which verb is responsible.
        started = time.perf_counter()
        outcome: str = "exception"
        try:
            url = f"{self._base}{path}"
            last_error = ""
            last_exc: Exception | None = None
            for attempt in range(self._max_retries + 1):
                if attempt:
                    # Issue #471 — count every re-attempt so a retry storm
                    # is separable from a slow success: the duration
                    # histogram below observes only the terminal outcome,
                    # and the backoff sleeps silently inflate its buckets.
                    REST_RETRIES_TOTAL.labels(method=method.upper()).inc()
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
                    outcome = "200"
                    return response
                if response.status_code < 500:
                    raise OpenStudioApiError(
                        f"{method} {path} returned HTTP {response.status_code}: "
                        f"{response.text[:200]}"
                    )
                # Issue #226: 5xx retries are GET-only. POST/PUT/DELETE/PATCH are non-idempotent
                # by default (RFC 9110 §9.2.2); a 504 that follows a server-side commit must NOT
                # re-fire the same request. ``DELETE /analyses/{id}`` is documented as idempotent
                # in practice, but a 504 mid-cascade returns the operator to a tick where the
                # cascade may have partially executed — the safer default is no retry, and any
                # call site that wants retry-safe mutating behavior must opt in with its own
                # idempotency-key design rather than inherit it from this client.
                if method.upper() != "GET":
                    raise OpenStudioApiError(
                        f"{method} {path} returned HTTP {response.status_code} (no retry: "
                        f"non-GET verb is non-idempotent per RFC 9110 §9.2.2, see issue #226): "
                        f"{response.text[:200]}"
                    )
                last_error = f"HTTP {response.status_code}"
                last_exc = None
            raise OpenStudioApiError(
                f"{method} {path} failed after {self._max_retries + 1} attempts: {last_error}"
            ) from last_exc
        finally:
            elapsed = time.perf_counter() - started
            REST_REQUEST_DURATION_SECONDS.labels(
                method=method.upper(), outcome=outcome
            ).observe(elapsed)

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

    def list_datapoints(self) -> list[dict]:
        """GET /data_points.json — full data_point docs (heavy poll, escalation-only).

        Live v3.11.0 contract: this is the heavy endpoint — the per-dp docs
        carry ``_id``, ``analysis_id``, ``status``, ``status_message``, and
        ``ip_address`` (always ``null`` on K8s — see #83 D2; the SLA
        escalator no longer uses it). The retention pipeline is the ONLY
        caller (issue #252): it needs the per-analysis datapoint ID set
        for archival Job args, so the heavy poll is gated by the
        retention tick (run at most once per ``storage-cronjob.yaml``
        schedule — 600 s by default) and only when a spawn is actually
        due. The SLA monitor's per-tick observation is the LIGHT
        endpoint (:meth:`list_started_datapoints`); the watchdog's
        per-tick observation is also the LIGHT endpoint.

        Issue #252 — added as the public surface replacement for the
        legacy ``client._request_json("GET", "/data_points.json")`` seam
        that retention.py reached into after ``get_datapoints_full()``
        was deleted in #104. The private method is unchanged in
        signature; the AST test in
        ``tests/test_openstudio_client.py::test_request_json_not_called_outside_openstudio_client``
        enforces "no module outside ``openstudio_client.py`` calls
        ``_request_json``" so a future maintainer refactoring the
        private method (retry/backoff signature, timestamp
        normalisation, etc.) silently breaks no caller. Same retry /
        backoff / timestamp-normalisation envelope as every other
        method on this client.

        Returns a list of raw data_point dicts with timestamp fields
        normalised to tz-aware UTC. Empty list is the canonical
        "no datapoints yet" response (404 is not a possibility here —
        the endpoint always returns 200 with an empty list on a clean
        cluster).
        """
        return self._request_json("GET", "/data_points.json")

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
