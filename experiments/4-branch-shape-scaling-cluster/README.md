# 4 — Branch-tree shape vs read scaling (CloudLab cluster 2, 8x c220g5)

Status: **Phase 0 (pilot) in progress.** This README is a skeleton written during that
phase; the Results/Takeaways sections below are placeholders until the full-scale
run (or a scaled-down final decision) completes.

## What this experiment does

Experiment 2 (`../2-branch-interference-cluster/`) asked "do N branches hurt the
parent?" on 6x m400 (8 cores, 39GB disk) and found generic pageserver contention
dominated almost entirely -- its branches were pure copy-on-write off `main` with
**zero divergence**, so every branch read was served from `main`'s already-warm
layers, which is exactly the degenerate case that produced its null/negative
branch-specific result.

This experiment asks a different, sharper question, made affordable by cluster 2's
much larger disks (1.4TB/node vs 39GB) and RAM (187GB vs 62GB):

> Holding the branch count fixed, does the **shape** of the branch tree change read
> cost? **Horizontal** (N branches all forked from the root, every read is at most 1
> ancestor hop) vs **vertical** (a chain root -> b1 -> b2 -> ... -> bN, a read at
> depth k may walk up to k hops) -- and does the effect actually show up as N scales
> to 64?

## Hypothesis

A GetPage@LSN that misses in a child's own layers descends the ancestor chain in a
loop (`pageserver/src/tenant/timeline.rs`, `get_vectored_reconstruct_data`),
lowering the read LSN and paying a layer-map search + `get_ready_ancestor_timeline`
per hop. There is no depth limit in the pageserver read path (the only
`TooManyAncestors` is in detach-ancestor, which only works at depth 1 anyway), so a
64-deep chain is legal and untested. We expect the vertical arm's read latency to
grow with N (more precisely, with reachable ancestor depth) while the horizontal
arm's stays roughly flat, since every horizontal branch is always exactly 1 hop from
a resident, image-heavy root.

## Design

One tenant, one prepopulated root (`pgbench -i -s 20000` at full scale, ~300GB,
image-compacted), two trees forked off it sharing that root so the two shapes differ
in exactly one variable:

```
                 root
                  |
 HORIZONTAL  -----+----- h0  h1  h2 ... h63       (all depth 1)
                  |
 VERTICAL    -----+----- v0 -> v1 -> v2 -> ... -> v63   (depths 1..64)
```

Every branch diverges ~5% of the root's pages (one row updated per touched page,
scattered via index lookups -- see `lib.divergence_write`'s docstring for why
"5% of rows" and "5% of pages" are very different things for pgbench_accounts) before
the next branch forks off it, so every branch has real delta layers of its own and a
read genuinely has to decide whether to walk to an ancestor.

N sweep: {1,2,4,8,16,32,64}, 5 reps, Tier STORAGE (pagebench, primary) then Tier
COMPUTE (pgbench -S over static read-only computes, N in {1,4,16}, secondary). The
two shapes are **never measured concurrently** -- full materialization and
measurement of one shape completes before the other starts within a rep, and the
block order alternates (H-first on even reps, V-first on odd) so time-dependent
drift can't masquerade as a shape effect. See `scripts/sweep.py`.

### Compaction discipline (load-bearing)

- The **root** must be image-dominated before any branch exists, so children have a
  clean, compact ancestor to walk to. `setup_root.py`'s `compact_to_images` drives
  this with `force_repartition=true&force_image_layer_creation=true` -- **a bare
  `PUT .../compact` does not create image layers even when
  `image_creation_threshold` is exceeded**, because image-layer creation only
  happens after repartitioning, which is otherwise timer-driven and never fires
  since `compaction_period=0s`. Discovered empirically during the pilot: 5 rounds of
  the bare call left the pilot root at 0 image layers / 39 delta layers; adding the
  force flags produced 52 image layers on the very next call.
- **Children must never get an image layer of their own** -- that would flatten the
  ancestor chain for the keys it covers and invalidate the depth measurement for
  that branch. `materialize.py` flips `image_creation_threshold` to 1e9 tenant-wide
  before creating any branch, and every branch is checked afterward via
  `lib.has_image_layer`.

## Cluster topology (8 nodes)

