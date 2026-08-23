# ADR-7: Asymmetric vulnerability-audit floors with triaged ignore-lists

## Status

Accepted.

## Context

CI generated an SPDX SBOM and SLSA provenance for every published image, but
nothing consumed them: no pip-audit of the lockfile, no image scan, no
policy gate anywhere. The supply chain proved *what* was in the image but
never checked whether any of it had a published CVE — an image could carry
a critical-severity dependency to a digest-pinned production Deployment
with every check green. Because releases are immutable-by-digest and the
digest is auto-committed back into `deploy/`, a CVE discovered post-publish
produced no CI signal to rebuild; the pin kept serving the vulnerable layer
set until someone manually bumped it.

A single severity floor cannot serve both scanners. The PyPI/OSV advisory
feed pip-audit reads is not severity-filtered — low-severity advisories
exist for nearly every package, so a CRITICAL,HIGH floor there would be
permanent noise. Debian-base image CVEs, by contrast, arrive in a volume
that must be filtered: CRITICAL,HIGH (with `--ignore-unfixed`) is the
signal.

The gate's first run made the triage policy concrete: the stale
`python:3.12-slim` base digest scanned 3 CRITICAL + 50 HIGH, which shipped
as 16 time-boxed `.trivyignore` exceptions each citing #564 — the issue
that owned the durable fix, a base-image refresh to
`python:3.12-slim-bookworm` (`sha256:a116514e…`), the only candidate
scanning 0 C + 0 H with an empty ignorefile. Python 3.14 was rejected
because every then-published digest carried strictly more gate-visible
HIGHs (setuptools CVE-2025-47273, msgpack GHSA-6v7p-g79w-8964) — it could
not meet the floor until the published base shipped the fixes. After the
refresh, `.trivyignore` is comments-only: bookworm's residual 16
CRITICAL/HIGH are all `UNFIXED` upstream, excluded by `--ignore-unfixed`
because no digest move can patch them.

## Decision

Two scanners, two floors, one triage mechanism:

- The ci.yml `audit` job runs `pip-audit --require-hashes -r
  requirements.lock` failing on **ANY known advisory** (deliberately
  stricter than the image floor — the feed is not severity-filtered), and a
  no-push image build + trivy scan gated at **CRITICAL,HIGH**
  (`--ignore-unfixed`).
- release.yml re-scans the **actually-pushed** image with the identical
  floor and `.trivyignore`, in both publish jobs, BEFORE the digest-pin
  commit — a vulnerable layer set can never be pinned into `deploy/`.
- Triage lives in `.pip-audit-ignore.txt` / `.trivyignore`: one advisory id
  per line, a justification comment, and a follow-up issue that owns the
  durable fix (the time-boxed-exception pattern above). Ignore entries are
  debt pointing at a fix, not a severity override.
- pip-audit remains CI-only tooling, installed from `requirements-ci.txt`
  (ADR-6) — never a dependency in `pyproject.toml` or either lockfile.

## Consequences

- pip-audit can go red on a low-severity advisory in a transitive dep; the
  response is upgrade-or-triage with justification + follow-up issue —
  never quietly widening the floor to match the image gate.
- Image CVEs without an upstream fix are invisible to the gate by design;
  they are documented residual risk (the base-refresh issue records them),
  not ignore-file candidates — an entry would suppress nothing a digest
  move could fix.
- The durable fix for base-image CVEs is a base refresh; a `.trivyignore`
  entry is the time-boxed bridge that makes the refresh issue findable from
  the red gate.
- The floors interlock with the neighbors: the runtime-only lockfile
  (ADR-6) shrinks the pip-audit surface, and the pre-pin rescan is the last
  content gate before ADR-8's digest pin freezes the layer set for
  `deploy/`.

## Links

- Issues #481 (the gates) · #564 (base refresh that cleared the 16 triage
  entries; 3.12-bookworm over 3.14) · #479 (runtime-only audit surface)
- `.pip-audit-ignore.txt` · `.trivyignore` · `Dockerfile` (digest-pinned
  base)
- `.github/workflows/ci.yml` (audit job) · `.github/workflows/release.yml`
  (pre-pin rescans)
- AGENTS.md rule "Vulnerability scanning (issue #481)"
- ADR-6 · ADR-8
