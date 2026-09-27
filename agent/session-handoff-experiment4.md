**EXPERIMENT COMPLETE as of 2026-09-25 ~19:30 UTC.** Root build, materialize, and
both measurement tiers all finished; `README.md`'s `## Results` and
`## Takeaways` are written up. This file is kept for historical/process context
(the mid-run scale changes, the idle-gap lesson) but there is no more background
work running for this experiment. If picking this up fresh, just read
`experiments/4-branch-shape-scaling-cluster/README.md` and `log.md` -- you
shouldn't need anything below this point.

---

# Session handoff — experiment 4 (branch-tree shape vs read scaling)

**UPDATED 2026-09-25 ~18:25 UTC**: the user reframed this as a proof-of-concept
("verify branching causes interference + explore depth vs width") with a
same-day deadline ("finish by tomorrow morning"). The original `root_scale=20000`
(~300GB, ~2.74 day projection) run was abandoned partway through, then a
`root_scale=3000` (~59GB, ~13h projection) attempt was ALSO abandoned partway
through (its tenant was deleted) once the deadline made even that too slow.
**Settled on `root_scale=500`** (same as the pilot -- fully characterized, no new
unknowns) with the full N-grid kept intact and reps/runtime trimmed.

**Root build (Phase 2) finished successfully at ~06:43 UTC** (`pgbench_init_wall_s`
came in at 4798s/~80min -- about 2.2x the pilot's own ~37min for the identical
scale, real variance, not a bug; `compact_wall_s`=1002s, matching the pilot
closely). **Important gotcha for whoever reads this next**: nothing was watching
for that completion, so the pipeline sat idle for ~11.6 hours (06:43 -> 18:19)
before anyone noticed and launched the next phase. **Always leave a Monitor (or
equivalent) armed across a phase boundary**, or explicitly tell the user you're
stepping away from active monitoring and when you'll check back -- don't just stop
polling and let a finished phase sit idle indefinitely.

`sweep.py` (Phase 3 materialize + Phase 4 measure) was launched at ~18:25 UTC once
this was caught. See "What's running RIGHT NOW" below for the live process; see
"History of this session's runs" further down for the two abandoned root-scale
attempts, kept for the record.

Originally written 2026-09-24 ~22:15 UTC because the session may run out of
tokens before the background run finishes. **If you are a new session picking
this up, read this file first**, then
`experiments/4-branch-shape-scaling-cluster/log.md` (detailed process log) and
`README.md` (outcome-oriented writeup) in that same directory.

## What's running RIGHT NOW (as of this writing)

**Root build (Phase 2) is DONE** -- `data/cluster_state_prod.json` exists:
```
tenant_id:        43babb6642c84af9ad1def295dcfaa84
root_timeline_id: 965145c7408f4582b933e53033732dd9
root_lsn:         2/22289EC8
```

**Phase 3+4 (`sweep.py`: materialize both trees, then both measurement tiers) is
now running**, launched ~18:25 UTC:

```
PID:      3423930 (on the coordinator, NOT on any cluster node)
Command:  python3 sweep.py   (run from experiments/4-branch-shape-scaling-cluster/scripts,
                               NO `PILOT` env var set)
Started:  2026-09-25 ~18:25 UTC
Log:      /tmp/prod_sweep.log
```

**To check on it from a new session:**
```bash
pgrep -af "python3 sweep.py"             # confirm it's still running; note the PID
tail -50 /tmp/prod_sweep.log             # see current progress
wc -l /mydata/jiyu/neon/experiments/4-branch-shape-scaling-cluster/data/raw_prod.jsonl
  # expected total: (7 horizontal N-values + 5 vertical N-values) x REPS(3) storage
  # points + (3 N-values x 2 shapes x REPS(2)) compute points = 36 + 12 = 48 lines
  # when fully done.
```

If the process is gone and `raw_prod.jsonl` has fewer than 48 lines, it crashed or
was killed — check the tail of the log for a traceback. `sweep.py` is resumable
(checks `raw_prod.jsonl` per point, manifests per entity), so just re-launch the
same way (`nohup python3 sweep.py > /tmp/prod_sweep.log 2>&1 &`) and it picks up
where it left off, UNLESS the crash happened mid-materialization with a
partially-written manifest entry -- check `data/manifests/{horizontal,vertical}_prod.json`
for a sane entry count first.

**Once `sweep.py` finishes**: run `python3 analyze.py` (fast), then write up
`README.md`'s `## Results` and `## Takeaways` from `figures/*_prod.png` and
`data/summary_table_prod.md`.

## Projected timeline for the CURRENT (root_scale=500) run

| Phase | Actual / Projected |
|---|---:|
| Root build (data gen + pkey index + compaction) | **DONE**: 4798s (~80min) data gen + 1002s (~16.7min) compaction, ~3.9h wall-clock total including the pkey index step |
| Materialize both trees (64 horizontal + 16 vertical) -- **running now** | ~9min projected |
| Measurement, both tiers, REPS trimmed to 3/2 -- **not started** | ~50min projected |
| **Remaining** | **~1h from ~18:25 UTC** |

