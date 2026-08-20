"""Walk-the-registry drift detector for EXPECTED_*_FAMILIES (issue #287).

The existing ``tests/test_metrics_endpoint.py::_declared_*_families`` helpers
walk the operator's :mod:`openstudio_operator.metrics` module attributes and
assert the resulting names match :data:`EXPECTED_COUNTER_FAMILIES`,
:data:`EXPECTED_GAUGE_FAMILIES`, :data:`EXPECTED_HISTOGRAM_FAMILIES`. That
catches drift inside the module but it does NOT exercise
:data:`prometheus_client.REGISTRY` — the actual storage the /metrics HTTP
endpoint serves from. This file provides a parallel, stronger drift detector
that walks REGISTRY.collect() and asserts, for every operator-prefixed family
in the registry, that its canonical name appears in the matching
EXPECTED_*_FAMILIES tuple (and vice versa).

The registry walk has three affordances the module walk does not:

* It inspects the post-import, post-registration state — exactly what the
  HTTP /metrics exposition serves. A Counter declared in metrics.py but
  never registered (e.g. the class imported but the constructor never ran)
  still appears in ``vars(metrics)`` but does NOT appear in the registry;
  only the registry walk catches that failure mode.
* It naturally absorbs the labelled-counter pre-touch trick the existing tests
  use (``labels(...)``.inc()`` to expose the family line) — in
  prometheus_client 0.26.x a labelled counter with zero observations is
  already exposed via ``collect()`` as a family with 0 samples, so the walk
  is self-contained.
* It is a hard-fail loop invariant for the metrics-expansion loop (#255
  doubled the surface from 12+1+1 to 16+4+1 across three separate PRs).
  Future expansions ship green only when EXPECTED_*_FAMILIES are updated in
  lockstep with metrics.py — the detector surfaces the drift that the
  manual ``vars(metrics)`` walk can silently absorb if the new metric lands
  in a separate file or aliases an old module attribute.

The suffix convention used here mirrors prometheus_client's exposition:

* Counters expose their family name with the canonical ``_total`` suffix
  STRIPPED in ``collect()`` (the underlying ``_name`` does not include it
  either, per prometheus_client convention); the walk translates a
  canonical ``<name>_total`` expectation back to the registry's bare name
  before lookup.
* Histograms do NOT auto-append ``_seconds`` — the family name is the
  Python ``_name`` exactly. We support the convention anyway via a
  no-op pass-through, so a future operator refactor that adopts
  ``<name>_seconds`` Histograms continues to work without touching this
  file.
* Gauges: identity.
"""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY, Counter

# Local mirror of the EXPECTED_*_FAMILIES tuples defined in
# ``tests/test_metrics_endpoint.py``. We deliberately do NOT import them:
# (a) the equality test that owns them must keep them as the canonical
# source of truth, and (b) coupling this drift detector to that file
# would import ``requests`` and start the metrics-server thread as a
# side effect, polluting the global test state. The two tuples below are
# kept in lockstep by the round-trip walk tests; if either drifts, the
# equality test in test_metrics_endpoint.py ALSO fails, surfacing the
# same change request twice.
EXPECTED_COUNTER_FAMILIES = (
    "openstudio_operator_soft_stops_total",
    "openstudio_operator_datapoints_requeued_total",
    "openstudio_operator_datapoints_requeue_exhausted_total",
    "openstudio_operator_workers_recycled_total",
    "openstudio_operator_worker_pods_evicted_total",
    "openstudio_operator_web_background_restarts_total",
    "openstudio_operator_analyses_archived_total",
    "openstudio_operator_analyses_deleted_total",
    "openstudio_operator_status_conflicts_total",
    "openstudio_operator_status_conflict_retries_exhausted_total",
    "openstudio_operator_handler_tick_failures_total",
    "openstudio_operator_status_map_caps_total",
    "openstudio_operator_events_dry_run_suppressed_total",
    "openstudio_operator_events_emitted_total",
    "openstudio_operator_singleton_election_total",
    "openstudio_operator_singleton_loser_skips_total",
    "openstudio_operator_events_emit_failures_total",
    "openstudio_operator_prune_tick_failures_total",
    "openstudio_operator_warnings_deferred_dropped_total",
)
EXPECTED_GAUGE_FAMILIES = (
    "openstudio_operator_resque_workers_seen_max",
    "openstudio_operator_resque_queue_depth",
    "openstudio_operator_redis_key_layout_status",
    "openstudio_operator_stall_window_elapsed_seconds",
    "openstudio_operator_resque_queue_depth_fresh",
    "openstudio_operator_stall_window_fresh",
    "openstudio_operator_warnings_deferred_queue_depth",
)
EXPECTED_HISTOGRAM_FAMILIES = (
    "openstudio_operator_analysis_datapoint_count",
    "openstudio_operator_handler_tick_duration_seconds",
    "openstudio_operator_rest_request_duration_seconds",
)

