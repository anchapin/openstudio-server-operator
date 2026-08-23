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
| Kubernetes Events (`kopf.event` / `create_namespaced_event`) | the `emit=` parameter on the **four** handler wrappers (`analysis_sla`, `datapoint_watchdog`, `worker_recycler`, `web_background_monitor` — `storage_pruner` removed in #78, `hpa_floor` removed in #77), all routed through `EventEmitter.emit` in `events.py` (#164); `singleton.py` lines **212/221** (the two state-change emits inside `SingletonGuard.enforce`) and line **439** (`_emit_kopf_event`); `handlers/dry_run_audit.py` (`_emit_kopf_event`, issue #397 — the `DryRunToggled` Normal Event on every `spec.dryRun` transition deliberately bypasses the D11 gate: it must fire exactly when dryRun is toggled ON, the one moment an EventEmitter-routed audit signal would self-suppress; query with `kubectl get events --field-selector reason=DryRunToggled`); the prune entrypoint's CoreV1 emitter (`prune_entrypoint.py:95 build_event_emitter`) | Observability objects. Post-#164 they are **suppressed by** the dry-run substitution, not the substitution itself: when `spec.dryRun=true` the operator-side `EventEmitter.emit` short-circuits and increments `EventEmitter.suppressed_count` (#164) — no kube-apiserver call; the prune-side `run_retention_tick` returns before any Event would be issued. The substitution mechanism is the in-memory counter (operator) or the early-return / suppressed marker record (prune). (K8s may rate-limit them; they mutate no managed resource.) |
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
| **web_background restart cooldown** | `status.lastWebBackgroundRestart` scalar + `status.stallWindowStartedAt` scalar | web_background_monitor.py:276-278 — THE GATE, checked FIRST before any Redis/K8s read; set at :328 | Persisted scalars survive restarts (`test_operator_restart_mid_cooldown_honors_persisted_anchor`; since #582 the stall-window start is checkpointed too — `test_restart_mid_window_restores_elapsed_stall_window` — so a restart no longer resets an accumulating window to zero; pre-#582 it could only DELAY a restart, never false-trigger one). | `test_cooldown_blocks_second_restart_even_if_stall_persists`, `test_operator_restart_mid_cooldown_honors_persisted_anchor`, `test_restart_mid_window_restores_elapsed_stall_window`, `test_operator_restart_after_cooldown_needs_fresh_sustained_window`, `test_gate_closed_senses_nothing_at_all`, `test_dry_run_suppresses_patch_marks_event_and_advances_anchor` |
| **Archival in-flight dedup** | `status.archivedAnalyses[id]` (`ArchivedAnalysisRecord`) | retention.py `run_retention_tick` (prune CronJob since #78) — spawn only for ids NOT in the tracked snapshot; adopt paths (:405-449) adopt an existing deterministic-named Job instead of double-spawning | Restart resumes WATCHING the Job named in the persisted record rather than respawning (`test_restart_midflight_resumes_watching_rather_than_respawning`); a lost record write after Job create is healed by read-first adoption. Dry-run marker (`jobName=None`) makes suppression once-per-analysis; the dryRun-lift reconcile clears it. | `test_spawn_creates_deterministic_job_writes_inflight_record_and_events`, `test_orphan_inflight_job_without_record_is_adopted_not_respawned`, `test_restart_midflight_resumes_watching_rather_than_respawning`, `test_dry_run_suppresses_spawn_and_delete_with_observable_tracking` |
| **Deterministic archival Job names** | `archival_job_name(id)` — pure function (`oscm-archive-<sanitized>-<sha256-8>`) | retention.py `_spawn_archival` (prune CronJob since #78) — same id ⇒ same name ⇒ re-create conflicts (409) rather than suffix-littering | Pure function of the analysis id; survives restarts trivially. Failed-Job retry frees the name via the (now dryRun-gated, #42) cleanup delete. | `test_job_name_deterministic_per_analysis` (tests/test_archival.py), `test_job_failure_retains_analysis_warns_and_retries_next_tick`, `test_dry_run_suppresses_failed_job_cleanup_delete` |
| **Verified-then-delete** (cardinal rule) | `ArchivedAnalysisRecord.verified_at` — written ONLY on observed Job `Complete` | retention.py `_reconcile_tracked`/`_delete_verified` (prune CronJob since #78) — retry-delete only for verified; `delete_analysis` unreachable without it | Verification is durable in CR status; a restart re-reads it and retries the delete exactly once per tick. | `test_job_success_verifies_then_deletes_and_prunes_status`, `test_cardinal_never_deleted_without_verified_success`, `test_verified_record_deletion_failure_retries_delete_next_tick`, `test_dry_run_suppresses_delete_of_verified_analysis` |
| **HPA-floor cooldown** | `HpaFloorState` (in-memory per CR) — **documented deviation: no v1alpha1 status scalar exists for this module** | REMOVED in #77 — was hpa_floor.py:191 (`gate_open`); file deleted. The replacement KEDA ScaledObject owns the cooldown via its own `cooldownPeriod` field (deploy/keda-scaledobject.yaml: scaling 0→N uses KEDA's polling interval, scale-down uses the HPA's own stabilization window). KEDA is the system of record for autoscaling state. | n/a (no longer operator-side) |
| **Singleton guard** (not an action, but gating) | none — stateless | `resolve_active_cr` recomputes oldest from `metadata.creationTimestamp` on EVERY tick/event (singleton.py:171-174, 243-245) | Nothing persisted, nothing in memory decides the winner; the only memory (`_last_state`) is an Event-noise gate. Fail-closed on API errors. | `test_gated_wrapper_serves_only_oldest`, `test_gated_wrapper_fails_closed_on_api_error` |
| **StatusStore itself** | n/a | read-modify-write with 409-restart, patches recomputed from fresh reads (status_store.py:263-302) | Mutation is a pure function of its arguments; same-value writes are no-ops. | `test_set_same_value_twice_writes_once`, `test_mark_soft_stop_escalated_is_idempotent`, `test_clear_started_since_is_idempotent` |
| **Deferred Warning-Event queue** (at-least-once across restarts, #402) | `status.deferredEvents` array — the crash-surviving mirror of `QueuedKopfEventSink`'s in-memory queue. Typed accessors `get_deferred_events` / `append_deferred_event` / `clear_deferred_events` (status_store.py:654-725), all routed through the same 409-safe `_mutate` RMW as every other `.status` write; CRD `status` carries the typed array. | `defer_to_next_tick` mirrors every ACCEPTED deferral into the array at accept time (events_sinks.py:293 via `_persist`, :209) with `max_entries=MAX_DEFERRED_WARNING_EVENTS` (=1000, events_sinks.py:77) — a defensive backstop so a clear-failure streak cannot grow the array past the in-memory queue's own bound; the append is skipped (no write) at the cap, and the `queue_full` DROP branch never touches the persisted surface (the `WARNINGS_DEFERRED_DROPPED_TOTAL` counter stays the account of record for backpressure losses). | **Memory in `.status` only (D04-clean):** the sink's in-memory `_queue` is cache; the persisted array is what survives a crash. `flush_for` (events_sinks.py:300, wired by the `@kopf.on.event` drain handler in `handlers/__init__.py:117`) drains the PERSISTED list FIRST, deduplicating exact `(namespace, name, reason, message)` in-memory twins — exactly-once in the common dual-write path; a restarted process starts with an empty queue and re-emits every persisted entry on its first watch tick (at-least-once — a duplicate K8s Event, never a silent loss). A failed CLEAR leaves the list in place to re-emit next tick; a failed append degrades to the pre-#402 in-memory-only behavior (loses only crash-survivability for that entry). Persistence is armed from a `@kopf.on.startup` handler (`handlers/__init__.py:157`), never import time — test/library imports touch no cluster. | `test_defer_persists_accepted_entry_to_status`, `test_flush_drains_persisted_first_and_dedupes_in_memory_twin`, `test_restart_re_emits_persisted_entries_on_fresh_sink`, `test_persist_append_failure_degrades_to_in_memory_only`, `test_persisted_clear_failure_re_emits_next_tick_at_least_once`, `test_persisted_read_failure_drains_in_memory_anyway`, `test_backpressure_drop_never_reaches_the_persisted_surface`, `test_handlers_install_deferred_event_persistence_at_startup` (tests/test_events_sinks.py) |

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
ruff check . && pytest                       # both green (1158 tests across 51 files, current count)
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
local; the scrape is in-cluster). The exhaustive inventory — **21 counters + 18 gauges + 5 histograms** — is asserted by the canonical
`EXPECTED_COUNTER_FAMILIES`, `EXPECTED_GAUGE_FAMILIES`, and
`EXPECTED_HISTOGRAM_FAMILIES` tuples in `tests/_metrics_inventory.py`
(#406; shared by `tests/test_metrics_endpoint.py` and
`tests/test_walk_metrics_registry.py`):
drift in either direction fails CI before it ships, so any new metric added to
this codebase MUST be added to both the table below and the matching
`EXPECTED_*` tuple in the same PR (the `hpa_floor_adjustments_total`
counter, removed in #77, is the canonical "you forgot" example — see
`docs/kind-validation.md` step 4.7). The 12+1+1 → 16+4+1 expansion was
landed in the auto-improvement-loop iteration 2 sweep (#237 dry-run
gate Prometheus surface + emitted companion counter, #238 Resque queue
depth Gauge, #239 singleton-guard election outcomes, #253 Redis
key-layout validation status, #254 sustained-window elapsed seconds
Gauge, #255 kopf.event emission failure counter). The post-#255 → 18+7+3
expansion was landed in the iteration 3 sweep (#306 storage-prune
CronJob skip-tick failure counter, #308 handler tick + REST round-trip
duration histograms, #310 QueuedKopfEventSink drop counter + queue depth
gauge, #312 paired freshness timestamp gauges for `resque_queue_depth`
and `stall_window_elapsed_seconds`). The post-#312 → 19+7+3 expansion is
#403 (singleton-guard loser per-tick skip counter — the per-tick twin of
the change-gated #239 election counter). The post-#403 → 19+8+3 expansion
is #393 (metrics-server bind-outcome gauge — the first bind attempt's
`1.0`/`0.0` record; D11-exempt like the rest of the registry, and set
once at process start, before any dry-run-gated action could exist). The
expansion to 20+9+3 is #469 (handler last-tick scheduler-heartbeat gauge
`handler_last_tick_timestamp{module}` — stamped to `time.time()` at the
end of every `run_oscm_tick` invocation on every terminal path; the only
signal whose flatness means the scheduler itself is dead, generalizing
the #312 freshness-pair idiom to the timers). The post-#469 expansions:
#492 added the four config-state posture gauges (`dry_run_active`,
`server_url_set`, `redis_url_set`, `auto_soft_stop_enabled` — 20+13+3),
and #489 added the status-map size gauge (`status_map_entries`, taking
the inventory to 20+14+3 — the lead-time companion to the #171 cap
counter: set to `len(map)` inside `status_store._read_status` on every
read, so a map trending toward `STATUS_MAP_MAX_ENTRIES` is visible days
before the post-hoc counter + `StatusMapCapped` Event fire at the first
anchor eviction). The post-#489 expansion to 20+15+3 is #504
(`build_info{version, python_version}` — the constant fleet-identity
gauge set at metrics import time from the installed distribution
metadata, `unknown` fallback when the distribution is absent; one
fixed-cardinality series, D11-exempt by construction like #393 — set
at import, before any dry-run-gated action could exist). It costs one
series and makes every other series interpretable against a release
during single-replica Recreate redeploys, where a rolling-window scrape
mixes old/new pod series with identical labels. The post-#504 expansion
to 20+16+3 is #491 (`singleton_wrapped_handlers` — the boot-time
singleton-guard wrap count, set at the end of
`singleton.install_singleton_guard` to the number of OSCM spawning
handlers whose fn carries the gate marker; `0` on a booted operator
that expects timers is the silent-unwrap failure mode the kopf pin
documents — a kopf upgrade moved the private
`registry._spawning._handlers` layout, D05 enforcement silently
disabled while every timer still fires ungated. The runtime complement
of the CI-time `tests/test_singleton_registry_coverage.py` fence and of
the #469 heartbeat — heartbeat proves scheduling, wrap count proves
guarding; shipped with the `OpenStudioOperatorSingletonGuardUnwrapped`
alert). D11-exempt like #393/#504 — set at boot wiring time, before any
dry-run-gated action could exist). The post-#570 expansion to 20+18+5
adds `singleton_expected_handlers` — the wrap-gap DENOMINATOR to the
#491 wrap count: set at install time from the OSCM spawning-handler
population kopf actually reports (the Python-level registry count on
the internals-mismatch branch), it makes a PARTIAL unwrap
(`wrapped < expected`, e.g. 3 of 4 — one handler skipped for missing
#250 registration or a `dataclasses.replace` TypeError, leaving a
kopf-registered OSCM timer running UN-GATED) scrapeable and alertable
where the `== 0` expression read a plausible fraction and stayed
silent; the rekeyed `OpenStudioOperatorSingletonGuardUnwrapped` uses
strict `<`, which subsumes the old clause. D11-exempt like #491 — set
at boot wiring time, before any dry-run-gated action could exist). The
post-#491 expansion to 20+16+5
is #488 (`redis_request_duration_seconds{operation}` +
`kube_api_request_duration_seconds{verb}` — the two remaining
dependencies got the #308 REST treatment: every ReadOnlyRedisClient read
method and every wrapped kube chokepoint (status-store RMW get/patch,
rolling-restart patch, escalation pod list/delete, leg-C pod list,
label-selector Deployment read) observes its wall-clock duration on
BOTH success and failure paths, so an inflated
`handler_tick_duration_seconds` bucket can finally be attributed to
Redis vs REST vs kube during an incident; the label vocabularies are
pinned — `llen|smembers|scan|exists` (the `exists` value added by #688
for the layout validator's O(1) verdict probes) and
`get|patch|delete|list` — and
both families share the #308 bucket set so the three dependency
histograms render on one dashboard axis). The post-#488 expansion to
20+17+5 is #490 (`redis_key_layout_status_fresh` — the #312
freshness-pair idiom applied to the #253 key-layout status gauge,
which was set only by the `@kopf.on.event` watch and therefore held
its boot value indefinitely on a steady-state cluster; every
`_check_redis_key_layout_for_cr` run now stamps the pair in lockstep
through `handlers/_set_redis_key_layout_status`, and the
`web_background_monitor` timer re-runs the check at most once per
`REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL` (5 min) so a mid-flight
Resque layout drift is revalidated-and-surfaced before the first
vacuous restart window can complete; alert
`time() - redis_key_layout_status_fresh > 600` = 2× the interval).

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
| `openstudio_operator_events_dry_run_suppressed_total` | `events` (`EventEmitter.emit` dry-run branch) (#237) | Kubernetes Events suppressed by the dry-run gate (D11) — incremented at the same site as `EventEmitter.suppressed_count`, inside `EventEmitter.emit` when `dry_run=True`. **Labelled by `reason`** mirroring the warning-event vocabulary (`AnalysisSoftStopped` \| `AnalysisEscalated` \| `DatapointRequeued` \| `DatapointRequeueExhausted` \| `WorkerRecycled` \| `WebBackgroundRestarted` \| `ResqueKeyLayoutUnknown`). | n/a — observability for the dry-run path (the substitution observable was previously in-process only). Companion to `events_emitted_total`; the emitted-vs-suppressed rate ratio is the headline SLO for an audit-only install. |
| `openstudio_operator_events_emitted_total` | `events` (`EventEmitter.emit` non-dry-run branch) (#237) | Companion to `events_dry_run_suppressed_total` — every successful `kopf.event` call from `EventEmitter` (`dry_run=False`). Same `reason` label vocabulary. | n/a — observability for the Event path; `rate(events_emitted_total) / rate(events_dry_run_suppressed_total)` is the headline SLO for an audit-only install (a non-trivial suppressed rate with zero emitted rate is the intended steady state; the inverse drift — suppressed > emitted during a non-dry-run deploy — is the alert signal). |
| `openstudio_operator_singleton_election_total` | `singleton` (`SingletonGuard.enforce` post-decode branches) (#239) | Singleton-guard election outcomes (D05). **Labelled by `outcome`** — `outcome` ∈ {`idle`, `active`, `conflict`}; only fires on STATE CHANGES (mirrors the existing change-gated log/Event noise channel — steady state is silent). | n/a — observability for the silent-bypass failure mode (when the kopf registry internals change shape and `install_singleton_guard` returns 0 without the AST coverage test catching it, the corruption is silent on the dashboard without this counter). Alert on sustained nonzero rate on `outcome=conflict` (a multi-CR namespace is a singleton-guard violation). |
| `openstudio_operator_singleton_loser_skips_total` | `singleton` (`_gated` wrapper `if not active:` branch) (#403) | Per-tick loser suppressions — incremented on EVERY tick whose CR is not the oldest in the namespace (D05). **Labelled by `(module, namespace, name)`** — the LOSER CR's identity; `module` is the wrapped handler's name (same vocabulary as `handler_tick_failures_total`). The per-tick twin of the change-gated #239 election counter, which is silent for a stable multi-CR namespace. | n/a — observability for sustained per-tick loser load; cardinality is bounded by the one-winner-per-namespace invariant (D05). Alert on `rate(...[5m]) > 0` — a sustained multi-CR configuration. |
| `openstudio_operator_events_emit_failures_total` | `events` (`EventEmitter.emit` try/except wrapper) (#255) | `kopf.event` posting failures caught by `EventEmitter.emit`'s try/except wrapper BEFORE re-raising. **Labelled by `reason`** — same vocabulary as `events_emitted_total` — so a dashboard can tell WHICH handler path's Event emission failed. Sustained nonzero rate means the operator cannot post Kubernetes Events to the apiserver, **distinct from** the REST/Redis/K8s API signals that surface via `handler_tick_failures_total`. | n/a — observability for the Event posting path; complements `handler_tick_failures_total` (which captures `ApiException` for all three paths under one label and cannot distinguish "REST API down" from "Event posting down"). |
| `openstudio_operator_prune_tick_failures_total` | `prune_entrypoint` (storage-prune CronJob) (#306) | Skip-tick failures inside the storage-prune CronJob (`prune_entrypoint.main()`). **Labelled by `reason`** — `reason` ∈ {`cr_list_failure`, `runtime_failure`, `redis_url_empty`} — the three bump sites (the K8s API CR list failure, the caught-exception branch, and the exit-3 empty-`spec.redisUrl` guard — #392, so the `rate(prune_tick_failures_total[5m]) > 0` #306 SLO alert covers a sustained redisUrl wedge too). The CronJob pod exposes the same `/metrics` endpoint on port 9090 as the operator, gated by the parallel `openstudio-storage-pruner-metrics-ingress` NetworkPolicy. | n/a — observability for the prune pipeline (separate from the operator's scope); a sustained nonzero rate means the prune pipeline is repeatedly skipping ticks (RBAC, apiserver, NFS, redis-secret KeyRef) and the storage-archival backlog is growing. |
| `openstudio_operator_warnings_deferred_dropped_total` | `events_sinks` (`QueuedKopfEventSink.defer_to_next_tick` cap path) (#310) | `defer_to_next_tick` calls rejected by the sink's cap (MAX_DEFERRED_WARNING_EVENTS = 1000). **Labelled by `reason`** — initial vocabulary is `queue_full` (the only drop path today); the label leaves room for a future per-reason-cap branch without a Counter rename. | n/a — observability for QueueKopfEventSink backpressure; sustained nonzero rate means Warning Events are being silently lost. Pair with `warnings_deferred_queue_depth` to see how close the queue is to the cap on subsequent ticks. |

**Labelled convention (#117).** `handler_tick_failures_total` is the
first labelled counter in the registry and the canonical pattern for
any future "which module is degraded" metric: one labelled family,
`module × error_type`, with one increment per observation. A future
maintainer wiring a new mutation that can fail in catch-and-skip paths
SHOULD extend the same `module` label set (rather than introducing
a new unlabelled counter) so the dashboard's per-module degradation view
stays comparable across error sources — reinventing a non-comparable
unlabelled counter here is exactly the regression #181 guards against.

### Gauges

| Gauge | Module | Sets / Meaning | Notes |
|---|---|---|---|
| `openstudio_operator_resque_workers_seen_max` | `web_background_monitor` (#44/#87) | Monotonic max of distinct Resque worker ids ever observed in process lifetime (`SMEMBERS resque:workers`, emitted every poll regardless of queue depth) | #44 — Resque key-layout leg-2 non-vacuity safeguard; #87 dropped the original `AND queue depth > 0` alert conjunction so the gauge populates on a healthy idle fleet. `0` with a reachable Redis unambiguously means no workers are registered — alert on `== 0`. Not a decision counter — does not follow the §2 anchor pairing convention. |
| `openstudio_operator_resque_queue_depth` | `web_background_monitor` (`_stall_condition_holds` leg-A read) (#238) | LLEN of the two managed Resque queues (`resque:queue:simulations` and `resque:queue:requeued`) on **every** sensing tick (same path the stall-condition leg-A reads, no separate cost). **Labelled by `queue`** (cardinality bounded to the two managed queues — 2 total). | n/a — surfaces the operator's authoritative reading as a cross-check against KEDA's external metrics view (a centralized-constants / live v3.11.0 layout drift (#44/#66/#67) shows up as the operator's depths disagreeing with KEDA's). Alert on `simulations` > 0 sustained while `resque_workers_seen_max == 0` (the dangerous silent misbehavior signature). |
| `openstudio_operator_redis_key_layout_status` | `handlers` (`_check_redis_key_layout_for_cr` per-CR check) (#253) | Cluster-wide latest observation of the boot-time Redis key-layout validator (#163). `1.0` when the most recent `validate_key_layout()` call returned `ok`; `0.0` for every other terminal status (`degraded` \| `unreachable` \| `error` \| `skipped`). One series for the cluster-wide validator state (no per-CR labels — cardinality stays bounded regardless of CR count). | n/a — observability for the post-#44 failure mode (a v3.11.0 layout drift takes `resque_workers_seen_max` silent, the stall condition fires vacuously, and the operator periodic-restarts `web_background` while everything looks healthy). Alert on `== 0` without log scraping. |
| `openstudio_operator_redis_key_layout_status_fresh` | `handlers` (`_set_redis_key_layout_status`, in lockstep with the status gauge at every `_check_redis_key_layout_for_cr` run — the `@kopf.on.event` watch plus the #490 revalidation riding the `web_background_monitor` stall tick) (#490) | Last-run Unix timestamp for the `redis_key_layout_status` data gauge. Set to `time.time()` on EVERY terminal path (`ok` \| `degraded` \| `unreachable` \| `error` \| `skipped` — fresh means recently validated; the status gauge carries the result). | n/a — staleness pair for `redis_key_layout_status`. Pre-#490 the status gauge was watch-event-only (boot listing + CR edits), so a steady-state cluster held its boot value indefinitely and a mid-flight Resque layout drift was both unreported and undetected. Since #490 the web_background timer re-runs the check at most once per `REDIS_KEY_LAYOUT_REVALIDATION_INTERVAL` (5 min, `_constants.py`; half the default `stallWindowMinutes` so drift surfaces before the first vacuous restart window completes). Alert: `time() - openstudio_operator_redis_key_layout_status_fresh > 600` (2× the interval). |
| `openstudio_operator_stall_window_elapsed_seconds` | `web_background_monitor` (`run_stall_tick` post-`tracker.observe()`) (#254) | Sustained-window elapsed seconds for the web_background stall. Set after `tracker.observe()` to the elapsed seconds when the stall condition held this tick, or `0` when it broke (the tracker resets). | n/a — heads-up display between the first sustained observation and the eventual `web_background_restarts_total` increment. Without this gauge, three or more Redis/K8s-leg ticks can accumulate toward a restart with nothing on the dashboard until the gate trips. Rate > 0 means the window is accumulating; exact value shows how close to action (the action fires at `stallWindowMinutes`). |
| `openstudio_operator_resque_queue_depth_fresh` | `web_background_monitor` (`_stall_condition_holds` post-`queue_depths()`) (#312) | Last-successful-update Unix timestamp for the `resque_queue_depth` data gauge. Set to `time.time()` immediately after every successful `queue_depths()` Redis call — NOT touched on the exception path (Redis unreachable, ApiException, etc.). | n/a — staleness pair for `resque_queue_depth`; the data gauge advances on success but is a static stale value on failure, and without this freshness pair the operator has lost visibility silently. Dashboard query: `time() - openstudio_operator_resque_queue_depth_fresh` — alert on a sustained gap (e.g. > 5× the sensing tick cadence). |
| `openstudio_operator_stall_window_fresh` | `web_background_monitor` (`run_stall_tick` post-`tracker.observe()`) (#312) | Last-successful-update Unix timestamp for the `stall_window_elapsed_seconds` data gauge. Set to `time.time()` immediately after the `STALL_WINDOW_ELAPSED_SECONDS.set(...)` sequence on both the holding and broken paths. | n/a — staleness pair for `stall_window_elapsed_seconds`; mirrors the `resque_queue_depth_fresh` round-trip pattern. Resetting only the freshness gauge (simulating "we lost visibility") leaves the data gauge holding its prior value — the exact failure mode #312 fixes. |
| `openstudio_operator_warnings_deferred_queue_depth` | `events_sinks` (`QueuedKopfEventSink.defer_to_next_tick` / `flush`) (#310) | Current depth of the in-process QueuedKopfEventSink queue. Unlabelled (the queue is process-wide, not per-CR) — cardinality stays bounded regardless of CR count. Set on every `defer` / `flush` call. | n/a — observability for QueuedKopfEventSink backpressure. Sustained nonzero values mean the apiserver watch stream is stalled and Warning Events are piling up. Companion to `warnings_deferred_dropped_total` which fires when the cap (MAX_DEFERRED_WARNING_EVENTS = 1000) is exceeded. Alert when the depth approaches the cap (e.g. > 80% of 1000). |
| `openstudio_operator_metrics_server_bound` | `metrics` (`start_metrics_server` first bind attempt) (#393) | Outcome of the /metrics server's FIRST bind attempt: `1.0` on a successful bind, `0.0` on `OSError` (port already in use, unbindable address). Never re-touched after the first attempt. **Labelled by `(addr, port)`** (the configured bind target — `0.0.0.0:9090` in the stock deployment; 1 series, fixed cardinality). Covers both authN modes (open plaintext and the #401 bearer-token server share the single `except OSError` branch). | n/a — the canonical "Prometheus scrape is down because of US" signal (`== 0`). Self-referential edge: a failed bind means this pod's `/metrics` is unscrapeable, so the `0.0` is the durable post-mortem record and pairs with blackbox-exporter `up == 0`; the WARNING log still fires. Not a decision counter — does not follow the §2 anchor pairing convention. |
| `openstudio_operator_handler_last_tick_timestamp` | all four timer wrappers via the shared tick-runner `_oscm_handlers.run_oscm_tick` (#473) (#469) | Unix-epoch seconds of the most recent COMPLETED `run_oscm_tick` invocation per module — the scheduler heartbeat. Set to `time.time()` in a `finally` at the END of every invocation, on EVERY terminal path (successful tick, empty-`serverUrl` idle return, caught skip-tuple failure, propagating uncaught exception). **Labelled by `module`** (the four OSCM timer module names — same vocabulary as `handler_tick_failures_total`; 4 series). The event-driven `dry_run_audit` watch handler (`@kopf.on.event`, not a timer) is consciously EXCLUDED — no cadence to be stale against. | n/a — the only signal whose FLATNESS means the scheduler itself is dead (kopf internals shift so `install_singleton_guard` returns 0 and the timers are silently unwrapped; the CR is deleted; the scheduling loop wedges). Every other family is event-driven and reads green while nothing runs. Alert: `time() - handler_last_tick_timestamp{module=...} > 3 * <interval>` (per-module intervals in `_constants.py`). D11-exempt (in-process metric); dry-run-transparent. |
| `openstudio_operator_dry_run_active` | shared tick-runner `_oscm_handlers.run_oscm_tick` (per-tick stamp, post-#473) + `dry_run_audit` watch handler (#397 wiring point — immediate flip on spec.dryRun transitions) (#492) | **The D11 posture.** 1.0 when the CR's `spec.dryRun` is true (every mutating action suppressed, Events dry-run-marked); 0.0 when mutations are LIVE. **Labelled by `(namespace, name)`** (CR identity, #311; bounded by D05). Stamped per tick right after `OperatorConfig.from_spec` succeeds — before the idle check and the guarded try, so idle and wiring-failing ticks (#493) refresh it too — and flipped immediately on real transitions by the audit watcher. | n/a — `events_dry_run_suppressed_total` (#237) only increments when an action is ATTEMPTED, so a quiet dry-run operator and a quiet live operator produce identical scrapes without this gauge. Alert: `dry_run_active == 1` sustained beyond a migration window on production (shipped as `OpenStudioOperatorDryRunActive`, `for: 1h`). In-process metric — D11-exempt by construction (recording posture is not a mutation). |
| `openstudio_operator_server_url_set` | shared tick-runner `_oscm_handlers.run_oscm_tick` (post-config-parse stamp) (#492) | 1.0 when the CR carries a non-empty `spec.serverUrl` (the authoritative config path, #3); 0.0 = the idle posture (every tick returns early at the empty-serverUrl branch). **Labelled by `(namespace, name)`** (#311; bounded by D05). | n/a — an unexpected 0 on a cluster that should be working means the CR spec is incomplete. |
| `openstudio_operator_redis_url_set` | shared tick-runner `_oscm_handlers.run_oscm_tick` (post-config-parse stamp) (#492) | 1.0 when the CR has a Redis URL source at config-parse time — non-empty inline `spec.redisUrl` OR the #463 `spec.redisCredentials.secretRef` (the preferred production shape; the ref wins when both are present); 0.0 = the #116 refuse-to-operate posture. **Labelled by `(namespace, name)`** (#311; bounded by D05). Distinct from secret-resolution success (client_factory owns that). | n/a — pairs with the per-CR redis-URL guard Warning Event (#116): 0.0 is the posture that guard fires on. |
| `openstudio_operator_auto_soft_stop_enabled` | shared tick-runner `_oscm_handlers.run_oscm_tick` (post-config-parse stamp) (#492) | 1.0 when `analysisPolicy.autoSoftStop` is true (the CRD default) — the SLA monitor is armed; 0.0 = the SLA monitor is fully passive (a full stop — no soft-stop, no escalation, no anchors written). **Labelled by `(namespace, name)`** (#311; bounded by D05). | n/a — previously the debug log line was the only record of a passive SLA monitor. Alert on an unexpected 0 on production. |
| `openstudio_operator_status_map_entries` | `status_store` (`_read_status` — the single read site every RMW cycle and typed getter lands on) (#489) | `len(map)` for each of the four capped `.status` maps, stamped on every read (RMW fresh GETs and plain getters alike; a 409 burst re-stamps per attempt — idempotent `.set()`). **Labelled by `(namespace, name, map_name)`** — `map_name` ∈ {`softStops`, `requeues`, `startedSince`, `archivedAnalyses`} (the exact `.status` map keys, same vocabulary as `status_map_caps_total`); cardinality bounded by D05, one series per CR-map pair. The stamp is read-time: a write's own RMW stamps the pre-write map, the next read stamps the post-write length (every tick reads before deciding, so the gauge is fresh within one poll). | n/a — the LEAD-TIME companion to `status_map_caps_total` (#171): the cap counter + `StatusMapCapped` Warning Event fire only AFTER `STATUS_MAP_MAX_ENTRIES` (10000) is hit and the oldest D04 idempotency anchors are already being dropped (an evicted `softStops`/`startedSince` anchor for a still-relevant analysis silently re-arms the double-soft-stop / double-requeue paths the anchors exist to prevent). Alert on `> 8000` (0.8 × 10000) sustained 30m — shipped as `OpenStudioOperatorStatusMapNearCap`; the 2000-entry headroom is days of runway at typical fill rates, with `archivedAnalyses`' monotonic growth the canonical long-lived-cluster case. In-process metric — D11-exempt; read-path only, forces no write. |
| `openstudio_operator_build_info` | `metrics` (import-time constant stamp, `importlib.metadata`) (#504) | Fleet-identity row: constant `1` recording which operator release is emitting the exposition. Set ONCE at metrics import time — before any handler, config parse, or server bind runs. **Labelled by `(version, python_version)`**: `version` = the installed `openstudio-server-operator` distribution version (`unknown` when the distribution is absent, so a bare-venv import stays alive), `python_version` = `sys.version.split()[0]`. One series, fixed cardinality by construction. | n/a — identity, not health; no alert. During an upgrade the operator is a single-replica Recreate Deployment, so a rolling-window scrape after redeploy mixes series from the old and the new pod with identical labels — previously the only post-hoc correlation was pod-start timestamps against the deployment history. D11-exempt by construction (set at import, before any dry-run-gated action could exist — same shape as the #393 bind gauge); not a decision counter — does not follow the §2 anchor pairing convention. |
| `openstudio_operator_singleton_wrapped_handlers` | `singleton` (`install_singleton_guard` end-of-install stamp) (#491) | Boot-time count of OSCM spawning handlers the singleton guard actually wrapped — set at the END of `install_singleton_guard` to the number of registry entries whose fn carries the gate marker (an idempotent re-install wraps nothing new but keeps reporting the gated population; the internals-mismatch branch sets `0`). Unlabelled — one series, process-wide. | n/a — the RUNTIME half of the kopf-pin fence. `0` on a booted operator that expects timers is the silent-unwrap failure mode (a kopf upgrade moved the private `registry._spawning._handlers` layout — D05 enforcement silently disabled while every timer still fires ungated); the CI-time fence is `tests/test_singleton_registry_coverage.py`. Alert on `== 0` sustained (`OpenStudioOperatorSingletonGuardUnwrapped`, `for: 5m`) — complementary to the #469 heartbeat (`handler_last_tick_timestamp` proves scheduling, this gauge proves guarding). D11-exempt (set at boot wiring time, before any dry-run-gated action could exist); not a decision counter — does not follow the §2 anchor pairing convention. |
| `openstudio_operator_singleton_expected_handlers` | `singleton` (`install_singleton_guard` pre-wrap scan stamp) (#570) | Boot-time count of OSCM spawning handlers the singleton guard EXPECTED to wrap — set BEFORE the wrap loop from the population kopf actually reports (every `registry._spawning._handlers` entry whose selector matches the OSCM resource and whose fn is not None); on the registry-internals-mismatch branch the Python-level `_oscm_handlers.REGISTRY` count stands in (the kopf-side scan is impossible there, and `0 < 0` would silently re-open the gap the `== 0` clause used to cover). Unlabelled — one series, process-wide. | n/a — the wrap-gap DENOMINATOR for `singleton_wrapped_handlers` (#491). A PARTIAL unwrap (one handler skipped for missing #250 Python-registry registration, or a `dataclasses.replace` TypeError) leaves a kopf-registered OSCM timer running UN-GATED while the wrap gauge reads a plausible "3 of 4" and the historical `== 0` alert stays silent — in a multi-CR namespace the ungated timer double-serves the loser CR (the exact D05 violation). Alert on `singleton_wrapped_handlers < singleton_expected_handlers` sustained (the rekeyed `OpenStudioOperatorSingletonGuardUnwrapped`, `for: 5m`) — strict `<` subsumes the old `== 0` clause (a total unwrap is always `0 < N` for any nonzero expectation). D11-exempt like the #491 gauge (boot wiring, before any dry-run-gated action could exist); not a decision counter — does not follow the §2 anchor pairing convention. |

### Histograms

| Histogram | Module | Observes | Buckets |
|---|---|---|---|
| `openstudio_operator_analysis_datapoint_count` | `analysis_sla` + `datapoint_watchdog` (#179, relabelled #472) | Per-tick counts observed by the SLA monitor (`ANALYSIS_DATAPOINT_COUNT.labels(view="analyses_per_tick").observe(len(analyses))` at `handlers/analysis_sla.py`) and the datapoint watchdog (`ANALYSIS_DATAPOINT_COUNT.labels(view="started_datapoints_per_tick").observe(len(started_ids))` at `handlers/datapoint_watchdog.py`). **Labelled by `view`** (#472): the two populations have different units and magnitudes, so the pre-#472 unlabelled merge produced meaningless percentiles and silently reweighted on any cadence change — dashboard queries MUST pin the `view` label. Two series total; still one `.observe()` per observed count, not per-CR (per-CR labelling would multiply the series count by the analysis count and defeat the bounded-cardinality design). The observation site is `analysis_sla.py` for the SLA branch and `datapoint_watchdog.py` for the watchdog branch — both increment identically under `spec.dryRun` (D11-exempt category — in-process metrics, not cluster state). | `[5, 10, 50, 100, 500, 1000, 5000]` — bucket-capped to bound per-(analysis \| datapoint) cardinality while still surfacing the "we just started getting 5000-point analyses" shift (Goal 10, OSS hardening). |
| `openstudio_operator_handler_tick_duration_seconds` | all four timer wrappers (`analysis_sla` / `datapoint_watchdog` / `worker_recycler` / `web_background_monitor`) (#308) | Per-`module` wall-clock duration of the four `@kopf.timer` wrappers, observed regardless of success or caught-exception outcome. **Labelled by `module`** (same vocabulary as the failure counter). Sustained degradation (REST 5xx storm, GC pause, kopf bus contention, NFS stall) is visible to Prometheus BEFORE it crosses the failure threshold captured by `handler_tick_failures_total`. | `[0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60]` seconds — covers the healthy band (sub-second typical) through the action threshold (the timer wrappers run at cadence 30-60s; an observation > 60s means the tick crossed the next-cadence boundary). |
| `openstudio_operator_rest_request_duration_seconds` | `openstudio_client` (`_request` retry envelope) (#308) | Per-`(method, outcome)` wall-clock duration of OpenStudioClient REST calls — includes the GET-only 3× retry envelope. **Labelled by `(method, outcome)`** — `method` ∈ {`GET`, `POST`, `DELETE`} (the verbs the operator actually uses); `outcome` ∈ {`"200"`, `"exception"`}. Sustained non-zero rate on `outcome="exception"` is the canonical REST-degraded alert. | `(0.05, 0.1, 0.5, 1, 2, 5)` — the canonical set from #308; pinned so a future refactor that broadens or narrows the resolution at the healthy band is caught at CI. |
| `openstudio_operator_redis_request_duration_seconds` | `redis_client` (`queue_depth` LLEN · `worker_heartbeats` SMEMBERS+HGETALL incl. `stale_workers` delegation · `validate_key_layout` EXISTS probes + failure-path SCAN loop (#688) · `workers_for_analysis` SMEMBERS+GETs) (#488) | Wall-clock duration of every ReadOnlyRedisClient read method, observed on BOTH success and failure paths (duration is duration — error counting lives on the tick-failure counters). **Labelled by `operation`** — `llen` \| `smembers` \| `scan` \| `exists` (`exists` is the #688 layout-verdict probe; a method issuing multiple commands is timed once under its dominant operation label). Attributes an inflated tick-duration bucket to Redis vs REST vs kube. | `(0.05, 0.1, 0.5, 1, 2, 5)` — the canonical #308 set, shared so the three dependency histograms render on one dashboard axis. |
| `openstudio_operator_kube_api_request_duration_seconds` | `status_store` (`_read_status` GET · `_mutate` PATCH) · `_k8s` (`deployment_label_selector` GET · `rolling_restart_deployment` PATCH) · `analysis_sla` (escalation LIST + DELETE) · `web_background_monitor` (leg-C pod LIST) (#488) | Wall-clock duration of the operator's Kubernetes API calls at the wrapped chokepoints, observed on BOTH success and failure paths. A slow-but-SUCCESSFUL apiserver is the blind spot the #119 409 counters leave open. **Labelled by `verb`** — `get` \| `patch` \| `delete` \| `list`. | `(0.05, 0.1, 0.5, 1, 2, 5)` — the canonical #308 set, shared with the REST + Redis dependency histograms. |

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
