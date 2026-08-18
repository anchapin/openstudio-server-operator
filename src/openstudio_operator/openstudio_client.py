"""Thin REST client for the OpenStudio Server API used by the operator.

Endpoint contract (from the plan doc — exact action spellings matter):

  GET    /analyses.json                     poll ~every 30s
  GET    /data_points.json?status=started   zombie datapoint detection
  GET    /cluster.json                      worker/queue state
  PUT    /analyses/{id}/action              body {"action": "soft_stop"}; escalation "kill" / "hard_stop"
  POST   /data_points/{id}/requeue          zombie datapoint requeue
  DELETE /analyses/{id}                     post-archival cleanup
"""

from __future__ import annotations


class OpenStudioApiError(RuntimeError):
    """Raised when the OpenStudio REST API returns an unexpected response."""


class OpenStudioClient:
    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_seconds

    # --- Reads -----------------------------------------------------------

    def list_analyses(self) -> list[dict]:
        """GET /analyses.json — TODO(phase 1): used by the SLA monitor."""
        raise NotImplementedError

    def list_started_datapoints(self) -> list[dict]:
        """GET /data_points.json?status=started — TODO(phase 2): zombie watchdog."""
        raise NotImplementedError

    def get_cluster(self) -> dict:
        """GET /cluster.json — TODO(phase 2): web_background stall detection."""
        raise NotImplementedError

    # --- Actions ---------------------------------------------------------

    def soft_stop_analysis(self, analysis_id: str) -> None:
        """PUT /analyses/{id}/action {"action": "soft_stop"} — TODO(phase 1)."""
        raise NotImplementedError

    def escalate_analysis(self, analysis_id: str, action: str) -> None:
        """PUT /analyses/{id}/action with "kill" or "hard_stop" — TODO(phase 1)."""
        if action not in ("kill", "hard_stop"):
            raise ValueError(f"invalid escalation action: {action!r}")
        raise NotImplementedError

    def requeue_datapoint(self, datapoint_id: str) -> None:
        """POST /data_points/{id}/requeue — TODO(phase 2)."""
        raise NotImplementedError

    def delete_analysis(self, analysis_id: str) -> None:
        """DELETE /analyses/{id} — TODO(phase 3): post-archival cleanup."""
        raise NotImplementedError
