#!/usr/bin/env bash
#
# scripts/verify_issues_closed.sh — verify that all issues mentioned in a
# PR's body are actually closed after merge.
#
# This addresses issue #961: PR body validation gap where GitHub's auto-close
# uses exact issue number matching. A PR body that says "Closes #939" but the
# commit also fixed #941 will leave #941 open because GitHub only matched #939.
#
# Usage:
#   bash scripts/verify_issues_closed.sh <PR_NUMBER>
#
# Exit codes:
#   0  all issues mentioned in PR body are closed
#   1  one or more issues remain open (issues listed on stdout)
#   2  invalid usage, PR not found, or API error
#
# Requirements: gh CLI authenticated with repo:read scope.

set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <PR_NUMBER>" >&2
  exit 2
fi

PR_NUMBER=$1

if ! [[ "$PR_NUMBER" =~ ^[0-9]+$ ]]; then
  echo "PR_NUMBER must be a positive integer, got: $PR_NUMBER" >&2
  exit 2
fi

# Fetch the PR body
BODY=$(gh pr view "$PR_NUMBER" --json body --jq '.body // ""')

# Extract distinct closingReferences-style issue numbers from the body
# Case-insensitive, allow optional whitespace before the issue number.
# Deduplicated to handle "Closes #455" appearing twice (still closes one issue).
ISSUES=$(printf '%s\n' "$BODY" \
  | grep -oiE '(closes|fixes|resolves)\s+#[0-9]+' \
  | grep -oE '#[0-9]+' \
  | sort -u)

if [[ -z "$ISSUES" ]]; then
  echo "INFO: PR #$PR_NUMBER body contains no closingReferences (Closes/Fixes/Resolves)" >&2
  exit 0
fi

OPEN_ISSUES=""

while IFS= read -r issue_ref; do
  # Strip the leading '#' to get the issue number
  issue_num="${issue_ref#\#}"

  # Check if the issue is closed
  state=$(gh issue view "$issue_num" --json state --jq '.state' 2>/dev/null || echo "UNKNOWN")

  if [[ "$state" != "CLOSED" ]]; then
    if [[ -n "$OPEN_ISSUES" ]]; then
      OPEN_ISSUES="${OPEN_ISSUES}, ${issue_ref}"
    else
      OPEN_ISSUES="${issue_ref}"
    fi
  fi
done <<< "$ISSUES"

if [[ -n "$OPEN_ISSUES" ]]; then
  echo "FAIL: PR #$PR_NUMBER merged but these issues remain open: $OPEN_ISSUES" >&2
  exit 1
fi

echo "OK: PR #$PR_NUMBER merged — all linked issues are closed"
exit 0