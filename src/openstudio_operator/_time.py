"""Single tz-aware UTC timestamp parser for the operator (issue #174, D12).

D12 says every API timestamp is normalized to tz-aware UTC at the boundary,
*before* it reaches business logic. That boundary is duplicated across three
modules today (``openstudio_client._parse_timestamp``, ``status_store._parse_utc``,
``singleton._parse_utc``) with byte-equivalent bodies — a fix to one is silently
missed in the others. This module is the single source of truth; the three
callers each keep a one-line alias that delegates here (status_store and
singleton bind theirs from :func:`utc_parser` — issue #506 — so the
None-rejection / re-raise wrapper body also exists exactly once, while each
module keeps its own exception type in its public contract).

Accepted inputs:

* ``None`` — returns ``None`` (callers no longer have to guard for missing
  fields themselves; ``status_store``'s ``None if raw is None else _parse_utc(...)``
  pattern collapses to a single ``_parse_utc(raw, ctx)`` call).
* naive ISO-8601 strings (``2024-01-01T00:00:00``) — assumed UTC, per D12 —
  so a degraded server response still yields an anchorable datetime.
* ISO-8601 with a ``Z`` suffix (``...T00:00:00Z``) — normalized to ``+00:00``
  defensively (harmless under Python 3.11+, where ``datetime.fromisoformat``
  accepts ``Z`` natively; preserves the historical behavior of the two
  copies that did the substitution explicitly).
* ISO-8601 with an explicit zone offset (``...T00:00:00+05:00``) — converted
  to UTC via ``astimezone(UTC)``.
* Fractional seconds (``...T00:00:00.123456+00:00``) — preserved at microsecond
  resolution.

Raises :class:`ValueError` on non-string non-None input or on a string that
``datetime.fromisoformat`` cannot parse. Callers that need a domain-specific
exception (``StatusStoreError``, ``SingletonGuardError``) wrap the call and
re-raise with their own context.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


def parse_iso_utc(value: str | None) -> datetime | None:
    """Normalize an ISO-8601 timestamp to a timezone-aware UTC ``datetime``.

    Returns ``None`` for ``None`` input. Otherwise returns a ``datetime`` whose
    ``tzinfo`` is :data:`datetime.UTC` — naive input is treated as UTC per D12.

    Raises :class:`ValueError` if ``value`` is not a ``str`` (and not ``None``)
    or cannot be parsed as ISO-8601.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"expected ISO-8601 string, got {type(value).__name__}")
    # Defensive Z-normalization: harmless under Python 3.11+ (fromisoformat
    # accepts Z natively) and identical to the historical behavior of the
    # status_store / singleton copies.
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"unparseable timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def utc_parser(error_cls: type[Exception]) -> Callable[[Any, str], datetime]:
    """Build a domain-flavored ``_parse_utc(value, context)`` closure (issue #506).

    ``singleton`` and ``status_store`` used to carry byte-identical wrappers
    over :func:`parse_iso_utc` — ``None`` rejection plus a ``ValueError``
    re-raise — differing only in the exception class they raised. This factory
    declares that seam once: the returned closure rejects ``None`` with the
    legacy ``expected ISO-8601 string`` shape (call sites guard for it, so it
    never arrives as a valid input) and re-raises parse failures as
    ``error_cls`` prefixed with the caller's ``context``, preserving each
    module's public exception contract. Per D12 the underlying parse still
    normalizes to tz-aware UTC at the API boundary.
    """
    def _parse_utc(value: Any, context: str) -> datetime:
        if value is None:
            raise error_cls(f"{context}: expected ISO-8601 string, got NoneType")
        try:
            return parse_iso_utc(value)
        except ValueError as exc:
            raise error_cls(f"{context}: {exc}") from exc

    return _parse_utc
