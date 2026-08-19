"""Unit tests for :mod:`openstudio_operator._time` — the single tz-aware UTC
parser for the operator (issue #174, D12).

These cases are the explicit acceptance criteria from the issue: naive ISO,
``Z`` suffix, ``+00:00`` suffix, explicit offset, fractional seconds, and
malformed input. They also cover the ``None``-passthrough and the
non-string-non-None rejection that the helper introduced on top of the
three legacy copies (which all blew up on ``None`` and silently mis-parsed
non-string input).

The hypothesis-driven cases at the bottom (issue #246) cover the long
tail: any naive ISO string is treated as UTC, any offset string round-trips
through UTC, malformed-but-fromisoformat-friendly strings raise
:class:`ValueError`, and ``parse(parse(x)) == parse(x)`` (idempotency).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from openstudio_operator._time import parse_iso_utc

# --- None passthrough -----------------------------------------------------


def test_none_returns_none():
    assert parse_iso_utc(None) is None


# --- Naive ISO string (D12: assumed UTC) ---------------------------------


def test_naive_iso_string_assumed_utc():
    parsed = parse_iso_utc("2024-01-02T03:04:05")
    assert parsed == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


# --- Z-suffix ------------------------------------------------------------


def test_z_suffix_returns_utc():
    parsed = parse_iso_utc("2024-01-02T03:04:05Z")
    assert parsed == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)


# --- +00:00 explicit offset ----------------------------------------------


def test_plus_zero_zero_offset_returns_utc():
    parsed = parse_iso_utc("2024-01-02T03:04:05+00:00")
    assert parsed == datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)


# --- Explicit non-UTC offset ---------------------------------------------


def test_explicit_offset_is_converted_to_utc():
    # 08:00 at +05:00 is 03:00 UTC.
    parsed = parse_iso_utc("2024-01-02T08:00:00+05:00")
    assert parsed == datetime(2024, 1, 2, 3, 0, 0, tzinfo=UTC)


def test_negative_offset_is_converted_to_utc():
    # 08:00 at -06:00 is 14:00 UTC.
    parsed = parse_iso_utc("2024-01-02T08:00:00-06:00")
    assert parsed == datetime(2024, 1, 2, 14, 0, 0, tzinfo=UTC)


# --- Fractional seconds --------------------------------------------------


def test_fractional_seconds_preserved_at_microsecond_resolution():
    parsed = parse_iso_utc("2024-01-02T03:04:05.123456+00:00")
    assert parsed == datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)


def test_fractional_seconds_with_z_suffix():
    parsed = parse_iso_utc("2024-01-02T03:04:05.789Z")
    assert parsed == datetime(2024, 1, 2, 3, 4, 5, 789000, tzinfo=UTC)


# --- Malformed input -----------------------------------------------------


def test_unparseable_string_raises_valueerror():
    with pytest.raises(ValueError, match="unparseable timestamp"):
        parse_iso_utc("not-a-timestamp")


def test_empty_string_raises_valueerror():
    with pytest.raises(ValueError, match="unparseable timestamp"):
        parse_iso_utc("")


# --- Non-string non-None rejection (TypeError per TRY004) ----------------


def test_non_string_input_raises_typeerror():
    with pytest.raises(TypeError, match="expected ISO-8601 string"):
        parse_iso_utc(123)  # type: ignore[arg-type]


def test_list_input_raises_typeerror():
    with pytest.raises(TypeError, match="expected ISO-8601 string"):
        parse_iso_utc([2024, 1, 2])  # type: ignore[arg-type]


# --- Hypothesis-driven property tests (issue #246) --------------------------
#
# The D12 timestamp boundary is the operator's hottest parse path — every CR
# status patch, every API timestamp the openstudio_client reads, every
# singleton guard sort goes through parse_iso_utc. The hand-written cases
# above cover the explicit acceptance criteria from #174 but miss the long
# tail (timezone offsets near the DST boundary, leap seconds in naive
# strings, mixed Z and +00:00 separators, microsecond rounding, year-0001 /
# year-9999 boundaries, and any string that satisfies
# ``datetime.fromisoformat`` but yields a tzinfo the caller did not expect).
#
# The four properties are the acceptance criteria from #246:
#
#   (a) arbitrary naive ISO strings assume UTC;
#   (b) arbitrary offset strings convert to UTC and tzinfo stays set;
#   (c) malformed-but-fromisoformat-friendly strings raise ValueError;
#   (d) idempotency: parse(parse(x)) == parse(x).
#
# Hypothesis's ``.hypothesis/`` artifact directory stays locally cached and
# gitignored (see repo .gitignore — explicit ``.hypothesis/`` entry was added
# in this same change). ``deadline=None`` so a slow CI runner does not trip
# the default 200ms deadline on the deeper years.


# ``datetime`` fields hypothesis can plausibly generate; bounded to keep the
# search space finite (year 1..9999 mirrors the stdlib ``datetime`` range).
_naive_datetime = st.datetimes(
    min_value=datetime(1, 1, 1, 0, 0, 0),  # noqa: DTZ001 — intentional naive bound
    max_value=datetime(9999, 12, 31, 23, 59, 59, 999999),  # noqa: DTZ001 — intentional naive bound
    timezones=st.none(),
)


class _FixedOffset(tzinfo):
    """Minimal ``tzinfo`` carrying a single fixed UTC offset, no DST.

    Hypothesis's ``datetimes(timezones=...)`` expects ``tzinfo``
    instances, not raw ``timedelta`` values. We wrap a ``timedelta`` in
    this thin subclass so the strategy can return offsets that mirror
    real-world zones (whole-hour, half-hour, 45-minute zones) without
    dragging in pytz / dateutil — neither is in the operator's
    production dependency footprint and AGENTS.md forbids adding one
    for property-based tests.
    """

    def __init__(self, offset: timedelta) -> None:
        self._offset = offset

    def utcoffset(self, _dt: datetime | None) -> timedelta:
        return self._offset

    def dst(self, _dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, _dt: datetime | None) -> str:
        total = int(self._offset.total_seconds())
        sign = "+" if total >= 0 else "-"
        abs_total = abs(total)
        hours, rem = divmod(abs_total, 3600)
        minutes = rem // 60
        return f"{sign}{hours:02d}:{minutes:02d}"


# Any UTC offset, including half-hour and 45-minute zones (Asia/Kathmandu
# is +05:45, Asia/Kolkata is +05:30, etc.) — the legacy ``_parse`` copies
# only handled whole-hour offsets, which broke on live capture. The
# ``+00:00`` case is a degenerate member; it stays in the strategy because
# the wall-clock-shift assertion below naturally short-circuits when the
# offset is zero.
_offsets = [
    _FixedOffset(timedelta(hours=h))
    for h in range(-23, 24)
] + [
    _FixedOffset(timedelta(hours=5, minutes=30)),  # Asia/Kolkata, Asia/Colombo
    _FixedOffset(timedelta(hours=5, minutes=45)),  # Asia/Kathmandu
    _FixedOffset(timedelta(hours=12, minutes=45)),  # Pacific/Chatham
    _FixedOffset(timedelta(hours=-3, minutes=30)),  # Canada/Newfoundland
    _FixedOffset(timedelta(hours=-9, minutes=30)),  # Pacific/Marquesas
]
_offset = st.sampled_from(_offsets)


@given(value=_naive_datetime)
def test_hypothesis_naive_iso_string_assumed_utc(value: datetime) -> None:
    """(a) Any naive ISO string round-trips to a tz-aware UTC ``datetime``.

    The datetime the strategy generated carries no tzinfo; the parser must
    attach UTC (D12) and preserve the wall-clock components exactly.
    """
    raw = value.isoformat()
    parsed = parse_iso_utc(raw)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert parsed.year == value.year
    assert parsed.month == value.month
    assert parsed.day == value.day
    assert parsed.hour == value.hour
    assert parsed.minute == value.minute
    assert parsed.second == value.second
    assert parsed.microsecond == value.microsecond


@given(value=st.datetimes(
    min_value=datetime(1, 1, 2),  # noqa: DTZ001 — intentional naive bound
    max_value=datetime(9999, 12, 30),  # noqa: DTZ001 — intentional naive bound
    timezones=_offset,
))
def test_hypothesis_offset_string_converts_to_utc(value: datetime) -> None:
    """(b) Any string with an explicit offset normalizes to UTC.

    The parser preserves the absolute instant, only the wall-clock shifts.
    The resulting tzinfo must be :data:`datetime.UTC` (not the original
    offset) — callers downstream anchor on ``utcoffset() == timedelta(0)``.
    """
    offset = value.utcoffset()
    assert offset is not None
    raw = value.isoformat()
    parsed = parse_iso_utc(raw)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    # Round-trip the absolute instant — the wall-clock may have shifted
    # across a day boundary in either direction.
    expected_utc = value.astimezone(UTC)
    assert parsed == expected_utc


@given(value=_naive_datetime)
def test_hypothesis_plus_zero_zero_offset_returns_utc(value: datetime) -> None:
    """Auxiliary: ``+00:00`` offset strings are treated identically to naive UTC strings.

    A naive datetime appended with ``+00:00`` must parse to the same
    instant as the bare naive form (D12: any UTC offset normalises to
    UTC). The property runs the same strategy as the (a) test but
    appends an explicit ``+00:00`` offset string and asserts the parsed
    wall-clock equals the input.
    """
    raw = value.isoformat() + "+00:00"
    parsed = parse_iso_utc(raw)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == value.replace(tzinfo=UTC)


@given(value=_naive_datetime)
def test_hypothesis_z_suffix_round_trips_to_same_instant(value: datetime) -> None:
    """``Z`` suffix is a UTC offset by definition; the parsed instant matches the input.

    A naive datetime interpreted as UTC, then re-emitted with a ``Z``
    suffix, must parse back to the same wall-clock — the kind of cross-
    boundary sanity check the legacy status_store / singleton copies
    would silently miss (issue #174 found one such bug live).
    """
    naive_iso = value.isoformat() + "Z"
    parsed = parse_iso_utc(naive_iso)
    assert parsed is not None
    assert parsed == value.replace(tzinfo=UTC)


# Malformed strings that ``datetime.fromisoformat`` (3.11+) refuses to parse.
# The strategy enumerates the common foot-gun shapes that the operator has
# to reject loudly:
#   * empty string,
#   * non-ISO ASCII (ctime, slashes, two-digit years),
#   * ISO-like strings with values out of the calendar range
#     (month 13, day 32, hour 25, minute 60, second 60).
# The parser must reject every one of them with ``ValueError`` — a
# successful parse on any of these inputs is the regression we're
# guarding against (the original singleton._parse_utc raised a less
# helpful ``ValueError`` with no message for these cases).
_garbage_strings = st.sampled_from(
    [
        "",  # empty: fromisoformat raises
        "not-a-timestamp",
        "2024",  # year-only: fromisoformat rejects in 3.11+
        "2024-01",  # year-month only: fromisoformat rejects in 3.11+
        "24-01-02",  # two-digit year — fromisoformat rejects
        "2024/01/02 03:04:05",  # slash separators, fromisoformat rejects
        "Mon Jan 02 03:04:05 2024",  # ctime shape
        "2024-13-01T00:00:00",  # month 13
        "2024-01-32T00:00:00",  # day 32
        "2024-01-02T25:00:00",  # hour 25
        "2024-01-02T00:60:00",  # minute 60
        "2024-01-02T00:00:60",  # second 60
        "x" * 64,  # junk that fromisoformat rejects
    ]
)


@given(raw=_garbage_strings)
def test_hypothesis_garbage_string_raises_valueerror(raw: str) -> None:
    """(c) Malformed strings raise :class:`ValueError`.

    The parser's contract is "any string ``datetime.fromisoformat``
    cannot parse raises ValueError"; the strategy enumerates the common
    foot-gun shapes (empty, ASCII garbage, ISO-like with calendar or
    time overflow).
    """
    with pytest.raises(ValueError, match="unparseable timestamp"):
        parse_iso_utc(raw)


@given(value=_naive_datetime)
def test_hypothesis_idempotency_naive(value: datetime) -> None:
    """(d) ``parse(parse(x)) == parse(x)`` for naive ISO input.

    The operator parses, formats back into ISO, and re-parses across
    many code paths (e.g. status_store RMW persists a normalised ISO
    string; the next tick re-reads it). The fixed point must be stable
    in one round trip.
    """
    raw = value.isoformat()
    once = parse_iso_utc(raw)
    assert once is not None
    twice = parse_iso_utc(once.isoformat())
    assert twice == once
    assert twice is not None


@given(value=st.datetimes(
    min_value=datetime(1, 1, 2),  # noqa: DTZ001 — intentional naive bound
    max_value=datetime(9999, 12, 30),  # noqa: DTZ001 — intentional naive bound
    timezones=_offset,
))
def test_hypothesis_idempotency_with_offset(value: datetime) -> None:
    """(d) ``parse(parse(x)) == parse(x)`` for offset-tagged ISO input.

    Round-trip the offset-bearing form: parse, re-emit (now in UTC because
    the parser normalised), re-parse. Must be stable in one extra hop —
    this is the property the legacy singleton._parse_utc silently violated
    on ``+05:30`` input (Kolkata), which is why we fuzz with non-whole-hour
    offsets.
    """
    raw = value.isoformat()
    once = parse_iso_utc(raw)
    assert once is not None
    assert once.tzinfo is not None
    assert once.utcoffset() == timedelta(0)
    twice = parse_iso_utc(once.isoformat())
    assert twice == once
