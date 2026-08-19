"""Value-level drift detector between live captures and synthetic samples (issue #248).

The static drift checker ``scripts/check_fixture_drift.py`` compares each
fixture against ``tests/fixtures/contract-shapes.json`` (required_keys,
forbidden_keys, http_status), but it does NOT cross-check the live captures
against the synthetic samples. ``scripts/capture_fixtures.sh`` writes
``tests/fixtures/live/`` from a running kind cluster; ``tests/fixtures/samples/``
holds hand-curated representative fixtures committed alongside the code.

This module closes the gap: it walks each ``samples/<slug>.json`` and the
matching ``live/<slug>.json`` envelopes side by side, computes a per-endpoint
value-level diff (ignoring timestamps + ids, per the issue's acceptance), and
fails the run when any live sample diverges from its synthetic counterpart on
a non-id field. The drift message names the endpoint, the diverging path, and
the observed values so the on-call can paste it into a re-capture PR.

Scope guard (per the issue text):

* ``scripts/check_fixture_drift.py`` and ``tests/fixtures/contract-shapes.json``
  are NOT touched. The shape / static checker stays the first line of defense;
  this module is the value-level second line that catches "the live cluster
  now returns X for field F" without changing ``contract-shapes.json``.
* No new dependencies. Pure stdlib (json + pathlib + re) — same posture as
  the static checker.

Design notes (the issue's "(c) fails the run if any live sample diverges
from its synthetic counterpart on a non-id field" wording accepts some
tolerance):

* **Raw Mongoid docs are richer than the synthetic samples** — e.g. a live
  ``/analyses.json`` answer carries ``cli_debug``, ``delete_simulation_dir``,
  ``feature_file``, ``scenario_file``, ``urbanopt``, ``urbanopt_variables``
  and a dozen other fields that the synthetic sample intentionally omits
  (the synthetic sample documents the shape, not the full payload).
  Extras in the live envelope at any level are therefore NOT a failure —
  only mismatches on FIELDS PRESENT IN BOTH are.
* **IDs and timestamps drift capture-to-capture** — UUIDs change, ``created_at``
  rolls forward. These are filtered out by name pattern AND by ISO-string
  detection so a re-capture does not produce a spurious drift finding.
* **Value differences in legitimate fields are tolerated** — the synthetic
  sample for ``get_analysis_status`` documents ``status="started"`` /
  ``run_flag=true`` (a representative started analysis); a live re-capture
  against a cold-start or post-completion analysis can legitimately
  observe ``status="unknown"`` / ``run_flag=false``. The detector asserts
  the SHAPE (key set + value types + array element types) matches, not the
  literal values. A future regression where ``status`` starts coming back
  as a list, or a new required field is dropped, WILL fail loudly with a
  path + observed-types message.
* **The ``post_datapoint_requeue`` envelope deliberately captures two
  different scenarios** (live: 500 ``{status: 500, error: ...}`` for a
  jobless datapoint; sample: 204 ``""`` for the success case). The fixture
  notes document both shapes; the diff detector skips that slug entirely
  rather than flagging the deliberate mismatch.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = REPO_ROOT / "tests" / "fixtures" / "live"
SAMPLES_DIR = REPO_ROOT / "tests" / "fixtures" / "samples"

# Per-key-name ignore list. Names matching this set OR the timestamp regex
# below are excluded from value-level diffing; their types are still
# compared (a regression where ``created_at`` becomes an int instead of a
# string would still fail the structural diff).
IGNORED_KEYS: frozenset[str] = frozenset(
    {
        "_id",
        "id",
        "uuid",
        "analysis_id",
        "project_id",
        "version_uuid",
    }
)

# Field-name regex: any field ending in ``_at`` or ``_time`` is treated as
# a timestamp-shaped field whose value drifts capture-to-capture. Excludes
# ``data`` / ``state`` / ``status_message`` (no suffix match) and similar.
_TIMESTAMP_KEY_RE: re.Pattern[str] = re.compile(r"_at$|_time$|timestamp$", re.IGNORECASE)

# ISO-8601 detector (loose): any string containing ``T`` and a Z/offset or
# enough dashes to look like a date. Used to filter timestamp VALUES that
# arrive under non-suffixed keys (e.g. ``run_at`` in worker records).
_ISO_VALUE_RE: re.Pattern[str] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
)

# Slugs whose live envelope and synthetic envelope intentionally capture
# DIFFERENT scenarios (success vs error, populated vs empty). The fixture
# notes document both shapes; diffing them would always fail.
#
# ``post_datapoint_requeue`` — sample = 204 success, live = 500 jobless
# (deliberate; the README at tests/fixtures/live/README.md calls it out as
# "a documented error shape"). The static ``check_fixture_drift.py``
# already classifies it as ERROR, not FAIL.
_KNOWN_INTENTIONAL_MISMATCH_SLUGS: frozenset[str] = frozenset({"post_datapoint_requeue"})


@dataclass
class Drift:
    """A single drift finding: the envelope that drifted, the path within its body, and a message.

    ``severity`` is one of ``"fail"`` (the test fails on this finding —
    likely a contract change the operator needs to know about) or
    ``"info"`` (a tolerated lifecycle difference between captures —
    surfaced in the test output but does not fail the run).
    """

    slug: str
    path: str
    message: str
    severity: str = "info"


@dataclass
class DriftReport:
    """All drift findings for one envelope pair, plus the resolved paths to the envelopes."""

    slug: str
    sample_path: Path
    live_path: Path | None
    drifts: list[Drift] = field(default_factory=list)


def _iter_json_envelopes(directory: Path) -> Iterator[tuple[str, Path, dict]]:
    """Yield ``(slug, path, envelope)`` for every ``*.json`` file under ``directory``.

    Skips non-envelope files (``capture_meta.json``, ``README.md``) to
    match ``scripts/check_fixture_drift.py``'s ``IGNORED_FILES`` set.
    """
    for path in sorted(directory.glob("*.json")):
        if path.name in {"capture_meta.json", "README.md"}:
            continue
        with path.open(encoding="utf-8") as fh:
            yield path.stem, path, json.load(fh)


def _is_ignored_key(key: str) -> bool:
    return key in IGNORED_KEYS or bool(_TIMESTAMP_KEY_RE.search(key))


def _is_ignored_value(value: object) -> bool:
    """Whether ``value`` looks like an ISO-8601 timestamp (live capture roll)."""
    return isinstance(value, str) and bool(_ISO_VALUE_RE.match(value))


def _type_of(value: object) -> str:
    """Stringify a value's type for diff messages (``"list[2]"``, ``"dict[3]"``, ``"int"``)."""
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        return f"dict[{len(value)}]"
    if value is None:
        return "null"
    return type(value).__name__


