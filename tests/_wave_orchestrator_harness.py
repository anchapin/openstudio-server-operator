"""Deterministic pure-Python harness for the wave-orchestrator state machine (issue #381).

The orchestrator's "implementation" is a documented *procedure* (``docs/
skill-snapshot/SKILL.md`` Phases 0-5 + ``docs/skill-snapshot/REFERENCE.md``
state-machine / resume / merge-ordering rules), not an executable in this
repo. This module re-implements that procedure as a small deterministic
state machine so ``tests/test_wave_orchestrator_e2e.py`` can replay a
sample wave end-to-end with every external command doubled:

* :class:`FakeGh` — deterministic ``gh`` double driven by the fixture
  scenario; records every invocation in order.
* :class:`FakeGit` — deterministic ``git`` double (worktrees, branches,
  develop file-set for merge-conflict modelling); records every call.
* :class:`WaveStateFile` — Python mirror of the snapshot's
  ``wave-state-helpers.sh`` semantics: atomic write-then-rename via a
  ``.wave-state.XXXXXX.json`` temp in the same directory, plus stale-temp
  detection for the mid-write corruption pattern.
* :class:`WaveOrchestratorHarness` — Phases 0-4 (+ resume) as documented.
  PR bodies and the Phase 3c §1.5 / §5 snippets are rendered with the
  *real* ``scripts/render_orchestrator_snippet.py`` (issue #382), and
  Phase 4c auto-close goes through the *real*
  ``scripts/auto_close_issues.py`` with only its ``gh`` chokepoint faked.
  Post-merge verification runs the *snapshot* copy of
  ``docs/skill-snapshot/scripts/verify_issues_closed.sh`` through a fake
  ``gh`` executable shim placed on PATH (skipped when bash is absent).

The wave-planning step is re-implemented in Python from the documented
algorithm (file-reference extraction → shared-file/shared-label conflict
graph → greedy coloring, max 3 per wave); when ``node`` is available the
e2e test additionally cross-checks the Python plan against the snapshot
``docs/skill-snapshot/scripts/wave-planner.js`` so the two cannot drift
on the sample fixture.

No network, no real ``gh``/``git``, no filesystem outside ``root``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SNAPSHOT_DIR = REPO_ROOT / "docs" / "skill-snapshot"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "wave-orchestrator"
DEFAULT_FIXTURE = FIXTURES_DIR / "sample-wave.json"
VERIFY_SCRIPT = SNAPSHOT_DIR / "scripts" / "verify_issues_closed.sh"
WAVE_PLANNER_JS = SNAPSHOT_DIR / "scripts" / "wave-planner.js"

MAX_PER_WAVE = 3
MAX_UNIQUE_DEPS_PER_WAVE = 3

# Phase 3c §5 auto-append template (SKILL.md). NEXT_ISSUE is "#M" in the
# template and must never survive rendering (issue #382).
SCOPE_GUARD_TEMPLATE = (
    "Scope guard: Do NOT touch any other area of the codebase; "
    "#M owns the follow-up area."
)

# Phase 3c §1.5 pre-PR verification snippet (SKILL.md), rendered per issue.
PRE_PR_TEMPLATE = """cd ../worktrees/issue-{N}-{slug}
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Sub-agent left uncommitted changes in worktree; aborting PR creation"
  exit 1
fi
.venv/bin/pytest tests/test_{affected_file}.py -q"""


def _load_script_module(name: str, path: Path) -> Any:
    """Load ``scripts/<name>.py`` by path, sharing one instance per name."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


auto_close_issues = _load_script_module(
    "auto_close_issues", SCRIPTS_DIR / "auto_close_issues.py"
)
render_orchestrator_snippet = _load_script_module(
    "render_orchestrator_snippet", SCRIPTS_DIR / "render_orchestrator_snippet.py"
)

# ---------------------------------------------------------------------------
# Deterministic wave planning (mirrors wave-planner.js on the documented
# algorithm: file-ref extraction, shared-file/shared-label conflicts,
# greedy coloring with MAX_PER_WAVE, unknown-deps cap).
# ---------------------------------------------------------------------------

_PATH_PATTERNS = [
    re.compile(r"[`'\"\[\s]([a-zA-Z0-9_./-]+/[a-zA-Z0-9_./-]+\.[a-z]{2,4})(?::\d+)?[`'\"\]\s]"),
    re.compile(r"(?<![a-zA-Z0-9_/.-])([a-zA-Z0-9_./-]+/[a-zA-Z0-9_./-]+\.[a-z]{2,4}):(\d+)"),
    re.compile(r"[`'\"]([a-zA-Z0-9_./-]+\.[a-z]{2,4})[`'\"]"),
]


def extract_file_refs(text: str) -> list[str]:
    """File paths referenced in issue text (backtick/quote/whitespace delimited)."""
    files: set[str] = set()
    for pattern in _PATH_PATTERNS:
        for match in pattern.finditer(text or ""):
            found = match.group(1)
            if "http" not in found and "://" not in found and len(found) > 3:
                files.add(found)
    return sorted(files)


def analyze_issue(issue: dict[str, Any]) -> dict[str, Any]:
    labels = [
        label["name"] if isinstance(label, dict) else str(label)
        for label in issue.get("labels") or []
    ]
    affected = extract_file_refs(f"{issue.get('title', '')}\n{issue.get('body', '')}")
    return {
        "number": issue["number"],
        "title": issue.get("title", ""),
        "labels": labels,
        "affected_files": affected,
        "has_known_deps": bool(affected),
    }


