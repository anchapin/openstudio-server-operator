# kube-apiserver audit-policy recipe for the operator's API surface (issue #399)

## Why this recipe exists

D04 makes the CR `.status` subresource the operator's audit trail, and D11
marks every suppressed mutation with a kopf Warning Event — but both of those
live at the operator's level, inside the cluster the operator manages. A
SOC2/PCI auditor asks the complementary question: **what did the
kube-apiserver itself see?** Until #399 there was no documented recipe to
turn on kube-apiserver audit logging for the resources this operator and its
storage-retention companion touch, and for a deployment that holds rotated
Redis + Mongo credentials and manages NFS eviction on `DELETE /analyses/{id}`,
that gap is material.

The mutating surface is exactly four kube-apiserver calls (verified against
the source, post-#395):

| kube-apiserver call | Code site | Executing identity | Trigger |
|---|---|---|---|
| `patch_namespaced_custom_object_status` | `src/openstudio_operator/status_store.py` (409-safe RMW write path) | operator SA and prune SA (both anchor status) | every status anchor: softStops, requeues, recycle/restart markers, `archivedAnalyses` |
| `patch_namespaced_deployment` | `src/openstudio_operator/_k8s.py::rolling_restart_deployment` — the shared post-#395 helper; called from `handlers/worker_recycler.py` and `handlers/web_background_monitor.py` | operator SA | worker recycle / `web-background` rolling restart |
| `delete_namespaced_pod` | `src/openstudio_operator/handlers/analysis_sla.py` (soft-stop escalation loop) | operator SA | grace-period expiry eviction of `app=worker` pods |
| `create_namespaced_job` / `delete_namespaced_job` | `src/openstudio_operator/retention.py` — manifest built by `archival.py::build_archival_job` with the deterministic `oscm-archive-<sanitized-id>-<sha256-8>` name | prune SA (`openstudio-storage-pruner-sa` — the operator Role has had no `batch` verbs since #78) | retention archival spawn / failed-Job retry cleanup |

The prune CronJob (`deploy/storage-cronjob.yaml`) runs the retention pipeline
under its own service account, so the `batch/jobs` rule below also records
its writes — for an auditor that is a feature, not noise: every Job that
touched NFS data appears in one place.

## The `audit-policy.yaml` fragment

Documentation-only by design (see the scope guard below): copy this into a
file on the machine that hosts your kind node, e.g. `audit-policy.yaml`
next to your kind config.

```yaml
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages:
  - "RequestReceived"     # one record per request, not two
omitManagedFields: true   # strip the managed-fields noise from RequestResponse bodies
rules:
  # 1. The operator's audit trail (D04): every read AND write of the OSCM CR
  #    and its .status subresource. kopf's watch on the CR is a long-running
  #    request and logs once per (re)connection at ResponseStarted — modest
  #    volume, and it captures the poller's identity, which an auditor wants.
  - level: RequestResponse
    resources:
      - group: "energy.nrel.gov"
        resources: ["openstudioclustermanagers", "openstudioclustermanagers/status"]

  # 2. Rolling restarts: worker recycle + web_background restart are merge
  #    PATCHes on the Deployment pod-template annotation (verbs scoped to
  #    patch so helm's own apply traffic stays at the Metadata catch-all).
  - level: RequestResponse
    verbs: ["patch"]
    resources:
      - group: "apps"
        resources: ["deployments"]

  # 3. Archival Jobs: create on retention spawn, delete on failed-Job retry
  #    cleanup (retention.py). resourceNames cannot scope this rule — see the
  #    note below — so it records every Job mutation in the namespace.
  - level: RequestResponse
    verbs: ["create", "delete"]
    resources:
      - group: "batch"
        resources: ["jobs"]

  # 4. Soft-stop escalation evictions: pod deletes by the operator SA. Note a
  #    human's `kubectl delete pod` matches this rule too — recording all
  #    deleters is the point of a server-side trail.
  - level: RequestResponse
    verbs: ["delete"]
    resources:
      - group: ""           # core API group
        resources: ["pods"]

  # Catch-all: everything else (lists, watches, events, logs) at Metadata
  # level only — request fingerprints without bodies.
  - level: Metadata
```

### Why the `batch/jobs` rule is not name-filtered

Audit-policy rules do support `resourceNames`, but it never applies to
`create` (there is no name in the request path to match — the name lives in
the body), and the archival Job names are deterministic yet unbounded (one
per archived analysis: `oscm-archive-<sanitized-id>-<sha256-8>`), so a static
allowlist cannot enumerate them. The rule therefore records `create`/`delete`
on **every** Job in the namespace and honest docs beat a false sense of
precision. If you need to isolate the archival subset when querying, filter
the audit records by `user.username`
(`system:serviceaccount:openstudio-server:openstudio-storage-pruner-sa`) or
by the `oscm-archive-` prefix on `objectRef.name` after the fact — the
`RequestResponse` bodies carry the full Job manifest, including the
`app.kubernetes.io/managed-by=openstudio-operator` labels that
`deploy/storage-cronjob.yaml`'s admission policy trusts.

## Enabling it on a kind cluster

kind has no `auditPolicy` field; the supported mechanism is to mount the
policy file into the control-plane node and patch kubeadm's API-server extra
args. Save the fragment above as `audit-policy.yaml` beside a kind config
like this (merge with `scripts/create-kind-cluster.sh`'s config if you build
on the repo recipe):

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraMounts:
      - hostPath: ./audit-policy.yaml        # resolved relative to where you run `kind create`
        containerPath: /etc/kubernetes/audit-policy.yaml
        readOnly: true
    kubeadmPatches:
      - |
        kind: ClusterConfiguration
        apiServer:
          extraArgs:
            audit-policy-file: /etc/kubernetes/audit-policy.yaml
            audit-log-path: "-"              # stdout → `docker logs`
```

`audit-log-path: "-"` sends audit records to the API server's stdout, so
they surface in the control-plane container's log stream — the cheapest
verification path on kind. For anything long-lived, log to a file instead
(`audit-log-path: /var/log/kubernetes/audit.log`) and add a second
`extraMounts` entry mounting that directory out to the host, plus
`audit-log-maxage`/`audit-log-maxbackup` rotation args.

Apply and (re)create:

```console
$ kind create cluster --config kind-audit.yaml --name oscm-audit
$ kubectl get --raw /readyz   # wait for the API server to come up
```

The `ValidatingAdmissionPolicy` objects from
`deploy/pod-delete-admission-policy.yaml` need K8s 1.30+ anyway, so the
`audit.k8s.io/v1` policy version is not an added constraint.

## Verifying the operator's calls appear

Two caveats before you grep:

- **`spec.dryRun: true` suppresses the calls entirely (D11)** — nothing
  reaches the kube-apiserver, so nothing can be audited. Verify with
  `dryRun: false` per the flip procedure in `docs/validation.md` §Phase D.
- kopf Warning Events (core `events`) are deliberately **not** in the four
  rules — the scope guard forbids changing event emission, and the
  Metadata catch-all already fingerprints them.

A zero-mutation smoke probe (rule 1 records reads too):

```console
$ kubectl -n openstudio-server get oscm -o yaml > /dev/null
$ docker logs "$(docker ps -qf name=control-plane)" 2>&1 \
    | grep audit.k8s.io/v1 | tail -1
{"kind":"Event","apiVersion":"audit.k8s.io/v1","verb":"list","user":{"username":"..."},...}
```

Then drive the four surfaces (the module tables in `docs/validation.md`
§Phase C and `docs/kind-validation.md` have the per-module procedures) and
confirm each rule fires — filter on `objectRef`:

```console
$ docker logs "$(docker ps -qf name=control-plane)" 2>&1 \
    | grep audit.k8s.io/v1 \
    | grep -E '"resource":"(openstudioclustermanagers|deployments|jobs|pods)"' \
    | grep -E '"verb":"(patch|create|delete)"' \
    | grep -oE '"username":"[^"]+"|"verb":"[^"]+"|"resource":"[^"]+"|"subresource":"[^"]+"|"name":"[^"]+"'
```

Expected: `patch` + `subresource=status` records under the operator and
prune SAs (`openstudioclustermanagers`), `patch` records on `deployments`
around each recycle/restart, `create` records for `oscm-archive-*` names in
`batch/jobs` under the prune SA, and `delete` records on `pods` under the
operator SA when a soft-stop escalates.

## Scope guard: the operator never installs this

The operator only ever mutates resources inside its namespaced Role; a
cluster-scoped audit policy (and the kube-apiserver restart applying it) is
a cluster-admin action. This doc is the recipe — no `deploy/` manifest
ships one, on purpose. If you need the audit trail to be provably present,
check for the API-server args from outside the repo:

```console
$ kubectl -n kube-system get pod -l component=kube-apiserver -o yaml \
    | grep -e audit-policy-file -e audit-log-path
```

Upstream references: [Auditing in Kubernetes](https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/)
(policy schema, levels, stages) and the [kind kubeadm config patches guide](https://kind.sigs.k8s.io/docs/user/configuration/#kubeadm-config-patches).
