# ADR-1: Pin kopf to >=1.37,<1.45

## Status

Accepted.

## Context

The singleton guard (decision D05 — exactly one OpenStudioClusterManager CR
per namespace, oldest wins) is enforced by *wrapping* every registered OSCM
timer handler so the guard can gate each tick. Kopf exposes no public API for
enumerating and wrapping registered handlers, so
`singleton.install_singleton_guard` reaches into kopf's private
`registry._spawning._handlers` (a shape that exists since kopf 1.37). Private
internals carry no semver-compatibility promise: a future kopf release can
rename or move the attribute without breaking anything at import time.

The dangerous failure mode is silence. If the internal moves, the guard's
lookup finds nothing to wrap and returns without error — handlers run
*unwrapped*, and D05 enforcement is quietly disabled. Nothing crashes; the
operator just stops being singleton-safe.

## Decision

Pin `kopf >=1.37,<1.45` in `pyproject.toml`. The lower bound is the first
release with the private registry shape the guard depends on. The upper bound
forces every kopf minor upgrade to be a deliberate bump, verified against the
CI gate `tests/test_singleton_registry_coverage.py`, which cross-checks the
Python-level OSCM handler registry against kopf's actual registry at gate
time — if the internals change shape or a handler escapes wrapping, the gate
fails loudly instead of shipping silently-unwrapped handlers.

## Consequences

- Kopf upgrades are never routine dependency bumps; they require checking the
  registry internals, running the coverage gate, and moving the upper bound in
  the same commit.
- A kopf release that renames the private attribute still *imports* fine — the
  test suite is the only thing that catches it, so skipping tests on an
  upgrade is how the silent-unwrapped failure ships.
- Handler signatures are also version-sensitive (kopf 1.4x changed handler
  `body` parameters); the pin keeps those surfaces predictable too.

## Links

- AGENTS.md working rule "kopf is pinned >=1.37,<1.45 on purpose"
- `src/openstudio_operator/singleton.py` · `src/openstudio_operator/_oscm_handlers.py`
- `tests/test_singleton_registry_coverage.py` · `tests/test_handler_boundaries.py` (kopf 1.4x, #232)
- `docs/audit-dryrun-idempotency.md` (D05) · `pyproject.toml`
