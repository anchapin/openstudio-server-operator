# First-time contributor onboarding

A walkthrough for first-time contributors and AI agents working in this
repository. **Read this once, then keep [`AGENTS.md`](../AGENTS.md) and
[`README.md`](../README.md) open side-by-side.** This file is the
"how do I work here without breaking invariants" guide; it complements
those two — it does not duplicate them.

If you only have five minutes, skim the [Quick start](#quick-start) and
the [Working rules that bite](#working-rules-that-bite-in-practice)
sections, then jump straight to the task at hand.

> **What this doc is NOT.** This is not an architecture overview — see
> [`docs/architecture-plan.md`](./architecture-plan.md). This is not a
> REST contract reference — see
> [`docs/contracts/openstudio-server-v3.11.0-rest.md`](./contracts/openstudio-server-v3.11.0-rest.md).
> This is not a deployment guide — see [`README.md`](../README.md). And
> this is not the agent-prompt manual for kopf/CRD/RBAC fixed identifiers
> — see [`AGENTS.md`](../AGENTS.md).

---

## Quick start

From a clean checkout, the minimum loop is:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
# Runtime deps come from the hash-pinned lockfile (#173) — verify hashes,
# then install the operator + dev extras. --require-hashes fails the install
# on the first byte mismatch, so a corrupted or stale `requirements.lock`
# shows up here before pytest can run.
pip install --require-hashes -r requirements.lock
pip install -e '.[dev]'

# guard against wave-orchestration drift (#71) — see "Venv drift guard" below
bash scripts/check_editable_install.sh

# pick ONE test file first, then narrow with -k
.venv/bin/pytest tests/test_status_store.py -k conflict_retry

# when confident, run the full suite
.venv/bin/pytest

# lint (line-length 100; E402 enforced via [tool.ruff.lint] extend-select, #507; CI pins ruff>=0.16)
ruff check .
```

After the first green run you are ready to read the layout, look at an
existing handler module as a template, and start editing.

### Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | **3.12** | `Dockerfile` pins `python:3.12-slim` by digest (`#113`); local dev should match. `pyproject.toml` allows `>=3.11` but 3.12 is what CI and the container image use. |
| `pip` | latest | used to install from `requirements.lock` (hash-pinned, `--require-hashes`) and then the editable package. |
| `requirements.lock` | pinned | the source of truth for runtime deps (`#173`) AND dev deps (`#298`). Every package is pinned to an exact version with `--hash=sha256:...` annotations. Generated with `pip-compile --extra=dev --generate-hashes --output-file=requirements.lock pyproject.toml` (requires `pip-tools`); see `requirements.lock` header for the exact invocation. The `tests/test_dependency_drift.py` CI gate fails the build if any `[project.optional-dependencies].dev` entry is missing its `--hash` line. Never edit by hand. |
| `requirements.txt` | pinned | the runtime-only lockfile (`#479`) the Dockerfile installs from — same `pip-compile` invocation as above but WITHOUT `--extra=dev`, so no dev tools ship in the production image. Refresh it in the same commit as `requirements.lock`. |
| `ruff` | **`>=0.16`** | pinned in `pyproject.toml` `[project.optional-dependencies].dev`. Local floor must match CI's floor — the `#68` incident (TRY004 + RUF059) was caused by an old local ruff that activated new CI rules. If `ruff --version` reports `<0.16`, upgrade before committing. |
| `pytest` | `>=8.0` | installed by `pip install -e '.[dev]'`. **Use the venv pytest, not the system one** — the system pytest cannot resolve the `openstudio_operator` package import and reports `ModuleNotFoundError` for almost every test. |
| `kubectl` + a cluster | for live validation only | `kopf run --module openstudio_operator.handlers` needs the CRD applied and the namespace `openstudio-server` reachable. The unit-test loop does NOT require a cluster. |
| `kind` (optional) | for the validation recipe | `scripts/create-kind-cluster.sh` + `scripts/deploy-openstudio-stack.sh` stand up a single control-plane node and the full helm overlay. See `docs/kind-validation.md` for the live-evidence runbook. |

Mocking dependencies are **pinned in `pyproject.toml`**: `responses` for
the OpenStudio REST API, `fakeredis` for the read-only Redis client
(`#12`), and `hypothesis` for the D12 timestamp boundary property tests
(`#246`). `hypothesis` is sanctioned for property-based boundary tests
only — it is not a fourth HTTP/Redis mock. Do not add a second HTTP or
Redis mocking library.

---

## Running tests

### Single file

The fastest feedback loop. Always start here before opening the full
suite:

```bash
.venv/bin/pytest tests/test_<module>.py -k <pattern>
```

Examples that exercise the cross-cutting contracts most cheaply:

```bash
# 409 retry semantics — D04's status store
.venv/bin/pytest tests/test_status_store.py -k conflict_retry

# Resque worker-identity path (post-#83 D2)
.venv/bin/pytest tests/test_analysis_sla.py -k resque_worker

# Redis read-only allowlist — #12
.venv/bin/pytest tests/test_redis_client.py -k readonly

# Singleton guard gate that fires when kopf internals change shape
.venv/bin/pytest tests/test_singleton_registry_coverage.py
```

The test IDs are descriptive by design — `pytest --collect-only -q
tests/<file>` will show you the names. Use them.

### Full suite

1128 tests across 50 files (run `.venv/bin/pytest --collect-only` to
re-verify the count before bumping `AGENTS.md`). Two seconds on a warm
cache; ten on a cold one. CI runs the same command under
`.github/workflows/ci.yml` job `test`.

### Coverage / lint

```bash
ruff check .                                    # lint
ruff check . --fix                              # auto-fix where safe
ruff format .                                   # (optional) formatter
```

There is no coverage gate in CI — the operator is small enough that the
focus is on cross-cutting invariants (D04 / D05 / D11 / D12) which are
verified by targeted tests, not by line coverage.

---

## Venv drift guard (#71)

Wave-orchestration worktrees often share one virtualenv across multiple
checkouts. When that happens, `pip install -e '.[dev]'` re-points the
venvs `openstudio_operator` import at whichever worktree installed
last, and `pytest`/`ruff` then run against the **wrong source tree**
producing phantom "pre-existing failures". CI is unaffected (fresh
runners); this check exists for local and wave workflows.

Before running tests or lint against a new checkout, either:

```bash
pip install --require-hashes -r requirements.lock  # re-pin runtime deps (#173)
pip install -e '.[dev]'                              # re-pin the venv to THIS checkout
# OR
bash scripts/check_editable_install.sh               # verify without re-installing
```

`scripts/check_editable_install.sh` exits `0` when the active venv
resolves `openstudio_operator` inside the current checkout, and exits
`1` with a `remedy:` line otherwise. Add it to the top of your local
test script.

---

## Stale worktree pre-flight check (#383)

Prior sessions can leave worktree directories under `../worktrees/`
whose branches were deleted on merge (or which belong to *other*
projects sharing the parent dir). `git worktree prune` cannot see them,
so they accumulate on disk and a new orchestrator may mistake the
leftovers for in-progress work. Run the visibility check BEFORE
creating new worktrees — Phase 0/0a of a wave cycle:

```bash
bash scripts/check_stale_worktrees.sh                       # list only (always safe)
bash scripts/check_stale_worktrees.sh --worktrees-dir /home/alex/Projects/worktrees
```

It prints one `STALE worktree: <dir> (no matching branch)` line per
`issue-*` directory backed by no local branch (`fix/` `feat/` `docs/`
`chore/` + dirname) and not registered in `git worktree list`, plus a
summary. If the count exceeds the threshold (default 5), a `WARNING`
line says to surface a Warning event and confirm with the user before
proceeding. List mode exits `0` and never deletes anything; deletion
is opt-in and confirmed:

```bash
bash scripts/check_stale_worktrees.sh --prune-stale-worktrees            # still list-only
bash scripts/check_stale_worktrees.sh --prune-stale-worktrees --yes \
    --min-age-days 30        # actually deletes stale dirs older than 30d
```

Regression tests: `tests/test_stale_worktree_check.py`.

---

## Adding a new OSCM timer handler module

This is the highest-traffic contributor workflow in this repo: every
new plan-module OSCM timer follows the same six-step pattern. Skip a
step and `tests/test_singleton_registry_coverage.py` will fail loudly
— that is the gate.

1. **Create the module** under `src/openstudio_operator/handlers/`
   (`analysis_sla.py`, `datapoint_watchdog.py`, `web_background_monitor.py`,
   and `worker_recycler.py` are the existing templates). Read one of them
   first — they are the canonical examples and the comments name the
   cross-cutting contracts (D04/D05/D11/D12) inline.

2. **Register exactly one `@kopf.timer`** for the OSCM resource using
   `@kopf.timer(**CRD_SPEC, interval=...)` with `CRD_SPEC` from
   `src/openstudio_operator/_constants.py` (the canonical CRD identity,
   issue #495 — no raw group/version/plural literals). The handler's `id` is what the
   singleton guard will look up. The timer body is a thin delegate to
   `run_oscm_tick` in `src/openstudio_operator/_oscm_handlers.py`
   (issue #473): the shared runner owns config parse, the
   empty-`serverUrl` idle check, store/emitter construction, the
   canonical `SKIP_TICK_EXCEPTIONS` tuple, the
   `HANDLER_TICK_FAILURES_TOTAL` increment, and the single skip-tick
   log line — your module supplies only a `wire` closure (its specific
   clients) and a `tick` closure (the `run_*_tick` invocation), plus
   any result-specific tail logging. Since #493 the `wire` call runs
   INSIDE the runner's guarded region, so a client-construction failure
   (kubeconfig load, TLS validation) gets the standard counted skip-tick
   instead of an uncaught kopf handler error. Do NOT copy a pre-#473
   ~35-line wrapper; the four existing modules are the canonical
   `wire`/`tick` templates.

3. **Register the handler in the Python-level OSCM registry** — the
   second, mandatory registration (issues #250/#285, `register_fn`
   convenience since #407). The import block (step 4) makes *kopf*
   see the timer; this call makes the Python-level `REGISTRY` in
   `src/openstudio_operator/_oscm_handlers.py` see it, and the
   singleton guard cross-checks the two registries at gate time.
   Import the `__name__`-introspecting convenience under the alias
   all four handler modules use, then make the one-line call the
   module's last statement (the live template is
   `src/openstudio_operator/handlers/analysis_sla.py`):

   ```python
   from openstudio_operator._oscm_handlers import (
       register_fn as _register_oscm_handler,
   )

   # ... the @kopf.timer handler above ...

   _register_oscm_handler(your_new_handler)
   ```

   The id is introspected from `fn.__name__`, so it cannot drift out
   of agreement with the kopf `id`; call `register(id, fn)` directly
   only when the id must differ from the function name (none of the
   production timers do). Forgetting this call is the bug
   `test_python_registry_includes_all_oscm_spawning_handlers` is
   designed to catch — the handler lands in the kopf registry but
   not the Python registry, and the test fails with `OSCM timer(s)
   registered with kopf but NOT in the Python-level registry:
   ['your_new_id']` — read that error, it is the spec.

4. **Add the module to the import block** in
   `src/openstudio_operator/handlers/__init__.py`. The import block is
   what makes `kopf run --module openstudio_operator.handlers` see the
   new timer; the `install_singleton_guard()` call at the bottom of that
   file then wraps it automatically — DO NOT try to wire the guard per
   module.

5. **Add the handler `id` to `EXPECTED_OSCM_TIMER_HANDLER_IDS`** in
   `tests/test_singleton_registry_coverage.py`. Forgetting this is the
   bug `test_all_oscm_spawning_handlers_are_singleton_guarded` is
   designed to catch — both the "added but not declared" and "declared
   but not added" directions.

6. **Write the targeted unit tests** under `tests/test_<module>.py`. Use
   `responses` for the OpenStudio REST surface and `fakeredis` for the
   queue-fabric client; do not introduce new mocking libraries.

```bash
# local gate that catches step-3 and step-5 mistakes BEFORE pushing
.venv/bin/pytest tests/test_singleton_registry_coverage.py -v
```

If you skip step 5, the test fails with `New OSCM spawning handler(s)
registered but not declared in EXPECTED_OSCM_TIMER_HANDLER_IDS:
['your_new_id']` — read that error, it is the spec.

### Do NOT add storage polling or autoscaling to a handler module

Both are explicit non-goals in this repo:

- **Storage pruning/archival** is owned by the `deploy/storage-cronjob.yaml`
  CronJob since `#78`. The operator process does not poll NFS; the
  retention pipeline runs as a native Job primitive via
  `openstudio_operator.prune_entrypoint`. Do not re-introduce a
  `storage_pruner` handler module.
- **Autoscaling** is owned by the KEDA ScaledObject in
  `deploy/keda-scaledobject.yaml` since `#77`. The operator's RBAC has
  no `horizontalpodautoscalers` verbs and the `hpa_floor` handler module
  is deleted. Do not re-introduce HPA-floor adjustment code.

---

## Working rules that bite in practice

These are the rules from [`AGENTS.md`](../AGENTS.md) that have caused
real regressions. They are the ones to re-read BEFORE opening a PR.

### Singleton guard internals (D05, #14, #47)

`openstudio_operator.singleton.install_singleton_guard` reaches into
kopf's private `registry._spawning._handlers` (kopf ≥ 1.37). That
internal is not part of kopf's public API: a kopf upgrade can rename or
move it, and the failure mode is **not** a crash — it is silently
unwrapped handlers (D05 enforcement quietly disabled). Three
defenses:

- `pyproject.toml` pins `kopf>=1.37,<1.45`. Bumping the upper bound is
  a deliberate act, not a side-effect of `pip install --upgrade`.
- `tests/test_singleton_registry_coverage.py` is the loud-fail CI gate
  that fails clearly when the internals change shape.
- The guard itself logs a warning naming `D05 enforcement disabled`
  when it cannot find the attribute; see
  `test_install_singleton_guard_emits_warning_on_missing_internals`.

If you upgrade kopf, expect to update both the upper bound AND the
test. Do not "fix" the silence by making the guard raise.

### `spec.redisUrl` empty-by-default guard (#116)

`spec.redisUrl` defaults to **empty by design**. The historical default
baked the kind-recipe password `openstudio` into every CRD; the operator
now refuses to operate and emits a per-CR `Warning` event
(`reason=RedisUrlEmpty`) when the field is empty AND no
`spec.redisCredentials.secretRef` is set. Helm-chart users must configure
one of the two paths below. **Do not "fix" the default** — it is the
regression fence. See `handlers/__init__.py` for the drain-queue pattern
kopf requires for events from non-event contexts.

### Redis credentials via Secret reference (#463) — recommended

`spec.redisUrl` is stored plaintext in etcd, returned verbatim to every
principal with `get/list` on the OSCM CR, and typically committed to
GitOps repos — an inline password leaked in three persistent places. The
**recommended production shape** keeps the credential out of the CR spec
entirely:

```yaml
spec:
  redisUrl: ""                      # empty — the #116 fence stays silent
  redisCredentials:
    secretRef:
      name: openstudio-redis        # must match ^openstudio-redis[a-z0-9-]*$
      key: redis-url                # key holding the FULL redis:// URL
```

The named Secret key must hold the **complete** URL
(`redis://:password@queue:6379`) — full-URL semantics, not the bare
password; this avoids URL-reconstruction logic in the operator and matches
the value the helm recipe already templates into the web / worker
`REDIS_URL` env vars. TLS-enabled Redis works with the `rediss://` scheme
(Azure Cache / Memorystore / TLS-only ElastiCache): accepted by the same
CRD and Secret patterns, connected with certificate verification against
the system trust store by default — an explicit CA bundle can be pinned
via the `REDIS_TLS_CA_BUNDLE` env var on the operator (issue #476).
Semantics:

- **Preferred over inline** — when both `secretRef` and a (grandfathered)
  inline `redisUrl` are present, the Secret wins.
- **Resolution** happens in `client_factory.get_read_only_redis_client`:
  one namespaced `CoreV1` get of exactly the named Secret (the operator's
  single bounded "never reads Secrets" exception — documented in
  `deploy/rbac.yaml`; the grant is `get`-only). The resolved URL is
  fence-checked against the in-cluster `redis://` pattern (#390 SSRF fence
  preserved; credentials allowed because carrying them is the point).
- **Failures** (`RedisCredentialResolutionError`: Secret missing, key
  missing, invalid value) are not cached — the next call re-resolves, so a
  Secret created after the first attempt is picked up on the next tick.
- **Cache key** — the `(redisUrl, secretRef, namespace)` tuple; changing
  the secretRef name/key yields a fresh client. An in-place password
  rotation keeps the cached client on the old URL until the operator
  restarts (same runbook step the inline path needs — rotation also
  touches every `REDIS_URL` env consumer; see
  `scripts/rotate_redis_password.sh`).

Since #463 the CRD pattern for `spec.redisUrl` **rejects embedded
credentials** (`@` userinfo) at apply time; `redis://queue:6379`
credential-free stays valid for no-auth dev clusters, and existing CRs
with pre-#463 inline passwords are grandfathered (Kubernetes does not
re-validate stored objects) — rotate them to the secretRef shape at the
next spec edit.

### `spec.serverUrl` is the only server URL (`#3`)

The CRD `spec.serverUrl` is the authoritative server URL
(`config.py` reads it). The `OPENSTUDIO_SERVER_URL` env var is gone —
single config path enforced. If you find yourself reaching for an env
var, stop.

### Every mutating action is gated by `spec.dryRun` (D11)

Default `false`; when `true`, the mutation is suppressed and a
dry-run-marked Kubernetes Event is emitted instead. The cross-cutting
audit at [`docs/audit-dryrun-idempotency.md`](./audit-dryrun-idempotency.md)
enumerates every mutating call site (R1–R6, K1–K6). **If you add a new
mutation, that audit needs a new row and a new gate.**

Two non-negotiables when adding a mutation:

- Gate it identically to the surrounding mutations (see the existing
  `dry_run = config.dry_run` / `if not dry_run:` pattern in each handler).
- Write a regression test in `tests/test_dryrun_walkthrough.py` (or a
  module-specific dry-run test) that flips `dryRun: true` and asserts
  the mutation is suppressed AND the Event is emitted.

### kopf version pin (`>=1.37,<1.45`)

See the singleton-guard note above. The pyproject comment block
documents the why; do not delete it during a "cleanup" pass.

### Operator memory lives in `CR .status` only (D04)

Maps `softStops`/`requeues`/`startedSince`/`archivedAnalyses` plus
scalars `lastRecycleAt`/`lastWebBackgroundRestart`/`stallWindowStartedAt` (#582). In-memory state is
cache, never source of truth. The status store handles 409 retries
internally — do not build a parallel retry path on top of it.

If your handler DOES need a module-level per-CR cache (a presentation
dedup set, a sustained-window clock), follow the #497 convention
documented with a census in `src/openstudio_operator/_cr_cache.py`:
key it by `(namespace, name)`, UID-validate the entry (a different
`metadata.uid` means the singleton CR was deleted and recreated under
the same name — start fresh), and expose the uniform
`reset_per_cr_caches(namespace=None, name=None)` seam.
`tests/test_cache_keying_convention.py` fences the census in both
directions (cache-bearing modules must have the seam; cache-free
modules must not).

### REST contract trapdoors (D12, `docs/contracts/...`)

Verified against v3.11.0:

- No `PUT action`, no `kill`/`hard_stop`, no `/cluster.json`.
- Unknown ids never 404; raw docs omit nil fields.
- Ids are UUIDs.
- `DELETE /analyses/{id}` 302s without `Accept: application/json`.
- `requeue` 500s on jobless datapoints — guard the call site.
- Analysis states: `na → init → queued → started → post-processing →
  completed`. **No `stopping`/`failed`**. Grace-period waits anchor on
  CR `.status` timestamps, never server state.
- The queue fabric is Redis (Service `queue`, Resque queues `simulations`
  + `requeued`). The operator reads it directly, **read-only** (`#12`).

Full ground truth: [`docs/contracts/openstudio-server-v3.11.0-rest.md`](./contracts/openstudio-server-v3.11.0-rest.md).

### Redis password is not a fixed literal (#150)

The kind recipe (`scripts/manifests/02-redis.yaml`) and
`deploy/redis-credentials-secret.yaml` no longer ship the
publicly-known `openstudio` literal. Since #462 the deploy/ Secret ships
only the unusable sentinel `CHANGE_ME_RUN_ROTATE_SCRIPT` (never a
working credential); the kind-recipe manifests keep the rotated
placeholder `openstudio-rotated`. The matching `REDIS_URL` env vars in
the manifests for `web`, `web_background`, and `worker` were rotated in
lockstep.

Fresh installs MUST run `scripts/rotate_redis_password.sh` first —
it generates a per-cluster 32-character random password, substitutes
it into the five manifests at apply time, and updates the live
`openstudio-redis` Secret so the helm chart's `web` / `web_background`
/ `worker` Deployments pick it up. Since #499 the script writes the
password to a 0600 file (default `./rotated-redis-password.txt`, or
`--out-file PATH`) and prints only the path — stdout carries no secret
material; `--print-only` is the explicit escape hatch whose help text
documents the scrollback/CI-log exposure trade-off. The companion CI
guard `scripts/check_redis_password_unique.sh` fails the build if the
legacy `openstudio` literal re-appears as a Redis password in any of
the five manifest files (#150 was a real incident; this is the
regression fence), and — since #462 — if
`deploy/redis-credentials-secret.yaml` ships anything other than the
unusable sentinel `CHANGE_ME_RUN_ROTATE_SCRIPT` as its committed
password (the renamed `openstudio-rotated` placeholder was itself a
publicly-known credential). The same rules hold for Mongo via
`scripts/rotate_mongo_password.sh` (same #499 file-based output,
default `./rotated-mongo-password.txt`) +
`scripts/check_mongo_password_unique.sh` (#219/#462).

### `/metrics` ingress is namespace-scoped (#166)

The plaintext Prometheus endpoint (`metrics.py`'s
`prometheus_client.start_http_server(port, addr="0.0.0.0")` +
`deploy/operator-deployment.yaml` `containerPort: 9090`) has **no
authN, no authZ, no TLS**. `deploy/network-policy.yaml` therefore
ships an Ingress policy `openstudio-operator-metrics-ingress`
(`policyTypes: [Ingress]`) that allows TCP/9090 to the operator pod
only from:

- a namespace labeled `kubernetes.io/metadata.name: prometheus` (the
  default scraper namespace for stock `kube-prometheus-stack`), AND
- a same-namespace peer in `openstudio-server` that opted in via the
  `app.kubernetes.io/component: metrics-scraper` label — the #295
  convention, next rule.

A cluster whose Prometheus runs in a differently-named namespace
(`monitoring`, `kube-prometheus-stack`, `observability`, etc.) MUST
edit the `namespaceSelector` label match in that policy before
applying. Otherwise the operator's metrics (queue depths,
status-conflict retries, `resque_workers_seen_max`,
`status_map_caps_total`, the dry-run gate counters) **silently
become unreadable** — there is no other failure signal.

The enforcement test
[`tests/test_deploy_manifests.py::test_network_policy_metrics_ingress_has_prometheus_and_peer_allow`](../../tests/test_deploy_manifests.py)
asserts both peers are present, that the same-namespace peer is
label-scoped (a return to `podSelector: {}` fails CI), and fails on
either removal. The
unrelated no-custom-autoscaling rule above (#77) keeps the operator
process from owning any autoscaling surface; #166 governs which
**external** scrapers can reach the `/metrics` endpoint it does
emit.

### `metrics-scraper` label convention (#295)

The AGENTS.md Working-rules bullet of the same name defines the label
behind the #166 policy above: `app.kubernetes.io/component:
metrics-scraper` is the project's opt-in marker for pods that
legitimately need to scrape the operator's `/metrics` endpoint from
inside `openstudio-server` (a Prometheus sidecar, an in-cluster debug
scraper). No helm-chart pod in this stack carries that value — `web` /
`web-background` / `worker` / `db` (Mongo) / `redis` / `queue` / NFS
all set their own `app.kubernetes.io/component` — so the label-scoped
peer allow admits exactly the pods that opted in.

When adding a new in-cluster scraper, set
`app.kubernetes.io/component: metrics-scraper` on its pod template;
that single label is the contract the NetworkPolicy trusts. Two
failure modes:

- **Forgetting the label is silent.** The NetworkPolicy drops the
  TCP/9090 connection and the scraper reads nothing — no Event, no
  operator log line.
- **"Fixing" that by widening the selector is loud.** Reintroducing
  `podSelector: {}` (or any broader selector) re-opens the plaintext
  endpoint to every helm-chart pod and fails
  [`tests/test_deploy_manifests.py::test_network_policy_metrics_ingress_has_prometheus_and_peer_allow`](../../tests/test_deploy_manifests.py)
  in CI.

### Operator pod-delete VAP (#293)

RBAC `PolicyRule` has no `labelSelector` slot: `verbs: [delete]` on
`pods` cannot be constrained to a subset of pods by label. The AGENTS.md
ValidatingAdmissionPolicies bullet (issues #293, #294) fills the gap at
the admission layer. `deploy/pod-delete-admission-policy.yaml` is a
cluster-scoped `ValidatingAdmissionPolicy` +
`ValidatingAdmissionPolicyBinding` that rejects any `DELETE pod`
request from the operator ServiceAccount
(`system:serviceaccount:openstudio-server:openstudio-operator-sa`)
unless the target pod carries `app=worker` — exactly the eviction path
`worker_recycler.py` uses. The CEL short-circuit leaves every other
actor (humans via `kubectl`, the prune SA) unrestricted; only the
operator SA is narrowed. `failurePolicy: Fail` keeps a broken CEL
expression from failing open.

Failure modes:

- A new operator code path that deletes a NON-worker pod passes RBAC
  and is then rejected at admission — that is the fence working. Point
  the delete at `app=worker` pods, or extend the policy deliberately.
- Editing the manifest (dropping the worker-label check, flipping
  `failurePolicy: Fail`) fails the `test_pod_delete_admission_*`
  family in `tests/test_deploy_manifests.py` — notably
  [`test_pod_delete_admission_policy_validations_check_operator_sa_and_worker_label`](../../tests/test_deploy_manifests.py)
  and `test_pod_delete_admission_policy_failure_policy_is_fail`.
- **Requires K8s 1.30+** (`admissionregistration.k8s.io/v1` GA); on a
  pre-1.30 cluster the manifest fails to apply — skip both VAPs there.

### Prune CronJob batch/jobs VAP (#294)

The second half of the AGENTS.md ValidatingAdmissionPolicies bullet:
`deploy/storage-cronjob.yaml` embeds a second cluster-scoped
`ValidatingAdmissionPolicy` + `ValidatingAdmissionPolicyBinding`
(`openstudio-prune-job-scope`) that narrows the prune SA's
`batch/jobs create|update|delete` verbs to Jobs carrying BOTH
`app.kubernetes.io/managed-by=openstudio-operator` AND
`app.kubernetes.io/component=archival` — the labels `archival.py`
stamps on every archival Job — AND named `oscm-archive-*`
(`metadata.name.startsWith("oscm-archive-")`, the deterministic-name
convention of `archival.py::archival_job_name`; labels alone are
spoofable by the very SA the policy constrains, #398) (the CEL checks
`object` for CREATE/UPDATE and `oldObject` for DELETE). RBAC still
grants the verbs; the VAP does the narrowing RBAC cannot express. Same
K8s 1.30+ requirement and `failurePolicy: Fail` stance as #293.

Failure modes:

- **Label drift between `archival.py` and the policy is the nasty
  one.** The policy stays syntactically valid but rejects the
  operator's own archival Jobs at CREATE — the retention pipeline
  stalls. The regression fence
  [`tests/test_deploy_manifests.py::test_prune_job_scope_vap_label_keys_match_archival_manifest`](../../tests/test_deploy_manifests.py)
  builds a real Job via `build_archival_job` and asserts the VAP's
  label pairs match what `archival.py` emits.
- Relaxing the CEL validations (dropping a label requirement, the
  `oscm-archive-` name-pattern clause (#398), or the `oldObject`
  DELETE-side clause) fails
  `test_prune_job_scope_vap_validations_require_archival_labels` and
  the rest of the `test_prune_job_scope_vap_*` family.

### Other rules worth knowing

- **Policy values** belong in `config.py` / the CRD `spec`. No hardcoded
  constants in handlers — there is a literal scan in the audit doc.
- **`deploy/operator-deployment.yaml`** is single-replica with
  `strategy: Recreate`. Do not scale replicas or switch to
  `RollingUpdate` without adding leader election.
- **The operator never reads secrets.** rclone archival jobs receive
  credentials via `envFrom secretRef` only.

---

## Updating the audit doc

[`docs/audit-dryrun-idempotency.md`](./audit-dryrun-idempotency.md) is
the contract that gates any non-dry-run work-cluster exposure. When you
change a D11 or D04 contract you MUST update it in the same PR. The
rows it tracks are mechanical:

- **Adding a mutating call site** → new R/K row with gate file:line,
  dry-run Event substitution, status `GATED`. Include the regression test.
- **Removing a mutating call site** → mark the row `REMOVED (#NN)` and
  link the issue.
- **Changing an idempotency anchor** → update §2 ("anchor proofs per
  module") and re-run the relevant `tests/test_<module>.py -k
  <anchor>` test.
- **Adding a new exempt category** → §1.3 with the rationale (the
  existing rows are `CR .status writes`, `K8s Events`, `Prometheus
  counters`, `operator self-wiring`).

The CI gate that catches drift here is the lint+test job; there is no
specific audit-doc test, but the maintainers re-verify it on every
release-PR.

---

## Branching & PR conventions

The canonical, quick-reference summary of branch naming, PR-body
keywords, merge-subject hygiene (#88), and required CI checks now
lives in [`CONTRIBUTING.md`](../CONTRIBUTING.md) — start there.
`AGENTS.md` also carries a one-paragraph branching-model pointer at
the top of the file.

Two project-specific notes that are NOT in `CONTRIBUTING.md` because
they are maintainer escape hatches, not contributor paths:

- The `guard-branch-pairing` job is a required status check on
  **`main`** (not `develop`); its name is part of the public CI
  contract — do not rename it.
- When GitHub Actions is unavailable, the maintainer may push via
  admin bypass after locally verifying `ruff + pytest` green on the
  branch (and `docker build` when Release/Docker files are touched).
  A retroactive CI run on `develop` HEAD follows once runners
  recover. This is a maintainer escape hatch, not a contributor
  path.

Use a lowercase, hyphenated slug that names the change. The
issue-number prefix lets `gh pr merge` produce a clean closing keyword
on squash-merge without further edits.

### PR body

The PR body MUST include `Closes #N` (or `Fixes #N` / `Resolves #N`)
when the issue SHOULD close on merge, and `Refs #N` (or `for #N` /
`touches #N`) when it should stay open. See the merge-subject hygiene
section below — `develop` closes issues from PR **bodies** too, so the
keywords matter in BOTH places.

### Scope guard (issue #301)

Every PR body MUST also carry a `Scope guard:` block. This is the
counterpart to the keyword rule above: keywords tell GitHub which
issues to auto-close, but they say nothing about **what this PR
intentionally does NOT touch**. The scope guard makes that explicit so
reviewers can verify the change stays within its lane and so follow-up
work is unambiguously assigned.

The rule was first stated in the PR/branch-conventions prose and
finally documented here (issue #301). Before #301 the convention was
referenced but never defined; new contributors (and the
auto-improvement-loop / wave-orchestrator) produced PRs that satisfied
the keyword rule but violated the unspoken scope rule. The
[`scripts/check_pr_body_scope.sh`](../scripts/check_pr_body_scope.sh)
CI gate (wired into the `lint` job) fails the build when the block is
missing, empty, lacks an issue reference, or contains no rationale.

**Required content of the `Scope guard:` line:**

1. The issues this PR actually touches (`#N`, comma-separated, with a
   one-line rationale per issue — usually just the issue title is
   enough).
2. A statement of what is intentionally **not** being changed in this
   PR. Use one of these forms:
   - `Do NOT touch <area>; #M owns that.` (preferred when the area is
     already tracked by a follow-up issue),
   - `<area> is out of scope; tracked by #M.` (when the rationale needs
     more context),
   - `<area> is unchanged in this PR.` (when the area is intentionally
     left alone but no follow-up issue exists yet).
3. At least one of the rationale phrases `Do NOT`, `do not`, `owns`,
   `out of scope`, `not in scope`, `do not modify`, `unchanged`, or
   `untouched` — the lint script greps for one of these so a bare
   `Scope guard: #301` line is rejected.

**Canonical example** (closing PR that touches one issue + defers
another):

```markdown
Closes #301

## What this changes

- Adds `scripts/check_pr_body_scope.sh` and wires it into the `lint`
  CI job.
- Documents the scope-guard rule in `AGENTS.md` and
  `docs/onboarding.md`.

## What this does NOT change

- Scope guard: Do NOT touch any of the four OSCM timer handler modules
  (`analysis_sla`, `datapoint_watchdog`, `worker_recycler`,
  `web_background_monitor`); #297 owns that refactor.
- Scope guard: Do NOT touch `CONTRIBUTING.md` (it does not exist yet);
  #302 owns adding it.
```

The two `Scope guard:` lines name the areas intentionally excluded and
the follow-up issues that own them — a reviewer can verify at a glance
that nothing leaked past the stated scope.

**Why a separate rule from #88?** The merge-subject hygiene rule
(#88) governs closing/keep-open keywords; it runs against commit
subjects. The scope-guard rule (#301) governs PR bodies and the
*intent* of the change — it is a content review guard, not a keyword
parser. Both rules run independently: a PR with perfect keywords and
no scope guard still fails CI on this gate.

**Running the check locally:**

```bash
# Pipe a PR body from gh:
gh pr view 301 --json body -q .body \
  | bash scripts/check_pr_body_scope.sh -

# Or check a saved file:
bash scripts/check_pr_body_scope.sh --file /tmp/pr_body.md
```

### Merge-subject hygiene (#88)

`develop` is the default branch, so GitHub closes an issue when **any**
commit with a closing keyword lands there — including auto-generated
squash-merge subjects. Two consequences:

- For **keep-open PRs**, keep closing keywords OUT of the PR title AND
  out of every commit subject on the branch. Use `Refs #N` /
  `for #N` / `touches #N` everywhere.
- For **closing PRs**, the closing keyword IS desired in both the
  squash subject (`fix: resolve #N — …`) and the PR body
  (`Closes #N`).
- Always merge with an explicit subject override rather than relying on
  the auto-generated one:

  ```bash
  gh pr merge N --squash --subject "fix: resolve #N — short summary" --body "Closes #N"
  ```

This is the same rule the maintainer applies by hand; documenting it
here so contributors do not accidentally close issues via
auto-generated subjects.

### Required CI checks

The `require-ci-on-develop-prs` ruleset requires two jobs to pass on
PR merges into `develop`:

- `lint` — `ruff check .`
- `test`  — `.venv/bin/pytest`

The `guard-branch-pairing` job is a required status check on **`main`**
(not `develop`); its name is part of the public CI contract — do not
rename it.

When GitHub Actions is unavailable, the maintainer may push via admin
bypass after locally verifying `ruff + pytest` green on the branch (and
`docker build` when Release/Docker files are touched). A retroactive CI
run on `develop` HEAD follows once runners recover. This is a maintainer
escape hatch, not a contributor path.

---

## Good first PR candidates

These are the surfaces where a smaller PR can land without touching the
cross-cutting contracts:

- **`docs/` typos and broken references.** Run
  `oma-docs verify` (or grep by hand) against the markdown files; fix
  any stale links, dead anchors, or wrong commands. The "Doc-drift
  guards (CI fails the build if you forget)" section of `AGENTS.md`
  makes drift your problem if you do not fix it.
- **`tests/` gaps that do not require module changes.** Add an extra
  edge case to `tests/test_status_store.py` (e.g. a 409 burst that
  exhausts the retry budget), a fixture variant under
  `tests/fixtures/samples/`, or a snapshot entry to `tests/golden/` if
  the existing generator covers it.
- **Doc-comment drift inside `src/openstudio_operator/`.** The
  cross-references inside module docstrings (e.g. the singleton guard
  pointing to `tests/test_singleton_registry_coverage.py`) are
  hand-maintained. If you spot a mismatch, a focused PR that brings the
  comment back in sync with the code is welcome.
- **One-shot bug fixes** that are scoped to a single handler module
  and have a clear repro in `tests/`. Look for issues tagged
  `good first issue` in the issue tracker.

What NOT to start with: anything that crosses the D04/D05/D11/D12
contract boundaries (storage polling, autoscaling, secret reading,
status-store schema changes, kopf upgrades). Those need a design
discussion first and an updated audit doc row.

---

## Pointers to other docs

| You want to… | Read |
|--------------|------|
| Understand the operator's modules and intent | [`docs/architecture-plan.md`](./architecture-plan.md) |
| Verify what an HTTP call does and does not do | [`docs/contracts/openstudio-server-v3.11.0-rest.md`](./contracts/openstudio-server-v3.11.0-rest.md) |
| Confirm a D11/D04 invariant holds | [`docs/audit-dryrun-idempotency.md`](./audit-dryrun-idempotency.md) |
| Run the kind recipe for a real cluster | [`docs/kind-validation.md`](./kind-validation.md) |
| Turn on kube-apiserver audit logging for the operator's API surface | [`docs/audit-policy.md`](./audit-policy.md) (#399) |
| See the validated runbook for production pre-reqs | [`docs/validation.md`](./validation.md) |
| Look up a fixed identifier (CRD/RBAC/namespace) | [`AGENTS.md`](../AGENTS.md) "Fixed identifiers" |
| Remind yourself which Working rule applies | [`AGENTS.md`](../AGENTS.md) "Working rules" |