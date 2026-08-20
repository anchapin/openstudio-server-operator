<!--
PR body conventions for this repo (issues #88 and #301).

Closing keyword (PR auto-closes the issue when merged to `develop`):
  Closes #N     Fixes #N     Resolves #N

Keep-open keyword (PR stays open after merge):
  Refs #N       for #N       touches #N

Scope guard (#301): a single `Scope guard:` LINE — NOT a markdown
heading. The script greps `^[[:space:]]*[-*+]?[[:space:]]*Scope guard:`
and rejects `## Scope guard:` / `# Scope guard:`. The line must include:
  - a rationale phrase (Do NOT / owns / out of scope / not in scope /
    do not modify / unchanged / untouched), and
  - a reference to at least one issue `#N`.

Full rule + canonical example: docs/onboarding.md#scope-guard-issue-301.
-->

## What

<!-- One paragraph: what this PR changes. Bullet list is fine for multi-file PRs. -->

## Why

<!-- One paragraph: motivation. Link the issue this PR addresses. -->

## Verification

<!-- How this was tested locally. Cite the actual commands (e.g. `ruff check .`, `.venv/bin/pytest`, fixture capture, kind cluster). -->

## Out of scope

<!-- IMPORTANT: the line below is the Scope guard (#301) block. It must be a
     single LINE starting with `Scope guard:` — NOT a heading. Pick a
     rationale phrase + reference at least one issue `#N`. Delete this
     comment before submitting. Example:
       Scope guard: docs/onboarding.md is untouched; #220 owns the onboarding test-count guard. -->

Scope guard: <area> is unchanged; #N owns that.

<!--
Below: use `Closes #N` for closing PRs (squash subject + body must agree).
       Use `Refs #N` for keep-open PRs. Replace `#N` with the issue number.
-->

Refs #N