# kind validation environment — OpenStudio Server 3.11.0 (issue #19, decision D13)

Reproducible recipe for standing up a single-node [kind](https://kind.sigs.k8s.io/)
cluster running the real `nrel/openstudio-server:3.11.0` stack, capturing REST
API fixtures against the operator's contract, and smoke-testing Phase 1 with
`dryRun: true`. This is the dev-machine half of D13; the work-cluster dry-run
runbook (#20) builds on it.

Everything here is **approximation-labeled**: it is strong enough to validate
the REST API contract and the operator's read paths, weak for NFS-eviction
testing (see [Approximations](#approximations-vs-production)).

## Prerequisites

- docker (daemon running)
- kind, kubectl — e.g.:

  ```bash
  curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.24.0/kind-linux-amd64 && chmod +x ./kind && sudo mv ./kind /usr/local/bin/
  curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && sudo install kubectl /usr/local/bin/kubectl
  ```

- curl, jq (fixture capture), python3 + `.venv` (`pip install -e '.[dev]'`)
- ~10 GB free disk (3.11.0 server image is ~2 GB compressed), outbound network
  to Docker Hub on the first run

## Quickstart

```bash
scripts/create-kind-cluster.sh          # idempotent; kind cluster 'os-operator-validation'
scripts/deploy-openstudio-stack.sh      # applies scripts/manifests/*.yaml, waits for rollouts
kubectl -n openstudio-server port-forward svc/web 8080:80   # then http://localhost:8080
```

First deployment pulls the images inside the kind node and boots Rails
(`wait-for-it` on `db`/`queue`, mongoid index creation) — expect **5–15
minutes** until `deployment/web` is Ready. On a slow link, pre-pull on the
host and load instead:

```bash
docker pull nrel/openstudio-server:3.11.0
kind load docker-image nrel/openstudio-server:3.11.0 --name os-operator-validation
# (same for mongo:6.0.7 / redis:6.0.9)
```

## What gets deployed (topology parity vs the helm chart)

Names are load-bearing — the operator targets them by fixed identifier
(AGENTS.md "Fixed identifiers"). All in namespace `openstudio-server`.

| Chart object (develop) | kind manifest | Parity notes |
|---|---|---|
| Deployment `db` (mongo:6.0.7) + Service `db:27017` | `scripts/manifests/01-mongo.yaml` | same image/creds (`openstudio`/`openstudio`, auth_source `admin`); persistence emptyDir |
| Deployment `redis` (redis:6.0.9) + Service `queue:6379` | `scripts/manifests/02-redis.yaml` | same image/password (`--requirepass openstudio`); persistence emptyDir |
| PVC `nfs-pvc` (RWX, storageClass `nfs`) | `scripts/manifests/03-nfs-hostpath.yaml` | **hostPath stand-in** on the kind node (`/tmp/openstudio-kind-nfs`) |
| Deployment `web` + Service `web:80` | `scripts/manifests/04-web.yaml` | image pinned **3.11.0** (chart default 3.8.0-1 is NOT the target); same command/env (`QUEUES=analysis_wrappers`, `REDIS_URL`, `MONGO_USER/PASSWORD`, `SECRET_KEY_BASE` chart default); mounts `nfs-pvc` at `/mnt/openstudio` |
| Deployment `web-background` | `scripts/manifests/05-web-background.yaml` | same command (`resque:workers` scaled by `COUNT`); `OS_SERVER_NUMBER_OF_WORKERS` tuned 30→2; mounts `nfs-pvc` |
| Deployment `worker` + HPA `worker-hpa` | `scripts/manifests/06-worker.yaml` | scaled-minimal: replicas **1**, HPA 1–2; `QUEUES=requeued,simulations`; emptyDir scratch at `/mnt/openstudio`; preStop hook + `terminationGracePeriodSeconds: 5200` kept |
| LoadBalancer, cluster-autoscaler, rserve, priority classes | — | intentionally omitted |

## Approximations vs production

- **hostPath for `nfs-pvc`** — fine for API validation and for exercising the
  `DELETE /analyses/{id}` server-side cascade (it rm-rf's real directories
  under `/mnt/openstudio/server/assets/...` on the stand-in volume, and
  `web`'s `rails-entrypoint` provisions them at boot). **Weak for
  NFS-eviction testing**: a hostPath never disappears, so stuck-mount /
  unmount-eviction scenarios cannot be reproduced. Contents live inside the
  kind node container: survive pod restarts, die with the cluster.
- **mongo/redis persistence is emptyDir** — data/queue state lost on pod
  restart; irrelevant for API shape validation, relevant if you restart
  mid-analysis.
- **No metrics-server** — `worker-hpa` never scales on CPU (stays at
  `minReplicas`). That is deliberate: the HPA exists for topology parity as
  the Phase-4 patch target (#18 `minReplicas` adjuster), not for load tests.
- **Single node, no node-group labels** — the chart's affinity
  (`nodegroup: web-group|worker-group`) is dropped; everything co-schedules.
- **web replicas pinned to 1, `strategy: Recreate`** — mirrors the
  single-writer assumption the chart makes (see values.yaml comment on
  distributed file locking).

## Fixture capture + drift check

```bash
kubectl -n openstudio-server port-forward svc/web 8080:80 &
scripts/capture_fixtures.sh --base-url http://localhost:8080          # reads only
scripts/capture_fixtures.sh --mutate                                   # + soft_stop/action/requeue (interactive WARNING)
scripts/capture_fixtures.sh --mutate --with-delete                     # + DELETE cascade (louder WARNING)
scripts/check_fixture_drift.py --live                                  # diff shapes vs tests/fixtures/contract-shapes.json
scripts/check_fixture_drift.py --samples                               # committed synthetic samples (no cluster needed)
```

`capture_fixtures.sh` writes one JSON envelope per endpoint
(`{endpoint, method, http_status, content_type, location?, body}`) to
`tests/fixtures/live/` and **fails loudly when the stack is unreachable**
(it will offer to start the port-forward itself, then refuse to run).
Per-analysis endpoints need an analysis to exist — creation routes are
**nested** (top-level `POST /analyses.json` 404s; `resources :analyses` is
`only: [:index]` at the top level). Minimal REST seeding, live-verified:

```bash
B=http://localhost:8080
PID=$(curl -s -X POST $B/projects.json -H 'Content-Type: application/json' \
      -d '{"project": {"name": "fixture-capture"}}' | jq -r ._id)
AID=$(curl -s -X POST $B/projects/$PID/analyses.json -H 'Content-Type: application/json' \
      -d '{"analysis": {"name": "fixture-capture-analysis"}}' | jq -r ._id)
DPID=$(curl -s -X POST $B/analyses/$AID/data_points.json -H 'Content-Type: application/json' \
      -d '{"data_point": {"name": "fixture-capture-dp"}}' | jq -r ._id)
```

This yields shape-complete fixtures (a fresh analysis has no `status` key
yet — see the drift findings). For richer states (`start_time` populated,
started datapoints with `ip_address`) upload a real seed model via the web
UI or the python client before re-running the capture. Commit the captured
files (acceptance: "fixtures committed") and record the drift result in the
section below.

### Drift findings (live-verified on kind, 2026-08-18, stack `3.11.0`)

Captured with `scripts/capture_fixtures.sh --mutate --with-delete` against a
minimal seeded stack (project → analysis → datapoint created via the nested
REST routes; commit `tests/fixtures/live/`).
`scripts/check_fixture_drift.py --live`: **10 pass, 2 error-shape, 0 fail**
after folding the findings below into `tests/fixtures/contract-shapes.json`.

1. **Unknown ids never 404.** `mongoid.yml` sets `raise_not_found_error:
   false`, so `Analysis#find` returns nil: `GET
   /analyses/{unknown}/page_data.json` answers **200 `{analysis: null}`**
   and `GET /analyses/{unknown}/status.json` answers **200
   `{analyses: []}`** (status uses `where()`, which never raises). The
   operator client must treat these bodies as not-found — a 404-based
   error branch would never fire.
2. **Raw docs omit nil fields — absent ≠ null.** A fresh analysis has **no
   `status` key at all** (state machine values appear only after the
   workflow advances); `start_time` likewise never appears in
   `/analyses.json` (forbidden in the shapes file). `page_data.json`
   (`as_json(only:)`) drops nil fields too — a minimal analysis serializes
   as only `{name, data_points, results, output_variables}`, so the **SLA
   anchor `start_time` is absent, not null, until the first job** —
   absence-tolerant access required. DataPoint docs, in contrast, carry
   `run_start_time` / `ip_address` / `job_id` as explicit `null`s before
   start.
3. **Ids are UUID strings**, not BSON ObjectIds, and raw docs expose `_id`
   only; the derived views (`status.json`, `data_points/status`) duplicate
   it as `id`.
4. **Content negotiation changes the status code.** `DELETE
   /analyses/{id}` returns **204** with `Accept: application/json` but a
   **302 HTML redirect** with curl's default `Accept: */*` (same for the
   `action`/`requeue` POSTs' HTML variants). The capture script pins
   `Accept: application/json` for these; `soft_stop` is captured with
   `*/*` because it is HTML-only by design.
5. **`requeue` on a jobless datapoint 500s.** A dp that was never
   queued/started has no Resque job; `POST /data_points/{id}/requeue`
   returns `500 {status: 500, error: "Internal Server Error"}` (JSON under
   JSON accept). Only requeue dps that have a `job_id`. The captured
   fixture intentionally documents this error shape.
6. **`action start` optimistically reports success.** On a seedless
   analysis it still returns `{code: 200, analysis: …}`; the failure
   surfaces later via the workflow (analysis resets to unset-status). Do
   not treat `code: 200` as "analysis will complete".
7. **`status.json` wrapping is count-based:** exactly one match →
   `{analysis: …}`; zero or many → `{analyses: […]}` (live-verified for 0
   and 1). `exit_on_guideline_14` renders as an integer.
8. **`soft_stop` redirects with an absolute URL** (e.g.
   `http://localhost:8080/analyses/{id}`) — don't parse it as a path.

Static-only findings (pre-live, still true): `soft_stop` has no
`format.json` (302 HTML is the whole contract surface); `action` carries
the real outcome in the body's `code` field over HTTP 200.

These findings are folded into `tests/fixtures/contract-shapes.json`
(`live_verified_global_rules`) — **follow-up needed in
`.agents/skills/_shared/api-contracts/openstudio-server-v3.11.0-rest.md`
(main checkout) to incorporate 1–6**; until then this file is the more
current truth. Value-level drift (timestamp formats, state membership
across a full run) still needs a capture against an analysis with a real
seed model — create one via the web UI or the python client, re-run
`capture_fixtures.sh`, and diff again.

## Phase 1 `dryRun: true` smoke walkthrough (feeds #20's runbook)

Intended sequence once the Phase-1 handler work (#8) is merged; #5, #6 and
#7 (config, client, CR-status store) are already in. Steps 1, 2 and 5 are
**live-verified** against the kind stack while preparing this doc (CRD/RBAC
apply + a `dryRun: true` OSCM CR accepted with defaults populated; client
reads succeed); steps 3–4 need #8's handler. Run from a checkout on the
dev machine with kubeconfig pointed at kind:

1. **Install the operator's K8s objects** (note: #3 fixes the manifests'
   stale `openstudio` namespace references — use its fixed versions or
   patch them to `openstudio-server`):

   ```bash
   kubectl apply -f deploy/crd.yaml
   kubectl apply -f deploy/rbac.yaml
   ```

2. **Create the OSCM custom resource in dry-run mode** — the CRD
   `spec.serverUrl` is the authoritative server URL (D-decision; the env
   var in `deploy/operator-deployment.yaml` stays an unreconciled
   placeholder):

   ```yaml
   apiVersion: energy.nrel.gov/v1alpha1
   kind: OpenStudioClusterManager
   metadata:
     name: validation
     namespace: openstudio-server
   spec:
     serverUrl: http://web.openstudio-server.svc.cluster.local
     dryRun: true
   ```

   One OSCM CR per namespace, oldest wins (D05) — keep exactly one.

3. **Run the operator locally** (no image needed; single process, no leader
   election by design):

   ```bash
   kopf run --module openstudio_operator.handlers --namespace openstudio-server -v
   ```

4. **Expected observations with `dryRun: true` (D11):**
   - the poller reads `GET /analyses.json` (~30 s cadence) against the live
     3.11.0 server — verify in `web` logs that the requests arrive;
   - NO mutating request (`soft_stop`, `action`, `requeue`, `DELETE`) is
     ever sent — check web logs / `kubectl -n openstudio-server port-forward
     svc/queue 6379` Resque keys if in doubt;
   - dry-run-marked Kubernetes Events are emitted on the CR when a policy
     would have fired (e.g. `AnalysisSoftStopped` dry-run flavour once #8
     lands);
   - operator memory changes land in the CR `.status` subresource only
     (D04): `kubectl get oscm validation -o yaml` after a few ticks.

5. **Fixture-adjacent smoke of the client layer** (no operator involved;
   verified against the live kind stack while preparing this doc):

   ```bash
   .venv/bin/python - <<'EOF'
   from openstudio_operator.openstudio_client import OpenStudioClient
   c = OpenStudioClient("http://localhost:8080")
   print(c.list_analyses())
   print(c.get_analysis_page_data("<an-analysis-id>"))
   EOF
   ```

   This exercises the UTC timestamp boundary and retry path against the
   real server (contract discipline, Q11). Mind the live findings above:
   on an unknown id the server answers 200 `{analysis: null}` and the
   client (live-verified) PASSES THAT BODY THROUGH without raising —
   callers must null-check the `analysis` key themselves.

6. **Flip `dryRun: false` only if** you want to observe a real soft-stop on
   a throwaway analysis in kind — never against any other cluster from this
   recipe. #20's runbook owns the work-cluster equivalent.

## Teardown

```bash
scripts/teardown-kind-env.sh   # deletes the whole cluster (mongo/redis/NFS stand-in data die with it)
```

## Troubleshooting

- `deployment/web` not Ready: check `kubectl -n openstudio-server logs
  deploy/web -c web` — the container `wait-for-it`-blocks on `db`/`queue`
  DNS; long image pulls are the usual delay. Readiness probe intentionally
  allows 90 s boot + 10 min of failures on first start.
- Mongo auth errors in web logs: the stack expects root credentials
  `openstudio`/`openstudio` (chart defaults) with `auth_source: admin` —
  they are baked into `01-mongo.yaml` and `04-web.yaml`; keep them in sync.
- Port-forward flakes: restart it; the capture script starts its own when
  the base URL is the default `http://localhost:8080`.
- Drift checker fails on a live fixture: read the printed `DRIFT:` lines —
  missing required keys or forbidden keys (`start_time` on raw analysis
  docs) mean the contract file needs updating, not the checker.
