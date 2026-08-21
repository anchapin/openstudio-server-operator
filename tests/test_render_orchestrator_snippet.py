"""Regression tests for scripts/render_orchestrator_snippet.py (issue #382).

The Phase 3c snippets in ``docs/skill-snapshot/SKILL.md`` are templates
whose literal placeholders (``{N}``, ``{slug}``, ``{PR_NUMBER}``,
``{affected_file}``, ``#M``) must be substituted before execution — the
substitution mechanism is the §0 contract documented alongside them, with
``scripts/render_orchestrator_snippet.py`` as the canonical renderer.

The §1.5 and §5 templates are extracted from the snapshot at test time
(not hardcoded) so the tests track the documented snippets: if a snippet
is restructured, extraction fails loudly here instead of silently testing
a stale copy. The acceptance drill from the issue — render §1.5 against
``{N}=370``, ``{slug}=pre-report-back-git-status``,
``{affected_file}=test_events_emit_failures`` and get a *working* bash
command — is exercised three ways: string assertions, ``bash -n``
syntax validation, and real execution against a scratch git worktree
with a stub ``.venv/bin/pytest``, plus the §1.5 manual drill (untracked
``.junk`` file must abort with exit 1 before any PR creation).
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "render_orchestrator_snippet.py"
SKILL_SNAPSHOT = REPO_ROOT / "docs" / "skill-snapshot" / "SKILL.md"

# Import via importlib so the file is loaded by path (same convention as
# tests/test_auto_close_issues.py — the rest of the suite imports
# ``openstudio_operator`` packages, so we don't want a top-level import
# of a scripts/ module to shadow anything).
_spec = importlib.util.spec_from_file_location("render_orchestrator_snippet", SCRIPT)
assert _spec is not None and _spec.loader is not None
ros = importlib.util.module_from_spec(_spec)
sys.modules["render_orchestrator_snippet"] = ros
_spec.loader.exec_module(ros)

# Placeholder-shaped tokens the rendered output must never contain. The
# lookbehind exempts bash variable expansions (${BODY}, ${NEXT_ISSUE}).
RESIDUAL_TOKEN_RE = re.compile(r"(?<!\$)\{[A-Za-z_][A-Za-z0-9_]*\}")

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _fenced_block_containing(marker: str) -> str:
    """First fenced ```bash block containing *marker* in the SKILL snapshot."""
    text = SKILL_SNAPSHOT.read_text(encoding="utf-8")
    fences = re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)
    matches = [fence for fence in fences if marker in fence]
    assert matches, f"no fenced bash block containing {marker!r} in {SKILL_SNAPSHOT}"
    return matches[0]


# The §0 contract preamble itself carries a bash fence (the helper usage
# example) that contains "--issue-number" — scope the §1.5 lookup to the
# unique "Step 1.5" comment so the right block is found regardless.
SECTION_15_TEMPLATE = _fenced_block_containing("Step 1.5: pre-PR verification")
SECTION_5_TEMPLATE = _fenced_block_containing('NEXT_ISSUE="#M"')

# The exact sample values from issue #382's acceptance criterion.
SAMPLE_N = "370"
SAMPLE_SLUG = "pre-report-back-git-status"
SAMPLE_AFFECTED_FILE = "test_events_emit_failures"


def render_section_15(**overrides: str) -> str:
    kwargs = {
        "issue_number": SAMPLE_N,
        "slug": SAMPLE_SLUG,
        "affected_file": SAMPLE_AFFECTED_FILE,
    }
    kwargs.update(overrides)
    return ros.render_snippet(SECTION_15_TEMPLATE, **kwargs)


def test_section_15_sample_renders_expected_commands() -> None:
    """Acceptance drill: §1.5 + the issue's exact sample values."""
    rendered = render_section_15()
    assert "cd ../worktrees/issue-370-pre-report-back-git-status" in rendered
    assert ".venv/bin/pytest tests/test_events_emit_failures.py -q" in rendered
    assert not RESIDUAL_TOKEN_RE.search(rendered)
    assert "#M" not in rendered


def test_section_15_affected_file_accepts_full_path() -> None:
    """extractFileRefs-style input (tests/test_X.py) renders identically."""
    rendered = render_section_15(affected_file="tests/test_events_emit_failures.py")
    assert ".venv/bin/pytest tests/test_events_emit_failures.py -q" in rendered
    assert not RESIDUAL_TOKEN_RE.search(rendered)


def test_section_15_rendered_output_is_valid_bash() -> None:
    rendered = render_section_15()
    proc = subprocess.run(
        ["bash", "-n"],
        input=rendered,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"bash -n rejected rendered snippet: {proc.stderr}"


def _make_scratch_environment(tmp_path: Path) -> tuple[Path, Path]:
    """Build the §1.5 execution context: cwd + a clean sibling worktree.

    Returns ``(cwd, worktree)`` where ``cwd/../worktrees/issue-370-...``
    resolves to ``worktree`` — a committed git repo with a stub
    ``.venv/bin/pytest`` — so every rendered command in §1.5 succeeds.
    """
    worktrees = tmp_path / "worktrees"
    worktree = worktrees / f"issue-{SAMPLE_N}-{SAMPLE_SLUG}"
    (worktree / ".venv" / "bin").mkdir(parents=True)
    (worktree / "tests").mkdir()
    pytest_stub = worktree / ".venv" / "bin" / "pytest"
    pytest_stub.write_text('#!/bin/sh\necho "pytest-stub: $*"\nexit 0\n')
    pytest_stub.chmod(0o755)
    (worktree / "tests" / f"{SAMPLE_AFFECTED_FILE}.py").write_text("# stub\n")
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "-q", str(worktree)],
        check=True,
        env=GIT_ENV,
    )
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True, env=GIT_ENV)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=worktree,
        check=True,
        env=GIT_ENV,
    )
    pytest_stub = worktree / ".venv" / "bin" / "pytest"
    pytest_stub.write_text('#!/bin/sh\necho "pytest-stub: $*"\nexit 0\n')
    pytest_stub.chmod(0o755)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    return cwd, worktree


def test_section_15_rendered_snippet_executes_green(tmp_path: Path) -> None:
    """The rendered §1.5 is a *working* bash command end-to-end."""
    cwd, _worktree = _make_scratch_environment(tmp_path)
    proc = subprocess.run(
        ["bash", "-c", render_section_15()],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**GIT_ENV, "PATH": os.environ["PATH"]},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert f"pytest-stub: tests/{SAMPLE_AFFECTED_FILE}.py -q" in proc.stdout


def test_section_15_manual_drill_uncommitted_changes_abort(tmp_path: Path) -> None:
    """The §1.5 manual drill: an untracked .junk file must exit 1."""
    cwd, worktree = _make_scratch_environment(tmp_path)
    (worktree / ".junk").write_text("leftover\n")
    proc = subprocess.run(
        ["bash", "-c", render_section_15()],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={**GIT_ENV, "PATH": os.environ["PATH"]},
        check=False,
    )
    assert proc.returncode == 1
    assert "uncommitted changes" in proc.stdout


def test_section_5_next_issue_substitutes_real_issue() -> None:
    rendered = ros.render_snippet(
        SECTION_5_TEMPLATE,
        issue_number="365",
        slug="pr-body-scope-guard",
        pr_number="412",
        next_issue="383",
    )
    assert 'NEXT_ISSUE="#383"' in rendered
    assert "gh pr edit 412 --body" in rendered
    assert "#M" not in rendered
    assert not RESIDUAL_TOKEN_RE.search(rendered)


@pytest.mark.parametrize("next_issue", ["", "n/a", "N/A"], ids=["omitted", "na", "na-upper"])
def test_section_5_next_issue_defaults_to_na(next_issue: str) -> None:
    rendered = ros.render_snippet(
        SECTION_5_TEMPLATE,
        issue_number="365",
        slug="pr-body-scope-guard",
        pr_number="412",
        next_issue=next_issue,
    )
    assert 'NEXT_ISSUE="n/a"' in rendered
    assert "#M" not in rendered


def test_worktree_directory_name_composition() -> None:
    """Mapping 5: issue-{N}-{slug} composes from the atomic substitutions."""
    rendered = ros.render_snippet(
        "ls issue-{N}-{slug}", issue_number=SAMPLE_N, slug=SAMPLE_SLUG
    )
    assert rendered == "ls issue-370-pre-report-back-git-status"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Hyphenated words are one word: "Pre-report" is the first word.
        ("Pre-report back git status check", "pre-report-back-git"),
        (
            "Sub-agent template: pre-report-back git status check",
            "sub-agent-template-pre-report-back",
        ),
        ("Redis client read-only queue depth probe?", "redis-client-read-only"),
    ],
)
def test_derive_slug_first_three_words(title: str, expected: str) -> None:
    assert ros.derive_slug(title) == expected


@pytest.mark.parametrize(
    "value",
    [
        "test_events_emit_failures",
        "test_events_emit_failures.py",
        "tests/test_events_emit_failures.py",
    ],
)
def test_normalize_affected_file_reduces_to_stem(value: str) -> None:
    assert ros.normalize_affected_file(value) == "test_events_emit_failures"


@pytest.mark.parametrize(
    ("template", "kwargs", "match"),
    [
        ("cd {N}", {"issue_number": "abc", "slug": "x"}, "positive issue number"),
        ("cd {PR_NUMBER}", {"issue_number": "370", "slug": "x"}, "unresolved placeholders"),
        ("echo {count}", {"issue_number": "1", "slug": "x"}, "unresolved placeholders"),
        ("#M", {"issue_number": "1", "slug": "x", "next_issue": "zero"}, "positive issue number"),
    ],
    ids=["bad-issue-number", "missing-pr-number", "unknown-token", "bad-next-issue"],
)
def test_render_fails_loudly(template: str, kwargs: dict[str, str], match: str) -> None:
    """No silent partial renders — bad input raises before output exists."""
    with pytest.raises(ValueError, match=match):
        ros.render_snippet(template, **kwargs)


def test_cli_renders_sample_snippet() -> None:
    template = (
        "cd ../worktrees/issue-{N}-{slug}\n"
        ".venv/bin/pytest tests/test_{affected_file}.py -q\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--template",
            template,
            "--issue-number",
            SAMPLE_N,
            "--slug",
            SAMPLE_SLUG,
            "--affected-file",
            f"tests/{SAMPLE_AFFECTED_FILE}.py",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "cd ../worktrees/issue-370-pre-report-back-git-status\n" in proc.stdout
    assert ".venv/bin/pytest tests/test_events_emit_failures.py -q\n" in proc.stdout
    assert "#M" not in proc.stdout


def test_cli_derives_slug_from_title() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--template",
            "ls issue-{N}-{slug}",
            "--issue-number",
            SAMPLE_N,
            "--title",
            "Pre-report back git status check",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ls issue-370-pre-report-back-git"


def test_cli_exits_nonzero_on_unresolved_placeholder() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--template",
            "gh pr edit {PR_NUMBER}",
            "--issue-number",
            SAMPLE_N,
            "--slug",
            SAMPLE_SLUG,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "unresolved placeholders" in proc.stderr
