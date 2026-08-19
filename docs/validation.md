# Work-cluster validation runbook (issue #20, decision D13)

The second half of D13. The dev-machine half — kind cluster, real `3.11.0`
images, REST fixture capture, and the Phase-1 `dryRun: true` smoke — lives in
[docs/kind-validation.md](kind-validation.md) (issue #19); this runbook
**builds on it and does not repeat it**. Read it first, especially the
[drift findings](kind-validation.md#drift-findings-live-verified-on-kind-2026-08-18-stack-3110):
every verification curl below is written against those live-verified quirks.

What kind could not prove gets proven here, on the work cluster that runs the
real helm deployment (NatLabRockies chart, `develop` branch, namespace
`openstudio-server`, server images pinned `3.11.0`):

- a **real NFS provisioner** behind `nfs-pvc` (kind used a hostPath
  stand-in — see [Approximations](kind-validation.md#approximations-vs-production)),
- **real KEDA ScaledObject dynamics** on `keda-hpa-worker` (kind has no
  metrics-server, so KEDA cannot activate a Redis-scaler query there —
  Scale-up/Scale-down probes need the work cluster's Prometheus stack),
  replacing the chart's `worker-hpa` HPA which is deleted in
  [Prerequisites](#prerequisites-cluster-installed-outside-this-operator),
  then verified extinct in [Phase D](#phase-d--flip-dryrun-false)'s
  negative-control step,
- mutating actions (`soft_stop`, `requeue`, `DELETE`) against analyses that
  matter — under `dryRun` first, then for real on throwaway analyses only.

## Module status (post-#77/#78; column updated after wave-3 merges)

The table below reflects the handlers that are loaded by `handlers/__init__.py`
on every operator boot and the manifests actually shipped under `deploy/`.
Earlier revisions of this runbook described modules 2/3/5 as "stubs" and
referenced the deleted `handlers/storage_pruner.py`; those lines predate the
#10/#11/#13/#78 merges and are no longer accurate.

| Module | Code path | State | Notes |
|---|---|---|---|
| 1 — Analysis SLA soft-stop | `src/openstudio_operator/handlers/analysis_sla.py` | **live** | SLA clock + escalation re-sourced to verified contract in #83/#96; LEGACY escalation seam trim tracked in #105 |
| 2 — Zombie datapoint watchdog | `src/openstudio_operator/handlers/datapoint_watchdog.py` | **live** | Requeue path for `started → jobless` datapoints |
| 3 — Worker recycler | `src/openstudio_operator/handlers/worker_recycler.py` | **live** | Idle Resque fence + pod-delete recycle; operator Surface-trim tracked in #104 |
| 4 — NFS archival + prune | `src/openstudio_operator/archival.py` (Job generator, in-process) → `src/openstudio_operator/retention.py` + `src/openstudio_operator/prune_entrypoint.py` (CronJob entrypoint) | **live** | Prune orchestration moved from operator to a dedicated CronJob in #78; operator Role lost `batch/jobs`. Live-cloud scheduling validation tracked in #101 |
| 5 — web_background stall detector | `src/openstudio_operator/handlers/web_background_monitor.py` | **live** | Reads Resque queue depth via `redis_client.stale_workers(...)`; pod eviction on stall |
| Phase 4 — KEDA autoscaling | `deploy/keda-scaledobject.yaml` (ScaledObject + TriggerAuthentication) | **live** | Replaces custom Redis HPA-floor handler in #77; operator owns zero autoscaling surface and emits no `hpa_floor_adjustments_total` counter |
| Singleton guard (D05) | `src/openstudio_operator/singleton.py`, installed from `handlers/__init__.py` | **live** | Passive oldest-CR-per-namespace guard; Warning Event + loud log on second CR |

## Ground rules (from AGENTS.md)

- **D11** — every mutating action is gated by `spec.dryRun`. The default is
  `false`; this runbook always starts with `true` and flips it only in
  [Phase D](#phase-d--flip-dryrun-false) after the dry-run evidence is in.
- **D04** — operator memory lives in the CR `.status` subresource only
  (`softStops`, `requeues`, `startedSince`, `archivedAnalyses`,
  `lastRecycleAt`, `lastWebBackgroundRestart`). `kubectl get oscm` is the
  audit trail.
- **D05** — exactly one OSCM CR per namespace (oldest wins). Keep exactly
  one; this runbook uses name `validation`.
- **D12** — REST failures retry inside the client, then the tick is skipped;
  never "verify" by absence of a log line alone.

## API quirks that shape the verification steps

Full detail in [docs/kind-validation.md](kind-validation.md#drift-findings-live-verified-on-kind-2026-08-18-stack-3110);
the four that change how you interpret curl output here:

1. **Unknown ids never 404** — `GET /analyses/{id}/page_data.json` answers
   `200 {"analysis": null}`. To prove an analysis is gone after `DELETE`,
   check `GET /analyses.json` no longer lists it (or `page_data` returns
   `analysis: null`); a 404-based check will never fire.
2. **`DELETE /analyses/{id}` returns 302 without a JSON Accept** — send
   `Accept: application/json` and expect **204**. A 302 from curl's default
   `Accept: */*` is not an error.
3. **`POST /data_points/{id}/requeue` 500s on a jobless datapoint** (no
   `job_id` → no Resque job). Only requeue datapoints that show a `job_id`.
4. **`action start` optimistically reports `code: 200`** — it does not mean
   the analysis will complete; watch the state machine instead
   (`na → init → queued → started → post-processing → completed`).

## Prerequisites

- Work-cluster kubeconfig (this is the real deployment — treat every action
  as production-adjacent); `kubectl` access to namespace `openstudio-server`.
- A checkout of this repo on the dev machine with
  `python -m venv .venv && pip install -e '.[dev]'`.
- `curl`, `jq`; port-forward ability to `svc/web`.
- The operator image (`ghcr.io/anchapin/openstudio-server-operator:dev`) is
  published automatically on pushes to `develop`; prefer applying
  `deploy/operator-deployment.yaml`. If the published image is unavailable,
  run the operator locally (Phase A step 4), or build/push the dev image
  yourself before using the Deployment.
- Note (#69): the `:dev` publish pipeline was broken from #57 until the #69
  fix (README.md was excluded from the Docker build context, failing pip
  metadata generation). It is live again — future waves can pull the image
  directly.

## Phase 0 — pre-flight: fixture drift on the work cluster

Confirm the work cluster's REST surface still matches the contract the
operator was built against (the work cluster may run a different Redis /
mongoid config than kind):

```bash
kubectl -n openstudio-server port-forward svc/web 8080:80 &
scripts/capture_fixtures.sh --base-url http://localhost:8080   # reads only
scripts/check_fixture_drift.py --live
```

- Reads are safe. `--mutate` / `--with-delete` modes fire `soft_stop`,
  `requeue`, `DELETE` — **only** against a throwaway project/analysis you
  created for validation (they are interactive and warn; heeded here).
- Seeding a project → analysis → datapoint uses the nested routes
  (`POST /projects.json`, `POST /projects/{id}/analyses.json`, …) —
  [the exact snippets are in kind-validation.md](kind-validation.md#fixture-capture--drift-check).
- A **real batch** needs a real seed model: upload via the web UI
  (`kubectl -n openstudio-server port-forward svc/web 8080:80`) or the
  OpenStudio python client. An empty analysis never produces `start_time`
  (absent ≠ null) and can never exercise the SLA path.

Record drift results next to the run in the validation ticket. Any drift
means stop and fix the contract/tests first — not the runbook.

## KEDA cluster prerequisite (issue #77)

The operator no longer owns worker autoscaling — a standard KEDA
ScaledObject does. **Two cluster-prerequisite steps** (both idempotent,
both are cluster-admin tasks outside the operator process):

1. **Install KEDA in the `keda` namespace** — pick one of:

   ```bash
   # Path A: helm (preferred)
   helm repo add kedacore https://kedacore.github.io/charts && helm repo update kedacore
   helm install keda kedacore/keda --namespace keda --create-namespace --version 2.20.2

   # Path B: raw manifests (no helm required)
   scripts/install-keda.sh
   ```

   The script prefers helm when present; the raw-manifest path is the
   fallback. Both install KEDA 2.20.2 into the `keda` namespace and
   wait for `keda-operator` rollout.

2. **Disable the chart's `worker-hpa` HorizontalPodAutoscaler** — the
   helm chart ships an unconditional CPU HPA on `worker` (1–2 in kind,
   2–20 in production). The custom HPA-floor adjuster (#18) previously
   co-existed with it by patching only `minReplicas`; the KEDA
   ScaledObject scales the same Deployment, so **two autoscalers would
   fight one Deployment** if both are present. Disable the chart's HPA
   **BEFORE** applying the ScaledObject:

   ```bash
   # Helm-managed chart: disable the HPA via values override
   helm upgrade <release> openstudio-server/openstudio-server \
     --namespace openstudio-server \
     --set worker.autoscaling.enabled=false
   # OR post-render (raw delete)
   kubectl -n openstudio-server delete hpa worker-hpa --ignore-not-found
   ```

   On the kind cluster, the post-render is the recipe's
   `scripts/manifests/06-worker.yaml`: the `worker-hpa` HPA block is
   REMOVED from that manifest as of #77, so a fresh
   `scripts/deploy-openstudio-stack.sh` does not apply it.

Then apply the ScaledObject and its credentials:

```bash
kubectl apply -f deploy/redis-credentials-secret.yaml
kubectl apply -f deploy/keda-scaledobject.yaml
```

Verify the ScaledObject is `Ready=True` and the HPA KEDA owns is
active:

```bash
kubectl -n openstudio-server get hpa,scaledobject,worker
kubectl -n keda logs deploy/keda-operator -f   # watch scaling decisions
kubectl -n keda logs deploy/keda-metrics-apiserver -f   # watch Redis polls
```

The autoscaling acceptance criterion — **worker deployment scales from
0 to N based on pending Redis queue items** — is verified by submitting
N jobs to `resque:queue:simulations` and watching
`kubectl get worker -w`; scale-down is verified by waiting for the
queue to drain and watching the same. Live evidence in
[docs/kind-validation.md#live-capture-evidence-2026-08-19-issue-77-keda-migration](kind-validation.md#live-capture-evidence-2026-08-19-issue-77-keda-migration).

## Phase A — deploy the operator with `dryRun: true`

1. **Install the operator's K8s objects** (post-#3 manifests; same as the
   [kind walkthrough](kind-validation.md#phase-1-dryrun-true-smoke-walkthrough-feeds-20s-runbook)):

   ```bash
   kubectl apply -f deploy/crd.yaml
   kubectl apply -f deploy/rbac.yaml
   ```

2. **Create the OSCM custom resource, dry-run mode, with tuned policies**
   so a short batch can trip the SLA clock within minutes instead of hours
   (policy values are CRD spec — this is the intended knob, never code):

   ```yaml
   apiVersion: energy.nrel.gov/v1alpha1
   kind: OpenStudioClusterManager
   metadata:
     name: validation
     namespace: openstudio-server
   spec:
     serverUrl: http://web.openstudio-server.svc.cluster.local
     dryRun: true
     targetWorkerDeployment: worker
     targetWebBackgroundDeployment: web-background
     analysisPolicy:
       maxDurationMinutes: 180   # drop to ~1 for the Module-1 throwaway test
       autoSoftStop: true
     datapointPolicy:
       maxDatapointRuntimeMinutes: 45
       maxAutoRequeues: 3
     workerPolicy:
       recycleAfterAnalysis: true
       recycleWorkerIntervalHours: 12
       minRecycleIntervalMinutes: 30
   ```

3. **Confirm defaults landed**: `kubectl -n openstudio-server get oscm
   validation -o yaml` — spec defaults (CRD) populated, `.status` empty.

4. **Run the published operator Deployment** — the `:dev` image is published
   by the release workflow after pushes to `develop`:

   ```bash
   kubectl apply -f deploy/operator-deployment.yaml
   kubectl -n openstudio-server rollout status deploy/openstudio-operator
   ```

   If the image is unavailable, use the local fallback instead:

   ```bash
   source .venv/bin/activate
   kopf run --module openstudio_operator.handlers --namespace openstudio-server -v
   ```

   `/metrics` is served on **localhost:9090** (started at import from
   `src/openstudio_operator/handlers/__init__.py`; port must match
   `deploy/operator-deployment.yaml` `containerPort` and
   `metrics.DEFAULT_METRICS_PORT`). If you built the dev image and applied
   the Deployment instead: `kubectl -n openstudio-server port-forward
   deploy/openstudio-operator 9090:9090`.

5. **Baseline dry-run invariants** (identical to kind):

   - reads arrive — `kubectl -n openstudio-server logs deploy/web -c web`
     shows `GET /analyses.json` at the ~30 s cadence;
   - **no mutating request** (`soft_stop`, `action`, `requeue`, `DELETE`)
     appears in web logs while `dryRun: true`;
   - ticks that fail (network blips) are skipped and retried next poll —
     operator log shows `analysis SLA tick skipped`, never a crash.

## Phase B — run one real batch

Create a **throwaway** project and analysis with a real seed model (web UI
or python client), with enough datapoints to occupy a worker for a few
minutes. Let it run to `completed` at least once before any policy tuning —
this is the baseline that proves the operator's reads are correct on real
data:

- `GET /analyses/{id}/page_data.json` now carries `analysis.start_time`
  (the SLA anchor — it appears only after the first job; absence means
  skip, not failure);
- started datapoints carry `run_start_time`, `ip_address`, `job_id`;
- watch `kubectl get oscm validation -o yaml` — with M1 live you will see
  `.status` stay empty while the batch is healthy.

Keep this batch's ids around — the module checks below reuse them.

## Phase C — module-by-module live verification

All checks in this phase run with `spec.dryRun: true` unless a step says
otherwise (Phase D owns the flip). Events live on the CR object:

```bash
kubectl -n openstudio-server get events --sort-by=.lastTimestamp \
  --field-selector involvedObject.name=validation
```

Metrics: `curl -s localhost:9090/metrics | grep openstudio_operator_`.

### Module 1 — Analysis SLA soft-stop (LIVE, #8)

Event name (from code, `src/openstudio_operator/handlers/analysis_sla.py`):
**`AnalysisSoftStopped`** (Warning). Metric:
`openstudio_operator_soft_stops_total`. Status anchor: `.status.softStops`.

1. Start a second throwaway analysis; while it is `started`, set
   `spec.analysisPolicy.maxDurationMinutes: 1`
   (`kubectl -n openstudio-server patch oscm validation --type merge -p
   '{"spec":{"analysisPolicy":{"maxDurationMinutes":1}}}'`).
2. After the next ~30 s tick expect, in order: a Warning Event
   `AnalysisSoftStopped` whose message ends
   **"soft stop suppressed (spec.dryRun)"**; web logs showing **no**
   `GET /analyses/{id}/soft_stop` request; `.status.softStops` gaining
   `{<analysis-id>: {issuedAt: …, outcome: "dry-run"}}`;
   `openstudio_operator_soft_stops_total` +1.
3. One-shot proof: wait two more ticks — the Event/anchor do not repeat for
   the same id (the anchor is the idempotency mechanism, D04).
4. Restore `maxDurationMinutes` when done.

### Module 1b — grace wait + pod eviction **[pending module merge — #9]**

After Phase D (real soft-stop), for an analysis that stays `started` past
`gracefulStopTimeoutMinutes` (default 15) after the soft-stop anchor:

- with `forceDeleteOnEscalation: false` (default): **no** eviction, a loud
  log/Event instead;
- with `true` on a throwaway analysis: worker pods whose pod IP matches a
  started datapoint's `ip_address` (from `GET /data_points.json` — the
  escalation-only full-doc call) are deleted; sibling worker pods survive;
  the datapoint is re-run by the queue, not killed server-side (there is no
  `kill`/`hard_stop` API — escalation is Kubernetes-side only).
- **Work-cluster-only check:** observe pod termination under the **real NFS
  provisioner** — unmount latency, no stuck-mount evictions on the node
  (kind's hostPath stand-in cannot reproduce this; see
  [Approximations](kind-validation.md#approximations-vs-production)).

### Module 2 — zombie datapoint watchdog **[pending module merge — #10]**

Planned Event (per handler docstring / plan doc): **`DatapointRequeued`**
(Normal). Metrics: `openstudio_operator_datapoints_requeued_total`,
`openstudio_operator_datapoints_requeue_exhausted_total`. Status anchors:
`.status.requeues`, `.status.startedSince`.

1. Dry-run trip: start a batch, `kubectl patch` the CR to
   `datapointPolicy.maxDatapointRuntimeMinutes: 1`; a started datapoint
   older than that (clock = operator-tracked `startedSince`, **not** a
   server timestamp — the light poll `GET /data_points/status?status=1&jobs=started`
   returns none) yields a dry-run-marked `DatapointRequeued` Event, a
   `.status.requeues` entry, and no `POST /data_points/{id}/requeue` in web
   logs.
2. Post-flip (Phase D): the real requeue lands on the `requeued` Resque
   queue — verify via Redis (read-only, #12 client semantics):
   `redis-cli -h queue.openstudio-server -a openstudio-rotated llen requeued`
   (issue #150 — was the legacy literal `openstudio` until #150; for a fresh
    cluster, run `scripts/rotate_redis_password.sh` to install a per-cluster
    random password and substitute it here).
   Mind quirk 3: only datapoints **with** `job_id` requeue; a jobless dp
   500s — the handler must never select one.
3. Bound proof: keep the dp zombie past `maxAutoRequeues` (default 3) —
   requeues stop, `…_requeue_exhausted_total` increments, a final Event
   marks abandonment.

### Module 3 — gated worker recycler **[pending module merge — #11]**

Planned Event: **`WorkerRecycled`** (per handler docstring / plan doc /
AGENTS.md). Metric: `openstudio_operator_workers_recycled_total`. Status
scalar: `.status.lastRecycleAt`.

1. After the Phase-B batch completes (analysis leaves `started`, no other
   analysis running, `recycleAfterAnalysis: true`): dry-run Event
   `WorkerRecycled`, `.status.lastRecycleAt` set, and **no** change to the
   `worker` Deployment (`kubectl -n openstudio-server rollout status
   deploy/worker` quiet; `kubectl get deploy worker -o jsonpath='{.spec.template.metadata.annotations}'`
   has no `restartedAt`).
2. Post-flip: the recycle is a `restartedAt` annotation patch → rolling
   restart of `deploy/worker`; `lastRecycleAt` gates repeats
   (`minRecycleIntervalMinutes`, default 30). The chart's HPA `worker-hpa`
   is untouched by the recycler — replicas may regrow, that is the HPA's
   business.
3. Worker scratch is emptyDir (chart) — verify **no** attempt to clear
   `/mnt/openstudio` on workers (dropped from the plan; NFS is mounted only
   by `web`).

### Module 4 — archival + NFS prune **[pending module merge — #16 wiring]**

`src/openstudio_operator/archival.py` (#15, merged) is the Job generator;
the storage-pruner orchestration (retention gate → Job spawn → verified
upload → `DELETE /analyses/{id}`) is #16. Status anchor:
`.status.archivedAnalyses` (the only durable signal of a verified-then-
deleted analysis; no Prometheus byte counter — see #50 / audit doc
Appendix D). No Event name is defined in code yet — verify via objects +
status, not Events.

1. Configure a throwaway bucket: `storagePolicy.archiveToS3: true`,
   `backend: s3|gcs|azure`, `bucket: …`, `secretRef: <a Secret in this
   namespace carrying the rclone env vars>` — credentials reach the Job via
   `envFrom` only; the operator must never read them (no secrets RBAC).
   Set `retentionDays: 0` to archive immediately.
2. Dry-run: after a completed analysis passes the retention gate expect the
   dry-run Event/log and **no** Job created
   (`kubectl -n openstudio-server get jobs` unchanged).
3. Post-flip: a Job named after the analysis id appears, mounts `nfs-pvc`
   **read-only** at `/mnt/openstudio`, runs rclone copy + `rclone check`;
   **only a Completed Job** (the verified-upload gate) triggers
   `DELETE /analyses/{id}` — send it with `Accept: application/json` and
   expect 204 (quirk 2); then verify absence via `GET /analyses.json`
   (quirk 1). `.status.archivedAnalyses` gains the record.
4. **Work-cluster-only check (explicit deferral):** the NFS-mount behavior
   kind could not prove — real provisioner mounts inside the archival Job,
   read-only enforcement, and space actually reclaimed on the NFS export
   after the server-side `DELETE` cascade (the DELETE rm-rf's the asset
   dirs under `server/assets/…`; confirm with the PV's `capacity`/export
   df, not just the Job status).
5. Failure drill (post-flip, throwaway analysis): point `bucket` at a
   non-writable prefix — the Job must fail (nonzero exit), **no DELETE is
   sent**, the analysis survives, `.status.archivedAnalyses` stays clean.

### Module 5 — web_background stall detector **[pending module merge — #13]**

No Event name defined in code yet. Status scalar:
`.status.lastWebBackgroundRestart`. Signal source: the read-only Redis
client (`src/openstudio_operator/redis_client.py`, #12) — queue depths
(`LLEN simulations` / `LLEN requeued`) + Resque worker registry
(`resque:workers`, heartbeats).

1. Healthy baseline: with the batch running, the detector must do nothing —
   `deploy/web-background` unchanged, `.status.lastWebBackgroundRestart`
   unset.
2. Induced stall (work-cluster-safe approximation): scale the Resque
   workers down (`kubectl -n openstudio-server scale deploy/web-background
   --replicas=0`) with items queued; past `stallWindowMinutes` (default 10)
   expect dry-run Event/log in dry-run, or — post-flip — a restart of
   `deploy/web-background` and `.status.lastWebBackgroundRestart` set,
   followed by queue progress resuming (`llen simulations` drains).
3. Cooldown: a second induced stall inside the cooldown must not restart
   again (anchor = `.status.lastWebBackgroundRestart`).

### Phase 4 — KEDA ScaledObject scaling (issue #77)

The custom HPA-floor reconciliation loop (#18) was REMOVED in #77 and
replaced with a standard KEDA ScaledObject. The operator has zero
autoscaling surface — KEDA owns the HPA, and the operator's Role lost
its `horizontalpodautoscalers` verbs (the same RBAC-shrink pattern as
#78). The acceptance criterion is:

> "Worker deployment scales from 0 to N based on pending Redis queue
> items."

Steps (assumes the [KEDA cluster prerequisite](#keda-cluster-prerequisite-issue-77)
is met — KEDA installed, `worker-hpa` deleted, ScaledObject applied):

1. **Baseline:** `kubectl -n openstudio-server get hpa,scaledobject,worker`
   — ONE HPA (`keda-hpa-worker`; the chart's `worker-hpa` is gone), the
   ScaledObject reports `Ready=True`, worker at `replicas: 0` (or 1 if
   `minReplicaCount` was raised).
2. **Scale-up probe:** RPUSH N jobs onto `resque:queue:simulations` (or
   `resque:queue:requeued`); watch `kubectl -n openstudio-server get
   worker -w` for the replica count to climb to N (clamped to
   `maxReplicaCount: 5`). Capture: timestamps, `keda_scaledobject_metrics`
   value, `keda_scaler_metrics` value, replica count progression.
3. **Scale-down probe:** drain the queue (or `DEL resque:queue:simulations`
   to force it); watch the worker Deployment drop to `minReplicaCount`
   after KEDA's `cooldownPeriod: 60 s`. Capture: timestamps, replica
   count.
4. **Negative control:** `curl -s localhost:9090/metrics | grep
   openstudio_operator_` exposes no `hpa_floor_adjustments_total` and no
   `^# HELP` for it (proves #77 removal was complete — no orphan
   counter, no orphan incrementer; the operator's `/metrics` surface is
   exclusively the action families that survived #77).
5. **Operator metric co-existence:** KEDA's metrics adapter and the
   operator's `/metrics` endpoint serve distinct signals; both
   reachable in-cluster via `kubectl -n openstudio-server
   port-forward deploy/openstudio-operator 9090:9090` and `kubectl -n
   keda port-forward deploy/keda-metrics-apiserver 6443:6443`
   respectively.

Live evidence in [docs/kind-validation.md#live-capture-evidence-2026-08-19-issue-77-keda-migration](kind-validation.md#live-capture-evidence-2026-08-19-issue-77-keda-migration).

## Phase D — flip `dryRun: false`

Preconditions, all evidenced above in dry-run:

- [ ] Phase 0 fixture drift clean (or explained) on the work cluster;
- [ ] Module 1 dry-run Event + anchor + metric observed;
- [ ] every merged module's dry-run evidence collected;
- [ ] only throwaway analyses/projects are in scope for mutation.

```bash
kubectl -n openstudio-server patch oscm validation --type merge -p '{"spec":{"dryRun":false}}'
```

Then re-run the module steps marked "post-flip" above, on throwaway
analyses only. The flip changes only the mutation — anchors, metrics and
Events behave identically (D11), which is exactly what you are verifying.
`AnalysisSoftStopped` messages now end **"soft stop issued"** and web logs
show the `soft_stop` request.

**End state**: leave `dryRun: false` only if the cluster owner accepts the
operator acting for real; otherwise flip back and keep the CR as a
monitored dry-run.

## Rollback

Order matters. The operator must stop acting **before** its API objects
disappear, and CR deletion must happen **before** CRD deletion (deleting a
CRD cascades all CRs and their `.status` state with no per-object control).

1. **Stop the operator first** — `Ctrl-C` the local `kopf run` process, or:

   ```bash
   kubectl -n openstudio-server scale deploy/openstudio-operator --replicas=0
   # (or: kubectl -n openstudio-server delete deploy openstudio-operator)
   ```

2. **Delete the OSCM CR** — removes the status store and stops Events:

   ```bash
   kubectl -n openstudio-server delete oscm validation
   ```

   (Events already emitted age out per Kubernetes TTL; they are not
   deleted with the CR.)

3. **Remove the operator's RBAC and CRD** (CRD last — it cascades any
   remaining CRs):

   ```bash
   kubectl delete -f deploy/rbac.yaml
   kubectl delete -f deploy/crd.yaml
   ```

4. **Helm uninstall — PV DATA-LOSS WARNING.** Uninstalling the OpenStudio
   helm release may delete its PVCs (notably `nfs-pvc`); if the backing PV
   was dynamically provisioned with `reclaimPolicy: Delete`, the NFS data
   (every analysis asset the server ever stored) is destroyed. Safe
   sequence:

   ```bash
   helm list -n openstudio-server                      # find the release name
   kubectl -n openstudio-server get pvc nfs-pvc -o jsonpath='{.spec.volumeName}'
   kubectl patch pv <that-pv> -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'
   # optionally also: kubectl -n openstudio-server patch pvc nfs-pvc \
   #   -p '{"metadata":{"annotations":{"helm.sh/resource-policy":"keep"}}}'
   # optionally archive/snapshot the NFS export first
   helm uninstall <release> -n openstudio-server
   kubectl get pv | grep nfs                            # must show Released, not Deleted
   ```

   Without step "patch pv" first, treat `helm uninstall` as
   **destroying the NFS data** — it is irreversible.

## Sign-off checklist (ties back to the issue's acceptance criteria)

- [ ] Every merged module's dry-run evidence captured (Events, metrics,
      `.status` anchors) — Phase C;
- [ ] `dryRun: false` evidence captured on throwaway analyses — Phase D;
- [ ] NFS-mount behavior verified against the real provisioner
      (Module 1b eviction, Module 4 archival + space reclaim);
- [ ] KEDA-driven scaling verified end-to-end (worker scales 0→N on backlog, back to 0 on drain; `keda_scaledobject_metrics` advances; `openstudio_operator_hpa_floor_adjustments_total` is absent from the operator `/metrics`) — #77;
- [ ] Rollback rehearsed or at least dry-walked, including the PV
      reclaimPolicy patch before any helm uninstall.
