"""End-to-end replay of the wave-orchestrator state machine (issue #381).

The orchestrator's pipeline (Phase 0 pre-flight → 1 discover → 2 plan →
3 execute → 4 CI+merge → resume) previously had only per-step unit
tests: the wave-planner tested in isolation (node), auto-close tested in
isolation, verify_issues_closed.sh tested in isolation. Nothing covered
the integration, and the two failure modes observed in the 2026-08-20
cycle — sub-agents reconstructing a corrupted ``wave-state.json``
mid-cycle, and close+reopen retrigger episodes — had no regression
coverage at all.

This module drives the deterministic harness in
``tests/_wave_orchestrator_harness.py`` over the canonical sample
fixture ``tests/fixtures/wave-orchestrator/sample-wave.json`` (3 issues
→ 1 wave, ascending merge order, one AGENTS.md count-line rebase, one
corruption+resume episode) and asserts:

* the wave plan matches the snapshot,
* the PR creation sequence (order, titles, bodies incl. the #382-rendered
  Scope guard with ``NEXT_ISSUE`` substitution, base branch) matches,
* the merge order is ascending and the count-line conflict goes through
  the documented rebase → force-push → close/reopen retrigger flow,
* the final ``wave-state.json`` shape matches the snapshot,
* ``--dry-run`` prints the plan and makes zero gh/git calls,
* a crashed orchestrator (truncated state + stale temp) is recovered by
  the documented resume procedure, including stale-worktree cleanup and
  the #289 missed-issue-close gap,
* (when node is available) the snapshot ``wave-planner.js`` produces the
  same plan as the Python harness, and
* (when bash is available) the snapshot ``verify_issues_closed.sh``
  passes/fails correctly through a fake ``gh`` executable on PATH, and
* (issue #379) the wave-numbered skill-snapshot naming convention
  (``SKILL.wave-<N>.md``) lets two concurrent skill-touching waves rebase
  clean against real git while the legacy single-name pattern reproduces
  the add/add conflict, plus the harness FakeGit model mirrors both.

No network; every external command is doubled by the harness.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import _wave_orchestrator_harness as harness_mod
from _wave_orchestrator_harness import (
    DEFAULT_FIXTURE,
    WAVE_PLANNER_JS,
    WaveOrchestratorHarness,
    load_fixture,
)

HAS_NODE = shutil.which("node") is not None
HAS_BASH = shutil.which("bash") is not None


@pytest.fixture()
def fixture() -> dict:
    return load_fixture(DEFAULT_FIXTURE)


@pytest.fixture()
def replay(fixture: dict, tmp_path: Path) -> WaveOrchestratorHarness:
    """A completed clean-run replay (run once per test that needs it)."""
    orchestrator = WaveOrchestratorHarness.fresh(fixture, tmp_path)
    orchestrator.run()
    return orchestrator


@pytest.fixture()
def resumed(fixture: dict, tmp_path: Path) -> WaveOrchestratorHarness:
    """A completed crash+resume replay."""
    orchestrator = WaveOrchestratorHarness.crashed(fixture, tmp_path)
    orchestrator.resume()
    return orchestrator


# ---------------------------------------------------------------------------
# Fixture integrity — the sample must stay self-consistent or every other
# assertion below is meaningless.
# ---------------------------------------------------------------------------


class TestSampleFixture:
    def test_shape_is_canonical_three_issue_single_wave(self, fixture: dict) -> None:
        assert [issue["number"] for issue in fixture["issues"]] == [401, 402, 403]
        expected = fixture["expected"]
        assert expected["wave_plan"]["total_waves"] == 1
        assert expected["wave_plan"]["waves"][0]["issues"] == [401, 402, 403]
        for section in ("pr_creation_sequence", "merge_order", "final_state"):
            assert section in expected, f"fixture missing expected.{section}"
        assert "crash_episode" in fixture

    def test_branch_names_follow_slug_convention(self, fixture: dict) -> None:
        derive_slug = harness_mod.render_orchestrator_snippet.derive_slug
        titles = {issue["number"]: issue["title"] for issue in fixture["issues"]}
        for number, agent in fixture["sub_agents"].items():
            slug = derive_slug(titles[int(number)])
            branch = f"fix/issue-{number}-{slug}"
            assert branch in fixture["expected"]["final_state"]["issues"][number][
                "branch"
            ]
            assert agent["affected_file"].startswith("tests/test_")

    def test_count_line_overlap_exists_between_401_and_402_only(
        self, fixture: dict
    ) -> None:
        """The planner cannot see it (not in issue text), but the two test-adding
        branches both edit AGENTS.md — the count-line rebase trigger."""
        files = {
            number: set(agent["files_changed"])
            for number, agent in fixture["sub_agents"].items()
        }
        assert files["401"] & files["402"] == {"AGENTS.md"}
        assert files["401"] & files["403"] == set()
        assert files["402"] & files["403"] == set()

    def test_no_planner_visible_conflicts_in_issue_text(self, fixture: dict) -> None:
        """The 3 issues share no extracted files/labels → planner groups them
        into one wave (the rebase then comes from the invisible overlap)."""
        analyzed = [harness_mod.analyze_issue(issue) for issue in fixture["issues"]]
        for i in range(len(analyzed)):
            for j in range(i + 1, len(analyzed)):
                assert not set(analyzed[i]["affected_files"]) & set(
                    analyzed[j]["affected_files"]
                )
                assert not set(analyzed[i]["labels"]) & set(analyzed[j]["labels"])


# ---------------------------------------------------------------------------
# (a) Wave plan
# ---------------------------------------------------------------------------


class TestWavePlan:
    def test_python_planner_reproduces_expected_plan(self, fixture: dict) -> None:
        plan = harness_mod.plan_waves(fixture["issues"])
        expected = fixture["expected"]["wave_plan"]
        assert plan["total_issues"] == expected["total_issues"]
        assert plan["total_waves"] == expected["total_waves"]
        assert [
            [issue["number"] for issue in wave["issues"]] for wave in plan["waves"]
        ] == [wave["issues"] for wave in expected["waves"]]

    def test_replay_records_expected_plan(self, replay: WaveOrchestratorHarness) -> None:
        result = replay.result
        assert result.plan["total_waves"] == 1
        assert [issue["number"] for issue in result.plan["waves"][0]["issues"]] == [
            401,
            402,
            403,
        ]

    @pytest.mark.skipif(not HAS_NODE, reason="node not available (CI has no setup-node)")
    def test_snapshot_node_planner_agrees_with_python_plan(self, fixture: dict) -> None:
        """Cross-check: the snapshot wave-planner.js must produce the same
        single-wave grouping as the harness planner on the sample fixture."""
        with tempfile.TemporaryDirectory() as tmp:
            issues_file = Path(tmp) / "issues.json"
            issues_file.write_text(json.dumps(fixture["issues"]), encoding="utf-8")
            completed = subprocess.run(
                ["node", str(WAVE_PLANNER_JS), str(issues_file)],
                capture_output=True,
                text=True,
                check=True,
            )
        node_plan = json.loads(completed.stdout)
        python_plan = harness_mod.plan_waves(fixture["issues"])
        node_waves = [
            [issue["number"] for issue in wave["issues"]] for wave in node_plan["waves"]
        ]
        python_waves = [
            [issue["number"] for issue in wave["issues"]]
            for wave in python_plan["waves"]
        ]
        assert node_waves == python_waves == [[401, 402, 403]]
        assert node_plan["_meta"]["filtered_closed"] == 0


# ---------------------------------------------------------------------------
# (b) PR creation sequence
# ---------------------------------------------------------------------------


class TestPrCreationSequence:
    def test_sequence_matches_snapshot(
        self, fixture: dict, replay: WaveOrchestratorHarness
    ) -> None:
        result = replay.result
        expected = fixture["expected"]["pr_creation_sequence"]
        assert len(result.pr_creation_sequence) == len(expected) == 3
        for got, want in zip(result.pr_creation_sequence, expected, strict=True):
            assert got["issue"] == want["issue"]
            assert got["pr"] == want["pr"], "PR numbers must be assigned in order"
            assert got["title"] == want["title"]
            assert got["initial_body"] == want["initial_body"]
            assert got["final_body"] == want["final_body"]
            assert got["base"] == want["base"] == "develop"

    def test_scope_guard_renders_next_issue_never_placeholder(
        self, replay: WaveOrchestratorHarness
    ) -> None:
        """The #382 contract: #M never survives — each body references the
        next-priority issue as #<n>, the last one renders n/a."""
        bodies = [record["final_body"] for record in replay.result.pr_creation_sequence]
        assert "#M" not in "".join(bodies)
        assert "#402 owns the follow-up area." in bodies[0]
        assert "#403 owns the follow-up area." in bodies[1]
        assert "n/a owns the follow-up area." in bodies[2]
        for record in replay.result.pr_creation_sequence:
            assert "Scope guard:" in record["final_body"]

    def test_pre_pr_snippets_are_fully_rendered(self, replay: WaveOrchestratorHarness):
        """Phase 3c §1.5: the rendered pre-PR verification targets the right
        test file per issue — no {placeholders} survive (#382)."""
        snippets = replay.result.pre_pr_verifications
        assert len(snippets) == 3
        assert ".venv/bin/pytest tests/test_metrics_endpoint.py -q" in snippets[0]
        assert ".venv/bin/pytest tests/test_worker_recycler.py -q" in snippets[1]
        assert ".venv/bin/pytest tests/test_redis_client.py -q" in snippets[2]
        for snippet in snippets:
            assert "{N}" not in snippet and "{slug}" not in snippet
            assert "{affected_file}" not in snippet

    def test_prs_created_before_any_merge(self, replay: WaveOrchestratorHarness) -> None:
        """SKILL.md: do not proceed to Phase 4 until every PR in the wave
        exists — all three `pr create` calls precede the first `pr merge`."""
        calls = replay.result.gh_calls
        first_merge = next(
            index for index, call in enumerate(calls) if call[:2] == ("pr", "merge")
        )
        creates = [
            index for index, call in enumerate(calls) if call[:2] == ("pr", "create")
        ]
        assert len(creates) == 3
        assert max(creates) < first_merge