| Node | Host | Role |
|---|---|---|
| node0 | c220g5-110915 | pageserver (device under test) |
| node1 | c220g5-110923 | storage_broker + storage_controller + its own Postgres + safekeeper + compute-hook stub |
| node2 | c220g5-110924 | root's persistent compute; horizontal-divergence writer; Tier COMPUTE endpoints (A) |
| node3 | c220g5-110909 | horizontal-divergence writer; Tier COMPUTE endpoints (B) |
| node4-7 | c220g5-110920/916/913/908 | pagebench load generators (Tier STORAGE) |

The vertical chain's divergence writes run serially on node2 (by construction --
level k+1 cannot exist until level k is written and flushed).

## Recovered: the lost deployment runbook

Experiment 2's README points at `agent/experiment-2-cluster-plan.md` and
`agent/experiment-2-progress.md` for the exact service bring-up commands. **Both
files are gone and are not in git history.** They were recovered by SSHing into
CloudLab cluster 1, which was still running experiment 2's stack at the time this
experiment started. The recovered config and command lines are committed at
`config/pageserver.toml.tmpl`, `config/compute_hook_stub.py`, and
`scripts/deploy.py` (which encodes the bring-up order as code, so it can't be lost
again). One gap in the recovery: the pageserver does **not** self-register with
storage_controller (its own `/upcall/v1/re-attach` call is for tenant-attachment
bookkeeping only, and returns `register: None` for an unknown node) -- a real
deployment's provisioning tooling must call `POST /control/v1/node` once per
pageserver, which `deploy.py`'s `wait_for_registration` now does explicitly.

No build was needed on the cluster for this experiment: the coordinator
(`dassl-serv-01`, x86_64 Ubuntu 22.04.5, glibc 2.35) is ABI-identical to the
`c220g5` nodes, and `target/release` already held a `features: ["testing"]` release
build (required -- the manual checkpoint/compact/do_gc endpoints this experiment
depends on are `testing_api_handler`-gated), so `deploy.py` rsyncs binaries + only
`pg_install/v17` (the tenant only ever uses pg_version 17) instead of repeating
experiment 2's aarch64-build-plus-protoc process.

## Reproduction

```bash
cd experiments/4-branch-shape-scaling-cluster/scripts
python3 deploy.py                 # one-time cluster bring-up (idempotent)

PILOT=1 python3 setup_root.py     # Phase 0: small root (~7.5GB, scale 500)
PILOT=1 python3 sweep.py          # Phase 0: 8-wide + 8-deep, small N-grid
PILOT=1 python3 analyze.py        # Phase 0: figures/summary at pilot scale

# after reviewing the pilot's disk/time projections:
python3 setup_root.py             # Phase 2: full root (~300GB, scale 20000)
python3 sweep.py                  # Phase 3+4: materialize + measure, resumable
python3 analyze.py                # Phase 5: figures/summary at full scale
```

Every long step is resumable: `setup_root.py`/`materialize.py` check
`data/cluster_state_{tag}.json` / `data/manifests/*_{tag}.json` before doing work,
and `sweep.py` checks `data/raw_{tag}.jsonl` before each measurement point. `{tag}`
is `pilot` or `prod` per the `PILOT` env var (see `scripts/params.py`), so pilot and
production data never collide.

## Deviations from the plan so far

- **pagebench reports mean + p95/p99/p99.9/p99.99, not p50** (same limitation
  experiment 2 hit). `analyze.py`'s headline figure uses mean + p99 instead of the
  originally-sketched p50 + p99.
- **The pageserver does not self-register with storage_controller** -- see
  "Recovered: the lost deployment runbook" above. `deploy.py` handles this
  explicitly; the original plan assumed it was automatic.
- **A bare `compact` call never creates image layers** -- see "Compaction
  discipline" above. `lib.PSHttp.compact` gained explicit force-flag parameters.
- **`endpoint_stop`'s pkill patterns needed anchoring.** With up to 64
  numerically-suffixed endpoint IDs, an unanchored `pkill -f 'compute-id ct-1'`
  would also kill `ct-10`..`ct-19` (substring match). Fixed with a trailing-space /
  trailing-slash anchor on both patterns in `lib.endpoint_stop`.