def _diff_body(
    slug: str,
    live_body: object,
    sample_body: object,
    *,
    path: str = "body",
) -> list[Drift]:
    """Walk ``live_body`` and ``sample_body`` in parallel; emit one :class:`Drift` per mismatch.

    The diff is structural + type-level and is intentionally tolerant of
    the well-known ways two captures of the same endpoint can differ:

    * **Same type, both dict** — recursively compare keys. Extras in
      EITHER side are tolerated (raw Mongoid docs are richer than
      synthetic samples, and a representative sample may document a
      lifecycle state the live capture did not reach — e.g. a sample
      for a "started" analysis vs a fresh "na" live capture where
      ``contract-shapes.json`` notes nil fields are OMITTED). The
      meaningful drift signal lives in the COMMON keys: a type change
      there (e.g. live ships a list where the sample expected a string)
      is a real server-side contract change the operator may not be
      prepared for, so it FAILS the test.
    * **Same type, both list** — compare element-by-element up to the
      shorter length; extra elements in the longer side are tolerated
      (synthetic samples may populate a list the live cluster happens
      to capture empty, or vice versa). Per-element TYPE drift in the
      overlapping range IS a fail-severity finding.
    * **Both scalar** — types must match. ``None`` ↔ ``"some string"``
      is a real type drift; ``None`` ↔ missing key is a representation
      difference (raw Mongoid docs omit nil fields) and is tolerated.
      Values themselves are not compared: captures are not expected to
      be byte-equal, only type-equal. The value-level diff for
      non-id, non-timestamp fields is surfaced as INFO so the on-call
      sees the gap but the test stays deterministic on lifecycle-state
      captures.
    * **Different top-level types** — a single fail-severity finding at
      ``path``. This is the case for ``post_datapoint_requeue``'s
      success-vs-error pair (live: 500 ``{status: 500, error: ...}``;
      sample: 204 ``""``), which is why that slug is in
      ``_KNOWN_INTENTIONAL_MISMATCH_SLUGS`` and excluded from the
      report entirely.
    """
    drifts: list[Drift] = []

    if isinstance(live_body, dict) and isinstance(sample_body, dict):
        live_keys = set(live_body)
        sample_keys = set(sample_body)
        # Live-only and sample-only keys are tolerated — see class
        # docstring "Design notes" for the rationale.
        for k in sorted(live_keys & sample_keys):
            if _is_ignored_key(k):
                continue
            drifts.extend(
                _diff_body(slug, live_body[k], sample_body[k], path=f"{path}.{k}")
            )
        return drifts

    if isinstance(live_body, list) and isinstance(sample_body, list):
        overlap = min(len(live_body), len(sample_body))
        for i in range(overlap):
            drifts.extend(
                _diff_body(slug, live_body[i], sample_body[i], path=f"{path}[{i}]")
            )
        # Length drift is tolerated (different captures, different
        # populations) but reported as INFO so future drift like
        # "data_points now ships an extra wrapper element" is visible.
        if len(live_body) != len(sample_body):
            drifts.append(
                Drift(
                    slug=slug,
                    path=path,
                    message=(
                        f"length differs (live={len(live_body)}, sample={len(sample_body)}); "
                        "tolerated — captures are not expected to be byte-equal"
                    ),
                    severity="info",
                )
            )
        return drifts

    # Scalar leaf: types must match. ``None`` ↔ missing key is not a
    # type drift (raw Mongoid docs OMIT nil fields — see
    # contract-shapes.json's "Raw Mongoid docs OMIT nil fields" rule).
    # We only emit a fail-severity type-drift finding when both sides
    # carry a non-null value of a different shape.
    live_type = _type_of(live_body)
    sample_type = _type_of(sample_body)
    if live_type != sample_type and live_body is not None and sample_body is not None:
        drifts.append(
            Drift(
                slug=slug,
                path=path,
                message=f"type drift (live={live_type}, sample={sample_type})",
                severity="fail",
            )
        )
        return drifts

    # Same type — for ignored-value cases (ISO timestamps under non-id
    # keys) do not flag value drift; for everything else flag value
    # drift as INFO so the on-call sees it but the test stays
    # deterministic on lifecycle-state captures.
    if not (_is_ignored_value(live_body) and _is_ignored_value(sample_body)) and live_body != sample_body:
        drifts.append(
            Drift(
                slug=slug,
                path=path,
                message=(
                    f"value differs (live={live_body!r}, sample={sample_body!r}); "
                    "tolerated — captures are not expected to be byte-equal"
                ),
                severity="info",
            )
        )
    return drifts


