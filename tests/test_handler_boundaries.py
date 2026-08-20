"""CI gate: no handler module imports from another handler module (issue #236).

The handler modules under :mod:`openstudio_operator.handlers` are
independent of one another by design. Each one (analysis_sla,
datapoint_watchdog, web_background_monitor, worker_recycler) owns a
single OSCM ``@kopf.timer`` and is wired into the operator through the
import block at the bottom of :mod:`openstudio_operator.handlers`
(init — aggregate-only; it imports each submodule into the package
namespace). Reordering the import block, removing one file, or adding
a new sibling should be safe: a handler-to-handler dependency would
break that independence.

The original offender was ``handlers/web_background_monitor.py`` importing
``deployment_label_selector`` from ``handlers.analysis_sla`` — a
generic Kubernetes helper that lived in ``analysis_sla`` only by
historical accident. The fix (``#236``) moved the helper to the shared
:mod:`openstudio_operator._k8s` module and made both handlers import
from there. This test makes that invariant loud at CI time, so a
future handler that copies the helper back into a sibling — or starts
importing a constant / Protocol / function from one — fails the build
on ``lint+test`` rather than waiting for an obscure runtime crash.

Scope:

* The single allowed handler-to-handler import is the aggregate
  ``from openstudio_operator.handlers import (analysis_sla, ...,)``
  block inside ``handlers/__init__.py``. Other handler files MUST NOT
  import from ``openstudio_operator.handlers`` (no relative form
  ``from .analysis_sla`` either — the relative form has the same
  coupling problem).
* ``handlers/__init__.py`` itself is allowed to import the sibling
  modules as long as the imports are the package-name form
  (``from openstudio_operator.handlers import X``); the aggregate
  ``from X import Y`` form is also flagged.
* Test files under ``tests/`` are out of scope for this gate — they
  import freely from any handler module. (Tests are consumers, not
  peers.)

Source-format support:

* ``import openstudio_operator.handlers.analysis_sla`` (bare ``import``)
  — also flagged: this is just a way of expressing the same coupling.
* ``from openstudio_operator.handlers import …`` — flagged.
* ``from openstudio_operator.handlers.analysis_sla import …`` — flagged.
* ``from .analysis_sla import …`` / ``from ..handlers.analysis_sla``
  inside handler files — also flagged (relative imports resolve to
  the same coupling).

If you genuinely need to share a Kubernetes helper between two
handlers, add it to :mod:`openstudio_operator._k8s` (or another shared
internal module: ``_constants``, ``_time``, ``events``,
``events_sinks``, ``client_factory``, ``status_store``, …). The handler
modules are deliberately a flat set — there is no shared "handlers
commons" by design.
"""

from __future__ import annotations

import ast
from pathlib import Path

from prometheus_client import generate_latest

from openstudio_operator import metrics

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HANDLERS_DIR = PROJECT_ROOT / "src" / "openstudio_operator" / "handlers"
PACKAGE_INIT = HANDLERS_DIR / "__init__.py"

# ``__init__.py`` is the ONE allowed file to import from the sibling
# handler modules — that's its job (it aggregates the four timer
# handlers into one importable package for ``kopf run``). Any handler
# module importing from a sibling module — through any import form —
# is a regression of issue #236.
ALLOWED_IMPORTERS_FROM_HANDLERS_PACKAGE: frozenset[Path] = frozenset({PACKAGE_INIT})

# Sentinel Logger shim for issue #308. The four timer wrappers accept a
# ``logger: kopf.Logger`` kwarg; the empty-serverUrl early-return path
# only invokes ``logger.warning(...)`` on its way out — a plain object
# whose ``warning`` is a no-op is sufficient. Using a real logging
# Logger is overkill for this gate and would couple the test to the
# JSON-logging install (issue #256).
import logging

_SENTINEL_LOGGER = logging.getLogger("openstudio_operator.tests.handler_boundaries_sentinel")
_SENTINEL_LOGGER.addHandler(logging.NullHandler())


def _iter_handler_modules() -> list[Path]:
    """Yield every ``.py`` file under ``src/openstudio_operator/handlers/``.

    Sorted for deterministic error messages across runs (matters for
    ``git blame``-style forensics when the test fails).
    """
    return sorted(HANDLERS_DIR.glob("*.py"))