# ---------------------------------------------------------------------------
# (c) Merge order + the count-line rebase episode
# ---------------------------------------------------------------------------


class TestMergeOrder:
    def test_merge_order_is_ascending_issue_number(
        self, fixture: dict, replay: WaveOrchestratorHarness
    ) -> None:
        result = replay.result
        expected = fixture["expected"]["merge_order"]
        assert result.merge_order == [entry["issue"] for entry in expected] == [
            401,
            402,
            403,
        ]
        for got, want in zip(result.merge_events, expected, strict=True):
            assert got["issue"] == want["issue"]
            assert got["pr"] == want["pr"]
            assert got["subject"] == want["subject"]
            assert got["body"] == want["body"]

    def test_count_line_rebase_episode(
        self, fixture: dict, replay: WaveOrchestratorHarness
    ) -> None:
        """Issue #402's PR conflicts on AGENTS.md after #401 merges (both
        branches bump the test-count line). The documented protocol must
        run: rebase → resolve via re-count → force-push → close+reopen
        retrigger → merge."""
        expected_rebase = next(
            entry for entry in fixture["expected"]["merge_order"] if entry["rebase"]
        )
        assert expected_rebase["issue"] == 402

        assert len(replay.result.rebases) == 1
        rebase = replay.result.rebases[0]
        want = expected_rebase["rebase"]
        assert rebase["issue"] == 402
        assert rebase["pr"] == 502
        assert rebase["triggered_by_merge_of"] == want["triggered_by_merge_of"] == 401
        assert rebase["conflicting_files"] == want["conflicting_files"] == ["AGENTS.md"]
        assert rebase["conflict_type"] == want["conflict_type"]

        # Two leased pushes for the branch: the initial sub-agent push and
        # the post-rebase force-push — the latter must FOLLOW the rebase.
        pushes = [
            index
            for index, call in enumerate(replay.result.git_calls)
            if call[:2] == ("push", "-u")
            and "--force-with-lease" in call
            and call[3] == "fix/issue-402-document-the-worker"
        ]
        assert len(pushes) == 2
        rebase_continue = next(
            index
            for index, call in enumerate(replay.result.git_calls)
            if call == ("rebase", "--continue")
        )
        assert pushes[0] < rebase_continue < pushes[1]

        # Retrigger: close then reopen on PR 502 only.
        assert replay.result.retriggers == [
            {"pr": 502, "issue": 402, "sequence": ["close", "reopen"]}
        ]
        pr_502_tail = [
            call
            for call in replay.result.gh_calls
            if call[:3] in (("pr", "close", "502"), ("pr", "reopen", "502"))
        ]
        assert pr_502_tail == [
            ("pr", "close", "502"),
            ("pr", "reopen", "502"),
        ]

    def test_issues_401_and_403_merge_without_conflict(
        self, replay: WaveOrchestratorHarness
    ) -> None:
        assert [entry["issue"] for entry in replay.result.rebases] == [402]
        # No close/reopen retrigger anywhere except PR 502.
        assert all(entry["pr"] == 502 for entry in replay.result.retriggers)

    def test_all_worktrees_cleaned_and_branches_deleted(
        self, replay: WaveOrchestratorHarness
    ) -> None:
        assert not any(replay.git.worktrees.values())
        assert replay.git.local_branches == set()
        assert replay.git.remote_branches == set()
        # Every merge's cleanup ends with a prune (worktree remove BEFORE
        # branch delete is asserted implicitly by the calls being recorded
        # in that order per merge in _merge_one).
        assert ("worktree", "prune") in replay.result.git_calls

    def test_all_issues_closed_after_phase_4c(self, replay: WaveOrchestratorHarness):
        assert replay.gh.issue_states == {401: "CLOSED", 402: "CLOSED", 403: "CLOSED"}


