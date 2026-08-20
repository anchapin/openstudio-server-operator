"""Unit tests for scripts/auto_close_issues.py (issue #289).

Covers the wave-orchestrator's Phase 4c (Issue Close Verification) helper:

* :func:`parse_closing_refs` — single, comma-separated, multi-line, and
  mixed prose bodies; dedup + order.
* :func:`auto_close_issues_from_pr_body` — dispatches the right ``gh``
  invocations in source order, skips issues that ``gh issue view``
  already reports as CLOSED (GitHub's auto-close machinery wins), and
  leaves the run idempotent under retry.

All subprocess work goes through :func:`auto_close_issues._run_gh`, which
the tests monkeypatch — no real ``gh`` calls are made.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Import via importlib so the file is loaded by path; the rest of the
# suite imports ``openstudio_operator`` packages so we don't want a
# top-level ``import auto_close_issues`` to shadow anything.
_spec = importlib.util.spec_from_file_location(
    "auto_close_issues",
    SCRIPTS_DIR / "auto_close_issues.py",
)
assert _spec is not None and _spec.loader is not None
auto_close_issues = importlib.util.module_from_spec(_spec)
sys.modules["auto_close_issues"] = auto_close_issues
_spec.loader.exec_module(auto_close_issues)

REPO = "anchapin/openstudio-server-operator"


class FakeCall(NamedTuple):
    """One recorded ``_run_gh`` invocation."""

    args: tuple[str, ...]


class FakeGh:
    """Stand-in for ``auto_close_issues._run_gh`` that records every call.

    Configure ``pr_body`` (string returned by ``gh pr view ... --jq .body``)
    and ``states`` (mapping of issue number → ``"OPEN"`` / ``"CLOSED"``)
    per test. Every ``gh`` call lands in ``self.calls`` in invocation
    order, which is what the order-verification assertions check against.
    """

    def __init__(
        self,
        *,
        pr_body: str = "",
        states: dict[int, str] | None = None,
    ) -> None:
        self.pr_body = pr_body
        self.states: dict[int, str] = dict(states or {})
        self.calls: list[FakeCall] = []

    def __call__(self, args: list[str], *, check: bool = True) -> auto_close_issues.GhResult:
        self.calls.append(FakeCall(args=tuple(args)))
        if args[:2] == ["pr", "view"]:
            return auto_close_issues.GhResult(stdout=self.pr_body)
        if args[:2] == ["issue", "view"]:
            # ``gh issue view <N> --repo ... --json state --jq .state``
            number = int(args[2])
            return auto_close_issues.GhResult(
                stdout=self.states.get(number, "OPEN"),
                returncode=0,
            )
        if args[:2] == ["issue", "close"]:
            return auto_close_issues.GhResult(stdout="", returncode=0)
        raise AssertionError(f"unexpected gh call: {args}")


# ---------------------------------------------------------------------------
# parse_closing_refs — pure function, no subprocess mocking required.
# ---------------------------------------------------------------------------


def test_parse_single_issue():
    assert auto_close_issues.parse_closing_refs("Closes #243") == [243]


def test_parse_comma_separated_keeps_order():
    body = "Closes #243, #244, #245"
    assert auto_close_issues.parse_closing_refs(body) == [243, 244, 245]


def test_parse_multi_line_keeps_order():
    body = (
        "This PR fixes the worker recycler race.\n"
        "\n"
        "Closes #243\n"
        "Closes #244\n"
        "Closes #245\n"
    )
    assert auto_close_issues.parse_closing_refs(body) == [243, 244, 245]


def test_parse_dedup_when_repeated_across_lines():
    body = "Closes #243\nCloses #243\nCloses #244"
    assert auto_close_issues.parse_closing_refs(body) == [243, 244]


def test_parse_ignores_prose_with_hash_but_no_keyword():
    body = (
        "See steps: #1 #2 #3 for context.\n"
        "Implements the plan from #243.\n"
    )
    assert auto_close_issues.parse_closing_refs(body) == []


def test_parse_handles_fixes_and_resolves_keywords():
    body = "Fixes #100\nResolves #101"
    assert auto_close_issues.parse_closing_refs(body) == [100, 101]


def test_parse_keyword_must_be_on_same_line_as_reference():
    """GitHub's rule is 'keyword on the same line as the #N' — match it."""
    body = "Closes:\n- #243\n- #244\n"
    # The keyword line has no #N on it; the bullet lines have no keyword.
    # Per GitHub's same-line rule, none of these auto-close.
    assert auto_close_issues.parse_closing_refs(body) == []


