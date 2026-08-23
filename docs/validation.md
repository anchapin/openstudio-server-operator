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

## Module status (what is live: operator handlers + state-carrying `deploy/` manifests)

The table below reflects the handlers that are loaded by `handlers/__init__.py`
on every operator boot plus every state-carrying guardrail/observability
manifest shipped under `deploy/` — the runbook's index of what is live. The
operator's own wiring (CRD, RBAC, Deployment, storage CronJob) is applied and
verified in the Phases below; the four namespace-hardening + alerting
artifacts are applied from the
[Namespace hardening + alerting artifacts](#namespace-hardening--alerting-artifacts-issues-400--112--166--468)
Prerequisites subsection (#587), which owns their apply steps and operational
caveats — cross-reference it; this table does not repeat them. The complete
`deploy/` inventory (including the credential-secret templates and
`namespace-labels.yaml`) is [`AGENTS.md`](../AGENTS.md)'s Layout bullet.
Earlier revisions of this runbook described modules 2/3/5 as "stubs" and
referenced the deleted `handlers/storage_pruner.py`; those lines predate the
#10/#11/#13/#78 merges and are no longer accurate.

| Module | Code path | State | Notes |
|---|---|---|---|
| 1 — Analysis SLA soft-stop | `src/openstudio_operator/handlers/analysis_sla.py` | **live** | SLA clock + escalation re-sourced to verified contract in #83/#96; LEGACY escalation seam trim tracked in #105 |
| 2 — Zombie datapoint watchdog | `src/openstudio_operator/handlers/datapoint_watchdog.py` | **live** | Requeue path for `started → jobless` datapoints |
| 3 — Worker recycler | `src/openstudio_operator/handlers/worker_recycler.py` | **live** | Idle Resque fence + pod-delete recycle; operator Surface-trim tracked in #104 |
| 4 — NFS archival + prune | `src/openstudio_operator/archival.py` (Job generator, in-process) → `src/openstudio_operator/retention.py` + `src/openstudio_operator/prune_entrypoint.py` (CronJob entrypoint) | **live** | Prune orchestration moved from operator to a dedicated CronJob in #78; operator Role lost `batch/jobs`. Live-cloud scheduling validation tracked in #101 |
| 5 — web_background stall detector | `src/openstudio_operator/handlers/web_background_monitor.py` | **live** | Reads Resque queue depth via `redis_client.stale_workers(...)`; pod eviction on stall; stall window anchored in CR status `stallWindowStartedAt` (#582) |
| Phase 4 — KEDA autoscaling | `deploy/keda-scaledobject.yaml` (ScaledObject + TriggerAuthentication) | **live** | Replaces custom Redis HPA-floor handler in #77; operator owns zero autoscaling surface and emits no `hpa_floor_adjustments_total` counter |
| Singleton guard (D05) | `src/openstudio_operator/singleton.py`, installed from `handlers/__init__.py` | **live** | Passive oldest-CR-per-namespace guard; Warning Event + loud log on second CR |
| Metrics endpoint + optional bearer-token authN (#401) | `src/openstudio_operator/metrics.py` (operator boot) + `src/openstudio_operator/prune_entrypoint.py::main` (prune CronJob, shared `start_metrics_server()`) | **live** | Plaintext `/metrics` on :9090 for BOTH the operator and the pruner; empty-by-default `OPENSTUDIO_METRICS_TOKEN_FILE` opt-in — when set, requests must carry `Authorization: Bearer <token>` and a missing/empty file fails CLOSED (401 for everything; pruner parity #478). Ingress is NetworkPolicy-gated (row below) |
| VAP pod-delete scope (#293) | `deploy/pod-delete-admission-policy.yaml` (cluster-scoped ValidatingAdmissionPolicy + Binding) | **live** | Narrows the operator SA's `pods/delete` to pods labeled `app=worker` — a constraint RBAC cannot express (`PolicyRule` has no `labelSelector`). Requires K8s 1.30+ (`admissionregistration.k8s.io/v1` GA); pre-1.30 clusters must skip it |
| VAP prune batch/jobs scope (#294) | `deploy/storage-cronjob.yaml` (embedded cluster-scoped ValidatingAdmissionPolicy + Binding) | **live** | Narrows the prune SA's `batch/jobs` create/update/delete to Jobs carrying both `app.kubernetes.io/managed-by=openstudio-operator` and `app.kubernetes.io/component=archival` and named `oscm-archive-*` (labels alone are spoofable — the name pattern from `archival.py::archival_job_name` is the second factor, #398). Requires K8s 1.30+; pre-1.30 clusters must skip it |
| VAP deployment-patch scope (#573) | `deploy/deployment-patch-admission-policy.yaml` (cluster-scoped ValidatingAdmissionPolicy + Binding) | **live** | Narrows the operator SA's `apps/deployments` patch/update to the names `worker` and `web-background` (the worker-recycler and web-background-restart surfaces) — RBAC's `resourceNames` cannot express a CR-configurable target. Admission presents HTTP PATCH as operation UPDATE (the operations enum has no PATCH literal, #565). Requires K8s 1.30+; pre-1.30 clusters must skip it |
| VAP secret-read scope (#572) | `deploy/secret-read-admission-policy.yaml` (cluster-scoped ValidatingAdmissionPolicy + Binding) | **live** | Constrains the operator SA's Secret *mutations* to `openstudio-redis*` names (VAPs cannot intercept GET — admission runs on the mutating path only); the read side is bounded by RBAC instead — the Role's `secrets: get` grant is exact-name-bounded via `resourceNames` to `openstudio-redis` (#606). Requires K8s 1.30+; pre-1.30 clusters must skip it |
| PriorityClass eviction protection (#414) | `deploy/priority-class.yaml` | **live** | Cluster-scoped `openstudio-operator-critical`, referenced via `priorityClassName` by BOTH `deploy/operator-deployment.yaml` and the prune CronJob — node-pressure eviction protection. Must exist before the Deployment (admission rejects pods naming a nonexistent PriorityClass); applied in Phase A step 1 |
| ResourceQuota + LimitRange (#400) | `deploy/resource-quota.yaml` | **live** | Bounds the namespace's aggregate + per-container resource surface; sized by #580 to the full KEDA burst envelope (recomputed by `tests/test_deploy_manifests.py` so drift fails CI). Admission-time only — running pods untouched. Apply steps + caveats: [Namespace hardening subsection](#namespace-hardening--alerting-artifacts-issues-400--112--166--468) |
| NetworkPolicy egress + metrics-ingress fence (#112/#166) | `deploy/network-policy.yaml` | **live** | Default-deny egress for the operator-owned pod surface (operator, storage-pruner, archival Jobs) plus label-scoped ingress lockdown of BOTH plaintext `/metrics` endpoints (operator #166; pruner parity #478; apiserver egress peer #578). The #166 scraper-namespace footgun and the #578 hosted-apiserver trap: [subsection](#namespace-hardening--alerting-artifacts-issues-400--112--166--468) |
| Alerting + discovery surface (#468/#682) | `deploy/prometheustrule.yaml` · `deploy/service-metrics.yaml` · `deploy/servicemonitor-metrics.yaml` · `deploy/grafana-dashboard.json` | **live** | PrometheusRule alerts transcribed from the `metrics.py` docstrings (incl. prune Job failed + absence-of-success alerts #569, singleton-unwrap rekey #570) + the Service/ServiceMonitor discovery pair fronting the operator's named port `metrics` (:9090) so the alerts have a scrape target (#682) + Grafana dashboard for the `/metrics` surface (drift-gated by `tests/test_monitoring_artifacts.py`). Cluster-admin apply, `release: prometheus` pickup label on both the rule and the ServiceMonitor — operator RBAC deliberately holds no `prometheusrules`/`servicemonitors`/`services` verbs. See the [subsection](#namespace-hardening--alerting-artifacts-issues-400--112--166--468) |

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
- The two cluster-scoped ValidatingAdmissionPolicies that narrow the
  operator / prune ServiceAccounts beyond what RBAC can express (RBAC
  `PolicyRule` has no `labelSelector`; `resourceNames` takes exact strings
  only). Apply them in this order alongside the Phase A operator manifests:
  1. `deploy/pod-delete-admission-policy.yaml` (#293) — operator SA's
     `pods/delete` restricted to pods labeled `app=worker`.
  2. `deploy/storage-cronjob.yaml` (#294) — prune SA's `batch/jobs`
     create/update/delete restricted to Jobs carrying both
     `app.kubernetes.io/managed-by=openstudio-operator` and
     `app.kubernetes.io/component=archival` and named `oscm-archive-*`
     (#398); the same manifest ships the
     prune CronJob with its SA/Role.

  Both policies are cluster-scoped and independent of each other; both
  are gated on Kubernetes 1.30+ — run the
  [K8s minor version gate](#k8s-minor-version-gate-130) at the end of
  this section before applying either. Pre-1.30 clusters must skip both
  and run with RBAC-only guardrails (see the Module status table above).
- Note (#69): the `:dev` publish pipeline was broken from #57 until the #69
  fix (README.md was excluded from the Docker build context, failing pip
  metadata generation). It is live again — future waves can pull the image
  directly.

### K8s minor version gate (1.30+)

Both admission policies above are `admissionregistration.k8s.io/v1`
objects, an API that is GA only on Kubernetes **1.30+**. Gate on the
server minor version before applying anything from this runbook:

```bash
kubectl version --short   # the Server Version line must read v1.30 or newer
```

- **1.30+** — apply both policies as written (the Prerequisites bullet
  above and Phase A step 1).
- **pre-1.30** — fail forward, do not abort: skip BOTH policies — omit the
  `deploy/pod-delete-admission-policy.yaml` apply in Phase A step 1, and
  strip the embedded VAP documents out of `deploy/storage-cronjob.yaml`
  before applying it. The operator and the prune CronJob run without the
  policies; what you lose is the label-scoped narrowing of `pods/delete`
  and `batch/jobs` verbs (#293/#294), so those ServiceAccounts stay as
  wide as their RBAC Roles allow.

### Corporate PKI: optional TLS CA bundle for the upstream server URL (issue #396)

The operator's REST client pins `verify=True` and honors the
`OPENSTUDIO_TLS_CA_BUNDLE` env var (`openstudio_client.py::
_resolve_tls_ca_bundle`, issue #296). Default: the env var ships as an
empty string and the client uses the system trust store —
`deploy/operator-deployment.yaml` applies and runs with **no Secret
present**. On a cluster whose `spec.serverUrl` traffic is fronted by a
corporate CA, enable the bundle in this order (the volumeMount + secret
volume ship commented-out in the manifest, mirroring the #401
metrics-token pattern — a live `secretName` reference to a missing
Secret would block pod scheduling):

1. Create the Secret **before** applying the uncommented Deployment:

   ```bash
   kubectl -n openstudio-server create secret generic openstudio-tls-ca-bundle \
     --from-file=ca-bundle.crt=/path/to/corporate-ca-bundle.pem
   ```

   The file must contain at least one `-----BEGIN CERTIFICATE-----` PEM
   block; the client validates this on first tick and aborts every tick
   with `OperatorConfigError` otherwise (the #296 fence).

2. In `deploy/operator-deployment.yaml`, uncomment BOTH `tls-ca-bundle`
   stanzas (the `volumeMounts` entry and the `volumes[].secret` entry)
   and set the env override to the in-pod path the mount produces:

   ```yaml
   env:
     - name: OPENSTUDIO_TLS_CA_BUNDLE
       value: /etc/openstudio/tls/ca-bundle.crt
   ```

   The mount name, secret name, mount path, and key→path mapping are
   pinned by `tests/test_deploy_manifests.py` (issue #396) — keep the
   manifest, the env value, and this runbook in sync.

3. Apply and verify the first tick reads the bundle:

   ```bash
   kubectl -n openstudio-server apply -f deploy/operator-deployment.yaml
   kubectl -n openstudio-server rollout status deployment/openstudio-operator
   kubectl -n openstudio-server logs deployment/openstudio-operator --tail=50
   ```

   A healthy first tick shows the usual timer-handler boot with **no**
   `OperatorConfigError` / `OPENSTUDIO_TLS_CA_BUNDLE` line. A
   set-but-broken value (Secret forgotten, stanza left commented, non-PEM
   file) fails loud on every tick — that abort is the #296 fence working,
   not a bug to work around; fix the offending step above.

Scope guard (issue #396): this bundle governs the **upstream
`spec.serverUrl` REST traffic only** — it does NOT change the operator's
own kube-apiserver connection and does NOT touch the rclone archival
credentials.

### Namespace hardening + alerting artifacts (issues #400 / #112 / #166 / #468)

Four more `deploy/` artifacts harden and observe the namespace the
operator runs in: `deploy/resource-quota.yaml` (#400, sized by #580),
`deploy/network-policy.yaml` (#112; metrics ingress #166; pruner parity
#478; apiserver peer fixed #578), `deploy/prometheustrule.yaml` (#468;
prune alerts #569, unwrap rekey #570) and
`deploy/grafana-dashboard.json` (#468). None of them is an operator
object — the operator's namespaced Role holds no verbs for any of the
four — so each is a cluster-admin apply done once alongside the Phase A
manifests (order between them is free; each is idempotent).
[`AGENTS.md`](../AGENTS.md) inventories all four in its `deploy/` Layout
bullet; this subsection is the operator-runbook spelling of their
operational caveats:

1. **ResourceQuota + LimitRange — `deploy/resource-quota.yaml` (#400,
   sized by #580)** — bounds the namespace's aggregate resource surface
   (requests 4 CPU / 8 Gi, limits 20 CPU / 20 Gi) and injects
   per-container defaults (requests 500m / 512Mi, limits 2 CPU / 4 Gi)
   for containers that omit resources:

   ```bash
   # Issue #400 — ResourceQuota (the aggregate namespace bound) +
   # LimitRange (the per-container defaults the quota's limits-tracking
   # depends on). Namespaced core/v1; no operator RBAC involved.
   kubectl apply -f deploy/resource-quota.yaml
   ```

   - Quota is admission-time only: running pods are untouched, but any
     NEW pod is rejected while the namespace sits at the bounds. The
     #580 sizing covers the full KEDA burst envelope at real requests
     (2.5 CPU / 6.375 Gi requests; 18.75 CPU / 19 Gi limits aggregate at
     `maxReplicaCount=5`) with headroom, and
     `tests/test_deploy_manifests.py::test_resource_quota_covers_keda_burst_envelope`
     recomputes the envelope from the manifests so drift fails CI — but a
     namespace running extra non-chart workloads must re-size first, or
     the next rescheduled chart pod (or the single-replica `Recreate`
     operator itself) is quota-rejected until something else terminates.
   - The LimitRange is load-bearing, not cosmetic: the chart pods
     (`web`, `web-background`, `worker`) declare `limits.memory` but NOT
     `limits.cpu`, and a quota that tracks limits REJECTS containers
     omitting them — do not delete the LimitRange while the quota is in
     place.

2. **NetworkPolicy — `deploy/network-policy.yaml`** — default-deny
   egress for the operator-owned pod surface (operator, storage-pruner,
   archival Jobs) plus ingress lockdown of BOTH plaintext `/metrics`
   endpoints (operator + storage-pruner, port 9090):

   ```bash
   # Issues #112/#166 — default-deny egress + explicit allows (DNS,
   # apiserver, web, Redis, storage HTTPS) and the two metrics-ingress
   # policies. Read the two caveats below BEFORE applying on this
   # cluster's CNI.
   kubectl apply -f deploy/network-policy.yaml
   ```

   - **The #166 footgun — edit the namespace label or `/metrics` goes
     silently unreadable.** Both metrics-ingress policies allow scraping
     ONLY from (a) a namespace labeled
     `kubernetes.io/metadata.name: prometheus` and (b) same-namespace
     pods labeled `app.kubernetes.io/component: metrics-scraper` (#295
     label convention). A cluster whose Prometheus runs in a
     differently-named namespace (`monitoring`,
     `kube-prometheus-stack`, `observability`, …) MUST edit the
     `namespaceSelector` label match in BOTH policies before applying —
     otherwise scraping is silently dropped (connection timeouts, no
     error event anywhere). This is exactly the trap AGENTS.md's
     [Working rules](../AGENTS.md) `/metrics`-ingress bullet warns
     about; never widen the same-namespace peer to `podSelector: {}`
     (the label-scoped selector is the regression fence behind
     `tests/test_deploy_manifests.py`).
   - **Enforcing-CNI apiserver trap (#578).** The operator's apiserver
     egress peer is the compound selector (kube-system namespace +
     `component: kube-apiserver` pods) — the self-hosted control-plane
     shape (kubeadm / kind / self-managed). Hosted control planes
     (EKS / GKE / AKS …) run the API server OUTSIDE the cluster, so the
     peer matches nothing there and operator apiserver traffic
     (kubeconfig watches, StatusStore RMW) is blackholed while the deny
     policy holds. Uncomment the `ipBlock` alternative in the manifest
     and set the API endpoint CIDR as narrowly as the endpoint allows.
     kindnet does not enforce NetworkPolicy, which is why the kind
     walkthrough can never catch this (see
     [Approximations](kind-validation.md#approximations-vs-production)).
   - On a CNI that does not enforce NetworkPolicy the apply is inert but
     harmless — the policy bites only where the CNI honors it
     (Calico / Cilium / …).

3. **PrometheusRule — `deploy/prometheustrule.yaml` (#468)** — the alert
   definitions transcribed from the `metrics.py` docstrings (handler
   tick failures, REST exception rate, heartbeat staleness, the #570
   singleton-unwrap `wrapped < expected` pair, the prune Job failed +
   #569 absence-of-success alerts):

   ```bash
   # Issue #468 — cluster-admin apply (the operator Role deliberately
   # holds no `prometheusrules` verbs); rename the `release:` label if
   # your kube-prometheus-stack release is not the stock `prometheus`.
   kubectl apply -f deploy/prometheustrule.yaml
   ```

   - Cluster-admin scope, no operator RBAC: the operator never creates
     or mutates PrometheusRule objects. The resource is namespaced
     (`monitoring.coreos.com/v1`) and lives in `openstudio-server` so a
     per-namespace Prometheus picks it up next to the scrape target.
   - The `release: prometheus` label matches kube-prometheus-stack's
     default `ruleSelector` (stock release name `prometheus`). A
     Prometheus installed under a different release name needs the label
     renamed (or the manifest's labels added to the selector); clusters
     not running the Prometheus Operator can transcribe the `expr`
     strings into static rule files — they are plain PromQL.
   - Scrape prerequisite (shipped since #682): every expression assumes
     a scrape config on the operator pod's plaintext `:9090/metrics`.
     Discovery is no longer hand-rolled — apply the shipped pair (a
     ClusterIP Service fronting the Deployment's named port `metrics`
     plus a ServiceMonitor wiring a Prometheus Operator scrape job to
     it, validated on a live kube-prometheus-stack cluster: endpoints
     populated → `up == 1` within one interval):

     ```bash
     # Issue #682 — cluster-admin apply (the operator Role deliberately
     # holds no `services`/`servicemonitors` verbs); rename the
     # `release:` label on the ServiceMonitor if your
     # kube-prometheus-stack release is not the stock `prometheus`
     # (same caveat as the PrometheusRule above).
     kubectl apply -f deploy/service-metrics.yaml -f deploy/servicemonitor-metrics.yaml
     ```

     Ingress stays gated by artifact 2 above — the scrape comes from
     the Prometheus server pod, which the metrics-ingress policy's
     `prometheus`-namespace peer already allows on the stock shape
     (only a differently-named Prometheus namespace needs the
     NetworkPolicy edit); without that ingress the target stays
     permanently down and the alerts remain empty. Clusters not
     running the Prometheus Operator can point a static scrape config
     at the Service's `openstudio-operator-metrics.openstudio-server.svc:9090`.
     The prune group's two
     alerts additionally require **kube-state-metrics** (standard in
     kube-prometheus-stack): they key on `kube_job_status_failed` and,
     since #645, the CronJob-recency absence-of-success
     `time() - kube_cronjob_status_last_successful_time …` (+ a
     never-succeeded `unless` bootstrap arm) — which needs
     kube-state-metrics **>= 2.5.0** (the series shipped in v2.5.0,
     upstream PR #1732; older KSM degrades that alert to
     schedule-staleness only).

4. **Grafana dashboard — `deploy/grafana-dashboard.json` (#468)** —
   panels for the action counters, tick-duration histograms and Resque
   queue-fabric gauges. Import via the Grafana UI (Dashboards → Import
   → upload the JSON); bind the `${DS_PROMETHEUS}` datasource input to
   the Prometheus instance scraping the operator's `:9090/metrics`
   (README.md#metrics) — without that binding every panel renders
   datasource-not-found. Panel queries mirror
   `tests/_metrics_inventory.py` and are drift-gated by
   `tests/test_monitoring_artifacts.py`, so a metrics-family rename
   fails CI instead of silently blanking panels.

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

## Deploy-time signature verification (#500)

CI proves the operator image is signed (ci.yml `cosign-verify-dev-image`,
#459), but nothing verified the signature where it matters — when the
digest-pinned image is applied to a cluster. As of #500,
`scripts/deploy-openstudio-stack.sh` closes that loop: before any
`kubectl apply`, it extracts the `image:` line from
`deploy/operator-deployment.yaml` (digest-pinned by release.yml, #149) and
runs `cosign verify` against the release workflow identity. If `cosign` is
not installed it prints a loud skip warning and continues (the kind dev flow
does not hard-require cosign); if cosign IS installed and verification
fails, the deploy aborts.

The exact manual command (same identity/issuer pair as ci.yml —
copy-pasteable for any production install):

```bash
cosign verify \
  --certificate-identity "https://github.com/anchapin/openstudio-server-operator/.github/workflows/release.yml@refs/heads/develop" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com" \
  "$(python3 -c 'import yaml; print(next(c["image"] for c in yaml.safe_load(open("deploy/operator-deployment.yaml"))["spec"]["template"]["spec"]["containers"] if c["image"].startswith("ghcr.io/")))')"
```

For production enforcement, a Kyverno `verifyImages` policy makes the
kubelet-side check unconditional (Kyverno ≥ 1.8; requires the Kyverno
admission webhook — a cluster-admin prerequisite like KEDA):

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: verify-operator-image-signature
spec:
  validationFailureAction: Enforce
  webhookTimeoutSeconds: 30
  rules:
    - name: verify-openstudio-operator-signature
      match:
        any:
          - resources:
              kinds: [Pod]
              namespaces: [openstudio-server]
      verifyImages:
        - imageReferences:
            - "ghcr.io/anchapin/openstudio-server-operator:*"
          attestors:
            - entries:
                - keyless:
                    subject: "https://github.com/anchapin/openstudio-server-operator/.github/workflows/release.yml@refs/heads/develop"
                    issuer: "https://token.actions.githubusercontent.com"
                    rekor:
                      url: "https://rekor.sigstore.dev"
```

Note the `subject`/`issuer` pair is the Kyverno spelling of the same
`--certificate-identity`/`--certificate-oidc-issuer` pair ci.yml encodes —
keep all three (ci.yml, deploy script, this policy) in sync. Releases cut
from tags would need the subject widened to
`...release.yml@refs/tags/v*` (as release.yml's own tag-verify step does).

## Phase A — deploy the operator with `dryRun: true`

1. **Install the operator's K8s objects** (post-#3 manifests; same as the
   [kind walkthrough](kind-validation.md#phase-1-dryrun-true-smoke-walkthrough-feeds-20s-runbook);
   the VAP apply below requires the
   [K8s minor version gate (1.30+)](#k8s-minor-version-gate-130) from
   Prerequisites):

   ```bash
   # Issue #498 — PSS `restricted` labels for the namespace (mirrors the
   # #388 kind recipe plus `warn`). Metadata-only manifest: kubectl apply
   # MERGES the labels onto the existing (helm-created) namespace and
   # replaces nothing. Chart pods satisfy `restricted` (#388 proof); an
   # environment running a workload that cannot comply must deliberately
   # downgrade per-environment, not drop the labels.
   kubectl apply -f deploy/namespace-labels.yaml
   kubectl apply -f deploy/crd.yaml
   kubectl apply -f deploy/rbac.yaml
   # Issue #293 — ValidatingAdmissionPolicy narrows the operator SA's
   # pods/delete to pods labeled app=worker (RBAC has no labelSelector).
   # Apply AFTER rbac.yaml so the operator SA referenced in the CEL rule
    # already exists at admission-evaluation time. Skip this apply on a
    # pre-1.30 cluster (RBAC-only guardrails).
    kubectl apply -f deploy/pod-delete-admission-policy.yaml
    # Issue #414 — cluster-scoped PriorityClass that BOTH
    # deploy/operator-deployment.yaml and deploy/storage-cronjob.yaml
    # reference via `priorityClassName: openstudio-operator-critical`.
    # Admission rejects pods naming a nonexistent PriorityClass
    # ("no PriorityClass with the name openstudio-operator-critical
    # found"), so it must exist BEFORE the operator Deployment (step 4
    # below) or the Module-4 prune CronJob creates any pod. Cluster-
    # scoped, so its order relative to the namespaced applies above is
    # otherwise free.
    kubectl apply -f deploy/priority-class.yaml
    ```

   The prune-side VAP (#294) has **no apply step of its own**: it rides
   inside `deploy/storage-cronjob.yaml` — the same manifest that ships the
   prune CronJob with its SA/Role (Prerequisites bullet above). When
   Module 4's storage pruning goes live,
   `kubectl apply -f deploy/storage-cronjob.yaml` installs the CronJob and
   the VAP in one stroke; on a pre-1.30 cluster strip the embedded VAP
   documents out of that manifest before applying it.

   The four namespace-hardening + alerting artifacts
   (`deploy/resource-quota.yaml`, `deploy/network-policy.yaml`,
   `deploy/prometheustrule.yaml`, `deploy/grafana-dashboard.json`) are
   applied from the
   [Namespace hardening + alerting artifacts](#namespace-hardening--alerting-artifacts-issues-400--112--166--468)
   Prerequisites subsection, not from this block: they harden and observe
   the namespace the operator runs in, and the operator's Role holds no
   verbs for any of them (#587).

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
