"""Redis key-layout validation, in its own module (issues #163 / #490 / #584).

Issue #163 — boot-time Redis key-layout validation (D05/D13 invariant).
``validate_key_layout()`` is documented in AGENTS.md as the startup-time
assertion that the Redis Service ``queue`` exposes the Resque keyspace the
operator reads (``resque:worker:*``, ``resque:queue:simulations``, etc.). A
quiet drift (helm chart upgrade to a Resque-2.x-with-different-prefix
layout, or a different queue backend entirely) would otherwise only be
noticed downstream when the worker-registry gauges go silent — too late for
the boot-time identity the singleton guard polices. The fix is to call
``validate_key_layout()`` once per CR at operator boot (the kopf watch's
initial listing IS the boot path for any non-zero namespace), emit a
structured log line ``redis_key_layout=ok|degraded|unreachable``, and queue
a Warning Event on the degraded branch. The whole call is wrapped in
try/except so a Redis connectivity failure degrades gracefully (operator
continues to boot, retries on the next CR tick) — never crashes the
process. The drain path is the consolidated
:func:`openstudio_operator.handlers._drain_queued_warning_events`.

Issue #490 — validation is no longer boot-only: the web_background stall
tick re-runs this check every
``_constants.REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL`` (5 min) via
``web_background_monitor._maybe_revalidate_redis_key_layout``, so a
mid-flight layout drift is caught within a bounded window instead of
waiting for a CR edit to happen to coincide. Every run stamps the paired
freshness gauge through :func:`_set_redis_key_layout_status` below.

Issue #584 — why this is a module and not a function in
``handlers/__init__.py``: historically the check lived in the package
``__init__``, and because ``__init__`` imports ``web_background_monitor``
at package load, the #490 rider had to reach back for it via a
function-local ``from openstudio_operator.handlers import
_check_redis_key_layout_for_cr`` — a real import cycle that only worked
deferred, and only failed at the first stall tick. A handler module
depending on a private symbol of the package ``__init__`` is the boundary
violation this module eliminates: the check is a domain function over the
Redis surface, so it lives here, imports ONLY neutral domain modules
(``config`` / ``client_factory`` / ``events_sinks`` / ``metrics`` /
``redis_client``) — never sibling handler modules — and handler modules
may import IT (the boundary gate in ``tests/test_handler_boundaries.py``
allowlists this module as shared support, #584).

Issue #590 — the watch path is freshness-gated. The kopf watch delivers
an event for EVERY OSCM write, including the ``.status`` subresource
patches the operator's own StatusStore RMW makes several times per SLA
tick — so pre-#590 every status write re-ran ``validate_key_layout()``
(two O(1) EXISTS probes plus, only on the failure path, a bounded
diagnostic SCAN — issue #688) for zero signal: the #490 cadence already
bounds how fresh the layout signal needs to be, and the gauges this module sets are
documented process-wide (no per-CR labels). The watch handler now skips
when the #490 freshness stamp (``REDIS_KEY_LAYOUT_STATUS_FRESH``, set in
lockstep by :func:`_set_redis_key_layout_status` — the SAME stamp the
periodic path maintains) is within
``REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL``, except on the kopf watch's
initial listing / resync (``type is None``), which validates
unconditionally — that IS the #163 boot path, and it must not depend on
gauge residue from a prior process or test.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping

import kopf

from openstudio_operator._constants import (
    CRD_SPEC,
    REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL,
)
from openstudio_operator.client_factory import get_read_only_redis_client
from openstudio_operator.config import OperatorConfig, OperatorConfigError
from openstudio_operator.events_sinks import get_default_sink
from openstudio_operator.metrics import (
    REDIS_KEY_LAYOUT_STATUS,
    REDIS_KEY_LAYOUT_STATUS_FRESH,
)
from openstudio_operator.redis_client import RedisClientError

logger = logging.getLogger(__name__)

# The SAME process-wide sink ``handlers/__init__.py`` binds and drains
# (``get_default_sink()`` returns the singleton ``QueuedKopfEventSink``
# instance): a deferral queued here is flushed by the package-level
# ``_drain_queued_warning_events`` @kopf.on.event handler and mirrored to
# ``status.deferredEvents`` by its #402 persistence wiring. No new queue,
# no wiring change — the sink identity is what keeps the drain path in
# ``__init__`` unchanged after the #584 move.
_sink = get_default_sink()


def _emit_redis_key_layout_event(
    namespace: str, name: str, reason: str, message: str
) -> None:
    """Defer a redis-key-layout Warning Event to the next OSCM watch tick.

    Counterpart to :func:`_check_redis_key_layout_for_cr`'s degraded
    branch. Fires from the per-CR check; the actual ``kopf.event``
    emit happens on the next OSCM watch tick (same queue/defer pattern
    as the ``RedisUrlEmpty`` helper in ``handlers/__init__.py`` for issue
    #116, both routed through the consolidated shared sink after #234).
    """
    _sink.defer_to_next_tick(
        namespace=namespace, name=name, reason=reason, message=message,
    )


def _set_redis_key_layout_status(value: float) -> None:
    """Set the #253 status gauge + its #490 freshness stamp in lockstep.

    Issue #490 — the status gauge alone is a blind-holds-value signal on
    a steady-state cluster (no OSCM watch events → no revalidation), so
    every terminal path of
    :func:`_check_redis_key_layout_for_cr` goes through THIS helper: the
    freshness timestamp proves the validator ran recently while the
    status value carries the result. Centralizing the pair here keeps
    the two gauges from drifting apart the way a hand-maintained second
    ``.set(...)`` line at each of the eight branches eventually would.
    """
    REDIS_KEY_LAYOUT_STATUS.set(value)
    REDIS_KEY_LAYOUT_STATUS_FRESH.set(time.time())


def _validate_item_meta(
    item: object,
) -> tuple[dict, str, str] | None:
    """Validate item has dict shape and extract namespace/name for the CR.

    Issue #726 — consolidates the three formerly-separate skipped branches
    into one helper. Issue #738 — ``kopf.Body`` is a ``Mapping``, not a
    ``dict``, so the top-level check uses ``Mapping`` instead of ``dict``.
    Returns a 3-tuple ``(meta, ns, nm)`` when the item is well-formed, or
    ``None`` when it is not (in which case the gauge has already been set
    to ``0.0`` by this function).
    """
    if not isinstance(item, Mapping):
        _set_redis_key_layout_status(0.0)
        return None
    meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else item
    if not isinstance(meta, dict):
        _set_redis_key_layout_status(0.0)
        return None
    ns = str(meta.get("namespace") or "")
    nm = str(meta.get("name") or "")
    if not ns or not nm:
        _set_redis_key_layout_status(0.0)
        return None
    return (meta, ns, nm)


def _key_layout_validation_is_fresh(*, now: float | None = None) -> bool:
    """Issue #590 — whether the #490 freshness stamp is within the cadence.

    Reads the SAME stamp the #490 periodic path maintains
    (:data:`openstudio_operator.metrics.REDIS_KEY_LAYOUT_STATUS_FRESH`,
    set in lockstep with the status gauge by
    :func:`_set_redis_key_layout_status` on every terminal path of the
    check — ``ok`` included), so the watch path and the periodic rider
    share one freshness budget: a validation run by EITHER path within
    ``REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL`` (5 min) suppresses the
    other. The gauge defaults to ``0.0`` (never validated this process),
    which reads as maximally stale — exactly the fresh-boot posture — so
    the first watch event of a process never skips.

    ``now`` defaults to :func:`time.time` and is injectable for tests.
    The ``Gauge._value.get()`` read is the repo's established direct-read
    seam (same as the test suite's gauge assertions).
    """
    current = time.time() if now is None else now
    last = float(REDIS_KEY_LAYOUT_STATUS_FRESH._value.get())  # type: ignore[attr-defined]
    return (current - last) < REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL.total_seconds()


def _check_redis_key_layout_for_cr(
    item: object, *, logger: logging.Logger
) -> str:
    """Run ``validate_key_layout()`` for one OSCM CR; return a status string.

    Returns one of ``"ok"``, ``"degraded"``, ``"unreachable"``, ``"error"``,
    or ``"skipped"`` (empty redis_url with no secretRef, nameless item,
    etc.). The handler controls the structured log line based on the return
    value; the test suite asserts the line is emitted (see
    ``tests/test_redis_client.py::test_redis_key_layout_check_emits_*``).

    Issue #567 — the check is secretRef-aware: the effective Redis URL is
    resolved through the factory's #463 path (``secret_ref`` from
    ``spec.redisCredentials.secretRef`` plus the CR's ``namespace``), so a
    secretRef-only CR (``spec.redisUrl`` empty — the preferred production
    shape) VALIDATES instead of returning ``"skipped"``. The skip branch
    survives only for a CR with neither an inline URL nor a secretRef
    (the #116 empty-``spec.redisUrl`` concern). A resolution failure
    (missing Secret/key, bad value) raises
    :class:`~openstudio_operator.redis_client.RedisCredentialResolutionError`
    — a :class:`~openstudio_operator.redis_client.RedisClientError`
    subclass — and therefore lands on the ``"unreachable"`` branch; a
    malformed secretRef raises ``ValueError`` in the config parse and
    lands on the ``"error"`` branch (the function stays total — never
    raises).

    Wrapped in try/except so a Redis connectivity failure (network down,
    DNS failure, refused connection, timeout) does NOT crash the boot —
    the operator continues in degraded mode and retries on the next tick,
    per the issue's "silent-misbehavior risk" counter-spec.

    Issue #253 — every return path updates the cluster-wide
    ``openstudio_operator_redis_key_layout_status`` Gauge: ``1.0`` on
    ``ok`` (the most recent validator run succeeded) and ``0.0`` for
    every other terminal status (``degraded`` | ``unreachable`` |
    ``error`` | ``skipped``). The gauge is a cluster-wide latest-observation
    signal — no per-CR labels, so cardinality stays bounded regardless of
    CR count.

    Issue #490 — every return path ALSO stamps the paired
    ``openstudio_operator_redis_key_layout_status_fresh`` timestamp gauge
    (via :func:`_set_redis_key_layout_status`, in lockstep with the
    status value). Callers besides the ``@kopf.on.event`` watch: the
    periodic revalidation riding the web_background stall tick
    (``web_background_monitor._maybe_revalidate_redis_key_layout`` on
    ``REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL``) — that cadence is what
    bounds the freshness gap a dashboard's ``time() - fresh`` computation
    alerts on.

    Issue #688 — the ``degraded`` verdict underneath is EXISTS-anchored:
    ``validate_key_layout()`` decides drift via direct O(1) ``EXISTS``
    probes of the two required Resque keys, so a ``degraded`` status
    always means an EXISTS-verified absence — NEVER a SCAN sample that
    was too small for the keyspace (the pre-#688 false-verdict class that
    pinned this gauge to 0.0 and fired the critical
    ``OpenStudioOperatorRedisKeyLayoutInvalid`` alert permanently on
    production-scale fleets). The bounded diagnostic SCAN runs only on
    the failure path, to build the error-message evidence.
    """
    # Issue #726 — consolidate three skipped branches into one helper guard.
    # Issue #738 — _validate_item_meta uses Mapping (not dict) for the
    # top-level check because kopf.Body is a Mapping.
    validated = _validate_item_meta(item)
    if validated is None:
        return "skipped"
    _meta, ns, nm = validated
    spec = item.get("spec") or {}
    try:
        # Issue #567 — parse through OperatorConfig (the single config
        # path) so the secretRef resolution matches the timer wire
        # closures exactly; the parse runs INSIDE the try so a malformed
        # secretRef keeps the function total (broad-except → "error").
        config = OperatorConfig.from_spec(spec)
        if not config.redis_url and config.redis_credentials.secret_ref is None:
            # Neither credential source is set — a separate concern
            # (#116) — don't fail layout validation on the operator's
            # intentional refusal to default.
            logger.debug(
                "redis_key_layout skip: OSCM %s/%s has empty spec.redisUrl "
                "and no spec.redisCredentials.secretRef (#116)",
                ns,
                nm,
            )
            _set_redis_key_layout_status(0.0)
            return "skipped"
        get_read_only_redis_client(
            config.redis_url,
            secret_ref=config.redis_credentials.secret_ref,
            namespace=ns,
        ).validate_key_layout()
    except OperatorConfigError as exc:
        # Layout drift (issue #44). Since #475 this class lives in config.py
        # and is NOT a RedisClientError subclass, so the ordering of this
        # except ladder no longer depends on subclassing — the layout-drift
        # branch is reachable only here, by name, which is the intent.
        logger.warning(
            "redis_key_layout=degraded namespace=%s name=%s reason=%s: %s",
            ns,
            nm,
            "layout_drift",
            exc,
        )
        _emit_redis_key_layout_event(
            ns,
            nm,
            "RedisKeyLayoutDrift",
            (
                "Redis key layout validation failed (issue #163): "
                f"{exc}. Modules 3/5 (worker recycler / web_background "
                "stall) may produce noisy signals or stay silent — the "
                "centralized Resque key constants in "
                "src/openstudio_operator/redis_client.py do not match the "
                "live Redis layout. Verify with "
                f"`redis-cli -u <redis_url> KEYS 'resque:*'` and update "
                "the constants (see issue #44 / docs/kind-validation.md)."
            ),
        )
        _set_redis_key_layout_status(0.0)
        return "degraded"
    except (RedisClientError, OSError) as exc:
        # Redis connectivity failure (refused, DNS, timeout) — wire-level,
        # not a layout drift. Since #567 this branch also absorbs the
        # secretRef resolution failures (``RedisCredentialResolutionError``
        # is a ``RedisClientError`` subclass): a missing Secret/key or a
        # fence-violating value reads as "unreachable", and the log line
        # names the exact object to fix. Loud warning, no event (we don't
        # know the layout drifted; we just couldn't reach the server).
        # Operator MUST continue to boot — the issue's hard requirement.
        logger.warning(
            "redis_key_layout=unreachable namespace=%s name=%s: %s",
            ns,
            nm,
            exc,
        )
        _set_redis_key_layout_status(0.0)
        return "unreachable"
    except Exception as exc:  # noqa: BLE001 — defensive last-resort (see web_background_monitor.py)
        logger.warning(
            "redis_key_layout=error namespace=%s name=%s: %s: %s",
            ns,
            nm,
            type(exc).__name__,
            exc,
        )
        _set_redis_key_layout_status(0.0)
        return "error"
    logger.info(
        "redis_key_layout=ok namespace=%s name=%s",
        ns,
        nm,
    )
    _set_redis_key_layout_status(1.0)
    return "ok"


@kopf.on.event(**CRD_SPEC)
def _redis_key_layout_check(
    name: str, namespace: str, body: kopf.Body, **_kwargs: object
) -> None:
    """Run ``validate_key_layout()`` per CR at boot (initial listing) and on every change.

    The kopf watch's initial listing fires this for every existing CR — that
    IS the boot path. Idempotent: ``validate_key_layout()`` is reentrant;
    its ok path is two O(1) EXISTS probes (issue #688) and its diagnostic
    SCAN sample stays capped at ``VALIDATE_SCAN_KEY_BUDGET`` keys. A queued
    Warning Event is
    drained on the next tick by the package-level
    :func:`openstudio_operator.handlers._drain_queued_warning_events`
    (the registration lives here since #584; ``handlers/__init__.py``
    imports this module in its aggregate block so the decorator runs at
    operator load).

    Issue #590 — steady-state watch events (``type`` is not ``None``) are
    freshness-gated: when the #490 stamp is within
    ``REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL`` the validation is skipped —
    a ``.status`` subresource patch (the operator's own StatusStore RMW,
    several per SLA tick under analysis churn) carries zero layout signal
    the cadence has not already bounded. Initial-listing / resync events
    (``type is None`` — kopf marks the watch's list-replay events this
    way, see ``kopf._core.reactor.processing``) bypass the gate and
    validate unconditionally, preserving the #163 boot contract.
    """
    if _kwargs.get("type") is not None and _key_layout_validation_is_fresh():
        logger.debug(
            "redis_key_layout watch skip (#590): validation fresh within "
            "REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL (%ss) — not re-running "
            "for %s/%s",
            REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL.total_seconds(),
            namespace,
            name,
        )
        return
    _check_redis_key_layout_for_cr(body, logger=logger)
