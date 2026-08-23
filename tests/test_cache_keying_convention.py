"""Per-CR cache keying + reset-seam convention tests (issues #497 / #652).

The convention lives in :mod:`openstudio_operator._cr_cache` (docstring +
census): every module-level per-CR handler cache is keyed by
``(namespace, name)`` and UID-VALIDATED, with a uniform
``reset_per_cr_caches(namespace=None, name=None)`` seam on each
cache-bearing module. These tests pin the shared helpers, the seam
contract on every census module, and the delete+recreate (#364)
no-leak behavior AT THE SEAM — the behavioral end-to-end proofs (a
recreated CR earning a fresh stall window / re-earning its exhaustion
Warning through the real tick functions) live next to their harnesses
in ``test_web_background_monitor.py`` and ``test_datapoint_watchdog.py``.

Since #652 the census itself is fenced: the tuples are single-sourced
in :mod:`tests._cache_census` (imported by this file AND by conftest's
autouse reset), and the AST fence tests at the bottom walk
``src/openstudio_operator/handlers/*.py`` and fail any module that is
in neither tuple — a new handler with a module-level cache can no
longer ship unclassified, with no seam, no uid validation, and no
conftest reset.
"""

import ast
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from _cache_census import (
    CACHE_BEARING_MODULES,
    CACHE_FREE_MODULES,
    NON_CONVENTION_CACHE_SHAPED_STATE,
)
from openstudio_operator import _cr_cache
from openstudio_operator.handlers import datapoint_watchdog, web_background_monitor

NS = "openstudio-server"
NAME = "oscm"
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
WINDOW = timedelta(minutes=10)

#: ``CACHE_BEARING_MODULES`` / ``CACHE_FREE_MODULES`` are single-sourced in
#: ``tests/_cache_census.py`` (issue #652) — imported here for the seam
#: tests below and by conftest for the autouse reset; no hand-maintained
#: duplicate exists anywhere. ``CACHE_BEARING_MODULES`` members MUST expose
#: the uniform reset seam; ``CACHE_FREE_MODULES`` members deliberately
#: expose none (memory in CR ``.status``, D04, or state documented in
#: ``NON_CONVENTION_CACHE_SHAPED_STATE``).


@pytest.fixture(autouse=True)
def _clean_module_caches():
    """Drop every per-CR cache entry before AND after each test here."""
    for module in CACHE_BEARING_MODULES:
        module.reset_per_cr_caches()
    yield
    for module in CACHE_BEARING_MODULES:
        module.reset_per_cr_caches()


# --- Shared helpers (_cr_cache) ------------------------------------------------


def test_cr_uid_extracts_from_dict_body() -> None:
    assert _cr_cache.cr_uid({"metadata": {"uid": "abc-123"}}) == "abc-123"


def test_cr_uid_tolerates_missing_or_malformed_metadata() -> None:
    assert _cr_cache.cr_uid({}) is None
    assert _cr_cache.cr_uid({"metadata": {}}) is None
    assert _cr_cache.cr_uid({"metadata": None}) is None
    assert _cr_cache.cr_uid({"metadata": "not-a-mapping"}) is None
    assert _cr_cache.cr_uid(None) is None  # type: ignore[arg-type]
    assert _cr_cache.cr_uid(42) is None  # type: ignore[arg-type]


def test_cr_uid_reads_mapping_view_bodies() -> None:
    """kopf >=1.4x delivers ``body`` as a Body MappingView, not a dict."""

    class BodyLike:
        """Duck-typed stand-in for kopf.Body (Mapping, not dict subclass)."""

        def get(self, key: str, default: object = None) -> object:
            return {"metadata": {"uid": "view-uid"}}.get(key, default)

    assert _cr_cache.cr_uid(BodyLike()) == "view-uid"


def test_uid_is_stale_requires_both_uids_present_and_unequal() -> None:
    assert _cr_cache.uid_is_stale("uid-a", "uid-b") is True  # the #364 signature
    assert _cr_cache.uid_is_stale("uid-a", "uid-a") is False
    assert _cr_cache.uid_is_stale(None, "uid-b") is False  # pre-#497 entry
    assert _cr_cache.uid_is_stale("uid-a", None) is False  # synthetic body
    assert _cr_cache.uid_is_stale(None, None) is False


# --- Seam contract on every census module --------------------------------------


def test_every_cache_bearing_module_exposes_the_uniform_seam() -> None:
    for module in CACHE_BEARING_MODULES:
        seam = getattr(module, "reset_per_cr_caches", None)
        assert callable(seam), (
            f"{module.__name__} is in the #497 cache-bearing census but "
            "exposes no reset_per_cr_caches seam — see "
            "openstudio_operator/_cr_cache.py for the convention."
        )


