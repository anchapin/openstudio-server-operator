"""Tests for the JSON log formatter + installer wired in by issue #256.

The operator's logs are emitted as JSON lines (one object per record) so
SREs can query Loki / CloudWatch Logs / Datadog on canonical fields
instead of regexing unstructured text. The formatter is a stdlib
``logging.Formatter`` subclass; this file asserts:

* a sample record formats as a single line of valid JSON,
* the canonical seven fields are present on every line,
* kopf-injected ``k8s_ref`` is flattened to top-level ``namespace`` and
  ``name`` keys (the SRE query surface),
* unknown ``extra=`` fields survive as top-level keys (forward-compat),
* :func:`install_json_logging` is idempotent — the operator's
  ``handlers/__init__.py`` calls it at module import; repeated imports
  must not stack handlers.

The handlers package is imported in :func:`test_handlers_init_installs_json`
because the operator process wires the formatter there at module load
time (issue #256 scope: operator process + prune CronJob).
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from openstudio_operator.logging_setup import (
    JsonLogFormatter,
    install_json_logging,
)


def _capture_formatter_log(
    formatter: JsonLogFormatter,
    record: logging.LogRecord,
) -> str:
    """Format ``record`` and assert the formatter produced exactly one line."""
    line = formatter.format(record)
    assert "\n" not in line, f"JSON log line must be single-line, got: {line!r}"
    return line


def _make_record(
    *,
    name: str = "openstudio_operator.test",
    level: int = logging.INFO,
    msg: str = "redis_key_layout=ok namespace=test-ns name=test-osc",
    args: tuple[object, ...] = (),
    extra: dict[str, object] | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=42,
        msg=msg,
        args=args,
        exc_info=None,
    )
    if extra:
        for key, value in extra.items():
            setattr(record, key, value)
    return record


def test_sample_log_line_is_valid_json() -> None:
    """A single emitted record must be parseable as one JSON object.

    The acceptance criterion for #256 — an SRE alerting on the log
    stream must be able to ``json.loads(line)`` without a try/except.
    """
    line = _capture_formatter_log(JsonLogFormatter(), _make_record())
    parsed = json.loads(line)
    assert isinstance(parsed, dict), (
        f"Expected JSON object, got {type(parsed).__name__}: {line!r}"
    )


def test_canonical_fields_are_present_on_every_record() -> None:
    """All seven canonical fields appear on every emitted JSON line.

    The README "Logs" section lists these as the SRE query surface; a
    regression that drops one (e.g. a future ``del module`` refactor)
    breaks every dashboard built on the field.
    """
    line = _capture_formatter_log(JsonLogFormatter(), _make_record())
    parsed = json.loads(line)
    for field in (
        "timestamp",
        "level",
        "logger",
        "message",
        "module",
        "funcName",
        "lineno",
    ):
        assert field in parsed, (
            f"Missing canonical field {field!r} in emitted line: {line!r}"
        )
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "openstudio_operator.test"
    assert parsed["message"] == "redis_key_layout=ok namespace=test-ns name=test-osc"
    assert parsed["module"].endswith("test_logging_setup")
    assert parsed["lineno"] == 42


def test_kopf_injected_k8s_ref_flattens_to_namespace_and_name() -> None:
    """kopf's ObjectLogger adapter injects ``extra={'k8s_ref': {...}}``;
    the formatter must surface ``namespace`` and ``name`` as flat top-level
    keys so Loki queries can filter on them directly.

    The pre-#256 fallback required a regex on the message string; that
    path broke every time the message format drifted (issue #163's
    ``redis_key_layout=degraded namespace=... name=...`` line, for
    example). The flattened keys are the contract.
    """
    record = _make_record(
        extra={"k8s_ref": {"namespace": "openstudio-server", "name": "osc-prod"}},
    )
    parsed = json.loads(_capture_formatter_log(JsonLogFormatter(), record))
    assert parsed["namespace"] == "openstudio-server", (
        f"Expected flattened k8s_ref.namespace, got: {parsed!r}"
    )
    assert parsed["name"] == "osc-prod", (
        f"Expected flattened k8s_ref.name, got: {parsed!r}"
    )


def test_kopf_k8s_ref_with_only_namespace_flattens_name_not_emitted() -> None:
    """A ``k8s_ref`` with only ``namespace`` populates only that key.

    Cluster-scoped objects have no ``name``; emitting ``name=""`` would
    break ``{name="..."}` Loki filter queries (empty-string match noise).
    """
    record = _make_record(extra={"k8s_ref": {"namespace": "kube-system"}})
    parsed = json.loads(_capture_formatter_log(JsonLogFormatter(), record))
    assert parsed["namespace"] == "kube-system"
    assert "name" not in parsed


def test_unknown_extra_fields_survive_as_top_level_keys() -> None:
    """A future handler that adds ``extra={'analysis_id': '...'}`` gets a
    free ``"analysis_id": "..."`` field in the JSON line — forward-compat
    seam, not a per-call formatter change.
    """
    record = _make_record(
        extra={"analysis_id": "abc-123", "tick": "sla_v2"},
    )
    parsed = json.loads(_capture_formatter_log(JsonLogFormatter(), record))
    assert parsed["analysis_id"] == "abc-123"
    assert parsed["tick"] == "sla_v2"


def test_install_json_logging_is_idempotent() -> None:
    """Repeated ``install_json_logging()`` calls do NOT stack handlers.

    The handlers package is imported multiple times in a pytest session
    (collection + execution + kopf's own internal re-imports). Without
    the sentinel guard every repeat would double the log volume on
    stderr and break Loki's parse-by-line invariant.
    """
    test_logger_name = f"openstudio_operator.test.idempotent.{id(object())}"
    target = logging.getLogger(test_logger_name)
    target.handlers.clear()

    first = install_json_logging(logger=target)
    second = install_json_logging(logger=target)
    third = install_json_logging(logger=target)

    assert first is second is third, (
        "install_json_logging must return the same handler on repeated calls "
        "(the sentinel attribute is the guard)"
    )
    assert target.handlers.count(first) == 1, (
        "Repeated installs must not stack the handler on the logger"
    )

    target.handlers.remove(first)
    target.setLevel(logging.NOTSET)


def test_install_json_logging_emits_valid_json_to_stream(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A roundtrip through :func:`install_json_logging` yields parseable JSON.

    End-to-end check: install the formatter on a fresh logger, log a
    record, capture stderr, ``json.loads`` the line. This is what the
    operator does in production at every handler tick; a regression in
    the formatter or the installer shows up here as a JSONDecodeError.
    """
    test_logger_name = f"openstudio_operator.test.stream.{id(object())}"
    target = logging.getLogger(test_logger_name)
    target.handlers.clear()
    target.propagate = False

    handler = install_json_logging(logger=target)
    try:
        target.info("hello %s", "world")
    finally:
        target.handlers.remove(handler)
        target.setLevel(logging.NOTSET)
        target.propagate = True

    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert lines, f"Expected at least one captured stderr line, got: {captured.err!r}"
    last_line = lines[-1]
    parsed = json.loads(last_line)
    assert parsed["message"] == "hello world"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == test_logger_name


def test_json_formatter_writes_to_custom_stream() -> None:
    """The handler can be wired to any stream (e.g. an in-memory buffer)
    for unit testing the operator's tick log output without stderr noise.

    This shape is what the operator tests would use if they wanted to
    assert a specific log record's JSON shape on the operator's own
    logger — the formatter is plain stdlib, no special hook required.
    """
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger(f"openstudio_operator.test.buffer.{id(object())}")
    logger.handlers.clear()
    logger.propagate = False
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("tick ok")
    finally:
        logger.handlers.remove(handler)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True

    line = buffer.getvalue().strip()
    parsed = json.loads(line)
    assert parsed["message"] == "tick ok"
    assert parsed["level"] == "INFO"


def test_handlers_init_installs_json() -> None:
    """Importing :mod:`openstudio_operator.handlers` installs the JSON
    formatter at module load time — the operator process wire (#256 scope).

    The sentinel attribute lives on the root logger; the handlers
    package's top-level call to :func:`install_json_logging` is the
    production wiring, and the prune entrypoint's :func:`main` does the
    same for the CronJob. Without this import-side effect the operator
    would emit plaintext from the kopf loggers until the first explicit
    formatter call (and there is no such call in the per-tick code).
    """
    import openstudio_operator.handlers  # noqa: F401 — side effect: install_json_logging()

    sentinel = getattr(logging.getLogger(), "_openstudio_operator_json_handler", None)
    assert isinstance(sentinel, logging.Handler), (
        "Importing openstudio_operator.handlers must install the JSON "
        "log formatter on the root logger (issue #256)."
    )
    formatter = sentinel.formatter
    assert isinstance(formatter, JsonLogFormatter), (
        f"Expected JsonLogFormatter on the installed handler, got {type(formatter).__name__}"
    )
