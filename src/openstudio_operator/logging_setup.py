"""Structured-JSON log formatter + installer for the operator processes (issue #256).

The operator historically emitted plain-text log lines via stdlib
``logging`` (``logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s")``)
and let kopf's own :class:`kopf._core.actions.loggers.ObjectLogger` adapter
log per-CR/per-handler lines into the ``kopf.objects`` logger. Both
streams were unstructured text — for an SRE alerted on Loki / CloudWatch
Logs / Datadog, extracting ``CR name``, ``namespace``, and tick outcomes
required a regex and broke every time the message format drifted.

This module installs a :class:`logging.Formatter` subclass that emits a
single-line JSON object per record, on top of (NOT replacing) the existing
``logging`` configuration. kopf's own loggers (``kopf``, ``kopf.objects``)
continue to use their adapters — their records are now formatted as JSON
too, so the operator's log stream is uniformly machine-parseable.

Canonical fields (top-level keys in every record):

* ``timestamp`` — tz-aware ISO-8601 UTC at the moment the record was emitted
  (e.g. ``2026-08-19T14:23:45.123456+00:00``).
* ``level`` — ``logging`` level name (``INFO``, ``WARNING``, ``ERROR`` …).
* ``logger`` — the dotted logger name (``openstudio_operator.handlers``,
  ``kopf.objects``, …).
* ``message`` — the formatted message (``record.getMessage()``).
* ``module`` / ``funcName`` / ``lineno`` — source-location fields, useful
  for tracing a line back to a handler without grepping the codebase.

kopf-injected fields (present iff kopf's :class:`ObjectLogger` adapter
set ``extra={'k8s_ref': {...}}`` on the underlying call — true for every
CR-scoped log emitted from inside a kopf handler):

* ``namespace`` — ``k8s_ref['namespace']`` flattened for Loki/CloudWatch
  queryability (``{namespace="openstudio-server"}``).
* ``name`` — ``k8s_ref['name']`` flattened the same way.

Any other non-reserved :pyattr:`logging.LogRecord` attribute is serialized
as a top-level key, so a future maintainer who adds ``extra={'foo': ...}``
to a logger call gets a free ``"foo": ...`` field in the JSON line —
forward-compat for handler-injected structured data.

:func:`install_json_logging` is idempotent (a sentinel attribute on the
root logger guards against double-install from repeated imports during
test collection), so it is safe to call from both the operator process
(:mod:`openstudio_operator.handlers`) and the prune CronJob
(:mod:`openstudio_operator.prune_entrypoint`) without coordination.

Scope guard (issue #256): does NOT replace kopf's own logger or
formatter — it installs an additional handler on the root logger that
formats every record going through the root logger chain. kopf's
CLI ``--log-format=json`` flag still works independently and produces
its own JSON stream; in production the operator runs without that flag
and this module is the JSON layer.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

#: Stdlib reserved LogRecord attribute names — anything in this set is a
#: logging-internal field and MUST NOT be re-emitted as a top-level JSON
#: key (already represented by ``timestamp``/``level``/``logger``/``module``
#: etc., or is a stdlib machinery detail like ``args``/``msg``).
_RESERVED_LOGRECORD_ATTRS: frozenset[str] = frozenset({
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
})

#: Sentinel used by :func:`install_json_logging` to short-circuit repeat
#: installs from test collection re-imports / multi-process entrypoints.
_INSTALL_SENTINEL_ATTR = "_openstudio_operator_json_handler"


class JsonLogFormatter(logging.Formatter):
    """Format a :class:`logging.LogRecord` as a single-line JSON object.

    The canonical seven fields (:data:`!_CANONICAL_FIELDS`) are always
    present. kopf's :class:`kopf._core.actions.loggers.ObjectLogger`
    adapter injects ``extra={'k8s_ref': {...}}`` on per-CR logs; that
    dict is unpacked into top-level ``namespace`` and ``name`` keys so
    downstream log queries can filter on those fields directly. Any other
    non-reserved record attribute is emitted as a top-level key — that
    path is the forward-compat seam for future handler-injected
    structured data (``extra={'analysis_id': '...'}`` etc.).
    """

    #: Top-level keys the formatter always emits. Documented here AND in
    #: :data:`openstudio_operator.logging_setup` module docstring so the
    #: README can quote a single source.
    _CANONICAL_FIELDS: tuple[str, ...] = (
        "timestamp",
        "level",
        "logger",
        "message",
        "module",
        "funcName",
        "lineno",
    )

    def format(self, record: logging.LogRecord) -> str:
        timestamp = (
            datetime.fromtimestamp(record.created, tz=UTC).isoformat()
        )
        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "lineno": record.lineno,
        }

        k8s_ref = getattr(record, "k8s_ref", None)
        if isinstance(k8s_ref, dict):
            namespace = k8s_ref.get("namespace")
            if isinstance(namespace, str) and namespace:
                payload["namespace"] = namespace
            name = k8s_ref.get("name")
            if isinstance(name, str) and name:
                payload["name"] = name

        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_ATTRS:
                continue
            if key in payload:
                continue
            if key.startswith("_"):
                continue
            if key == "k8s_ref":
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except TypeError:
                payload[key] = repr(value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def install_json_logging(
    *,
    level: int = logging.INFO,
    logger: logging.Logger | None = None,
) -> logging.Handler:
    """Install :class:`JsonLogFormatter` on ``logger`` (default: root).

    Adds a single :class:`logging.StreamHandler` emitting JSON to
    ``stderr`` at ``level`` (default ``INFO``), idempotently guarded by
    the :data:`_INSTALL_SENTINEL_ATTR` attribute on the chosen logger.
    Repeated calls return the existing handler — no double-output.

    Idempotency is essential because the operator's
    :mod:`openstudio_operator.handlers` package may be imported more
    than once in a test session (collection + execution phases both run
    ``import openstudio_operator.handlers``), and the prune CronJob
    entrypoint is the only process to call this from outside the kopf
    import path.

    Returns the installed (or pre-existing) handler so tests can
    introspect / remove it.
    """
    target = logger if logger is not None else logging.getLogger()
    existing = getattr(target, _INSTALL_SENTINEL_ATTR, None)
    if isinstance(existing, logging.Handler):
        return existing

    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(JsonLogFormatter())
    target.addHandler(handler)
    target.setLevel(level)
    setattr(target, _INSTALL_SENTINEL_ATTR, handler)
    return handler