def test_cache_free_modules_stay_cache_free() -> None:
    """Cache-free census members hold no #497 seam (D04 or documented exempt).

    Their memory is the CR ``.status`` subresource (softStops anchors /
    lastRecycleAt), a per-tick re-stamp, or — for ``dry_run_audit`` —
    the one dedup cache documented in
    ``NON_CONVENTION_CACHE_SHAPED_STATE``. No ``reset_per_cr_caches``
    seam should exist. If this fails, someone added module-level per-CR
    state to a cache-free module — route it through the #497 convention
    instead (key + uid-validate + seam).
    """
    for module in CACHE_FREE_MODULES:
        assert not hasattr(module, "reset_per_cr_caches"), (
            f"{module.__name__} was census-classified cache-free (its "
            "memory lives in CR .status, D04) but now exposes a reset "
            "seam — update the #497 census in _cr_cache.py either way."
        )


@pytest.mark.parametrize("module", CACHE_BEARING_MODULES)
def test_seam_rejects_half_specified_scope(module) -> None:
    """Namespace-only / name-only resets are caller bugs, not scoped resets."""
    with pytest.raises(ValueError):
        module.reset_per_cr_caches(namespace=NS)
    with pytest.raises(ValueError):
        module.reset_per_cr_caches(name=NAME)


# --- Delete+recreate (#364) no-leak proofs at the seam -------------------------


def test_tracker_cache_delete_recreate_starts_fresh_window() -> None:
    """CR A accumulates 9 of 10 window minutes; recreated CR B starts at zero.

    The pre-#497 leak: the tracker keyed only by ``(namespace, name)``
    survived the delete, so the recreated CR's first holding tick would
    satisfy the OLD window and restart web_background early — NOT the
    conservative fresh-observation semantics D07 promises.
    """
    tracker_a = web_background_monitor._get_tracker(NS, NAME, uid="uid-a")
    tracker_a.observe(True, NOW, WINDOW)
    tracker_a.observe(True, NOW + timedelta(minutes=9), WINDOW)
    assert tracker_a.first_observed == NOW  # 9 of 10 minutes accumulated

    # Delete + recreate: same (namespace, name), NEW uid → fresh tracker.
    tracker_b = web_background_monitor._get_tracker(NS, NAME, uid="uid-b")
    assert tracker_b is not tracker_a
    assert tracker_b.first_observed is None  # the leak would carry NOW here
    # The fresh clock does not satisfy the window on its first holding tick
    # even at the timestamp that WOULD have satisfied CR A's window.
    assert tracker_b.observe(True, NOW + timedelta(minutes=10), WINDOW) is False
    # ...but earns its own window after a full sustained span.
    assert tracker_b.observe(True, NOW + timedelta(minutes=20), WINDOW) is True


def test_exhausted_cache_delete_recreate_starts_fresh_dedup() -> None:
    """CR A's exhaustion-dedup entries must not silence CR B's Warnings."""
    seen_a = datapoint_watchdog._get_exhausted_seen(NS, NAME, uid="uid-a")
    seen_a.add("dp-1")

    # Same CR (same uid): stable set identity — dedup keeps working.
    assert datapoint_watchdog._get_exhausted_seen(NS, NAME, uid="uid-a") is seen_a

    # Delete + recreate: same (namespace, name), NEW uid → fresh set.
    seen_b = datapoint_watchdog._get_exhausted_seen(NS, NAME, uid="uid-b")
    assert seen_b is not seen_a
    assert seen_b == set()  # the leak would carry {"dp-1"} here


def test_uid_validation_records_uid_on_first_sighting() -> None:
    """A uid-less lookup followed by a uid'd one arms later validation."""
    seen = datapoint_watchdog._get_exhausted_seen(NS, NAME)  # pre-#497 shape
    seen.add("dp-1")
    # First uid sighting is recorded without discarding the state it cannot
    # prove stale (None never declares staleness).
    assert datapoint_watchdog._get_exhausted_seen(NS, NAME, uid="uid-a") is seen
    # Now a different uid IS provably stale → fresh set.
    assert datapoint_watchdog._get_exhausted_seen(NS, NAME, uid="uid-b") == set()


