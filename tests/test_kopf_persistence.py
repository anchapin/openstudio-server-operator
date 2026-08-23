"""Issue #681 — kopf persistence stays off the read-only OSCM main resource.

The #228 RBAC grants the operator ``get/list/watch`` on
``openstudioclustermanagers`` and ``get/patch`` ONLY on the ``/status``
subresource. kopf's patch router (``kopf/_cogs/clients/patching.py``) sends
every patch key EXCEPT ``status`` to the MAIN resource URL — so the two kopf
defaults that target metadata (``AnnotationsDiffBaseStorage`` for the
diffbase, the ``KopfFinalizerMarker`` finalizer stamp demanded by every
``@kopf.timer``) produced #681's recurring ``APIForbiddenError`` retry storm
while the four OSCM timers kept working.

These tests pin the fix end to end:

* :func:`openstudio_operator.kopf_persistence.operator_persistence_settings`
  produces a status-scoped diffbase storage and ``finalizer=None``;
* :func:`openstudio_operator.kopf_persistence.disarm_oscm_finalizer_requirements`
  flips ``requires_finalizer`` off the OSCM spawning handlers (kopf 1.44
  hardcodes it True for timers — the setting alone is NOT enough, see the
  empirical test below);
* the real handler population (imported from
  ``openstudio_operator.handlers``) survives the disarm + singleton-guard
  sequence without re-demanding the finalizer;
* the programmatic entrypoint (:mod:`openstudio_operator.__main__`) wires it
  all together, because the ``kopf run`` CLI cannot carry settings.
"""

from __future__ import annotations

import sys
import types

import kopf

from openstudio_operator import kopf_persistence, singleton
from openstudio_operator._constants import CRD_GROUP, CRD_PLURAL, CRD_SPEC, CRD_VERSION


def _oscm_spawning(registry: object) -> list[object]:
    """The OSCM spawning handlers in a registry (same scan as the guard)."""
    return [
        h
        for h in registry._spawning._handlers  # type: ignore[attr-defined]
        if singleton._selector_matches_oscms(h)
    ]


# ---- Persistence settings -------------------------------------------------


def test_diffbase_storage_is_status_scoped():
    """The settings use kopf's StatusDiffBaseStorage — never the annotations
    default, whose ``store()`` PATCHes ``metadata.annotations`` on the main
    resource (403 under #228)."""
    settings = kopf_persistence.operator_persistence_settings()
    assert isinstance(
        settings.persistence.diffbase_storage, kopf.StatusDiffBaseStorage
    ), settings.persistence.diffbase_storage
    assert not isinstance(
        settings.persistence.diffbase_storage, kopf.AnnotationsDiffBaseStorage
    )
    assert settings.persistence.diffbase_storage.field == (
        "status",
        "kopf",
        "last-handled-configuration",
    )


def test_status_diffbase_store_lands_in_the_status_patch_key():
    """kopf's patch router keys off the TOP-LEVEL patch key: only ``status``
    goes to the ``/status`` URL. The storage must therefore write everything
    under a single ``status`` key."""
    storage = kopf.StatusDiffBaseStorage()
    patch = kopf.Patch()
    storage.store(body={}, patch=patch, essence={"spec": {"serverUrl": "x"}})
    assert list(patch.keys()) == ["status"], dict(patch)


def test_kopf_default_annotations_diffbase_would_patch_main_resource():
    """Negative control documenting WHY the kopf default is rejected: its
    ``store()`` writes into ``metadata`` — the main-resource patch path that
    #228 forbids (this is one of the two #681 403 sources)."""
    storage = kopf.AnnotationsDiffBaseStorage()
    patch = kopf.Patch()
    storage.store(body=kopf.Body({}), patch=patch, essence={"spec": {"serverUrl": "x"}})
    assert "metadata" in dict(patch), dict(patch)
    assert "status" not in dict(patch), dict(patch)


