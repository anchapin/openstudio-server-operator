# ADR-5: spec.redisUrl empty-by-default regression fence

## Status

Accepted.

## Context

The historical CRD default baked a full Redis URL — including the kind-recipe
password `openstudio`, a publicly-known literal from the upstream deployment
recipe — into `spec.redisUrl` for every CR. Fresh installs therefore silently
ran with a credential anyone could look up. The danger of a bad default is
that it never errors: clusters ran on the known password indefinitely, and
rotating it later meant touching every CR.

The fix deliberately chose *loud over convenient*: an empty default that
refuses to operate is safer than a working default built on a known password.

## Decision

`spec.redisUrl` defaults to **empty by design**. When the field is empty the
operator refuses to operate on that CR and emits a per-CR `Warning` event;
users must set the URL explicitly. Since the Secret-ref evolution, the
preferred production shape is `spec.redisCredentials.secretRef{name,key}`
pointing at a Secret whose key holds the **full** `redis://` URL — preferred
over an inline password when both are present, resolved through the
operator's single bounded `secrets: [get]` exception (see ADR-4's RBAC
discussion; the operator otherwise never reads Secrets). The CRD pattern for
`spec.redisUrl` rejects embedded `@` credentials outright, and the committed
credential Secret manifests ship only the unusable sentinel
`CHANGE_ME_RUN_ROTATE_SCRIPT` — fresh installs must run the rotation scripts.

## Consequences

- Do not "fix" the empty default — it is the regression fence. A default URL
  of any shape reintroduces the silent-known-credential hazard.
- Fresh installs fail loudly (Warning event + no-op operator) until the URL
  or secretRef is configured; that is the intended first-run experience.
- One bounded exception to the operator's never-reads-secrets rule exists
  (`client_factory.get_read_only_redis_client`, cache-keyed by secretRef
  identity, fence-checked against the in-cluster Redis pattern).
- CI guards fail the build if a password literal or a non-sentinel committed
  credential reappears in the manifests.

## Links

- Issues #116 (empty default) · #463 (secretRef evolution) · #150 / #219 /
  #462 (password rotation + sentinel fence)
- `deploy/crd.yaml` · `deploy/redis-credentials-secret.yaml` ·
  `deploy/rbac.yaml` (the bounded `secrets: [get]` grant)
- `src/openstudio_operator/client_factory.py` ·
  `scripts/rotate_redis_password.sh`
- AGENTS.md working rule "spec.redisUrl defaults to empty by design"