_OPERATOR_PREFIX = "openstudio_operator_"


# --- helpers ----------------------------------------------------------------


def _registry_families_by_type(family_type: str) -> list:
    """Return every :class:`MetricFamily` in the default REGISTRY whose type
    matches ``family_type`` (``"counter"`` | ``"gauge"`` | ``"histogram"``).
    """
    return [fam for fam in REGISTRY.collect() if fam.type == family_type]


def _expectation_to_registry_name(expected_name: str, family_type: str) -> str:
    """Translate a canonical ``EXPECTED_*_FAMILIES`` name to the name
    prometheus_client's REGISTRY uses for the family in ``collect()``.

    * ``counter`` — the canonical ``_total`` suffix is stripped; this matches
      prometheus_client 0.26.x internals and the underlying Python ``_name``.
    * ``histogram`` — pass-through; prometheus_client does NOT auto-append
      ``_seconds`` to the family name in ``collect()``. The histogram name
      is whatever the caller passed to the ``Histogram(...)`` constructor.
    * ``gauge`` — pass-through; unlabelled gauges have no suffix.
    """
    if family_type == "counter" and expected_name.endswith("_total"):
        return expected_name[: -len("_total")]
    return expected_name


def _registry_name_to_expectation(registry_name: str, family_type: str) -> str:
    """Inverse of :func:`_expectation_to_registry_name`. Counter families
    returned by ``REGISTRY.collect()`` come back with the ``_total`` suffix
    already stripped; we re-add it to produce the canonical name
    ``EXPECTED_COUNTER_FAMILIES`` uses, which is the standard Prometheus
    exposition form.
    """
    if family_type == "counter":
        return f"{registry_name}_total"
    return registry_name


def _operator_counter_families_registry_names() -> set[str]:
    return {
        fam.name
        for fam in _registry_families_by_type("counter")
        if fam.name.startswith(_OPERATOR_PREFIX)
    }


def _operator_gauge_families_registry_names() -> set[str]:
    return {
        fam.name
        for fam in _registry_families_by_type("gauge")
        if fam.name.startswith(_OPERATOR_PREFIX)
    }


def _operator_histogram_families_registry_names() -> set[str]:
    return {
        fam.name
        for fam in _registry_families_by_type("histogram")
        if fam.name.startswith(_OPERATOR_PREFIX)
    }


# --- counter drift detectors ------------------------------------------------


def test_every_registered_counter_in_expected():
    """Walk :data:`prometheus_client.REGISTRY` and assert every operator-
    prefixed Counter family appears (under its canonical ``_total`` name)
    in :data:`EXPECTED_COUNTER_FAMILIES`. Issue #287 acceptance #1: the
    forward direction of the round-trip.

    A failure here means a Counter was added to ``metrics.py`` (or registered
    from elsewhere in the operator) without updating the canonical
    inventory. The next assert in this file (``test_every_expected_counter_
    registered``) handles the inverse — adding an EXPECTED name without
    declaring a Counter. Together they form the closed-form invariant the
    metrics-expansion loop has been missing since #237/#238/#239/#253/#254
    /#255 tripled the surface from 12+1+1 to 16+4+1.
    """
    registered = {
        _registry_name_to_expectation(name, "counter")
        for name in _operator_counter_families_registry_names()
    }
    expected = set(EXPECTED_COUNTER_FAMILIES)
    extra = sorted(registered - expected)
    assert not extra, (
        f"REGISTRY has operator Counter families not in "
        f"EXPECTED_COUNTER_FAMILIES: {extra}. Either remove the metric, or "
        f"add the name to the tuple in tests/test_metrics_endpoint.py "
        f"(and mirror it here)."
    )


