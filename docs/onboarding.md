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

# lint (line-length 100; CI pins ruff>=0.16)
ruff check .
```

After the first green run you are ready to read the layout, look at an
existing handler module as a template, and start editing.

### Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | **3.12** | `Dockerfile` pins `python:3.12-slim` by digest (`#113`); local dev should match. `pyproject.toml` allows `>=3.11` but 3.12 is what CI and the container image use. |
| `pip` | latest | used to install from `requirements.lock` (hash-pinned, `--require-hashes`) and then the editable package. |
| `requirements.lock` | pinned | the source of truth for runtime deps (`#173`). Every package is pinned to an exact version with `--hash=sha256:...` annotations. Generated with `pip-compile --generate-hashes --output-file=requirements.lock pyproject.toml` (requires `pip-tools`); see `requirements.lock` header for the exact invocation. Never edit by hand. |
| `ruff` | **`>=0.16`** | pinned in `pyproject.toml` `[project.optional-dependencies].dev`. Local floor must match CI's floor — the `#68` incident (TRY004 + RUF059) was caused by an old local ruff that activated new CI rules. If `ruff --version` reports `<0.16`, upgrade before committing. |
| `pytest` | `>=8.0` | installed by `pip install -e '.[dev]'`. **Use the venv pytest, not the system one** — the system pytest cannot resolve the `openstudio_operator` package import and reports `ModuleNotFoundError` for almost every test. |
| `kubectl` + a cluster | for live validation only | `kopf run --module openstudio_operator.handlers` needs the CRD applied and the namespace `openstudio-server` reachable. The unit-test loop does NOT require a cluster. |
| `kind` (optional) | for the validation recipe | `scripts/create-kind-cluster.sh` + `scripts/deploy-openstudio-stack.sh` stand up a single control-plane node and the full helm overlay. See `docs/kind-validation.md` for the live-evidence runbook. |

Mocking dependencies are **pinned in `pyproject.toml`**: `responses` for
the OpenStudio REST API, `fakeredis` for the read-only Redis client
(`#12`). Do not add a second HTTP or Redis mocking library.

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

540 tests across 28 files (run `.venv/bin/pytest --collect-only` to
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

## Adding a new OSCM timer handler module

This is the highest-traffic contributor workflow in this repo: every
new plan-module OSCM timer follows the same five-step pattern. Skip a
step and `tests/test_singleton_registry_coverage.py` will fail loudly
— that is the gate.

1. **Create the module** under `src/openstudio_operator/handlers/`
   (`analysis_sla.py`, `datapoint_watchdog.py`, `web_background_monitor.py`,
   and `worker_recycler.py` are the existing templates). Read one of them
   first — they are the canonical examples and the comments name the
   cross-cutting contracts (D04/D05/D11/D12) inline.

2. **Register exactly one `@kopf.timer`** for the OSCM resource using
   `_SPEC["group"]/_SPEC["version"]/_SPEC["plural"]` from
   `src/openstudio_operator/config.py`. The handler's `id` is what the
   singleton guard will look up.

3. **Add the module to the import block** in
   `src/openstudio_operator/handlers/__init__.py`. The import block is
   what makes `kopf run --module openstudio_operator.handlers` see the
   new timer; the `install_singleton_guard()` call at the bottom of that
   file then wraps it automatically — DO NOT try to wire the guard per
   module.

4. **Add the handler `id` to `EXPECTED_OSCM_TIMER_HANDLER_IDS`** in
   `tests/test_singleton_registry_coverage.py`. Forgetting this is the
   bug `test_all_oscm_spawning_handlers_are_singleton_guarded` is
   designed to catch — both the "added but not declared" and "declared
   but not added" directions.

5. **Write the targeted unit tests** under `tests/test_<module>.py`. Use
   `responses` for the OpenStudio REST surface and `fakeredis` for the
   queue-fabric client; do not introduce new mocking libraries.

```bash
# local gate that catches step-4 mistakes BEFORE pushing
.venv/bin/pytest tests/test_singleton_registry_coverage.py -v
```

If you skip step 4, the test fails with `New OSCM spawning handler(s)
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
(`reason=RedisUrlEmpty`) when the field is empty. Helm-chart users must
set it explicitly (or template it from the Redis Secret). **Do not "fix"
the default** — it is the regression fence. See `handlers/__init__.py`
for the drain-queue pattern kopf requires for events from non-event
contexts.

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
scalars `lastRecycleAt`/`lastWebBackgroundRestart`. In-memory state is
cache, never source of truth. The status store handles 409 retries
internally — do not build a parallel retry path on top of it.

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
publicly-known `openstudio` literal (rotated to the placeholder
`openstudio-rotated`). The matching `REDIS_URL` env vars in the
manifests for `web`, `web_background`, and `worker` were rotated in
lockstep.

Fresh installs MUST run `scripts/rotate_redis_password.sh` first —
it generates a per-cluster 32-character random password, substitutes
it into the five manifests at apply time, and updates the live
`openstudio-redis` Secret so the helm chart's `web` / `web_background`
/ `worker` Deployments pick it up. The companion CI guard
`scripts/check_redis_password_unique.sh` fails the build if the
legacy `openstudio` literal re-appears as a Redis password in any of
the five manifest files (#150 was a real incident; this is the
regression fence).

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
- any same-namespace peer in `openstudio-server` (sidecar or
  colocated scraper, identified by an empty `podSelector`).

A cluster whose Prometheus runs in a differently-named namespace
(`monitoring`, `kube-prometheus-stack`, `observability`, etc.) MUST
edit the `namespaceSelector` label match in that policy before
applying. Otherwise the operator's metrics (queue depths,
status-conflict retries, `resque_workers_seen_max`,
`status_map_caps_total`, the dry-run gate counters) **silently
become unreadable** — there is no other failure signal.

The enforcement test
[`tests/test_deploy_manifests.py::test_network_policy_metrics_ingress_has_prometheus_and_peer_allow`](../../tests/test_deploy_manifests.py)
asserts both peers are present and fails CI on either removal. The
unrelated no-custom-autoscaling rule above (#77) keeps the operator
process from owning any autoscaling surface; #166 governs which
**external** scrapers can reach the `/metrics` endpoint it does
emit.

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

### Branch naming

```
fix/issue-N-slug
feat/issue-N-slug
docs/issue-N-slug
chore/issue-N-slug
```

Use a lowercase, hyphenated slug that names the change. The
issue-number prefix lets `gh pr merge` produce a clean closing keyword
on squash-merge without further edits.

### PR body

The PR body MUST include `Closes #N` (or `Fixes #N` / `Resolves #N`)
when the issue SHOULD close on merge, and `Refs #N` (or `for #N` /
`touches #N`) when it should stay open. See the merge-subject hygiene
section below — `develop` closes issues from PR **bodies** too, so the
keywords matter in BOTH places.

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
  any stale links, dead anchors, or wrong commands. The "Update
  protocol" section of `AGENTS.md` makes drift your problem if you do
  not fix it.
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
| See the validated runbook for production pre-reqs | [`docs/validation.md`](./validation.md) |
| Look up a fixed identifier (CRD/RBAC/namespace) | [`AGENTS.md`](../AGENTS.md) "Fixed identifiers" |
| Remind yourself which Working rule applies | [`AGENTS.md`](../AGENTS.md) "Working rules" |