# ADR-2: Single-replica Recreate Deployment, no leader election

## Status

Accepted.

## Context

The operator is a poller: every tick it reads the OpenStudio REST API, Redis
queue depths, and the CR `.status` subresource, then takes mutating actions —
analysis soft-stops, datapoint requeues, worker pod recycling, `web_background`
restarts. Idempotency (decision D04: all memory lives in `.status`, grace
anchors on `.status` timestamps, 409-safe read-modify-write) makes repeated
actions *safe* across process restarts, but idempotent ≠ harmless under
concurrency: two simultaneous pollers double-soft-stop the same analysis,
requeue a datapoint that another poller just requeued, restart `web_background`
twice, and race on `.status` writes that the 409-retry loop resolves only
after both sides have acted.

Kopf supports leader election, but configuring it adds moving parts (a lease,
election timeouts, failover tuning) that this deployment does not need: the
operator manages one namespace, and a missed tick is retried naturally on the
next poll — a counted, logged skip (D12), not an outage.

## Decision

`deploy/operator-deployment.yaml` ships `replicas: 1` with
`strategy: Recreate`, and no leader election. `Recreate` (not `RollingUpdate`)
is load-bearing: a rolling update would briefly run the old and new pod
together — two pollers — on every deploy.

## Consequences

- Every deployment has a brief window with no active poller; ticks are
  skipped (counted in `HANDLER_TICK_FAILURES_TOTAL`) and the next poll picks
  the work up. Accepted by design.
- Scaling `replicas` above 1, or switching to `RollingUpdate`, without first
  adding leader election reintroduces the double-poller hazard — duplicate
  soft-stops, restarts, and event storms that `.status` idempotency does not
  prevent.
- High availability for the operator itself is deliberately *not* a goal;
  availability of the managed stack is unaffected by an operator restart.

## Links

- AGENTS.md working rule "operator-deployment.yaml is single-replica with
  strategy: Recreate on purpose"
- `deploy/operator-deployment.yaml`
- `docs/audit-dryrun-idempotency.md` (D04 state model, D12 skip-tick)
- `src/openstudio_operator/status_store.py` (409-safe RMW)