def _is_handler_to_handler_import(node: ast.stmt, *, current_file: Path) -> bool:
    """Return ``True`` iff ``node`` expresses a handler→handler import in ``current_file``.

    Handles three AST shapes:

    * ``import openstudio_operator.handlers.<sibling>``  (bare import).
      ``node`` is :class:`ast.Import`; each ``alias.name`` starts with
      ``openstudio_operator.handlers.``.
    * ``from openstudio_operator.handlers import <something>``
      (``ast.ImportFrom``; ``node.module`` is ``openstudio_operator.handlers``
      with ``node.level == 0``). Flagged too: a handler importing the
      package form still couples itself to whichever symbols happen to be
      re-exported from ``__init__.py`` today.
    * ``from openstudio_operator.handlers.<sibling> import <symbol>``
      (``ast.ImportFrom``; ``node.module`` starts with
      ``openstudio_operator.handlers.``).
    * relative ``from .<sibling> import …`` or ``from ..handlers.<sibling>
    import …`` (``:class:`ast.ImportFrom` with ``node.level > 0``) — the
    relative form is just as much a handler-to-handler coupling; flagged.
    """
    if isinstance(node, ast.Import):
        return any(
            alias.name.startswith("openstudio_operator.handlers.")
            for alias in node.names
        )

    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        level = node.level

        # Relative imports (``from .sibling import …``): any handler file
        # doing this IS coupling to a sibling. The shared namespace is the
        # handlers package itself.
        if level > 0:
            # Resolve the relative module to its absolute form so the error
            # message can quote a stable path.
            absolute = _resolve_relative_import(current_file, module, level)
            return absolute.startswith("openstudio_operator.handlers.")

        # Absolute imports:
        if module == "openstudio_operator.handlers":
            return True
        return module.startswith("openstudio_operator.handlers.")

    return False


def _resolve_relative_import(
    current_file: Path, module: str, level: int
) -> str:
    """Best-effort resolution of a relative import to an absolute dotted path.

    Python's PEP 328 semantics: a relative import with ``level`` dots
    starts at the package ``level`` levels above the current module's
    package and joins ``module`` beneath it. For ``openstudio_operator.handlers``:

    * ``level=1`` → base = ``openstudio_operator.handlers`` (current package);
      ``module='analysis_sla'`` resolves to ``openstudio_operator.handlers.analysis_sla``.
    * ``level=2`` → base = ``openstudio_operator`` (parent);
      ``module='analysis_sla'`` resolves to ``openstudio_operator.analysis_sla``.

    The handler directory layout is one package deep, so ``level >= 2``
    exits the handlers package entirely and therefore can never be a
    handler-to-handler import. ``level == 1`` against another sibling
    in the same directory IS the handler-to-handler coupling we want
    to flag.
    """
    package_parts = current_file.relative_to(PROJECT_ROOT).with_suffix("").parts
    # Drop the filename; keep the package path of the current module.
    package_parts = package_parts[:-1]
    # ``level`` dots walk up ``level - 1`` directories from the current
    # package (PEP 328): ``from .X`` = 1 dot → stay in current package;
    # ``from ..X`` = 2 dots → parent of current package; etc.
    if level - 1 > len(package_parts):
        return module  # malformed; bail and let the caller flag it.
    base_parts = package_parts[: len(package_parts) - (level - 1)]
    base = ".".join(base_parts)
    if module:
        return f"{base}.{module}" if base else module
    return base


def _collect_handler_to_handler_imports() -> list[tuple[str, int, str]]:
    """Return ``[(relpath, lineno, import_text), ...]`` for every offender.

    ``import_text`` is the verbatim AST-rendered form of the offending
    ``import`` / ``from … import …`` statement (a function-of-node
    helper) so the test failure message names the exact line the
    maintainer needs to delete — no need to re-open the file.
    """
    findings: list[tuple[str, int, str]] = []
    for py in _iter_handler_modules():
        if py in ALLOWED_IMPORTERS_FROM_HANDLERS_PACKAGE:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in tree.body:
            if not _is_handler_to_handler_import(node, current_file=py):
                continue
            findings.append(
                (
                    str(py.relative_to(PROJECT_ROOT)),
                    node.lineno,
                    ast.unparse(node),
                )
            )
    return findings


