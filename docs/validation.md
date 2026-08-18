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
- **real HPA dynamics** on `worker-hpa` (kind has no metrics-server),
- mutating actions (`soft_stop`, `requeue`, `DELETE`) against analyses that
  matter — under `dryRun` first, then for real on throwaway analyses only.

## Module status at time of writing

| Module | Handler | State | Issue |
|---|---|---|---|
| 1 — Analysis SLA soft-stop | `src/openstudio_operator/handlers/analysis_sla.py` | **live** | #8 merged |
| 1b — Grace wait + pod eviction | (extends `analysis_sla.py`) | pending | #9 |
| 2 — Zombie datapoint watchdog | `src/openstudio_operator/handlers/datapoint_watchdog.py` | stub | #10 |
| 3 — Worker recycler | `src/openstudio_operator/handlers/worker_recycler.py` | stub | #11 |
| 4 — Archival + NFS prune | `src/openstudio_operator/handlers/storage_pruner.py` (stub) + `src/openstudio_operator/archival.py` (Job generator, merged) | orchestration pending | #15 merged, #16 pending |
| 5 — web_background stall detector | `src/openstudio_operator/handlers/web_background_monitor.py` | stub (Redis client ready in `src/openstudio_operator/redis_client.py`) | #13 |
| Phase 4 — HPA-floor adjuster | (none yet) | pending | #18 |

Steps depending on unmerged code are tagged **[pending module merge — #N]**.
Run them as written once the module lands; they are part of this runbook, not
optional extras.

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
  **unpublished** — run the operator locally (Phase A step 4), or build/push
  the dev image yourself before using `deploy/operator-deployment.yaml`.

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

4. **Run the operator** — locally (image is unpublished; kubeconfig points
   at the work cluster):

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
   `redis-cli -h queue.openstudio-server -a openstudio llen requeued`.
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
upload → `DELETE /analyses/{id}`) is #16. Metric:
`openstudio_operator_storage_freed_bytes`. Status anchor:
`.status.archivedAnalyses`. No Event name is defined in code yet — verify
via objects + status, not Events.

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
   (quirk 1). `.status.archivedAnalyses` gains the record;
   `…_storage_freed_bytes` grows.
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

### Phase 4 — HPA-floor adjuster **[pending module merge — #18]**

**Work-cluster-only check (explicit deferral):** real HPA dynamics are
untestable on kind (no metrics-server there; `worker-hpa` sat pinned at
`minReplicas` — see
[Approximations](kind-validation.md#approximations-vs-production)).

1. Enqueue a batch large enough to grow the Redis backlog; verify the
   operator **patches the chart's existing `worker-hpa` `minReplicas`**
   (`kubectl -n openstudio-server get hpa worker-hpa -o jsonpath='{.spec.minReplicas}'`)
   — and that **no second autoscaler exists** (no KEDA objects; KEDA is a
   documented future migration, not this design).
2. Backlog drains → the floor relaxes back; CPU-driven scaling above the
   floor still belongs to the HPA. Confirm the operator never touches
   `desiredReplicas` directly.

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
- [ ] HPA-floor dynamics verified against the real `worker-hpa` (#18);
- [ ] Rollback rehearsed or at least dry-walked, including the PV
      reclaimPolicy patch before any helm uninstall.
