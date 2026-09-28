# Log — experiment 4 (branch-tree shape vs read scaling)

Chronological process log: every setup/modification step, and why. Complements
`README.md`, which is outcome-oriented; this file is process-oriented. Written
retroactively for steps before this policy existed (see `agent/CLAUDE.md`'s
"Experiments" section), then kept current going forward.

## Exploration and planning

- Explored `experiments/2-branch-interference-cluster/` (the predecessor this is
  modeled on) and the Neon branching/read-only-compute APIs via two parallel
  Explore agents, to reuse existing patterns (`lib.py`'s SSH/HTTP helpers, the
  4-arm design) rather than re-deriving them. Findings: experiment 2's deployment
  runbook (`agent/experiment-2-cluster-plan.md`, `-progress.md`) referenced by its
  README no longer exists and isn't in git history; branch creation supports
  `ancestor_start_lsn` but experiment 2 never used it (always branched at tip);
  read-only computes need `ComputeMode::Static(lsn)` + an LSN lease; no depth limit
  exists in the pageserver's ancestor-chain read path.
- Verified CloudLab cluster 2 (8x c220g5) was live and reachable, `/mydata` empty
  and root-owned, `/proj` had headroom. Confirmed the coordinator (`dassl-serv-01`,
  x86_64 Ubuntu 22.04.5) is ABI-identical to the cluster nodes and
  `/mydata/jiyu/neon/target/release` already has a `features: ["testing"]` release
  build — decided to rsync binaries instead of rebuilding on the aarch64/x86_64
  cluster (avoids repeating experiment 2's protoc saga).
- SSHed into CloudLab cluster 1 (still running experiment 2's stack) to recover the
  lost service configs and command lines, since experiment 2's README pointed at
  now-missing files for them. Recovered `pageserver.toml`, the safekeeper/
  storage_controller/storage_broker/compute-hook-stub command lines, and the
  compute-hook stub's source, by reading them directly off the running cluster 1
  nodes. Committed as `config/pageserver.toml.tmpl`, `config/compute_hook_stub.py`,
  and encoded as executable steps in `scripts/deploy.py` (so this recovery effort
  isn't repeatable-by-hand-only next time).
- Wrote the full plan (`/home/jiyu/.claude/plans/refer-to-agent-cloudlab-md-and-cosmic-newell.md`),
  flagging two numerical concerns before committing machine-time: the 5%-divergence
  disk budget (later shown to be far worse than guessed) and the depth-effect
  saturation point (later moot once divergence was cut to 0.1%). Got user sign-off
  via `AskUserQuestion` on: both tiers (storage+compute), N up to 64, ~300GB root
  (scale 20000), 5% divergence.

## Harness implementation

Ported `experiments/2-branch-interference-cluster/scripts/lib.py` into this
experiment's `scripts/lib.py`, keeping the load-bearing gotchas verbatim
(`REMOTE_HOME` never `~`, `ssh_pkill`'s self-match bracket trick,
`spec.safekeepers_generation = None`, `_timeout_wrap` around pagebench). Retargeted
`NODES`/roles at cluster 2's 8 hosts (verified IPs/hostnames this session). Added,
beyond the port:

- `timeline_branch(..., ancestor_start_lsn=...)` — experiment 2 never passed this;
  the vertical chain needs it for reproducible branch points.
- `wait_for_last_flush_lsn` — checkpoint the compute, poll the pageserver's
  `last_record_lsn` to the flushed WAL position. Needed before every branch so
  branching at "tip" doesn't miss just-written data.
- Static (read-only, pinned-LSN) compute config support in `_build_compute_config`,
  for Tier COMPUTE.
- `tenant_config_patch` (PATCH `/v1/tenant/config`) to flip `image_creation_threshold`
  between "small" (root build) and "huge" (branch build) — see the compaction-
  discipline bug below for why this exists.
- `divergence_write` / `calibrate_page_rows` / `table_row_count` for the
  page-scattered divergence UPDATEs.
- `diskstats_now` for the disk-bound sanity check figure.
- `has_image_layer` / `PSHttp.layer_map_info` to verify children never get an image
  layer of their own.

Wrote `params.py` (pilot/prod switch via `PILOT` env var), `deploy.py` (Phase 1),
`setup_root.py` (Phase 2), `materialize.py` (Phase 3), `measure.py` + `sweep.py`
(Phase 4), `analyze.py` (Phase 5) — all new, modeled on experiment 2's script split
but adapted for two shapes instead of three arms, and for per-branch (not
per-load-node) pagebench processes so per-depth latency is directly available.

## Phase 1 — deploy

Ran `deploy.py` end-to-end: `chown /mydata` on all 8 nodes, rsync binaries (~510MB-
595MB per relevant node) + `pg_install/v17` only (the tenant only uses pg_version
17, so v14-16 aren't needed), bring up services in the recovered order. Verified
idempotent by re-running.

**Bug found and fixed: safekeeper's `--remote-storage={local_path="..."}` TOML
literal lost its quotes.** `ssh_background` wraps the remote command in an explicit
`bash -c '<cmd>'`, which is itself parsed by a shell (in addition to sshd's own
implicit parse of the outer SSH command) — a literal `"` in the command text is
consumed as shell-quoting syntax by that second parse and never reaches the
binary's argv. Fixed by escaping as `\"` in the Python string, which survives both
parses (single-quote-protected through the first, backslash-escaped through the
second). Confirmed via `ps aux` showing the quotes intact in the running process's
argv.

**Bug found and fixed: the pageserver doesn't self-register with
storage_controller.** `GET /control/v1/node` came back empty after pageserver
startup — its own `/upcall/v1/re-attach` call is for tenant-attachment bookkeeping
only (`register: None` for an unknown node), not topology registration. Added
`lib.register_pageserver_node` (`POST /control/v1/node` with the
`NodeRegisterRequest` shape from `libs/pageserver_api/src/controller_api.rs`) to
`deploy.py`'s `wait_for_registration`.

## Phase 0 — pilot (first attempt, scale 500, 5% divergence)

Ran `PILOT=1 setup_root.py`. The local Bash-tool call hit its own timeout while
`pgbench -i` was still running remotely; confirmed via `pgrep` on the remote node
that the detached-by-SSH-but-not-explicitly-backgrounded `pgbench -i` process kept
running unaffected. Wrote a one-off continuation script
(`_finish_pilot_root.py`, since deleted) using the tenant/timeline IDs from the
killed run's stdout, waited for the remote process to exit, then ran the rest of
`setup_root.main()`'s logic by hand and wrote `cluster_state_pilot.json` manually
(this also surfaced a "forgot to reproduce `PILOT=1`'s effect on the state
filename/tag" slip, caught and fixed by hand).

**Bug found and fixed: `compact` without force flags never creates image
layers.** Ran `checkpoint -> compact x5 -> do_gc`; layer map showed 0 image layers,
39 delta layers. `PUT .../compact`'s query params
(`force_l0_compaction`/`force_repartition`/`force_image_layer_creation`, see
`pageserver/src/http/routes.rs`'s `timeline_compact_handler`) are all off by
default — image-layer creation only happens after repartitioning, which is
otherwise timer-driven and never fires since `compaction_period=0s`. Added the
three force-flag parameters to `lib.PSHttp.compact` and set them in
`setup_root.compact_to_images`; re-running produced 52 image layers immediately.

Started branch materialization (`materialize_horizontal`/`materialize_vertical`,
8-wide + 8-deep). All 8 horizontal branches failed identically: pageserver
`406 Not Acceptable: invalid branch start lsn: less than latest GC cutoff`.

**Bug found and fixed: `root_lsn` must be read after `do_gc`, not before.**
`setup_root.py` had captured `root_lsn` from `wait_for_last_flush_lsn` *before*
running `compact_to_images` (which ends in `do_gc(gc_horizon=0)`). Since
`gc_horizon=0` + `pitr_interval=0s` collapse the GC cutoff to the tip *at the
moment `do_gc` runs* — which had advanced microscopically past the earlier flush
point by then — every branch attempt at the stale LSN failed. Fixed by moving the
`root_lsn` read to *after* `compact_to_images`, using the timeline's
`last_record_lsn` at that point (always `>=` the cutoff that produced it, so always
valid to branch from). Manually recomputed and patched the already-built pilot
root's `cluster_state_pilot.json` rather than rebuilding, then re-ran
materialization successfully: all 8 horizontal branches completed, each verified
`clean_no_image_layer: true`.

While the vertical chain (serial) was running, its first level's divergence write
exceeded its 1800s timeout and crashed the whole script (unhandled
`subprocess.TimeoutExpired`). Investigated via `pg_stat_activity` on the writer
compute mid-query: `wait_event = Neon/PS_ReadIO`, i.e. it really was doing GetPage
I/O, not hung. Measured aggregate throughput from the completed horizontal batch
(~24 pages/sec per writer connection, from 8 branches x 40,983 pages completing in
~28 min running 8-way parallel) and used it to project full-scale (root_scale
20000, 40x pilot) cost at the user's original 5% divergence: ~19h/branch, ~50 days
for a 64-level serial vertical chain, and ~2.3TB of disk against 1.4TB available
(the disk-per-branch overhead measured ~1.4x the raw touched-page bytes, the
*opposite* of the plan's guessed 0.4x compression factor).

Presented these two blocking findings to the user via `AskUserQuestion` (paths
considered: cut divergence to 0.5% or 0.1%, shrink root scale instead, or specify
custom numbers; separately, whether to cap the serial vertical arm's N below 64).
**Decision: divergence 5% -> 0.1%; `VERTICAL_N_MAX` 64 -> 16 (horizontal stays
64).** Encoded in `params.py` as per-shape `HORIZONTAL_N_MAX`/`VERTICAL_N_MAX`/
`HORIZONTAL_N_GRID`/`VERTICAL_N_GRID` (previously a single shared `N_MAX`/`N_GRID`
— updated `sweep.py` and `analyze.py`'s four `N_GRID` call sites to key off shape).

## Phase 0 — pilot (second attempt, corrected parameters)

While re-timing a single divergence write (to get a clean number uncontaminated by
the crash above), found its `EXPLAIN` was a **parallel sequential scan over the
whole 50M-row table**, not the intended index lookup — `SELECT indexname FROM
pg_indexes WHERE tablename='pgbench_accounts'` returned zero rows.

**Bug found and fixed: `pgbench -i -I dtGvp`'s combined primary-key step silently
didn't build the index.** This mattered a lot: a full-table seq scan doesn't get
cheaper as `divergence_fraction` shrinks (it touches every page regardless of how
many match), so the just-decided 0.1%-divergence fix would not have delivered its
intended speedup without this also being fixed. Manually running
`pgbench -i -I p -s 500 <connstr>` alone built the missing index in 400s and fixed
the plan (`Nested Loop` + `Index Scan`, cost ~1,090,000 -> ~2,335). Added
`lib.verify_pgbench_pkey_exists`, called from `lib.pgbench_init` immediately after
init — and restructured `pgbench_init` to run `-I dtGv` and `-I p` as two separate
invocations rather than one combined `-I dtGvp` (the combined form failed silently
*twice*, including once on a run that was confirmed to complete on its own with no
interruption from this end — root cause not isolated, but the split-and-verify
form is what's proven to work).

Deleted the messy pilot tenant (`DELETE /v1/tenant/{id}`, confirmed via
`GET /v1/tenant` returning `[]` and node0's disk dropping back to ~56KB used) and
manifests, rather than trying to patch around the accumulated one-off fixes and
stray test writes (an ad-hoc `EXPLAIN`/timing test had been run directly against
the root's own persistent compute, dirtying it past its recorded `root_lsn` — a
mistake: should have used a disposable target, not the experiment's actual root,
for that side investigation).

Restarted `PILOT=1 setup_root.py` clean. Hit the same local-tool-timeout-vs-remote-
survives pattern as the first pilot attempt; wrote another one-off continuation
(`_finish_setup_root.py`) with the new tenant/timeline IDs. This run's
`pgbench -i -I dtGv` completed, but `verify_pgbench_pkey_exists` correctly caught
the primary key was *still* missing — confirming the "sometimes silently fails"
behavior is real and not just an artifact of interruption, and validating that the
new verification check does its job.

**In progress:** building the missing primary key directly against the current
root data (to avoid re-running ~35min of data generation) via the now-separate `-I
p` step. First attempt at this used `params.ROOT_SCALE` without `PILOT=1` set in
that shell invocation, silently pointing `-s` at 20000 instead of 500 — a
scale-flag mismatch caught before it could write a wrong `cluster_state_pilot.json`
(would have gone to the pilot's tenant ID but with `root_scale: 20000` recorded and
the pilot/prod state-file-naming confusion from earlier recurring). Retrying with
the correct env var.

Retried the missing-index build directly via idempotent SQL
(`CREATE UNIQUE INDEX IF NOT EXISTS pgbench_accounts_pkey ...`, bypassing pgbench's
`-I p` entirely — see the fix above). First two attempts were killed by hand after
~30 minutes each, on the assumption they were stuck; **this was a mistake**: killing
a backend mid-`CREATE INDEX` rolls back the entire (non-concurrent) index build,
so each kill just threw away 30 minutes of real progress and forced a full restart.
Recognized this after the second kill and committed to letting the third attempt
run through to completion, however long it took, monitoring via `pg_stat_activity`
without interrupting.

**Finding: building a plain B-tree index on the existing 50M-row table took 2724s
(~45.4 min)**, roughly 7x slower than the first attempt's lucky 400s. Likely
explanation: the 400s run happened immediately after `pgbench -i`'s data
generation, when the touched pages were still resident in the pageserver's own
page cache; by the third attempt, several intervening operations (failed builds,
`pg_terminate_backend`, deletions, compactions) had evicted that warmth, so this
build paid full GetPage@LSN latency for its full-table scan phase (node0's own
load average stayed at ~0.5 throughout — the pageserver was healthy and idle, not
overloaded; the cost is inherent per-page round-trip latency, not contention).

**This is a new, previously unflagged cost for the full-scale run**: unlike a
divergence write (which only touches `divergence_fraction` of the table), building
`pgbench_accounts`' primary key is an unavoidable one-time **full-table** operation
on the root. Linearly projecting the pilot's 2724s across the 40x row-count
increase to full scale (root_scale 20000): **~30 hours** just for this one index
build, before any branch is created. This must be reported to the user alongside
the earlier divergence-write/vertical-chain-depth findings, as it affects the Phase
2 (root build) timeline independent of anything already decided about
`DIVERGENCE_FRACTION` or `VERTICAL_N_MAX`.

## Open items

- Root cause of `pgbench -i -I dtGvp`'s silent primary-key-step failure IS now
  understood (see above): it is not idempotent and touches all four standard
  tables' primary keys unconditionally, so a table that already has some of them
  (from an earlier partial run) causes every subsequent attempt to fail immediately
  on those, before ever reaching pgbench_accounts. The direct-SQL,
  `IF NOT EXISTS`-guarded workaround in `lib.pgbench_init` is the fix; no further
  investigation needed.
- The ~7x slowdown between the first (400s) and third (2724s) index-build attempts
  is attributed to pageserver-side cache warmth, not confirmed via direct
  cache-hit-rate instrumentation (`pageserver_page_cache_read_hits_total` /
  `_accesses_total` were not pulled for this specific operation). A natural next
  step if this matters more precisely later.
## Phase 0 pilot: materialization with the corrected parameters, and first sweep test

With the root's index genuinely in place, re-ran materialization (0.1% divergence,
8 horizontal + 4 vertical for a quick pilot check). Hit one more leftover-state
bug: `h0`'s compute failed with "could not bind IPv4 address 0.0.0.0: Address
already in use" on port 55433. Cause: `div-n2-vchain` (the vertical chain's writer
from the very first, crashed pilot attempt, hours earlier) was still running,
bound to that port — its `_populate_branch` call had raised mid-`divergence_write`
(the 1800s-timeout crash from earlier in this log), so control never reached its
`endpoint_stop` call. `endpoint_stop` is keyed by `endpoint_id`, not by port, so a
*different* endpoint_id (`div-n2-p55433`) reusing the same port had no way to know
to stop it first. Killed the zombie by hand (`kill -9` on the compute_ctl and its
child postgres) and re-ran; this time both trees materialized cleanly and fast:
**33.4s for 8 horizontal branches (8-way parallel), 68.1s for 4 vertical levels
(serial)** — all 8+4 branches recorded exactly 819 pages touched (matching
`0.1% x ~819,672 total pages`) and `clean_no_image_layer: true`. This is the
proof-of-concept that the divergence-fraction fix delivers the expected speedup
once the index is real: **~24.5 pages/sec/connection under 8-way contention,
~48 pages/sec/connection solo** — both broadly consistent with the very first
(pre-bug-fix) horizontal measurement's implied rate, meaning the divergence-write
throughput ceiling itself hasn't changed; what changed is that 0.1% divergence
needs far fewer pages touched than 5% did.

Wrote a small standalone test of `measure.measure_storage_point` (N=2, 20s runtime)
to validate the Tier STORAGE measurement path end-to-end for the first time.

**Bug found and fixed: `measure_storage_point`'s `collect` was being called with
full entry dicts instead of their `idx` values** (`ex.map(collect, [e for e in
entries])` should have been `[e["idx"] for e in entries]`), causing
`TypeError: unhashable type: 'dict'` on the very first attempt to look up
`remote_logs[idx]`. One-line fix in `measure.py`.

## Open items

- Root cause of `pgbench -i -I dtGvp`'s silent primary-key-step failure IS now
  understood (see above): it is not idempotent and touches all four standard
  tables' primary keys unconditionally, so a table that already has some of them
  (from an earlier partial run) causes every subsequent attempt to fail immediately
  on those, before ever reaching pgbench_accounts. The direct-SQL,
  `IF NOT EXISTS`-guarded workaround in `lib.pgbench_init` is the fix; no further
  investigation needed.
- The ~7x slowdown between the first (400s) and third (2724s) index-build attempts
  is attributed to pageserver-side cache warmth, not confirmed via direct
  cache-hit-rate instrumentation (`pageserver_page_cache_read_hits_total` /
  `_accesses_total` were not pulled for this specific operation). A natural next
  step if this matters more precisely later.
- `lib.endpoint_stop`/`endpoint_start` key liveness tracking purely by
  `endpoint_id`, with no cross-check against the port a NEW endpoint is about to
  bind. A crashed script that skips its own cleanup can leave a zombie compute
  squatting on a port that a later, differently-named endpoint then collides with.
  Not fixed generically (would need a port-level liveness check in
  `endpoint_start`) -- worth doing if this recurs.
Re-ran the fixed test: no crash, but every branch came back `request_count: 0,
missed: 0` despite the pageserver's own `redo_delta` metrics showing real traffic
(`pageserver_get_vectored_seconds_count` +7946 during the window) and the raw
remote log showing a live, healthy `RPS: ~110-130 MISSED: 0` stream throughout.

**Bug found and fixed: `measure_storage_point` killed its own background pagebench
processes 2 seconds before their `--runtime` deadline.** Each background process is
launched with `--runtime (runtime_s + settle_s)`; the code then did
`time.sleep(runtime_s + settle_s - 2)` before `ssh_pkill`-ing it — 2 seconds short
of that same deadline. `ssh_pkill` killing the process mid-flight meant it never
reached its own graceful-exit/JSON-print step, so `collect()`'s log-parsing found
no JSON block and silently fell back to its empty default (this is silent because
`pagebench_summary` treats "no JSON" as a valid empty result rather than an error —
worth keeping in mind for future debugging: a 0 in these fields doesn't
distinguish "genuinely idle" from "killed too early"). Fixed by sleeping
`runtime_s + settle_s + 5` (past the deadline with margin) before collecting/
killing, since collect() runs afterward regardless.

## Open items

- Root cause of `pgbench -i -I dtGvp`'s silent primary-key-step failure IS now
  understood (see above): it is not idempotent and touches all four standard
  tables' primary keys unconditionally, so a table that already has some of them
  (from an earlier partial run) causes every subsequent attempt to fail immediately
  on those, before ever reaching pgbench_accounts. The direct-SQL,
  `IF NOT EXISTS`-guarded workaround in `lib.pgbench_init` is the fix; no further
  investigation needed.
- The ~7x slowdown between the first (400s) and third (2724s) index-build attempts
  is attributed to pageserver-side cache warmth, not confirmed via direct
  cache-hit-rate instrumentation (`pageserver_page_cache_read_hits_total` /
  `_accesses_total` were not pulled for this specific operation). A natural next
  step if this matters more precisely later.
- `lib.endpoint_stop`/`endpoint_start` key liveness tracking purely by
  `endpoint_id`, with no cross-check against the port a NEW endpoint is about to
  bind. A crashed script that skips its own cleanup can leave a zombie compute
  squatting on a port that a later, differently-named endpoint then collides with.
  Not fixed generically (would need a port-level liveness check in
  `endpoint_start`) -- worth doing if this recurs.
Re-ran the same N=2 test with the timing fix: **fully valid data** for the first
time in this experiment -- `request_count` ~4100-4164 per branch over the 20s
window (2 clients each), `latency_mean_ms` ~17, `latency_p99_ms` ~57,
`latency_p99.9_ms` ~77, `missed: 0` for both branches. Sane numbers, no crashes,
no silent empty results.

Also smoke-tested `measure_compute_point` (Tier COMPUTE, N=1, static read-only
endpoint, `pgbench -S`): completed cleanly in 19.5s, `tps=114.67`,
`latency_avg_ms=34.88`. This path had never been exercised before this test and
needed no fixes.

**Phase 0 is now complete**: every stage of the pipeline (deploy, root build,
materialize both shapes, Tier STORAGE measurement, Tier COMPUTE measurement) has
run successfully end-to-end at pilot scale, with real, sane data at every stage.
Three real bugs were found and fixed in the measurement path alone
(`measure_storage_point`'s dict-vs-idx `TypeError`, its 2-second-early `pkill`),
on top of the three found earlier in root-build/materialization (missing
`register_pageserver_node` call, `compact` needing force flags, `root_lsn` read
timing, the non-idempotent `pgbench -i -I p` step). None of these would have
surfaced without actually running the full pipeline end-to-end at least once —
this is the concrete payoff of treating Phase 0 as a real gate rather than a
formality.

## Open items

- Root cause of `pgbench -i -I dtGvp`'s silent primary-key-step failure IS now
  understood (see above): it is not idempotent and touches all four standard
  tables' primary keys unconditionally, so a table that already has some of them
  (from an earlier partial run) causes every subsequent attempt to fail immediately
  on those, before ever reaching pgbench_accounts. The direct-SQL,
  `IF NOT EXISTS`-guarded workaround in `lib.pgbench_init` is the fix; no further
  investigation needed.
- The ~7x slowdown between the first (400s) and third (2724s) index-build attempts
  is attributed to pageserver-side cache warmth, not confirmed via direct
  cache-hit-rate instrumentation (`pageserver_page_cache_read_hits_total` /
  `_accesses_total` were not pulled for this specific operation). A natural next
  step if this matters more precisely later.
- `lib.endpoint_stop`/`endpoint_start` key liveness tracking purely by
  `endpoint_id`, with no cross-check against the port a NEW endpoint is about to
  bind. A crashed script that skips its own cleanup can leave a zombie compute
  squatting on a port that a later, differently-named endpoint then collides with.
  Not fixed generically (would need a port-level liveness check in
  `endpoint_start`) -- worth doing if this recurs.
- A `request_count`/`missed` of 0 from `pagebench_summary` does not distinguish
  "genuinely no traffic" from "the process was killed before it could report" --
  worth adding an explicit sentinel (e.g. `None` vs `0`, or a `parse_ok: bool`
  field) if silent zeros cause confusion again.
## Phase 2 (production root build): three attempts, two scale changes

**Attempt 1 -- `root_scale=20000` (~300GB), matching the original plan exactly.**
Launched `setup_root.py`; presented the ~2.74-day projection (from the validated
Phase 0 numbers) to the user before it got far. **Decision: shrink the root.**

**Attempt 2 -- `root_scale=3000` (~59GB)**, the user's chosen shrink. Ran
successfully for ~5 hours (data generation completed, ~1h into the primary-key
index build, no errors) before the user reframed the whole experiment's purpose:
"this is just a proof of concept ... to verify that branching does cause
performance interference[,] and explore the difference between depth and width
branching ... I hope to finish this by at most tomorrow morning." Given that, even
the ~13h projection for `root_scale=3000` was more runway than needed and cut it
closer to the deadline than comfortable. **Decision: shrink again, harder, and
trim reps/runtime too** -- a POC doesn't need publication-grade sample sizes.
Killed the index-build process (a bare `pkill`, not a `pg_terminate_backend`
this time, since the whole tenant was about to be deleted anyway so its rollback
didn't matter), deleted the tenant via `DELETE /v1/tenant/{id}`, removed the
`*_prod.json` state/manifest files.

**Attempt 3 -- `root_scale=500`, matching the PILOT exactly** (chosen specifically
so root-build timing has *direct* measured data, not a fresh extrapolation:
~37min data gen, 6.7-45min pkey index depending on cache warmth, ~16.6min
compaction). Kept `HORIZONTAL_N_MAX=64`/`VERTICAL_N_MAX=16` and both grids at
their original full values -- at this scale, materializing the full grid only
costs minutes (819 pages/branch), so there's no reason to narrow it; the
depth-vs-width comparison the user actually wants is exactly what the full grid
shows. Trimmed `REPS` 5->3, `COMPUTE_TIER_REPS` 3->2, `STORAGE_RUNTIME_S` and
`COMPUTE_TIER_RUNTIME_S` 60->30 for extra speed margin against the deadline.
Revised total projection: **~2-2.7h**, comfortably inside "by tomorrow morning."
Launched and running as of this log entry -- see `agent/session-handoff-experiment4.md`
for the live PID/log path if picking this back up.

## Phase 3+4 (production materialize + measure): complete

`sweep.py` ran to completion: 64 horizontal + 16 vertical branches materialized
(all `clean_no_image_layer: true`), then Tier STORAGE (36 points: 7 horizontal +
5 vertical N-values x 3 reps) and Tier COMPUTE (12 points: 3 N-values x 2 shapes x
2 reps) measured. All 48 points recorded to `data/raw_prod.jsonl` (points that
failed still get a record, with an `error` key, per `sweep.py`'s existing
try/except -- nothing was lost or silently dropped).

**Data quality**: 1 Tier STORAGE point failed on a transient SSH connection reset
to a load-generator node (`kex_exchange_identification: read: Connection reset by
peer`) -- a one-off network blip, not investigated further (2 of that point's 3
reps still succeeded). 8 of 12 Tier COMPUTE points failed with
`Failed to acquire lsn lease: error connecting to server: Cannot assign requested
address (os error 99)` inside compute_ctl's own `lsn_lease_bg_task` -- this is
`EADDRNOTAVAIL`, i.e. **ephemeral port exhaustion** on the node running the static
compute. Root cause: `measure_compute_point` starts and stops a `compute_ctl`
process (and its child postgres) per branch per point, back-to-back, with no
cooldown -- at N=16 that's 16 start/stop cycles in quick succession on top of
whatever the previous points already did, and each teardown leaves sockets in
TIME_WAIT for ~60s. By the time later points ran, the two compute-tier nodes had
exhausted their local ephemeral port range faster than TIME_WAIT sockets could
free up. **Not fixed in code** (out of scope given the deadline and Tier COMPUTE's
secondary role in the design) -- a real fix would add a short pause between
teardown and the next startup, or reduce concurrent endpoint churn, or increase
`net.ipv4.ip_local_port_range`/enable `tcp_tw_reuse` on the affected nodes.

Ran `analyze.py` on the resulting data -- see `README.md`'s `## Results` and
`## Takeaways` sections for the full write-up. Headline: branching clearly causes
latency interference that grows with N (confirmed, p99 ~50ms at N=1 to ~2.4s at
N=64); branch-tree *shape* does not meaningfully change end-to-end latency at
matched N, even though the ancestor-walk mechanism (`layers_per_read`) is directly
confirmed and grows cleanly with depth (2.8 -> 11 layers, depth 1 -> 16) --
per-hop cost is apparently too cheap, relative to load-driven contention, to show
up in end-to-end latency at this depth range and divergence fraction.

**Also worth noting**: the root build finished successfully at ~06:43 UTC on
2026-09-25, but nothing was watching for that completion, so the pipeline sat idle
until ~18:19 UTC when the user asked for a status update and the gap was caught --
almost 11.6 hours of dead time. The deadline still had enough slack to absorb this,
but it's a real process failure, not a close call. See
`agent/session-handoff-experiment4.md`'s note on this for the lesson (always leave
a Monitor armed across a phase boundary, or say explicitly when stepping back from
active monitoring and when you'll check again).

## Open items (carried forward)

- Root cause of `pgbench -i -I dtGvp`'s silent primary-key-step failure IS now
  understood: it is not idempotent and touches all four standard tables' primary
  keys unconditionally, so a table that already has some of them (from an earlier
  partial run) causes every subsequent attempt to fail immediately on those,
  before ever reaching pgbench_accounts. The direct-SQL, `IF NOT EXISTS`-guarded
  workaround in `lib.pgbench_init` is the fix; no further investigation needed.
- The ~7x slowdown between the first (400s) and third (2724s) index-build attempts
  is attributed to pageserver-side cache warmth, not confirmed via direct
  cache-hit-rate instrumentation (`pageserver_page_cache_read_hits_total` /
  `_accesses_total` were not pulled for this specific operation). A natural next
  step if this matters more precisely later.
- `lib.endpoint_stop`/`endpoint_start` key liveness tracking purely by
  `endpoint_id`, with no cross-check against the port a NEW endpoint is about to
  bind. A crashed script that skips its own cleanup can leave a zombie compute
  squatting on a port that a later, differently-named endpoint then collides with.
  Not fixed generically (would need a port-level liveness check in
  `endpoint_start`) -- worth doing if this recurs.
- A `request_count`/`missed` of 0 from `pagebench_summary` does not distinguish
  "genuinely no traffic" from "the process was killed before it could report" --
  worth adding an explicit sentinel (e.g. `None` vs `0`, or a `parse_ok: bool`
  field) if silent zeros cause confusion again.
- **Experiment complete.** `README.md`'s `## Results` and `## Takeaways` are
  written up from the real production data. Remaining loose end, if anyone wants
  to pick it up: fix Tier COMPUTE's ephemeral-port-exhaustion bug (see above) and
  re-run just that tier for a cleaner secondary-tier comparison.

## 2026-09-27: remote-storage backend comparison (local_fs / MinIO / S3)

Built the pluggable backend (`storage_backend.py`, `backend_sweep.py`, `analyze_backend.py`,
`deploy.py --backend`) and ran the warm horizontal sweep N=1..32 × 3 reps on each backend. See
README "Follow-up: pluggable remote-storage backend". Issues hit along the way:

- dl.min.io returns **410** for both the community `minio` server and `mc`. Built MinIO from source
  with Go 1.27.1 (`go install github.com/minio/minio@master`, CGO off) and used the AWS CLI v2
  (installed without root on node0) instead of `mc`.
- The IAM policy initially lacked `s3:ListBucket` on the **bucket** ARN (object ARNs don't cover
  it). The pageserver needs it for attach. `GetBucketLocation` is still denied, which is harmless:
  the region is configured.
- The first `switch` crashed polling the pageserver while its HTTP port was still closed (curl
  rc=7, while storcon's "Active" was stale). Now tolerated.
- The probe got **403** from MinIO: curl 7.81's `--aws-sigv4` omits `x-amz-content-sha256`. Replaced
  it with `aws s3 presign` URLs plus plain curl.
- **Process failure:** the MinIO arm sat idle after the local_fs sweep finished until the user asked
  for status. Everything after that ran as one chained script under a phase monitor.
- The drift-control point showed a post-restart transient (layers/read 3.43, HDD at 100%, p99
  2322 ms). A re-measurement 4 min later was normal. See the README caveats.

Cluster left on **local_fs**. MinIO is still running on node3, and the prod data is still in the
MinIO and S3 buckets (see the cleanup notes in the final report).