# ---------------------------------------------------------------------------
# (d) Final wave-state.json shape
# ---------------------------------------------------------------------------


class TestFinalState:
    def test_final_state_matches_snapshot(
        self, fixture: dict, replay: WaveOrchestratorHarness
    ) -> None:
        assert replay.result.final_state == fixture["expected"]["final_state"]

    def test_state_file_on_disk_is_valid_json(
        self, replay: WaveOrchestratorHarness
    ) -> None:
        state = json.loads(replay.state.path.read_text(encoding="utf-8"))
        assert state["issues"]["401"]["status"] == "merged"
        assert state["issues"]["402"]["pr"] == 502
        assert state["issues"]["403"]["worktree_cleaned"] is True

    def test_no_stale_temp_files_survive(self, replay: WaveOrchestratorHarness) -> None:
        assert replay.state.stale_temps() == []

    def test_no_partial_state_written_at_any_point(self, fixture: dict, tmp_path: Path):
        """Every mid-run read of the state file must parse — the guarantee the
        2026-08-20 mid-write corruption violated. Wraps WaveStateFile.read for
        the whole replay and asserts each observation decodes."""
        observed: list[str] = []
        original_read = harness_mod.WaveStateFile.read

        def observing_read(self: harness_mod.WaveStateFile) -> dict:
            raw = self.path.read_text(encoding="utf-8")
            observed.append(raw)  # must not raise between atomic writes
            return original_read(self)

        harness_mod.WaveStateFile.read = observing_read  # type: ignore[assignment]
        try:
            orchestrator = WaveOrchestratorHarness.fresh(fixture, tmp_path)
            orchestrator.run()
        finally:
            harness_mod.WaveStateFile.read = original_read  # type: ignore[assignment]
        assert len(observed) >= 5  # plan init + per-issue/per-merge transitions
        for raw in observed:
            json.loads(raw)


