#!/usr/bin/env python3
"""Fixture drift checker for issue #19.

Diffs captured OpenStudio Server REST fixtures (tests/fixtures/live/, written
by scripts/capture_fixtures.sh) — and the committed synthetic samples
(tests/fixtures/samples/) — against the endpoint shapes documented in the
operator's API contract, distilled into tests/fixtures/contract-shapes.json.

Verdicts per fixture:
  PASS   success-shape response, all required keys present, none forbidden
  ERROR  fixture is a documented error shape (e.g. captured against a
         non-existent ObjectId sentinel) — allowed, does not fail the run
  FAIL   shape drift vs the contract (missing required key, forbidden key
         present, unexpected HTTP status) — exits 1
  WARN   conditionally-populated key absent (e.g. ip_address before start)

Full value-level drift detection needs a live run; this checker is the static
key/shape layer (see docs/kind-validation.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SHAPES_PATH = REPO_ROOT / "tests" / "fixtures" / "contract-shapes.json"
LIVE_DIR = REPO_ROOT / "tests" / "fixtures" / "live"
SAMPLES_DIR = REPO_ROOT / "tests" / "fixtures" / "samples"

IGNORED_FILES = {"capture_meta.json", "README.md"}


@dataclass
class Verdict:
    outcome: str
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)


def load_shapes(path: Path = SHAPES_PATH) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)["endpoints"]


def _check_keys(
    label: str,
    obj: dict,
    required: list[str],
    forbidden: list[str],
    expected: list[str],
    verdict: Verdict,
) -> None:
    for key in required:
        if key not in obj:
            verdict.problems.append(f"{label}: missing required key '{key}'")
    for key in forbidden:
        if key in obj:
            verdict.problems.append(f"{label}: forbidden key '{key}' present")
    for key in expected:
        if key not in obj:
            verdict.warnings.append(f"{label}: conditional key '{key}' absent (OK pre-start)")
    extras = sorted(set(obj) - set(required) - set(forbidden) - set(expected))
    if extras:
        verdict.infos.append(f"{label}: extra keys beyond contract (normal for raw docs): {extras}")


def _check_nested(label: str, value: object, spec: dict, verdict: Verdict) -> None:
    if isinstance(value, list):
        if not value:
            verdict.infos.append(f"{label}: empty list — item keys unverifiable")
            return
        for i, item in enumerate(value):
            if not isinstance(item, dict):
                verdict.problems.append(f"{label}[{i}]: expected object, got {type(item).__name__}")
                continue
            _check_keys(
                f"{label}[{i}]",
                item,
                spec.get("item_required_keys", []),
                spec.get("item_forbidden_keys", []),
                spec.get("item_expected_keys", []),
                verdict,
            )
    elif isinstance(value, dict):
        _check_keys(
            label,
            value,
            spec.get("required_keys", []),
            spec.get("item_forbidden_keys", []),
            spec.get("item_expected_keys", []),
            verdict,
        )
    else:
        verdict.problems.append(f"{label}: expected object or array, got {type(value).__name__}")


SLUG_SUFFIX_ALIASES = ("_notfound", "_stop", "_start")


def resolve_spec(slug: str, shapes: dict) -> tuple[str, dict] | tuple[None, None]:
    """Map a fixture filename stem to its contract shape.

    Capture files carry per-request suffixes (post_analysis_action_stop,
    get_analysis_page_data_notfound, ...); the contract is keyed by endpoint.
    """
    if slug in shapes:
        return slug, shapes[slug]
    for suffix in SLUG_SUFFIX_ALIASES:
        if slug.endswith(suffix):
            base = slug[: -len(suffix)]
            if base in shapes:
                return base, shapes[base]
    return None, None


def check_envelope(slug: str, envelope: dict, spec: dict) -> Verdict:
    verdict = Verdict(outcome="PASS")
    status = envelope.get("http_status")
    body = envelope.get("body")
    resp = spec["response"]
    error_statuses = spec.get("error_shape", {}).get("http_status", [])

    if status not in resp["http_status"]:
        if status in error_statuses:
            verdict.outcome = "ERROR"
            verdict.infos.append(f"documented error shape (HTTP {status})")
            return verdict
        verdict.problems.append(f"unexpected HTTP status {status} (allowed: {resp['http_status']})")
        return verdict

    body_type = resp.get("body_type")
    if body_type == "empty":
        if body not in ("", None):
            verdict.problems.append(f"expected empty body, got: {str(body)[:80]!r}")
        if resp.get("expect_location") and not envelope.get("location"):
            verdict.problems.append("expected a Location header")
        return verdict
    if body_type == "html-or-empty":
        if resp.get("expect_location") and not envelope.get("location"):
            verdict.problems.append("expected a Location header on the redirect")
        return verdict

    if not isinstance(body, (dict, list)):
        verdict.problems.append(f"expected JSON object/array body, got {type(body).__name__}")
        return verdict

    null_key = spec.get("error_shape", {}).get("null_key")
    if null_key and isinstance(body, dict) and set(body) == {null_key} and body[null_key] is None:
        verdict.outcome = "ERROR"
        verdict.infos.append(f"documented not-found shape ({{{null_key}: null}} over HTTP 200)")
        return verdict

    if isinstance(body, list):
        _check_nested(
            "body",
            body,
            {
                "item_required_keys": resp.get("item_required_keys", []),
                "item_forbidden_keys": resp.get("item_forbidden_keys", []),
                "item_expected_keys": resp.get("item_expected_keys", []),
            },
            verdict,
        )
        return verdict

    alternatives = resp.get("any_of_top_keys")
    if alternatives:
        matched = [keys for keys in alternatives if all(k in body for k in keys)]
        if not matched:
            verdict.problems.append(f"none of the alternative key-sets {alternatives} present")
            return verdict
        chosen = matched[0]
        top_key = chosen[0]
        if "status_view_keys" in resp:
            value = body.get(top_key)
            if isinstance(value, list):
                _check_nested(
                    f"body.{top_key}",
                    value,
                    {"item_required_keys": resp["status_view_keys"]},
                    verdict,
                )
            elif isinstance(value, dict):
                _check_keys(
                    f"body.{top_key}",
                    value,
                    resp["status_view_keys"],
                    [],
                    [],
                    verdict,
                )
            else:
                verdict.problems.append(f"body.{top_key}: expected object or array")
        return verdict

    _check_keys(
        "body",
        body,
        resp.get("required_keys", []),
        resp.get("item_forbidden_keys", []),
        [],
        verdict,
    )
    for top_key, nested_spec in resp.get("nested", {}).items():
        if top_key not in body:
            continue
        _check_nested(f"body.{top_key}", body[top_key], nested_spec, verdict)
    return verdict


def iter_fixtures(directory: Path):
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        if path.name in IGNORED_FILES:
            continue
        with path.open(encoding="utf-8") as fh:
            yield path, json.load(fh)


def run(directory: Path, shapes: dict) -> int:
    fails = 0
    errors = 0
    passes = 0
    seen = 0
    for path, envelope in iter_fixtures(directory):
        slug = path.stem
        seen += 1
        _base_slug, spec = resolve_spec(slug, shapes)
        if spec is None:
            print(f"FAIL  {path.name}: no contract shape for endpoint '{slug}'")
            fails += 1
            continue
        verdict = check_envelope(slug, envelope, spec)
        if verdict.problems:
            verdict.outcome = "FAIL"
            fails += 1
        elif verdict.outcome == "ERROR":
            errors += 1
        else:
            passes += 1
        print(f"{verdict.outcome:<5} {path.name} (HTTP {envelope.get('http_status')})")
        for problem in verdict.problems:
            print(f"        DRIFT: {problem}")
        for warning in verdict.warnings:
            print(f"        WARN:  {warning}")
        for info in verdict.infos:
            print(f"        info:  {info}")
    if seen == 0:
        print(f"No fixtures found in {directory}")
        return 0
    print(f"\n{directory}: {seen} fixture(s): {passes} pass, {errors} error-shape, {fails} fail")
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--live", action="store_true", help="check tests/fixtures/live/")
    group.add_argument("--samples", action="store_true", help="check tests/fixtures/samples/")
    parser.add_argument(
        "--shapes",
        type=Path,
        default=SHAPES_PATH,
        help="path to contract-shapes.json (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    shapes = load_shapes(args.shapes)
    directory = LIVE_DIR if args.live else SAMPLES_DIR
    return run(directory, shapes)


if __name__ == "__main__":
    sys.exit(main())
