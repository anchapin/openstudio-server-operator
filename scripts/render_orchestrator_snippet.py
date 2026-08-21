#!/usr/bin/env python3
"""Render wave-orchestrator snippet templates into runnable bash (issue #382).

The Phase 3c snippets in ``docs/skill-snapshot/SKILL.md`` are templates,
not runnable bash: they carry literal placeholders (``{N}``, ``{slug}``,
``{PR_NUMBER}``, ``{affected_file}``, ``#M``) that the orchestrator must
substitute before executing or committing them. This helper is the
canonical substitution mechanism (documented as Phase 3c §0 in the
snapshot):

* ``{N}`` — the real issue number (e.g. ``370``).
* ``{slug}`` — the slug from the Phase 3a worktree name
  (``issue-{N}-{slug}``), which was derived from the issue title as the
  lowercase, hyphen-joined first 3 words (e.g. ``pre-report-back-git-
  status``). The existing worktree name is authoritative — pass it via
  ``--slug``; ``--title`` re-derives it when no worktree exists yet.
* ``issue-{N}-{slug}`` — the worktree directory name from Phase 3a; it
  emerges from the two atomic ``{N}`` + ``{slug}`` substitutions.
* ``{PR_NUMBER}`` — the PR number recorded in ``wave-state.json`` for the
  current issue.
* ``{affected_file}`` — the bare test-file stem inferred from the issue
  body (wave-planner ``extractFileRefs`` output, or the first
  ``tests/test_X.py`` mentioned). Full paths are normalized to the stem so
  ``tests/test_{affected_file}.py`` renders exactly once.
* ``#M`` / ``NEXT_ISSUE="#M"`` — the next-priority open issue from the
  same wave as ``#<number>``, or the literal ``n/a`` when no follow-up
  exists. ``#M`` is never emitted verbatim.

The render fails loudly — ``ValueError`` / nonzero exit — when any
``{placeholder}`` or ``#M`` survives substitution, so a template can never
fail silently the way verbatim execution would (#382's failure mode).
Stdlib only; no third-party dependencies.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ISSUE_PLACEHOLDER = "#M"
NA = "n/a"
_SLUG_WORDS = 3
# Placeholder-shaped tokens only: the lookbehind exempts bash variable
# expansions (``${BODY}``, ``${NEXT_ISSUE}``) which are legitimate rendered
# bash, and the body pattern exempts ``{}`` / ``{print $NF}`` awk payloads.
_RESIDUAL_TOKEN_RE = re.compile(r"(?<!\$)\{[A-Za-z_][A-Za-z0-9_]*\}")


def derive_slug(title: str) -> str:
    """First three whitespace words of *title*, lowercased and hyphen-joined.

    Hyphens inside a word survive (``Pre-report`` stays one component),
    matching the Phase 3a worktree-naming heuristic. When the worktree
    already exists its directory name (``issue-{N}-{slug}``) is the
    authoritative slug — pass that via ``--slug`` instead of re-deriving.
    """
    words: list[str] = []
    for word in title.split():
        cleaned = re.sub(r"[^a-z0-9-]", "", word.lower())
        if cleaned:
            words.append(cleaned)
        if len(words) == _SLUG_WORDS:
            break
    if not words:
        raise ValueError(f"cannot derive a slug from title {title!r}: no words")
    return "-".join(words)


def normalize_affected_file(value: str) -> str:
    """Reduce a test-file reference to the bare module stem.

    ``test_events_emit_failures``, ``test_events_emit_failures.py``, and
    ``tests/test_events_emit_failures.py`` all reduce to
    ``test_events_emit_failures``. The stem — not the path — is the
    canonical ``{affected_file}`` value; see :func:`render_snippet` for how
    the two template shapes consume it.
    """
    stem = value.strip().removesuffix(".py")
    if "/" in stem:
        stem = stem.rsplit("/", 1)[1]
    if not stem:
        raise ValueError(f"cannot normalize affected file {value!r}: empty stem")
    return stem


def _issue_number(value: str | int, *, what: str) -> str:
    """Validate *value* as a positive issue number, tolerating a ``#`` prefix."""
    text = str(value).strip()
    number = text.removeprefix("#")
    if not number.isdigit() or int(number) <= 0:
        raise ValueError(f"{what} must be a positive issue number, got {value!r}")
    return number