# ---------------------------------------------------------------------------
# --dry-run: prints the plan, zero gh/git calls
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_makes_no_api_calls(self, fixture: dict, tmp_path: Path) -> None:
        orchestrator = WaveOrchestratorHarness.fresh(fixture, tmp_path)
        result = orchestrator.run(dry_run=True)
        assert result.gh_calls == []
        assert result.git_calls == []
        assert result.shim_calls == []
        # No state file was written either — dry-run leaves no trace.
        assert not orchestrator.state.path.exists()
        assert orchestrator.state.stale_temps() == []

    def test_dry_run_prints_plan_bodies_and_merge_order(
        self, fixture: dict, tmp_path: Path
    ) -> None:
        orchestrator = WaveOrchestratorHarness.fresh(fixture, tmp_path)
        result = orchestrator.run(dry_run=True)
        output = "\n".join(result.output_lines)
        assert "WAVE ORCHESTRATOR — DRY RUN" in output
        assert "wave 1: #401 Add metrics histogram for soft-stop latency" in output
        assert "feat: resolve #401 — soft-stop latency histogram" in output
        assert "docs: resolve #402 — worker recycle grace period runbook" in output
        assert "#403 owns the follow-up area." in output
        assert "n/a owns the follow-up area." in output
        assert "#M" not in output
        assert "Would-be merge order (ascending issue number):" in output
        assert result.merge_order == [401, 402, 403]
        assert [record["issue"] for record in result.pr_creation_sequence] == [
            401,
            402,
            403,
        ]

    def test_cli_dry_run_flag_exits_clean_without_api_calls(
        self, fixture: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The literal --dry-run flag: runnable entrypoint, prints the plan,
        exit 0, and the harness never touched gh/git (all fakes stayed at
        zero calls — enforced by pointing the harness at a fresh tmp root)."""
        argv = [
            sys.executable,
            str(Path(harness_mod.__file__)),
            "--fixture",
            str(DEFAULT_FIXTURE),
            "--dry-run",
        ]
        completed = subprocess.run(
            argv, capture_output=True, text=True, check=True, cwd=tmp_path
        )
        assert "WAVE ORCHESTRATOR — DRY RUN" in completed.stdout
        assert "wave 1: #401" in completed.stdout
        assert "DRY RUN complete — no API calls made." in completed.stdout


# ---------------------------------------------------------------------------
# Corruption + resume (the 2026-08-20 failure mode)
# ---------------------------------------------------------------------------


class TestCorruptionResume:
    def test_truncated_state_is_detected_as_corrupt(
        self, fixture: dict, tmp_path: Path
    ) -> None:
        orchestrator = WaveOrchestratorHarness.crashed(fixture, tmp_path)
        with pytest.raises(harness_mod.StateCorruptionError):
            orchestrator.state.read()
        assert orchestrator.state.stale_temps() != []

    def test_resume_rebuilds_state_from_ground_truth(
        self, fixture: dict, tmp_path: Path
    ) -> None:
        orchestrator = WaveOrchestratorHarness.crashed(fixture, tmp_path)
        rebuilt = orchestrator._rebuild_state_from_ground_truth()
        assert rebuilt == fixture["crash_episode"]["expected_rebuilt_state"]

    def test_resume_restores_correct_state_and_finishes(
        self, fixture: dict, resumed: WaveOrchestratorHarness
    ) -> None:
        result = resumed.result
        assert result.final_state == fixture["crash_episode"]["expected_final_state"]
        # The stale temp from the interrupted write is gone.
        assert resumed.state.stale_temps() == []
        assert resumed.state.read()["issues"]["401"]["worktree_cleaned"] is True

    def test_resume_completes_stale_worktree_cleanup(
        self, fixture: dict, resumed: WaveOrchestratorHarness
    ) -> None:
        """Issue #401 was merged pre-crash but never cleaned: resume step (a)
        must remove its worktree, delete local+remote branch, prune."""
        git = resumed.git
        assert not any(git.worktrees.values())
        assert git.local_branches == set()
        assert git.remote_branches == set()
        cleanup = [call for call in resumed.result.git_calls if call[0] in (
            "worktree",
            "branch",
        )]
        assert ("branch", "-d", "fix/issue-401-add-metrics-histogram") in cleanup
        assert (
            "push",
            "origin",
            "--delete",
            "fix/issue-401-add-metrics-histogram",
        ) in resumed.result.git_calls

    def test_resume_closes_issue_401_missed_by_github_autoclose(
        self, fixture: dict, resumed: WaveOrchestratorHarness
    ) -> None:
        """The #289/#961 gap: PR #501 merged but GitHub left issue #401 OPEN.
        Resume must run Phase 4c (auto-close via the real helper, then the
        snapshot verify script) and close it."""
        assert resumed.gh.issue_states == {
            401: "CLOSED",
            402: "CLOSED",
            403: "CLOSED",
        }
        assert any(
            call[:3] == ("issue", "close", "401") for call in resumed.result.gh_calls
        )
        assert any(401 in entry["closed"] for entry in resumed.result.auto_closed)

    def test_resume_replays_the_same_merge_order(
        self, fixture: dict, resumed: WaveOrchestratorHarness
    ) -> None:
        assert resumed.result.merge_order == [402, 403]  # 401 already merged
        # The resumed PR for #402 keeps the documented body shape.
        resumed_402 = next(
            record
            for record in resumed.result.pr_creation_sequence
            if record["issue"] == 402
        )
        assert resumed_402["pr"] == 504
        assert "#403 owns the follow-up area." in resumed_402["final_body"]
        assert resumed_402["base"] == "develop"

    def test_resume_reruns_count_line_rebase_on_new_pr(
        self, resumed: WaveOrchestratorHarness
    ) -> None:
        """The resumed #402 PR (504) still conflicts with develop (which now
        contains #401's AGENTS.md bump) — the rebase + close/reopen episode
        must replay on the NEW PR number."""
        assert resumed.result.rebases == [
            {
                "issue": 402,
                "pr": 504,
                "triggered_by_merge_of": 401,
                "conflicting_files": ["AGENTS.md"],
                "conflict_type": "count-line",
                "resolution": "re-count (accept develop's higher test/file counts)",
            }
        ]
        assert resumed.result.retriggers == [
            {"pr": 504, "issue": 402, "sequence": ["close", "reopen"]}
        ]

    def test_full_replay_then_simulated_crash_midway_recovers(
        self, fixture: dict, tmp_path: Path
    ) -> None:
        """Crash-during-atomic-write never corrupts: simulate the crash by
        running the full replay, then verifying every intermediate state the
        atomic writer produced was parseable (the guarantee whose absence
        forced the 2026-08-20 manual reconstruction)."""
        orchestrator = WaveOrchestratorHarness.fresh(fixture, tmp_path)
        orchestrator.run()
        # The final on-disk artifact round-trips and matches the snapshot.
        on_disk = json.loads(orchestrator.state.path.read_text(encoding="utf-8"))
        assert on_disk == fixture["expected"]["final_state"]


# ---------------------------------------------------------------------------
# Snapshot script coverage (bash/node availability gated)
# ---------------------------------------------------------------------------


class TestSnapshotScripts:
    @pytest.mark.skipif(not HAS_BASH, reason="bash not available")
    def test_verify_issues_closed_script_via_gh_shim(
        self, fixture: dict, tmp_path: Path
    ) -> None:
        """The snapshot verify_issues_closed.sh driven through a fake gh
        executable on PATH: exit 0 when all linked issues are closed, exit 1
        when one remains open (the escalation guard)."""
        from _wave_orchestrator_harness import VERIFY_SCRIPT

        orchestrator = WaveOrchestratorHarness.fresh(fixture, tmp_path)
        gh = orchestrator.gh
        gh.prs[601] = {
            "number": 601,
            "body": "Closes #401\n\nScope guard: ...",
            "state": "MERGED",
        }
        gh.issue_states = {401: "CLOSED", 402: "OPEN"}
        gh.dump_transport(orchestrator.transport_path)
        env = {
            **os.environ,
            "PATH": f"{orchestrator.shim_dir}:{os.environ.get('PATH', '')}",
            "WAVE_GH_TRANSPORT": str(orchestrator.transport_path),
            "WAVE_GH_CALLS": str(orchestrator.shim_calls_path),
        }

        ok = subprocess.run(
            ["bash", str(VERIFY_SCRIPT), "601"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert ok.returncode == 0
        assert "all linked issues are closed" in (ok.stderr + ok.stdout)

        gh.prs[602] = {
            "number": 602,
            "body": "Closes #402",
            "state": "MERGED",
        }
        gh.dump_transport(orchestrator.transport_path)
        failing = subprocess.run(
            ["bash", str(VERIFY_SCRIPT), "602"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert failing.returncode == 1
        assert "#402" in failing.stderr

    @pytest.mark.skipif(not HAS_NODE, reason="node not available (CI has no setup-node)")
    def test_node_planner_dry_run_outputs_analysis_without_waves(
        self, fixture: dict, tmp_path: Path
    ) -> None:
        issues_file = tmp_path / "issues.json"
        issues_file.write_text(json.dumps(fixture["issues"]), encoding="utf-8")
        completed = subprocess.run(
            ["node", str(WAVE_PLANNER_JS), str(issues_file), "--dry-run"],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(completed.stdout)
        assert payload["_meta"]["mode"] == "dry-run"
        assert payload["_meta"]["total_issues"] == 3
        assert "waves" not in payload


# ---------------------------------------------------------------------------
# Skill-snapshot naming convention (issue #379): wave-numbered snapshot
# names (SKILL.wave-<N>.md) make concurrent skill-touching waves
# collision-free; the legacy single-name pattern (every wave adding
# docs/skill-snapshot/SKILL.md) is the proven add/add failure mode.
# ---------------------------------------------------------------------------

HAS_GIT = shutil.which("git") is not None

GIT_TEST_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.email=wave-orchestrator@test",
            "-c",
            "user.name=wave-orchestrator-test",
            "-C",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=GIT_TEST_ENV,
    )
    if check and completed.returncode != 0:
        raise AssertionError(f"git {args!r} failed:\n{completed.stderr}")
    return completed


def _snapshot_repo(tmp_path: Path) -> tuple[Path, str]:
    """A real git repo on branch ``develop`` with one base commit.

    Returns ``(repo, base_sha)`` — the shared pre-merge point both wave
    branches cut from, which is the 2026-08-20 cycle shape: wave 2's
    worktree existed before wave 1's snapshot merged.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "checkout", "-b", "develop")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _commit_snapshot(repo: Path, name: str, content: str) -> None:
    snapshot = repo / "docs" / "skill-snapshot" / name
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"snapshot {name}")


