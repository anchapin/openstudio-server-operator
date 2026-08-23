"""kopf persistence configuration that keeps framework bookkeeping OFF the
read-only OSCM main resource (issue #681).

The #228 RBAC shape is deliberate: the operator's Role holds
``get/list/watch`` on ``openstudioclustermanagers`` and ``get/patch`` ONLY on
the ``/status`` subresource — the CR body itself is read-only from the
operator's perspective. kopf's own bookkeeping, however, defaults to writing
the MAIN resource:

* ``diffbase_storage=AnnotationsDiffBaseStorage`` (the kopf default) stores
  the last-handled essence under ``metadata.annotations`` — a main-resource
  PATCH.
* ``finalizer='kopf.zalando.org/KopfFinalizerMarker'`` (the kopf default) is
  stamped into ``metadata.finalizers`` whenever any spawning handler
  (``@kopf.timer``/``@kopf.daemon``) serves the resource — kopf 1.44
  hardcodes ``requires_finalizer=True`` for timers/daemons at registration
  (``kopf/on.py``), because a deletion must stop them. With the four OSCM
  timers registered, EVERY processing cycle queued a
  ``block_deletion`` JSON-patch against the main resource → the recurring
  ``APIForbiddenError`` retry storm captured in #681 (the timers themselves
  kept working — only the bookkeeping write was forbidden).

kopf's patch router (``kopf/_cogs/clients/patching.py::patch_obj``) splits a
patch by top-level key: ``status`` goes to the ``/status`` subresource URL,
everything else (``metadata`` included) goes to the main-resource URL. That
is why StatusStore RMW works while annotation/finalizer writes 403.

This module produces settings that make every kopf-persisted field
status-scoped or absent:

* ``diffbase_storage=StatusDiffBaseStorage`` — kopf's public status-stanza
  storage (``status.kopf.last-handled-configuration``), routed through
  ``/status`` like every other operator status write. (Dormant today — it is
  only stored from changing-cause handling, and the operator registers no
  ``@kopf.on.create/update/resume`` handlers — but it arms the fence before
  anyone adds one.)
* ``finalizer=None`` — the documented kopf way to disable finalizer
  management. NECESSARY BUT NOT SUFFICIENT: kopf 1.44's timer registration
  still demands a finalizer, and ``finalizers.block_deletion(body, None)``
  would append ``None`` into ``metadata.finalizers`` — still a main-resource
  patch (with an invalid null value, to boot). Hence the companion
  :func:`disarm_oscm_finalizer_requirements`, which flips
  ``requires_finalizer`` off the OSCM spawning handlers so
  ``deletion_must_be_blocked`` is never True and neither the block nor the
  unblock finalizer patch is ever queued.

The KopfFinalizerMarker is deliberately absent for this operator shape: the
service account holds NO ``delete`` verb on OSCM CRs, no ``@kopf.on.delete``
/ ``@kopf.on.cleanup`` handler exists (fenced by
``tests/test_kopf_persistence.py::test_no_oscm_handlers_require_the_finalizer``),
and CR deletion simply ends the watch — there is nothing for a finalizer to
protect. The private-registry reach below mirrors the singleton guard's
documented kopf-internals dependency (``registry._spawning._handlers``,
kopf 1.37+; see :mod:`openstudio_operator.singleton` and
``tests/test_singleton_registry_coverage.py``).
"""

from __future__ import annotations

import dataclasses
import logging

import kopf

logger = logging.getLogger(__name__)


def operator_persistence_settings() -> kopf.OperatorSettings:
    """Build kopf settings whose persistence writes never touch the main resource.

    Returns a fresh :class:`kopf.OperatorSettings` with:

    * ``persistence.diffbase_storage`` = :class:`kopf.StatusDiffBaseStorage`
      (status-subresource-routed; the kopf default
      ``AnnotationsDiffBaseStorage`` would PATCH ``metadata.annotations`` on
      the main resource — forbidden under #228),
    * ``persistence.finalizer`` = ``None`` (no ``metadata.finalizers``
      bookkeeping; the SA cannot delete CRs, so nothing needs blocking).

    NOTE: these two settings alone do NOT stop the #681 finalizer PATCH while
    OSCM timers are registered — kopf 1.44 hardcodes
    ``requires_finalizer=True`` for timers. Call
    :func:`disarm_oscm_finalizer_requirements` as well (the programmatic
    entrypoint ``openstudio_operator.__main__`` does both, in this order).
    """
    settings = kopf.OperatorSettings()
    settings.persistence.diffbase_storage = kopf.StatusDiffBaseStorage()
    settings.persistence.finalizer = None
    return settings


def disarm_oscm_finalizer_requirements(registry: object | None = None) -> int:
    """Flip ``requires_finalizer`` off every OSCM spawning handler (#681).

    Walks the kopf registry's private ``_spawning._handlers`` list (kopf
    1.37+; the same internal the singleton guard wraps fns on) and replaces
    each OSCM ``@kopf.timer``/``@kopf.daemon`` with an identical handler
    carrying ``requires_finalizer=False``. With that flag off (and
    ``persistence.finalizer=None``), kopf's processing loop computes
    ``deletion_must_be_blocked=False`` and queues neither the
    ``block_deletion`` nor the ``allow_deletion`` patch — the main-resource
    JSON-patch that produced #681's ``APIForbiddenError`` retry storm is
    structurally unreachable.

    Scoped to OSCM handlers only (selector match, like the singleton guard):
    a future spawning handler on a DIFFERENT resource keeps kopf's default
    finalizer semantics — and would surface its own RBAC error rather than
    being silently disarmed.

    Defensive on kopf-internals drift, mirroring
    :func:`openstudio_operator.singleton.install_singleton_guard`: if the
    registry layout is not as expected, warn loudly and disarm nothing
    (return ``0``) rather than failing silently. Idempotent; returns the
    number of newly disarmed handlers.

    MUST run after the handler modules are imported (the timers register at
    import time) and after :func:`openstudio_operator.singleton.install_singleton_guard`
    (its ``dataclasses.replace`` copies would otherwise carry the old flag —
    order-independent in practice because both mutate the same list in place,
    but the canonical order is handlers-import → guard → disarm, as wired in
    ``openstudio_operator.__main__``).
    """
    from openstudio_operator import singleton

    reg = registry if registry is not None else kopf.get_default_registry()
    spawning = getattr(reg, "_spawning", None)
    handlers = getattr(spawning, "_handlers", None)
    if not isinstance(handlers, list):
        logger.warning(
            "kopf persistence (#681): kopf registry internals not as expected "
            "— OSCM finalizer requirements NOT disarmed; expect main-resource "
            "finalizer PATCH 403s (see tests/test_singleton_registry_coverage.py)"
        )
        return 0

    disarmed = 0
    for index, handler in enumerate(list(handlers)):
        if not singleton._selector_matches_oscms(handler):
            continue
        if not getattr(handler, "requires_finalizer", False):
            continue  # already disarmed (idempotent re-run)
        try:
            handlers[index] = dataclasses.replace(handler, requires_finalizer=False)
        except TypeError:
            logger.warning(
                "kopf persistence (#681): cannot disarm handler %r — not "
                "replaceable; it may still demand the finalizer",
                getattr(handler, "id", "?"),
            )
            continue
        disarmed += 1
    if disarmed:
        logger.info(
            "kopf persistence (#681): disarmed the finalizer requirement on "
            "%d OSCM spawning handler(s) — kopf bookkeeping now never writes "
            "the main resource (SA holds no delete verb; nothing to protect)",
            disarmed,
        )
    return disarmed
