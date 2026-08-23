# ADR-6: Dual lockfiles — runtime-only `requirements.txt`, dev `requirements.lock`

## Status

Accepted.

## Context

The byte-identical-rebuild goal originally produced one lockfile:
`requirements.lock`, compiled with `pip-compile --extra=dev
--generate-hashes` and installed wholesale by the Dockerfile via
`pip install --require-hashes`. That shipped pytest, hypothesis, responses,
fakeredis, and ruff — plus their transitive closures — into the production
image that runs the operator and the prune CronJob: a container holding a
service-account token with `pods/delete` and `jobs` verbs. Test frameworks
are not written for hostile input (pytest plugins auto-load from entry
points; hypothesis executes generated programs), none of it is needed at
runtime, and nothing in CI depended on dev pins living in the runtime lock —
CI installs `-e '.[dev]'` separately after the lockfile install.

The 2026-08-22 security audit found the same discipline gap twice more. The
Dockerfile's `pip install --no-deps .` ran with default build isolation
against an unpinned `requires = ["hatchling"]`, so every image build
downloaded and executed the *latest* hatchling from PyPI, unverified by
`--require-hashes` — the builder itself floated even though its inputs were
pinned. And the ci.yml lint/audit jobs installed pip-audit, ruff, and
pyyaml from mutable latest, leaving the vulnerability gate itself built
from unverified code.

## Decision

Compile and refresh **two lockfiles as an inseparable pair**:

- `requirements.txt` — the runtime-only closure (compiled without
  `--extra=dev`, still `--generate-hashes`); the Dockerfile installs from
  this and nothing else.
- `requirements.lock` — the dev closure (`--extra=dev --generate-hashes`);
  CI installs it for reproducible test environments.

`tests/test_dependency_drift.py` gates both against `pyproject.toml`, so
neither half can drift or be regenerated without the other. release.yml
runs the **built** image with an `importlib.find_spec` blacklist
(pytest/hypothesis/responses/fakeredis/ruff) between build and the
digest-pin commit, so a leaked dev tool can never be pinned into `deploy/`;
ci.yml verifies the runtime lockfile installs hash-clean in a fresh venv on
every PR.

The build toolchain gets the same discipline: `pyproject.toml` pins
`requires = ["hatchling==1.32.0"]` exactly, the Dockerfile installs
hatchling from the hash-pinned `requirements.txt`, and the wheel is built
with `--no-build-isolation` — no ephemeral PyPI fetch during the build.

A **third, CI-only pin file** rides the same refresh: `requirements-ci.txt`
hash-pins pip-audit/ruff/pyyaml for the lint/audit jobs. It is deliberately
NOT part of the pair gate and never appears in `pyproject.toml` or either
lockfile (pip-audit stays CI-only); a structural fence
(`tests/test_ci_workflow_hygiene.py`) fails any workflow `pip install`
without `--require-hashes`/exact pin.

## Consequences

- Dependency bumps must append BOTH pip-compile regenerations to the branch
  (and `requirements-ci.txt` on the weekly pass); refreshing one half fails
  the drift test by design.
- Dev-only dependency changes land in `requirements.lock` alone — but a
  mistake that leaks them into `requirements.txt` is caught at release time
  (the built-image blacklist), not at PR time.
- hatchling upgrades are deliberate pins moving `pyproject.toml` and both
  lockfiles in the same PR; an unpinned `[build-system].requires` spec
  fails the drift gate.
- The runtime closure is smaller (30 packages vs 40), shrinking both the
  image and the pip-audit surface (ADR-7).
- `requirements-ci.txt` staleness degrades CI tooling only — it can never
  affect the shipped image, which is why it stays outside the pair gate.

## Links

- Issues #479 (the split) · #576 (hatchling build-backend pin) · #577
  (`requirements-ci.txt`) · #11 / #173 (the original single-lockfile
  byte-identical-rebuild goal)
- `requirements.txt` · `requirements.lock` · `requirements-ci.txt` ·
  `pyproject.toml` · `Dockerfile`
- `tests/test_dependency_drift.py` · `tests/test_ci_workflow_hygiene.py`
- `.github/workflows/ci.yml` · `.github/workflows/release.yml` (dev-tool
  absence assertion)
- AGENTS.md rule "CI uses a hash-pinned lockfile"
