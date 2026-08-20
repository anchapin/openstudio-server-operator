# Contributing

Thanks for your interest in this project. This file is the canonical,
quick-reference entry point for branch, PR, and merge conventions.
The deeper walkthrough (venv setup, the 5-step "add a new OSCM timer
handler" pattern, audit-doc update rules, working-rules-that-bite,
good-first-PR candidates) lives in
[`docs/onboarding.md`](./docs/onboarding.md) — read that first if you
are new here.

## Branching model

- `develop` — the default branch. All work lands here. Direct pushes are
  allowed for the owner.
- `main` — the release branch. Protected by the `protect-main` ruleset;
  no direct or force pushes. Updates require a PR **from `develop`**.
  The required `guard-branch-pairing` CI check fails any PR into `main`
  whose source branch is not `develop`. **Never push `main` directly.**
- The `require-ci-on-develop-prs` ruleset requires `lint` + `test` on
  every PR merged into `develop`.

## Branch naming

```
fix/issue-N-slug
feat/issue-N-slug
docs/issue-N-slug
chore/issue-N-slug
```

Use a lowercase, hyphenated slug that names the change. The
`issue-N-` prefix lets `gh pr merge` produce a clean closing keyword on
squash-merge without further edits.

## PR body keywords

The PR body MUST include `Closes #N` (or `Fixes #N` / `Resolves #N`)
when the issue should close on merge, and `Refs #N` (or `for #N` /
`touches #N`) when it should stay open. **Scope guard:** `develop`
closes an issue when **any** commit with a closing keyword lands there
— including auto-generated squash-merge subjects. The keywords therefore
matter in BOTH the PR body AND the squash subject.

## Merge-subject hygiene (#88)

`develop` is the default branch, so GitHub closes an issue when **any**
commit with a closing keyword lands there — including auto-generated
squash-merge subjects. Two consequences:

- For **keep-open PRs**, keep closing keywords OUT of the PR title AND
  out of every commit subject on the branch. Use `Refs #N` /
  `for #N` / `touches #N` everywhere.
- For **closing PRs**, the closing keyword IS desired in both the
  squash subject (`fix: resolve #N — …`) and the PR body
  (`Closes #N`).
- Always merge with an explicit subject override rather than relying on
  the auto-generated one:

  ```bash
  gh pr merge N --squash --subject "fix: resolve #N — short summary" --body "Closes #N"
  ```

## Required CI checks

The `require-ci-on-develop-prs` ruleset requires two jobs to pass on
PR merges into `develop`:

- `lint` — `ruff check .`
- `test`  — `.venv/bin/pytest`

The `guard-branch-pairing` job is a required status check on **`main`**
(not `develop`); its name is part of the public CI contract — do not
rename it. Full rationale and the maintainer admin-bypass path (for
GitHub Actions outages) live in
[`docs/onboarding.md`](./docs/onboarding.md#branching--pr-conventions).

## Where to go next

- First time here? Start with
  [`docs/onboarding.md`](./docs/onboarding.md) (issue #178) — the
  venv drift guard, the 5-step "add a new OSCM timer handler" pattern,
  and the working-rules-that-bite section are all there.
- Looking for a small first PR? See the **Good first PR candidates**
  list at the end of `docs/onboarding.md` (doctypo fixes, fixture
  variants, focused doc-comment drift fixes). Avoid the
  D04/D05/D11/D12 contract boundaries until you have read the audit
  doc.
- AI coding agents: see [`AGENTS.md`](./AGENTS.md) for the codebase
  map, drift guards, and the venv drift guard (issue #71).