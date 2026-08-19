# OpenStudio Server REST API Contract — verified against NREL/OpenStudio-server tag `v3.11.0`

Source of truth: Rails routes/controllers/models at `v3.11.0` (`server/` subdir of the repo).
Helm target: NatLabRockies/openstudio-server-helm `develop` branch, namespace `openstudio-server`.
Fixtures: to be captured from a live 3.11.0 instance (kind or work cluster) via
`scripts/capture_fixtures.sh` and diffed against `tests/fixtures/` (Q2 decision: source-derived
now, verified later).

The original source of this file lived outside the repo at
`.agents/skills/_shared/api-contracts/openstudio-server-v3.11.0-rest.md`
(gitignored — clones never saw it). It was vendored in-repo at this path by
issue **#48**, with the live-drift findings from issue **#19's** kind capture
folded in. Per-endpoint tables below carry inline "Live-verified (2026-08-18)"
callouts that reference §1–§5 in the "Live-verified server quirks" section.
The machine-readable distillation lives in `tests/fixtures/contract-shapes.json`
(`live_verified_global_rules`); the prose capture log is in
`docs/kind-validation.md` ("Drift findings (live-verified on kind, 2026-08-18)").

## Field types (live-verified 2026-08-18)

- **All ids are UUID strings** (Mongoid `key: :uuid`, configured in
  `config/mongoid.yml`), not BSON `ObjectId`. Raw Mongoid docs
  (`/analyses.json`, `/data_points.json`) expose the UUID as `_id` only;
  derived views (`status.json`, `data_points/status`) duplicate it as `id`
  alongside `_id`. See "Live-verified server quirks" §3.
- **Timestamps** (`created_at`, `updated_at`, `run_start_time`, `start_time`,
  `end_time`) are ISO 8601 with zone. The client normalizes to timezone-aware
  UTC at the boundary (see "Client discipline" below).

## Analysis endpoints