def test_finalizer_is_disabled():
    """``finalizer=None`` — no ``metadata.finalizers`` bookkeeping: the SA
    holds no delete verb, no ``@kopf.on.delete``/cleanup handler exists, and
    CR deletion simply ends the watch. Nothing to protect."""
    settings = kopf_persistence.operator_persistence_settings()
    assert settings.persistence.finalizer is None


# ---- Why finalizer=None alone is NOT enough (kopf 1.44.x) ----------------


def test_kopf_timers_hardcode_the_finalizer_requirement():
    """kopf 1.44 registers every ``@kopf.timer`` with
    ``requires_finalizer=True`` (kopf/on.py) — the setting cannot turn that
    off, which is why :func:`disarm_oscm_finalizer_requirements` exists."""
    registry = kopf.OperatorRegistry()

    @kopf.on.timer(**CRD_SPEC, registry=registry, interval=60)
    def _timer(**_: object) -> None:  # pragma: no cover (never invoked)
        pass

    (handler,) = registry._spawning._handlers  # type: ignore[attr-defined]
    assert handler.requires_finalizer is True, handler


def test_finalizer_none_block_deletion_would_stamp_null_finalizer():
    """Empirical fence for the disarm's necessity: with ``finalizer=None``,
    kopf's raw ``block_deletion`` appends ``None`` into
    ``metadata.finalizers`` — i.e. it would STILL JSON-patch the main
    resource (with an invalid null value) had the requirement not been
    disarmed. Verified against the installed kopf 1.44.x."""
    from kopf._cogs.structs import finalizers as kopf_finalizers

    body: dict = {}
    kopf_finalizers.block_deletion(body, None)
    assert body == {"metadata": {"finalizers": [None]}}, body


# ---- disarm_oscm_finalizer_requirements -----------------------------------


def test_disarm_flips_oscm_timers_only_and_is_idempotent():
    registry = kopf.OperatorRegistry()

    @kopf.on.timer(**CRD_SPEC, registry=registry, interval=60)
    def _oscm_timer(**_: object) -> None:  # pragma: no cover (never invoked)
        pass

    @kopf.on.timer("example.com", "v1", "widgets", registry=registry, interval=60)
    def _foreign_timer(**_: object) -> None:  # pragma: no cover (never invoked)
        pass

    (oscm_handler, foreign_handler) = registry._spawning._handlers  # type: ignore[attr-defined]

    assert kopf_persistence.disarm_oscm_finalizer_requirements(registry) == 1
    assert oscm_handler.requires_finalizer is True  # the ORIGINAL object: untouched
    assert _oscm_spawning(registry)[0].requires_finalizer is False  # the replacement
    assert foreign_handler.requires_finalizer is True  # scoped: non-OSCM keeps kopf semantics

    # Idempotent: a re-run finds nothing left to disarm.
    assert kopf_persistence.disarm_oscm_finalizer_requirements(registry) == 0


def test_disarm_is_defensive_on_registry_internals_drift(caplog):
    """Mirrors the singleton guard: if kopf's private ``_spawning._handlers``
    layout moves, warn loudly and disarm NOTHING rather than fail silently."""
    bogus = types.SimpleNamespace()  # no _spawning at all
    with caplog.at_level("WARNING"):
        assert kopf_persistence.disarm_oscm_finalizer_requirements(bogus) == 0
    assert "not as expected" in caplog.text, caplog.text


# ---- The real handler population (fence tests) ----------------------------


def _ensure_metrics_stubbed() -> None:
    """Stub ``start_metrics_server`` BEFORE the handlers package import.

    Same pattern as tests/test_singleton_registry_coverage.py: the handlers
    package binds /metrics at import time, which a test process must not do.
    """
    import openstudio_operator.metrics as metrics_mod

    if getattr(metrics_mod.start_metrics_server, "__wrapped_for_test__", False):
        return

    def _stub(*args: object, **kwargs: object) -> int:
        return 9090

    _stub.__wrapped_for_test__ = True  # type: ignore[attr-defined]
    metrics_mod.start_metrics_server = _stub  # type: ignore[assignment]