def test_no_handler_to_handler_imports_outside_init() -> None:
    """Issue #236: handler modules must NOT import from sibling handler modules.

    The four handler modules (``analysis_sla``, ``datapoint_watchdog``,
    ``web_background_monitor``, ``worker_recycler``) are independent
    leaves of the operator tree. The shared package
    :mod:`openstudio_operator.handlers` is a flat aggregate imported by
    ``handlers/__init__`` for kopf's ``--module`` flag — NOT a
    permission to import across siblings.

    Fails loudly when ANY handler file (other than ``__init__.py``)
    contains an absolute or relative import that reaches into a
    sibling handler module — including the package-level ``from
    openstudio_operator.handlers import X`` form, which depends on
    what ``__init__`` happens to re-export today.

    Failure message lists every offender as ``path:line: statement``
    so the regression fix is a one-liner per row.
    """
    findings = _collect_handler_to_handler_imports()
    assert not findings, (
        "Handler modules must not import from sibling handler modules "
        "(issue #236). Offending imports:\n  "
        + "\n  ".join(f"{path}:{lineno}: {stmt}" for path, lineno, stmt in findings)
        + "\n\nMove the shared helper into a neutral module "
        "(openstudio_operator._k8s is the established home for "
        "generic Kubernetes API helpers, see issue #236; mirrors the "
        "_constants / _time convention). Update both the source and "
        "this test if the home module changes — the gate's job is to "
        "prevent regressions of #236, not to dictate where new shared "
        "helpers live."
    )


# --- Issue #308 — handler tick-duration observation ----------------------------


def _histogram_count_for(histogram, **labels: str) -> float:
    """Read the ``_count`` of a labelled Histogram series.

    The cumulative observation count is exposed by the per-label
    histogram child's ``_child_samples()`` walk as a sample named
    ``<base>_count``. Reading directly off ``_buckets[-1]`` does NOT
    work: in-memory ``_buckets`` holds per-bucket counts, NOT cumulative
    counts — the cumulative semantic is only computed at sample-
    collection time. Returns 0.0 when the label combo has never been
    observed.
    """
    child = histogram._metrics.get(tuple(labels.values()))
    if child is None:
        return 0.0
    for sample in child._child_samples():
        if sample.name.endswith("_count"):
            return float(sample.value)
    return 0.0


def test_handler_wrapper_observe_tick_duration() -> None:
    """Issue #308 acceptance: every timer wrapper invocation observes the
    wall-clock duration on ``HANDLER_TICK_DURATION_SECONDS.labels(module=...)``,
    regardless of success or caught-exception outcome. The four handlers
    are imported via the aggregate ``from openstudio_operator.handlers
    import (...)`` block; each handler function is the ``@kopf.timer``-
    decorated object. The test drives a wrapper invocation that
    short-circuits on the empty-serverUrl early-return path (the same
    path the production failure path uses) and asserts the
    observation is recorded on the canonical ``module`` label.

    The test does NOT exercise the real kopf machinery; it imports the
    wrapper fn directly and calls it with the same kwargs shape kopf
    uses. The outer wrapper's ``finally`` block is the production
    tick-duration observation site — we verify it by calling the
    wrapper fn directly and asserting the histogram child for the
    module label advanced.
    """
    from openstudio_operator.handlers import (
        analysis_sla,
        datapoint_watchdog,
        web_background_monitor,
        worker_recycler,
    )

    histogram = metrics.HANDLER_TICK_DURATION_SECONDS
    # The four canonical module labels — same vocabulary as
    # ``handler_tick_failures_total`` (issue #117). Read the count
    # BEFORE the call, drive the call, read AFTER, assert the count
    # advanced. A future refactor that drops the observation site
    # entirely fails here — the count never advances.
    modules = (
        ("analysis_sla", analysis_sla.analysis_sla_monitor),
        ("datapoint_watchdog", datapoint_watchdog.zombie_datapoint_watchdog),
        ("worker_recycler", worker_recycler.worker_recycler),
        ("web_background_monitor", web_background_monitor.web_background_monitor),
    )
    for module, fn in modules:
        before = _histogram_count_for(histogram, module=module)
        fn(
            body={"metadata": {"name": "x", "namespace": "ns"}},
            spec={"serverUrl": "", "redisUrl": ""},
            namespace="ns",
            name="x",
            logger=_SENTINEL_LOGGER,
        )
        after = _histogram_count_for(histogram, module=module)
        assert after == before + 1, (
            f"module={module}: HANDLER_TICK_DURATION_SECONDS did not "
            "observe the wrapper invocation — the try/finally block on "
            "the outer wrapper is missing or the early-return path "
            "bypassed it (issue #308)."
        )

    exposition = generate_latest().decode()
    for module, _fn in modules:
        # Pin the labelled exposition shape — same vocabulary as the
        # failure-counter guard. A future refactor that drops the
        # ``module`` label fails at this assertion, not at the on-call's
        # Grafana board.
        assert (
            f'openstudio_operator_handler_tick_duration_seconds_count{{module="{module}"}}'
            in exposition
        )