def plan_waves(issues: list[dict[str, Any]]) -> dict[str, Any]:
    """Group issues into waves per the documented conflict rules."""
    analyzed = [analyze_issue(issue) for issue in issues if issue.get("state") != "CLOSED"]
    count = len(analyzed)
    adjacency: list[set[int]] = [set() for _ in range(count)]
    for i in range(count):
        for j in range(i + 1, count):
            a, b = analyzed[i], analyzed[j]
            shares = set(a["affected_files"]) & set(b["affected_files"])
            shares_label = set(a["labels"]) & set(b["labels"])
            if shares or shares_label:
                adjacency[i].add(j)
                adjacency[j].add(i)

    colors = [-1] * count
    color_counts: list[int] = []
    for node in range(count):
        used = {colors[n] for n in adjacency[node] if colors[n] != -1}
        assigned = next(
            (
                color
                for color in range(len(color_counts))
                if color not in used and color_counts[color] < MAX_PER_WAVE
            ),
            None,
        )
        if assigned is None:
            assigned = len(color_counts)
            color_counts.append(0)
        colors[node] = assigned
        color_counts[assigned] += 1

    raw_waves: list[list[dict[str, Any]]] = []
    for wave_number in range(len(color_counts)):
        members = [analyzed[i] for i in range(count) if colors[i] == wave_number]
        if members:
            raw_waves.append(members)

    # Issue #369 unknown_deps safety net (structural cap; --no-stacking not modeled).
    final_waves: list[list[dict[str, Any]]] = []
    for wave in raw_waves:
        unknowns = [issue for issue in wave if not issue["has_known_deps"]]
        knowns = [issue for issue in wave if issue["has_known_deps"]]
        if len(unknowns) > MAX_UNIQUE_DEPS_PER_WAVE:
            final_waves.extend([unknown] for unknown in unknowns)
            if knowns:
                final_waves.append(knowns)
        else:
            final_waves.append(wave)

    return {
        "total_issues": count,
        "total_waves": len(final_waves),
        "waves": [
            {"wave": index + 1, "issues": wave_issues}
            for index, wave_issues in enumerate(final_waves)
        ],
    }


# ---------------------------------------------------------------------------
# Fake gh / Fake git
# ---------------------------------------------------------------------------


class StateCorruptionError(Exception):
    """wave-state.json is not parseable JSON (mid-write interruption)."""


@dataclass
class GhResult:
    stdout: str
    returncode: int = 0


class FakeGh:
    """Deterministic ``gh`` double; every call is recorded in ``self.calls``."""

    def __init__(
        self,
        *,
        repo: str,
        first_pr_number: int,
        ci: dict[str, Any],
        issues_payload: list[dict[str, Any]] | None = None,
    ) -> None:
        self.repo = repo
        self._next_pr = first_pr_number
        self._ci = ci
        self.prs: dict[int, dict[str, Any]] = {}
        self.issue_states: dict[int, str] = {}
        self.calls: list[tuple[str, ...]] = []
        self._issues_payload = issues_payload or []
        # Injected by the harness: pr number -> "MERGEABLE" | "CONFLICTING".
        self.mergeable_provider: Callable[[int], str] = lambda _pr: "MERGEABLE"

    # -- construction from a crash snapshot ------------------------------
    def load_snapshot(self, snapshot: dict[str, Any]) -> None:
        for pr in snapshot["prs"]:
            record = dict(pr)
            record.setdefault("mergedAt", None)
            self.prs[int(pr["number"])] = record
            self._next_pr = max(self._next_pr, int(pr["number"]) + 1)
        self.issue_states = {
            int(number): state for number, state in snapshot["issue_states"].items()
        }

    # -- invocation -------------------------------------------------------
    def run(self, args: list[str], *, check: bool = True) -> GhResult:
        self.calls.append(tuple(args))
        result = self._dispatch(args)
        if check and result.returncode != 0:
            raise RuntimeError(f"fake gh failed: {args!r} -> {result.returncode}")
        return result

    def run_gh_bound(self, args: list[str], *, check: bool = True) -> GhResult:
        """``auto_close_issues._run_gh``-compatible bound method."""
        return self.run(args, check=check)

    def _dispatch(self, args: list[str]) -> GhResult:
        head, sub = args[0], args[1]
        if head == "auth":
            return GhResult(stdout=f"Logged in to github.com account ({self.repo})\n")
        if head == "issue" and sub == "list":
            return GhResult(stdout=json.dumps(self._issues_payload))
        if head == "issue" and sub == "view":
            return GhResult(stdout=self.issue_states.get(int(args[2]), "OPEN"))
        if head == "issue" and sub == "close":
            self.issue_states[int(args[2])] = "CLOSED"
            return GhResult(stdout="")
        if head == "pr" and sub == "list":
            return GhResult(stdout=self._pr_list(args))
        if head == "pr" and sub == "create":
            return GhResult(stdout=self._pr_create(args))
        if head == "pr" and sub == "view":
            return GhResult(stdout=self._pr_view(args))
        if head == "pr" and sub == "edit":
            self.prs[int(args[2])]["body"] = args[args.index("--body") + 1]
            return GhResult(stdout="")
        if head == "pr" and sub == "checks":
            return GhResult(stdout=self._ci["output"], returncode=self._ci["returncode"])
        if head == "pr" and sub == "merge":
            return self._pr_merge(args)
        if head == "pr" and sub == "close":
            self.prs[int(args[2])]["state"] = "CLOSED"
            return GhResult(stdout="")
        if head == "pr" and sub == "reopen":
            self.prs[int(args[2])]["state"] = "OPEN"
            return GhResult(stdout="")
        raise AssertionError(f"fake gh: unhandled argv shape {args!r}")

    def _pr_list(self, args: list[str]) -> str:
        term = args[args.index("--search") + 1]
        numbers = [
            pr["number"]
            for pr in self.prs.values()
            if pr["headRefName"].startswith(term) and pr["state"] == "OPEN"
        ]
        return json.dumps(sorted(numbers))

    def _pr_create(self, args: list[str]) -> str:
        number = self._next_pr
        self._next_pr += 1
        self.prs[number] = {
            "number": number,
            "title": args[args.index("--title") + 1],
            "body": args[args.index("--body") + 1],
            "baseRefName": args[args.index("--base") + 1],
            "headRefName": args[args.index("--head") + 1],
            "state": "OPEN",
            "mergedAt": None,
        }
        return f"https://github.com/{self.repo}/pull/{number}\n"

    def _pr_view(self, args: list[str]) -> str:
        pr = self.prs[int(args[2])]
        field_name = args[args.index("--json") + 1]
        if field_name == "mergeable":
            return self.mergeable_provider(int(args[2]))
        if field_name == "mergedAt":
            return "null" if pr["mergedAt"] is None else str(pr["mergedAt"])
        return str(pr.get(field_name, ""))

    def _pr_merge(self, args: list[str]) -> GhResult:
        pr = self.prs[int(args[2])]
        pr["state"] = "MERGED"
        pr["mergedAt"] = "2026-08-20T09:00:00Z"
        # GitHub auto-close: every closing-keyword ref of the MERGE body.
        for issue_number in auto_close_issues.parse_closing_refs(
            args[args.index("--body") + 1]
        ):
            if self.issue_states.get(issue_number) == "OPEN":
                self.issue_states[issue_number] = "CLOSED"
        return GhResult(stdout="")

    # -- shim transport (verify_issues_closed.sh runs as a subprocess) ----
    def dump_transport(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "prs": {
                        str(number): {"body": pr["body"], "state": pr["state"]}
                        for number, pr in self.prs.items()
                    },
                    "issues": {
                        str(number): state for number, state in self.issue_states.items()
                    },
                }
            ),
            encoding="utf-8",
        )


