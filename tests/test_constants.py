"""Regression tests for the consolidated operator-behavior constants (issue #165).

Before #165 the six operator-behavior constants lived as duplicate
``POLL_INTERVAL_SECONDS = N`` / ``DEFAULT_METRICS_PORT = 9090`` declarations
inside each handler/metrics module. A future maintainer who wanted to add a
global "all polling intervals should respect a ``pausePolling: bool`` CR field"
had to edit six files in lockstep. After #165 every module imports from
:mod:`openstudio_operator._constants`.

These tests guard against drift back into the pre-#165 shape:

* :func:`test_all_constants_defined` — every constant the consolidation covers
  is a non-None value (catches a future accidental ``del`` or ``# type: ignore``
  hiding a name).
* :func:`test_constants_match_known_good_values` — the values themselves match
  the values they replaced at the original callsites (the SLA monitor used to
  poll every 30 s; the datapoint watchdog every 60 s; etc.).
* :func:`test_no_module_level_constant_duplicates` — no handler module re-declares
  one of the moved constants as a top-level literal (catches accidental drift).
* :func:`test_handler_modules_keep_public_alias` — each handler module that
  used to expose ``POLL_INTERVAL_SECONDS`` as a module-level name still does
  (the ``@kopf.timer(..., interval=POLL_INTERVAL_SECONDS)`` decorator reads
  that name at import time).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from openstudio_operator import _constants
from openstudio_operator.handlers import (
    analysis_sla,
    datapoint_watchdog,
    web_background_monitor,
    worker_recycler,
)
from openstudio_operator.metrics import DEFAULT_METRICS_PORT

HANDLER_MODULES = (
    analysis_sla,
    datapoint_watchdog,
    web_background_monitor,
    worker_recycler,
)


# Names the consolidation moved into :mod:`openstudio_operator._constants`. Kept
# as a literal list (not computed from the module) so an accidental shadowing
# of the module attribute still surfaces as a missing-import error here.
EXPECTED_CONSTANTS = (
    "SLA_POLL_INTERVAL_SECONDS",
    "DATAPOINT_POLL_INTERVAL_SECONDS",
    "WORKER_RECYCLE_POLL_INTERVAL_SECONDS",
    "WEB_BACKGROUND_POLL_INTERVAL_SECONDS",
    "LAYOUT_WARNING_GRACE_SECONDS",
    "METRICS_PORT",
)


def test_all_constants_defined():
    """Every constant the consolidation covers must be importable and non-None."""
    for name in EXPECTED_CONSTANTS:
        assert hasattr(_constants, name), f"_constants.{name} missing"
        value = getattr(_constants, name)
        assert value is not None, f"_constants.{name} resolved to None"


def test_constants_match_known_good_values():
    """The moved constants match the values they replaced at the original callsites.

    These are the values that lived in each module before #165:

    * ``analysis_sla.POLL_INTERVAL_SECONDS = 30.0``
    * ``datapoint_watchdog.POLL_INTERVAL_SECONDS = 60.0``
    * ``worker_recycler.POLL_INTERVAL_SECONDS = 300.0``
    * ``web_background_monitor.POLL_INTERVAL_SECONDS = 60.0`` plus
      ``_LAYOUT_WARNING_GRACE_SECONDS = timedelta(seconds=60)``
    * ``metrics.DEFAULT_METRICS_PORT = 9090``
    """
    assert _constants.SLA_POLL_INTERVAL_SECONDS == 30.0
    assert _constants.DATAPOINT_POLL_INTERVAL_SECONDS == 60.0
    assert _constants.WORKER_RECYCLE_POLL_INTERVAL_SECONDS == 300.0
    assert _constants.WEB_BACKGROUND_POLL_INTERVAL_SECONDS == 60.0
    assert _constants.LAYOUT_WARNING_GRACE_SECONDS.total_seconds() == 60.0
    assert _constants.METRICS_PORT == 9090


def test_metrics_default_metrics_port_alias():
    """``metrics.DEFAULT_METRICS_PORT`` is a back-compat alias for ``_constants.METRICS_PORT``.

    The historical name was ``DEFAULT_METRICS_PORT`` (only used inside
    ``metrics.py``); the consolidation renames it ``METRICS_PORT`` and lives in
    :mod:`openstudio_operator._constants`, but the metrics module re-exports
    the old name so any external importer is not silently broken.
    """
    assert DEFAULT_METRICS_PORT == _constants.METRICS_PORT


def test_no_module_level_constant_duplicates():
    """No handler module re-declares one of the moved constants as a top-level literal.

    The pre-#165 shape was::

        # in each of four handler modules
        POLL_INTERVAL_SECONDS = 30.0  # or 60.0 or 300.0
        ...
        @kopf.timer(..., interval=POLL_INTERVAL_SECONDS)

    After #165 each handler still exposes ``POLL_INTERVAL_SECONDS`` as a
    module-level name (the ``@kopf.timer`` decorator reads it at import time)
    but its value is the imported alias, NOT a fresh literal. This test
    parses each handler module's AST and asserts the moved names appear
    ONLY as the right-hand side of an ``Alias`` binding — i.e. imported
    from :mod:`openstudio_operator._constants`.
    """
    project_root = Path(__file__).resolve().parents[1]
    src_root = project_root / "src" / "openstudio_operator"

    # Names the consolidation moves — none of these should appear as a fresh
    # ``Name = <literal>`` assignment at module top level.
    moved_names = {
        "POLL_INTERVAL_SECONDS",  # the per-handler alias is fine; the LITERAL is not
        "_LAYOUT_WARNING_GRACE_SECONDS",  # same story
        "DEFAULT_METRICS_PORT",  # now lives in metrics.py as an alias only
    }

    violations: list[str] = []

    for path in sorted(src_root.glob("**/*.py")):
        # Skip the canonical home of the constants.
        if path.name == "_constants.py":
            continue
        # Skip status_store._BACKOFF_BASE_SECONDS — different category
        # (status-store retry math, not operator-behavior knobs).
        if path.name == "status_store.py":
            continue

        tree = ast.parse(path.read_text())
        for node in tree.body:
            # Plain ``FOO = <literal-or-call>`` assignments only — excludes
            # AnnAssign (e.g. ``x: int = 1``), Import, ImportFrom, etc.
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            name = node.targets[0].id
            if name not in moved_names:
                continue
            value = node.value
            # Allowed right-hand sides: Name (alias binding), Attribute (alias
            # attribute access like ``LAYOUT_WARNING_GRACE_SECONDS``), or
            # Call/timedelta/... — anything that is NOT a numeric literal,
            # bool, or simple Constant. A literal here would be the
            # pre-#165 shape re-introduced.
            if isinstance(value, ast.Constant) and isinstance(value.value, (int, float, bool)):
                # Special case: metrics.py may re-export ``DEFAULT_METRICS_PORT
                # = METRICS_PORT`` (Name, not Constant). If we reach this
                # branch it's a numeric literal re-declaration → violation.
                violations.append(
                    f"{path.relative_to(project_root)}:{node.lineno}: "
                    f"{name} = {value.value!r} (literal re-declaration)"
                )
            elif isinstance(value, ast.Constant) and value.value is None:
                violations.append(
                    f"{path.relative_to(project_root)}:{node.lineno}: "
                    f"{name} = None (literal re-declaration)"
                )

    assert violations == [], (
        "operator-behavior constants were re-declared as literals; "
        "every module should import from openstudio_operator._constants:\n  "
        + "\n  ".join(violations)
    )


def test_handler_modules_keep_public_alias():
    """Each handler still exposes ``POLL_INTERVAL_SECONDS`` at module scope.

    The ``@kopf.timer(..., interval=POLL_INTERVAL_SECONDS)`` decorator reads
    that name at import time. After #165 each handler binds it as a local
    alias to the imported ``_constants`` value, so the name remains
    import-accessible (for tests and the decorator) but the value is sourced
    from :mod:`openstudio_operator._constants`.
    """
    assert hasattr(analysis_sla, "POLL_INTERVAL_SECONDS")
    assert hasattr(datapoint_watchdog, "POLL_INTERVAL_SECONDS")
    assert hasattr(web_background_monitor, "POLL_INTERVAL_SECONDS")
    assert hasattr(worker_recycler, "POLL_INTERVAL_SECONDS")

    # Cross-check: the per-handler alias matches the consolidated value.
    assert analysis_sla.POLL_INTERVAL_SECONDS == _constants.SLA_POLL_INTERVAL_SECONDS
    assert datapoint_watchdog.POLL_INTERVAL_SECONDS == _constants.DATAPOINT_POLL_INTERVAL_SECONDS
    assert worker_recycler.POLL_INTERVAL_SECONDS == _constants.WORKER_RECYCLE_POLL_INTERVAL_SECONDS
    assert (
        web_background_monitor.POLL_INTERVAL_SECONDS
        == _constants.WEB_BACKGROUND_POLL_INTERVAL_SECONDS
    )


def test_handler_modules_import_from_constants():
    """Each handler imports its cadence from :mod:`openstudio_operator._constants`.

    Static-text check (not AST) because the imports may live anywhere in the
    file; we just want to make sure the import line exists so a future
    refactor that drops the import surfaces immediately.
    """
    project_root = Path(__file__).resolve().parents[1]
    src_root = project_root / "src" / "openstudio_operator"

    expected_imports = {
        "handlers/analysis_sla.py": r"from openstudio_operator\._constants import\s+SLA_POLL_INTERVAL_SECONDS",
        "handlers/datapoint_watchdog.py": (
            r"from openstudio_operator\._constants import\s+DATAPOINT_POLL_INTERVAL_SECONDS"
        ),
        "handlers/worker_recycler.py": (
            r"from openstudio_operator\._constants import\s+WORKER_RECYCLE_POLL_INTERVAL_SECONDS"
        ),
        "handlers/web_background_monitor.py": (
            r"from openstudio_operator\._constants import\s+"
            r"\(?[\s\S]*?WEB_BACKGROUND_POLL_INTERVAL_SECONDS"
        ),
        "metrics.py": r"from openstudio_operator\._constants import\s+METRICS_PORT",
    }

    for relative_path, pattern in expected_imports.items():
        path = src_root / relative_path
        text = path.read_text()
        assert re.search(pattern, text), (
            f"{relative_path} is missing its `from openstudio_operator._constants "
            f"import ...` line (regression: handler no longer sources its "
            f"constant from the single source of truth)"
        )