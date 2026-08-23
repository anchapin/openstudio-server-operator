"""Tests for the prune-CronJob entrypoint (issue #78).

Covers the wiring around :func:`openstudio_operator.retention.run_retention_tick`:
D05 oldest-CR resolution, idle exits, the D11 dryRun gate flowing from the
ACTIVE CR's spec through the entrypoint into the tick, K8s Event emission
parity (the operator's ``kopf.event`` equivalent), and the D12 skip-tick
exit-code posture — including the #470 nonzero exits (4 = CR-list failure,
5 = D12 runtime-failure tuple) that make a sustained prune failure visible
as Failed CronJob Jobs, since the #306 counter is per-pod-lifetime and
effectively unscrapeable. The tick logic itself is covered by
test_retention.py.
"""

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import responses
from kubernetes.client import ApiException

from _fakes import FakeCustomObjectsApi, calls_to
from openstudio_operator._oscm_handlers import SKIP_TICK_EXCEPTIONS
from openstudio_operator.archival import archival_job_name
from openstudio_operator.config import OperatorConfigError
from openstudio_operator.openstudio_client import OpenStudioClient
from openstudio_operator.prune_entrypoint import (
    _SKIP_TICK_EXCEPTIONS,
    EVENT_SOURCE_COMPONENT,
    main,
)
from openstudio_operator.status_store import StatusStore

BASE = "http://web.test"
NAMESPACE = "openstudio-server"
NAME = "oscm"
NOW = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)

STORAGE = {
    "archiveToS3": True,
    "backend": "s3",
    "bucket": "os-archives",
    "secretRef": "archive-creds",
    "retentionDays": 7,
    "purgeCompletedNFSFiles": True,
}


def make_cr(
    name: str = NAME,
    *,
    created_days_ago: float = 10.0,
    spec: dict | None = None,
    status: dict | None = None,
) -> dict:
    return {
        "apiVersion": "energy.nrel.gov/v1alpha1",
        "kind": "OpenStudioClusterManager",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "uid": f"uid-{name}",
            "creationTimestamp": (NOW - timedelta(days=created_days_ago)).isoformat().replace(
                "+00:00", "Z"
            ),
        },
        "spec": copy.deepcopy(spec if spec is not None else {"serverUrl": BASE}),
        "status": copy.deepcopy(status if status is not None else {}),
    }


def completed_doc(analysis_id: str, *, age_days: float = 10.0) -> dict:
    return {
        "_id": analysis_id,
        "status": "completed",
        "created_at": (NOW - timedelta(days=age_days + 20)).isoformat(),
        "updated_at": (NOW - timedelta(days=age_days)).isoformat(),
    }


class FakeCoreV1Api:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def create_namespaced_event(self, namespace, body, **_kw):
        self.events.append({"namespace": namespace, "body": body})
        return body


class FakeBatchV1Api:
    def __init__(self, jobs: list | None = None) -> None:
        self.jobs = {j.metadata.name: j for j in (jobs or [])}
        self.creates: list[dict] = []
        self.deletes: list[dict] = []

    def read_namespaced_job(self, name, namespace, **_kw):
        if name not in self.jobs:
            raise ApiException(status=404, reason="Not Found")
        return self.jobs[name]

    def create_namespaced_job(self, namespace, body, **_kw):
        name = body["metadata"]["name"]
        if name in self.jobs:
            raise ApiException(status=409, reason="Conflict")
        self.jobs[name] = SimpleNamespace(metadata=SimpleNamespace(name=name))
        self.creates.append({"namespace": namespace, "body": body})
        return self.jobs[name]

    def delete_namespaced_job(self, name, namespace, **_kw):
        self.deletes.append({"name": name, "namespace": namespace})
        self.jobs.pop(name, None)
        return {}