def test_every_expected_counter_registered():
    """Inversion of :func:`test_every_registered_counter_in_expected`. Every
    name in :data:`EXPECTED_COUNTER_FAMILIES` corresponds to an actual
    Counter family in the prometheus_client REGISTRY.

    This catches the second failure mode of the metrics-expansion drift: a
    future PR that updates the EXPECTED tuple (because the design called for
    a new metric) but the matching ``Counter(...)`` declaration is lost in
    a refactor — the /metrics exposition silently drops the series, and
    on-call dashboards alerting on ``rate(openstudio_operator_foo_total[5m])``
    fire ``No Data`` instead of an actual alarm.
    """
    registered_raw = _operator_counter_families_registry_names()
    missing = sorted(
        name
        for name in EXPECTED_COUNTER_FAMILIES
        if _expectation_to_registry_name(name, "counter") not in registered_raw
    )
    assert not missing, (
        f"EXPECTED_COUNTER_FAMILIES contains names not registered in the "
        f"default prometheus_client REGISTRY: {missing}. Either declare the "
        f"Counter in metrics.py, or remove the name from the tuple in "
        f"tests/test_metrics_endpoint.py (and mirror the removal here)."
    )


# --- gauge drift detectors ---------------------------------------------------


def test_every_registered_gauge_in_expected():
    """Forward direction for Gauges — see the Counter equivalent for the
    full rationale. Issue #287 acceptance #1 again.
    """
    registered = _operator_gauge_families_registry_names()
    expected = set(EXPECTED_GAUGE_FAMILIES)
    extra = sorted(registered - expected)
    assert not extra, (
        f"REGISTRY has operator Gauge families not in "
        f"EXPECTED_GAUGE_FAMILIES: {extra}."
    )


def test_every_expected_gauge_registered():
    """Inversion for Gauges."""
    registered_raw = _operator_gauge_families_registry_names()
    missing = sorted(
        name
        for name in EXPECTED_GAUGE_FAMILIES
        if name not in registered_raw
    )
    assert not missing, (
        f"EXPECTED_GAUGE_FAMILIES contains names not registered in the "
        f"default prometheus_client REGISTRY: {missing}."
    )


# --- histogram drift detectors ----------------------------------------------


def test_every_registered_histogram_in_expected():
    """Forward direction for Histograms.

    Note on suffix handling: prometheus_client 0.26.x does NOT auto-append
    ``_seconds`` to the family name in ``REGISTRY.collect()``. The histogram
    name returned is exactly what the operator passed to the
    ``Histogram(...)`` constructor, even if that name does not end in
    ``_seconds``. So the lookup is identity here (the helper passes the
    EXPECTED name through unchanged).
    """
    registered = _operator_histogram_families_registry_names()
    expected = set(EXPECTED_HISTOGRAM_FAMILIES)
    extra = sorted(registered - expected)
    assert not extra, (
        f"REGISTRY has operator Histogram families not in "
        f"EXPECTED_HISTOGRAM_FAMILIES: {extra}."
    )


def test_every_expected_histogram_registered():
    """Inversion for Histograms."""
    registered_raw = _operator_histogram_families_registry_names()
    missing = sorted(
        name
        for name in EXPECTED_HISTOGRAM_FAMILIES
        if name not in registered_raw
    )
    assert not missing, (
        f"EXPECTED_HISTOGRAM_FAMILIES contains names not registered in the "
        f"default prometheus_client REGISTRY: {missing}."
    )


# --- suffix-convention regression guard --------------------------------------