class TestSkillSnapshotNumberedNames:
    @pytest.mark.skipif(not HAS_GIT, reason="git not available")
    def test_numbered_names_rebase_clean_across_waves(
        self, tmp_path: Path
    ) -> None:
        """The #379 acceptance drill against real git: wave 1 adds
        ``SKILL.wave-1.md`` and merges; wave 2 branched from the SAME
        pre-merge base adds ``SKILL.wave-2.md`` and rebases onto develop —
        zero conflicts, and both full snapshots coexist on develop."""
        repo, base = _snapshot_repo(tmp_path)

        _git(repo, "checkout", "-b", "fix/issue-401-wave1-skill")
        _commit_snapshot(repo, "SKILL.wave-1.md", "# phase 3c v1\n")
        _git(repo, "checkout", "develop")
        _git(repo, "merge", "--no-ff", "fix/issue-401-wave1-skill")

        _git(repo, "checkout", "-b", "fix/issue-402-wave2-skill", base)
        _commit_snapshot(repo, "SKILL.wave-2.md", "# phase 3c v2\n")
        rebase = _git(repo, "rebase", "develop", check=False)
        assert rebase.returncode == 0, rebase.stderr
        assert _git(repo, "ls-files", "-u").stdout == ""  # zero unmerged paths

        _git(repo, "checkout", "develop")
        _git(repo, "merge", "--no-ff", "fix/issue-402-wave2-skill")
        for name in ("SKILL.wave-1.md", "SKILL.wave-2.md"):
            assert (repo / "docs" / "skill-snapshot" / name).exists()

    @pytest.mark.skipif(not HAS_GIT, reason="git not available")
    def test_legacy_single_name_add_add_conflicts(self, tmp_path: Path) -> None:
        """The failure mode #379 exists to kill: two waves adding the bare
        ``SKILL.md`` (each carrying its own skill-home content) produce a
        real add/add conflict at rebase — proving the regression test
        above guards a real failure, not a hypothetical."""
        repo, base = _snapshot_repo(tmp_path)

        _git(repo, "checkout", "-b", "fix/issue-401-legacy-skill")
        _commit_snapshot(repo, "SKILL.md", "# phase 3c v1\n")
        _git(repo, "checkout", "develop")
        _git(repo, "merge", "--no-ff", "fix/issue-401-legacy-skill")

        _git(repo, "checkout", "-b", "fix/issue-402-legacy-skill", base)
        _commit_snapshot(repo, "SKILL.md", "# phase 3c v2 (edited)\n")
        rebase = _git(repo, "rebase", "develop", check=False)
        assert rebase.returncode != 0
        unmerged = _git(repo, "ls-files", "-u").stdout.splitlines()
        assert unmerged, "expected an add/add conflict on the snapshot path"
        assert {line.rsplit("/", 1)[-1] for line in unmerged} == {"SKILL.md"}
        stages = {int(line.split()[2]) for line in unmerged}
        assert {2, 3} <= stages  # ours + theirs → add/add, not a base edit
        _git(repo, "rebase", "--abort")

    def test_harness_model_legacy_names_collide(self) -> None:
        """FakeGit's conflict model (branch files ∩ develop files) mirrors
        the legacy collision: both waves snapshot the bare SKILL.md."""
        git = harness_mod.FakeGit()
        git.branch_files["fix/issue-401-legacy"] = {"docs/skill-snapshot/SKILL.md"}
        git.branch_files["fix/issue-402-legacy"] = {"docs/skill-snapshot/SKILL.md"}
        git.merge_into_develop("fix/issue-401-legacy")
        assert git.conflicting_files("fix/issue-402-legacy") == [
            "docs/skill-snapshot/SKILL.md"
        ]

    def test_harness_model_numbered_names_are_disjoint(self) -> None:
        git = harness_mod.FakeGit()
        git.branch_files["fix/issue-401-numbered"] = {
            "docs/skill-snapshot/SKILL.wave-1.md"
        }
        git.branch_files["fix/issue-402-numbered"] = {
            "docs/skill-snapshot/SKILL.wave-2.md"
        }
        git.merge_into_develop("fix/issue-401-numbered")
        assert git.conflicting_files("fix/issue-402-numbered") == []

    def test_committed_snapshots_follow_numbered_convention(self) -> None:
        """The repo-side artifact of the convention: at least one
        wave-numbered snapshot exists and the newest one carries the §3a
        copy rule (skill-home SKILL.md Phase 3a, mirrored verbatim)."""
        numbered = sorted(
            harness_mod.SNAPSHOT_DIR.glob("SKILL.wave-*.md"),
            key=lambda path: int(path.stem.removeprefix("SKILL.wave-")),
        )
        assert numbered, "no wave-numbered SKILL snapshot committed (issue #379)"
        newest = numbered[-1].read_text(encoding="utf-8")
        assert "SKILL.wave-${WAVE}.md" in newest
        assert "issue #379" in newest.lower()
