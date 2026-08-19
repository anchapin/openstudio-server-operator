"""Kubernetes Event emission helper for Kopf-based operator handlers (#164).

Issue #164 consolidates what used to be ``analysis_sla.EventEmitter`` (a
``Callable[[str, str, str], None]`` type alias) into a proper class so every
handler can depend on a single, named module instead of reaching sideways
into a sibling handler module. The class form also lets the dry-run gate
(D11) and the suppressed-event counter live here instead of being
open-coded at every call site.

Construction pattern (one per tick):

* The timer wrapper for each kopf handler creates an :class:`EventEmitter`
  once per tick, passing the OSCM ``body`` (the kwarg kopf delivers to
  every timer/event callback) and ``dry_run=config.dry_run``. The handler
  function then receives the instance as its ``emit=`` parameter and
  calls it as a callable (``emit("Warning", REASON, message)``) — the
  ``__call__`` shim makes that work for both class instances and the
  test-side closures that pre-date this module.
* In production the class is the canonical seam; in tests the existing
  ``def emit(event_type, reason, message) -> None: ...`` closure pattern
  still works (Python doesn't enforce the type annotation) and that is
  the seam those tests have been patching all along.

The class wraps the kopf built-in :func:`kopf.event` which posts an
Event to the kube-apiserver attached to the OSCM CR. Issue #164 also
makes the dry-run substitution mechanism explicit: when
``spec.dryRun`` is set, the class suppresses the kube-apiserver call
and increments :attr:`suppressed_count` instead — the operator still
records that the action WOULD have happened, just without spamming the
apiserver. Tests and metrics can read the counter to distinguish
"no action attempted" from "action attempted but suppressed".
"""

from __future__ import annotations

import logging

import kopf

from openstudio_operator.metrics import (
    EVENTS_DRY_RUN_SUPPRESSED_TOTAL,
    EVENTS_EMIT_FAILURES_TOTAL,
    EVENTS_EMITTED_TOTAL,
)

logger = logging.getLogger(__name__)


class EventEmitter:
    """Emit a Kubernetes Event for an OSCM CR, gated on ``dry_run``.

    The class form replaces the pre-#164 ``Callable[[str, str, str], None]``
    type alias that lived in :mod:`openstudio_operator.handlers.analysis_sla`
    and was imported sideways by every other handler. The class adds:

    * a single ``kopf.event`` chokepoint — every handler routes through
      :meth:`emit`, so the dry-run policy and any future observability
      (e.g. a ``prometheus_client.Counter`` for emitted vs. suppressed)
      can be added in one place;
    * an explicit suppressed-event counter (:attr:`suppressed_count`) for
      dry-run validation — the user can read ``emitter.suppressed_count``
      after a tick to confirm "yes, N events were suppressed", without
      parsing log lines.

    ``__call__`` is provided for backwards compatibility with the
    pre-# idiom ``emit("Warning", REASON, message)``: tests and the
    handler call sites use the call syntax uniformly. New code should
    prefer the explicit :meth:`emit` method.

    Construct with the OSCM body the kopf timer delivers and the active
    ``config.dry_run`` value; both are bound at construction time and
    immutable for the lifetime of the instance (one tick).
    """

    __slots__ = ("_body", "_dry_run", "_suppressed_count")

    def __init__(self, body: dict, *, dry_run: bool = False) -> None:
        """Bind the OSCM body and the dry-run gate for one tick.

        ``body`` is the OSCM CR body kopf hands to every timer/event
        callback — it's what :func:`kopf.event` attaches the Event to.
        ``dry_run`` mirrors :attr:`openstudio_operator.config.OperatorConfig.dry_run`
        and decides whether :meth:`emit` posts the Event or just
        increments the suppressed counter.
        """
        self._body = body
        self._dry_run = dry_run
        self._suppressed_count = 0

    @property
    def suppressed_count(self) -> int:
        """Number of Events suppressed by the dry-run gate since construction.

        Increments each time :meth:`emit` is called with ``dry_run=True``.
        A handler may compare against the count of times it called
        :meth:`emit` to confirm every planned Event was either posted
        (counter unchanged) or recorded as suppressed (counter > 0).
        """
        return self._suppressed_count

    @property
    def dry_run(self) -> bool:
        """Whether this emitter's gate is currently in dry-run mode."""
        return self._dry_run

    def emit(self, event_type: str, reason: str, message: str) -> None:
        """Emit a Kubernetes Event for the OSCM, or record the suppression.

        With ``dry_run=False``: delegates to :func:`kopf.event` — the
        Event is posted to the kube-apiserver attached to ``body``.

        With ``dry_run=True``: increments :attr:`suppressed_count`, logs
        at INFO level (so the suppression is observable in operator
        logs without hitting kube-apiserver), and returns without
        making the cluster call. This is the single dry-run substitution
        site for every OSCM handler — pre-#164 each handler open-coded
        the gate around its ``emit`` callsite.
        """
        if self._dry_run:
            self._suppressed_count += 1
            # Issue #237 — Prometheus surface for the dry-run gate. The
            # counter is incremented at the same site as suppressed_count
            # so a /metrics scrape can prove dry-run mode is firing
            # without parsing logs. Labelled by reason so dashboards can
            # distinguish which handler path the gate intercepted.
            EVENTS_DRY_RUN_SUPPRESSED_TOTAL.labels(reason=reason).inc()
            logger.info(
                "dry-run suppressed %s Event reason=%s message=%r (#164)",
                event_type,
                reason,
                message,
            )
            return
        # Issue #237 — companion emitted counter. Together with the
        # dry-run suppressed counter above, the ratio
        # ``rate(events_emitted_total) / rate(events_dry_run_suppressed_total)``
        # is the headline SLO for an audit-only install.
        EVENTS_EMITTED_TOTAL.labels(reason=reason).inc()
        try:
            kopf.event(self._body, type=event_type, reason=reason, message=message)
        except Exception:  # defensive: increment counter, re-raise unchanged
            # Issue #255 — observability surface for kopf.event posting
            # failures. The handler wrapper would otherwise catch the
            # raised ``ApiException`` via
            # ``handler_tick_failures_total{module,error_type}`` and
            # collapse "Event posting down" into the same counter as
            # "REST API down" (both ``ApiException``). Increment the
            # dedicated counter BEFORE re-raising so a sustained
            # ``ApiException`` storm from the event posting path is its
            # own /metrics signal — labelled by ``reason`` so a dashboard
            # can tell WHICH handler path's Event emission failed.
            # Cardinality matches ``events_emitted_total`` so the failure
            # series can be rate-correlated with the success series.
            EVENTS_EMIT_FAILURES_TOTAL.labels(reason=reason).inc()
            raise

    def __call__(self, event_type: str, reason: str, message: str) -> None:
        """Backwards-compat shim: ``emit("Warning", REASON, msg)`` syntax.

        Every pre-#164 handler call site — and every test-side closure —
        uses ``emit(type, reason, message)`` (a plain callable). Keeping
        that syntax alive lets the class drop in without changing every
        call site; new code should prefer :meth:`emit` directly.
        """
        self.emit(event_type, reason, message)