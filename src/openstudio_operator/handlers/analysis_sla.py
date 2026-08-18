"""Module 1 (plan Phase 1): Analysis SLA monitor & soft-stop manager.

Logic (per plan): poll GET /analyses.json every 30s; for analyses with
status == "started" exceeding spec.analysisPolicy.maxDurationMinutes:
  1. PUT /analyses/{id}/action {"action": "soft_stop"}
  2. emit K8s Warning Event: AnalysisSoftStopped
  3. wait gracefulStopTimeoutMinutes
  4. if still "stopping", escalate to {"action": "kill"} or {"action": "hard_stop"}
"""

import kopf

_SPEC = {"group": "energy.nrel.gov", "version": "v1alpha1", "plural": "openstudioclustermanagers"}


@kopf.timer(_SPEC["group"], _SPEC["version"], _SPEC["plural"], interval=30.0)
def analysis_sla_monitor(spec: dict, logger: kopf.Logger, **_: object) -> None:
    """TODO(phase 1): implement SLA check + soft-stop + escalation flow."""
    logger.info(
        "analysis_sla_monitor tick (serverUrl=%s, maxDurationMinutes=%s) — TODO(phase 1)",
        spec.get("serverUrl"),
        spec.get("analysisPolicy", {}).get("maxDurationMinutes"),
    )
