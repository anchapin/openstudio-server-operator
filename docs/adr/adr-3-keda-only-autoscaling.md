# ADR-3: KEDA ScaledObject is the only autoscaler

## Status

Accepted.

## Context

The original design (Phase 4) included a custom `hpa_floor` handler: operator
code that patched an HPA's `minReplicas` to keep `worker` replicas from
collapsing while Resque queue depth was high. That put autoscaling *logic* in
the operator — extra `horizontalpodautoscalers` RBAC verbs, a second opinion
about queue depth that could drift from the autoscaler's own view, and custom
scaling code to maintain. The upstream helm chart additionally ships its own
`worker-hpa` HPA, so a KEDA migration risked leaving two autoscalers attached
to the same Deployment — two controllers with different signals and lag
fighting over `replicas`, oscillating worker counts up and down.

## Decision

Delete the custom adjuster entirely (the `hpa_floor` handler module and the
`openstudio_operator_hpa_floor_adjustments_total` counter are gone) and let
**all** autoscaling live in `deploy/keda-scaledobject.yaml`: a KEDA
ScaledObject scaling the `worker` Deployment on Redis queue depth. KEDA
(≥ 2.20, cluster-admin prerequisite — `scripts/install-keda.sh` or helm
`kedacore/keda`) is required before this works, and the chart's `worker-hpa`
HPA **must be disabled** — exactly one autoscaler per Deployment.

## Consequences

- Operator RBAC dropped the whole `horizontalpodautoscalers` rule; the
  operator holds no autoscaling verbs at all.
- Clusters without KEDA get no worker scaling until an admin installs it
  (runbook: `docs/validation.md` "KEDA cluster prerequisite").
- The operator still *reads* Redis queue depths for observability (Resque
  gauges); a persistent disagreement between the operator's gauges and KEDA's
  external-metrics view is a debugging signal, not two controllers.
- Any future autoscaling change is a manifest change plus a KEDA upgrade
  concern — never new operator scaling code.

## Links

- Issues #18 (the deleted HPA-floor adjuster) · #77 (the KEDA migration)
- `deploy/keda-scaledobject.yaml` · `deploy/rbac.yaml`
- AGENTS.md working rule "No custom autoscaling code"
- `docs/validation.md` (KEDA cluster prerequisite) · README.md status note