Comfortably fits the "finish by tomorrow morning" deadline given at ~02:40 UTC on
2026-09-25, DESPITE the ~11.6h idle gap after root-build completion (see the note
at the top of this file) -- there's enough slack that the gap didn't actually
threaten the deadline, but don't count on that margin existing next time.

## History of this session's runs (for context, not action)

1. **`root_scale=20000`** (~300GB): matched the original experiment plan exactly.
   Projected ~2.74 days. Abandoned before it got far (still in `pgbench -i`) once
   the projection was computed and reviewed with the user.
2. **`root_scale=3000`** (~59GB): the user's first "shrink it" instruction.
   Projected ~13h. Ran for ~5 hours (finished data generation, was mid-way through
   the primary-key index build) before the user reframed the whole experiment as a
   same-day proof-of-concept, at which point even ~13h was too slow. Its tenant
   (`e5e37bf8f9ae4758926392e051b5debb`) was deleted; log preserved at
   `/tmp/prod_setup_root.log` (no "2") for reference only.
3. **`root_scale=500`** (~9.8GB, same as pilot): **current run**, see above.

## What to do when it finishes or fails

**If `cluster_state_prod.json` exists (root build succeeded):** the next steps,
which have NOT been run yet at full scale, are:
```bash
cd /mydata/jiyu/neon/experiments/4-branch-shape-scaling-cluster/scripts
nohup python3 sweep.py > /tmp/prod_sweep.log 2>&1 &   # materializes both trees, then runs both measurement tiers
# at the current root_scale=500, this should take roughly ~9min (materialize) +
# ~50min (measure, both tiers, REPS trimmed to 3/2) -- launch it the same detached
# way (bare `nohup ... &`, NOT wrapped in a foreground tool call with its own
# timeout -- see "Lessons" below for why this matters).
```
Then once `sweep.py` finishes: `python3 analyze.py` (fast, produces
`figures/*_prod.png` and `data/summary_table_prod.md`). Then write up the
`## Results` and `## Takeaways` sections of `README.md` (currently `TBD`) from
that output, following the standing policy in `agent/CLAUDE.md`'s "Experiments"
section (which now also requires `log.md` for every experiment -- already present
here, keep appending to it).

**If it crashed:** read the tail of `/tmp/prod_setup_root.log` for the traceback.
Cross-check against the six bugs already found and fixed this session (all fixed
in code already -- see "Bugs found and fixed" below); if it's a new failure mode,
add it to `log.md` the same way the existing entries are written (what happened,
why, how it was diagnosed and fixed) -- **the user explicitly asked for this
process-log discipline to be a standing habit for every experiment from now on,
recorded in `agent/CLAUDE.md`**.

**Cleanup gotcha if you have to restart the root build**: delete the tenant first
(`curl -X DELETE http://10.10.1.2:1234/v1/tenant/<tenant_id>` from node1, or via
`lib.curl_json(lib.STORCON_NODE, "DELETE", f"{lib.STORCON_BASE}/v1/tenant/{tenant_id}", ...)`)
and delete `data/cluster_state_prod.json` and `data/manifests/*_prod*.json` if any
exist, so you start from a clean slate rather than a partially-built one.

## Where everything is

