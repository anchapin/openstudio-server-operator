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


# --- Issue #397 — DryRunToggled audit Event on spec.dryRun transitions ------------


def _make_oscm_body(
    dry_run: bool | None = False,
    *,
    namespace: str = "openstudio-server",
    name: str = "oscm-a",
    managed_fields: list[dict] | None = None,
) -> dict:
    """A minimal OSCM CR body in the shape kopf delivers to watch handlers.

    ``dry_run=None`` omits the key entirely (the pre-toggle baseline shape
    most existing CRs carry — ``config.py`` defaults it to False).
    """
    body: dict = {
        "apiVersion": "energy.nrel.gov/v1alpha1",
        "kind": "OpenStudioClusterManager",
        "metadata": {"namespace": namespace, "name": name},
        "spec": {"serverUrl": "http://web.openstudio-server.svc.cluster.local"},
    }
    if dry_run is not None:
        body["spec"]["dryRun"] = dry_run
    if managed_fields is not None:
        body["metadata"]["managedFields"] = managed_fields
    return body


def _make_audit_sink() -> tuple[list[tuple[dict, str, str, str]], object]:
    """Recorder sink in the repo's EventSink shape (mirrors test_singleton_guard)."""
    events: list[tuple[dict, str, str, str]] = []

    def emit(obj: dict, event_type: str, reason: str, message: str) -> None:
        events.append((obj, event_type, reason, message))

    return events, emit


def _drain_dryruntoggled(events) -> list[tuple[dict, str, str, str]]:
    return [e for e in events if e[2] == "DryRunToggled"]


def test_dry_run_toggle_false_to_true_emits_exactly_one_event() -> None:
    """Issue #397 acceptance: flipping ``dryRun`` false → true fires the
    ``DryRunToggled`` Event reason exactly ONCE per transition — not per
    reconcile. The first observation (operator start / initial listing) is a
    baseline and emits nothing; the transition emits exactly one Normal
    Event carrying the old and new value as ``"false"``/``"true"`` strings
    in the message (kopf.event accepts no Event labels in 1.37–1.44).
    """
    from openstudio_operator.handlers import dry_run_audit

    dry_run_audit.reset_audit_state()
    events, emit = _make_audit_sink()

    # First sight — baseline, no emission (operator boot / initial listing).
    assert dry_run_audit.record_dry_run_transition(
        _make_oscm_body(dry_run=False), logger=_SENTINEL_LOGGER, emit=emit
    ) is False
    # The patch: dryRun flipped on.
    assert dry_run_audit.record_dry_run_transition(
        _make_oscm_body(dry_run=True), logger=_SENTINEL_LOGGER, emit=emit
    ) is True
    # Steady state after the flip — the operator's own .status RMW patches
    # fire watch events; none of them may re-emit.
    assert dry_run_audit.record_dry_run_transition(
        _make_oscm_body(dry_run=True), logger=_SENTINEL_LOGGER, emit=emit
    ) is False

    toggled = _drain_dryruntoggled(events)
    assert len(toggled) == 1, f"expected exactly one DryRunToggled, got {len(toggled)}"
    obj, event_type, reason, message = toggled[0]
    assert reason == "DryRunToggled"
    assert event_type == "Normal"
    assert obj["metadata"]["name"] == "oscm-a"
    assert obj["metadata"]["namespace"] == "openstudio-server"
    # The old/new values ride in the message as lowercase strings.
    assert "false" in message and "true" in message
    assert "false → true" in message
    # The audit signal must NOT be routed through the D11 gate — it fires
    # even though the NEW mode is dry-run (that's the whole point of #397):
    assert "SUPPRESSED" in message


def test_dry_run_unchanged_emits_no_events_on_reconcile_noise() -> None:
    """Issue #397 scope guard: zero emissions when dryRun never transitions —
    repeated observations (reconciles, resyncs, the operator's own status
    patches) must stay silent. The diff is the gate."""
    from openstudio_operator.handlers import dry_run_audit

    dry_run_audit.reset_audit_state()
    events, emit = _make_audit_sink()

    for _ in range(5):
        dry_run_audit.record_dry_run_transition(
            _make_oscm_body(dry_run=True), logger=_SENTINEL_LOGGER, emit=emit
        )

    assert events == [], f"unchanged dryRun must emit nothing, got {events}"