def test_parse_comma_inside_keyword_line_is_collected():
    body = "Fixes #243, #244 and resolves #245, #246."
    assert auto_close_issues.parse_closing_refs(body) == [243, 244, 245, 246]


def test_parse_handles_trailing_punctuation_on_reference():
    body = "Closes #243, #244; also #246."
    assert auto_close_issues.parse_closing_refs(body) == [243, 244, 246]


def test_parse_empty_body_returns_empty():
    assert auto_close_issues.parse_closing_refs("") == []


def test_parse_keyword_is_case_insensitive():
    body = "closes #243\nCLOSES #244\nCloses #245"
    assert auto_close_issues.parse_closing_refs(body) == [243, 244, 245]


# ---------------------------------------------------------------------------
# auto_close_issues_from_pr_body — subprocess surface.
# ---------------------------------------------------------------------------


def test_closes_only_open_issues(monkeypatch):
    """All references returned as OPEN ⇒ close each one in order."""
    fake = FakeGh(
        pr_body="Closes #243, #244, #245",
        states={243: "OPEN", 244: "OPEN", 245: "OPEN"},
    )
    monkeypatch.setattr(auto_close_issues, "_run_gh", fake)

    closed = auto_close_issues.auto_close_issues_from_pr_body(287, repo=REPO)

    assert closed == [243, 244, 245]
    close_calls = [c for c in fake.calls if c.args[:2] == ("issue", "close")]
    assert [c.args[2] for c in close_calls] == ["243", "244", "245"]
    # The close comments cite the PR number the helper was called with.
    for call in close_calls:
        assert "-c" in call.args
        assert "Closed via PR #287" in call.args[call.args.index("-c") + 1]


def test_skips_already_closed_issues_and_keeps_returned_order(monkeypatch):
    """GitHub auto-closed the first; we close the rest in source order."""
    fake = FakeGh(
        pr_body="Closes #243, #244, #245",
        states={243: "CLOSED", 244: "OPEN", 245: "OPEN"},
    )
    monkeypatch.setattr(auto_close_issues, "_run_gh", fake)

    closed = auto_close_issues.auto_close_issues_from_pr_body(287, repo=REPO)

    assert closed == [244, 245]
    close_calls = [c for c in fake.calls if c.args[:2] == ("issue", "close")]
    assert [c.args[2] for c in close_calls] == ["244", "245"]


def test_invocation_order_pr_body_then_state_then_close(monkeypatch):
    """For each reference: gh pr view → (issue view → issue close) × N."""
    fake = FakeGh(
        pr_body="Closes #243, #244\nFixes #245",
        states={243: "OPEN", 244: "OPEN", 245: "OPEN"},
    )
    monkeypatch.setattr(auto_close_issues, "_run_gh", fake)

    closed = auto_close_issues.auto_close_issues_from_pr_body(287, repo=REPO)

    assert closed == [243, 244, 245]
    # Expected sequence (one entry per call):
    #   pr view 287
    #   issue view 243, issue close 243
    #   issue view 244, issue close 244
    #   issue view 245, issue close 245
    subcommands = [c.args[:2] for c in fake.calls]
    assert subcommands == [
        ("pr", "view"),
        ("issue", "view"),
        ("issue", "close"),
        ("issue", "view"),
        ("issue", "close"),
        ("issue", "view"),
        ("issue", "close"),
    ]
    # The pr view call carries the PR number and the body jq filter.
    assert fake.calls[0].args == (
        "pr",
        "view",
        "287",
        "--repo",
        REPO,
        "--json",
        "body",
        "--jq",
        ".body",
    )