@pytest.mark.parametrize("module", CACHE_BEARING_MODULES)
def test_seam_scoped_reset_drops_exactly_one_cr(module) -> None:
    """``reset_per_cr_caches(namespace, name)`` drops one key, keeps the rest."""
    if module is web_background_monitor:
        module._get_tracker(NS, NAME, uid="uid-a")
        module._get_tracker("other-ns", "other", uid="uid-x")
    else:
        module._get_exhausted_seen(NS, NAME, uid="uid-a")
        module._get_exhausted_seen("other-ns", "other", uid="uid-x")

    module.reset_per_cr_caches(NS, NAME)

    remaining = set(module._tracker_cache if module is web_background_monitor
                    else module._EXHAUSTED_WARNED)
    assert remaining == {("other-ns", "other")}

    module.reset_per_cr_caches()  # drop all
    remaining = set(module._tracker_cache if module is web_background_monitor
                    else module._EXHAUSTED_WARNED)
    assert remaining == set()


# --- The #652 AST fence: census derived from the code under test ----------------
#
# Template: ``test_python_registry_includes_all_oscm_spawning_handlers``
# (#250) — derive the expectation instead of trusting a hand list. The
# fence walks ``src/openstudio_operator/handlers/*.py`` with ``ast`` and
# fails any handler module that is in NEITHER census tuple, so the
# blind spot that let ``dry_run_audit`` / ``redis_layout_check`` ship
# unclassified (and that would let a fifth timer handler add a
# module-level ``_cache: dict = {}`` with no seam, no uid validation,
# and no conftest autouse reset — the exact regression #497 exists to
# prevent) is now a CI failure instead of silent drift.

_HANDLERS_DIR = pathlib.Path(__file__).resolve().parent.parent / "src" / "openstudio_operator" / "handlers"


def _handler_module_files() -> dict[str, pathlib.Path]:
    """Map file stem → path for every handler module (``__init__`` excluded).

    ``__init__.py`` is the operator entrypoint (module imports + guard
    installation), not a handler module — it deliberately has no census
    classification of its own.
    """
    return {
        path.stem: path
        for path in sorted(_HANDLERS_DIR.glob("*.py"))
        if path.name != "__init__.py"
    }


def _assigned_names(node: ast.stmt) -> tuple[list[str], ast.expr | None]:
    """Names + value for a module-level ``Assign`` / valued ``AnnAssign``."""
    if isinstance(node, ast.Assign):
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        return names, node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [node.target.id] if isinstance(node.target, ast.Name) else [], node.value
    return [], None


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _convention_cache_names(tree: ast.Module) -> set[str]:
    """Module globals that INSTANTIATE the executable convention (#583).

    ``cr_cache.PerCRCache(...)`` / ``PerCRCache(...)`` at module level —
    how both cache-bearing modules declare their caches
    (``_EXHAUSTED_WARNED``, ``_tracker_cache``).
    """
    names: set[str] = set()
    for stmt in tree.body:
        targets, value = _assigned_names(stmt)
        if isinstance(value, ast.Call) and _call_name(value) == "PerCRCache":
            names.update(targets)
    return names


def _factory_names(tree: ast.Module) -> set[str]:
    """Module-level ``_get_*`` functions — the cache-shaped factory pattern."""
    return {
        stmt.name
        for stmt in tree.body
        if isinstance(stmt, ast.FunctionDef) and stmt.name.startswith("_get_")
    }


def _is_empty_container(value: ast.expr) -> bool:
    """``{}`` / ``set()`` literals and their no-arg ``dict()`` / ``set()`` calls."""
    if isinstance(value, ast.Dict):
        return not value.keys
    if isinstance(value, ast.Set):
        return not value.elts
    return (
        isinstance(value, ast.Call)
        and _call_name(value) in ("dict", "set")
        and not value.args
        and not value.keywords
    )


def _bare_container_names(tree: ast.Module) -> set[str]:
    """Module globals assigned an EMPTY dict/set literal or no-arg constructor.

    The "fifth timer handler adds ``_cache: dict = {}``" shape from the
    #652 issue text — cache-shaped by construction, so it must be routed
    through the convention (or explicitly exempted).
    """
    names: set[str] = set()
    for stmt in tree.body:
        targets, value = _assigned_names(stmt)
        if _is_empty_container(value):
            names.update(targets)
    return names