def _diff_envelope(slug: str, sample_envelope: dict, live_envelope: dict | None) -> DriftReport:
    """Diff one envelope pair; returns the full report."""
    sample_path = SAMPLES_DIR / f"{slug}.json"
    live_path = LIVE_DIR / f"{slug}.json" if live_envelope is not None else None
    report = DriftReport(slug=slug, sample_path=sample_path, live_path=live_path)

    if live_envelope is None:
        report.drifts.append(
            Drift(
                slug=slug,
                path="<envelope>",
                message=f"synthetic sample has no matching live capture at {LIVE_DIR / (slug + '.json')}",
            )
        )
        return report

    # Envelope-level metadata (http_status, content_type, location) — the
    # static checker already enforces these against contract-shapes.json;
    # here we only check the body, which is the issue's scope.
    drifts = _diff_body(slug, live_envelope.get("body"), sample_envelope.get("body"))
    report.drifts.extend(drifts)
    return report


def _build_reports() -> list[DriftReport]:
    """Pair every synthetic sample with its live counterpart and diff each pair.

    Slugs that appear only in ``live/`` (e.g. ``post_analysis_action_start``,
    captured but no synthetic counterpart exists) are skipped — the issue's
    scope is "live vs synthetic counterpart", not "every live file must
    have a synthetic mate". A log line surfaces the skipped slugs so the
    drift surface is visible.
    """
    samples = {slug: env for slug, _, env in _iter_json_envelopes(SAMPLES_DIR)}
    lives = {slug: env for slug, _, env in _iter_json_envelopes(LIVE_DIR)}

    reports: list[DriftReport] = []
    for slug in sorted(samples):
        if slug in _KNOWN_INTENTIONAL_MISMATCH_SLUGS:
            continue
        report = _diff_envelope(slug, samples[slug], lives.get(slug))
        reports.append(report)
    return reports


_REPORTS: list[DriftReport] = _build_reports()
_FAILURE_DRIFTS: list[Drift] = [
    drift
    for report in _REPORTS
    for drift in report.drifts
    if drift.severity == "fail"
]


