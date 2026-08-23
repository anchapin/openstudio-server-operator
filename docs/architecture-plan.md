# **OpenStudio Server Kubernetes Operator: Architectural Blueprint & Implementation Plan**

> **Historical design document** — kept for decision provenance, not current
> wiring. The authoritative module layout is [`AGENTS.md`](../AGENTS.md)'s
> Layout section plus [`docs/adr/`](adr/). Two boxes in the §2 diagram are
> superseded: the in-process "Storage & Artifact Pruner" moved to the
> `deploy/storage-cronjob.yaml` CronJob (#78), and the "KEDA / HPA
> Controller" is deleted — autoscaling is KEDA-only, no operator HPA code
> (#77).

## **1\. Executive Summary & Objectives**

OpenStudio Server is a distributed energy simulation platform running on Ruby/Rails (web), background orchestrators (web\_background), database engines (MongoDB), and simulation compute nodes (worker). Running OpenStudio Server on Kubernetes via Helm provides baseline orchestration, but long-running, resource-heavy simulation workloads frequently suffer from:

1. **Analysis Stagnation:** Analyses hanging in started state due to deadlocks or unhandled EnergyPlus errors.  
2. **Resource Exhaustion:** Worker memory leaks, lingering RServe/EnergyPlus zombie threads, and local disk space saturation on worker nodes.  
3. **NFS Volume Bloat:** High-volume simulation outputs (.sql, .html, .osm) exhausting Shared Persistent Volumes (NFS/EFS/GCS Fuse).  
4. **Manual Intervention Needs:** Operations teams having to manually SSH, issue REST calls to soft-stop/kill runs, or bounce worker deployment pods.

### **Objective**

Build a lightweight, Kubernetes-native **OpenStudio Operator** (using **Python Kopf** or **Go Kubebuilder**) that automates operational tasks, implements automated recovery and soft stops, dynamically scales workers, and handles lifecycle data archiving/pruning.

## **2\. System Architecture & Component Design**

The Operator sits alongside the standard Helm deployment (openstudio-server-helm), observing both the Kubernetes API and the OpenStudio REST/MongoDB state.

┌─────────────────────────────────────────────────────────────────────────┐  
│                       OpenStudio Server Operator                        │  
│                                                                         │  
│  ┌──────────────────────┐   ┌──────────────────────┐   ┌─────────────┐  │  
│  │ Analysis Timeout     │   │ Zombie Datapoint     │   │ Worker      │  │  
│  │ & Soft-Stop Manager  │   │ Requeue Watchdog     │   │ Recycler    │  │  
│  └──────────┬───────────┘   └──────────┬───────────┘   └──────┬──────┘  │  
│             │                          │                      │         │  
│  ┌──────────┴───────────┐   ┌──────────┴───────────┐   ┌──────┴──────┐  │  
│  │ Storage & Artifact   │   │ Web Background       │   │ KEDA / HPA  │  │  
│  │ Pruner (S3/GCS)      │   │ Health Monitor       │   │ Controller  │  │  
│  └──────────────────────┘   └──────────────────────┘   └─────────────┘  │  
└────────────────────────────────────┬────────────────────────────────────┘  
                                     │  
           ┌─────────────────────────┴─────────────────────────┐  
           ▼                                                   ▼  
┌──────────────────────┐                            ┌─────────────────────┐  
│ OpenStudio REST API  │                            │ Kubernetes API      │  
│  \- /analyses.json    │                            │  \- Deployments      │  
│  \- /analyses/{id}/…  │                            │  \- Pods             │  
│  \- /data\_points.json │                            │  \- Events & CRDs    │  
│  \- /data\_points/…    │                            │                      │  
└──────────────────────┘                            └─────────────────────┘

## **3\. Custom Resource Definition (CRD) Specification**

The operator will be governed by a CRD named OpenStudioClusterManager. This exposes declarative policy controls to cluster administrators.

### **openstudioclustermanager\_crd.yaml**

apiVersion: apiextensions.k8s.io/v1  
kind: CustomResourceDefinition  
metadata:  
  name: openstudioclustermanagers.energy.nrel.gov  
spec:  
  group: energy.nrel.gov  
  versions:  
    \- name: v1alpha1  
      served: true  
      storage: true  
      schema:  
        openAPIV3Schema:  
          type: object  
          properties:  
            spec:  
              type: object  
              properties:  
                serverUrl:  
                  type: string  
                  description: "Internal K8s DNS URL for OpenStudio web service."  
                targetWorkerDeployment:  
                  type: string  
                  description: "Name of worker Deployment to manage/recycle."  
                targetWebBackgroundDeployment:  
                  type: string  
                  description: "Name of web\_background Deployment."  
                analysisPolicy:  
                  type: object  
                  properties:  
                    maxDurationMinutes:  
                      type: integer  
                      default: 180  
                    gracefulStopTimeoutMinutes:  
                      type: integer  
                      default: 15  
                    autoSoftStop:  
                      type: boolean  
                      default: true  
                datapointPolicy:  
                  type: object  
                  properties:  
                    maxDatapointRuntimeMinutes:  
                      type: integer  
                      default: 45  
                    maxAutoRequeues:  
                      type: integer  
                      default: 3  
                workerPolicy:  
                  type: object  
                  properties:  
                    recycleWorkerIntervalHours:  
                      type: integer  
                      default: 12  
                    recycleAfterAnalysis:  
                      type: boolean  
                      default: true  
                storagePolicy:  
                  type: object  
                  properties:  
                    archiveToS3:  
                      type: boolean  
                      default: false  
                    backend:  
                      type: string  
                      enum: [s3, gcs, azure]  
                    bucket:  
                      type: string  
                    secretRef:  
                      type: string  
                    retentionDays:  
                      type: integer  
                      default: 7  
                    purgeCompletedNFSFiles:  
                      type: boolean  
                      default: true

## **4\. Operator Core Modules & Logic Flow**

### **Module 1: Analysis Lifecycle & Soft-Stop Manager**

* **Goal:** Prevent stuck simulations from clogging worker pods indefinitely.
* **Logic Flow (issue #83 re-sourced — D1 SLA clock anchor, D2 escalation path):**
  1. Poll `GET /analyses.json` every 30 seconds (raw Mongoid docs; no reliable `status` field — issue #19 D1/#83 D1: status is omitted on a fresh analysis, so the SLA tick cannot filter candidates from the index alone).
  2. For each analysis, fetch `GET /analyses/{id}/status.json` to read the real analysis `status` (the only endpoint that reliably reports status on v3.11.0). The new SLA clock anchor is the **operator-observed first sight of the analysis in the `started` state** — written to `CR .status.softStops[aid].issuedAt` with `outcome="watching"` (D04 durable store). On subsequent ticks, runtime is `now - issuedAt`; the page_data `start_time` field is no longer consulted as a clock anchor (it is absent until the first job runs, per contract §2).
  3. Once a `watching` anchor exists, check runtime against `spec.analysisPolicy.maxDurationMinutes`. If exceeded, **soft-stop**: `GET /analyses/{id}/soft_stop` (cooperative stop; does not wait for in-flight runs). Upgrade the anchor's `outcome` to `"issued"` (or `"dry-run"` under dryRun). Emit K8s Warning Event `AnalysisSoftStopped`. This is the operator's one-shot soft-stop (the `issuedAt`+`outcome="issued"` pair is the durable idempotency mechanism — survives ticks and operator restarts).
  4. Grace wait: a still-`started` anchor past `analysisPolicy.gracefulStopTimeoutMinutes` from its `issuedAt` is escalated (issue #9). Restart-safe: a fresh operator process honors the original `issuedAt`, never its own startup time.
  5. Escalate on the Kubernetes side: the surgical pod eviction is now re-sourced to **Resque worker identity** (issue #83 D2). Pre-#83 matched started-datapoint `ip_address` (heavy `GET /data_points.json`) against worker pod `status.podIP` — but on v3.11.0 datapoint `ip_address` is always null, so that path never matched. The new path reads the Resque worker set (`SMEMBERS resque:workers`), filters to workers whose `payload.args` reference the analysis id (`GET resque:worker:{worker_id}` per candidate), and maps each matching worker id back to its pod via the standard `{hostname}:{pid}:{queues}` shape (the K8s pod's `hostname` defaults to the pod name). `list_namespaced_pod` is consulted to verify each candidate pod exists in the namespace. `analysisPolicy.forceDeleteOnEscalation` switches the delete grace: `false` (default) passes no `grace_period_seconds` (cooperative drain via the chart's preStop + 5200s cap); `true` passes `grace_period_seconds=0` (immediate SIGKILL).
  6. Each escalated analysis emits a Warning Event `AnalysisEscalated`, counts the decision in `WORKER_PODS_EVICTED_TOTAL`, and stamps the anchor via `StatusStore.mark_soft_stop_escalated`. Anchor retirement: an anchored analysis that left `started` (completed, post-processing, ...) or vanished from the API prunes the anchor — no escalation, the soft stop worked.

**Reserved surface: ``stop_analysis`` (issue #49).** The waiting-variant stop (``POST /analyses/{id}/action`` with ``analysis_action=stop``) is contract-correct and tested, but has zero call sites: SLA uses the non-waiting ``soft_stop`` (Module 1, step 3) and retention owns the deletion path. It is kept in ``OpenStudioClient`` for contract completeness and is **reserved** for future wiring. Any handler that wires it MUST add ``spec.dryRun`` gating (D11) and a ``.status``-anchored idempotency record (D04), and MUST update ``docs/audit-dryrun-idempotency.md`` §1.1 row R2 (replace DORMANT with GATED) in the same PR.

### **Module 2: Zombie Datapoint Watchdog & Auto-Requeue**

* **Goal:** Detect individual stalled simulations caused by worker OOM or pod eviction.  
* **Logic Flow:**  
  1. Poll GET /data\_points/status?status=1&jobs=started, the light watchdog view ({data\_points: [{\_id, id, analysis\_id, status, status\_message}]}; Rails quirk: the presence of the status param gates filtering and the filter value is read from jobs). It returns no timestamps.  
  2. Track the runtime clock operator-side: record each datapoint's first-seen time in the CR .status startedSince map. GET /data\_points.json ignores query params (there is no server-side status filter), so it is reserved for escalation, where its full docs supply the datapoint ip\_address.  
  3. If elapsed time exceeds spec.datapointPolicy.maxDatapointRuntimeMinutes and requeues \< maxAutoRequeues:  
     * Call POST /data\_points/{id}/requeue (204 No Content; destroys the existing Resque job on the requeue/simulations queues and re-enqueues onto the requeued queue; it does not kill a wedged worker process, Module 1 escalation handles that).  
     * Increment the requeue tracker in CR .status.  
     * Emit K8s Normal Event: DatapointRequeued.

### **Module 3: Worker Node Hygiene & Post-Run Recycler**

* **Goal:** Flush transient EnergyPlus memory leaks and local temporary scratch directories.  
* **Logic Flow:**  
  1. Listen for analysis status transition: started \-\> completed / stopped / failed.  
  2. If spec.workerPolicy.recycleAfterAnalysis \== true and no other active analysis is running:  
     * Perform a rolling restart of the worker deployment (kubectl rollout restart deployment/\<targetWorkerDeployment\>).  
     * Force-clear lingering temp files on the shared volume if configured.

### **Module 4: Artifact Archiver & NFS Storage Pruner**

* **Goal:** Prevent shared NFS storage (PV) from running out of disk space — **without** the operator core process holding any storage polling loop or batch-Job credentials (issue #78 moved the pipeline to native Job primitives).
* **Architecture (#78):** the retention pipeline runs in a namespaced **CronJob** (`deploy/storage-cronjob.yaml`, `openstudio-storage-pruner`, schedule `*/10 * * * *` — the same 600 s cadence the in-operator timer had) whose container runs `python -m openstudio_operator.prune_entrypoint` from the operator image. One CronJob run = one `run_retention_tick` (library code in `src/openstudio_operator/retention.py`) against the **oldest** OSCM CR (D05), honoring that CR's `spec.dryRun` (D11) and anchoring all state in its `.status.archivedAnalyses` (D04). The operator's only remaining poll loop is the Module 1 SLA watch, which doubles as the completion observer — the server offers no push, so *some* observer must poll; storage completions are now observed by the scheduled prune tick instead.
* **Logic Flow (ordering unchanged from #16 — this is the invariant):**
  1. Retention eligibility: analysis `status == "completed"` for ≥ `storagePolicy.retentionDays` (clock = the doc's `updated_at`, falling back to `created_at`; neither usable ⇒ never eligible).
  2. Spawn an ephemeral K8s Job (the unchanged #15 generator, `archival.py`) mounted to the NFS PV read-only; it streams the analysis + datapoint asset trees to the `storagePolicy.backend` bucket (s3 | gcs | azure) and verifies with `rclone check` (size+hash). A Completed Job IS the verified-upload gate.
  3. Only on verified upload: `DELETE /analyses/{id}` — the server-side cascade that rm-rf's the NFS asset dirs and MongoDB documents. **This REST delete is the only NFS cleanup; nothing ever rm-rf's NFS paths directly.**
* **Deliberately NOT a disk-watermark pruner:** the original #78 sketch ("configurable disk high-water marks") was rejected at issue-triage time — a watermark deleter touching NFS contents directly would bypass the MongoDB bookkeeping and could destroy un-archived analyses. The CronJob preserves the bookkeeping-anchored ordering above. Scoping a watermark sweeper to operator-owned scratch paths was also moot: worker scratch is `emptyDir` and archival Jobs are TTL-ephemeral, so the operator owns no persistent NFS paths of its own.
* **Why CronJob-owned (not operator-emitted prune Jobs):** one actor owns the whole state machine (spawn → watch → verify → delete) with the proven `run_retention_tick` code unchanged, the operator Role loses the entire `batch/jobs` rule, and the completion trigger granularity is one CronJob period (≤ 10 min) — identical latency to the retired 600 s timer. Splitting "operator fires archival on completion, CronJob prunes" would have spread one D04 state machine across two processes for no latency gain.
* **D11 (dryRun) on the externalized workload:** the entrypoint builds `OperatorConfig.from_spec(active_cr.spec)`, so `spec.dryRun: true` suppresses archival-Job spawn, failed-Job cleanup and the analysis DELETE — emitting the same dry-run-marked Events on the CR via CoreV1 (source `openstudio-storage-pruner`) that `kopf.event` produced before. The CronJob's *schedule* itself is a cluster-admin-owned manifest property (like the operator Deployment's `strategy: Recreate`) — deliberately NOT a CRD field, because nothing in the operator consumes it.
* **RBAC shift (quantified):** moving the work shifts permissions to the prune workload's ServiceAccount rather than eliminating them — but the long-lived operator credential gets strictly smaller, and the new Role cannot touch anything but CRs/CR-status, Jobs and Events.

| Rule (namespace `openstudio-server`) | Operator Role before #78 | Operator Role after #78 | Prune SA Role (#78, new) |
|---|---|---|---|
| `energy.nrel.gov` `openstudioclustermanagers` (+ `/status`) | `*` | `*` (unchanged) | `list`; `/status`: `get, patch` |
| `apps` `deployments` | get, list, watch, patch, update | unchanged | — |
| `""` `pods` | get, list, watch, delete | unchanged | — |
| `""` `events` | get, list, watch, create, patch | unchanged | create |
| `autoscaling` `horizontalpodautoscalers` | get, list, patch | **— (rule removed in #77)** | — |
| `batch` `jobs` | get, list, watch, create, delete | **— (rule removed)** | get, create, delete |
| **Total** | 6 rules / 22 enumerated verbs (+CR `*`) | 4 rules / 14 enumerated verbs (+CR `*`) | 4 rules / 7 verbs |

Net: the operator drops two whole rules (autoscaling/horizontalpodautoscalers via #77 and batch/jobs via #78) and 8 enumerated verbs; the prune SA adds 7 narrowly-scoped verbs on 4 resources — **no** Secrets, Deployments, Pods, HPA, PVC or NFS access (the archival Jobs it spawns carry their own envFrom credentials and read-only PVC mount). Both Roles stay namespaced; no ClusterRole anywhere.

### **Module 5: Resque / web\_background Watchdog**

* **Goal:** Ensure the job distribution mechanism hasn't deadlocked.  
* **Logic Flow:**  
  1. Read queue state directly from Redis (Service queue:6379; Resque queues simulations and requeued): queue depth via LLEN simulations / LLEN requeued, worker liveness via the Resque worker registry and heartbeat keys. /cluster.json does not exist in v3.11.0, and /compute\_nodes.json is legacy and unpopulated on Kubernetes, so neither is a liveness source.  
  2. If queued data points exist \> 0, but 0 workers are processing for \> 10 minutes while worker pods are Running and healthy:  
     * Assume web\_background scheduler loop is stuck.  
     * Trigger pod restart for web\_background deployment.

## **5\. State Machine Diagrams**

### **Analysis State Automation**

\[ Queued \] ───\> \[ Started \] ───────────────────────────────────────────────┐  
                     │                                                      │  
                     ├─ (Exceeds SLA Timeout) ─\> \[ Soft Stop Triggered \]    │  
                     │                                   │                  │  
                     │                          (Grace Period Pass?)        │  
                     │                                /     \\               │  
                     │                             (Yes)    (No)            │  
                     │                               /        \\             │  
                     │                       \[ Completed \]  \[ Evict Pods \]  │  
                     │                            │               │         │  
                     └────────────────────────────┴───────────────┴─────────┴─\> \[ Trigger Worker Recycle & Storage Prune \]

Analysis states (verified v3.11.0): na \-\> init \-\> queued \-\> started \-\> post-processing \-\> completed. There is no stopping, stopped, or failed state; soft\_stop and action stop only flip the run\_flag boolean, so grace-period decisions anchor on CR .status timestamps rather than server state. There is no kill or hard\_stop action: the (No) branch is Kubernetes-side eviction of worker pods identified by their Resque worker record (issue #83 D2 — pre-#83 escalation matched started-datapoint `ip_address` against worker pod IPs, but on v3.11.0 datapoint `ip_address` is always null, so the IP path never matched; the Resque-worker-identity path uses `SMEMBERS resque:workers` + `GET resque:worker:{worker_id}` to resolve the worker to its pod name).

## **6\. Implementation Phasing & Roadmap**

### **Phase 1: MVP Controller (Week 1–2)**

* Setup Kopf/Kubebuilder framework and deploy base CRD.  
* Implement Analysis SLA monitor & automated REST **Soft-Stop** caller.  
* Implement basic K8s Event emissions (AnalysisSoftStopped, WorkerRecycled).

### **Phase 2: Datapoint Watchdog & Worker Hygiene (Week 3–4)**

* Implement zombie datapoint detection and auto-requeue logic.  
* Implement post-analysis rolling worker restarts.  
* Implement web\_background queue-stall detector.

### **Phase 3: Storage & Archival Automation (Week 5\)**

* Build S3/GCS export container & K8s Job generator module.  
* Implement NFS workspace cleanup and API object deletion routines.

### **Phase 4: Production Hardening & Autoscaling (Week 6\)**

* **Worker autoscaling — KEDA `ScaledObject` (issue #77; supersedes the #18 HPA-floor adjuster).** Standard KEDA scaler driving the `worker` Deployment from the Resque `simulations` + `requeued` queue depths (Resque 2.x key format `resque:queue:<name>`, verified in #44). The ScaledObject's `value` expression sums the two queue `LLEN`s; the value translates 1:1 into replicas (one worker per pending job), bounded by `minReplicaCount: 0` and `maxReplicaCount: 5` (raise in production). The chart's unconditional CPU HPA (`worker-hpa`) is disabled — the kind recipe's `scripts/manifests/06-worker.yaml` no longer includes it. KEDA's metrics adapter is the sole autoscaler for `worker`; the operator's only role is the ScaledObject's existence (it does not install KEDA or manage the ScaledObject — both are cluster-admin tasks). The custom HPA-floor reconciliation loop and its D10 design rationale (the chart's unconditional HPA forced an in-operator minReplicas patcher) are GONE — see `docs/kind-validation.md` for the live scale-up/scale-down evidence. The original #18 HPA-floor design is documented in the git history of `src/openstudio_operator/handlers/hpa_floor.py` (deleted) for reference.
* Implement Prometheus metrics endpoint (/metrics) inside the operator to track total soft-stops, auto-requeues, and storage freed.
* Restrict operator ServiceAccount RBAC permissions.

## **7\. RBAC & Security Deployment Specification**

The operator requires restricted K8s permissions.

### **operator\_rbac.yaml**

apiVersion: v1
kind: ServiceAccount
metadata:
  name: openstudio-operator-sa
  namespace: openstudio-server
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: openstudio-operator-role
  namespace: openstudio-server
rules:
  # Manage Custom Resources
  - apiGroups: ["energy.nrel.gov"]
    resources: ["openstudioclustermanagers", "openstudioclustermanagers/status"]
    verbs: ["*"]
  # Manage Worker Deployments & Pod Restarts
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch", "patch", "update"]
  # Read pods; delete for surgical escalation of zombie datapoint workers (#9)
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch", "delete"]
  # Emit Kubernetes Events (soft-stop / recycle / dry-run markers)
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["get", "list", "watch", "create", "patch"]
  # NOTE: archival Jobs no longer run in the operator process (#78) — the
  # storage-prune CronJob's ServiceAccount owns batch/jobs create/delete
  # (deploy/storage-cronjob.yaml). This Role intentionally omits batch/jobs.
  # The custom HPA-floor adjuster (#18) was removed in #77; horizontal
  # pod autoscaling is owned by the KEDA ScaledObject (deploy/keda-scaledobject.yaml).
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: openstudio-operator-rb
  namespace: openstudio-server
subjects:
  - kind: ServiceAccount
    name: openstudio-operator-sa
    namespace: openstudio-server
roleRef:
  kind: Role
  name: openstudio-operator-role
  apiGroup: rbac.authorization.k8s.io  
