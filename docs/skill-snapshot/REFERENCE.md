# GitHub Wave Orchestrator — Reference

## Implementation Sub-agent Template

When spawning a Task sub-agent to implement an issue, use this prompt:

```
You are implementing a fix for GitHub issue #{NUMBER}: {TITLE}

IMPORTANT: The `--base develop` flag is MANDATORY. Omitting it causes the PR
to target main (the repository default branch), which blocks the wave pipeline.
The orchestrator has an auto-fix step, but always specify --base develop
explicitly to avoid recovery overhead.
```

Repository: {OWNER}/{REPO}
Branch: fix/issue-{NUMBER}-{SLUG} (already checked out)
Workdir: ../worktrees/issue-{NUMBER}-{SLUG}

Steps:
1. Read the full issue: gh issue view {NUMBER}
2. Read the issue comments for additional context: gh issue view {NUMBER} --comments
3. Analyze what code needs to change
4. Implement the fix/feature with tests
5. Run local checks if available (make test-fast, make lint)
 6. Commit: git add -A && git commit -m "{fix|feat}: resolve #{NUMBER} — {brief description}"
     (Use `refs #{NUMBER}` instead of `resolve #{NUMBER}` for keep-open PRs —
      detect from issue body: "Acceptance criterion" + "Closes #N" = closing;
      "Refs #N" only = keep-open.)
 7. Push: git push -u origin fix/issue-{NUMBER}-{SLUG} --force-with-lease
    NOTE: If push fails (e.g., remote branch exists with newer commits), use
    `git pull --rebase origin develop` first, then push again with --force-with-lease.
     NOTE: After this step the orchestrator independently verifies the PR exists
     and creates it if missing. See SKILL.md Phase 3c § Recovery. Do NOT create
     the PR yourself — the orchestrator owns PR creation for all sub-agents.

Rules:
- Work ONLY in your assigned worktree ({WORKDIR})
- Do NOT modify files outside the scope of this issue
- `body` fixture gotcha: If your test references the `body` fixture, add it to the test function signature: `def test_...(body, ...)`. Pytest's default `conftest.py` does NOT auto-inject fixtures not declared in the signature (issue #299).
- Scope guard line: Always include a `Scope guard: Do NOT touch <unrelated-area>; #M owns that.` line in the PR body. The repo's `scripts/check_pr_body_scope.sh` enforces this in CI (issue #301).
  The orchestrator does NOT auto-append a `Scope guard` line; the sub-agent must include it.
- Include tests for the fix/feature if the repo has a test suite
- Follow the repo's AGENTS.md and code style conventions
- If the issue is unclear, add a comment asking for clarification: gh issue comment {NUMBER} -b "..."
- Report back: PR number, files changed, any blockers encountered
```

## CI Sub-agent Template

When spawning a Task sub-agent to monitor CI and merge a PR:

```
You are shepherding PR #{NUMBER} through CI to merge.

Repository: {OWNER}/{REPO}
PR: {PR_URL}
Branch: fix/issue-{NUMBER}-{SLUG}
Workdir: ../worktrees/issue-{NUMBER}-{SLUG}
Issue: #{NUMBER}

IMPORTANT: Record your worktree path in wave-state.json IMMEDIATELY after
checking out (before any other steps). This ensures cleanup can happen
even if the sub-agent is interrupted. Run:
  source scripts/wave-state-helpers.sh
  STATE_FILE=$(get_state_file)
  atomic_write_json ".issues[\"{NUMBER}\"].worktree = \"../worktrees/issue-{NUMBER}-{SLUG}\"" "$STATE_FILE"

Steps:
1. Record worktree path in wave-state.json (see IMPORTANT above)
2. Check CI status: gh pr checks {NUMBER}
3. IF CI is green:
   a. Check mergeable: gh pr view {NUMBER} --json mergeable
   b. If CONFLICTING → follow the merge conflict protocol below