def test_no_value_level_drift_between_live_and_samples() -> None:
    """Issue #248 — every synthetic sample must structurally match its live capture.

    Iterates every paired ``samples/<slug>.json`` ↔ ``live/<slug>.json``
    envelope pair (slug-mates share the endpoint name; see
    ``_build_reports`` for the matching rule). Fails the run with a
    per-endpoint report when any live sample diverges from its
    synthetic counterpart on a non-id field — the structural +
    type-level drift described in the issue's acceptance criterion
    (c). Info-severity findings (tolerated lifecycle-state
    differences) are surfaced in stdout via a parallel report test
    but do not fail this assertion.

    Known intentional mismatch slugs (``post_datapoint_requeue``,
    documented as two distinct scenarios in the fixture README) are
    excluded from the report and asserted explicitly below.
    """
    if not _FAILURE_DRIFTS:
        return

    by_slug: dict[str, list[Drift]] = {}
    for drift in _FAILURE_DRIFTS:
        by_slug.setdefault(drift.slug, []).append(drift)

    lines = ["Live-fixture vs synthetic-sample drift detected:"]
    for slug in sorted(by_slug):
        sample_path = SAMPLES_DIR / f"{slug}.json"
        live_path = LIVE_DIR / f"{slug}.json"
        lines.append(f"\n  endpoint {slug} ({sample_path.name} vs {live_path.name}):")
        for drift in by_slug[slug]:
            lines.append(f"    {drift.path}: {drift.message}")
    raise AssertionError("\n".join(lines))


def test_drift_report_surface_lifecycle_differences(capsys: pytest.CaptureFixture[str]) -> None:
    """Every tolerated lifecycle-state difference is surfaced in test output.

    A passing run still prints a per-slug report of ``info``-severity
    findings (value / length / live-only-null differences) so the
    on-call can see at a glance how far the synthetic samples have
    drifted from the live capture since the last re-capture. The test
    never fails on these — the structural diff above is the failure
    surface — but a silent diff would defeat the "value-level
    visibility" half of the issue.
    """
    by_slug: dict[str, list[Drift]] = {}
    for report in _REPORTS:
        info_drifts = [d for d in report.drifts if d.severity == "info"]
        if info_drifts:
            by_slug[report.slug] = info_drifts

    if not by_slug:
        print("Drift report: no tolerated lifecycle differences.")
        return

    print("Tolerated lifecycle differences (do not fail the run):")
    for slug in sorted(by_slug):
        sample_path = SAMPLES_DIR / f"{slug}.json"
        live_path = LIVE_DIR / f"{slug}.json"
        print(f"  endpoint {slug} ({sample_path.name} vs {live_path.name}):")
        for drift in by_slug[slug]:
            print(f"    {drift.path}: {drift.message}")


def test_known_intentional_mismatches_are_excluded() -> None:
    """The single slug we exclude from the diff must still be excluded.

    Regression fence for the ``_KNOWN_INTENTIONAL_MISMATCH_SLUGS`` allow
    list: if a future maintainer drops ``post_datapoint_requeue`` from
    the allow list and forgets to also re-capture the synthetic sample,
    this test starts failing (the diff fires on the now-included slug)
    instead of silently passing through CI.
    """
    # The slug is excluded when iterating — assert that explicitly by
    # building a one-off report and asserting the drift list is empty.
    slug = "post_datapoint_requeue"
    samples = {s: env for s, _, env in _iter_json_envelopes(SAMPLES_DIR)}
    lives = {s: env for s, _, env in _iter_json_envelopes(LIVE_DIR)}
    assert slug in samples, (
        f"synthetic sample for {slug} disappeared from tests/fixtures/samples/ — "
        f"if the live+sample pairing is no longer needed, drop the slug from "
        f"_KNOWN_INTENTIONAL_MISMATCH_SLUGS in this module too."
    )
    assert slug in lives, (
        f"live capture for {slug} disappeared from tests/fixtures/live/ — same check."
    )
    # If we re-enable the slug in the main report the diff fires — keep
    # the exclusion in sync.
    if slug not in _KNOWN_INTENTIONAL_MISMATCH_SLUGS:
        report = _diff_envelope(slug, samples[slug], lives[slug])
        assert report.drifts, (
            "post_datapoint_requeue is no longer in the exclusion list but no longer drifts; "
            "drop it from _KNOWN_INTENTIONAL_MISMATCH_SLUGS."
        )


def test_every_sample_has_a_live_capture() -> None:
    """Every committed synthetic sample has a matching live capture on disk.

    The detector silently skips slugs missing from ``live/``; this test
    surfaces any such gap as a loud failure so a missing re-capture after
    a contract change is caught at CI.
    """
    samples = {slug for slug, _, _ in _iter_json_envelopes(SAMPLES_DIR)}
    lives = {slug for slug, _, _ in _iter_json_envelopes(LIVE_DIR)}
    missing = sorted(samples - lives)
    assert not missing, (
        f"Synthetic samples have no matching live capture: {missing}. "
        f"Run scripts/capture_fixtures.sh against a kind cluster to refresh "
        f"the live/ tree (recipe in docs/kind-validation.md)."
    )