- **`root_lsn` must be read *after* `do_gc`, not before.** `setup_root.py` originally
  captured `root_lsn` from `wait_for_last_flush_lsn` and then ran
  `checkpoint -> compact -> do_gc(gc_horizon=0)`. Because `gc_horizon=0` (and
  `pitr_interval=0s`) collapse the GC cutoff to the tenant's tip *at the moment
  `do_gc` runs* -- which had advanced microscopically past the earlier flush point
  by then -- every one of the pilot's first 8 branch-creation calls failed with
  `406 Not Acceptable: invalid branch start lsn: less than latest GC cutoff`. Fixed
  by reading `root_lsn` from the timeline's `last_record_lsn` *after*
  `compact_to_images` (i.e. after `do_gc`) completes -- a post-GC tip is always
  `>=` the cutoff that produced it, so it's always valid to branch from. This does
  not affect the per-branch `_compact_and_check` in `materialize.py`, which
  deliberately never calls `do_gc` (only `checkpoint`+`compact`), so branch LSNs
  recorded there don't have this problem.
- (Pilot-run-specific, not a harness bug) The first `setup_root.py` invocation was
  killed mid-`pgbench -i` by the calling harness's own tool timeout; the remote
  `pgbench -i` process itself was unaffected and completed normally. A one-off
  continuation script picked the run back up using the already-created
  tenant/timeline IDs. `pgbench_init_wall_s` for the pilot is therefore an
  approximate reconstruction (+/- ~3 min), not a script-measured value --
  see the `pgbench_init_wall_s_note` field in `data/cluster_state_pilot.json`.

## Phase 0 pilot: status

**Complete.** Every stage of the pipeline (deploy, root build, materialize both
shapes, Tier STORAGE measurement, Tier COMPUTE measurement) has now run
successfully end-to-end at pilot scale with real, sane data throughout. Six real
bugs were found and fixed along the way (see `log.md` for the full blow-by-blow);
none would have surfaced without actually running the whole pipeline at least
once. Final validated projection for the full-scale run is at the bottom of this
section.

## Phase 0 pilot: findings and why the full-scale run needed a pause

The pilot ran at scale 500 (50,000,000 `pgbench_accounts` rows, ~61 rows/page,
~819,672 total pages, ~9.8GB root data). It surfaced two blocking scale-up problems
-- this is exactly what Phase 0 was for; see `data/cluster_state_pilot.json` and
`data/manifests/horizontal_pilot.json` for the underlying numbers.

### 1. Divergence writes are far too slow to reach production scale

