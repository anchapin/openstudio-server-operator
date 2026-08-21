#!/usr/bin/env bash
# Pre-flight visibility check for stale worktree directories (issue #383).
#
# Wave-orchestration sessions create sibling worktree directories under
# ../worktrees/. When a session ends without running the cleanup sequence
# (or predates it), the disk directories outlive their branches: git no
# longer knows about them (`git worktree list` excludes them, the branch
# was deleted on merge), but the directories persist and accumulate. New
# orchestrators may mistake the leftovers for in-progress work.
#
# This script lists every `issue-*` directory under the worktrees dir
# whose derived branch name (fix/<dir>, feat/<dir>, docs/<dir>,
# chore/<dir>) matches NO local branch and which is not registered in
# `git worktree list`. It prints one `STALE worktree:` line per hit plus
# a summary; when the count exceeds the threshold it prints a WARNING
# line that should be surfaced as a Warning event BEFORE any new
# worktrees are created (the issue #383 acceptance criterion).
#
# List mode NEVER deletes anything and always exits 0 — this is a
# visibility tool, not a CI gate. Deletion is a separate, explicit
# opt-in: `--prune-stale-worktrees` removes qualifying directories
# (stale AND older than --min-age-days) but ONLY together with the
# `--yes` confirmation flag; without `--yes` it lists what would be
# pruned and deletes nothing (first-iteration scope guard from the
# issue: no auto-delete without user confirmation).
#
# Exit codes:
#   0 — check ran (list mode is visibility, not a gate; also 0 when the
#       worktrees dir is absent, and after a completed --yes prune).
#   2 — usage error (unknown flag, non-numeric --threshold /
#       --min-age-days) or not inside a git repository.
#
# Usage:
#   bash scripts/check_stale_worktrees.sh
#   bash scripts/check_stale_worktrees.sh --worktrees-dir /path/to/worktrees
#   bash scripts/check_stale_worktrees.sh --threshold 3
#   bash scripts/check_stale_worktrees.sh --prune-stale-worktrees              # list only
#   bash scripts/check_stale_worktrees.sh --prune-stale-worktrees --yes \
#       --min-age-days 30                                                     # actually delete
#
# The script is intentionally dependency-free (bash + git + find + grep).
# Run it as Phase 0/0a of a wave cycle, from the main checkout or any
# registered worktree — the default worktrees dir is resolved relative
# to the MAIN working tree (the first entry of `git worktree list`),
# not relative to the caller's CWD, so it works from inside a worktree.
set -euo pipefail

WORKTREES_DIR=""
THRESHOLD=5
MIN_AGE_DAYS=30
PRUNE=0
YES=0

while [ $# -gt 0 ]; do
    case "$1" in
        --worktrees-dir)   shift; WORKTREES_DIR="${1:-}" ;;
        --worktrees-dir=*) WORKTREES_DIR="${1#--worktrees-dir=}" ;;
        --threshold)       shift; THRESHOLD="${1:-}" ;;
        --threshold=*)     THRESHOLD="${1#--threshold=}" ;;
        --min-age-days)    shift; MIN_AGE_DAYS="${1:-}" ;;
        --min-age-days=*)  MIN_AGE_DAYS="${1#--min-age-days=}" ;;
        --prune-stale-worktrees) PRUNE=1 ;;
        --yes)             YES=1 ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            exit 2
            ;;
    esac
    shift || true
done

case "$THRESHOLD" in
    ''|*[!0-9]*)
        echo "ERROR: --threshold must be a non-negative integer, got: '$THRESHOLD'" >&2
        exit 2
        ;;
esac
case "$MIN_AGE_DAYS" in
    ''|*[!0-9]*)
        echo "ERROR: --min-age-days must be a non-negative integer, got: '$MIN_AGE_DAYS'" >&2
        exit 2
        ;;
esac

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "ERROR: not inside a git repository — run from the operator checkout or one of its worktrees." >&2
    exit 2
fi

# Registered worktrees of THIS repo (basenames — same comparison the
# issue proposes: a directory registered in `git worktree list` is live
# by definition, even if its branch name differs from the dir name).
REGISTERED_NAMES=$(git worktree list --porcelain \
    | sed -n 's/^worktree //p' \
    | while IFS= read -r wt; do basename "$wt"; done)

# Local branches, one per line (refs/heads only — no remotes: a dir
# whose branch exists only on the origin remote still has no local
# checkout backing it).
BRANCHES=$(git for-each-ref --format='%(refname:short)' refs/heads/)

if [ -z "$WORKTREES_DIR" ]; then
    MAIN_WORKTREE=$(git worktree list --porcelain | head -n1 | sed 's/^worktree //')
    WORKTREES_DIR="$MAIN_WORKTREE/../worktrees"
fi

if [ ! -d "$WORKTREES_DIR" ]; then
    echo "worktrees dir not found: $WORKTREES_DIR — nothing to check (issue #383 pre-flight)."
    exit 0
fi

STALE_COUNT=0
PRUNED_COUNT=0

for DIR in "$WORKTREES_DIR"/issue-*/; do
    [ -d "$DIR" ] || continue
    NAME=$(basename "$DIR")
    DIR=${DIR%/}

    # Live by definition: registered in this repo's worktree metadata.
    if printf '%s\n' "$REGISTERED_NAMES" | grep -qx "$NAME"; then
        continue
    fi

    # Live if any conventionally-prefixed branch backs the directory.
    MATCH=0
    for PREFIX in fix feat docs chore; do
        if printf '%s\n' "$BRANCHES" | grep -qx "$PREFIX/$NAME"; then
            MATCH=1
            break
        fi
    done
    if [ "$MATCH" -eq 1 ]; then
        continue
    fi

    echo "STALE worktree: $DIR (no matching branch)"
    STALE_COUNT=$((STALE_COUNT + 1))

    if [ "$PRUNE" -eq 1 ] && [ "$YES" -eq 1 ]; then
        # find -mtime +N = age strictly greater than N whole days.
        if [ "$(find "$DIR" -maxdepth 0 -type d -mtime +"$MIN_AGE_DAYS" | wc -l)" -gt 0 ]; then
            rm -rf "$DIR"
            echo "PRUNED stale worktree: $DIR (no matching branch, older than $MIN_AGE_DAYS days)"
            PRUNED_COUNT=$((PRUNED_COUNT + 1))
        fi
    fi
done

echo "Stale worktree directories: $STALE_COUNT (threshold: $THRESHOLD)"
if [ "$STALE_COUNT" -gt "$THRESHOLD" ]; then
    echo "WARNING: $STALE_COUNT stale worktree directories exceed the threshold of $THRESHOLD —"
    echo "surface a Warning event and confirm with the user BEFORE creating new worktrees (issue #383)."
fi

if [ "$PRUNE" -eq 1 ]; then
    if [ "$YES" -eq 0 ]; then
        echo "NOTE: --prune-stale-worktrees given without --yes — listing only, nothing deleted."
    else
        echo "Pruned $PRUNED_COUNT stale worktree directories older than $MIN_AGE_DAYS days."
    fi
fi

exit 0
