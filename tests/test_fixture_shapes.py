"""Fixture shape tests for issue #19.

Validates, WITHOUT a live cluster:
  * tests/fixtures/contract-shapes.json covers exactly the endpoints the
    operator's API contract documents;
  * the committed synthetic samples (tests/fixtures/samples/, derived from
    the contract file + v3.11.0 controllers) conform to those shapes;
  * the drift checker actually rejects drift (negative tests).

When live fixtures exist under tests/fixtures/live/ (captured by
scripts/capture_fixtures.sh), they are shape-checked too; absent fixtures
skip gracefully. The checker logic lives in scripts/check_fixture_drift.py
(kept out of the package on purpose — it is dev tooling) and is imported
here via importlib so no packaging changes are needed.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
SHAPES_PATH = FIXTURES_DIR / "contract-shapes.json"
SAMPLES_DIR = FIXTURES_DIR / "samples"
LIVE_DIR = FIXTURES_DIR / "live"

OPERATOR_ENDPOINT_SLUGS = {
    "get_analyses",
    "get_analysis_status",
    "get_analysis_page_data",
    "get_data_points_status",
    "get_data_points",
    "get_analysis_soft_stop",
    "post_analysis_action",
    "post_datapoint_requeue",
    "delete_analysis",
}


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_fixture_drift", REPO_ROOT / "scripts" / "check_fixture_drift.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_fixture_drift"] = module
    spec.loader.exec_module(module)
    return module


CHECKER = _load_checker()


def _load_shapes() -> dict:
    with SHAPES_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)["endpoints"]


def _sample(name: str) -> dict:
    with (SAMPLES_DIR / name).open(encoding="utf-8") as fh:
        return json.load(fh)


def test_contract_shapes_covers_operator_endpoints():
    shapes = _load_shapes()
    assert set(shapes) == OPERATOR_ENDPOINT_SLUGS
    for slug, spec in shapes.items():
        assert spec["response"]["http_status"], slug
        assert "notes" in spec["response"], slug


@pytest.mark.parametrize(
    "name",
    sorted(p.name for p in SAMPLES_DIR.glob("*.json")),
)
def test_sample_fixture_conforms(name):
    envelope = _sample(name)
    slug = Path(name).stem
    base_slug, spec = CHECKER.resolve_spec(slug, _load_shapes())
    assert spec is not None, f"no contract shape for sample '{slug}'"
    verdict = CHECKER.check_envelope(base_slug, envelope, spec)
    assert verdict.problems == []
    if slug == "get_analysis_page_data_notfound":
        assert verdict.outcome == "ERROR"
    else:
        assert verdict.outcome == "PASS"


def test_live_fixtures_conform_or_skip():
    live_files = [
        p
        for p in sorted(LIVE_DIR.glob("*.json"))
        if p.name not in CHECKER.IGNORED_FILES
    ]
    if not live_files:
        pytest.skip(
            "no live fixtures captured yet — run scripts/capture_fixtures.sh "
            "against the kind 3.11.0 stack (docs/kind-validation.md)"
        )
    shapes = _load_shapes()
    for path in live_files:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        slug = path.stem
        base_slug, spec = CHECKER.resolve_spec(slug, shapes)
        assert spec is not None, f"{path.name}: unknown endpoint '{slug}'"
        verdict = CHECKER.check_envelope(base_slug, envelope, spec)
        assert verdict.problems == [], f"{path.name}: {verdict.problems}"


def test_checker_rejects_missing_required_key():
    envelope = _sample("get_analysis_page_data.json")
    del envelope["body"]["analysis"]["data_points"]
    verdict = CHECKER.check_envelope(
        "get_analysis_page_data", envelope, _load_shapes()["get_analysis_page_data"]
    )
    assert any("missing required key 'data_points'" in p for p in verdict.problems)


def test_checker_rejects_forbidden_key():
    envelope = _sample("get_analyses.json")
    envelope["body"][0]["start_time"] = "2026-08-18T12:03:21.000Z"
    verdict = CHECKER.check_envelope(
        "get_analyses", envelope, _load_shapes()["get_analyses"]
    )
    assert any("forbidden key 'start_time'" in p for p in verdict.problems)


def test_checker_rejects_unexpected_http_status():
    envelope = _sample("post_datapoint_requeue.json")
    envelope["http_status"] = 200
    verdict = CHECKER.check_envelope(
        "post_datapoint_requeue", envelope, _load_shapes()["post_datapoint_requeue"]
    )
    assert any("unexpected HTTP status" in p for p in verdict.problems)