def render_snippet(
    template: str,
    *,
    issue_number: str | int,
    slug: str,
    pr_number: str | int = "",
    affected_file: str = "",
    next_issue: str = "",
) -> str:
    """Substitute every documented Phase 3c placeholder in *template*.

    ``next_issue`` accepts a bare number, a ``#number`` form, ``n/a``, or
    empty (treated as ``n/a``); it substitutes every ``#M`` occurrence —
    the rendered output never contains ``#M`` verbatim.

    ``{affected_file}`` takes the bare module stem (``test_events_emit_
    failures``) and renders the full relative path. Both template shapes
    work: the Phase 3c §1.5 long shape ``tests/test_{affected_file}.py``
    is rewritten wholesale (a token-wise replace would double the ``test_``
    prefix and ``.py`` suffix), and any bare ``{affected_file}`` becomes
    ``tests/<stem>.py``. Raises :class:`ValueError` when a placeholder
    survives substitution (unknown token, or a known token whose value was
    not supplied) — the fail-loud contract from issue #382.
    """
    number = _issue_number(issue_number, what="issue_number")
    rendered = template.replace("{N}", number).replace("{slug}", slug)
    if pr_number:
        rendered = rendered.replace("{PR_NUMBER}", _issue_number(pr_number, what="pr_number"))
    if affected_file:
        full_path = f"tests/{normalize_affected_file(affected_file)}.py"
        rendered = rendered.replace("tests/test_{affected_file}.py", full_path)
        rendered = rendered.replace("{affected_file}", full_path)
    if next_issue.strip() and next_issue.strip().lower() != NA:
        follow_up = "#" + _issue_number(next_issue, what="next_issue")
    else:
        follow_up = NA
    rendered = rendered.replace(ISSUE_PLACEHOLDER, follow_up)
    residual = sorted(set(_RESIDUAL_TOKEN_RE.findall(rendered)))
    if residual:
        raise ValueError(
            "unresolved placeholders remain after substitution: "
            + ", ".join(residual)
            + " — supply the matching --pr-number / --affected-file / value "
            "or fix the template (issue #382)"
        )
    if ISSUE_PLACEHOLDER in rendered:
        raise ValueError(
            f"the {ISSUE_PLACEHOLDER} placeholder survived substitution — "
            "this is a render_orchestrator_snippet bug; #M must always "
            "render to '#<real-issue>' or 'n/a' (issue #382)"
        )
    return rendered


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="render_orchestrator_snippet.py",
        description=(
            "Render a Phase 3c orchestrator snippet template into runnable "
            "bash by substituting {N}, {slug}, {PR_NUMBER}, {affected_file}, "
            "and #M (issue #382)."
        ),
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--template-file",
        help="read the template from this file ('-' for stdin)",
    )
    source.add_argument("--template", help="inline template string")
    parser.add_argument(
        "--issue-number",
        required=True,
        help="real issue number substituted for {N} (e.g. 370)",
    )
    parser.add_argument(
        "--slug",
        help="slug substituted for {slug} (e.g. pre-report-back-git-status)",
    )
    parser.add_argument(
        "--title",
        help="issue title; derives --slug as the first 3 hyphen-joined words",
    )
    parser.add_argument(
        "--pr-number",
        default="",
        help="PR number from wave-state.json substituted for {PR_NUMBER}",
    )
    parser.add_argument(
        "--affected-file",
        default="",
        help=(
            "target test file substituted for {affected_file}; a bare stem "
            "('test_events_emit_failures') or a path "
            "('tests/test_events_emit_failures.py') both work"
        ),
    )
    parser.add_argument(
        "--next-issue",
        default="",
        help=(
            "next-priority open issue from the same wave, substituted for "
            "#M as '#<number>'; 'n/a' or omitted renders 'n/a'"
        ),
    )
    return parser


def _read_template(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    if args.template_file is not None:
        if args.template_file == "-":
            return sys.stdin.read()
        return Path(args.template_file).read_text(encoding="utf-8")
    if args.template is not None:
        return args.template
    if sys.stdin.isatty():
        parser.error("no template given: pass --template, --template-file, or pipe one on stdin")
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if bool(args.slug) == bool(args.title):
        parser.error("provide exactly one of --slug or --title")
    template = _read_template(args, parser)
    try:
        rendered = render_snippet(
            template,
            issue_number=args.issue_number,
            slug=args.slug if args.slug else derive_slug(args.title),
            pr_number=args.pr_number,
            affected_file=args.affected_file,
            next_issue=args.next_issue,
        )
    except ValueError as exc:
        print(f"render_orchestrator_snippet: error: {exc}", file=sys.stderr)
        return 1
    if not rendered.endswith("\n"):
        rendered += "\n"
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
