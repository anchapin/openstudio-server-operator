"""Prune-CronJob entrypoint — runs ONE retention tick, then exits (issue #78).

Executed by the CronJob in ``deploy/storage-cronjob.yaml`` as::

    python -m openstudio_operator.prune_entrypoint

This is the actor that owns the storage pipeline since #78 externalized it
out of the operator core process (the operator keeps no storage polling
loop; its Module 1 SLA poll remains the one light completion watch, D04/
D12). One CronJob run == one :func:`openstudio_operator.retention.run_retention_tick`
against the ACTIVE CR — the pipeline logic itself is untouched library code.

D05 preserved: the entrypoint serves only the OLDEST OSCM CR in the
namespace (:func:`openstudio_operator.singleton.resolve_active_cr`) — the
same oldest-wins rule the operator's guard enforces, so a loser CR's
storagePolicy is never honored by either actor.

D11 preserved: config is built from the ACTIVE CR's ``spec`` via
:meth:`OperatorConfig.from_spec`, so ``spec.dryRun`` gates every mutation
inside the tick (archival Job spawn, failed-Job cleanup delete, analysis
DELETE) exactly as it did in the operator timer. Status anchors (D04) are
deliberately NOT gated — they are the operator memory that makes the state
machine idempotent, the same exemption the audit grants the operator.

Events: the operator emitted via ``kopf.event``; here a minimal CoreV1
Events emitter attaches the same ``(type, reason, message)`` triples to the
CR object, so dry-run markers and lifecycle Events remain observable on the
CR for both actors.

Exit codes (the CronJob's own backoffLimit is 0 — a failed pod is not
retried in place; the next schedule IS the retry):

* ``0`` — tick ran, or was skipped idly (no CR / no serverUrl / archiveToS3
  false / D12 transient failure logged as a warning). Skip-tick parity with
  the old kopf wrapper, which logged and returned.
* ``2`` — wiring error (no POD_NAMESPACE, unparseable cluster state) — loud,
  surfaces as a Failed CronJob Job.
* unexpected exceptions propagate (non-zero) — visible, retried next run.

RBAC: the entrypoint's ServiceAccount (``deploy/storage-cronjob.yaml``)
holds only what one tick needs — OSCM list + status get/patch, batch jobs
get/create/delete, events create. No Deployments, no pods, no HPA, no
secrets, no NFS mounts: the archival Jobs it spawns carry their own
credentials (envFrom) and volume mounts.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from kubernetes.client import ApiException, BatchV1Api
from kubernetes.config import ConfigException

from openstudio_operator import singleton
from openstudio_operator._k8s import load_operator_kube_config
from openstudio_operator.client_factory import get_openstudio_client
from openstudio_operator.config import OperatorConfig
from openstudio_operator.logging_setup import install_json_logging
from openstudio_operator.metrics import PRUNE_TICK_FAILURES_TOTAL, start_metrics_server
from openstudio_operator.openstudio_client import OpenStudioApiError, OpenStudioClient
from openstudio_operator.retention import run_retention_tick
from openstudio_operator.singleton import (
    operator_batch_api,
    operator_core_api,
    operator_custom_objects_api,
)
from openstudio_operator.status_store import GROUP, PLURAL, VERSION, StatusStore, StatusStoreError

logger = logging.getLogger(__name__)

#: Event source component recorded on Events this entrypoint creates —
#: distinguishes prune-CronJob events from operator (kopf) events.
EVENT_SOURCE_COMPONENT = "openstudio-storage-pruner"

#: D12 skip-tick posture: same exception tuple the old kopf wrapper caught.
_SKIP_TICK_EXCEPTIONS = (OpenStudioApiError, StatusStoreError, ApiException, ValueError)

#: Issue #306 — ``reason`` label values for :data:`PRUNE_TICK_FAILURES_TOTAL`.
#: One per skip-tick branch in :func:`main`. The exception class name is
#: already in the WARNING log line via ``type(exc).__name__``; keeping the
#: label vocabulary bounded to the two branch names mirrors the bounded
#: cardinality convention from #117 / #171 / #237 / #239 / #255.
PRUNE_TICK_FAILURE_REASON_CR_LIST = "cr_list_failure"
PRUNE_TICK_FAILURE_REASON_RUNTIME = "runtime_failure"


class CoreApi(Protocol):
    """Structural type of ``CoreV1Api`` as used here — tests fake exactly this."""

    def create_namespaced_event(self, namespace: str, body: dict, **_: object) -> object: ...


class CustomApi(Protocol):
    """Structural type of ``CustomObjectsApi`` as used here."""

    def list_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, **_: object
    ) -> dict: ...


def build_event_emitter(
    core_api: CoreApi, cr: dict, namespace: str
) -> Callable[[str, str, str], None]:
    """``(type, reason, message)`` sink creating Events on the CR object.

    The operator's handlers emit via ``kopf.event``; this is the standalone
    equivalent (``create_namespaced_event`` with the CR as the
    ``involvedObject``). Failures to record an Event are logged and swallowed
    — an observability write must never abort a retention tick.
    """
    meta = cr.get("metadata") or {}
    involved = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "OpenStudioClusterManager",
        "name": str(meta.get("name") or ""),
        "namespace": namespace,
    }
    if meta.get("uid"):
        involved["uid"] = str(meta["uid"])

    def emit(event_type: str, reason: str, message: str) -> None:
        event = {
            "apiVersion": "v1",
            "kind": "Event",
            # client-go convention: unique name per emission, so repeats of
            # the same reason never 409 while earlier Events still exist.
            "metadata": {
                "name": f"{involved['name']}.{reason.lower()}.{time.time_ns()}",
                "namespace": namespace,
            },
            "involvedObject": involved,
            "type": event_type,
            "reason": reason,
            "message": message,
            "source": {"component": EVENT_SOURCE_COMPONENT},
            "firstTimestamp": datetime.now(UTC).isoformat(),
            "lastTimestamp": datetime.now(UTC).isoformat(),
            "count": 1,
        }
        try:
            core_api.create_namespaced_event(namespace, event)
        except ApiException as exc:  # observability only — never fatal
            logger.warning("could not record %s Event (%s): %s", reason, exc.status, exc.reason)

    return emit


def _load_kube_config() -> None:
    """Thin delegation to the single public loader (issue #305).

    The in-cluster / ``kube_config`` fallback loader this function used
    to host inline is now :func:`openstudio_operator._k8s.load_operator_kube_config`
    — the SINGLE public loader for every K8s client the operator
    builds. The companion ``_load_k8s_config`` in
    :mod:`openstudio_operator.singleton` was a verbatim copy of the
    same try/except; a future loader change (kubeconfig Secret
    reference, network-proxy client, custom CA bundle) would have had
    to land in two places. The wrapper is preserved only because the
    entrypoint's :func:`main` calls it explicitly (the redundant
    pre-factory load that avoids the ``ConfigException`` fallback path
    in environments where ``load_kube_config`` succeeds), and the
    visible call site documents that intent. The AST gate in
    ``tests/test_singleton_registry_coverage.py::test_only_one_kubeconfig_loader_call_site``
    rejects any inline ``load_incluster_config(`` /
    ``load_kube_config(`` call outside ``_k8s.py`` so a partial update
    fails CI loudly.
    """
    load_operator_kube_config()


def _list_crs(custom_api: CustomApi, namespace: str) -> list[dict]:
    resp = custom_api.list_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL)
    items = resp.get("items") or []
    return [item for item in items if isinstance(item, dict)]


def main(
    namespace: str | None = None,
    *,
    custom_api: CustomApi | None = None,
    batch_api: BatchV1Api | None = None,
    core_api: CoreApi | None = None,
    client_factory: Callable[[str], OpenStudioClient] | None = None,
    now: datetime | None = None,
) -> int:
    """Run one retention tick as the active CR's storage-prune actor.

    Dependency-injection seams (``*_api``/``client_factory``/``now``) exist
    for tests; production wiring builds real clients from in-cluster config.
    Returns the process exit code (see module docstring).
    """
    # Issue #256 — install the structured JSON log formatter before any
    # tick code emits. Idempotent on the root logger; safe to call even
    # when the test harness has already installed a different formatter
    # (the sentinel attribute guards against double-install). The
    # previous ``logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s")``
    # emitted unstructured text that broke Loki/CloudWatch structured
    # queries; this is the replacement.
    install_json_logging()
    # Issue #306 — serve /metrics on the conventional Prometheus port
    # (METRICS_PORT, 9090). The function is idempotent (the first call
    # binds the port; subsequent calls return the already-active port
    # without a second bind), so repeated invocations between production
    # ticks and unit tests are safe — in this isolated CronJob pod the
    # port is guaranteed free on first boot, and the WARN log on bind
    # failure is the only signal if a sibling process is already on it.
    # Placement: right after the JSON log formatter is installed so any
    # failure-to-bind warning is itself JSON-structured for Loki/CloudWatch.
    start_metrics_server()
    if namespace is None:
        namespace = os.environ.get("POD_NAMESPACE")
    if not namespace:
        logger.error("POD_NAMESPACE is not set — cannot locate the OSCM CR (wiring error)")
        return 2
    namespace = str(namespace)

    if custom_api is None or batch_api is None or core_api is None:
        # Issue #251 + #305 — every K8s client is built via the operator's
        # central factory (singleton.operator_*_api()), which loads the
        # in-cluster / kubeconfig fallback and caches the result for the
        # process lifetime. The factory itself calls the SINGLE public
        # loader (:func:`openstudio_operator._k8s.load_operator_kube_config`)
        # on first use; the explicit ``_load_kube_config()`` here is a thin
        # delegation to that same loader, so the factories find the
        # kubeconfig already loaded and the ``ConfigException`` warning
        # path inside the factory (the placeholder-client branch) is
        # avoided in environments where ``load_kube_config`` succeeds.
        _load_kube_config()
        custom_api = custom_api if custom_api is not None else operator_custom_objects_api()
        batch_api = batch_api if batch_api is not None else operator_batch_api()
        core_api = core_api if core_api is not None else operator_core_api()

    try:
        crs = _list_crs(custom_api, namespace)
    except (ApiException, ConfigException) as exc:
        # Issue #306 — bump the skip-tick counter so the sustained degraded
        # window is visible at /metrics. The WARNING log line is the same
        # event for log forwarding; the counter is the Prometheus signal an
        # SRE can alert on. The exception class name is preserved in the
        # log line via ``type(exc).__name__``; the label vocabulary stays
        # bounded to the two branch names defined above.
        PRUNE_TICK_FAILURES_TOTAL.labels(reason=PRUNE_TICK_FAILURE_REASON_CR_LIST).inc()
        logger.warning(
            "prune tick skipped, retrying next schedule — could not list OSCM CRs "
            "(%s: %s)",
            type(exc).__name__,
            exc,
        )
        return 0

    cr = singleton.resolve_active_cr(crs)  # D05: oldest CR wins, same rule as the operator
    if cr is None:
        logger.info("no OpenStudioClusterManager CRs in %s — prune tick idle (D05)", namespace)
        return 0
    name = str((cr.get("metadata") or {}).get("name") or "")
    spec = cr.get("spec") or {}

    config = OperatorConfig.from_spec(spec)
    if not config.server_url:
        logger.warning("spec.serverUrl is empty on %s — prune tick idle", name)
        return 0
    # Build the Event emitter early so the redisUrl guard below can use it
    # (issue #180). The emitter records on the CR via create_namespaced_event
    # and is observability-only — its failures never abort the tick (see
    # build_event_emitter's emit() closure).
    emit = build_event_emitter(core_api, cr, namespace)
    # Issue #180 — inherit the #116 redisUrl-empty guard. The operator process
    # emits a Warning Event per CR (singleton.py:_emit_redis_url_guard_events);
    # the prune actor is a separate process that shares the same Redis and the
    # same retention pipeline, so it must enforce the same fence. Without this
    # guard a misconfigured `spec.redisUrl` would let the prune tick fall back
    # to in-cluster defaults and silently skip the queue-aware steps in
    # run_retention_tick. We return non-zero so the CronJob logs the failure
    # visibly (the next schedule IS the retry — the CronJob's backoffLimit=0
    # means a Failed pod is not retried in place).
    if not config.redis_url:
        emit(
            "Warning",
            "RedisURLEmpty",
            (
                f"spec.redisUrl is empty on {namespace}/{name} (issues #116, #180). "
                "Storage prune actor is a no-op until you set this field "
                "explicitly. The previous default exposed the kind-recipe "
                "password `openstudio` and has been removed; helm-chart users "
                "should derive the URL from the Redis-secret KeyRef."
            ),
        )
        logger.warning(
            "OSCM %s/%s has empty spec.redisUrl — issue #180: prune actor is a no-op",
            namespace, name,
        )
        return 3

    store = StatusStore(namespace, name, custom_api)  # type: ignore[arg-type]
    client = (client_factory or get_openstudio_client)(config.server_url)

    try:
        result = run_retention_tick(
            client,
            store,
            config,
            now=now if now is not None else datetime.now(UTC),
            emit=emit,
            namespace=namespace,
            batch_api=batch_api,
        )
    except _SKIP_TICK_EXCEPTIONS as exc:
        # ValueError: invalid storagePolicy (e.g. bad backend enum) — same
        # skip-tick posture as the old kopf wrapper; next schedule retries (D12).
        # Issue #306 — bump the skip-tick counter for the Prometheus signal
        # (the WARNING log line is the same event for log forwarding).
        PRUNE_TICK_FAILURES_TOTAL.labels(reason=PRUNE_TICK_FAILURE_REASON_RUNTIME).inc()
        logger.warning(
            "prune tick skipped, retrying next schedule (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return 0

    if result.deleted:
        logger.info(
            "prune tick verified %d and deleted %d analysis(es)",
            len(result.verified),
            len(result.deleted),
        )
    elif result.verified:
        logger.info("prune tick verified %d archival Job(s) (delete withheld)", len(result.verified))
    elif result.spawned:
        logger.info("prune tick spawned %d archival Job(s)", len(result.spawned))
    return 0


if __name__ == "__main__":  # pragma: no cover (thin process boundary)
    raise SystemExit(main())
