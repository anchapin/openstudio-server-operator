#!/usr/bin/env bash
# CI / local guard — verify a PR body satisfies the scope-guard contract
# introduced in issue #301. The merge-subject hygiene rule (#88) only
# checks for closing/keep-open keywords; this script adds the
# complementary rule that the body MUST also declare its scope: which
# issues this PR actually touches and what is intentionally NOT being
# changed (with the follow-up issue that owns that area, when one
# exists).
#
# Exit codes:
#   0 — body satisfies the contract.
#   1 — body violates the contract (missing scope guard, missing
#       keyword, missing issue reference, or missing rationale phrase).
#   2 — usage error.
#
# Usage:
#   bash scripts/check_pr_body_scope.sh --file PATH    # check a file
#   bash scripts/check_pr_body_scope.sh -              # read body from stdin
#   gh pr view N --json body -q .body \
#     | bash scripts/check_pr_body_scope.sh -          # pipe from gh
#
# The script is intentionally dependency-free (just bash + grep). CI
# runs it from the `lint` job on every pull_request event with the
# `${{ github.event.pull_request.body }}` value piped in via stdin.
set -euo pipefail

INPUT_SOURCE=""
FILE=""

while [ $# -gt 0 ]; do
    case "$1" in
        -)            INPUT_SOURCE="stdin" ;;
        --file)       shift; FILE="${1:-}" ;;
        --file=*)     FILE="${1#--file=}" ;;
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

if [ -n "$INPUT_SOURCE" ] && [ -n "$FILE" ]; then
    echo "ERROR: pass either '-' (stdin) OR --file PATH, not both." >&2
    exit 2
fi

if [ "$INPUT_SOURCE" = "stdin" ]; then
    BODY=$(cat)
elif [ -n "$FILE" ]; then
    if [ ! -f "$FILE" ]; then
        echo "ERROR: file not found: $FILE" >&2
        exit 2
    fi
    BODY=$(cat "$FILE")
else
    echo "ERROR: must provide a PR body via --file PATH or via stdin ('-')." >&2
    echo "Hint: gh pr view N --json body -q .body | bash $0 -" >&2
    exit 2
fi

# --- Check 1: closing or keep-open keyword must be present (#88 rule).
# Matches `Closes #N`, `Fixes #N`, `Resolves #N`, `Refs #N`, `for #N`,
# `touches #N` (case-insensitive; the `#` is optional in the keep-open
# variants because real PRs use both shapes).
if printf '%s\n' "$BODY" | grep -qiE '\b(Closes|Fixes|Resolves|Refs|touches|for)\b[[:space:]]*#?[0-9]+'; then
    KEYWORD_OK=1
else
    KEYWORD_OK=0
fi

# --- Check 2: the scope-guard line itself. Accepts both bullet/list
# (`- Scope guard:` / `* Scope guard:`) and plain-paragraph (`Scope
# guard:`) forms. The grep anchors on the leading line so a passing
# mention in prose doesn't accidentally satisfy the rule.
SCOPE_GUARD_LINE=$(printf '%s\n' "$BODY" \
    | grep -E '^[[:space:]]*[-*+]?[[:space:]]*Scope[[:space:]]+[Gg]uard[[:space:]]*:' \
    | head -n1 || true)
if [ -n "$SCOPE_GUARD_LINE" ]; then
    SCOPE_GUARD_PRESENT=1
else
    SCOPE_GUARD_PRESENT=0
fi

# --- Check 2b: heading-vs-line pitfall (#385). A markdown heading
# `## Scope guard:` / `# Scope guard:` is a common mis-form for the
# scope-guard block — the body then reads naturally but the regex
# above (anchored on the line start, with optional whitespace + a
# bullet, but NOT a `#`) silently rejects it. Detect this misuse FIRST
# so the user sees a specific error pointing at the heading, not the
# generic "missing block" error below. Repro in PR #384 (first run
# failed lint because the body had `## Scope guard:` + bullet list).
HEADING_LINE=$(printf '%s\n' "$BODY" \
    | grep -E '^[[:space:]]*#+[[:space:]]+Scope[[:space:]]+[Gg]uard[[:space:]]*:' \
    | head -n1 || true)
