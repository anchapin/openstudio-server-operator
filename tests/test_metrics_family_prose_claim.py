"""CI drift gate (issue #387): the ``N counters + M gauges + K histograms`` prose
claim in ``README.md`` and ``docs/audit-dryrun-idempotency.md`` must agree with
the canonical tuple families in ``tests/test_metrics_endpoint.py``.

Why this test exists
--------------------
The existing ``tests/test_metrics_endpoint.py::EXPECTED_*_FAMILIES`` tuples
gate the **tuple side** of the metrics registry (the assertions
``test_declared_counters_match_expected_set`` / ``..._gauges_...`` /
``..._histograms_...`` walk ``vars(metrics)`` and compare against the tuples).
But the human-readable claim repeated in four prose locations — the
``README.md`` metrics section, the audit ``Appendix D`` introduction, the
``CHANGELOG.md`` Unreleased entry, and the ``docs/kind-validation.md``
acceptance criteria — is NOT gated by any test. Issue #387 caught this drift
in the wild: the prose claimed ``16 counters + 4 gauges + 1 histogram`` while
the actual registry (post-#306 / #308 / #310 / #312) has grown to ``18
counters + 7 gauges + 3 histograms``. Without a test against the prose, this
class of drift can land again silently — on-call operators trusting the
README as the metrics surface will see dashboards marked "missing metric"
when the registry was in fact the source of truth.

This test parses the prose claim out of ``README.md`` and
``docs/audit-dryrun-idempotency.md`` (the two locations that carry the
claim in a parseable ``N counters + M gauges + K histograms`` form, the
same shape the existing test-count guard uses for ``# N tests across M
files``) and asserts equality with
``len(EXPECTED_COUNTER_FAMILIES)`` / ``EXPECTED_GAUGE_FAMILIES`` /
``EXPECTED_HISTOGRAM_FAMILIES``. A failure prints the actual count and
the offending claim so the fix is a one-line bump in the prose.

Scope guard (issue #301)
------------------------
This test does NOT touch ``src/openstudio_operator/metrics.py`` itself or
the tuples in ``tests/test_metrics_endpoint.py`` — the tuples are the
SOLE source of truth, and the prose is held to them. The seven new
families are documented in the README metrics table and the audit
``Appendix D`` table; the prose claim is the head-count only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# The EXPECTED_*_FAMILIES tuples live in tests/test_metrics_endpoint.py
# (not in the production metrics module). Importing the test file
# directly reuses the single source of truth — the same one
# tests/test_walk_metrics_registry.py cross-checks against the
# prometheus_client REGISTRY. Importing the test module by file path
# avoids depending on a public re-export of the tuples from
# src/openstudio_operator/metrics.py (which would couple the production
# module to test-only constants).
_TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_DIR))
_metrics_endpoint = pytest.importorskip(
    "test_metrics_endpoint",
    reason="tests/test_metrics_endpoint.py must be importable for the prose-claim gate",
)
EXPECTED_COUNTER_FAMILIES = _metrics_endpoint.EXPECTED_COUNTER_FAMILIES
EXPECTED_GAUGE_FAMILIES = _metrics_endpoint.EXPECTED_GAUGE_FAMILIES
EXPECTED_HISTOGRAM_FAMILIES = _metrics_endpoint.EXPECTED_HISTOGRAM_FAMILIES

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
AUDIT_DOC = REPO_ROOT / "docs" / "audit-dryrun-idempotency.md"

#: Head-count claim shape: ``N counters + M gauges + K histograms`` (or
#: ``K histograms`` pluralised differently). The regex anchors on the
#: digit boundaries so a passing mention of "16 counters" in prose doesn't
#: accidentally satisfy the rule. Accepts the canonical shape used in the
#: README / audit doc; the optional word boundaries tolerate the dash and
#: parentheses variants found in the README layout comment. The `\s+`
#: between the digit and the noun tolerates Markdown line-wrapping (``**18\n
#: counters + 7 gauges + 3 histograms**`` is one logical claim rendered
#: across two physical lines in the audit doc).
_HEAD_COUNT_RE = re.compile(
    r"(?<![\w])(\d+)\s+counters?\s*\+\s*(\d+)\s+gauges?\s*\+\s*(\d+)\s+histograms?(?![\w])",
    re.IGNORECASE | re.DOTALL,
)


def _parse_head_count(text: str) -> tuple[int, int, int] | None:
    """Return ``(counters, gauges, histograms)`` from the first matching
    ``N counters + M gauges + K histograms`` claim, or ``None`` if absent.

    The regex is anchored on word boundaries so descriptive prose like
    "16 counters and 4 gauges" or "the 16+4+1 surface" does NOT match —
    only the canonical head-count form does. This mirrors the test-count
    guard's strict-shaped-regex approach (``#151`` / ``#220``).
    """
    match = _HEAD_COUNT_RE.search(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _expected_total() -> tuple[int, int, int]:
    """Return the canonical (counters, gauges, histograms) tuple counts.

    The three tuples are the SOLE source of truth — the prose is held to
    them. A failure here means the maintainer bumped a tuple (added a new
    Counter / Gauge / Histogram in ``metrics.py`` + the matching name in
    the ``EXPECTED_*`` tuple) without updating the prose claim in the same
    PR. The compensation direction is a one-line bump in the prose;
    ``tests/test_metrics_endpoint.py`` already proves the tuple ↔
    registry invariant on every CI run.
    """
    return (
        len(EXPECTED_COUNTER_FAMILIES),
        len(EXPECTED_GAUGE_FAMILIES),
        len(EXPECTED_HISTOGRAM_FAMILIES),
    )


def test_readme_head_count_matches_tuple_families():
    """``README.md`` carries the canonical prose claim in the metrics section
    header (line 67) and the repository layout comment (line 205). Both must
    agree with ``len(EXPECTED_*_FAMILIES)``.
    """
    claim = _parse_head_count(README.read_text(encoding="utf-8"))
    assert claim is not None, (
        f"README.md is missing the 'N counters + M gauges + K histograms' "
        f"head-count claim. Add a line like '**Current shape: "
        f"{_expected_total()[0]} counters + {_expected_total()[1]} gauges + "
        f"{_expected_total()[2]} histograms**' to the metrics section."
    )
    expected = _expected_total()
    assert claim == expected, (
        f"README.md head-count claim is stale: prose says {claim}, "
        f"len(EXPECTED_*_FAMILIES) is {expected}. "
        f"Update README.md to '**{expected[0]} counters + {expected[1]} "
        f"gauges + {expected[2]} histograms**'."
    )


def test_audit_doc_head_count_matches_tuple_families():
    """``docs/audit-dryrun-idempotency.md`` ``Appendix D`` carries the
    prose claim in its introduction. Mirrors ``#151`` / ``#220``'s
    per-doc test approach: each prose location gets its own named test
    so the failure message points at the exact file the maintainer needs
    to bump.
    """
    claim = _parse_head_count(AUDIT_DOC.read_text(encoding="utf-8"))
    assert claim is not None, (
        f"docs/audit-dryrun-idempotency.md is missing the 'N counters + "
        f"M gauges + K histograms' head-count claim. Add a line like "
        f"'exhaustive inventory — {_expected_total()[0]} counters + "
        f"{_expected_total()[1]} gauges + {_expected_total()[2]} histograms'"
        f" to the Appendix D introduction."
    )
    expected = _expected_total()
    assert claim == expected, (
        f"docs/audit-dryrun-idempotency.md head-count claim is stale: "
        f"prose says {claim}, len(EXPECTED_*_FAMILIES) is {expected}. "
        f"Update the Appendix D introduction to '{expected[0]} counters "
        f"+ {expected[1]} gauges + {expected[2]} histograms'."
    )


def test_readme_layout_line_uses_same_head_count():
    """The repository layout comment in ``README.md`` (line 205) shows the
    head count in the form ``(N+M+K)`` (e.g. ``(18+7+3)``). This is the
    compact form — it doesn't carry the verbose prose shape — but it
    must still match the tuple counts. We extract the three-digit
    pattern from the layout comment so the compact form is gated too.
    """
    text = README.read_text(encoding="utf-8")
    # Compact form: ``(18+7+3)`` — match any three-digit run joined by ``+``.
    compact_match = re.search(r"\((\d+)\+(\d+)\+(\d+)\)", text)
    assert compact_match is not None, (
        "README.md repository layout comment is missing the compact "
        "(N+M+K) head-count form. Add e.g. '(18+7+3)' to the metrics.py "
        "line in the Repository layout block."
    )
    claim = (
        int(compact_match.group(1)),
        int(compact_match.group(2)),
        int(compact_match.group(3)),
    )
    expected = _expected_total()
    assert claim == expected, (
        f"README.md repository layout compact form is stale: prose says "
        f"{claim}, len(EXPECTED_*_FAMILIES) is {expected}. Update the "
        f"metrics.py line in the layout block to '({expected[0]}+{expected[1]}"
        f"+{expected[2]})'."
    )


@pytest.mark.parametrize(
    "doc_name",
    ["README.md", "docs/audit-dryrun-idempotency.md"],
)
def test_prose_claim_matches_prose_claim(doc_name: str):
    """Both prose locations must carry the SAME head-count claim.

    A regression here means one of the two prose files was bumped
    independently of the other — the kind of half-applied drift fix
    that #387 was designed to catch. The two-locations-same-claim
    invariant is what makes the operator's docs self-consistent.
    """
    paths = {
        "README.md": README,
        "docs/audit-dryrun-idempotency.md": AUDIT_DOC,
    }
    readme_claim = _parse_head_count(README.read_text(encoding="utf-8"))
    audit_claim = _parse_head_count(AUDIT_DOC.read_text(encoding="utf-8"))
    assert readme_claim is not None, "README.md missing the head-count claim"
    assert audit_claim is not None, (
        "docs/audit-dryrun-idempotency.md missing the head-count claim"
    )
    assert readme_claim == audit_claim, (
        f"Head-count claims disagree across prose locations: README.md "
        f"says {readme_claim}, docs/audit-dryrun-idempotency.md says "
        f"{audit_claim}. Both must agree on the same N+M+K — see "
        f"len(EXPECTED_*_FAMILIES) = {_expected_total()}."
    )
    # The doc_name parametrize is decorative here — every pair must
    # agree. Reference the parametrize so the test is invariant when
    # future docs are added.
    assert doc_name in paths
