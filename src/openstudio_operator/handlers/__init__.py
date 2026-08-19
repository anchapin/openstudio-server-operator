"""Kopf handlers, one module per operator component in the plan doc.

Storage pruning/archival is NOT here by design (#78): the retention pipeline
runs as native Job primitives via ``deploy/storage-cronjob.yaml`` +
``openstudio_operator.prune_entrypoint`` (library code in
``openstudio_operator.retention``) — the operator core keeps no storage
polling loop.
"""

import kopf  # used by the Redis-URL-guard on-event handler below.

from openstudio_operator import singleton
from openstudio_operator.handlers import (  # noqa: F401
    analysis_sla,
    datapoint_watchdog,
    web_background_monitor,
    worker_recycler,
)
from openstudio_operator.metrics import start_metrics_server

# Issue #116 — the operator refuses to operate when ``spec.redisUrl`` is
# empty (Redis would carry the historical kind-recipe password otherwise).
# Since kopf.event is only callable from inside a handler / on-event
# callback, the singleton guard's _check routine uses a thread-local
# notification queue that the @kopf.on.event handler below drains. This
# keeps the warning message architecturally identical to the rest of the
# operator's on-CR events (issued via kopf.event under the right event_id).
_NOTIFY_QUEUE: list[tuple[str, str, str]] = []


def _emit_redis_warning_event(*, namespace: str, name: str, message: str) -> None:
    """Called by ``singleton._emit_redis_url_guard_events`` when it finds a
    CR with an empty ``spec.redisUrl``. The actual kopf.event emit happens
    on the next OSCM on-event tick via :func:`_drain_redis_warning_queue` —
    kopf only permits emits from within an active callback, and the
    singleton guard runs from non-event contexts (startup, watchdog
    events on non-OSCM objects).
    """
    _NOTIFY_QUEUE.append((namespace, name, message))


@kopf.on.event("energy.nrel.gov", "v1alpha1", "openstudioclustermanagers")
def _drain_redis_warning_queue(
    name: str, namespace: str, **_kwargs: object
) -> None:
    """Drain any queued Redis-URL-guard Warnings for THIS CR, then clear.

    Counterpart to :func:`_emit_redis_warning_event`. Fires once per
    OSCM watch event; idempotent because :func:`_emit_redis_warning_event`
    only enqueues once per ``(ns, name)`` via the singleton guard's
    cache.
    """
    pending = [msg for msg in _NOTIFY_QUEUE if msg[0] == namespace and msg[1] == name]
    if not pending:
        return
    for _ns, _nm, message in pending:
        kopf.event(
            body={"metadata": {"namespace": namespace, "name": name}},
            type="Warning",
            reason="RedisUrlEmpty",
            message=message,
        )
    _NOTIFY_QUEUE[:] = [
        msg for msg in _NOTIFY_QUEUE if not (msg[0] == namespace and msg[1] == name)
    ]

# Operator startup wiring: ``kopf run --module openstudio_operator.handlers``
# imports this package exactly once, making this import path the operator's
# entrypoint — so the Prometheus /metrics server is started here (issue #17).
# Idempotent and failure-tolerant; see openstudio_operator/metrics.py.
start_metrics_server()

# Singleton gate (issue #14, D05): exactly one OSCM CR per namespace is served
# — the oldest. ``singleton``'s own @kopf.on.event / @kopf.on.startup handlers
# police conflicts (Warning Events + loud logs), and this call centrally wraps
# every OSCM @kopf.timer registered above so all handler modules are gated
# without editing their files. MUST run after the handler modules are imported
# (it is); future handler modules: add them to the import block above and the
# gate picks them up here automatically. Idempotent.
singleton.install_singleton_guard()