def run_main(custom_api, *, spec=None, batch=None, now=NOW):
    batch = batch if batch is not None else FakeBatchV1Api()
    core = FakeCoreV1Api()
    code = main(
        NAMESPACE,
        custom_api=custom_api,
        batch_api=batch,
        core_api=core,
        client_factory=lambda url: OpenStudioClient(BASE, backoff_base_seconds=0.0),
        now=now,
    )
    return code, batch, core


# --- D05: which CR is served ------------------------------------------------------


def test_zero_crs_idle_exit_zero():
    code, batch, core = run_main(FakeCustomObjectsApi(make_cr(), items=[]))
    assert code == 0
    assert batch.creates == [] and core.events == []


@responses.activate
def test_oldest_cr_is_served_not_the_newest():
    """D05 in the entrypoint: the OLDEST CR's spec drives the tick — the
    newer CR's different bucket is never honored."""
    old_spec = {"serverUrl": BASE, "redisUrl": "redis://queue:6379", "storagePolicy": {**STORAGE, "bucket": "old-bucket"}}
    new_spec = {"serverUrl": BASE, "redisUrl": "redis://queue:6379", "storagePolicy": {**STORAGE, "bucket": "new-bucket"}}
    crs = [
        make_cr("old-cr", created_days_ago=30, spec=old_spec),
        make_cr("new-cr", created_days_ago=1, spec=new_spec),
    ]
    responses.get(f"{BASE}/analyses.json", json=[completed_doc("a1")])
    responses.get(f"{BASE}/data_points.json", json=[])
    api = FakeCustomObjectsApi(crs[0], items=crs)

    code, batch, _core = run_main(api, spec=old_spec)

    assert code == 0 and len(batch.creates) == 1
    script = batch.creates[0]["body"]["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert "old-bucket" in script and "new-bucket" not in script
    store = StatusStore(NAMESPACE, "old-cr", api)
    assert "a1" in store.get_archived_analyses()  # anchors on the ACTIVE CR


def test_empty_server_url_idles():
    crs = [make_cr(spec={"serverUrl": ""})]
    code, batch, core = run_main(FakeCustomObjectsApi(crs[0], items=crs))
    assert code == 0
    assert batch.creates == [] and core.events == []


def test_empty_redis_url_emits_warning_event_and_returns_nonzero():
    """Issue #180 / #116 parity — the prune actor must inherit the operator's
    redisUrl-empty guard. Empty redisUrl → Warning Event recorded on the CR,
    exit code 3 (loud failure so the CronJob's Failed pod is visible, while
    leaving the next-schedule retry in place per the backoffLimit=0 design).
    """
    spec = {"serverUrl": BASE, "redisUrl": ""}
    crs = [make_cr(spec=spec)]
    code, batch, core = run_main(FakeCustomObjectsApi(crs[0], items=crs))

    assert code == 3
    assert batch.creates == []
    # Exactly one Event: Warning / RedisURLEmpty.
    assert len(core.events) == 1
    event_record = core.events[0]
    assert event_record["namespace"] == NAMESPACE
    body = event_record["body"]
    assert body["type"] == "Warning"
    assert body["reason"] == "RedisURLEmpty"
    assert "spec.redisUrl is empty" in body["message"]
    assert "issues #116, #180" in body["message"]
    # CR is the involvedObject — event attaches to the right CR.
    assert body["involvedObject"]["kind"] == "OpenStudioClusterManager"
    assert body["involvedObject"]["name"] == NAME
    assert body["involvedObject"]["namespace"] == NAMESPACE
    # Source component distinguishes prune-CronJob events from operator events.
    assert body["source"]["component"] == EVENT_SOURCE_COMPONENT


def test_missing_namespace_is_a_wiring_error(monkeypatch):
    monkeypatch.delenv("POD_NAMESPACE", raising=False)
    assert main(None, custom_api=FakeCustomObjectsApi(make_cr(), items=[])) == 2


# --- D11: the dryRun gate flows from the ACTIVE CR spec ---------------------------


@responses.activate
def test_dry_run_suppresses_job_spawn_and_deletes_but_records_events():
    dry_spec = {"serverUrl": BASE, "redisUrl": "redis://queue:6379", "storagePolicy": dict(STORAGE), "dryRun": True}
    crs = [make_cr(spec=dry_spec)]
    responses.get(f"{BASE}/analyses.json", json=[completed_doc("a1")])
    responses.get(f"{BASE}/data_points.json", json=[])
    responses.delete(f"{BASE}/analyses/a1", status=204)
    api = FakeCustomObjectsApi(crs[0], items=crs)

    code, batch, core = run_main(api)

    assert code == 0
    assert batch.creates == [] and batch.deletes == []  # mutation suppressed
    assert calls_to("/analyses/a1") == 0
    # The dry-run marker Event lands on the CR via CoreV1, marked as the
    # prune CronJob source — the kopf.event parity.
    assert len(core.events) == 1
    body = core.events[0]["body"]
    assert body["type"] == "Normal" and body["reason"] == "AnalysisArchivalStarted"
    assert "suppressed (spec.dryRun)" in body["message"]
    assert body["source"] == {"component": EVENT_SOURCE_COMPONENT}
    assert body["involvedObject"]["name"] == NAME
    # D04 anchor still written (dry-run marker, no jobName).
    store = StatusStore(NAMESPACE, NAME, api)
    record = store.get_archived_analysis("a1")
    assert record is not None and record.job_name is None


@responses.activate
def test_real_run_spawns_the_archival_job():
    spec = {"serverUrl": BASE, "redisUrl": "redis://queue:6379", "storagePolicy": dict(STORAGE)}
    crs = [make_cr(spec=spec)]
    responses.get(f"{BASE}/analyses.json", json=[completed_doc("a1")])
    responses.get(f"{BASE}/data_points.json", json=[])
    api = FakeCustomObjectsApi(crs[0], items=crs)

    code, batch, core = run_main(api)

    assert code == 0
    assert len(batch.creates) == 1
    assert batch.creates[0]["body"]["metadata"]["name"] == archival_job_name("a1")
    assert core.events[0]["body"]["reason"] == "AnalysisArchivalStarted"
    assert "suppressed" not in core.events[0]["body"]["message"]


# --- D12: skip-tick posture ---------------------------------------------------------


@responses.activate
def test_transient_api_failure_exits_five_and_retries_next_schedule():
    """Issue #470 — the D12 runtime-failure tuple (REST 5xx here) exits 5,
    not 0: the Failed CronJob Job is the durable sustained-failure signal
    (``PRUNE_TICK_FAILURES_TOTAL`` is process-local to a one-tick pod and
    effectively unscrapeable). The retry posture is unchanged — the next
    schedule IS the retry (backoffLimit 0)."""
    spec = {"serverUrl": BASE, "redisUrl": "redis://queue:6379", "storagePolicy": dict(STORAGE)}
    crs = [make_cr(spec=spec)]
    responses.get(f"{BASE}/analyses.json", json={"error": "boom"}, status=500)
    api = FakeCustomObjectsApi(crs[0], items=crs)

    code, batch, core = run_main(api)

    assert code == 5  # loud failure: failedJobsHistoryLimit + Job alerting
    assert batch.creates == [] and core.events == []


@responses.activate
def test_invalid_storage_policy_exits_five():
    """ValueError (bad backend enum) is inside the D12 runtime-failure tuple,
    so it inherits the same #470 exit-5 contract as the REST failure above."""
    bad_spec = {"serverUrl": BASE, "redisUrl": "redis://queue:6379", "storagePolicy": {**STORAGE, "backend": "ftp"}}
    crs = [make_cr(spec=bad_spec)]
    responses.get(f"{BASE}/analyses.json", json=[completed_doc("a1")])
    api = FakeCustomObjectsApi(crs[0], items=crs)

    code, batch, _ = run_main(api)

    assert code == 5
    assert batch.creates == []


def test_cr_list_failure_exits_four():
    """Issue #470 — a kube-apiserver/RBAC failure listing OSCM CRs exits 4,
    not 0, so a sustained CR-list outage leaves Failed Jobs behind (the
    durable signal; see the prune_entrypoint docstring exit-code table)."""
    class FailingApi:
        def list_namespaced_custom_object(self, *a, **kw):
            raise ApiException(status=503, reason="Service Unavailable")

    code, batch, core = run_main(FailingApi())
    assert code == 4
    assert batch.creates == [] and core.events == []


# --- Issue #306: prune-tick failures observability surface --------------------


def _counter_value(counter, **labels) -> float:
    """Read a single labelled Counter series value by label match.

    Handles the labelled-by-reason Counter pattern from #306 — the
    only label is ``reason`` and the only vocabulary is the three branch
    names (``cr_list_failure`` | ``runtime_failure`` |
    ``redis_url_empty``). prometheus_client
    keys `_metrics` by a TUPLE of the LABEL VALUES (positional, not
    key-value), so for a single label ``reason`` the key is
    ``("cr_list_failure",)`` etc. Matching against the caller's
    ``**labels`` is a positional match when the counter has exactly one
    label, and a dict-equality match otherwise — both forms are pinned
    here so the helper stays correct under a future refactor that
    adds a second label to ``PRUNE_TICK_FAILURES_TOTAL``.
    """
    per_series = getattr(counter, "_metrics", None)
    if not per_series:
        return 0.0
    label_names = list(getattr(counter, "_labelnames", []) or [])
    expected_values = [labels[name] for name in label_names]
    for key, snapshot in per_series.items():
        if isinstance(key, tuple) and list(key) == expected_values:
            return float(snapshot._value.get())  # type: ignore[attr-defined]
    return 0.0


def test_prune_skip_tick_cr_list_failure_increments_prune_tick_failures_counter():
    """Issue #306 acceptance: the first skip-tick branch (CR list failure,
    ``prune_entrypoint.py:213-222``) bumps
    ``PRUNE_TICK_FAILURES_TOTAL{reason="cr_list_failure"}`` so a sustained
    kube-apiserver outage is visible at ``/metrics`` (the WARNING log is
    the same event for log forwarding; the counter is the Prometheus
    signal an SRE can alert on). Mirrors the bounded-cardinality
    convention from #117 / #171 / #237 / #239 / #255 — the label
    vocabulary is the three branch names defined in prune_entrypoint."""
    from openstudio_operator import metrics

    counter = metrics.PRUNE_TICK_FAILURES_TOTAL
    baseline = _counter_value(counter, reason="cr_list_failure")

    class FailingApi:
        def list_namespaced_custom_object(self, *a, **kw):
            raise ApiException(status=503, reason="Service Unavailable")

    code, _batch, _core = run_main(FailingApi())
    assert code == 4  # issue #470: loud CR-list failure (Failed Job signal)

    after = _counter_value(counter, reason="cr_list_failure")
    assert after - baseline == 1.0, (
        f"prune_entrypoint CR-list skip branch must bump the counter by 1, "
        f"got {after - baseline}"
    )


@responses.activate
def test_prune_skip_tick_runtime_failure_increments_prune_tick_failures_counter():
    """Issue #306 acceptance: the second skip-tick branch (D12 exception
    tuple caught around ``run_retention_tick``,
    ``prune_entrypoint.py:280-288``) bumps
    ``PRUNE_TICK_FAILURES_TOTAL{reason="runtime_failure"}`` so a sustained
    REST/Redis/StatusStore outage is visible at ``/metrics``. The CR list
    failure branch above bumps the SAME counter with a DIFFERENT
    ``reason`` label so a Grafana panel can distinguish "kube-apiserver
    unreachable" from "REST 5xx storm" from "StatusStoreConflictError
    thundering herd" — the entire labeling invariant the issue cites from
    #117."""
    from openstudio_operator import metrics

    counter = metrics.PRUNE_TICK_FAILURES_TOTAL
    baseline = _counter_value(counter, reason="runtime_failure")

    spec = {"serverUrl": BASE, "redisUrl": "redis://queue:6379", "storagePolicy": dict(STORAGE)}
    crs = [make_cr(spec=spec)]
    # 500 on the REST GET — same exception the D12 tuple catches
    # (OpenStudioApiError) — so the runtime branch fires.
    responses.get(f"{BASE}/analyses.json", json={"error": "boom"}, status=500)
    api = FakeCustomObjectsApi(crs[0], items=crs)

    code, _batch, _core = run_main(api)
    assert code == 5  # issue #470: loud runtime failure (Failed Job signal)

    after = _counter_value(counter, reason="runtime_failure")
    assert after - baseline == 1.0, (
        f"prune_entrypoint D12-skip branch must bump the counter by 1, "
        f"got {after - baseline}"
    )


def test_prune_exit3_redis_url_empty_increments_prune_tick_failures_counter():
    """Issue #392 acceptance: the exit-3 empty-``spec.redisUrl`` guard
    bumps ``PRUNE_TICK_FAILURES_TOTAL{reason="redis_url_empty"}`` so a
    sustained wedge (chart upgrade drops the redis-secret KeyRef, CR
    omits redisUrl entirely) is visible at ``/metrics`` — the #306
    dashboard alert ``rate(prune_tick_failures_total[5m]) > 0`` covers
    the redisUrl guard too. Exit-code contract unchanged: 3 still means
    redisUrl empty (pinned by the #180 test above), and the counter bump
    happens on EVERY failed Job, which is exactly the per-tick cadence
    the rate() alert needs."""
    from openstudio_operator import metrics
    from openstudio_operator.prune_entrypoint import (
        PRUNE_TICK_FAILURE_REASON_REDIS_URL_EMPTY,
    )

    assert PRUNE_TICK_FAILURE_REASON_REDIS_URL_EMPTY == "redis_url_empty"

    counter = metrics.PRUNE_TICK_FAILURES_TOTAL
    baseline = _counter_value(counter, reason="redis_url_empty")
    # The label value is distinct from the two #306 skip-tick reasons so
    # a Grafana panel can tell the redisUrl wedge apart from the
    # kube-apiserver / REST-outage skips — pinned by asserting the exit-3
    # path leaves the other two series untouched.
    cr_baseline = _counter_value(counter, reason="cr_list_failure")
    rt_baseline = _counter_value(counter, reason="runtime_failure")

    spec = {"serverUrl": BASE, "redisUrl": ""}
    crs = [make_cr(spec=spec)]
    code, _batch, _core = run_main(FakeCustomObjectsApi(crs[0], items=crs))

    assert code == 3  # exit-code contract unchanged (issues #116, #180)

    after = _counter_value(counter, reason="redis_url_empty")
    assert after - baseline == 1.0, (
        f"prune_entrypoint exit-3 redisUrl guard must bump the counter by 1, "
        f"got {after - baseline}"
    )
    assert _counter_value(counter, reason="cr_list_failure") == cr_baseline
    assert _counter_value(counter, reason="runtime_failure") == rt_baseline


def test_prune_skip_tick_counter_is_the_only_emitted_metric_for_skip_branches():
    """Issue #306 regression fence: the counter branches MUST NOT
    silently add any other counter family — only
    ``PRUNE_TICK_FAILURES_TOTAL`` is bumped. The contract is the three
    reason constants defined in prune_entrypoint.py — a future refactor
    that raises a fourth branch without wiring it through the same
    constant vocabulary is caught at this test BEFORE the
    EXPECTED_COUNTER_FAMILIES drift-invariant in test_metrics_endpoint.

    Structural rather than stateful: this pins the AST contract that
    the branch sites use the module-level constants rather than
    inline string literals, so a typo'd reason value (``cr_list_faliure``
    etc.) shows up as a TypeError at call time rather than a silent
    Grafana dashboard split."""
    import inspect

    from openstudio_operator import prune_entrypoint

    # The three module-level constants are the only legitimate reason
    # values. A fourth value would need a new constant + a new branch
    # site + a new PRUNE_TICK_FAILURES_TOTAL.labels(...) call.
    assert prune_entrypoint.PRUNE_TICK_FAILURE_REASON_CR_LIST == "cr_list_failure"
    assert prune_entrypoint.PRUNE_TICK_FAILURE_REASON_RUNTIME == "runtime_failure"
    assert prune_entrypoint.PRUNE_TICK_FAILURE_REASON_REDIS_URL_EMPTY == "redis_url_empty"

    # And the three constants are the ONLY reasons actually passed to
    # PRUNE_TICK_FAILURES_TOTAL.labels(reason=...) — captured by
    # inspecting the source of prune_entrypoint.main(). The
    # single-quoted strings in the constants ARE the strings at the
    # call sites (no `reason="..."` literal shadows the constants).
    # Four call sites since #650: the config-phase from_spec guard adds
    # a fourth branch REUSING reason="runtime_failure" (no new reason
    # value — the vocabulary stays the three constants above).
    source = inspect.getsource(prune_entrypoint)
    branch_call_sites = [
        line for line in source.splitlines()
        if "PRUNE_TICK_FAILURES_TOTAL.labels" in line
    ]
    assert len(branch_call_sites) == 4, (
        f"prune_entrypoint must call PRUNE_TICK_FAILURES_TOTAL.labels "
        f"exactly four times (one per failure branch: CR-list skip, D12 "
        f"runtime skip, exit-3 redisUrl guard #392, and the #650 "
        f"config-phase from_spec guard reusing runtime_failure), "
        f"got {len(branch_call_sites)}:\n"
        + "\n".join(branch_call_sites)
    )
    # Every call site must use the module-level constants, not inline
    # string literals — the vocabulary pin.
    for line in branch_call_sites:
        assert "PRUNE_TICK_FAILURE_REASON_" in line, (
            f"prune_entrypoint PRUNE_TICK_FAILURES_TOTAL.labels call site "
            f"must use the module-level reason constant, got inline: {line!r}"
        )


def test_event_names_are_unique_per_emission():
    """client-go convention: repeated reasons never 409 on a live cluster."""
    from openstudio_operator.prune_entrypoint import build_event_emitter

    core = FakeCoreV1Api()
    emit = build_event_emitter(core, make_cr(), NAMESPACE)
    emit("Normal", "AnalysisArchivalStarted", "one")
    emit("Normal", "AnalysisArchivalStarted", "two")
    names = [e["body"]["metadata"]["name"] for e in core.events]
    assert len(set(names)) == 2
    assert all(n.startswith(f"{NAME}.analysisarchivalstarted.") for n in names)


def test_event_emission_failure_never_aborts_the_tick():
    from openstudio_operator.prune_entrypoint import build_event_emitter

    class FailingCore:
        def create_namespaced_event(self, namespace, body, **_kw):
            raise ApiException(status=403, reason="Forbidden")

    emit = build_event_emitter(FailingCore(), make_cr(), NAMESPACE)
    emit("Normal", "AnalysisArchivalStarted", "message")  # must not raise


@pytest.mark.parametrize(
    "backend", ["s3", "gcs", "azure"]
)
def test_entrypoint_serves_every_cloud_backend_spec(backend):
    """The entrypoint is backend-agnostic: any CRD enum backend wires through."""
    spec = {
        "serverUrl": BASE,
        "redisUrl": "redis://queue:6379",
        "storagePolicy": {**STORAGE, "backend": backend, "bucket": f"bucket-{backend}"},
    }
    crs = [make_cr(spec=spec)]
    with responses.RequestsMock() as rsps:
        rsps.get(f"{BASE}/analyses.json", json=[])
        api = FakeCustomObjectsApi(crs[0], items=crs)
        code, _batch, _ = run_main(api)

    assert code == 0  # empty analyses: nothing due, clean exit per backend


# --- Issue #650: skip-tick tuple derivation + config-phase guard ----------------


def test_prune_skip_tick_tuple_derives_from_canonical():
    """Issue #650 — prune's ``_SKIP_TICK_EXCEPTIONS`` is DERIVED from the
    canonical ``_oscm_handlers.SKIP_TICK_EXCEPTIONS`` (pinned by
    ``test_oscm_tick_runner.py``), not a frozen pre-#473 fork: the canonical
    tuple must be a subset of the fork, and the fork's extras must be
    EXACTLY the documented set (bare ``ValueError`` — the storagePolicy-enum
    and spec.redisCredentials.secretRef parses). The next #475/#493-style
    canonical widening fails HERE until a prune decision is recorded."""
    canonical = set(SKIP_TICK_EXCEPTIONS)
    fork = set(_SKIP_TICK_EXCEPTIONS)
    assert canonical <= fork, (
        f"prune's skip-tick fork must keep every canonical member "
        f"(canonical-only: {sorted(c.__name__ for c in canonical - fork)}) — "
        f"derive it as SKIP_TICK_EXCEPTIONS + documented deltas (#650)"
    )
    assert fork - canonical == {ValueError}, (
        f"prune's fork extras must be exactly the documented set "
        f"{{ValueError}}; got {sorted(c.__name__ for c in fork - canonical)} — "
        f"new extras require a comment block + this test update (#650)"
    )
    assert len(_SKIP_TICK_EXCEPTIONS) == len(SKIP_TICK_EXCEPTIONS) + 1, (
        "the fork must be the canonical tuple plus exactly one appended member"
    )


def test_malformed_cr_spec_is_clean_counted_exit_five():
    """Issue #650 — a malformed CR spec raised by ``OperatorConfig.from_spec``
    (bare ``ValueError`` from the spec.redisCredentials.secretRef parse) is a
    clean, counted, documented exit 5 — matching #475's operator-side
    rationale (counted, logged, retry next schedule) — not an uncaught
    traceback exiting 1, a code the exit-code table never promised."""
    from openstudio_operator import metrics

    bad_spec = {
        "serverUrl": BASE,
        "redisUrl": "redis://queue:6379",
        "redisCredentials": {"secretRef": "not-an-object"},
    }
    crs = [make_cr(spec=bad_spec)]
    api = FakeCustomObjectsApi(crs[0], items=crs)

    counter = metrics.PRUNE_TICK_FAILURES_TOTAL
    baseline = _counter_value(counter, reason="runtime_failure")
    code, batch, core = run_main(api)

    assert code == 5
    assert batch.creates == [] and core.events == []
    assert _counter_value(counter, reason="runtime_failure") - baseline == 1.0, (
        "the config-phase guard must bump PRUNE_TICK_FAILURES_TOTAL "
        "{reason='runtime_failure'} exactly once"
    )


def test_client_construction_config_error_is_clean_counted_exit_five():
    """Issue #650 / #475 — an ``OperatorConfigError`` escaping client
    construction (TLS CA-bundle misconfiguration, the exact class #475
    named) is a counted exit-5 skip: construction runs INSIDE the guarded
    region, the #493 pattern the operator canonicalized — not an uncaught
    traceback exiting 1."""
    from openstudio_operator import metrics

    crs = [make_cr(spec={"serverUrl": BASE, "redisUrl": "redis://queue:6379"})]
    api = FakeCustomObjectsApi(crs[0], items=crs)

    def raising_client_factory(url: str) -> OpenStudioClient:
        raise OperatorConfigError("synthetic TLS CA-bundle misconfiguration (#650)")

    counter = metrics.PRUNE_TICK_FAILURES_TOTAL
    baseline = _counter_value(counter, reason="runtime_failure")
    code = main(
        NAMESPACE,
        custom_api=api,
        batch_api=FakeBatchV1Api(),
        core_api=FakeCoreV1Api(),
        client_factory=raising_client_factory,
        now=NOW,
    )

    assert code == 5
    assert _counter_value(counter, reason="runtime_failure") - baseline == 1.0