c. If MERGEABLE → merge: gh pr merge {NUMBER} --squash \
           --subject "fix: resolve #{NUMBER} — {brief description}" \
           --body "Closes #{NUMBER}"
        NOTE: Do NOT use --delete-branch here. The branch deletion must happen
       AFTER worktree removal (see step 3d below) to avoid:
       "error: cannot delete branch 'fix/issue-N' used by worktree at '../worktrees/issue-N'"

       Then verify the merge persisted:
       ```
       gh pr view {NUMBER} --json mergedAt --jq '.mergedAt'
       ```
        - If mergedAt is NOT null → merge succeeded, verify issues are closed:
          ```
          bash scripts/verify_issues_closed.sh {NUMBER}
          ```
          - If all issues closed → proceed to step 3d
          - If any issue remains open → report BLOCKER and STOP
- If mergedAt IS null → merge did NOT persist. Retry once:
           `gh pr merge {NUMBER} --squash \
           --subject "fix: resolve #{NUMBER} — {brief description}" \
           --body "Closes #{NUMBER}"`
          If second attempt also yields null mergedAt → report BLOCKED and STOP
    d. Clean up (ORDER MATTERS — worktree remove BEFORE branch delete):
       ```bash
       # Step 1: Remove worktree FIRST (branch must not be deleted yet)
       git worktree remove ../worktrees/issue-{NUMBER}-{SLUG}

       # Step 2: Delete local branch (safe now that worktree is gone)
       git branch -d fix/issue-{NUMBER}-{SLUG}

       # Step 3: Delete remote branch
       git push origin --delete fix/issue-{NUMBER}-{SLUG}

       # Step 4: Prune any stale worktree references
       git worktree prune
       ```

    e. Update wave-state.json to mark worktree as cleaned:
       ```bash
       source scripts/wave-state-helpers.sh
       STATE_FILE=$(get_state_file)
       atomic_write_json ".issues[\"{NUMBER}\"].worktree_cleaned = true" "$STATE_FILE"
       ```
3. IF CI is failing:
   a. Get failing run: gh run list --branch fix/issue-{NUMBER}-{SLUG} --limit 1
   b. Get logs: gh run view {RUN_ID} --log
   c. Diagnose the FIRST failing step
   d. Apply minimal fix in the worktree
   e. Commit and push: git add -A && git commit -m "fix: resolve CI failure" && git push
   f. Force a fresh CI run on the new HEAD (do NOT rely on automatic trigger):
      ```bash
      gh pr close {NUMBER}
      gh pr reopen {NUMBER}
      ```
      NOTE: The close/reopen cycle cancels any pending runs and dispatches a new
      workflow on the latest commit SHA. This is more reliable than `gh run rerun`
      which may re-run the old SHA. After close/reopen, poll for the new run:
      `gh run list --branch fix/issue-{NUMBER}-{SLUG} --limit 1` and verify its
      `headSha` matches the pushed commit.
   g. Wait for the new CI run to complete (poll every 30s, max 10 iterations)
   h. If still failing after 10 iterations → report blocker and STOP
4. IF merge conflict detected after another PR was merged:
   a. git fetch origin develop
   b. git rebase origin/develop
   c. Resolve conflicts automatically where possible:
      - Lock files: regenerate (npm install, pip install)
      - Import blocks: accept both sides
      - Version bumps: accept higher version
      - Generated files: regenerate
   d. If conflicts cannot be auto-resolved → STOP and report:
      "MERGE CONFLICT on PR #{NUMBER}: {list conflicting files}"
   e. git push --force-with-lease origin fix/issue-{NUMBER}-{SLUG}
   f. Force a fresh CI run on the new HEAD (do NOT rely on automatic trigger):
      ```bash
      gh pr close {NUMBER}
      gh pr reopen {NUMBER}
      ```
      After close/reopen, verify the new run was triggered on the correct SHA:
      `gh run list --branch fix/issue-{NUMBER}-{SLUG} --limit 1`
      and confirm its `headSha` matches the pushed commit. Return to step 1.