if [ -n "$HEADING_LINE" ]; then
    HEADING_DETECTED=1
else
    HEADING_DETECTED=0
fi

# --- Check 3: the scope-guard body must reference at least one issue
# (#N). A `Scope guard: ...` line that names no issue is not a scope
# guard, it's a wish.
if [ -n "$SCOPE_GUARD_LINE" ] && printf '%s\n' "$SCOPE_GUARD_LINE" | grep -qE '#[0-9]+'; then
    SCOPE_GUARD_HAS_REF=1
else
    SCOPE_GUARD_HAS_REF=0
fi

# --- Check 4: rationale phrase. The convention requires a positive
# declaration of intent — one of the following must appear in the
# scope-guard line:
#   - "Do NOT" / "do not"        — strong negative declaration
#   - "owns"                     — deferring to a follow-up issue
#   - "out of scope"             — explicit scope statement
#   - "not in scope"             — alias of the above
#   - "do not modify"            — explicit non-modification
#   - "unchanged"                — the area is left alone
#   - "untouched"                — alias of the above
if [ -n "$SCOPE_GUARD_LINE" ] \
    && printf '%s\n' "$SCOPE_GUARD_LINE" \
        | grep -qiE '(do[[:space:]]+not|owns|out[[:space:]]+of[[:space:]]+scope|not[[:space:]]+in[[:space:]]+scope|unchanged|untouched)'; then
    SCOPE_GUARD_HAS_RATIONALE=1
else
    SCOPE_GUARD_HAS_RATIONALE=0
fi

# --- Emit failures + summary.
FAIL=0
if [ "$KEYWORD_OK" -eq 0 ]; then
    echo "::error::PR body missing required issue keyword. Add one of: Closes #N, Fixes #N, Resolves #N (closing) or Refs #N, for #N, touches #N (keep-open). See docs/onboarding.md#merge-subject-hygiene-88." >&2
    FAIL=1
fi
if [ "$SCOPE_GUARD_PRESENT" -eq 0 ]; then
    echo "::error::PR body missing required 'Scope guard:' block (issue #301). Add a line of the form 'Scope guard: Do NOT touch <area>; #M owns that.' — see docs/onboarding.md#scope-guard-issue-301." >&2
    FAIL=1
fi
if [ "$SCOPE_GUARD_PRESENT" -eq 1 ] && [ "$SCOPE_GUARD_HAS_REF" -eq 0 ]; then
    echo "::error::'Scope guard:' block is present but does not reference any issue #N. The scope guard must name the follow-up issue(s) that own the out-of-scope area." >&2
    FAIL=1
fi
if [ "$SCOPE_GUARD_PRESENT" -eq 1 ] && [ "$SCOPE_GUARD_HAS_RATIONALE" -eq 0 ]; then
    echo "::error::'Scope guard:' block is present but contains no rationale phrase. Include one of: 'Do NOT', 'do not', 'owns', 'out of scope', 'not in scope', 'do not modify', 'unchanged', 'untouched'." >&2
    FAIL=1
fi

if [ "$HEADING_DETECTED" -eq 1 ]; then
    echo "::error::PR body uses a markdown heading for the scope guard ('${HEADING_LINE}'). The script requires a single 'Scope guard:' LINE — optionally bulleted ('- Scope guard: ...' / '* Scope guard: ...') — NOT a section header ('## Scope guard:'). See docs/onboarding.md#scope-guard-issue-301." >&2
    FAIL=1
fi
if [ "$FAIL" -eq 1 ]; then
    echo "" >&2
    echo "Required PR body shape (issue #301):" >&2
    echo "  Closes #N" >&2
    echo "  Scope guard: Do NOT touch <unrelated-area>; #M owns that." >&2
    echo "Full rule + canonical example: docs/onboarding.md#scope-guard-issue-301" >&2
    exit 1
fi

echo "PR body satisfies the scope-guard contract (issue #301)."
exit 0
