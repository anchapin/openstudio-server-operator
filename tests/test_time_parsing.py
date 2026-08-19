"""Unit tests for :mod:`openstudio_operator._time` — the single tz-aware UTC
parser for the operator (issue #174, D12).

These cases are the explicit acceptance criteria from the issue: naive ISO,
``Z`` suffix, ``+00:00`` suffix, explicit offset, fractional seconds, and
malformed input. They also cover the ``None``-passthrough and the
non-string-non-None rejection that the helper introduced on top of the
three legacy copies (which all blew up on ``None`` and silently mis-parsed
non-string input).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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