Report back: final status (MERGED / BLOCKED / CONFLICT), iterations used, files fixed.
   Note: MERGED status requires (a) mergedAt to be non-null AND (b) all issues
   mentioned in the PR body to be CLOSED after merge. If mergedAt is null, the
   merge did not persist — report BLOCKED. If any linked issue remains open after
   merge (issue #961), report BLOCKER with the open issue numbers.
```

## Merge Ordering Strategy

Within a wave, merge PRs in a specific order to minimize conflicts:

### Ordering Rules (applied in priority order)

1. **Ascending issue number.** Lower issue numbers were filed earlier and
   typically touch more stable code. Merging them first provides a stable
   base for later PRs to rebase onto.

2. **Fewer affected files first (tiebreaker).** If two issues have the same
   number, merge the one touching fewer files first — it's less likely to
   cause conflicts for the other.

3. **Documentation-only PRs first (tiebreaker).** PRs that only touch
   `*.md` files are conflict-free with code changes and should merge first.

### After Each Merge

```bash
# Immediately after merging PR #{N}:
git fetch origin develop

# Cleanup (ORDER MATTERS: worktree remove BEFORE branch delete)
# Use the worktree path from wave-state.json if available
source scripts/wave-state-helpers.sh
STATE_FILE=$(get_state_file)
WORKTREE_PATH=$(jq -r ".issues[\"{N}\"].worktree // \"../worktrees/issue-{N}-{slug}\"" \
  "$STATE_FILE" 2>/dev/null || echo "../worktrees/issue-{N}-{slug}")

git worktree remove "$WORKTREE_PATH"
git branch -d fix/issue-{N}-{slug}
git push origin --delete fix/issue-{N}-{slug}
git worktree prune

# For each remaining PR in the wave:
gh pr view {M} --json mergeable --jq '.mergeable'

# If CONFLICTING, the CI sub-agent for that PR handles rebase.
# No orchestrator intervention needed — the sub-agent template covers it.
```

## Resume and Recovery

### State File

The orchestrator writes a state file after each phase transition:

**Location:** `../worktrees/wave-state.<repo-slug>.json` (where `<repo-slug>` is
derived from `git remote get-url origin`, e.g. `openstudio-server-operator`)

**Backward compatibility (one release):** If the namespaced file does not exist,
the legacy `../worktrees/wave-state.json` is read with a deprecation warning.
The legacy path will be removed in a future release.

**Atomic writes:** All state file updates use write-then-rename via a temporary
file in the same directory (`mv /tmp/wave-state.XXXXXX.json <state-file>`) to
guarantee that interrupted writes never leave a partially-written file. The
helper `scripts/wave-state-helpers.sh` provides `atomic_write_json` for this.

```json
{
  "repo": "owner/repo",
  "started_at": "2025-06-10T14:30:00Z",
  "current_wave": 2,
  "total_waves": 4,
  "issues": {
    "42": {
      "wave": 1,
      "status": "merged",
      "pr": 101,
      "branch": "fix/issue-42-fix-cache",
      "worktree": "../worktrees/issue-42-fix-cache",
      "worktree_cleaned": true
    },
    "17": {
      "wave": 1,
      "status": "merged",
      "pr": 102,
      "branch": "fix/issue-17-nomad-exec",
      "worktree": "../worktrees/issue-17-nomad-exec",
      "worktree_cleaned": true
    },
    "31": {
      "wave": 2,
      "status": "pr_created",
      "pr": 103,
      "branch": "fix/issue-31-cache-key",
      "worktree": "../worktrees/issue-31-cache-key"
    },
    "8":  {
      "wave": 2,
      "status": "implementing",
      "worktree": "../worktrees/issue-8-docs-cli"
    }
  },
  "last_updated": "2025-06-10T14:45:00Z"
}
```

**Issue fields:**

| Field | Required | Description |
|---|---|---|
| `wave` | Yes | Wave number this issue was assigned to |
| `status` | Yes | Current status (see table below) |
| `pr` | For `pr_created`, `ci_fixing`, `conflicted`, `merged` | PR number |
| `branch` | Yes | Branch name (e.g., `fix/issue-42-fix-cache`) |
| `worktree` | Strongly recommended | Absolute or relative path to the worktree directory. **Required** for all non-pending statuses so cleanup can happen even after interrupt. |
| `worktree_cleaned` | For `merged` | Set to `true` after successful worktree cleanup |

### Status Values

| Status | Meaning | Resume action |
|---|---|---|
| `pending` | Not started | Create worktree, spawn implementation sub-agent |
| `implementing` | Sub-agent working | Check if worktree has uncommitted changes |
| `pr_created` | PR exists, CI not checked | Spawn CI sub-agent |
| `ci_fixing` | CI sub-agent working | Check PR checks status |
| `conflicted` | Merge conflict, needs resolution | Spawn CI sub-agent with conflict focus |
| `merged` | Complete | Skip |
| `escalated` | Blocked, needs human | Skip, report to user |

### Resume Procedure

```
1. source scripts/wave-state-helpers.sh
   STATE_FILE=$(get_state_file)
2. IF file does not exist → start fresh (Phase 0)
3. IF file exists:
   a. First, clean up any stale worktrees from previously merged issues
      (issues with status=="merged" but worktree_cleaned != true):
      ```
      for issue_num in $(jq -r '.issues | to_entries[] |
        select(.value.status == "merged" and .value.worktree_cleaned != true) |
        .key' "$STATE_FILE" 2>/dev/null); do
        worktree=$(jq -r ".issues[\"$issue_num\"].worktree" "$STATE_FILE" 2>/dev/null)
        branch=$(jq -r ".issues[\"$issue_num\"].branch" "$STATE_FILE" 2>/dev/null)
        if [[ -n "$worktree" && -d "$worktree" ]]; then
          git worktree remove "$worktree" 2>/dev/null || true
        fi
        if [[ -n "$branch" ]]; then
          git branch -d "$branch" 2>/dev/null || true
          git push origin --delete "$branch" 2>/dev/null || true
        fi
        atomic_write_json ".issues[\"$issue_num\"].worktree_cleaned = true" "$STATE_FILE"
      done
      git worktree prune
      ```
   b. Find the current_wave with incomplete issues
   c. For each issue in current_wave:
      - IF status == "implementing":
          Check if worktree exists and has changes
          IF yes → spawn sub-agent to continue
          IF no → recreate worktree from main
      - IF status == "pr_created" or "ci_fixing":
          Verify PR still exists: gh pr view {N}
          IF yes → spawn CI sub-agent
          IF no (PR was closed/deleted) → reset to "pending"
      - IF status == "conflicted":
          Spawn CI sub-agent with conflict resolution focus
      - IF status == "escalated":
          Report to user, skip
   d. Continue normal wave execution for remaining phases
```

### State Updates

Write to the namespaced state file (`../worktrees/wave-state.<repo-slug>.json`):
- After each wave plan is confirmed (initial state)
- After each sub-agent reports PR creation
- After each PR merge or escalation
- After each wave completion

All writes use `atomic_write_json` from `scripts/wave-state-helpers.sh` to
guarantee atomic updates.

### Cleanup on Successful Completion

```bash
source scripts/wave-state-helpers.sh
STATE_FILE=$(get_state_file)

# Before removing state, clean up any remaining worktrees for merged issues
for issue_num in $(jq -r '.issues | to_entries[] | select(.value.status == "merged" and .value.worktree_cleaned != true) | .key' "$STATE_FILE" 2>/dev/null); do
  worktree=$(jq -r ".issues[\"$issue_num\"].worktree" "$STATE_FILE" 2>/dev/null)
  branch=$(jq -r ".issues[\"$issue_num\"].branch" "$STATE_FILE" 2>/dev/null)
  if [[ -n "$worktree" && -d "$worktree" ]]; then
    git worktree remove "$worktree" 2>/dev/null || true
  fi
  if [[ -n "$branch" ]]; then
    git branch -d "$branch" 2>/dev/null || true
    git push origin --delete "$branch" 2>/dev/null || true
  fi
done
git worktree prune
rm "$STATE_FILE"
```

## File-Level Dependency Analysis

The wave planner extracts likely affected files from each issue using these
heuristics, applied in order of priority:

### 1. Explicit file references in issue body

Regex patterns that capture file paths mentioned in the issue text:

```
[`"]([a-zA-Z0-9_/.-]+\.[a-z]{2,4})[`"' ]
([a-zA-Z0-9_/.-]+/src/[a-zA-Z0-9_/.-]+\.[a-z]{2,4})
```

Common path patterns: `src/`, `lib/`, `test/`, `tests/`, `pkg/`, `cmd/`,
`internal/`, `osimflow/`, `bin/`.

### 2. Module and import references

```
import .+ from ['"](.+)['"]
from (.+) import
require\(['"](.+)['"]\)
use (.+::\w+)
```

Map module names to file paths using the repo's module structure.

### 3. Code symbol matching

Extract class names, function names, or constants from the issue body.
Search the codebase for definitions:

```bash
grep -rn "def {symbol}\|class {symbol}\|fn {symbol}\|func {symbol}" --include="*.{py,ts,js,rs,go}"
```

### 4. Label-based hints

Labels like `area:cache`, `component:executor`, `module:work` map to
directory prefixes configured per-repo. If no explicit mapping exists,
the label name is used as a fuzzy directory filter.

### 5. Fallback

If no files can be determined from the issue, mark it as `unknown_deps`.
Issues with `unknown_deps` are placed in single-issue waves (no parallelism)
to avoid silent conflicts.

## Merge Conflict Resolution Protocol

### Detection

After each PR merge in a wave, check remaining PRs:

```bash
gh pr view {N} --json mergeable --jq '.mergeable'
```

Values: `MERGEABLE`, `CONFLICTING`, `UNKNOWN` (still calculating).

Poll `UNKNOWN` every 15s for up to 2 minutes before treating as `CONFLICTING`.

### Resolution Flow

```
1. cd /path/to/worktree/issue-{N}-{slug}
2. git fetch origin develop
3. git rebase origin/develop

   IF rebase succeeds (no conflicts):
     → git push --force-with-lease origin fix/issue-{N}-{slug}
     → Force CI retrigger: gh pr close {N} && gh pr reopen {N}
     → Done

   IF rebase has conflicts:
     → Check conflicted files
     → Apply auto-resolution strategy (see below)
     → IF all conflicts resolved:
         git add -A && git rebase --continue
         git push --force-with-lease origin fix/issue-{N}-{slug}
         → Force CI retrigger: gh pr close {N} && gh pr reopen {N}
     → IF any conflict unresolvable:
         git rebase --abort
         → ESCALATE to user with conflict details
         → Do NOT skip silently
```

### Auto-Resolution Strategies

| Conflict type | Strategy |
|---|---|
| **Non-overlapping hunks** | Git resolves automatically on `rebase --continue` |
| **Generated files** (lock files, dist/, .generated) | Regenerate: `npm install`, `pip compile`, rebuild |
| **Import blocks** | Accept both sides (union of imports) |
| **Version bumps** (pyproject.toml, package.json) | Accept the higher version |
| **Test fixtures** | Accept the sub-agent's version (newer) |
| **Documentation** (README, CHANGELOG) | Accept the sub-agent's version |
| **Unrelated logic in same file** | Manual resolution required → escalate |

### Escalation Message Format

```
MERGE CONFLICT — Requires human resolution
PR #{N}: {title}
Branch: fix/issue-{N}-{slug}
Conflicting files:
  - {file_path} ({conflict_type})

Conflicted sections:
{git diff --name-only --diff-filter=U output}

Action needed: Rebase onto origin/develop and resolve conflicts manually.
Worktree: ../worktrees/issue-{N}-{slug}
```

## CI Failure Retry Protocol

### Iteration limits

| Level | Max | Action on exhaustion |
|---|---|---|
| Per-job fix iterations | 10 | Skip job, try next |
| Per-PR total iterations | 30 | Escalate to user |
| Conflict resolution attempts | 2 | Escalate to user |
| Overall wave timeout | 30 min | Report progress, ask to continue |

### Failure type routing

| Failure | Sub-agent action |
|---|---|
| Test failure | Read test, fix source or update assertion |
| Lint error | Auto-fix (`ruff --fix`, `eslint --fix`) |
| Type error | Fix annotations |
| Build error | Check deps, imports, config |
| Merge conflict | Apply conflict protocol (above) |
| Timeout | Increase test timeout or optimize |
| Flaky test | Re-run once; if passes → continue; if fails → investigate |
| Env/secret missing | Escalate (cannot fix without repo admin) |

## Worktree Safety

### Naming convention

```
../worktrees/issue-{NUMBER}-{SLUG}
```

Where `{SLUG}` is the first 3 dash-separated words of the issue title,
lowercased, non-alphanumeric chars stripped.

### Pre-creation checks

Before creating a worktree:

1. Verify the branch doesn't already exist: `git branch --list fix/issue-{N}-*`
2. Verify the worktree directory doesn't exist: `ls ../worktrees/`
3. Verify `develop` is up to date: `git fetch origin develop`

### Cleanup

After PR merge (ORDER MATTERS: worktree remove BEFORE branch delete):
```bash
# Use wave-state.json if available, otherwise construct from convention
source scripts/wave-state-helpers.sh
STATE_FILE=$(get_state_file)
WORKTREE_PATH=$(jq -r ".issues[\"{N}\"].worktree // \"../worktrees/issue-{N}-{slug}\"" \
  "$STATE_FILE" 2>/dev/null || echo "../worktrees/issue-{N}-{slug}")

# Step 1: Remove worktree FIRST (branch must not be deleted yet)
git worktree remove "$WORKTREE_PATH"

# Step 2: Delete local branch (safe now that worktree is gone)
git branch -d fix/issue-{N}-{slug}

# Step 3: Delete remote branch
git push origin --delete fix/issue-{N}-{slug}

# Step 4: Prune any stale worktree references
git worktree prune
```

On abort (user cancellation or critical failure):
```bash
# List all worktrees
git worktree list

# Remove all wave worktrees
git worktree list --porcelain | grep "^worktree " | grep "worktrees/issue-" | \
  awk '{print $2}' | xargs -I{} git worktree remove {}

# Clean up branches
git branch --list 'fix/issue-*' | xargs -I{} git branch -D {}

git worktree prune
source scripts/wave-state-helpers.sh
STATE_FILE=$(get_state_file)
rm -f "$STATE_FILE"
```

## Composition Points

### With `ci-iterative-fix`

CI sub-agents follow the `ci-iterative-fix` skill for the core
fetch-diagnose-fix-push-rerun loop. The wave orchestrator adds:

- Merge conflict detection and resolution (backported to `ci-iterative-fix`)
- Wave-aware ordering (merge earlier PRs first to minimize conflicts)
- Worktree lifecycle management

### With `parallel-issue-workflow`

For simple cases (all issues independent, no wave planning needed),
`parallel-issue-workflow` is sufficient. Use the wave orchestrator when:

- Issues have known or suspected dependencies
- There are more than 3 issues (wave cap kicks in)
- Previous runs had merge conflict issues

### With `pr-review-merge`

For CI monitoring of existing PRs (no wave orchestration needed),
use `pr-review-merge` directly. The wave orchestrator composes this
skill's merge logic into its Phase 4.
