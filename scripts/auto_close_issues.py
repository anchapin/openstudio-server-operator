#!/usr/bin/env python3
"""Auto-close comma-separated `Closes #N` references after squash merge (issue #289).

GitHub's auto-close machinery scans a squash-commit body for closing
keywords (``Closes #N``, ``Fixes #N``, ``Resolves #N``). When the keyword
appears on a line that contains a comma-separated list like
``Closes #243, #244, #245``, only the FIRST reference triggers
auto-close. The remaining references stay OPEN and have to be closed by
hand via ``gh issue close``.

The wave-orchestrator's Phase 4c (Issue Close Verification) is meant to
catch this. This helper is the concrete automation it calls:

1. Read the merged PR's body (``gh pr view <N> --json body --jq .body``).
2. Parse out every ``#N`` reference on every line that carries a closing
   keyword (covers single, comma-separated, and multi-line forms).
3. For each reference, ask ``gh issue view <N> --json state --jq .state``.
   If the issue is ``OPEN``, run ``gh issue close <N> -c "Closed via PR #<N>"``.

Returns the list of issue numbers that were actually closed by THIS call
(issues that GitHub already auto-closed are silently skipped — they're
already in CLOSED state when we inspect).

Usage::

    python scripts/auto_close_issues.py 287
    python scripts/auto_close_issues.py 287 --repo anchapin/openstudio-server-operator

Programmatic::

    from auto_close_issues import auto_close_issues_from_pr_body
    closed = auto_close_issues_from_pr_body(287)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import NamedTuple

# A closing keyword. GitHub's full vocabulary is longer
# (close/closes/closed/fix/fixes/fixed/resolve/resolves/resolved) but the
# imperative forms are what the orchestrator's merge-subject template
# emits (see docs/onboarding.md#merge-subject-hygiene-88). Extending the
# set is a one-line change here.
CLOSING_KEYWORD = re.compile(r"\b(?:Closes|Fixes|Resolves)\b", re.IGNORECASE)

# A bare ``#123`` reference — the leading ``#`` is required so we don't
# pick up e.g. ``steps: #1 #2 #3`` prose that happens to follow a keyword.
ISSUE_REF = re.compile(r"#(\d+)\b")

DEFAULT_REPO = "anchapin/openstudio-server-operator"


class GhResult(NamedTuple):
    """Minimal stand-in for ``subprocess.CompletedProcess`` used by tests."""

    stdout: str
    returncode: int = 0


def _run_gh(args: list[str], *, check: bool = True) -> GhResult:
    """Single subprocess chokepoint for every ``gh`` invocation.

    Tests monkeypatch this function to dispatch by argv shape; production
    code goes through ``subprocess.run`` so the call site reads as a
    normal shell-out.
    """
    completed = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=check,
    )
    return GhResult(stdout=completed.stdout, returncode=completed.returncode)


def parse_closing_refs(body: str) -> list[int]:
    """Extract ``#N`` references from lines that carry a closing keyword.

    Mirrors GitHub's "same-line" rule: the keyword and the reference must
    share a line. This is the strictest correct interpretation — a bullet
    that says ``Closes:`` on one line and ``- #243`` on the next is NOT
    matched by GitHub either, so matching it here would diverge from the
    auto-close machinery we're reconciling against.

    Order: top-to-bottom, deduplicated (a reference that appears on two
    keyword lines is counted once, at its first occurrence).
    """
    refs: list[int] = []
    seen: set[int] = set()
    for line in body.splitlines():
        if not CLOSING_KEYWORD.search(line):
            continue
        for match in ISSUE_REF.finditer(line):
            number = int(match.group(1))
            if number not in seen:
                refs.append(number)
                seen.add(number)
    return refs


def _pr_body(pr_number: int, repo: str) -> str:
    """Return the PR body (possibly empty) for ``pr_number``."""
    result = _run_gh(
        [
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "body",
            "--jq",
            ".body",
        ]
    )
    return result.stdout or ""


def _issue_state(issue_number: int, repo: str) -> str:
    """Return ``"OPEN"`` / ``"CLOSED"`` for the given issue number."""
    result = _run_gh(
        [
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "state",
            "--jq",
            ".state",
        ]
    )
    return (result.stdout or "").strip().upper()


def _close_issue(issue_number: int, pr_number: int, repo: str) -> None:
    """Run ``gh issue close <N> -c "Closed via PR #<pr_number>"``."""
    _run_gh(
        [
            "issue",
            "close",
            str(issue_number),
            "--repo",
            repo,
            "-c",
            f"Closed via PR #{pr_number}",
        ]
    )


def auto_close_issues_from_pr_body(
    pr_number: int,
    repo: str = DEFAULT_REPO,
) -> list[int]:
    """Close every ``Closes/Fixes/Resolves #N`` reference in a merged PR's body.

    Reads ``pr_number``'s body, iterates the parsed references in source
    order, and for every reference that ``gh issue view`` reports as
    OPEN runs ``gh issue close`` with a ``Closed via PR #<pr_number>``
    comment. Returns the list of issues actually closed by THIS call
    (already-closed ones are skipped in order to keep the call idempotent
    when the orchestrator retries).

    The function never raises on a missing PR or missing issue: it lets
    the underlying ``gh`` call's ``subprocess.CalledProcessError`` bubble
    up. The wave-orchestrator's Phase 4c runs this after a confirmed
    squash merge, so the PR exists; an issue number that no longer
    resolves is a real failure worth surfacing.
    """
    body = _pr_body(pr_number, repo)
    refs = parse_closing_refs(body)
    closed: list[int] = []
    for issue_number in refs:
        state = _issue_state(issue_number, repo)
        if state == "OPEN":
            _close_issue(issue_number, pr_number, repo)
            closed.append(issue_number)
    return closed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else "auto_close_issues",
    )
    parser.add_argument("pr_number", type=int, help="merged PR number")
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help="GitHub repo (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    closed = auto_close_issues_from_pr_body(args.pr_number, args.repo)
    if not closed:
        print("Closed 0 issue(s) — every Closes/Fixes/Resolves reference was already closed.")
    else:
        print(f"Closed {len(closed)} issue(s): {closed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())