def test_dry_run_toggles_back_and_forth_emit_once_per_transition() -> None:
    """Every ACTUAL transition emits once — the sequence F,T,F,T,T,F,F carries
    four edges (F→T, T→F, F→T, T→F) and four ``DryRunToggled`` Events; the
    repeated levels (T,T and F,F tails) emit nothing."""
    from openstudio_operator.handlers import dry_run_audit

    dry_run_audit.reset_audit_state()
    events, emit = _make_audit_sink()

    for dry_run in (False, True, False, True, True, False, False):
        dry_run_audit.record_dry_run_transition(
            _make_oscm_body(dry_run=dry_run), logger=_SENTINEL_LOGGER, emit=emit
        )

    toggled = _drain_dryruntoggled(events)
    assert len(toggled) == 4
    # Edges in order.
    assert "false → true" in toggled[0][3]
    assert "true → false" in toggled[1][3]
    assert "false → true" in toggled[2][3]
    assert "true → false" in toggled[3][3]


def test_dry_run_missing_field_defaults_false_and_first_toggle_emits() -> None:
    """A CR created without ``spec.dryRun`` (the CRD-defaulted shape) baselines
    as False — ``config.py`` applies the same default — so the first explicit
    ``dryRun: true`` patch emits ``false → true``."""
    from openstudio_operator.handlers import dry_run_audit

    dry_run_audit.reset_audit_state()
    events, emit = _make_audit_sink()

    dry_run_audit.record_dry_run_transition(
        _make_oscm_body(dry_run=None), logger=_SENTINEL_LOGGER, emit=emit
    )
    emitted = dry_run_audit.record_dry_run_transition(
        _make_oscm_body(dry_run=True), logger=_SENTINEL_LOGGER, emit=emit
    )

    assert emitted is True
    toggled = _drain_dryruntoggled(events)
    assert len(toggled) == 1
    assert "false → true" in toggled[0][3]


def test_dry_run_toggle_tracks_crs_independently() -> None:
    """The last-seen cache is per-CR: two OSCM CRs toggling independently each
    emit on their own transitions (and a loser-CR toggle is just as audible —
    the handler deliberately does not go through the singleton gate)."""
    from openstudio_operator.handlers import dry_run_audit

    dry_run_audit.reset_audit_state()
    events, emit = _make_audit_sink()

    bodies = [
        _make_oscm_body(dry_run=False, name="alpha"),
        _make_oscm_body(dry_run=False, name="beta"),
    ]
    for body in bodies:
        dry_run_audit.record_dry_run_transition(body, logger=_SENTINEL_LOGGER, emit=emit)

    dry_run_audit.record_dry_run_transition(
        _make_oscm_body(dry_run=True, name="alpha"), logger=_SENTINEL_LOGGER, emit=emit
    )
    # beta unchanged → still silent.
    dry_run_audit.record_dry_run_transition(
        _make_oscm_body(dry_run=False, name="beta"), logger=_SENTINEL_LOGGER, emit=emit
    )
    dry_run_audit.record_dry_run_transition(
        _make_oscm_body(dry_run=True, name="beta"), logger=_SENTINEL_LOGGER, emit=emit
    )

    toggled = _drain_dryruntoggled(events)
    assert len(toggled) == 2
    assert toggled[0][0]["metadata"]["name"] == "alpha"
    assert toggled[1][0]["metadata"]["name"] == "beta"


def test_dry_run_toggle_message_cites_managed_fields_writer() -> None:
    """Best-effort acting-principal attribution: the message cites the latest
    ``metadata.managedFields`` manager (the on-object last-writer hint) and
    points at the apiserver audit log."""
    from openstudio_operator.handlers import dry_run_audit

    dry_run_audit.reset_audit_state()
    events, emit = _make_audit_sink()
    managed = [
        {
            "manager": "openstudio-operator",
            "operation": "Update",
            "time": "2026-08-19T10:00:00Z",
        },
        {
            "manager": "kubectl-edit",
            "operation": "Update",
            "time": "2026-08-20T09:30:00Z",
        },
    ]

    dry_run_audit.record_dry_run_transition(
        _make_oscm_body(dry_run=False), logger=_SENTINEL_LOGGER, emit=emit
    )
    dry_run_audit.record_dry_run_transition(
        _make_oscm_body(dry_run=True, managed_fields=managed),
        logger=_SENTINEL_LOGGER,
        emit=emit,
    )

    toggled = _drain_dryruntoggled(events)
    assert len(toggled) == 1
    assert "'kubectl-edit'" in toggled[0][3]
    assert "audit log" in toggled[0][3]