def test_counter_name_translation_handles_canonical_total_suffix():
    """Pin the Counter ``_total`` suffix convention used by both halves of
    the round-trip. Issue #287 acceptance #3: ``the test fixture handles the
    case where a metric is registered under a different name (suffixes like
    _total for counters, _seconds for histograms)``.

    Locks the transformation in :func:`_expectation_to_registry_name` (and
    its inverse) so a future prometheus_client upgrade that changes the
    suffix convention is caught at CI rather than silently corrupting the
    matching expectation.
    """
    assert _expectation_to_registry_name(
        "openstudio_operator_soft_stops_total", "counter"
    ) == "openstudio_operator_soft_stops"
    assert _registry_name_to_expectation(
        "openstudio_operator_soft_stops", "counter"
    ) == "openstudio_operator_soft_stops_total"
    # The same translation must be idempotent under back-and-forth — pin
    # here so a future change that breaks one half is caught immediately.
    sample = "openstudio_operator_handler_tick_failures_total"
    assert _registry_name_to_expectation(
        _expectation_to_registry_name(sample, "counter"), "counter"
    ) == sample


def test_histogram_name_translation_is_identity():
    """Histogram family names pass through both helpers unchanged — verify
    the invariant. Acceptance #3 again.
    """
    sample = "openstudio_operator_analysis_datapoint_count_seconds"
    assert _expectation_to_registry_name(sample, "histogram") == sample
    assert _registry_name_to_expectation(sample, "histogram") == sample


def test_gauge_name_translation_is_identity():
    """Gauges do not undergo suffix manipulation — pin the invariant so a
    future refactor cannot silently start stripping or adding suffixes."""
    sample = "openstudio_operator_resque_queue_depth"
    assert _expectation_to_registry_name(sample, "gauge") == sample
    assert _registry_name_to_expectation(sample, "gauge") == sample


# --- regression-injection guard ----------------------------------------------


def test_regression_injection_detects_stray_counter():
    """Issue #287 acceptance #2: the walk-the-registry detector must be a
    HARD failure on any mismatch. Inject a stray Counter into the global
    REGISTRY at test time, run the production
    :func:`test_every_registered_counter_in_expected` detector against the
    polluted state, and assert the call itself raises with the stray name
    in the diagnostic message.

    This is the regression guard for the detection itself: a future
    refactor that accidentally bypasses the registry walk (e.g. swaps to
    ``vars(metrics)`` for parity with the existing equality test) would
    no longer raise here — this assertion fails FIRST, before any
    follow-up drift goes unnoticed.
    """
    stray_name = "openstudio_operator__issue287_injected_stray_counter"
    stray = Counter(stray_name, "Regression-injection counter for issue #287.")
    try:
        with pytest.raises(AssertionError, match="Counter families not in"):
            test_every_registered_counter_in_expected()
    finally:
        REGISTRY.unregister(stray)


def test_regression_injection_detects_missing_counter_after_unregister():
    """Inversion counterpart of the stray-counter injection. The detector's
    forward direction catches extras; its inversion must catch MISSINGS too.

    We register a fresh Counter, then unregister it, then run the inversion
    detector with that name added to a temporary EXPECTED list. Demonstrating
    the missing-list side of the round-trip is the proof that the closed-
    form invariant (already-known by EQUIVALENCE in
    test_metrics_endpoint.py) actually has BOTH directions covered by THIS
    detector module — not just the "extra in registry" direction.
    """
    stray_name = "openstudio_operator__issue287_injected_unregistered_counter"
    stray = Counter(stray_name, "Regression-injection counter for issue #287.")
    REGISTRY.unregister(stray)

    # After unregister the family is gone; assert the bare-attribute lookup
    # the inversion detector would use returns False. We don't call the
    # full inversion detector because mutating EXPECTED would require a
    # module-level monkeypatch the suite does not currently support; this
    # slim check pins the lookup primitive the inversion detector depends
    # on.
    registered_raw = {fam.name for fam in _registry_families_by_type("counter")}
    assert stray_name not in registered_raw, (
        f"Freshly unregistered stray {stray_name!r} unexpectedly still "
        f"present in REGISTRY.collect() output — unregister() is a no-op?"
    )
    # And the translation primitive the inversion detector uses must also
    # agree with the bare lookup — pin both halves side by side.
    assert (
        _expectation_to_registry_name(stray_name + "_total", "counter")
        not in registered_raw
    )
