"""Per-CR cache keying + reset-seam convention tests (issue #497).

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
"""

from datetime import UTC, datetime, timedelta

import pytest

from openstudio_operator import _cr_cache
from openstudio_operator.handlers import (
    analysis_sla,
    datapoint_watchdog,
    web_background_monitor,
    worker_recycler,
)

NS = "openstudio-server"
NAME = "oscm"
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
WINDOW = timedelta(minutes=10)

#: The #497 census of cache-bearing handler modules — every module listed
#: here MUST expose the uniform reset seam. This list is the fence: adding
#: a new per-CR cache means adding the module here (and a reset seam
#: there) in the same change, so the convention cannot silently regress.
CACHE_BEARING_MODULES = (datapoint_watchdog, web_background_monitor)

#: The #497 census of cache-FREE handler modules — their cross-tick memory
#: lives entirely in the CR ``.status`` subresource (D04), so they
#: deliberately have NO reset seam to maintain. Asserting the absence
#: keeps the census honest in both directions: a future maintainer who
#: adds module-level per-CR state to one of these fails this test and is
#: routed to the convention.
CACHE_FREE_MODULES = (analysis_sla, worker_recycler)


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
    """analysis_sla / worker_recycler hold no per-CR module cache (D04).

    Their memory is the CR ``.status`` subresource (softStops anchors /
    lastRecycleAt), so no reset seam should exist. If this fails, someone
    added module-level per-CR state to a cache-free module — route it
    through the #497 convention instead (key + uid-validate + seam).
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