def test_empty_body_returns_empty_and_no_close_calls(monkeypatch):
    fake = FakeGh(pr_body="")
    monkeypatch.setattr(auto_close_issues, "_run_gh", fake)

    closed = auto_close_issues.auto_close_issues_from_pr_body(287, repo=REPO)

    assert closed == []
    # Only the pr view call should have been made.
    assert [c.args[:2] for c in fake.calls] == [("pr", "view")]


def test_all_issues_already_closed_returns_empty(monkeypatch):
    """Idempotency: re-running on a closed batch is a no-op."""
    fake = FakeGh(
        pr_body="Closes #243, #244",
        states={243: "CLOSED", 244: "CLOSED"},
    )
    monkeypatch.setattr(auto_close_issues, "_run_gh", fake)

    closed = auto_close_issues.auto_close_issues_from_pr_body(287, repo=REPO)

    assert closed == []
    close_calls = [c for c in fake.calls if c.args[:2] == ("issue", "close")]
    assert close_calls == []


def test_default_repo_is_used_when_omitted(monkeypatch):
    fake = FakeGh(pr_body="Closes #1", states={1: "OPEN"})
    monkeypatch.setattr(auto_close_issues, "_run_gh", fake)

    auto_close_issues.auto_close_issues_from_pr_body(287)

    # Every call should reference the default repo (no repo argument
    # override ⇒ default constant).
    assert all(REPO in call.args for call in fake.calls)
    assert fake.calls[0].args == (
        "pr",
        "view",
        "287",
        "--repo",
        auto_close_issues.DEFAULT_REPO,
        "--json",
        "body",
        "--jq",
        ".body",
    )


def test_custom_repo_is_passed_through(monkeypatch):
    fake = FakeGh(pr_body="Closes #1", states={1: "OPEN"})
    monkeypatch.setattr(auto_close_issues, "_run_gh", fake)

    auto_close_issues.auto_close_issues_from_pr_body(287, repo="acme/widgets")

    assert all("acme/widgets" in call.args for call in fake.calls)


def test_check_flag_is_true_by_default(monkeypatch):
    """``check=True`` propagates so gh non-zero exits surface to the caller."""

    def fake(args: list[str], *, check: bool = True) -> auto_close_issues.GhResult:
        assert check is True
        if args[:2] == ["pr", "view"]:
            return auto_close_issues.GhResult(stdout="Closes #1")
        if args[:2] == ["issue", "view"]:
            return auto_close_issues.GhResult(stdout="OPEN")
        return auto_close_issues.GhResult(stdout="")

    monkeypatch.setattr(auto_close_issues, "_run_gh", fake)
    auto_close_issues.auto_close_issues_from_pr_body(287, repo=REPO)


# ---------------------------------------------------------------------------
# CLI entrypoint — exercised via ``main(["287"])``.
# ---------------------------------------------------------------------------


def test_main_returns_zero_and_prints_closed_list(monkeypatch, capsys):
    fake = FakeGh(pr_body="Closes #243, #244", states={243: "OPEN", 244: "OPEN"})
    monkeypatch.setattr(auto_close_issues, "_run_gh", fake)

    rc = auto_close_issues.main(["287"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "Closed 2" in captured.out
    assert "243" in captured.out and "244" in captured.out


def test_main_handles_zero_closures(monkeypatch, capsys):
    """No closing refs in body ⇒ helpful message, still exit 0."""

    def fake(args: list[str], *, check: bool = True) -> auto_close_issues.GhResult:
        return auto_close_issues.GhResult(stdout="Just a docs change, no body refs.")

    monkeypatch.setattr(auto_close_issues, "_run_gh", fake)
    rc = auto_close_issues.main(["287"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "0" in captured.out


if __name__ == "__main__":
    # Allow ``python tests/test_auto_close_issues.py`` for quick local runs.
    pytest.main([__file__, "-v"])