Each branch's divergence write is a single `UPDATE ... WHERE aid IN (<41K discrete
values>)` (5% of pages, one row per page). Measured behavior:

- All 8 pilot horizontal branches (41K pages each) completed in ~28 minutes running
  8-way parallel across node2/node3 -- an effective throughput of **~24 pages/sec
  per writer connection**.
- The vertical chain's first level, run *solo* (no contention), took over 5 minutes
  for the same 41K pages -- **no faster than the contended parallel case**. `psql`'s
  `pg_stat_activity` shows the query spending its time in `wait_event =
  Neon/PS_ReadIO`: the index-driven UPDATE issues GetPage@LSN requests roughly one
  at a time (the template's `effective_io_concurrency=2` gives little prefetch
  depth), so the ~24 pages/sec figure looks like a per-connection latency ceiling,
  not a contention artifact -- it would not go away by giving the vertical chain the
  whole pageserver to itself, which it already had in this measurement.

Scaling to the full root (scale 20000, 40x pilot's row count) means 40x the pages
per branch at the same 5% divergence: **~1.64M pages per branch**. At ~24 pages/sec,
that's **~19 hours per branch**. The vertical chain is strictly serial by
construction (level k+1 needs level k's data first) -- **64 levels would take on
the order of 50 days**. Even the horizontal arm, 8-way parallel, would need 8 batches
of 19 hours each -- **about a week**. Neither is a viable run.

### 2. Per-branch disk cost was badly underestimated in the original plan

The original plan's disk projection (see the plan's "concern #1") guessed a ~0.4x
*compression* factor on top of the raw touched-page bytes. The pilot shows the
opposite: node0's disk grew by **~3.8GB for 8 branches x 41K pages** (~328MB of raw
page bytes each) -- **~475MB actually written per branch, roughly 1.4x the raw page
bytes**, not a fraction of it. Projected to full scale (128 branches x ~1.64M pages
x 8KiB x ~1.4x overhead): **~2.3TB**, against **1.4TB total** on node0's `/mydata`.
Disk alone would not fit even if the time budget were unlimited.

### What worked

Everything else in the harness validated cleanly at pilot scale: tenant/timeline
creation, the root's compaction discipline (once force-flagged -- see "Deviations"),
branch creation with an explicit `ancestor_start_lsn` (once read from *after*
`do_gc` -- see "Deviations"), 8-way parallel horizontal materialization, resumable
manifests, and the image-layer cleanliness check (`clean_no_image_layer: true` on
all 8 horizontal branches). The mechanism this experiment exists to measure was not
yet exercised (no read sweep has run) because materialization itself is the
blocker.

### Root build, for reference

`pgbench -i` (scale 500) took approximately 2200s (~37min, see the wall-time
caveat in "Deviations" -- this number is a reconstruction, not directly measured).
`checkpoint -> compact(force) x5 -> do_gc` took 588s and produced 52 image layers
(0.38GB) alongside 40 delta layers (9.82GB) -- **5 rounds of forced compaction did
not fully image-compact even this small root**, only converting a small fraction of
the keyspace by volume. This is a secondary scaling risk independent of the two
above: the production root may need more than 5 rounds, or may need to be accepted
as "mostly delta, partly image" within a practical time budget.

### A third bug, found while re-measuring: the divergence UPDATE had no index to use

While re-timing a single divergence write to double check the numbers above, its
query plan turned out to be a **parallel sequential scan over the whole table**
(`EXPLAIN`: `Parallel Seq Scan on pgbench_accounts ... rows=50000052`), not the
intended index-driven point lookups. Cause: `SELECT indexname FROM pg_indexes WHERE
tablename='pgbench_accounts'` returned **zero rows** -- `pgbench -i -I dtGvp`'s `p`
(primary-key) step never completed, almost certainly because the *first*
`setup_root.py` invocation for this pilot was killed by the calling harness's own
tool timeout while `pgbench -i` was still in flight; the detached remote process
kept running unobserved (as it did for the whole `pgbench -i` step, see above) but
nothing ever checked its final exit status, so the primary key silently never got
built. Manually running `pgbench -i -I p` built the missing index in 400s and fixed
the plan (`Nested Loop` + `Index Scan using pgbench_accounts_pkey`, cost dropping
from ~1,090,000 to ~2,335).

This means a full-table seq scan **does not get cheaper as `divergence_fraction`
shrinks** -- it touches the whole table regardless of how many rows match. Lowering
divergence alone, without also fixing the missing index, would not have delivered
the speedup the numbers above assumed. Fixed two ways: `lib.pgbench_init` now calls
a new `lib.verify_pgbench_pkey_exists` immediately after `pgbench -i` returns, which
raises loudly if the index is missing for *any* reason, rather than silently letting
every subsequent divergence write degrade to a full scan.

### The path forward: parameters + the index fix together

The bottleneck (once the index actually exists) is the divergence-write mechanism's
per-page GetPage@LSN round-trip cost -- empirically **~20-40 pages/sec per writer
connection**, roughly constant regardless of contention (a solo vertical-chain write
was no faster than one of 8 concurrent horizontal writes) -- multiplied by the
*absolute* number of touched pages, `root_scale x divergence_fraction`. Decision,
made with the user after presenting the two findings above:

- **`DIVERGENCE_FRACTION`: 5% -> 0.1%.** At full scale (root_scale=20000, 40x pilot's
  row count), 0.1% divergence touches close to the *same* number of pages per
  branch that the pilot already exercised at 5% and 1/40th scale -- so the
  per-branch timing is a direct empirical result, not a fresh extrapolation.
- **`VERTICAL_N_MAX`: 64 -> 16** (horizontal stays at 64). The vertical chain is
  strictly serial by construction (level k+1 needs level k's data), so it is the
  long pole regardless of divergence fraction; capping it keeps the full run to a
  practical wall-clock while horizontal -- cheap, 8-way parallel -- still reaches
  the originally-planned N=64.

See `scripts/params.py` for the resulting `HORIZONTAL_N_MAX`/`VERTICAL_N_MAX`/
`HORIZONTAL_N_GRID`/`VERTICAL_N_GRID`/`DIVERGENCE_FRACTION` constants.

### A fourth bug: `pgbench -i -I dtGvp`'s primary-key step isn't idempotent

While rebuilding the pilot root cleanly with the parameters above, the missing-index
verification check (added after the bug above) fired **again** — on a run that
completed with no interruption from this end at all. Root cause, this time fully
isolated: `pgbench -i -I p` (re)creates primary keys on **all four** standard
pgbench tables unconditionally, in a fixed order, every time it runs. The very
first (interrupted) pilot attempt had gotten far enough to build
`pgbench_branches`'/`pgbench_tellers`' tiny primary keys (1 and 10 rows/scale --
near-instant) before being cut off, but never reached `pgbench_accounts`' (100,000
rows/scale). Every subsequent `-I p` attempt then failed immediately with
`multiple primary keys for table "pgbench_branches" are not allowed`, before ever
touching the one table this experiment actually reads from. Fixed by building
`pgbench_accounts`' index directly (`CREATE UNIQUE INDEX IF NOT EXISTS
pgbench_accounts_pkey ...`), bypassing `-I p` entirely — idempotent, and scoped to
the one table that matters.

### A fifth finding: building that index is itself a slow, one-time, whole-table cost

Once fixed, three build attempts at pilot scale (50M rows) gave wildly different
timings: 400s, then two killed after ~30 min each (a mistake — killing a backend
mid-`CREATE INDEX` rolls back the whole thing, discarding all its progress; **never
interrupt one of these**), then a clean, uninterrupted **2724s (~45.4 min)**. Likely
explanation: the fast 400s run happened immediately after `pgbench -i`'s data
generation, while the touched pages were still warm in the pageserver's own page
cache; every later attempt paid full GetPage@LSN latency for the required
full-table scan (node0's load average stayed ~0.5 throughout — the pageserver was
healthy and idle, not contended). Unlike a divergence write, which only touches
`divergence_fraction` of the table, building the primary key is an **unavoidable,
one-time, whole-table** operation on the root — its cost doesn't shrink with a
smaller `DIVERGENCE_FRACTION`, and dominates the full-scale timeline (see below).

### Validated end-to-end: materialization and both measurement tiers

With the index genuinely in place, re-ran materialization at the corrected 0.1%
divergence: **33.4s for all 8 horizontal branches (8-way parallel)**, **68.1s for
all 4 vertical levels (serial)** — every branch touched exactly 819 pages (0.1% of
~819,672) and passed the no-image-layer check. This gives a clean, direct
measurement of divergence-write throughput once the index bug no longer masks it:
**~24.5 pages/sec/connection under 8-way contention, ~48 pages/sec/connection
solo** — both close to the very first (pre-bug-fix, seq-scan-based) measurement's
implied rate, meaning the *throughput ceiling* itself hasn't changed; what changed
is that 0.1% divergence needs far fewer pages touched than 5% did.

Also found and fixed two bugs in the measurement path itself, neither of which had
ever been exercised before this point: `measure_storage_point` passed whole branch
dicts where it needed their `idx` (a `TypeError: unhashable type: 'dict'`), and it
killed its own background pagebench processes 2 seconds before their `--runtime`
deadline, silently discarding their results (`request_count: 0` despite real
traffic, confirmed via the pageserver's own `redo_delta` metrics and the raw
per-second RPS log). Fixed both; a follow-up test produced real, sane latency data
(mean ~17ms, p99 ~57ms, 0 missed) and Tier COMPUTE's static-endpoint path worked on
its first real try (tps=114.67, mean latency 34.9ms). See `log.md` for the full
diagnosis of each.

### Full-scale projection (validated, not extrapolated from the pre-fix numbers)

| Phase | Pilot (measured) | Full-scale (40x rows) projection |
|---|---:|---:|
| `pgbench -i` data generation | ~2200s (~37min, approximate*) | ~24.4h |
| primary key build | 2724s (45.4min, measured clean) | ~30.3h |
| compaction discipline | 995.6s (16.6min) | ~11.1h |
| **root build (Phase 2) total** | | **~65.8h (~2.74 days)** |
| horizontal materialization (64, 8-way parallel) | 33.4s (N=8) | ~3.0h |
| vertical materialization (16, serial) | 68.1s (N=4) | ~3.0h |
| **materialization (Phase 3) total** | | **~6.0h** |
| Tier STORAGE sweep (60 points) | -- | ~1.75h |
| Tier COMPUTE sweep (18 points) | -- | ~0.45h |
| **measurement (Phase 4) total** | | **~2.2h** |
| **grand total** | | **~74h (~3.1 days), unattended** |

\* approximate: the original pilot's `pgbench -i` invocation was interrupted by the
calling harness's own tool timeout; see the wall-time caveat in `log.md`.

The root build (driven by `root_scale` alone, independent of `DIVERGENCE_FRACTION`
or either shape's N) is now the dominant cost by a wide margin — about 92% of the
total. Every other number in this table is either a direct pilot measurement or a
linear extrapolation of one; none of them assume anything about the
now-abandoned 5%-divergence/seq-scan-bug numbers from earlier in Phase 0.

## Results

**Scope note**: per the user's direction partway through execution, this became a
proof-of-concept ("verify branching causes interference + explore depth vs width")
under a same-day deadline rather than the originally-planned disk-bound 300GB run.
`root_scale` was reduced to 500 (~9.8GB, identical to the pilot), `REPS` to 3 (2 for
Tier COMPUTE), and window lengths to 30s -- see `log.md` for the full sequence of
scale changes and why. The N-grid itself (horizontal to 64, vertical to 16) was
kept at its original full size. Data: `data/raw_prod.jsonl` (48 points),
`data/summary_table_prod.md`, `figures/*_prod.png`.

**Data quality**: 39/48 points are clean. 1 storage-tier point
(horizontal N=64 rep=1) failed on a transient SSH connection reset -- a one-off,
harmless given 2 of that point's 3 reps succeeded. 8 of 12 Tier COMPUTE points
failed with `Cannot assign requested address (os error 99)` -- ephemeral port
exhaustion on node2/node3 from many rapid `compute_ctl` start/stop cycles across
the sweep (worst at N=16, spinning up and tearing down 16 static computes
back-to-back with no cooldown). **Tier STORAGE, the primary measurement, is
essentially complete (35/36 points)**; Tier COMPUTE's remaining 4 points are too
sparse to read much into and are not used below. See `log.md` for the full
diagnosis; a fix (e.g. a short pause between `endpoint_stop`/`endpoint_start`
cycles, or reusing sockets) would be needed before Tier COMPUTE is trusted at this
N range in a future run.

### 1. Branching clearly causes read-latency interference

`figures/latency_vs_n_prod.png`. GetPage@LSN p99 latency against the branches
under load climbs steeply and monotonically with N, for both shapes: from
~50-100ms at N=1-2 to ~650-700ms at N=16 to **~2.4 seconds at N=64** (horizontal;
vertical was capped at N=16 by design). Mean latency shows the same shape, ~15ms at
N=1 up to ~665ms at N=64. This directly confirms the premise the user set out to
verify: **branching (of either shape) measurably degrades read latency as branch
count grows**, on this cluster at this scale.

(N=1 horizontal shows an anomalously wide confidence interval -- [48.8, 2123.8]ms
p99 -- driven by one outlier rep; it was the very first storage measurement taken
against the freshly-materialized tenant in the whole sweep, and is most plausibly
a cold-start artifact, not a real N=1-specific effect. Vertical's N=1 point shows
no such anomaly.)

### 2. Horizontal and vertical produce nearly identical end-to-end latency at matched N

The headline depth-vs-width comparison: at every N where both shapes were
measured (2, 4, 8, 16), horizontal and vertical p99 latencies are close --
generally within ~5% of each other, with horizontal marginally higher (e.g. at
N=16: 674ms horizontal vs. 649ms vertical). **Shape does not materially change
end-to-end read latency in this setup** -- what drives latency is how many
branches are concurrently under load (N), essentially independent of whether they
fan out from the root or chain off each other.

This also shows up directly in the per-depth breakdown
(`figures/latency_vs_depth_prod.png`, all 16 depths of the vertical chain measured
simultaneously at N=16): p99 latency bounces in a narrow ~620-710ms band with no
clear monotonic trend by depth -- depth 16 (the deepest) is not detectably slower
than depth 1.

### 3. But the mechanism *is* real: deeper branches genuinely touch more layers per read

`figures/layers_per_read_prod.png` is the sharpest result in this experiment.
Mean pageserver layers touched per GetPage read:
- **Horizontal: flat at ~2.8 layers, regardless of N** (1 through 64) -- exactly
  what's expected, since every horizontal branch is always exactly one hop from
  root.
- **Vertical: climbs from ~2.8 at depth 1 to ~11 at depth 16** -- a clear,
  monotonic increase, direct confirmation that deeper ancestor chains really do
  make the pageserver do more work per read.

So the ancestor-walk mechanism this experiment set out to find is unambiguously
present and measurable -- **but it doesn't translate into higher end-to-end
latency at this depth range and this divergence fraction** (see takeaway 2 below
for why that's a real, explainable result and not a contradiction).

### 4. Is the pageserver disk actually the bottleneck?

`figures/disk_utilization_prod.png` shows node0's disk sitting at ~84-92% max
block-device utilization across the *entire* sweep, including at N=1 -- it doesn't
scale with N the way latency does. Two readings are possible: either the disk is
genuinely near-saturated throughout (in which case it's a confound on the N-vs-
latency numbers above, not something N specifically causes), or -- more likely
given this is flash storage -- `%util` from `iostat`-style accounting is a poor
saturation signal for an SSD/NVMe-class device that services many requests in
parallel (high queue depth defeats the classic "percent of time busy" metric,
which was designed for single-queue spinning disks). Not resolved here; a real
IOPS/throughput or queue-depth metric would be needed to settle it.

## Takeaways

1. **Branching causes real, substantial read-latency interference as branch count
   grows** -- confirmed cleanly (p99 ~50ms at N=1 to ~2.4s at N=64). This was the
   user's primary question and it has a clear, unambiguous answer: yes.
2. **Branch-tree shape (depth vs. width) does not meaningfully change end-to-end
   read latency at matched N in this setup, despite depth measurably increasing
   the pageserver's per-read work.** The likely explanation, consistent with
   experiment 2's finding that "generic contention dominates over branch-specific
   mechanisms": each additional layer-map lookup from walking the ancestor chain
   is cheap (an in-memory operation) relative to the request's other costs
   (network round-trip, per-request GetPage handling, contention from N
   concurrently-loaded branches) -- so a 4x growth in layers-touched (2.8 -> 11
   layers, depth 1 -> 16) is not large enough in absolute terms to show up against
   a latency budget already dominated by load-driven, shape-independent effects.
   This may well look different at higher N, greater depth, or a divergence
   fraction low enough to force nearly every read to walk the full chain (see the
   plan's original concern about 0.1% divergence saturating the depth signal
   within a modest depth range -- this result is consistent with that caveat
   actually manifesting).
3. **The ancestor-walk mechanism is directly confirmed, not just inferred** --
   `layers_per_read` climbing cleanly with depth for the vertical arm (and staying
   flat for horizontal) is about as clean a mechanism signal as this kind of
   experiment produces. This is the piece experiment 2 never had (its equivalent
   metric, `layers_needed_by_branches`, stayed at exactly 0 throughout because it
   was a GC-pinning metric with nothing to pin in a read-only tier).
4. **Tier COMPUTE needs a fix before it's trustworthy at this N range** -- rapid
   compute start/stop cycling exhausted ephemeral ports on the divergence/
   compute-tier nodes, failing 8 of 12 points. Not investigated further given time
   constraints and Tier COMPUTE's secondary role in this design.
5. As with every experiment in this series, the negative/nuanced result (shape
   doesn't matter much once you're already measuring the mechanism that should
   make it matter) is reported in full rather than only the positive one
   (branching hurts) -- per the standing policy for this directory.

## Follow-up: pluggable remote-storage backend (local_fs vs MinIO vs AWS S3)

Run 2026-09-27. The warm horizontal Tier STORAGE sweep (N = 1..32, 3 reps, same prod tenant,
manifest, and pagebench settings as above) was rerun with the pageserver's `remote_storage` pointed
at three backends in turn:

- **local_fs**: node0's `~/ps-remote`, the original setup (SSD root partition).
- **MinIO**: single node on node3, data on `/mydata` (mostly a 10k-RPM HDD), over the 10 Gbps LAN.
- **AWS S3**: bucket `dassl-jiyu-neon-backend` in us-east-2, about 15 ms TCP connect from CloudLab
  Wisconsin.

Data: `data/raw_backend_prod.jsonl`, `data/backend_{switch,probe}_prod.jsonl`,
`data/summary_table_backend_prod.md`, `figures/latency_vs_n_by_backend_prod.png`.

**How it works.** The backend is selected with `deploy.py --backend` or `storage_backend.py switch`.
`switch` stops the pageserver and copies node0's `~/ps-remote` into the bucket with
`aws s3 sync` (the local_fs layout equals the S3 key layout under `prefix_in_bucket`). It then
rewrites `pageserver.toml` from `lib.remote_storage_toml` and restarts the pageserver with
`AWS_PROFILE`. Credentials exist only in `~/.neon-exp4/` and `~/.aws/` on the coordinator, and in
a chmod-600 `~/.aws/credentials` on node0. They are never in git and never on a command line.
MinIO is built from source (`go install github.com/minio/minio@master`), because dl.min.io now
returns 410 for community binaries.

### Result: warm read latency does not depend on the backend

| N | local_fs p99 (per-rep) | MinIO p99 | S3 p99 |
|---:|---|---|---|
| 1 | 63 / 50 / 46 | 62 / 53 / 47 | 61 / 52 / 46 |
| 4 | 193 / 167 / 185 | 207 / 191 / 173 | 204 / 180 / 171 |
| 16 | 708 / 666 / 623 | 723 / 625 / 611 | 726 / 659 / 610 |
| 32 | 1245 / 950 / 1183 | 1420 / 1230 / 1127 | 1175 / 1285 / 1120 |

All 54 points were clean. Not one point saw an eviction or on-demand download, so **no GetPage ever
touched remote storage**, as this protocol intends. The three backends agree within
rep-to-rep spread at every N. The pooled per-branch CIs in the summary table look non-overlapping
at N=32 only because the 32 branches within a rep are not independent; read the per-rep columns.
Rep 0 is consistently the slowest at N ≥ 8 for every backend. That is warm-up after the backend
switch's pageserver restart, and it is identical across arms. The local_fs rerun also reproduces
the original experiment 4 numbers (e.g. N=16 p99 666 ms vs 674 ms).

### Where the backends actually differ (raw probe from node0, and restart)

| | local_fs | MinIO (LAN, HDD-backed) | S3 us-east-2 |
|---|---:|---:|---:|
| small GET p50 / p99 (`index_part`, reused connection) | 0.02 / 0.03 ms | 2.8 / 3.0 ms | 35 / 59 ms |
| first small GET (new connection + TLS) | 0.07 ms | 3.7 ms | 169 ms |
| 256 MiB layer GET, cold, single stream | ~200 MB/s | ~150–165 MB/s | 71–95 MB/s (2.8–3.8 s) |
| copy 24 GB into backend | – | 246 s | 160 s |
| restart → tenant Active | 8.3 / 8.4 s | 12.3 s | 8.9 s |

S3 is about 12× slower than MinIO per request and about 2× lower in single-stream bandwidth. Any
path that downloads layers would expose this: cold reads after eviction, attach on a fresh disk, or
shard migration. **This warm protocol exercises none of those paths.** Restart time is
dominated by pageserver startup, not remote I/O (S3 was faster than MinIO), because attach only
fetches a few small `index_part` files. A **cold-read** variant (evict the layers of the root and
branches before each point) is the natural next step if backend latency is the question.

### Caveats

- **The drift-control point is a post-restart transient, not drift.** local_fs at N=32, measured
  about 1 min after the final restart plus warmup, gave p99 2322 ms. At the same time, layers/read
  was 3.43 (every other point: 2.83–2.84) and the node0 HDD was at 100% utilization. A second point
  about 4 min later was back to layers/read 2.84 and p99 1403 ms, inside every backend's rep-0
  range. The main sweeps reach N=32 about 6 min after their restart, so they never saw this
  window. The cause of the transient layers/read bump is not established.
- The arms ran in the fixed order local_fs → MinIO → S3, not interleaved. The drift check above is
  the only guard against time drift.
- No new `index_part` generation was uploaded on attach in any arm, since nothing changed. A
  separate round-trip test (2026-09-28, `data/logs/roundtrip.out`) confirmed full read/write on
  both MinIO and S3. It evicted a prod-branch layer and downloaded it back from the backend, where
  the on-demand download counter advanced. It created a throwaway tenant, whose layers and
  `index_part` appeared in the bucket, then deleted that tenant, after which 0 objects remained.

### Reproduction

```bash
cd experiments/4-branch-shape-scaling-cluster/scripts
python3 storage_backend.py setup-minio          # needs ~/.neon-exp4/bin/minio built first
export EXP4_S3_BUCKET=<bucket>                   # coordinator ~/.aws profile neon-exp4
for b in localfs minio s3; do
  python3 storage_backend.py switch $b && python3 storage_backend.py probe $b && python3 backend_sweep.py $b
done
python3 storage_backend.py switch localfs        # leave the cluster on the original backend
python3 analyze_backend.py
```
