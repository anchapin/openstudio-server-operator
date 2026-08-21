"""Documentation drift gate (issue #410): the kind-validation runbook must not
embed the legacy pre-#150 ``openstudio`` Redis password in a ``redis://`` URL.

Why this test exists
--------------------
``scripts/check_redis_password_unique.sh`` (issue #150) guards the *manifest*
password positions (``--requirepass``, ``stringData.password``, ``REDIS_URL``
env values) against the publicly-known ``openstudio`` literal — but it parses
YAML value positions only, so prose and fenced code blocks in
``docs/kind-validation.md`` are invisible to it. Four verification snippets in
that runbook still carried ``redis://:openstudio@queue...`` URLs after #150
landed. A reader who copies one into a fresh kind cluster gets an auth error
(the committed placeholder is ``openstudio-rotated``); a reader who copies one
into a pre-#150 cluster silently re-introduces the literal the shell guard
exists to keep out of source.

Historical capture logs are the one legitimate home for the literal: a capture
transcribed before #150 is authentic evidence and may keep it — but only when
the block is explicitly introduced by a leading ``PRE-#150`` marker line so no
reader can mistake it for a current recipe. This test fails the build when a
legacy ``redis://:openstudio@`` URL appears anywhere in
``docs/kind-validation.md`` outside the exemption window of such a marker.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KIND_VALIDATION_DOC = REPO_ROOT / "docs" / "kind-validation.md"

LEGACY_URL_SUBSTRING = "redis://:openstudio@"
HISTORICAL_CAPTURE_MARKER = "PRE-#150"
EXEMPTION_WINDOW_LINES = 8

FENCE = "```"
LEGACY_DBSIZE_LINE = (
    "$ redis-cli -u redis://:openstudio@queue.openstudio-server.svc.cluster.local:6379 DBSIZE"
)
ROTATED_CLIENT_LINE = (
    "c = ReadOnlyRedisClient("
    "'redis://:openstudio-rotated@queue.openstudio-server.svc.cluster.local:6379')"
)


def legacy_password_urls_outside_capture_labels(lines: list[str]) -> list[tuple[int, str]]:
    """Return ``(line_number, line)`` pairs for legacy Redis-password URLs that
    are not covered by a leading historical-capture marker.

    A URL line is exempt when a ``PRE-#150`` marker line appears within
    ``EXEMPTION_WINDOW_LINES`` lines *above* it (the marker is a leading
    comment on the capture block, per issue #410's labeling convention).
    """
    marker_line_numbers = [
        number for number, line in enumerate(lines) if HISTORICAL_CAPTURE_MARKER in line
    ]
    offenders: list[tuple[int, str]] = []
    for number, line in enumerate(lines):
        if LEGACY_URL_SUBSTRING not in line:
            continue
        labeled = any(
            0 <= number - marker <= EXEMPTION_WINDOW_LINES for marker in marker_line_numbers
        )
        if not labeled:
            offenders.append((number + 1, line.rstrip()))
    return offenders


def test_kind_validation_doc_has_no_legacy_redis_password_urls() -> None:
    content = KIND_VALIDATION_DOC.read_text(encoding="utf-8")
    offenders = legacy_password_urls_outside_capture_labels(content.splitlines())
    assert not offenders, (
        "docs/kind-validation.md embeds the legacy pre-#150 Redis password in a "
        "redis:// URL outside a labeled historical-capture section (issue #410). "
        "Scrub the snippet to the `openstudio-rotated` placeholder, or prefix the "
        "capture block with a `PRE-#150 capture log, not a current recipe` marker "
        f"line. Offending lines: {offenders!r}"
    )


def test_unlabeled_legacy_url_is_flagged() -> None:
    lines = [FENCE, LEGACY_DBSIZE_LINE, FENCE]
    offenders = legacy_password_urls_outside_capture_labels(lines)
    assert [number for number, _ in offenders] == [2]
    assert LEGACY_URL_SUBSTRING in offenders[0][1]


def test_legacy_url_is_exempt_only_within_capture_label_window() -> None:
    marker_line = "# PRE-#150 capture log, not a current recipe — capture evidence below."
    legacy_url = "$ redis-cli -u redis://:openstudio@queue.example:6379 DBSIZE"

    within_window = [marker_line] + [""] * (EXEMPTION_WINDOW_LINES - 1) + [legacy_url]
    assert legacy_password_urls_outside_capture_labels(within_window) == []

    beyond_window = [marker_line] + [""] * EXEMPTION_WINDOW_LINES + [legacy_url]
    offenders = legacy_password_urls_outside_capture_labels(beyond_window)
    assert [number for number, _ in offenders] == [len(beyond_window)]


def test_rotated_placeholder_url_is_never_flagged() -> None:
    lines = [ROTATED_CLIENT_LINE]
    assert legacy_password_urls_outside_capture_labels(lines) == []
