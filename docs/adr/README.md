# Architecture Decision Records

Durable records for the load-bearing architecture decisions — the constraints
whose *why* spans multiple documents and GitHub issues. Each record stands
alone offline: context, decision, and consequences are written as prose, and
issue numbers appear only as links, never as the justification.

Shape (lightweight MADR): `# ADR-N: Title` · `## Status` · `## Context` ·
`## Decision` · `## Consequences` · `## Links`.

| ADR | Decision |
| --- | --- |
| [ADR-1](./adr-1-kopf-pin.md) | Pin `kopf >=1.37,<1.45` — the singleton guard depends on kopf private internals |
| [ADR-2](./adr-2-single-replica-recreate.md) | Single-replica `Recreate` Deployment, no leader election |
| [ADR-3](./adr-3-keda-only-autoscaling.md) | KEDA ScaledObject is the only autoscaler; the custom HPA-floor adjuster is deleted |
| [ADR-4](./adr-4-validating-admission-policies.md) | ValidatingAdmissionPolicies express the label/name-scoped verbs RBAC cannot |
| [ADR-5](./adr-5-redisurl-empty-default.md) | `spec.redisUrl` empty-by-default regression fence |

Adding a new record: copy the shape above, take the next number, and link it
here. The substance comes from the AGENTS.md working rules and the code — the
ADR is where the *why* gets written down once, durably.
