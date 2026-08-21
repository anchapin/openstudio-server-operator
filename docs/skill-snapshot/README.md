# Skill snapshots (issue #379)

This directory exists ONLY to give wave branches a reviewable diff when a
sub-agent modifies the orchestrator skill files. **The canonical skill
files live in the skill home**
(`~/.config/opencode/skill/github-wave-orchestrator/`), **not in this
repository** — every file here is a disposable copy of a canonical file.

## Naming convention

A wave that modifies a skill-home file commits a **wave-numbered** copy:

```
docs/skill-snapshot/<basename>.wave-<N>.md     # e.g. SKILL.wave-11.md
```

where `<N>` is `.current_wave` from `wave-state.json` at branch time. The
rule is specified in the skill-home `SKILL.md` Phase 3a (skill-snapshot
copy rule) and is mirrored verbatim in the newest numbered snapshot.

Why: waves in one cycle branch from a shared pre-merge `develop`, so any
two skill-touching waves writing the same snapshot path collide add/add
at rebase — the 2026-08-20 cycle produced exactly that on
`docs/skill-snapshot/SKILL.md` four times (waves 1-4), burning a CI
sub-agent cycle per rebase iteration. Distinct wave numbers make the
collision structurally impossible. Regression proof:
`tests/test_wave_orchestrator_e2e.py::TestSkillSnapshotNumberedNames`
(concurrent numbered snapshots rebase clean against real git; the legacy
single-name pattern reproduces the add/add conflict).

## Migration from the pre-#379 single-name convention

No renames. The un-numbered files (`SKILL.md`, `REFERENCE.md`,
`scripts/…`) are **frozen legacy snapshots** — waves must never add or
edit them again. They stay so historical diffs remain valid and
`tests/test_render_orchestrator_snippet.py` (which parses the §0
placeholder-substitution contract out of the frozen `SKILL.md`) keeps
passing. From wave 11 of the 2026-08-20 cycle onward, every skill
snapshot lands under its wave-numbered name; the highest-numbered
snapshot on `origin/develop` always mirrors the canonical skill-home
file as of that wave.

## Rejected alternatives (from issue #379)

1. **Skill home IS the worktree** — a dedicated bare repo plus a
   long-lived skill branch checked out as the worktree, so skill-only
   changes land as ordinary commits and no snapshot is needed at all.
   The cleanest long-term answer, rejected for #379 because it requires
   bootstrap infrastructure outside this repository plus wave-planner /
   sub-agent-template changes that the issue's scope guard forbids.
   Revisit if snapshot churn grows.
2. **Skip the PR when the only diff is the snapshot** — fast-forward a
   tracking branch on the skill-home side instead. Rejected: it drops
   the visible-diff review property the snapshot exists for, and it
   requires changing the Phase 3c verification loop and the sub-agent
   template (out of scope for #379).