def _ensure_handlers_loaded() -> None:
    _ensure_metrics_stubbed()
    import openstudio_operator.handlers  # noqa: F401  — imported for registration


def test_real_registry_oscm_timers_are_disarmed():
    """After the production sequence (handlers import → singleton guard →
    disarm), every OSCM spawning handler in the REAL registry must carry
    ``requires_finalizer=False`` — and still be singleton-gated (the guard's
    ``dataclasses.replace`` copy and the disarm's compose)."""
    _ensure_handlers_loaded()
    registry = kopf.get_default_registry()
    oscm = _oscm_spawning(registry)
    assert oscm, "handlers package did not register its OSCM timers"

    kopf_persistence.disarm_oscm_finalizer_requirements(registry)

    for handler in _oscm_spawning(registry):
        assert handler.requires_finalizer is False, getattr(handler, "id", "?")
        assert getattr(handler.fn, singleton.GUARD_MARKER, False), getattr(
            handler, "id", "?"
        )


def test_no_oscm_handlers_require_the_finalizer():
    """Fence: no OSCM handler may (re-)demand the finalizer after the disarm.

    Fails loudly if a contributor adds an ``@kopf.on.delete`` /
    ``@kopf.on.cleanup`` (or any changing-cause) handler for the OSCM
    resource: those register with ``requires_finalizer=True`` in kopf and
    would re-arm the main-resource finalizer PATCH that #681 removed — they
    cannot be supported under the #228 RBAC and must be revisited
    deliberately (with a real finalizer story), not slipped in."""
    _ensure_handlers_loaded()
    registry = kopf.get_default_registry()
    kopf_persistence.disarm_oscm_finalizer_requirements(registry)

    spawning = _oscm_spawning(registry)
    assert spawning
    assert all(h.requires_finalizer is False for h in spawning)

    changing = [
        h
        for h in registry._changing._handlers  # type: ignore[attr-defined]
        if singleton._selector_matches_oscms(h)
    ]
    assert changing == [], [
        (getattr(h, "id", "?"), getattr(h, "requires_finalizer", None)) for h in changing
    ]


# ---- Programmatic entrypoint ----------------------------------------------


def test_parse_args_namespace_default_and_override():
    import openstudio_operator.__main__ as operator_main

    assert operator_main._parse_args([]).namespace == "openstudio-server"
    assert operator_main._parse_args(["--namespace", "other"]).namespace == "other"


def test_main_wires_persistence_settings_and_disarms_registry(monkeypatch):
    """``python -m openstudio_operator`` must hand kopf.run the #681 settings
    (status diffbase + finalizer=None) with the default watch namespace, and
    leave the default registry's OSCM timers disarmed. kopf.run/configure are
    stubbed: this asserts the wiring, not the reactor."""
    import openstudio_operator.__main__ as operator_main

    _ensure_handlers_loaded()

    captured: dict = {}
    monkeypatch.setattr(kopf, "run", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(kopf, "configure", lambda **_: None)
    monkeypatch.setattr(sys, "argv", ["openstudio_operator"])

    operator_main.main()

    assert captured["namespaces"] == ["openstudio-server"], captured
    settings = captured["settings"]
    assert isinstance(settings.persistence.diffbase_storage, kopf.StatusDiffBaseStorage)
    assert settings.persistence.finalizer is None

    registry = kopf.get_default_registry()
    oscm = _oscm_spawning(registry)
    assert oscm
    assert all(h.requires_finalizer is False for h in oscm)


def test_constants_still_drive_the_matching():
    """The disarm is selector-scoped to the canonical CRD identity; keep the
    constants honest (a rename that missed this module would silently widen
    or narrow the disarm)."""
    assert CRD_SPEC == {
        "group": CRD_GROUP,
        "version": CRD_VERSION,
        "plural": CRD_PLURAL,
    }