def test_cache_census_classifies_every_handler_module_exactly_once() -> None:
    """#652 exhaustiveness: every handler module is in exactly one tuple.

    Unclassified is the original blind spot (``dry_run_audit`` /
    ``redis_layout_check`` both shipped that way); double-classified or
    phantom entries (a tuple naming a module with no file) are equally
    fence failures — the census must stay a partition of the directory.
    """
    files = _handler_module_files()
    bearing = {module.__name__.rsplit(".", 1)[-1] for module in CACHE_BEARING_MODULES}
    free = {module.__name__.rsplit(".", 1)[-1] for module in CACHE_FREE_MODULES}

    assert not bearing & free, (
        f"Handler module(s) {sorted(bearing & free)} appear in BOTH census "
        f"tuples — a module is either cache-bearing or cache-free, never "
        f"both. Fix tests/_cache_census.py."
    )
    unclassified = set(files) - bearing - free
    assert not unclassified, (
        f"Handler module(s) {sorted(unclassified)} are in NEITHER census "
        f"tuple (issue #652). Classify each one in tests/_cache_census.py: "
        f"per-CR caches go through the #497 convention "
        f"(PerCRCache + reset_per_cr_caches seam → CACHE_BEARING_MODULES, "
        f"and conftest resets them automatically); modules with no "
        f"module-level per-CR cache go to CACHE_FREE_MODULES. Leaving a "
        f"module unclassified reintroduces the exact blind spot the fence "
        f"exists to close."
    )
    phantom = (bearing | free) - set(files)
    assert not phantom, (
        f"Census tuple(s) name handler module(s) {sorted(phantom)} with no "
        f"file under src/openstudio_operator/handlers/ — stale census "
        f"entries; remove them from tests/_cache_census.py."
    )


def test_cache_bearing_census_matches_ast_cache_evidence() -> None:
    """#652: convention-shaped declarations and the census agree, both ways.

    A module-level ``PerCRCache`` instantiation or ``_get_*`` factory
    MUST be on a ``CACHE_BEARING_MODULES`` module (that's what makes the
    conftest autouse reset + uid validation apply to it); a census entry
    with no such declaration is stale and must go.
    """
    files = _handler_module_files()
    bearing = {module.__name__.rsplit(".", 1)[-1] for module in CACHE_BEARING_MODULES}
    for stem, path in files.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        evidence = _convention_cache_names(tree) | _factory_names(tree)
        if stem in bearing:
            assert evidence, (
                f"{stem}.py is census-classified cache-bearing but declares "
                f"no PerCRCache global and no module-level _get_* factory — "
                f"either the cache moved (update tests/_cache_census.py) or "
                f"the declaration stopped being convention-shaped."
            )
        else:
            assert not evidence, (
                f"{stem}.py declares convention-shaped cache state "
                f"({sorted(evidence)}) but is NOT in CACHE_BEARING_MODULES "
                f"(issue #652). Route it through the #497 convention "
                f"(PerCRCache + reset_per_cr_caches seam) and add it to "
                f"tests/_cache_census.py, or remove the cache."
            )


def test_module_level_dict_set_state_is_fenced() -> None:
    """#652: bare module-level dict/set globals must be classified or exempt.

    This is the motivating scenario from the issue text: a handler that
    adds ``_cache: dict = {}`` keyed ``(namespace, name)`` with no seam,
    no uid validation, and no conftest autouse reset. Such a global must
    either live on a cache-bearing module (whose caches go through
    ``PerCRCache``) or be explicitly enumerated in
    ``NON_CONVENTION_CACHE_SHAPED_STATE`` with a documented lifecycle of
    its own — everything else fails here.
    """
    files = _handler_module_files()
    bearing = {module.__name__.rsplit(".", 1)[-1] for module in CACHE_BEARING_MODULES}
    exempt = {
        module.__name__.rsplit(".", 1)[-1]: set(names)
        for module, names in NON_CONVENTION_CACHE_SHAPED_STATE.items()
    }
    for stem, path in files.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bare = _bare_container_names(tree)
        if stem in bearing:
            continue  # convention home — PerCRCache globals are dict-shaped by design
        unexplained = bare - exempt.get(stem, set())
        assert not unexplained, (
            f"{stem}.py assigns module-level empty dict/set global(s) "
            f"{sorted(unexplained)} outside the #497 convention (issue "
            f"#652). Either route the state through PerCRCache + the "
            f"reset_per_cr_caches seam (CACHE_BEARING_MODULES in "
            f"tests/_cache_census.py) or add it to "
            f"NON_CONVENTION_CACHE_SHAPED_STATE there with a documented "
            f"lifecycle — the same triaged-exception pattern as "
            f".pip-audit-ignore.txt."
        )
    stale_pairs: dict[str, set[str]] = {}
    for stem, names in exempt.items():
        if stem not in files:
            stale_pairs[stem] = set(names)  # exempted module itself is gone
            continue
        tree = ast.parse(files[stem].read_text(encoding="utf-8"), filename=str(files[stem]))
        gone = names - _bare_container_names(tree)
        if gone:
            stale_pairs[stem] = gone
    assert not stale_pairs, (
        f"NON_CONVENTION_CACHE_SHAPED_STATE names global(s) that no longer "
        f"exist as module-level dict/set assignments: {stale_pairs} — "
        f"prune the entry in tests/_cache_census.py."
    )
