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
from kubernetes.config import ConfigException, load_incluster_config, load_kube_config

from openstudio_operator import singleton
from openstudio_operator.client_factory import get_openstudio_client
from openstudio_operator.config import OperatorConfig
from openstudio_operator.logging_setup import install_json_logging
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
    """In-cluster first, kubeconfig fallback (dev/kind parity with singleton).

    Issue #251 — kept for the prune entrypoint's load-once posture; the
    K8s client factories below (``operator_batch_api``,
    ``operator_core_api``, ``operator_custom_objects_api``) all call the
    same loader internally on first use, so this explicit call is
    redundant for the operator's process lifetime. Retained as a
    pre-#251 audit seam: a maintainer reading the entrypoint should see
    the loader path explicitly, not implicitly behind a factory call.
    """
    try:
        load_incluster_config()
    except ConfigException:
        load_kube_config()


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
    if namespace is None:
        namespace = os.environ.get("POD_NAMESPACE")
    if not namespace:
        logger.error("POD_NAMESPACE is not set — cannot locate the OSCM CR (wiring error)")
        return 2
    namespace = str(namespace)

    if custom_api is None or batch_api is None or core_api is None:
        # Issue #251 — every K8s client is built via the operator's
        # central factory (singleton.operator_*_api()), which loads the
        # in-cluster / kubeconfig fallback and caches the result for the
        # process lifetime. The factory itself calls the kubeconfig
        # loader on first use; the explicit ``_load_kube_config()`` here
        # is retained for the ``ConfigException`` fallback path that
        # the entrypoint still owns (the factories catch the same
        # exception internally, but doing the load once at the top
        # ensures the factories find the kubeconfig already loaded —
        # belt + braces, plus it preserves the historical pre-#251
        # ordering for the CustomObjectsApi construction).
        _load_kube_config()
        custom_api = custom_api if custom_api is not None else operator_custom_objects_api()
        batch_api = batch_api if batch_api is not None else operator_batch_api()
        core_api = core_api if core_api is not None else operator_core_api()

    try:
        crs = _list_crs(custom_api, namespace)
    except (ApiException, ConfigException) as exc:
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
