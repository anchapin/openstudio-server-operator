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

Runtime-only lockfile (issue #479)
----------------------------------
The production image installs from ``requirements.txt`` — a second
pip-compile output generated WITHOUT ``--extra=dev`` — so pytest /
hypothesis / responses / fakeredis / ruff and their dev-only transitives
never enter the runtime container. The mirror tests below fail when:

* a runtime dep declared in ``[project].dependencies`` is missing (or
  unhashed) in ``requirements.txt`` (the maintainer edited pyproject and
  re-ran only the ``--extra=dev`` compile), OR
* any dev-extra package appears by name in ``requirements.txt`` (the
  runtime lockfile was regenerated with the dev extra by mistake — the
  regression fence for the #479 attack-surface fix).

Both lockfiles are refreshed together by the commands documented in
``AGENTS.md`` (one ``pip-compile --extra=dev`` → ``requirements.lock``, one
without → ``requirements.txt``).

Build-backend pin (issue #576)
------------------------------
The Dockerfile builds the operator wheel with
``pip install --no-deps --no-build-isolation .``, so the build backend named
in ``[build-system]`` (hatchling) must ALREADY be installed in the image —
which the Dockerfile does from the hash-pinned ``requirements.txt``. Two
drift invariants guard that pair:

* every ``[build-system].requires`` spec is an exact ``==`` pin (an unpinned
  spec floats the backend to the PyPI latest wherever default build
  isolation still applies — local editable installs, non-image builds), AND
* every build-system package has a hash-pinned entry in BOTH lockfiles.
  pip-compile does NOT compile ``[build-system].requires`` — the hatchling
  closure was spliced into the lockfiles by hand — so a routine
  ``pip-compile`` regeneration silently DELETES those blocks and breaks the
  ``--no-build-isolation`` image build. This gate turns that deletion into
  a loud CI failure instead of a release-time ``docker build`` error.

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
REQUIREMENTS_TXT = REPO_ROOT / "requirements.txt"

# PEP 503: normalize name by lowercasing and replacing runs of [-_.] with -.
_NORMALIZE_RE = re.compile(r"[-_.]+")


def _normalize(name: str) -> str:
    """Apply PEP 503 name normalization (``PyYAML`` → ``pyyaml``)."""
    return _NORMALIZE_RE.sub("-", name).lower()


def _dep_names(entries: list[str]) -> list[str]:
    """Normalize a list of PEP 508 requirement strings to PEP 503 names.

    Strips inline comments (everything after ``#``) and version specifiers
    (``>=``, ``==``, ``<``, ``>``, ``~=``, ``!=``) so we end up with a clean
    list of package names. Extras markers (e.g. ``package[extra]``) are also
    stripped — the lockfiles pin only the base package.
    """
    names: list[str] = []
    for raw in entries:
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


def _dev_dep_names(pyproject_text: str) -> list[str]:
    """Extract the PEP 503-normalized names of every ``[project.optional-dependencies].dev`` entry."""
    data = tomllib.loads(pyproject_text)
    dev = data["project"]["optional-dependencies"]["dev"]
    return _dep_names(dev)


def _runtime_dep_names(pyproject_text: str) -> list[str]:
    """Extract the PEP 503-normalized names of every ``[project].dependencies`` entry."""
    data = tomllib.loads(pyproject_text)
    return _dep_names(data["project"]["dependencies"])


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


def test_runtime_lockfile_exists() -> None:
    """``requirements.txt`` (the runtime-only lockfile) is present at the repo root.

    Issue #479: the Dockerfile installs from ``requirements.txt`` — compiled
    WITHOUT ``--extra=dev`` — so dev tools never enter the production image.
    A missing file means the Docker build would fail (or worse, someone
    pointed it back at ``requirements.lock``).
    """
    assert REQUIREMENTS_TXT.exists(), (
        f"requirements.txt not found at {REQUIREMENTS_TXT}. "
        "Run `pip-compile --generate-hashes --no-strip-extras "
        "--output-file=requirements.txt pyproject.toml` to generate it "
        "(issue #479 — refresh it together with requirements.lock)."
    )


def test_every_runtime_dep_is_hash_pinned_in_runtime_lockfile() -> None:
    """Every ``[project].dependencies`` dep has a hash-pinned entry in ``requirements.txt``.

    Mirror of the dev-dep gate for the runtime lockfile: the maintainer who
    adds a runtime dep to ``pyproject.toml`` must re-run BOTH pip-compile
    commands. A runtime dep missing from ``requirements.txt`` would make the
    Docker image build install an unpinned (or absent) package.
    """
    pyproject_text = PYPROJECT.read_text()
    runtime_dep_names = _runtime_dep_names(pyproject_text)
    assert runtime_dep_names, (
        "No runtime deps found in pyproject.toml [project].dependencies — "
        "the operator has no dependencies, which cannot be right."
    )

    runtime_lockfile_text = REQUIREMENTS_TXT.read_text()
    blocks = _parse_lockfile(runtime_lockfile_text)

    missing = [name for name in runtime_dep_names if name not in blocks]
    unhashed = [name for name in runtime_dep_names if name in blocks and not blocks[name]]

    if missing or unhashed:
        problems: list[str] = []
        if missing:
            problems.append(f"Runtime deps missing from requirements.txt entirely: {missing}")
        if unhashed:
            problems.append(
                f"Runtime deps present in requirements.txt but without "
                f"--hash=sha256:... lines: {unhashed}"
            )
        pytest.fail(
            "pyproject.toml [project].dependencies drift detected vs "
            "requirements.txt (#479):\n  - "
            + "\n  - ".join(problems)
            + "\nRemediation: regenerate BOTH lockfiles together:\n"
            "  pip-compile --extra=dev --generate-hashes --no-strip-extras \\\n"
            "      --output-file=requirements.lock pyproject.toml\n"
            "  pip-compile --generate-hashes --no-strip-extras \\\n"
            "      --output-file=requirements.txt pyproject.toml"
        )


def test_no_dev_deps_in_runtime_lockfile() -> None:
    """No ``[project.optional-dependencies].dev`` package appears in ``requirements.txt``.

    The #479 regression fence: the runtime lockfile must be compiled WITHOUT
    the dev extra. A direct-dev-name blacklist is sufficient here — dev-only
    transitives (pluggy, iniconfig, ...) ride along with pytest, and the
    release-workflow ``docker run`` find_spec assert covers the built-image
    transitive story end to end.
    """
    pyproject_text = PYPROJECT.read_text()
    dev_dep_names = _dev_dep_names(pyproject_text)

    runtime_lockfile_text = REQUIREMENTS_TXT.read_text()
    blocks = _parse_lockfile(runtime_lockfile_text)

    leaked = sorted(name for name in dev_dep_names if name in blocks)
    assert not leaked, (
        f"Dev deps leaked into requirements.txt (the runtime-only lockfile, #479): "
        f"{leaked}. requirements.txt must be compiled WITHOUT --extra=dev:\n"
        "  pip-compile --generate-hashes --no-strip-extras \\\n"
        "      --output-file=requirements.txt pyproject.toml"
    )


def test_runtime_lockfile_every_entry_has_a_hash() -> None:
    """Every package entry in ``requirements.txt`` carries at least one ``--hash=sha256:...`` line.

    The Dockerfile runs ``pip install --require-hashes -r requirements.txt``;
    an unhashed entry breaks the release image build. Mirrors the broader
    ``requirements.lock`` hash-everywhere check.
    """
    runtime_lockfile_text = REQUIREMENTS_TXT.read_text()
    blocks = _parse_lockfile(runtime_lockfile_text)
    if not blocks:
        pytest.skip(
            "requirements.txt contains no package entries; "
            "the broader hash-everywhere check is vacuously true"
        )
    unhashed = sorted(name for name, has in blocks.items() if not has)
    assert not unhashed, (
        f"requirements.txt entries missing --hash=sha256:... lines: {unhashed}. "
        "Re-run `pip-compile --generate-hashes` to re-assert the hash pins."
    )


# An exact PEP 508 pin: one ``name==version`` clause, no other operators
# (``>=``, ``~=``, ``!=``, compound specs) — the only shape that cannot
# float to a different PyPI release (#576).
_EXACT_PIN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?==[A-Za-z0-9.+!_-]+$")


def test_build_system_requires_are_pinned_exactly() -> None:
    """Every ``[build-system].requires`` entry is an exact ``==`` pin (issue #576).

    The Dockerfile builds the operator wheel with ``--no-build-isolation``
    and installs the backend from the hash-pinned ``requirements.txt``, but
    the pyproject pin is what every OTHER build context (local editable
    installs, ``pip wheel``, sdist builds) reads — an unpinned spec there
    floats to the PyPI latest inside default build isolation, executing
    unverified PyPI code with full wheel-build privileges. Bare names
    (``hatchling``), floors (``hatchling>=1.26``), compatible releases
    (``~=``), and compound specs all fail here: only ``name==version`` is
    accepted.
    """
    with PYPROJECT.open("rb") as f:
        data = tomllib.load(f)
    requires = data.get("build-system", {}).get("requires", [])
    assert requires, (
        "pyproject.toml is missing [build-system].requires — the wheel "
        "build backend must be declared AND pinned (#576)."
    )
    unpinned = [spec for spec in requires if not _EXACT_PIN_RE.match(spec)]
    assert not unpinned, (
        f"[build-system].requires entries are not exact '==' pins (#576): {unpinned}. "
        "The build backend executes arbitrary code during wheel builds — pin it "
        "exactly (e.g. 'hatchling==1.32.0'), add its closure hash-pinned to BOTH "
        "lockfiles, and build with --no-build-isolation (see Dockerfile)."
    )


def test_build_backend_closure_is_hash_pinned_in_both_lockfiles() -> None:
    """Every ``[build-system].requires`` package is hash-pinned in BOTH lockfiles (#576).

    The Dockerfile relies on the runtime lockfile already containing the
    build backend: ``pip install --require-hashes -r requirements.txt``
    provides hatchling for the subsequent
    ``pip install --no-deps --no-build-isolation .``. pip-compile does NOT
    compile ``[build-system].requires``, so the closure lives in the
    lockfiles as a hand-spliced block — and the next routine
    ``pip-compile`` regeneration will DELETE it. This gate fails loudly the
    moment that happens, instead of the release workflow discovering a
    broken ``docker build`` (ModuleNotFoundError: No module named
    'hatchling').
    """
    with PYPROJECT.open("rb") as f:
        data = tomllib.load(f)
    requires = data.get("build-system", {}).get("requires", [])
    build_names = _dep_names(requires)
    assert build_names, (
        "No build-system requirements found in pyproject.toml — nothing "
        "guards the build backend lockfile presence (#576)."
    )

    problems: list[str] = []
    for label, path in (
        ("requirements.txt", REQUIREMENTS_TXT),
        ("requirements.lock", REQUIREMENTS_LOCK),
    ):
        blocks = _parse_lockfile(path.read_text())
        missing = [n for n in build_names if n not in blocks]
        unhashed = [n for n in build_names if n in blocks and not blocks[n]]
        if missing:
            problems.append(f"{label}: build backend missing entirely: {missing}")
        if unhashed:
            problems.append(f"{label}: build backend present without --hash lines: {unhashed}")

    assert not problems, (
        "pyproject.toml [build-system].requires drift detected vs the "
        "lockfiles (#576):\n  - " + "\n  - ".join(problems) + "\n"
        "The Dockerfile builds with --no-build-isolation, so the backend must "
        "be hash-pinned in requirements.txt (and requirements.lock, per the "
        "#479 pair rule). pip-compile omits [build-system].requires — the "
        "closure is hand-spliced; re-add it with --generate-hashes output "
        "after any regeneration."
    )


# CI-only tool lockfile (issue #577)
# ----------------------------------
# requirements-ci.txt is a THIRD lockfile: the hash-pinned source for the
# tooling the ci.yml lint/audit jobs run (pip-audit, ruff, pyyaml). It is
# deliberately NOT part of the #479 pair and NOT compiled from pyproject —
# pip-audit must never appear in pyproject.toml or either runtime lockfile
# (#481: it is a CI-only auditing tool and must stay out of the production
# image). The gates below fail when:
#
# * the file is missing, or does not pin all three tools the issue names,
# * any entry is not an exact ``==`` pin (a floor floats to PyPI latest —
#   the exact supply-chain hole #577 closes), or lacks its ``--hash`` lines,
# * pip-audit (or ruff/pyyaml-as-CI-tools, by name collision intent) leaks
#   into pyproject.toml / requirements.lock / requirements.txt.
#
# The workflow-side fence (no bare ``pip install`` in any workflow) lives
# in tests/test_ci_workflow_hygiene.py.

REQUIREMENTS_CI = REPO_ROOT / "requirements-ci.txt"

# The three tools issue #577 names, PEP 503-normalized.
CI_TOOL_NAMES = ("pip-audit", "pyyaml", "ruff")


def _entry_without_extras(entry: str) -> str:
    """Strip a PEP 508 extras marker (``cachecontrol[filecache]==0.14.4`` →
    ``cachecontrol==0.14.4``) — the lockfiles pin only the base package."""
    if "[" not in entry:
        return entry
    name, _, rest = entry.partition("[")
    return name + rest.partition("]")[2]


def test_requirements_ci_file_exists() -> None:
    """``requirements-ci.txt`` (the CI-only tool lockfile) exists at the repo root."""
    assert REQUIREMENTS_CI.exists(), (
        f"requirements-ci.txt not found at {REQUIREMENTS_CI}. The ci.yml "
        "lint/audit tooling must install from a hash-pinned file (issue #577) — "
        "see the regenerate procedure in that file's header comment."
    )


def test_requirements_ci_pins_the_three_ci_tools() -> None:
    """requirements-ci.txt carries exact ``==`` entries for pip-audit, ruff,
    and pyyaml — the three tools the pre-#577 ci.yml installed from mutable
    latest. A tool missing here means its workflow step either fails or was
    quietly reverted to a bare ``pip install``."""
    blocks = _parse_lockfile(REQUIREMENTS_CI.read_text())
    missing = [name for name in CI_TOOL_NAMES if name not in blocks]
    assert not missing, (
        f"requirements-ci.txt is missing the CI tools {missing} (issue #577). "
        "Regenerate it per the header comment so every ci.yml tool step "
        "installs from this file."
    )


def test_requirements_ci_every_entry_is_exact_pinned_and_hashed() -> None:
    """Every requirements-ci.txt entry is an exact ``==`` pin WITH ``--hash``
    lines — the same two-part contract the #479 pair enforces, applied to
    the CI tools (issue #577: the vulnerability gate must not itself be
    built from mutable-latest PyPI code)."""
    text = REQUIREMENTS_CI.read_text()
    blocks = _parse_lockfile(text)
    assert blocks, "requirements-ci.txt contains no package entries (issue #577)"
    entries = [
        line.rstrip("\\ ").rstrip()
        for line in text.splitlines()
        if line and not line[0].isspace() and not line.startswith("#") and "\\" in line
    ]
    not_exact = [entry for entry in entries if not _EXACT_PIN_RE.match(_entry_without_extras(entry))]
    unhashed = sorted(name for name, has in blocks.items() if not has)
    problems = []
    if not_exact:
        problems.append(f"entries not exact '==' pins: {not_exact}")
    if unhashed:
        problems.append(f"entries missing --hash=sha256:... lines: {unhashed}")
    assert not problems, (
        "requirements-ci.txt contract broken (issue #577):\n  - "
        + "\n  - ".join(problems)
        + "\nRemediation: regenerate per the header comment:\n"
        "  pip-compile --generate-hashes --no-strip-extras \\\n"
        "      --output-file=requirements-ci.txt /tmp/requirements-ci.in"
    )


# pip-audit is the one CI tool whose presence in the project dependency
# surfaces would be a supply-chain regression (#481): ruff and pyyaml ARE
# legitimate project deps (dev-extra / runtime) — only pip-audit is
# CI-only by design.
_CI_ONLY_TOOLS = ("pip-audit",)


def test_pip_audit_never_enters_project_dependency_surfaces() -> None:
    """pip-audit appears ONLY in requirements-ci.txt — never in pyproject.toml
    or either lockfile of the #479 pair (#481 rule, restated as a gate by
    #577): it is a CI-only auditing tool and must never reach the production
    image or the dev install."""
    surfaces = {
        "pyproject.toml": PYPROJECT.read_text(),
        "requirements.lock": REQUIREMENTS_LOCK.read_text(),
        "requirements.txt": REQUIREMENTS_TXT.read_text(),
    }
    leaked = sorted(
        label
        for label, text in surfaces.items()
        if any(_normalize(name) in _dep_names(text.splitlines()) for name in _CI_ONLY_TOOLS)
        or re.search(r"(?im)^pip-audit==", text) is not None
    )
    assert not leaked, (
        f"pip-audit leaked into {leaked} (issues #481/#577): it is "
        "a CI-only auditing tool — its only home is requirements-ci.txt, "
        "installed solely by the ci.yml audit job."
    )
