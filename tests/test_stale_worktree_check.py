"""Stale-worktree pre-flight check regression tests (issue #383).

Wave orchestration leaves sibling worktree directories under
``../worktrees/`` whose branches were deleted on merge (or which were
never registered with this repo's git metadata at all — e.g. worktrees
of *other* projects sharing the parent directory). ``git worktree
prune`` cannot see them, so they accumulate on disk and a new
orchestrator may mistake the leftovers for in-progress work.

``scripts/check_stale_worktrees.sh`` is the Phase-0 visibility tool the
issue asks for: it lists every ``issue-*`` directory whose derived
branch name (``fix/``, ``feat/``, ``docs/``, ``chore/`` + dirname)
matches no local branch and which is not registered in
``git worktree list``. List mode always exits 0 and never deletes
anything; deletion requires the explicit ``--prune-stale-worktrees
--yes`` pair (scope guard: no auto-delete without confirmation).

These tests exercise the script end-to-end against a throwaway git
repository (``git init`` in ``tmp_path``) with fake worktree
directories, covering the acceptance criteria: stale-dir detection,
non-stale dirs (branch exists / registered worktree), the threshold
WARNING line, and that ``--prune-stale-worktrees`` WITHOUT ``--yes``
deletes nothing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_stale_worktrees.sh"


def _make_repo(tmp_path: Path) -> Path:
    """Create a committed git repo so branches/worktrees can be created."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "-q", str(repo)],
        check=True,
        env=env,
    )
    (repo / "README.md").write_text("scratch repo\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
        env=env,
    )
    return repo


def _run_script(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def _set_old_mtime(path: Path) -> None:
    """Make ``path`` unambiguously older than any --min-age-days floor."""
    os.utime(path, (0, 0))


def test_stale_dir_without_matching_branch_is_listed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    worktrees = tmp_path / "worktrees"
    (worktrees / "issue-999-ghost").mkdir(parents=True)

    result = _run_script(repo, "--worktrees-dir", str(worktrees))

    assert result.returncode == 0
    assert "STALE worktree: " in result.stdout
    assert "issue-999-ghost" in result.stdout
    assert "(no matching branch)" in result.stdout
    assert "Stale worktree directories: 1 (threshold: 5)" in result.stdout


def test_dir_with_branch_or_registration_is_not_stale(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    # Backed by a live local branch (the conventional fix/ prefix).
    (worktrees / "issue-42-live").mkdir()
    subprocess.run(["git", "branch", "fix/issue-42-live"], cwd=repo, check=True)
    # Registered in git worktree list (detached — NO branch backs it),
    # which must still count as live per the issue's rule 1.
    subprocess.run(
        ["git", "worktree", "add", "--detach", "-q", str(worktrees / "issue-77-registered")],
        cwd=repo,
        check=True,
    )

    result = _run_script(repo, "--worktrees-dir", str(worktrees))

    assert result.returncode == 0
    assert "STALE worktree" not in result.stdout
    assert "issue-42-live" not in result.stdout
    assert "issue-77-registered" not in result.stdout
    assert "Stale worktree directories: 0" in result.stdout


def test_threshold_warning_emitted_only_when_count_exceeds_threshold(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    worktrees = tmp_path / "worktrees"
    for i in range(3):
        (worktrees / f"issue-100{i}-ghost").mkdir(parents=True)

    below = _run_script(repo, "--worktrees-dir", str(worktrees), "--threshold", "5")
    above = _run_script(repo, "--worktrees-dir", str(worktrees), "--threshold", "2")

    assert below.returncode == 0
    assert "WARNING" not in below.stdout
    assert "Stale worktree directories: 3 (threshold: 5)" in below.stdout

    assert above.returncode == 0
    assert "WARNING: 3 stale worktree directories exceed the threshold of 2" in above.stdout
    assert "BEFORE creating new worktrees" in above.stdout


def test_prune_without_yes_deletes_nothing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    worktrees = tmp_path / "worktrees"
    stale = worktrees / "issue-555-ghost"
    stale.mkdir(parents=True)
    _set_old_mtime(stale)

    result = _run_script(
        repo, "--worktrees-dir", str(worktrees), "--prune-stale-worktrees"
    )

    assert result.returncode == 0
    assert "STALE worktree: " in result.stdout
    assert (
        "NOTE: --prune-stale-worktrees given without --yes — listing only, nothing deleted."
        in result.stdout
    )
    assert "PRUNED" not in result.stdout
    assert stale.is_dir(), "scope guard violated: directory deleted without --yes"


def test_prune_with_yes_removes_only_old_stale_dirs(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    worktrees = tmp_path / "worktrees"
    old_stale = worktrees / "issue-1-old-ghost"
    fresh_stale = worktrees / "issue-2-fresh-ghost"
    old_live = worktrees / "issue-3-old-live"
    old_stale.mkdir(parents=True)
    fresh_stale.mkdir()
    old_live.mkdir()
    _set_old_mtime(old_stale)
    _set_old_mtime(old_live)
    subprocess.run(["git", "branch", "fix/issue-3-old-live"], cwd=repo, check=True)

    result = _run_script(
        repo,
        "--worktrees-dir",
        str(worktrees),
        "--prune-stale-worktrees",
        "--yes",
        "--min-age-days",
        "30",
    )

    assert result.returncode == 0
    assert not old_stale.exists(), "old stale dir should be pruned with --yes"
    assert "PRUNED stale worktree:" in result.stdout
    assert "issue-1-old-ghost" in result.stdout
    assert fresh_stale.is_dir(), "fresh stale dir must survive the min-age floor"
    assert old_live.is_dir(), "dir with matching branch must never be pruned"
    assert "Stale worktree directories: 2" in result.stdout
    assert "Pruned 1 stale worktree" in result.stdout
