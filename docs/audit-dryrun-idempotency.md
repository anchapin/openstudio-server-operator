# Cross-cutting audit: dryRun completeness · idempotency anchors · config purity

**Issue:** #21 ([T17], final gate before any non-dry-run work-cluster exposure)
**Date:** 2026-08-18
**Scope:** all merged modules — `analysis_sla` (#8/#9), `datapoint_watchdog` (#10),
`worker_recycler` (#11), `redis_client` (#12), `web_background_monitor` (#13),
`singleton` (#14), `archival` (#15), `retention` (#16; prune CronJob since #78), `metrics` (#17);
`hpa_floor` (#18) REMOVED in favor of KEDA ScaledObject (#77).
**Method:** full code walk of `src/openstudio_operator/` + mechanical sweeps
(grep for every mutating call site; AST scan of every numeric/string literal) +
test-name verification. `ruff check .` and `pytest` green (273 → 274 tests).

**Verdict:** 10 of 11 mutation sites were correctly gated. **One D11 violation
found and fixed in this PR** (failed-archival-Job cleanup delete un-gated —
filed as blocking issue **#42**, fix + regression test included here).
Idempotency: every repeated-tick action has a durable anchor-proof except the
two modules with documented in-memory conservative caches (both restart-safe
by delaying, never false-firing). Config purity: zero policy literals outside
`config.py`; appendices list every exempt transport-mechanics constant.

---

## 1. dryRun completeness (D11)

`spec.dryRun` (default `false`, `config.py:84`) suppresses the mutation. The
would-be dry-run-marked Event is itself suppressed (#164): the operator-side
`EventEmitter.emit` short-circuits when `dry_run=True` and increments
`EventEmitter.suppressed_count`; the prune-side `run_retention_tick` returns
before any Event would be issued. Status-anchored accounting proceeds
identically in dry-run (deliberate pacing choice, documented per module), so
flipping the flag back changes **only** the mutation.

### 1.1 Mutation-site enumeration

Every REST/K8s mutating call in `src/openstudio_operator/` (grep for
`soft_stop_analysis|stop_analysis|requeue_datapoint|delete_analysis|
delete_namespaced_pod|patch_namespaced_deployment|create_namespaced_job|
delete_namespaced_job|patch_namespaced_horizontalpodautoscaler|
patch_namespaced_custom_object_status`).
Since #78 the three storage rows (R4/K4/K5) execute in the prune CronJob
pod, not the operator process — the gate is identical: the entrypoint
builds config from the ACTIVE CR's `spec`, so `spec.dryRun` suppresses them
the same way (see the #78 addendum, §4). The operator process itself no
longer performs any storage mutation.

| # | Mutation | Site | Gate (file:line) | Dry-run Event substitution | Status |
|---|----------|------|------------------|----------------------------|--------|
| R1 | REST `GET /analyses/{id}/soft_stop` — `soft_stop_analysis` | `handlers/analysis_sla.py:234` | `dry_run = config.dry_run` / `if not dry_run:` (analysis_sla.py:232-233) | message suffix `"— soft stop suppressed (spec.dryRun)"` + `AnalysisSoftStopped` Warning Event (analysis_sla.py:240-244); anchor written with `outcome="dry-run"`. Post-#164: when `dry_run=True` the `EventEmitter.emit` call short-circuits — the `AnalysisSoftStopped` Event is **not** posted to the kube-apiserver; `EventEmitter.suppressed_count` increments instead (#164). The composed message is still visible in the operator INFO log line. | GATED |
| R2 | REST `POST /analyses/{id}/action` (`stop`) — `stop_analysis` | `openstudio_client.py:209` (definition only) | **No call sites** — client method exists for contract completeness; no handler invokes it. When wired (future module/phase) it MUST get the same `if not dry_run:` gate + marked Event. | n/a | DORMANT |
| R3 | REST `POST /data_points/{id}/requeue` — `requeue_datapoint` | `handlers/datapoint_watchdog.py:192` | `dry_run = config.dry_run` / `if not dry_run:` (datapoint_watchdog.py:190-191) | message suffix `"suppressed (spec.dryRun)"` + `DatapointRequeued` Normal Event (datapoint_watchdog.py:200-201); requeue budget incremented exactly as real. Post-#164: when `dry_run=True` the `EventEmitter.emit` call short-circuits — the `DatapointRequeued` Event is **not** posted to the kube-apiserver; `EventEmitter.suppressed_count` increments instead (#164). | GATED |
| R4 | REST `DELETE /analyses/{id}` — `delete_analysis` | `retention.py:315` (actor: prune CronJob since #78) | `if config.dry_run: … return False` before the call (retention.py:308-314); also withheld when `purgeCompletedNFSFiles` false (retention.py:296-300) | `AnalysisDeleted` Normal Event once, on the verification tick only; later ticks silent (persisted verified record is the observable) | GATED |
| K1 | K8s worker pod delete (escalation eviction) — `delete_namespaced_pod` | `handlers/analysis_sla.py:413` | `if not dry_run:` (analysis_sla.py:412); grace semantics per `forceDeleteOnEscalation` (analysis_sla.py:404-410) | message suffix `"— pod deletions suppressed (spec.dryRun)"` + `AnalysisEscalated` Warning Event with the would-be victim list (analysis_sla.py:426-433); anchor stamped with `escalationOutcome="dry-run"`. Post-#164: when `dry_run=True` the `EventEmitter.emit` call short-circuits — the `AnalysisEscalated` Event is **not** posted to the kube-apiserver; `EventEmitter.suppressed_count` increments instead (#164). | GATED |
| K2 | K8s worker Deployment `restartedAt` patch (recycle) — `patch_namespaced_deployment` | `handlers/worker_recycler.py:182` | `dry_run = config.dry_run` / `if not dry_run:` (worker_recycler.py:177-178) | message suffix `"— patch suppressed (spec.dryRun)"` + `WorkerRecycled` Normal Event (worker_recycler.py:192-194); `lastRecycleAt` advances (pacing choice). Post-#164: when `dry_run=True` the `EventEmitter.emit` call short-circuits — the `WorkerRecycled` Event is **not** posted to the kube-apiserver; `EventEmitter.suppressed_count` increments instead (#164). | GATED |
| K3 | K8s web-background Deployment `restartedAt` patch — `patch_namespaced_deployment` | `handlers/web_background_monitor.py:310` | `dry_run = config.dry_run` / `if not dry_run:` (web_background_monitor.py:305-306) | message suffix `"— patch suppressed (spec.dryRun)"` + `WebBackgroundRestarted` Warning Event (web_background_monitor.py:322-324); `lastWebBackgroundRestart` advances (pacing choice). Post-#164: when `dry_run=True` the `EventEmitter.emit` call short-circuits — the `WebBackgroundRestarted` Event is **not** posted to the kube-apiserver; `EventEmitter.suppressed_count` increments instead (#164). | GATED |
| K4 | K8s archival Job spawn — `create_namespaced_job` | `retention.py:481` (actor: prune CronJob since #78) | `dry_run = config.dry_run` / `if not dry_run:` (retention.py:479-480) | message suffix `"— Job spawn suppressed (spec.dryRun)"` + `AnalysisArchivalStarted` Normal Event; dry-run marker record (`jobName=None`) written. The prune entrypoint does not use `EventEmitter` — its `build_event_emitter` closure (#78) posts Events via CoreV1 `create_namespaced_event` and is **only** reachable after `run_retention_tick` clears the `dry_run` gate. When `dry_run=True` the function returns before the Event would be issued, so no kube-apiserver call happens; the dry-run marker record is the substitution observable. | GATED |
| K5 | K8s failed-archival-Job cleanup delete — `delete_namespaced_job` | `retention.py:474` (actor: prune CronJob since #78) | `if not config.dry_run:` (retention.py:473) | **was un-gated** — see §1.2 | **WAS UNGATED → fixed (#42)** |
| K6 | K8s HPA `minReplicas` patch — `patch_namespaced_horizontalpodautoscaler` | REMOVED in #77 — was `handlers/hpa_floor.py:319` (file deleted) | n/a — the operator no longer mutates the HPA; autoscaling is owned by a KEDA ScaledObject (deploy/keda-scaledobject.yaml) whose controller runs OUTSIDE the operator process and is not gated by `spec.dryRun` (intentional — cluster autoscaling is not a per-CR operator policy). The HPA mutations this row once gated are now zero — there is nothing to suppress. | n/a (no operator-side mutation) | **REMOVED (#77)** |

### 1.2 Finding: un-gated Job cleanup delete (filed as #42, fixed here)

`_spawn_archival` removed a Failed-terminal archival Job
(`delete_namespaced_job`, `propagation_policy="Foreground"`) to free the
deterministic name for a respawn — **before** evaluating `config.dry_run`.
Reachable when dryRun is flipped on after a real spawn failed: the reconcile
phase clears the failure marker, then the next spawn-phase tick deleted the
failed Job object even in observe-only mode (destroying forensic pod logs).
No user data or OpenStudio state was touched — operator-owned artifact,
24 h TTL would reap it anyway — but it violated the D11 contract.

**Fix (this PR):** the delete is gated identically to the spawn it precedes;
under dry-run the failed Job stays for forensics and the tick falls through
to the (suppressed) spawn path, writing the standard dry-run marker — so the
observable state machine is unchanged and the real cleanup+respawn happen on
the first tick after dryRun lifts.
**Regression test:** `test_dry_run_suppresses_failed_job_cleanup_delete`
(`tests/test_retention.py:666`) — verified red on pre-fix code, green after.

### 1.3 Exempt categories (documented, not gated by design)

| Category | Sites | Why exempt |
|----------|-------|------------|
| CR `.status` subresource writes (`patch_namespaced_custom_object_status`) | `status_store.py:281` (single chokepoint; all `StatusStore.set_*/clear_*/mark_*` route through `_mutate`) | **Operator memory (D04), not user-cluster mutation.** The audit brief explicitly exempts them: anchors are how idempotency survives dry-run and restarts. Gating them would break the once-per-analysis semantics. |
| Kubernetes Events (`kopf.event` / `create_namespaced_event`) | the `emit=` parameter on the **four** handler wrappers (`analysis_sla`, `datapoint_watchdog`, `worker_recycler`, `web_background_monitor` — `storage_pruner` removed in #78, `hpa_floor` removed in #77), all routed through `EventEmitter.emit` in `events.py` (#164); `singleton.py` lines **212/221** (the two state-change emits inside `SingletonGuard.enforce`) and line **439** (`_emit_kopf_event`); the prune entrypoint's CoreV1 emitter (`prune_entrypoint.py:95 build_event_emitter`) | Observability objects. Post-#164 they are **suppressed by** the dry-run substitution, not the substitution itself: when `spec.dryRun=true` the operator-side `EventEmitter.emit` short-circuits and increments `EventEmitter.suppressed_count` (#164) — no kube-apiserver call; the prune-side `run_retention_tick` returns before any Event would be issued. The substitution mechanism is the in-memory counter (operator) or the early-return / suppressed marker record (prune). (K8s may rate-limit them; they mutate no managed resource.) |
| Prometheus counter increments (`*.inc()`) | `metrics.py` consumers | In-process metrics, not cluster state. |
| Operator self-wiring | `handlers/__init__.py:18` (`start_metrics_server()`), client/redis/tracker caches | Operator-pod-local; no user-cluster object touched. |

### 1.4 Negative controls

- `redis_client.py` can structurally not mutate: single dispatch chokepoint
  `_execute` asserts an allowlist (`READ_ONLY_COMMANDS = {LLEN, SMEMBERS,
  GET}`, redis_client.py:53, 103-106); no write command name appears in the
  module; tests introspect the source and a recorded command log.
- `singleton.py` is read-only + Events: `list_namespaced_custom_object` and
  `kopf.event` only; losers are never deleted or mutated (docstring
  commitment, test-enforced).
- `archival.py` is a pure manifest generator (no cluster I/O).

---

## 2. Idempotency — anchor proofs per module (D04)

Restart-safety in every case rests on the same property: the decision anchor
lives in the CR `.status` subresource, which a fresh operator process re-reads
before acting; in-memory state is cache only. Ordering is mutate-then-anchor;
a lost anchor write after a successful mutation causes at most one benign
re-attempt of an action whose effect already happened (accepted D12 race,
documented per module).

| Module / action | Anchor | Checked before action | Restart-safety argument | Covering test(s) |
|---|---|---|---|---|
| **SLA soft-stop** (once per analysis) | `status.softStops[id]` (`SoftStopRecord`) | analysis_sla.py:224 (`if analysis_id in soft_stops: continue`) — BEFORE the REST call | Anchor persisted in CR; fresh process re-reads it and skips. Dry-run anchors use `outcome="dry-run"` — still one-shot, so flipping dryRun off never double-stops. | `test_soft_stop_fires_exactly_once_across_ticks`, `test_anchor_survives_operator_restart`, `test_dry_run_suppresses_rest_call_and_marks_event` |
| **SLA escalation** (never twice) | `SoftStopRecord.escalated_at` + `escalationOutcome` | analysis_sla.py:296-297 (`if record.escalated_at is not None: continue`) — before pod discovery/deletes; stamped at analysis_sla.py:434 via `mark_soft_stop_escalated` | Grace clock itself anchors on the persisted `issuedAt`, never process state: `test_restart_mid_grace_escalates_from_original_anchor_time`. Marker stamped after deletes; worst case (lost stamp) = one extra eviction burst against pods that likely no longer exist — documented accepted race. | `test_double_escalation_impossible`, `test_escalated_anchor_skips_grace_phase_entirely`, `test_restart_mid_grace_escalates_from_original_anchor_time`, `test_grace_not_yet_elapsed_waits_even_after_restart` |
| **Anchor retirement** (prune) | anchor absence | analysis_sla.py:289-294 — left `started`/vanished ⇒ `clear_soft_stop` (idempotent write-nothing on absent key) | Fresh process sees no anchor ⇒ nothing to escalate. | `test_analysis_completed_during_grace_prunes_anchor_without_escalating`, `test_analysis_vanished_from_api_prunes_anchor`, `test_clear_soft_stop_deletes_only_that_key_and_is_idempotent` |
| **Datapoint requeue budget** (bounded) | `status.requeues[dp].count` (`RequeueRecord`) | datapoint_watchdog.py:174-175, 189 (`budget >= max_requeues` ⇒ exhausted ⇒ NO action ever) — before the REST call; bumped at :203 | Budget persisted; NOT pruned on departure (a requeued datapoint legitimately leaves `started` while queued — wiping the budget would unbound the loop). Dry-run burns budget exactly like real ⇒ no double-burn on flag flip. | `test_requeue_fires_when_over_runtime_and_under_budget`, `test_exhausted_never_requeued_and_evented_once`, `test_budget_survives_operator_restart`, `test_departure_keeps_requeue_budget`, `test_dry_run_suppresses_rest_call_and_burns_budget_like_real` |
| **startedSince clock** (zombie judgment) | `status.startedSince[dp]` | set on first observation (datapoint_watchdog.py:164-167), pruned on departure (:157-159), reset on requeue for pacing (:206-207) | Post-restart backfill is conservative: unobserved-but-started gets a fresh clock at `now` — never punitive (light endpoint carries no timestamps). | `test_post_restart_backfill_is_conservative`, `test_exhaustion_reemits_once_after_simulated_restart` |
| **Worker recycle cooldown** | `status.lastRecycleAt` scalar | worker_recycler.py:160-163 — THE GATE, checked FIRST before any poll; set at :197 (advances in dry-run too) | Cooldown straddling an operator restart honored from the persisted scalar. Interval trigger treats `None` as infinitely elapsed (first-fire). | `test_restart_mid_cooldown_honors_persisted_last_recycle_at`, `test_restart_after_cooldown_recycles_from_persisted_state`, `test_gate_closed_blocks_trigger_1_without_even_polling`, `test_gate_closed_blocks_trigger_2`, `test_multiple_completions_in_quick_succession_recycle_exactly_once`, `test_dry_run_suppresses_patch_marks_event_and_advances_last_recycle_at` |
| **web_background restart cooldown** | `status.lastWebBackgroundRestart` scalar | web_background_monitor.py:276-278 — THE GATE, checked FIRST before any Redis/K8s read; set at :328 | Persisted scalar survives restarts (`test_operator_restart_mid_cooldown_honors_persisted_anchor`). The sustained-window `StallWindowTracker` is in-memory **by documented design** (D04-clean conservative cache): a restart resets to fresh observation, which can only DELAY a restart, never false-trigger one. | `test_cooldown_blocks_second_restart_even_if_stall_persists`, `test_operator_restart_mid_cooldown_honors_persisted_anchor`, `test_operator_restart_after_cooldown_needs_fresh_sustained_window`, `test_gate_closed_senses_nothing_at_all`, `test_dry_run_suppresses_patch_marks_event_and_advances_anchor` |
| **Archival in-flight dedup** | `status.archivedAnalyses[id]` (`ArchivedAnalysisRecord`) | retention.py `run_retention_tick` (prune CronJob since #78) — spawn only for ids NOT in the tracked snapshot; adopt paths (:405-449) adopt an existing deterministic-named Job instead of double-spawning | Restart resumes WATCHING the Job named in the persisted record rather than respawning (`test_restart_midflight_resumes_watching_rather_than_respawning`); a lost record write after Job create is healed by read-first adoption. Dry-run marker (`jobName=None`) makes suppression once-per-analysis; the dryRun-lift reconcile clears it. | `test_spawn_creates_deterministic_job_writes_inflight_record_and_events`, `test_orphan_inflight_job_without_record_is_adopted_not_respawned`, `test_restart_midflight_resumes_watching_rather_than_respawning`, `test_dry_run_suppresses_spawn_and_delete_with_observable_tracking` |
| **Deterministic archival Job names** | `archival_job_name(id)` — pure function (`oscm-archive-<sanitized>-<sha256-8>`) | retention.py `_spawn_archival` (prune CronJob since #78) — same id ⇒ same name ⇒ re-create conflicts (409) rather than suffix-littering | Pure function of the analysis id; survives restarts trivially. Failed-Job retry frees the name via the (now dryRun-gated, #42) cleanup delete. | `test_job_name_deterministic_per_analysis` (tests/test_archival.py), `test_job_failure_retains_analysis_warns_and_retries_next_tick`, `test_dry_run_suppresses_failed_job_cleanup_delete` |
| **Verified-then-delete** (cardinal rule) | `ArchivedAnalysisRecord.verified_at` — written ONLY on observed Job `Complete` | retention.py `_reconcile_tracked`/`_delete_verified` (prune CronJob since #78) — retry-delete only for verified; `delete_analysis` unreachable without it | Verification is durable in CR status; a restart re-reads it and retries the delete exactly once per tick. | `test_job_success_verifies_then_deletes_and_prunes_status`, `test_cardinal_never_deleted_without_verified_success`, `test_verified_record_deletion_failure_retries_delete_next_tick`, `test_dry_run_suppresses_delete_of_verified_analysis` |
| **HPA-floor cooldown** | `HpaFloorState` (in-memory per CR) — **documented deviation: no v1alpha1 status scalar exists for this module** | REMOVED in #77 — was hpa_floor.py:191 (`gate_open`); file deleted. The replacement KEDA ScaledObject owns the cooldown via its own `cooldownPeriod` field (deploy/keda-scaledobject.yaml: scaling 0→N uses KEDA's polling interval, scale-down uses the HPA's own stabilization window). KEDA is the system of record for autoscaling state. | n/a (no longer operator-side) |
| **Singleton guard** (not an action, but gating) | none — stateless | `resolve_active_cr` recomputes oldest from `metadata.creationTimestamp` on EVERY tick/event (singleton.py:171-174, 243-245) | Nothing persisted, nothing in memory decides the winner; the only memory (`_last_state`) is an Event-noise gate. Fail-closed on API errors. | `test_gated_wrapper_serves_only_oldest`, `test_gated_wrapper_fails_closed_on_api_error` |
| **StatusStore itself** | n/a | read-modify-write with 409-restart, patches recomputed from fresh reads (status_store.py:263-302) | Mutation is a pure function of its arguments; same-value writes are no-ops. | `test_set_same_value_twice_writes_once`, `test_mark_soft_stop_escalated_is_idempotent`, `test_clear_started_since_is_idempotent` |

**Gaps:** none blocking. The HPA-floor module (#18) is removed in #77
(see K6 row above) — its in-memory cooldown cache went with it. The
stall-window tracker remains as the single in-memory conservative
cache, with the same documented D04-clean property: a restart can only
delay action, never false-fire, and the v1alpha1 schema offers no status
field for it (follow-up only if tuning ever demands it).

---

## 3. Config purity

**Claim verified:** no policy value (timeout, limit, interval, threshold,
name-that-tunes-behavior) is hardcoded in handler logic; every decision
threshold reads `config.<policy>.<field>` at its use site.

### 3.1 Sanctioned policy locations (verified, imported — not duplicated)

All policy numbers live in `config.py`: dataclass defaults +
`from_spec` fallbacks (`180`/`15`/`45`/`3`/`12`/`30`/`10`/`7` …,
config.py:24-113, mirroring `deploy/crd.yaml`) and the module-level wiring
default `DEFAULT_WORKER_HEARTBEAT_STALE_SECONDS = 300.0` (config.py:21,
imported by web_background_monitor at :84 — not re-declared). The
HPA-floor policy constants (`DEFAULT_HPA_FLOOR_POLICY` and friends) were
REMOVED in #77 — the corresponding handler module was deleted and the
CRD has no HPA-floor spec field, so the policy values are no longer
carried in any CR and are no longer imported anywhere. KEDA
autoscaling parameters (queue trigger, `minReplicaCount`, `maxReplicaCount`,
`pollingInterval`, `cooldownPeriod`) live in `deploy/keda-scaledobject.yaml`
— a manifest owned by the cluster admin (same convention as the storage
prune CronJob's `*/10` schedule in #78), not in `config.py`.

### 3.2 Sweep proof (AST scan of every numeric/string literal, all of `src/openstudio_operator/`)

Every literal found outside `config.py` falls into an exempt class:

1. **Poll cadences** (`POLL_INTERVAL_SECONDS`): `30.0` analysis_sla, `60.0`
   datapoint_watchdog / web_background_monitor, `300.0`
   worker_recycler — each documented in-file as plan-mandated operator
   behavior, not cluster policy. (The `60.0` hpa_floor cadence was
   removed in #77; the `600.0` retention cadence left the operator
   with #78: it is now the storage-prune CronJob's `*/10 * * * *`
   schedule in `deploy/storage-cronjob.yaml` — a manifest property
   owned by the cluster admin, outside this sweep by the same logic as
   the operator Deployment's own manifest settings. KEDA's `pollingInterval`
   (15 s) lives in `deploy/keda-scaledobject.yaml` for the same
   reason.)
2. **Transport mechanics** — appendix A.
3. **K8s API protocol tokens** — HTTP status codes (`400`/`500`/`404`/`409`,
   `200`-char slice), job-condition strings (`"Complete"`/`"Failed"`),
   pod-phase `"Running"`, `grace_period_seconds` sentinel `0` vs `None`
   (API semantics of force-drain vs default grace), RFC 7386 content type.
4. **Domain-state vocabulary** (compared, never tuned): `"started"`,
   `"completed"`, trigger names, escalation outcomes (`"issued"`/`"dry-run"`/
   `"evicted"`/`"no-matching-pods"`), Event reason strings.
5. **Chart-fixed object identifiers** — appendix B.

**Violations: none.** The only near-miss class is the triple declaration of
the `"worker"` Deployment-name fallback (appendix B) — an identifier, not a
policy knob, and AGENTS.md's fixed-identifiers section sanctions it.

---

## Appendix A — transport-mechanics constants (exempt, complete list)

| Constant | Location | Value | Rationale |
|---|---|---|---|
| HTTP timeout / retries / backoff base | openstudio_client.py:93-95 | `10.0` s / `3` / `1.0` s | client transport; D12 retry discipline (~1/2/4 s jittered) |
| retry jitter bounds | openstudio_client.py:104 | `0.5`–`1.5` | jitter scale |
| Redis socket timeout | redis_client.py:89 | `5.0` s | read-only client transport |
| Status-store 409 retries / backoff | status_store.py:53-54 | `5` / `1.0` s | RMW conflict mechanics (D12) |
| Metrics port | metrics.py:85 | `9090` | operator-pod-local scrape endpoint |
| rclone image pin | archival.py:64 | `rclone/rclone:1.67.0` | supply-chain pin, never `latest` |
| Job `backoffLimit` / restart policy | archival.py:66-67 | `3` / `Never` | caps in-Job attempts at 4; keeps failed-pod logs |
| Job `ttlSecondsAfterFinished` | archival.py:68 | `86400` (24 h) | ephemeral-Job self-cleanup + post-mortem window |
| Job-name length budget / digest | archival.py:77-78 | `63` / `8` | DNS-1035 label constraints |
| Redis key layout / queue names | redis_client.py:55-57 | `simulations`, `requeued`, `resque:workers` | verified Resque wire format (contract file) |

## Appendix B — chart-fixed identifiers (exempt, complete list)

`worker` Deployment fallback — declared three times
(`analysis_sla._DEFAULT_WORKER_DEPLOYMENT`, `worker_recycler.DEFAULT_WORKER_DEPLOYMENT`,
`web_background_monitor.DEFAULT_WORKER_DEPLOYMENT`; the analysis_sla copy
documents the import-cycle rationale); `web-background` Deployment fallback;
`nfs-pvc` + `/mnt/openstudio` (archival.py:58-59);
`kubectl.kubernetes.io/restartedAt` annotation key
(worker_recycler/web_background_monitor). All are AGENTS.md "fixed
identifiers" or K8s protocol facts — none tunable policy. The pre-#77
`worker-hpa` HPA name (hpa_floor.py:121) and the K8s default
`minReplicas` constant (hpa_floor.py:127) are GONE — the chart's HPA
was removed in favor of KEDA (`deploy/keda-scaledobject.yaml`).

## Appendix C — verification commands

```bash
ruff check . && pytest                       # both green (331 tests post-#77 removal)
grep -rn -E 'soft_stop_analysis|stop_analysis|requeue_datapoint|delete_analysis|\
delete_namespaced_pod|patch_namespaced_deployment|create_namespaced_job|\
delete_namespaced_job|patch_namespaced_custom_object_status' src/                    # §1.1 table
# After #77 the operator no longer contains any
# `patch_namespaced_horizontalpodautoscaler` call site — K6 row removed.
# AST literal sweep for §3.2 (numeric + string constants per file, parent-contexted)
```

## Appendix D — Prometheus metrics registry (issue #17; #50 decision)

Metrics live in `src/openstudio_operator/metrics.py`, are module-level
singletons on `prometheus_client`'s default REGISTRY, and are served by
`start_metrics_server()` on the conventional port `9090` (operator-pod-
local; the scrape is in-cluster). The exhaustive inventory — **12
counters + 1 gauge + 1 histogram** — is asserted by `tests/test_metrics_endpoint.py`'s
`EXPECTED_COUNTER_FAMILIES`, `EXPECTED_GAUGE_FAMILIES`, and `EXPECTED_HISTOGRAM_FAMILIES`:
drift in either direction fails CI before it ships, so any new metric added to
this codebase MUST be added to both the table below and the matching
`EXPECTED_*` tuple in the same PR (the `hpa_floor_adjustments_total`
counter, removed in #77, is the canonical "you forgot" example — see
`docs/kind-validation.md` step 4.7).

Counters and the gauge follow the same in-process, dryRun-transparent
convention (D11-exempt category — in-process metrics, not cluster
state): none of them is suppressed by `spec.dryRun`, and the dry-run
substituted tick still increments identically (the gauge is observed
every poll regardless of dry-run — see #87). Coverage is exhaustive:
there is no metric declared in code without an incrementer / setter,
and there is no incrementer / setter without a metric. This invariant
is what makes "no permanently-zero metric" true — with the documented
nuance that a labelled counter family (`handler_tick_failures_total`,
#117) exposes its `# TYPE` line only after the first observation;
zero-observation series are normal when no tick is failing, and the
test pre-touches a sentinel series so the family-existence assertion
stays self-contained.

### Counters

| Counter | Module | Increments on | Anchor pairing |
|---|---|---|---|
| `openstudio_operator_soft_stops_total` | `analysis_sla` (#8) | Soft-stop issued (or dry-run substituted) | `status.softStops[id]` |
| `openstudio_operator_datapoints_requeued_total` | `datapoint_watchdog` (#10) | Requeue issued (or dry-run) | `status.requeues[dp]` |
| `openstudio_operator_datapoints_requeue_exhausted_total` | `datapoint_watchdog` (#10) | Requeue budget exceeded (decision counter) | `status.requeues[dp].count` |
| `openstudio_operator_workers_recycled_total` | `worker_recycler` (#11) | Recycle issued (or dry-run) | `status.lastRecycleAt` |
| `openstudio_operator_worker_pods_evicted_total` | `analysis_sla` (#9) | Escalation eviction (decision counter; dry-run counts too) | `SoftStopRecord.escalated_at` |
| `openstudio_operator_web_background_restarts_total` | `web_background_monitor` (#13) | web_background restart issued (or dry-run) | `status.lastWebBackgroundRestart` |
| `openstudio_operator_analyses_archived_total` | `retention` (#16; prune CronJob since #78) | Archival Job observed Complete (adopted completions included) | `status.archivedAnalyses[id].verified_at` |
| `openstudio_operator_analyses_deleted_total` | `retention` (#16; prune CronJob since #78) | `DELETE /analyses/{id}` issued post-verification (suppressed by `spec.dryRun`) | `status.archivedAnalyses[id].verified_at` |
| `openstudio_operator_status_conflicts_total` | `status_store` (#119) | Per-attempt 409 from the K8s API Server inside `_mutate`'s except branch (one increment per 409, before the backoff sleep) | n/a — conflict counter, not a decision counter |
| `openstudio_operator_status_conflict_retries_exhausted_total` | `status_store` (#119) | RMW cycle that exhausted the 409 retry budget and raised `StatusStoreConflictError` (tick skipped, anchor NOT written) | n/a — terminal failure of an anchor write |
| `openstudio_operator_handler_tick_failures_total` | `handlers/*` timer wrappers (#117) | Tick failure caught by a timer wrapper (catch-and-skip path). **Labelled by `(module, error_type)`** — `module` ∈ {`analysis_sla`, `datapoint_watchdog`, `worker_recycler`, `web_background_monitor`}; `error_type` ∈ {`OpenStudioApiError`, `StatusStoreError`, `ApiException`, `RedisClientError`} | n/a — observability for the wrapper catch-and-skip path (the tick was suppressed, no anchor was written) |
| `openstudio_operator_status_map_caps_total` | `status_store` (#171) | Defensive cap hit on a CR `.status` map — one increment per actual eviction (post-RMW, retry-stable — not per 409 attempt). **Labelled by `map_name`** — `map_name` ∈ {`softStops`, `requeues`, `startedSince`, `archivedAnalyses`}. When a map hits `STATUS_MAP_MAX_ENTRIES = 10000`, `status_store._set_map_entry` evicts the **oldest entries first** (sorted by key — the operator's keys are UUIDs, so the sort order is deterministic but not age-aware) before adding the new entry; the cap fires before etcd's 1.5 MB object-size limit can blow up a tick's read + JSON-parse + merge-patch. | n/a — defensive cap on the (otherwise unbounded) `.status` map; not a decision counter. The companion Warning Event (`StatusMapCapped`) is emitted from the same code path so the on-call has both a log/Event and a Prometheus signal to correlate (`rate(...[5m]) > 0` fires once per cap hit). |

**Labelled convention (#117).** `handler_tick_failures_total` is the
first labelled counter in the registry and the canonical pattern for
any future "which module is degraded" metric: one labelled family,
`module × error_type`, with one increment per observation. A future
maintainer wiring a new mutation that can fail in catch-and-skip paths
SHOULD extend the same `module` label set (rather than introducing a
new unlabelled counter) so the dashboard's per-module degradation view
stays comparable across error sources — reinventing a non-comparable
unlabelled counter here is exactly the regression #181 guards against.

### Gauge

| Gauge | Module | Sets / Meaning | Notes |
|---|---|---|---|
| `openstudio_operator_resque_workers_seen_max` | `web_background_monitor` (#44/#87) | Monotonic max of distinct Resque worker ids ever observed in process lifetime (`SMEMBERS resque:workers`, emitted every poll regardless of queue depth) | #44 — Resque key-layout leg-2 non-vacuity safeguard; #87 dropped the original `AND queue depth > 0` alert conjunction so the gauge populates on a healthy idle fleet. `0` with a reachable Redis unambiguously means no workers are registered — alert on `== 0`. Not a decision counter — does not follow the §2 anchor pairing convention. |

### Histogram

| Histogram | Module | Observes | Buckets |
|---|---|---|---|
| `openstudio_operator_analysis_datapoint_count` | `analysis_sla` + `datapoint_watchdog` (#179) | Per-tick count of datapoints observed by the SLA monitor (analyses per tick, `ANALYSIS_DATAPOINT_COUNT.observe(len(analyses))` at `handlers/analysis_sla.py:277`) and the datapoint watchdog (started datapoints per tick, `ANALYSIS_DATAPOINT_COUNT.observe(len(started_ids))` at `handlers/datapoint_watchdog.py:145`). Unlabelled — one `.observe()` per observed count, not per-CR; per-CR labelling would multiply the series count by the analysis count and defeat the bounded-cardinality design. The observation site is `analysis_sla.py` for the SLA branch and `datapoint_watchdog.py` for the watchdog branch — both increment identically under `spec.dryRun` (D11-exempt category — in-process metrics, not cluster state). | `[5, 10, 50, 100, 500, 1000, 5000]` — bucket-capped to bound per-(analysis \| datapoint) cardinality while still surfacing the "we just started getting 5000-point analyses" shift (Goal 10, OSS hardening). |

### D.1 Removed counter — `STORAGE_FREED_BYTES` (issue #50)

`openstudio_operator_storage_freed_bytes` was declared by #16 alongside
the archival+prune pipeline as the canonical "NFS bytes reclaimed"
metric — but #16 deliberately never incremented it: neither the rclone
archival Job's status nor the OpenStudio API exposes a byte figure at
delete time, and fabricating one would corrupt the counter. A
permanently-zero counter is worse than none (it reads as "zero bytes
ever freed"), so #50 removed it cleanly from `metrics.py`,
`tests/test_metrics_endpoint.py`'s `EXPECTED_COUNTER_FAMILIES`, and the
validation runbook's reference in `docs/validation.md` (Module 4 step 3).
The durable signal of an archived-then-deleted analysis is
`status.archivedAnalyses[id]`, which is what the runbook now uses.

**Reintroduction gate (when a real source is built):** parse the
archival Job's rclone copy summary (a small Job wrapper that writes a
status annotation is the cleanest path), OR a pre/post `du` on the NFS
tree before/after the verified `DELETE` lands. The increment site is
the retention pipeline's verified-delete branch (`retention.py`,
alongside `ANALYSES_DELETED_TOTAL`). Add the counter back to this
appendix's table at the same time.