| Endpoint | Method | Notes |
|---|---|---|
| `/analyses.json` | GET | Raw Mongoid docs. Fields include `status`, `run_flag`, `created_at`, `updated_at` (Mongoid timestamps, ISO8601 with zone). **No derived methods** (`start_time` NOT included). **Live-verified (2026-08-18):** raw Mongoid docs OMIT nil fields — a fresh analysis has no `status` key at all until the workflow advances. `start_time` is forbidden here (derived only on `page_data`). See "Live-verified server quirks" §2. |
| `/analyses/{id}/status.json` | GET | Derived view: dp counts by status, `run_flag`, job statuses. **SLA clock anchor lives here** post-#83 D1 (operator's first sight of the analysis in the `started` state — written to CR `status.softStops[aid].issuedAt`; see Module 1 below). Live-verified (2026-08-18): the only endpoint that reliably reports the real analysis status; `page_data.start_time` and `analyses.json` raw-doc `status` are both unreliable on v3.11.0 (the former is absent until the first job runs; the latter is omitted when nil). Unknown ids yield **200 `{analyses: []}`** (not 404) — see "Live-verified server quirks" §1, §7. |
| `/analyses/{id}/page_data.json` | GET | `{analysis: {status, start_time, end_time, run_flag?, ...}}` — `start_time` is derived from first job. **No longer the SLA clock anchor** as of #83 D1: the `as_json(only:)` filter drops nil fields, so `start_time` is **absent (not null)** until the first job runs (a minimal analysis serializes as `{name, data_points, results, output_variables}` only). The SLA clock now anchors on `/status.json` first-sight; `page_data` is still useful for derived fields (point counts, output variables) but the clock anchor has moved. Unknown ids yield **200 `{analysis: null}`** (not 404). See "Live-verified server quirks" §1, §2. |
| `/analyses/{id}/action` | POST | Body param is **`analysis_action`** ∈ `start` \| `stop` — NOT `action`, NOT `soft_stop`/`kill`/`hard_stop`. `stop` = set run_flag false, wait for in-flight. |
| `/analyses/{id}/soft_stop` | GET | Cooperative stop that does NOT wait for in-flight runs (semantics roughly inverted vs `stop`). |
| `/analyses/{id}/stop` | GET | Stop waiting for last submitted run. |
| `/analyses/{id}` | DELETE | **Cascades**: `data_points dependent: :destroy` (each dp `after_destroy` rm-rf's its NFS asset dir), `before_destroy :queue_delete_files`. This IS the NFS cleanup for the asset tree. **Live-verified (2026-08-18):** returns **204** with `Accept: application/json`; without it, returns a **302 HTML redirect** — the operator's JSON client must always send `Accept: application/json`. See "Live-verified server quirks" §4. |

### Analysis state machine (v3.11.0)

`na → init → queued → started → post-processing → completed`

**There is no `stopping`, `stopped`, or `failed` state.** Stop/soft-stop are cooperative
`run_flag` boolean flips; the status does not change when they're invoked.

## Datapoint endpoints

| Endpoint | Method | Notes |
|---|---|---|
| `/data_points.json` | GET | `DataPoint.all`, **params ignored** (no server-side status filter!). Full docs — heavy payload. Includes `run_start_time`, `updated_at`, `ip_address`, `status`, `status_message`, `job_id`. |
| `/data_points/status?status=1&jobs=started` | GET | Light view `{data_points: [{_id, id, analysis_id, status, status_message}]}`. Rails quirk: presence of `status` param gates filtering; the filter value is read from `jobs`. **No timestamps** — pair with operator-tracked `startedSince` clock (Q6 decision). |
| `/data_points/{id}/requeue` | POST | 204 No Content. Destroys existing Resque job on `:requeue`/`:simulations` queues, re-enqueues on `:requeued` queue. Does NOT kill a wedged worker process (Module 1 escalation handles that). **Live-verified (2026-08-18):** requeue on a datapoint that has never been queued/started (no Resque `job_id`) returns **500** `{status: 500, error: 'Internal Server Error'}`. Only requeue dps that have a `job_id`. See "Live-verified server quirks" §5. |
| `/data_points/{analysis_id}/requeue_started` | POST | Bulk: requeues all dps of the analysis whose `status_message != 'completed normal'`. |

### Datapoint states: `na | queued | started | completed` (+ `status_message` detail)

## Live-verified server quirks (captured on kind, 2026-08-18, stack `3.11.0`)

Five facts not visible from the Rails source alone were surfaced by
issue **#19's** live kind capture (recipe in `docs/kind-validation.md`); they
were folded into this file by issue **#48**. The same rules live
machine-readably in `tests/fixtures/contract-shapes.json` under
`live_verified_global_rules`. Per-endpoint tables above carry inline
"Live-verified" callouts that reference the §numbers below.

1. **Unknown ids never 404.** `mongoid.yml` sets `raise_not_found_error:
   false`, so `Analysis#find` returns nil instead of raising:
   - `GET /analyses/{unknown}/page_data.json` → **200** `{analysis: null}`
   - `GET /analyses/{unknown}/status.json` → **200** `{analyses: []}`
     (status uses `where()`, never raises; count-based wrapping)
   - The operator client must treat these bodies as not-found; a 404-based
     error branch would never fire.

2. **Raw docs omit nil fields — absent ≠ null.** A fresh analysis has **no
   `status` key at all** (state values appear only after the workflow
   advances); `start_time` never appears in `/analyses.json` (derived only
   on `page_data`). `page_data.json` (`as_json(only:)`) drops nil fields too
   — a minimal analysis serializes as `{name, data_points, results,
   output_variables}` only. The historical SLA clock anchor
   `page_data.start_time` is **absent, not null**, until the first job —
   the only endpoint that reports the live analysis `status` is
   `GET /analyses/{id}/status.json` (issue #83 D1: the SLA clock anchor
   has moved to the operator-observed first sight of the analysis in the
   `started` state via `/status.json`, written to CR
   `status.softStops[aid].issuedAt`). DataPoint docs, in contrast, carry
   `run_start_time` / `ip_address` / `job_id` as explicit `null`s before
   start.

3. **Ids are UUID strings** (Mongoid `key: :uuid`), not BSON `ObjectId`. Raw
   docs expose `_id` only; derived views duplicate it as `id` alongside
   `_id`. See the "Field types" section above.

4. **Content negotiation changes the status code.** Endpoints with both
   HTML and JSON variants (`action`, `requeue`, DELETE) return the HTML
   variant unless the client sends `Accept: application/json`:
   - `DELETE /analyses/{id}` → **204** with `Accept: application/json`;
     **302 HTML redirect** without it.
   - `POST /analyses/{id}/action`, `POST /data_points/{id}/requeue` →
     JSON body over HTTP 200 with `Accept: application/json`; HTML
     redirect without it.
   - `soft_stop` is HTML-only by design (no `format.json` branch) — always
     302. The capture script (`scripts/capture_fixtures.sh`) pins
     `Accept: application/json` for the mutating endpoints; the JSON
     operator client must do the same.

5. **`requeue` on a jobless datapoint 500s.** A datapoint that was never
   queued/started has no Resque job; `POST /data_points/{id}/requeue`
   returns **500** `{status: 500, error: "Internal Server Error"}`. Only
   requeue dps that have a `job_id`.

## Non-existent / legacy (DO NOT USE)

- `GET /cluster.json` — **does not exist.**
- `kill` / `hard_stop` actions — **do not exist anywhere** in v3.11.0.
- `GET /compute_nodes.json` — exists but **legacy/unpopulated on K8s** (only `batch_run_analyses.rb` writes ComputeNodes; helm workers never register). Not a liveness source.
- `PUT /analyses/{id}/action` — wrong verb; route is POST.

## Queue fabric (Module 5 / Phase 4)

- Redis Service: `queue:6379` in ns `openstudio-server`; URL `redis://:openstudio@queue:6379` (password `openstudio` by default).
- Workers consume `QUEUES=requeued,simulations` (Resque).
- Queue depth: `LLEN resque:queue:simulations` / `LLEN resque:queue:requeued` (Resque 2.x key layout — live-verified 2026-08-18, issue #67; the bare `LLEN simulations` form reads a non-existent key and returns 0 forever). Worker liveness: Resque worker registry/heartbeats keys.
- Per-worker record: `GET resque:worker:{worker_id}` (STRING, JSON) — used by the Module 1 escalation path (issue #83 D2) to discover which Resque workers are currently processing a given analysis. The JSON's `payload.args` carries the job arguments (e.g. `[analysis_id, datapoint_id, ...]` for the OpenStudio Server `RunSimulateDataPoint` job class). Worker ids are `{hostname}:{pid}:{queues}` — the first colon-delimited segment is the K8s pod name (pods default `hostname` to the pod name), so the escalation can map a worker id to a pod for `kubectl delete pod`. **Pre-#83 escalation matched started-datapoint `ip_address` against worker pod `status.podIP`** — but on v3.11.0 datapoint `ip_address` is always null, so that path never matched (issue #83 D2: surgical pod eviction re-sourced to Resque worker identity).
- ResqueWeb mounted at `/resque` (HTML only).
- Admin levers (future use): `POST /admin/prune_resque_workers`, `POST /admin/requeue_failed`.

## Deployment topology (helm develop)

| Object | Name | Notes |
|---|---|---|
| Namespace | `openstudio-server` | manifests must be updated from `openstudio` |
| Web Service | `web` :80 | `serverUrl` default `http://web.openstudio-server.svc.cluster.local` |
| Worker Deployment | `worker` | HPA `worker-hpa` (CPU, 2–20, **unconditional** — no enable flag) |
| web_background Deployment | `web-background` | 1 replica |
| Redis Service | `queue` :6379 | |
| NFS PVC | `nfs-pvc` | RWX, storageClass `nfs`; mounted **only by web** at `/mnt/openstudio` |
| Worker scratch | emptyDir | `/mnt/openstudio` is node-local in workers; restart = cleanup (Module 3 "temp clearing" is moot) |

- Assets on NFS: `/mnt/openstudio/server/assets/{data_points,analyses}/...`
- Worker preStop: touch kill.worker, QUIT resque, ~50s wait, pkill leftovers. `terminationGracePeriodSeconds: 5200`. `safe-to-evict: "false"` (autoscaler won't recycle; explicit deletes still work).
- Server image contract: `nrel/openstudio-server` **3.11.0** (user override; chart default 3.8.0-1 is NOT the target).

## Client discipline (Q11 decisions)

- All timestamps normalized to timezone-aware UTC at the client boundary.
- Retries: 3 attempts, jittered exponential backoff (~1s/2s/4s) in-client, then raise; handler skips tick; idempotency anchored in CR status.
