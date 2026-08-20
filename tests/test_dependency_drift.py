"""CI drift gate (issue #298): every dev dep declared in ``pyproject.toml`` must
be hash-pinned in ``requirements.lock``.

Why this test exists
--------------------
The operator's ``pyproject.toml`` declares five dev-only test libraries under
``[project.optional-dependencies].dev`` (``pytest`` for the runner, ``ruff``
for the linter, ``responses`` for the OpenStudio REST mocks, ``fakeredis``
for the read-only Redis mocks, ``hypothesis`` for the D12 timestamp property
tests). Issue #173 introduced ``requirements.lock`` with hash-pinning so
``pip install --require-hashes`` is reproducible across CI runs. The lockfile
was generated against ``pyproject.toml`` WITHOUT the ``--extra dev`` flag, so
the four non-``pytest`` dev pins were silently absent from the lockfile.
A maintainer who bumps any of those floors in ``pyproject.toml`` (e.g.
``hypothesis>=6.50`` after adding a new property test) would then ship a CI
install that resolves against the **unlocked** PyPI latest — which today
ships a different property-test API and would fail the existing
``tests/test_time_parsing.py:196-355`` ``@given`` calls under a different
Hypothesis release.

This test makes that failure mode loud. It runs on every CI invocation (the
project's lint+test gate, ``.github/workflows/ci.yml``) and fails clearly
when:

* a dev dep declared in ``pyproject.toml`` has no entry in
  ``requirements.lock`` (the maintainer added a new dev pin without re-running
  ``pip-compile``), OR
* a dev dep's entry in ``requirements.lock`` is missing its
  ``--hash=sha256:...`` line (the lockfile was hand-edited or partial-regenerated),
  OR
* something in ``requirements.lock`` slipped in without a hash (the lockfile
  contract is broken more broadly).

When the test fires, the failure message tells the maintainer exactly which
package is missing and the canonical remediation command (``pip-compile
--extra=dev --generate-hashes --output-file=requirements.lock pyproject.toml``),
so the fix is a one-line ``pip-compile`` invocation rather than a forensic
investigation.

PEP 503 normalization
---------------------
Package names on PyPI are case- and separator-insensitive (``PyYAML`` ==
``pyyaml`` == ``Py_Yaml``). The lockfile stores the canonical form
(``.lower()`` with runs of ``[-_.]`` collapsed to a single ``-``); this
test normalizes both sides the same way so ``prompt_toolkit`` vs
``prompt-toolkit`` etc. would compare equal. The five dev pins in this repo
are already lowercase ASCII with no separators, so the normalization is a
no-op for them today — but the invariant is documented for the day a future
dev dep introduces the trouble.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
REQUIREMENTS_LOCK = REPO_ROOT / "requirements.lock"

# PEP 503: normalize name by lowercasing and replacing runs of [-_.] with -.
_NORMALIZE_RE = re.compile(r"[-_.]+")


def _normalize(name: str) -> str:
    """Apply PEP 503 name normalization (``PyYAML`` → ``pyyaml``)."""
    return _NORMALIZE_RE.sub("-", name).lower()


def _dev_dep_names(pyproject_text: str) -> list[str]:
    """Extract the PEP 503-normalized names of every ``[project.optional-dependencies].dev`` entry.

    Strips inline comments (everything after ``#``) and version specifiers
    (``>=``, ``==``, ``<``, ``>``, ``~=``, ``!=``) so we end up with a clean
    list of package names. Extras markers (e.g. ``package[extra]``) are also
    stripped — the lockfile pins only the base package.
    """
    data = tomllib.loads(pyproject_text)
    dev = data["project"]["optional-dependencies"]["dev"]
    names: list[str] = []
    for raw in dev:
        # Strip inline comments and surrounding whitespace
        cleaned = raw.split("#", 1)[0].strip()
        if not cleaned:
            continue
        # Strip extras marker: "package[extra]" -> "package"
        cleaned = cleaned.split("[", 1)[0]
        # Strip version specifiers: "name>=1.0" -> "name"
        name = re.split(r"[<>=!~]", cleaned, 1)[0].strip()
        names.append(_normalize(name))
    return names


def _parse_lockfile(lockfile_text: str) -> dict[str, bool]:
    """Return ``{normalized_name: True if the entry has any --hash=sha256:... line}``.

    The lockfile is a sequence of blocks separated by blank lines. Each block
    starts with a top-level ``name==version \\`` header (no leading whitespace,
    not a ``#`` comment) and is followed by indented continuation lines:
    ``--hash=sha256:...`` lines and ``# via <package>`` provenance comments.
    A package header can be detected by matching
    ``^[A-Za-z0-9]`` (PEP 503-valid name char) followed by ``==``
    """
    has_hash: dict[str, bool] = {}
    current_name: str | None = None
    for line in lockfile_text.splitlines():
        # Top-level (non-indented, non-comment) line -> potential package header
        if line and not line[0].isspace() and not line.startswith("#"):
            match = re.match(r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)==", line)
            if match:
                current_name = _normalize(match.group(1))
                has_hash.setdefault(current_name, False)
                continue
            # Non-package top-level line (rare; no-op for the dev-dep gate)
            current_name = None
            continue
        # Indented line: check for hash lines attached to the current entry
        if current_name is not None and line.lstrip().startswith("--hash=sha256:"):
            has_hash[current_name] = True
        # Comment / blank lines do not change ``current_name`` — a blank line
        # between blocks is allowed by the format and the next non-blank line
        # re-asserts the name.
    return has_hash


def test_lockfile_exists() -> None:
    """``requirements.lock`` is present at the repo root."""
    assert REQUIREMENTS_LOCK.exists(), (
        f"requirements.lock not found at {REQUIREMENTS_LOCK}. "
        "Run `pip-compile --extra=dev --generate-hashes "
        "--output-file=requirements.lock pyproject.toml` to generate it."
    )


def test_pyproject_has_dev_extras_section() -> None:
    """``pyproject.toml`` declares a non-empty ``[project.optional-dependencies].dev`` list.

    Without this section the drift gate would have nothing to verify, and the
    operator's dev environment would silently regress (lint/CI would lack
    the test libraries).
    """
    with PYPROJECT.open("rb") as f:
        data = tomllib.load(f)
    dev = data["project"]["optional-dependencies"].get("dev")
    assert dev is not None, (
        "pyproject.toml is missing [project.optional-dependencies].dev; "
        "the dev-only test libraries (ruff, responses, fakeredis, hypothesis, "
        "pytest) must be declared under `[project.optional-dependencies].dev`."
    )
    assert len(dev) > 0, "[project.optional-dependencies].dev must be non-empty"


def test_every_dev_dep_is_hash_pinned_in_lockfile() -> None:
    """Every dev dep in ``pyproject.toml`` has a hash-pinned entry in ``requirements.lock``.

    The drift gate (issue #298). Two failure modes are distinguished:

    * ``missing`` — the package has no entry in the lockfile. The fix is
      ``pip-compile --extra=dev --generate-hashes --output-file=requirements.lock
      pyproject.toml``.
    * ``unhashed`` — the package has an entry but no ``--hash=sha256:...``
      line. The lockfile was hand-edited or partial-regenerated; the fix is
      the same ``pip-compile`` invocation, which re-asserts the hash.
    """
    pyproject_text = PYPROJECT.read_text()
    dev_dep_names = _dev_dep_names(pyproject_text)
    assert dev_dep_names, (
        "No dev deps found in pyproject.toml — the drift gate has nothing to verify. "
        "Re-add the dev extras (ruff, responses, fakeredis, hypothesis, pytest)."
    )

    lockfile_text = REQUIREMENTS_LOCK.read_text()
    lockfile_blocks = _parse_lockfile(lockfile_text)

    missing = [name for name in dev_dep_names if name not in lockfile_blocks]
    unhashed = [name for name in dev_dep_names if name in lockfile_blocks and not lockfile_blocks[name]]

    if missing or unhashed:
        problems: list[str] = []
        if missing:
            problems.append(
                f"Dev deps missing from requirements.lock entirely: {missing}"
            )
        if unhashed:
            problems.append(
                f"Dev deps present in requirements.lock but without "
                f"--hash=sha256:... lines: {unhashed}"
            )
        pytest.fail(
            "pyproject.toml [project.optional-dependencies].dev drift "
            "detected vs requirements.lock (#298):\n  - "
            + "\n  - ".join(problems)
            + "\nRemediation: regenerate the lockfile with both prod and dev "
            "extras hash-pinned:\n"
            "  pip-compile --extra=dev --generate-hashes \\\n"
            "      --output-file=requirements.lock pyproject.toml"
        )


def test_lockfile_every_entry_has_a_hash() -> None:
    """Every package entry in ``requirements.lock`` carries at least one ``--hash=sha256:...`` line.

    Broader companion to the dev-dep gate: if the lockfile contract is broken
    anywhere (not just dev deps), ``pip install --require-hashes`` will fail
    in CI. This test makes the failure mode loud BEFORE the install runs.
    """
    lockfile_text = REQUIREMENTS_LOCK.read_text()
    blocks = _parse_lockfile(lockfile_text)
    if not blocks:
        pytest.skip(
            "requirements.lock contains no package entries; "
            "the broader hash-everywhere check is vacuously true"
        )
    unhashed = sorted(name for name, has in blocks.items() if not has)
    assert not unhashed, (
        f"requirements.lock entries missing --hash=sha256:... lines: {unhashed}. "
        "Re-run `pip-compile --generate-hashes` to re-assert the hash pins."
    )