- **Experiment directory**: `experiments/4-branch-shape-scaling-cluster/`
  - `README.md` — outcome-oriented writeup. Has the full Phase 0 findings,
    all six bugs, and the validated timeline projection. `## Results` and
    `## Takeaways` are `TBD` pending the full-scale run.
  - `log.md` — detailed chronological process log (what was done, why, in order).
    **Keep appending to this as work continues** — the user asked for this to be
    standing policy (see `agent/CLAUDE.md`'s Experiments section update).
  - `config/` — `pageserver.toml.tmpl`, `compute_hook_stub.py` (recovered from
    experiment 2's still-live cluster 1 instance), `compute_config_template.json`
    (copied from experiment 2, schema-compatible).
  - `scripts/` — `lib.py` (shared helpers), `params.py` (pilot/prod switch via
    `PILOT` env var — **`ROOT_SCALE` was just reduced from 20000 to 3000** per the
    user's explicit choice to shrink the timeline from ~3 days to ~13h),
    `deploy.py` (Phase 1, idempotent, already run successfully), `setup_root.py`
    (Phase 2, **currently running**), `materialize.py` (Phase 3), `measure.py` +
    `sweep.py` (Phase 4), `analyze.py` (Phase 5).
  - `data/cluster_state_pilot.json`, `data/manifests/*_pilot.json`,
    `data/raw_pilot.jsonl` — pilot-scale data, already complete and validated.
    A full pilot sweep was also run in the background purely to produce example
    figures (`figures/*_pilot.png`) — check if it finished
    (`wc -l data/raw_pilot.jsonl`, expect 78 lines when done) and run
    `PILOT=1 python3 analyze.py` if you want those cosmetic figures, but this is
    NOT load-bearing for anything.

## Cluster state (CloudLab cluster 2, 8x c220g5, Wisconsin)

All 8 nodes deployed and running (Phase 1 complete, verified idempotent):

| Node | Host | Role |
|---|---|---|
| node0 | c220g5-110915 | pageserver |
| node1 | c220g5-110923 | storage_broker + storage_controller + its own Postgres + safekeeper + compute-hook stub |
| node2 | c220g5-110924 | root's persistent compute + horizontal-divergence writer + Tier COMPUTE (A) |
| node3 | c220g5-110909 | horizontal-divergence writer + Tier COMPUTE (B) |
| node4-7 | .../920,916,913,908 | pagebench load generators |

SSH: `ssh -i ~/.ssh/dassl_rsa JiyuHu23@<host>.wisc.cloudlab.us`. Full details,
IPs, and every gotcha in `lib.py`'s `NODES` dict and docstrings.

**Two tenants currently exist on the cluster and are harmless to leave**: the
pilot's tenant (`e6d6e4857b6543e8a950d44dc41e8e87`, complete, ~12GB) and the
current prod run's tenant (fresh ID each run -- check the top of
`/tmp/prod_setup_root2.log`). The earlier abandoned `root_scale=3000` tenant
(`e5e37bf8f9ae4758926392e051b5debb`) was already deleted.

## Bugs found and fixed this session (all already fixed in code; see `log.md` for full diagnosis of each)

1. Safekeeper's `--remote-storage={local_path="..."}` TOML literal lost its
   quotes through nested shell parsing in `ssh_background` — fixed with `\"`
   escaping.
2. Pageserver doesn't self-register with storage_controller — added
   `lib.register_pageserver_node`, called from `deploy.py`.
3. `PUT .../compact` needs explicit `force_l0_compaction`/`force_repartition`/
   `force_image_layer_creation` query params or it never creates image layers —
   added to `lib.PSHttp.compact` and used in `setup_root.compact_to_images`.
4. `root_lsn` must be read from the timeline's `last_record_lsn` **after**
   `do_gc`, not from `wait_for_last_flush_lsn` before it (GC cutoff moves) — fixed
   in `setup_root.py`.
5. `pgbench -i -I dtGvp`'s primary-key step is not idempotent (tries to rebuild
   all four standard tables' PKs unconditionally, fails immediately if any
   already exist from an interrupted earlier run) — fixed by building
   `pgbench_accounts`' index directly via idempotent SQL
   (`CREATE UNIQUE INDEX IF NOT EXISTS ...`) in `lib.pgbench_init`, bypassing
   `-I p` entirely. Also added `lib.verify_pgbench_pkey_exists`, called
   automatically after every `pgbench_init`.
6. Two bugs in `measure.py`'s `measure_storage_point`: (a) `collect` was called
   with whole branch dicts instead of their `idx` (`TypeError: unhashable type:
   'dict'`); (b) background pagebench processes were killed 2 seconds *before*
   their own `--runtime` deadline, so they never printed their JSON result
   (silently looked like "no traffic" even though real GetPage activity was
   happening — confirmed via the pageserver's own metrics). Both fixed.

Also worth knowing: `endpoint_stop`'s pkill patterns needed trailing-anchor fixes
(substring collision risk between e.g. `ct-1` and `ct-10`..`ct-19`) — already
fixed in `lib.py`.

## Lessons for whoever continues this (process discipline, not experiment content)

- **Never launch a long-running remote command as a plain foreground tool call**
  with a timeout shorter than the command's real duration. This session hit that
  repeatedly (`pgbench -i` outliving a 590s tool timeout) — the *remote* process
  survives and keeps running (SSH without a TTY doesn't propagate the kill), but
  the *local* Python wrapper dies mid-flight, orphaning its own bookkeeping
  (nothing checks the remote process's final exit code, state files don't get
  written, etc.). Always launch multi-minute-or-longer operations with a bare
  `nohup ... &` from a plain Bash call (not `run_in_background: true` wrapping a
  script that ALSO backgrounds itself internally — that just changes which layer
  the indirection happens at) and poll for completion via a sentinel file or the
  process's own PID, not by waiting on the tool call itself.
- **Never kill a `CREATE INDEX` (or any single-statement DDL) thinking it's
  "stuck"** — killing the backend rolls back all its progress. If a pageserver
  node's load average is low and `pg_stat_activity` shows a real `wait_event`
  (not a lock wait), it's making progress even if slower than expected; let it
  finish.
- Self-match is a recurring `pgrep`/`pkill` footgun: `pgrep -f "python3 foo.py"`
  run from inside a monitor script whose own command line contains that exact
  text will match itself forever. Check for a sentinel file's existence instead
  of process absence when in doubt.