# The fake ``gh`` executable placed on PATH for verify_issues_closed.sh.
# Dispatches on the argv shapes the script uses and appends every
# invocation to a calls log so tests can assert the shim was consulted.
# "{python}" is substituted at install time; every other brace is literal
# Python inside the shim, so no str.format here.
_GH_SHIM = """#!{python}
import json, os, pathlib, sys
transport = json.loads(pathlib.Path(os.environ["WAVE_GH_TRANSPORT"]).read_text())
with open(os.environ["WAVE_GH_CALLS"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:2] == ["pr", "view"]:
    pr = transport["prs"][args[2]]
    field = args[args.index("--json") + 1]
    sys.stdout.write(str(pr.get(field, "")))
elif args[:2] == ["issue", "view"]:
    sys.stdout.write(transport["issues"][args[2]])
else:
    sys.exit(f"gh shim: unhandled argv {args!r}")
"""


class FakeGit:
    """Deterministic ``git`` double: worktrees, branches, develop file-set."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.worktrees: dict[str, bool] = {}
        self.local_branches: set[str] = set()
        self.remote_branches: set[str] = set()
        self.develop_files: set[str] = set()
        self.branch_files: dict[str, set[str]] = {}
        self.subjects: dict[str, str] = {}
        self._rebased_clean: set[str] = set()
        self.cwd: str | None = None

    @staticmethod
    def branch_of_worktree(worktree: str) -> str:
        return f"fix/{worktree.rsplit('/', 1)[-1]}"

    def _branch_for_cwd(self) -> str:
        assert self.cwd is not None, "harness must set git.cwd before branch-scoped calls"
        return self.branch_of_worktree(self.cwd)

    def run(self, args: list[str]) -> GhResult:
        self.calls.append(tuple(args))
        return self._dispatch(args)

    def _dispatch(self, args: list[str]) -> GhResult:
        if args[0] == "worktree" and args[1] == "add":
            self.worktrees[args[2].rsplit("/", 1)[-1]] = True
            self.local_branches.add(args[args.index("-b") + 1])
            return GhResult(stdout="")
        if args[0] == "worktree" and args[1] == "remove":
            self.worktrees[args[2].rsplit("/", 1)[-1]] = False
            return GhResult(stdout="")
        if args[0] == "worktree" and args[1] == "prune":
            return GhResult(stdout="")
        if args[0] == "branch" and args[1] == "-d":
            self.local_branches.discard(args[2])
            return GhResult(stdout="")
        if args[0] == "push" and "--delete" in args:
            self.remote_branches.discard(args[-1])
            return GhResult(stdout="")
        if args[0] == "push" and "-u" in args:
            # shape: push -u origin <branch> --force-with-lease
            self.remote_branches.add(args[3])
            return GhResult(stdout="")
        if args[0] in ("fetch", "status"):
            return GhResult(stdout="")
        if args[0] == "log":
            return GhResult(stdout=self.subjects[self._branch_for_cwd()])
        if args[0] == "rebase" and args[1] == "--continue":
            self._rebased_clean.add(self._branch_for_cwd())
            return GhResult(stdout="")
        if args[0] == "rebase":
            branch = self._branch_for_cwd()
            if self.conflicting_files(branch):
                return GhResult(stdout="", returncode=1)
            self._rebased_clean.add(branch)
            return GhResult(stdout="")
        raise AssertionError(f"fake git: unhandled argv shape {args!r}")

    def conflicting_files(self, branch: str) -> list[str]:
        if branch in self._rebased_clean:
            return []
        return sorted(self.branch_files.get(branch, set()) & self.develop_files)

    def merge_into_develop(self, branch: str) -> None:
        self.develop_files |= self.branch_files.get(branch, set())
        self._rebased_clean.discard(branch)

    # -- construction from a crash snapshot ------------------------------
    def load_snapshot(self, disk: dict[str, Any], branches: dict[str, set[str]]) -> None:
        for worktree in disk["worktrees"]:
            self.worktrees[worktree.rsplit("/", 1)[-1]] = True
        self.local_branches = set(disk["local_branches"])
        self.remote_branches = set(disk["remote_branches"])
        self.develop_files = set(disk["develop_files"])
        self.branch_files = branches


# ---------------------------------------------------------------------------
# wave-state.json (mirrors wave-state-helpers.sh atomic semantics)
# ---------------------------------------------------------------------------


class WaveStateFile:
    """Namespaced state file with write-then-rename atomic semantics."""

    TEMP_PREFIX = ".wave-state."

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise StateCorruptionError(f"{self.path} does not exist") from exc
        except json.JSONDecodeError as exc:
            raise StateCorruptionError(f"{self.path} is corrupt: {exc}") from exc

    def write(self, state: dict[str, Any]) -> None:
        self._write_temp_then_rename(json.dumps(state, indent=2))

    def atomic_update(self, mutate: Callable[[dict[str, Any]], None]) -> None:
        state = self.read()
        mutate(state)
        self._write_temp_then_rename(json.dumps(state, indent=2))

    def _write_temp_then_rename(self, payload: str) -> None:
        handle, temp_name = tempfile.mkstemp(
            prefix=self.TEMP_PREFIX, suffix=".json", dir=self.path.parent
        )
        os.close(handle)
        temp_path = Path(temp_name)
        try:
            temp_path.write_text(payload, encoding="utf-8")
            os.replace(temp_path, self.path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    def write_raw(self, payload: str) -> None:
        """Direct non-atomic write — used ONLY to simulate the corruption."""
        self.path.write_text(payload, encoding="utf-8")

    def stale_temps(self) -> list[Path]:
        return sorted(self.path.parent.glob(f"{self.TEMP_PREFIX}*.json"))

    def remove_stale_temps(self) -> list[str]:
        removed = [entry.name for entry in self.stale_temps()]
        for entry in self.stale_temps():
            entry.unlink()
        return removed


# ---------------------------------------------------------------------------
# The orchestrator state machine
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    plan: dict[str, Any] = field(default_factory=dict)
    pr_creation_sequence: list[dict[str, Any]] = field(default_factory=list)
    merge_order: list[int] = field(default_factory=list)
    merge_events: list[dict[str, Any]] = field(default_factory=list)
    retriggers: list[dict[str, Any]] = field(default_factory=list)
    rebases: list[dict[str, Any]] = field(default_factory=list)
    pre_pr_verifications: list[str] = field(default_factory=list)
    final_state: dict[str, Any] = field(default_factory=dict)
    gh_calls: list[tuple[str, ...]] = field(default_factory=list)
    git_calls: list[tuple[str, ...]] = field(default_factory=list)
    shim_calls: list[list[str]] = field(default_factory=list)
    auto_closed: list[dict[str, Any]] = field(default_factory=list)
    output_lines: list[str] = field(default_factory=list)


def load_fixture(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class WaveOrchestratorHarness:
    """Phases 0-4 (+ documented resume) as a deterministic state machine."""

    def __init__(
        self,
        fixture: dict[str, Any],
        *,
        root: Path,
        gh: FakeGh,
        git: FakeGit,
    ) -> None:
        self.fixture = fixture
        self.meta = fixture["meta"]
        self.root = root
        self.worktrees_dir = root / "worktrees"
        self.gh = gh
        self.git = git
        slug = self.meta["repo"].split("/")[1]
        self.state = WaveStateFile(root / f"wave-state.{slug}.json")
        self.sub_agents = self._bind_slugs(fixture["sub_agents"], fixture["issues"])
        self.result = RunResult()
        self._install_gh_shim()

    # -- setup helpers ----------------------------------------------------
    def _bind_slugs(
        self, sub_agents: dict[str, Any], issues: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Derive each issue's slug via the real #382 renderer helper."""
        titles = {issue["number"]: issue["title"] for issue in issues}
        bound = {}
        for number_str, agent in sub_agents.items():
            slug = render_orchestrator_snippet.derive_slug(titles[int(number_str)])
            bound[number_str] = {**agent, "slug": slug}
            self.git.subjects[f"fix/issue-{number_str}-{slug}"] = agent["commit_subject"]
        return bound

    def _install_gh_shim(self) -> None:
        self.shim_dir = self.root / "bin"
        self.shim_dir.mkdir(parents=True, exist_ok=True)
        shim = self.shim_dir / "gh"
        shim.write_text(
            _GH_SHIM.replace("{python}", sys.executable), encoding="utf-8"
        )
        shim.chmod(0o755)
        self.transport_path = self.root / "gh-transport.json"
        self.shim_calls_path = self.root / "gh-shim-calls.jsonl"

    @classmethod
    def fresh(cls, fixture: dict[str, Any], root: Path) -> WaveOrchestratorHarness:
        gh = FakeGh(
            repo=fixture["meta"]["repo"],
            first_pr_number=fixture["meta"]["first_pr_number"],
            ci=fixture["ci_checks"],
            issues_payload=fixture["issues"],
        )
        for issue in fixture["issues"]:
            gh.issue_states[issue["number"]] = "OPEN"
        harness = cls(fixture, root=root, gh=gh, git=FakeGit())
        gh.mergeable_provider = harness._mergeable
        return harness

    @classmethod
    def crashed(cls, fixture: dict[str, Any], root: Path) -> WaveOrchestratorHarness:
        """Harness laid out per the fixture's crash_episode ground truth."""
        crash = fixture["crash_episode"]
        gh = FakeGh(
            repo=fixture["meta"]["repo"],
            first_pr_number=fixture["meta"]["first_pr_number"],
            ci=fixture["ci_checks"],
        )
        gh.load_snapshot(crash["gh_snapshot"])
        harness = cls(fixture, root=root, gh=gh, git=FakeGit())
        branch_files = {
            f"fix/issue-{number}-{agent['slug']}": set(agent["files_changed"])
            for number, agent in harness.sub_agents.items()
        }
        harness.git.load_snapshot(crash["disk"], branch_files)
        # Truncated mid-write state + a stale temp: the 2026-08-20 pattern.
        harness.state.write_raw(crash["truncated_state"])
        (root / crash["stale_temp"]).write_text(crash["truncated_state"], encoding="utf-8")
        gh.mergeable_provider = harness._mergeable
        return harness

    # -- conflict model ----------------------------------------------------
    def _mergeable(self, pr_number: int) -> str:
        head = self.gh.prs[pr_number]["headRefName"]
        return "CONFLICTING" if self.git.conflicting_files(head) else "MERGEABLE"

    def _branch(self, number: int) -> str:
        return f"fix/issue-{number}-{self.sub_agents[str(number)]['slug']}"

    def _worktree(self, number: int) -> str:
        return f"../worktrees/issue-{number}-{self.sub_agents[str(number)]['slug']}"

    def _worktree_name(self, number: int) -> str:
        return self._worktree(number).rsplit("/", 1)[-1]

    @staticmethod
    def _issue_of_branch(branch: str) -> int:
        # branch shape: fix/issue-401-add-metrics-histogram
        return int(branch.split("/", 1)[1].split("-")[1])

    # -- Phases ------------------------------------------------------------
    def run(self, *, dry_run: bool = False) -> RunResult:
        """Run Phases 0-4. ``dry_run=True`` plans + prints without any gh/git call."""
        if dry_run:
            return self._run_dry()
        self._phase0_preflight()
        issues = self._phase1_discovery()
        plan = self._phase2_plan(issues)
        self._phase3_execute(plan)
        self._phase4_merge(plan)
        self._collect_calls()
        self.result.final_state = self.state.read()
        return self.result

    def _run_dry(self) -> RunResult:
        out = self.result.output_lines
        out.append("WAVE ORCHESTRATOR — DRY RUN (no gh/git calls)")
        issues = list(self.fixture["issues"])
        out.append(f"Phase 1: discovery snapshot — {len(issues)} open issue(s)")
        plan = plan_waves(issues)
        self.result.plan = plan
        out.append(
            f"Phase 2: wave plan — {plan['total_waves']} wave(s), "
            f"{plan['total_issues']} issue(s)"
        )
        for wave in plan["waves"]:
            members = ", ".join(
                f"#{issue['number']} {issue['title']}" for issue in wave["issues"]
            )
            out.append(f"  wave {wave['wave']}: {members}")
        numbers = [issue["number"] for wave in plan["waves"] for issue in wave["issues"]]
        out.append("Would-be PR sequence (creation order):")
        for number in numbers:
            agent = self.sub_agents[str(number)]
            title = agent["commit_subject"]
            body = self._render_pr_body(number, numbers)
            snippet = self._render_pre_pr_snippet(number)
            self.result.pr_creation_sequence.append(
                {
                    "issue": number,
                    "pr": None,
                    "title": title,
                    "initial_body": body.split("\n\nScope guard:")[0],
                    "final_body": body,
                    "base": self.meta["base_branch"],
                }
            )
            self.result.pre_pr_verifications.append(snippet)
            out.extend(
                [
                    f"  PR (pending) issue #{number} branch {self._branch(number)}",
                    f"    title: {title}",
                    f"    body: {body}",
                    f"    pre-PR check: {snippet.splitlines()[-1]}",
                ]
            )
        order = self._merge_order(numbers)
        self.result.merge_order = order
        out.append("Would-be merge order (ascending issue number):")
        for position, number in enumerate(order, start=1):
            out.append(f"  {position}. issue #{number}")
        out.append("DRY RUN complete — no API calls made.")
        return self.result

    def _phase0_preflight(self) -> None:
        self.gh.run(["auth", "status"])
        self.git.run(["fetch", "origin", "develop"])
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        probe = self.worktrees_dir / ".test"
        probe.touch()
        probe.unlink()

    def _phase1_discovery(self) -> list[dict[str, Any]]:
        result = self.gh.run(
            [
                "issue",
                "list",
                "--state",
                "open",
                "--json",
                "number,title,body,labels,assignees",
            ]
        )
        return [
            issue
            for issue in json.loads(result.stdout)
            if not issue.get("assignees")  # unassigned only (SKILL.md Phase 1)
        ]

    def _phase2_plan(self, issues: list[dict[str, Any]]) -> dict[str, Any]:
        plan = plan_waves(issues)
        self.result.plan = plan
        state = {
            "repo": self.meta["repo"],
            "started_at": self.meta["started_at"],
            "current_wave": 1,
            "total_waves": plan["total_waves"],
            "issues": {
                str(issue["number"]): {"wave": wave["wave"], "status": "pending"}
                for wave in plan["waves"]
                for issue in wave["issues"]
            },
            "last_updated": self.meta["last_updated"],
        }
        self.state.write(state)
        return plan

    def _phase3_execute(self, plan: dict[str, Any]) -> None:
        for wave in plan["waves"]:
            numbers = [issue["number"] for issue in wave["issues"]]
            for number in numbers:
                self._execute_issue(number, numbers)

    def _execute_issue(self, number: int, wave_numbers: list[int]) -> None:
        agent = self.sub_agents[str(number)]
        branch = self._branch(number)
        worktree = self._worktree(number)
        # 3a worktree setup (status -> implementing, recorded in state).
        self.git.run(["worktree", "add", worktree, "-b", branch, "develop"])
        self.git.branch_files[branch] = set(agent["files_changed"])
        self.state.atomic_update(
            self._issue_mutator(
                number, status="implementing", branch=branch, worktree=worktree
            )
        )
        # 3b simulated sub-agent: clean tree check, commit subject, push.
        self.git.cwd = worktree
        self.git.run(["status", "--porcelain"])  # §1.5 clean-tree check
        self.git.run(["push", "-u", "origin", branch, "--force-with-lease"])
        snippet = self._render_pre_pr_snippet(number)
        self.result.pre_pr_verifications.append(snippet)
        # 3c PR verification loop: no PR exists -> recovery creates it
        # (the orchestrator owns PR creation).
        self.gh.run(
            [
                "pr",
                "list",
                "--search",
                branch,
                "--json",
                "number,title,state",
                "--jq",
                '.[] | select(.state=="OPEN") | .number',
            ]
        )
        pr_number = self._create_pr(number, agent, branch, wave_numbers)
        self.result.pr_creation_sequence.append(
            {
                "issue": number,
                "pr": pr_number,
                "title": agent["commit_subject"],
                "initial_body": self._keyword(number) + f" #{number}",
                "final_body": self.gh.prs[pr_number]["body"],
                "base": self.gh.prs[pr_number]["baseRefName"],
            }
        )
        self.state.atomic_update(
            self._issue_mutator(number, status="pr_created", pr=pr_number)
        )

    def _create_pr(
        self, number: int, agent: dict[str, Any], branch: str, wave_numbers: list[int]
    ) -> int:
        """Orchestrator-owned PR creation + §4 base check + §5 body validation."""
        url = self.gh.run(
            [
                "pr",
                "create",
                "--base",
                self.meta["base_branch"],
                "--title",
                agent["commit_subject"],
                "--body",
                f"{self._keyword(number)} #{number}",
                "--head",
                branch,
            ]
        ).stdout.strip()
        pr_number = int(url.rsplit("/", 1)[-1])
        # §4 PR base verification.
        base = self.gh.run(
            ["pr", "view", str(pr_number), "--json", "baseRefName", "--jq", ".baseRefName"]
        ).stdout
        assert base == self.meta["base_branch"], "PR base must be develop"
        # §5 PR body validation: append the rendered Scope guard line.
        body = self.gh.run(
            ["pr", "view", str(pr_number), "--json", "body", "--jq", ".body"]
        ).stdout
        if "Scope guard:" not in body:
            rendered = render_orchestrator_snippet.render_snippet(
                SCOPE_GUARD_TEMPLATE,
                issue_number=number,
                slug=agent["slug"],
                next_issue=self._next_issue(number, wave_numbers),
            )
            self.gh.run(
                ["pr", "edit", str(pr_number), "--body", f"{body}\n\n{rendered}"]
            )
        return pr_number

    def _keyword(self, number: int) -> str:
        subject = self.sub_agents[str(number)]["commit_subject"]
        return "Closes" if f"resolve #{number}" in subject else "Refs"

    def _render_pre_pr_snippet(self, number: int) -> str:
        agent = self.sub_agents[str(number)]
        return render_orchestrator_snippet.render_snippet(
            PRE_PR_TEMPLATE,
            issue_number=number,
            slug=agent["slug"],
            affected_file=agent["affected_file"],
        )

    def _render_pr_body(self, number: int, wave_numbers: list[int]) -> str:
        agent = self.sub_agents[str(number)]
        rendered = render_orchestrator_snippet.render_snippet(
            SCOPE_GUARD_TEMPLATE,
            issue_number=number,
            slug=agent["slug"],
            next_issue=self._next_issue(number, wave_numbers),
        )
        return f"{self._keyword(number)} #{number}\n\n{rendered}"

    @staticmethod
    def _next_issue(number: int, wave_numbers: list[int]) -> str:
        later = [other for other in wave_numbers if other > number]
        return str(min(later)) if later else "n/a"

    def _merge_order(self, numbers: list[int]) -> list[int]:
        """REFERENCE.md Merge Ordering Strategy: ascending number, then fewer
        files, then docs-only first."""

        def key(number: int) -> tuple[int, int, int]:
            files = self.sub_agents[str(number)]["files_changed"]
            docs_only = 0 if all(name.endswith(".md") for name in files) else 1
            return (number, len(files), docs_only)

        return sorted(numbers, key=key)

    def _phase4_merge(self, plan: dict[str, Any]) -> None:
        for wave in plan["waves"]:
            numbers = [issue["number"] for issue in wave["issues"]]
            for number in self._merge_order(numbers):
                record = self.state.read()["issues"].get(str(number), {})
                if record.get("status") == "merged":
                    continue  # already complete (resume path)
                self._merge_one(number)

    def _merge_one(self, number: int) -> None:
        pr_number = self.state.read()["issues"][str(number)]["pr"]
        agent = self.sub_agents[str(number)]
        branch = self._branch(number)
        worktree = self._worktree(number)
        checks = self.gh.run(["pr", "checks", str(pr_number)])
        assert checks.returncode == 0, "CI must be green before merge"
        mergeable = self.gh.run(
            ["pr", "view", str(pr_number), "--json", "mergeable", "--jq", ".mergeable"]
        ).stdout
        if mergeable == "CONFLICTING":
            self.result.rebases.append(
                self._conflict_protocol(number, pr_number, worktree, branch)
            )
        mergeable = self.gh.run(
            ["pr", "view", str(pr_number), "--json", "mergeable", "--jq", ".mergeable"]
        ).stdout
        assert mergeable == "MERGEABLE", f"PR #{pr_number} must be mergeable"
        self.gh.run(
            [
                "pr",
                "merge",
                str(pr_number),
                "--squash",
                "--subject",
                agent["commit_subject"],
                "--body",
                f"Closes #{number}",
            ]
        )
        merged_at = self.gh.run(
            ["pr", "view", str(pr_number), "--json", "mergedAt", "--jq", ".mergedAt"]
        ).stdout
        assert merged_at != "null", "merge must persist (mergedAt non-null)"
        self.git.merge_into_develop(branch)
        self.result.merge_events.append(
            {
                "issue": number,
                "pr": pr_number,
                "subject": agent["commit_subject"],
                "body": f"Closes #{number}",
            }
        )
        # Phase 4c: auto-close then verify (issues #961, #366, #289).
        self.result.auto_closed.append(
            {"pr": pr_number, "closed": self._auto_close(pr_number)}
        )
        self._verify_issues_closed(pr_number)
        # Cleanup: worktree remove BEFORE branch delete (REFERENCE.md).
        self.git.run(["worktree", "remove", worktree])
        self.git.run(["branch", "-d", branch])
        self.git.run(["push", "origin", "--delete", branch])
        self.git.run(["worktree", "prune"])
        self.result.merge_order.append(number)
        self.state.atomic_update(
            self._issue_mutator(number, status="merged", worktree_cleaned=True)
        )

    def _conflict_protocol(
        self, number: int, pr_number: int, worktree: str, branch: str
    ) -> dict[str, Any]:
        """REFERENCE.md Merge Conflict Resolution: fetch, rebase, auto-resolve,
        force-push, close/reopen retrigger."""
        self.git.cwd = worktree
        self.git.run(["fetch", "origin", "develop"])
        rebase = self.git.run(["rebase", "origin/develop"])
        conflicting: list[str] = []
        if rebase.returncode != 0:
            conflicting = self.git.conflicting_files(branch)
            # Auto-resolution: AGENTS.md count-line -> re-count (accept the
            # higher develop counts); other hunks resolve via non-overlap.
            self.git.run(["rebase", "--continue"])
        self.git.run(["push", "-u", "origin", branch, "--force-with-lease"])
        # CI retrigger on the new HEAD: close + reopen (not `run rerun`).
        self.gh.run(["pr", "close", str(pr_number)])
        self.gh.run(["pr", "reopen", str(pr_number)])
        self.result.retriggers.append(
            {"pr": pr_number, "issue": number, "sequence": ["close", "reopen"]}
        )
        triggered_by = next(
            (
                self._issue_of_branch(pr["headRefName"])
                for pr in sorted(self.gh.prs.values(), key=lambda record: record["number"])
                if pr["state"] == "MERGED"
                and set(conflicting)
                & self.git.branch_files.get(pr["headRefName"], set())
            ),
            None,
        )
        return {
            "issue": number,
            "pr": pr_number,
            "triggered_by_merge_of": triggered_by,
            "conflicting_files": conflicting,
            "conflict_type": "count-line",
            "resolution": "re-count (accept develop's higher test/file counts)",
        }

    def _auto_close(self, pr_number: int) -> list[int]:
        """Phase 4c step 1 via the real scripts/auto_close_issues.py."""
        original = auto_close_issues._run_gh
        auto_close_issues._run_gh = self.gh.run_gh_bound  # type: ignore[assignment]
        try:
            return auto_close_issues.auto_close_issues_from_pr_body(
                pr_number, repo=self.meta["repo"]
            )
        finally:
            auto_close_issues._run_gh = original  # type: ignore[assignment]

    def _verify_issues_closed(self, pr_number: int) -> None:
        """Phase 4c step 2 via the snapshot verify_issues_closed.sh + gh shim."""
        if not (shutil.which("bash") and VERIFY_SCRIPT.exists()):
            return
        self.gh.dump_transport(self.transport_path)
        env = {
            **os.environ,
            "PATH": f"{self.shim_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "WAVE_GH_TRANSPORT": str(self.transport_path),
            "WAVE_GH_CALLS": str(self.shim_calls_path),
        }
        completed = subprocess.run(
            ["bash", str(VERIFY_SCRIPT), str(pr_number)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if self.shim_calls_path.exists():
            self.result.shim_calls.extend(
                json.loads(line)
                for line in self.shim_calls_path.read_text(encoding="utf-8").splitlines()
            )
        assert completed.returncode == 0, (
            f"verify_issues_closed.sh failed: {completed.stderr}"
        )

    # -- Resume (REFERENCE.md §Resume and Recovery) -------------------------
    def resume(self) -> RunResult:
        """Documented resume: corruption recovery + stale cleanup + finish."""
        try:
            self.state.read()
        except StateCorruptionError:
            # Mid-write corruption: reconstruct from ground truth — the
            # documented 2026-08-20 recovery (observed PRs / branches /
            # worktrees), never from the truncated bytes.
            self.state.write(self._rebuild_state_from_ground_truth())
        removed_temps = self.state.remove_stale_temps()
        self.result.output_lines.append(f"resume: removed stale temps {removed_temps}")
        self._resume_stale_worktree_cleanup()
        self._resume_missed_issue_closes()
        state = self.state.read()
        for number in sorted(
            int(key)
            for key, record in state["issues"].items()
            if record.get("status") != "merged"
        ):
            record = self.state.read()["issues"][str(number)]
            status = record.get("status", "pending")
            if status in ("implementing", "pending"):
                self._resume_implementing(number)
            elif status in ("pr_created", "ci_fixing", "conflicted"):
                self._resume_pr_created(number, record)
        remaining = {
            "waves": [
                {
                    "wave": 1,
                    "issues": [
                        {"number": number}
                        for number in sorted(int(key) for key in state["issues"])
                    ],
                }
            ]
        }
        self._phase4_merge(remaining)
        self._collect_calls()
        self.result.final_state = self.state.read()
        return self.result

    def _rebuild_state_from_ground_truth(self) -> dict[str, Any]:
        """Reconstruct wave-state.json from observed PRs, issues, worktrees."""
        issues: dict[str, Any] = {}
        for number_str in self.sub_agents:
            number = int(number_str)
            branch = self._branch(number)
            worktree = self._worktree(number)
            worktree_exists = self.git.worktrees.get(self._worktree_name(number), False)
            merged_pr = next(
                (
                    pr
                    for pr in self.gh.prs.values()
                    if pr["headRefName"] == branch and pr["state"] == "MERGED"
                ),
                None,
            )
            open_pr = next(
                (
                    pr
                    for pr in self.gh.prs.values()
                    if pr["headRefName"] == branch and pr["state"] == "OPEN"
                ),
                None,
            )
            if merged_pr is not None:
                record: dict[str, Any] = {
                    "wave": 1,
                    "status": "merged",
                    "pr": merged_pr["number"],
                    "branch": branch,
                    "worktree": worktree,
                    "worktree_cleaned": not worktree_exists,
                }
            elif open_pr is not None:
                record = {
                    "wave": 1,
                    "status": "pr_created",
                    "pr": open_pr["number"],
                    "branch": branch,
                    "worktree": worktree,
                }
            elif worktree_exists:
                record = {
                    "wave": 1,
                    "status": "implementing",
                    "branch": branch,
                    "worktree": worktree,
                }
            else:
                record = {"wave": 1, "status": "pending"}
            issues[number_str] = record
        return {
            "repo": self.meta["repo"],
            "started_at": self.meta["started_at"],
            "current_wave": 1,
            "total_waves": 1,
            "issues": issues,
            "last_updated": self.meta["last_updated"],
        }

    def _resume_stale_worktree_cleanup(self) -> None:
        """Resume step (a): merged-but-uncleaned issues get full cleanup."""
        for number_str, record in self.state.read()["issues"].items():
            if record.get("status") != "merged" or record.get("worktree_cleaned"):
                continue
            worktree, branch = record["worktree"], record["branch"]
            if self.git.worktrees.get(worktree.rsplit("/", 1)[-1], False):
                self.git.run(["worktree", "remove", worktree])
            if branch in self.git.local_branches:
                self.git.run(["branch", "-d", branch])
            if branch in self.git.remote_branches:
                self.git.run(["push", "origin", "--delete", branch])
            self.state.atomic_update(
                self._issue_mutator(int(number_str), worktree_cleaned=True)
            )
        self.git.run(["worktree", "prune"])

    def _resume_missed_issue_closes(self) -> None:
        """Merged PRs whose linked issues stayed OPEN (the #289 gap):
        re-run Phase 4c auto-close + verify for each."""
        for record in self.state.read()["issues"].values():
            if record.get("status") != "merged":
                continue
            pr_number = record["pr"]
            for issue_number in auto_close_issues.parse_closing_refs(
                self.gh.prs[pr_number]["body"]
            ):
                if self.gh.issue_states.get(issue_number) == "OPEN":
                    self.result.auto_closed.append(
                        {"pr": pr_number, "closed": self._auto_close(pr_number)}
                    )
                    self._verify_issues_closed(pr_number)
                    break

    def _resume_implementing(self, number: int) -> None:
        """Resume: implementing with an existing worktree -> continue to PR."""
        agent = self.sub_agents[str(number)]
        branch = self._branch(number)
        worktree = self._worktree(number)
        if not self.git.worktrees.get(self._worktree_name(number), False):
            self.git.run(["worktree", "add", worktree, "-b", branch, "develop"])
            self.git.branch_files[branch] = set(agent["files_changed"])
        self.git.cwd = worktree
        if branch not in self.git.remote_branches:
            # Recovery step A (else branch): branch never pushed — push it.
            self.git.run(["push", "-u", "origin", branch, "--force-with-lease"])
        wave_numbers = sorted(int(key) for key in self.state.read()["issues"])
        pr_number = self._create_pr(number, agent, branch, wave_numbers)
        self.result.pr_creation_sequence.append(
            {
                "issue": number,
                "pr": pr_number,
                "title": agent["commit_subject"],
                "initial_body": f"{self._keyword(number)} #{number}",
                "final_body": self.gh.prs[pr_number]["body"],
                "base": self.gh.prs[pr_number]["baseRefName"],
            }
        )
        self.state.atomic_update(
            self._issue_mutator(number, status="pr_created", pr=pr_number)
        )

    def _resume_pr_created(self, number: int, record: dict[str, Any]) -> None:
        """Resume: pr_created -> verify the PR still exists (else -> pending)."""
        pr_state = self.gh.run(
            ["pr", "view", str(record["pr"]), "--json", "state", "--jq", ".state"]
        ).stdout
        if pr_state != "OPEN":
            self.state.atomic_update(self._issue_mutator(number, status="pending"))

    def _collect_calls(self) -> None:
        self.result.gh_calls = list(self.gh.calls)
        self.result.git_calls = list(self.git.calls)

    @staticmethod
    def _issue_mutator(
        number: int, **changes: Any
    ) -> Callable[[dict[str, Any]], None]:
        def mutate(state: dict[str, Any]) -> None:
            state["issues"].setdefault(str(number), {}).update(changes)

        return mutate


# ---------------------------------------------------------------------------
# CLI: the literal --dry-run flag (issue #381 acceptance criterion 2).
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wave-orchestrator-harness",
        description=(
            "Replay the wave-orchestrator state machine against a local fixture. "
            "--dry-run prints the wave plan, would-be PR titles/bodies, and "
            "merge order WITHOUT making any gh/git calls."
        ),
    )
    parser.add_argument(
        "--fixture",
        default=str(DEFAULT_FIXTURE),
        help="sample-wave fixture to replay (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan + print only; no gh/git calls, no state writes",
    )
    parser.add_argument(
        "--resume-crashed",
        action="store_true",
        help="lay out the fixture's crash episode and run the documented resume",
    )
    args = parser.parse_args(argv)
    fixture = load_fixture(Path(args.fixture))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        if args.resume_crashed:
            result = WaveOrchestratorHarness.crashed(fixture, root).resume()
        else:
            result = WaveOrchestratorHarness.fresh(fixture, root).run(
                dry_run=args.dry_run
            )
    if args.dry_run:
        print("\n".join(result.output_lines))
    else:
        print(
            f"replay complete: {len(result.pr_creation_sequence)} PR(s), "
            f"merge order {result.merge_order}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