def test_dry_run_toggle_audit_handler_fires_once_per_kopf_event_transition(
    monkeypatch,
) -> None:
    """Issue #397 acceptance, end-to-end shape: drive the REAL
    ``@kopf.on.event`` handler fn with the kwargs kopf delivers; a dryRun
    patch (old body false → new body true) produces exactly one Event; a
    further reconcile with the same value produces none."""
    from openstudio_operator.handlers import dry_run_audit

    dry_run_audit.reset_audit_state()
    events, emit = _make_audit_sink()
    monkeypatch.setattr(dry_run_audit, "_emit_kopf_event", emit)

    handler = dry_run_audit.dry_run_toggle_audit
    handler(
        body=_make_oscm_body(dry_run=False),
        namespace="openstudio-server",
        name="oscm-a",
        logger=_SENTINEL_LOGGER,
        type="MODIFIED",
    )
    handler(
        body=_make_oscm_body(dry_run=True),
        namespace="openstudio-server",
        name="oscm-a",
        logger=_SENTINEL_LOGGER,
        type="MODIFIED",
    )
    handler(
        body=_make_oscm_body(dry_run=True),
        namespace="openstudio-server",
        name="oscm-a",
        logger=_SENTINEL_LOGGER,
        type="MODIFIED",
    )

    toggled = _drain_dryruntoggled(events)
    assert len(toggled) == 1
    assert toggled[0][1] == "Normal"
    assert toggled[0][2] == "DryRunToggled"


def test_dry_run_toggle_audit_handler_deleted_evicts_cache(monkeypatch) -> None:
    """DELETED watch events evict the cache entry: a RECREATED same-name CR
    starts a fresh baseline instead of diffing against the dead CR's value
    (no false-positive DryRunToggled on the new CR's initial listing)."""
    from openstudio_operator.handlers import dry_run_audit

    dry_run_audit.reset_audit_state()
    events, emit = _make_audit_sink()
    monkeypatch.setattr(dry_run_audit, "_emit_kopf_event", emit)

    handler = dry_run_audit.dry_run_toggle_audit
    common = {"namespace": "openstudio-server", "name": "oscm-a", "logger": _SENTINEL_LOGGER}
    handler(body=_make_oscm_body(dry_run=False), type="ADDED", **common)
    handler(body=_make_oscm_body(dry_run=True), type="MODIFIED", **common)  # 1 emit
    handler(body=_make_oscm_body(dry_run=True), type="DELETED", **common)
    # Recreated with dryRun still true... then toggled false: only the real
    # transition (true→false) may emit, not the recreation baseline.
    handler(body=_make_oscm_body(dry_run=True), type="ADDED", **common)
    handler(body=_make_oscm_body(dry_run=False), type="MODIFIED", **common)

    toggled = _drain_dryruntoggled(events)
    assert len(toggled) == 2
    assert "false → true" in toggled[0][3]
    assert "true → false" in toggled[1][3]


def test_dry_run_toggle_audit_is_a_watch_handler_never_gated() -> None:
    """Wiring invariant for #397: ``dry_run_toggle_audit`` lives in kopf's
    WATCHING registry (fires on events, not on a timer), is NOT wrapped by
    the singleton ``_gated`` wrapper, and is NOT registered in the
    Python-level spawning-handler registry (that registry exists to gate
    timers/daemons only)."""
    import kopf

    import openstudio_operator.handlers  # noqa: F401 — populates the default registry
    from openstudio_operator import singleton

    registry = kopf.get_default_registry()
    watching_ids = [getattr(h, "id", "?") for h in registry._watching._handlers]
    assert "dry_run_toggle_audit" in watching_ids

    for handler in registry._watching._handlers:
        if getattr(handler, "id", "") == "dry_run_toggle_audit":
            assert not getattr(handler.fn, singleton.GUARD_MARKER, False)
            break

    from openstudio_operator import _oscm_handlers

    assert not _oscm_handlers.is_registered("dry_run_toggle_audit")
