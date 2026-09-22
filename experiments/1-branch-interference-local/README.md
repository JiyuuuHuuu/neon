# 1 — Branch interference on Neon's main branch (local mode)

Status: **complete** (Tier STORAGE, the primary measurement) / **partial, disk-constrained**
(Tier ENDPOINT, supplementary). See "Results" and "Takeaways" below.

## Headline result

Yes, branching degrades `main`'s read latency severely at scale — **but the mechanism is
tenant-scoped resource sharing, not branch ancestry.** At N=64 concurrently-loaded targets,
`main`'s p99 GetPage@LSN latency rose from ~0.6ms to ~21ms (branches) and ~19ms (unrelated
sibling timelines in the same tenant) — statistically indistinguishable — while N=64
**separate tenants** driving the identical aggregate read load left `main` at ~0.6ms,
unchanged from baseline. The branch-specific component (ancestry / `retain_lsn` / shared
layers) accounts for only **~9%** of the effect at N=64; the other **~91%** is attributable
to sharing a tenant with `main` at all (a single serialized WAL-redo process, serial
per-tenant compaction/GC scheduling), regardless of whether the sharing timelines are
branches. At N=256 branches, `main`'s p99 reached **~80ms** (a ~126x increase over
baseline), and the open-loop probe began missing its offered rate entirely (see
`figures/missed_vs_n.png`) — genuine saturation, not measurement noise.

## Hypothesis

In Neon local mode, creating many branches (timelines) off `main` and driving load
against those branches degrades the latency of accesses to `main`, relative to having
no branches.

## Why this is plausible

A branch is *metadata only* on the pageserver (`branch_timeline_impl`,
`pageserver/src/tenant.rs`): no layer data is copied, the child holds an `Arc` to the
parent `Timeline`, reads on the child fall through to the parent's layers, and the
child registers a `retain_lsn` on the parent that pins the parent's garbage
collection. Both are mechanisms by which many children could make `main` slower —
neither exists between two independent tenants, which is why this experiment includes
a control arm rather than just comparing "branches" to "no branches".

## Design

### Four arms — a 3-way decomposition, not a 2-arm comparison

A naive two-arm design (main alone vs. main + N branches) cannot tell branch-specific
interference apart from Neon's tenant-scoped resource sharing (a single WAL-redo
process per tenant, serial per-tenant compaction/GC — see "Findings that changed the
design" below). Four arms fix that:

| Arm | What exists | Isolates |
|---|---|---|
| **A0** | main only (N=0 point of any arm) | baseline |
| **A1** | main + N **branches** of main, same tenant | hypothesis |
| **A1b** | main + N **sibling root timelines**, same tenant, no ancestor | tenant-scoped sharing, no ancestry |
| **A2** | main + N **separate tenants** | machine contention only |

- **A1 − A1b** = the branch-specific effect (ancestry, `retain_lsn`, shared layers) —
  this is what the hypothesis is actually about.
- **A1b − A2** = tenant-scoped sharing (one WAL-redo process per tenant, serial
  per-tenant compaction/GC).
- **A2 − A0** = generic CPU/disk/process contention.

### Two measurement tiers

- **Tier STORAGE** — `pagebench get-page-latest-lsn` drives GetPage@LSN read load
  directly at the pageserver against the N branch/sibling/tenant targets (no Postgres
  needed on the load side once the targets are seeded), while a second, **open-loop**
  pagebench instance (`--per-client-rate`) probes `main` only. Cheap enough to scale
  A1 to N=256; A1b/A2 capped at N=64 (see deviations below).
- **Tier ENDPOINT** — real `neon_local` endpoints on all N targets running pgbench's
  default TPC-B mix (writes — this is what actually drives WAL ingest, WAL redo and
  compaction, the mechanisms Tier STORAGE's read-only load can't exercise), probed
  simultaneously with pagebench (storage-layer number) and `pgbench -S -c1` /
  `pgbench -N -c1` on main's endpoint (end-to-end read and write latency). N ∈
  {0,2,8,32,64}.

### GC/compaction protocol

Tenant config for every timeline: `gc_period=0s, compaction_period=0s,
pitr_interval=0s, gc_horizon=0, checkpoint_timeout=10years`. Background GC/compaction
are off; between every measurement window the driver explicitly calls the pageserver's
`checkpoint` → `compact` → `do_gc` HTTP endpoints on `main`. `do_gc`'s response
(`GcResult`) includes `layers_needed_by_branches` — a **direct measurement** of
branch-induced GC pinning, recorded alongside every latency point rather than only
inferred from it.

### Load scaling

The headline sweep holds **total offered load** roughly constant in spirit by scaling
per-target client count modestly with N rather than holding a fixed per-target count
(which would trivially show "more branches ⇒ more aggregate load ⇒ everything is
slower" in every arm, including the A2 control). The `gc_before` field on every
record, plus the mechanism figure, lets the write-up separate "more total work" from
"the same work landing on `main` differently".

### Per-window health assertions

Every Tier STORAGE point scrapes `/metrics` before and after and flags (does not
discard, but records) the point if `pageserver_evictions_total` or
`pageserver_remote_ondemand_downloaded_layers_total` moved, or if
`pageserver_tenant_throttling_count_accounted_start_global` is nonzero — see
`health_problems` in each JSONL record.

## Findings that changed the design (verified in code before running anything)

1. **Disk-usage eviction is armed by default** (`DiskUsageEvictionTaskConfig::default()`,
   `libs/pageserver_api/src/config.rs:295-307`, `enabled: true` at 80% usage, 60s
   period) and `neon_local` always configures remote storage
   (`control_plane/src/pageserver.rs:128-130`), so it always runs unless disabled.
   Disabled in `config/bench.conf`. Also mitigated by moving `NEON_REPO_DIR` to
   `/lake1/jiyu/neon-bench` (a different physical NVMe from the source tree, 586GB
   free at the time of writing, vs. `/mydata` at 80% used).
2. **`max_file_descriptors` defaults to 100** (`config.rs:672`), a global clock-swept
   slot cache (`pageserver/src/virtual_file.rs:344-380`). N separate tenants touch
   hundreds of distinct layer files against 100 slots and thrash; N branches of one
   tenant re-read *main's* files and don't — this alone would make the A2 control look
   worse for reasons unrelated to branching. Raised to 32768.
3. **Page cache is keyed only by `(file_id, blkno)`**, no tenant/timeline partitioning
   (`pageserver/src/page_cache.rs:130-131`), default 64MiB (`config.rs:671`). Branches
   reading through main's layers hit the *same* cache entries (constructive); separate
   tenants evict each other (destructive) — a hidden variable unless the cache is sized
   well above the working set. Raised to 8GiB.
4. **WAL redo is one process per tenant, fully serialized**: `walredo_mgr` is a field
   of `TenantShard` (`pageserver/src/tenant.rs:330`), shared by every timeline in that
   tenant; `PostgresRedoManager` holds a single process behind a mutex
   (`pageserver/src/walredo.rs:50-57`, `walredo/process.rs:277-286`). N branches share
   one redo process; N separate tenants get N. Real, but *tenant*-scoped, not
   ancestry-scoped — this is exactly what arm A1b isolates.
5. **`retain_lsn` is a no-op under Neon's stock defaults**: `DEFAULT_PITR_INTERVAL =
   "7 days"` (`config.rs:892`) already pins every layer created during a short
   experiment regardless of branches, and `DEFAULT_GC_PERIOD = "1 hr"` (`config.rs:884`)
   means background GC essentially never fires inside a measurement window. The
   hypothesized GC-pinning mechanism only exists with `pitr_interval=0s` and manual GC
   — see the protocol above.
6. **Background compaction/GC loops are per-tenant and serial over timelines**
   (`pageserver/src/tenant/tasks.rs:135-215`; `tenant.rs:3218-3290`, `4657-4677`) —
   another tenant-scoped (not ancestry-scoped) asymmetry, folded into A1b.
7. `timeline_get_throttle` / `pagestream_throttle` default to **disabled**
   (`config.rs:953`) and are per-tenant — asserted at runtime, not just trusted.
8. **pagebench's default probe is closed-loop**, which under-samples slow periods and
   biases p99 *optimistically* (false-negative risk). Probed open-loop via
   `--per-client-rate` instead; overrun count (`MISSED`) is only ever printed to
   stderr (`getpage_latest_lsn.rs:300-306`), never in the JSON output — the driver
   parses it from stderr. Percentiles are hardcoded to p95/p99/p99.9/p99.99
   (`request_stats.rs:54`); **no p50 is available** from this tool.
9. Every `neon_local` CLI invocation takes an exclusive flock on the repo dir
   (`control_plane/src/bin/neon_local.rs:726-760`), serializing all setup. Accepted as
   a wall-clock cost (setup runs unattended) rather than reimplementing the
   pageserver/storage-controller wire protocol to bypass it — see deviations below.
10. `libs/postgres_ffi/build.rs:59` iterates all four supported PG versions, so the
    full v14–v17 Postgres build is unavoidable even though only v17 is used at
    runtime.
11. The pageserver's manual-checkpoint HTTP endpoint is gated behind the `testing`
    Cargo feature (`testing_api_handler`, `pageserver/src/http/routes.rs:3973-3988`)
    — the release build was compiled with `CARGO_BUILD_FLAGS="--features=testing"` so
    the GC protocol above works.

## Deviations from the original plan (and why)

- **N is capped at 64 for A1b and A2** (both tiers), and for A1 in Tier ENDPOINT,
  rather than the originally discussed 256. Branches (A1) are free to create at scale
  — metadata only, data inherited via copy-on-write — but A1b's sibling timelines and
  A2's separate tenants each need an independent SQL-level seed
  (`pgbench -i -s 50`, ~750MB) through a transient endpoint before they hold
  comparable data, and every `neon_local` call in that path serializes on the repo
  flock (finding 9). At N=256 that one-time seeding cost is tens of minutes per arm;
  at N=64 it is a few minutes and was judged a better use of the time budget than a
  larger but shallower sweep. **A1 alone is measured out to N=256 in Tier STORAGE**,
  since it pays none of this cost.
- **Build required a newer `protoc`.** The distro's `protobuf-compiler` (3.12.4)
  predates proto3 `optional` field support (needs 3.15+, per the README's own build
  note); the build failed compiling `storage_broker`'s `.proto` until `protoc 25.3`
  (installed to `~/.local/protoc`, not via apt) was placed ahead of it on `PATH`.
- **Entity creation goes through `neon_local`, not raw pageserver/storage-controller
  HTTP.** Bypassing the flock (finding 9) was part of the original design; given the
  effort budget, using the officially-supported, well-tested CLI path was judged lower
  risk than hand-rolling tenant/timeline creation against the storage controller's
  wire protocol, at the cost of slower (but still bounded and one-time) setup.
- **Tier ENDPOINT ran against a *second*, freshly-built cluster on the root
  filesystem (`/home/jiyu/neon-bench-endpoint`, 787GB, 15% used at the time),
  brought up only after Tier STORAGE's cluster (`/lake1/jiyu/neon-bench`) was fully
  stopped.** `/lake1` filled to 93% used partway through Tier STORAGE materialization
  (see the disk incidents below); moving Tier ENDPOINT's independently-seeded
  entities (which need much more headroom under live write load) to a roomier
  filesystem was safer than trying to run both concurrently on `/lake1`, which was
  never an option anyway since both clusters use the same fixed ports
  (`config/bench.conf`) and so cannot run at the same time regardless of directory.
  Both clusters' results land in the same `data/raw.jsonl`, distinguished by the
  `tier`/`arm`/`n`/`rep` fields, not by which physical disk produced them.
- **Tier ENDPOINT's sweep parameters were cut roughly 6x mid-run** (`ENDPOINT_RUNTIME_S`
  30s→10s, `ENDPOINT_LOAD_CLIENTS` 4→2) after real write load exhausted the root
  filesystem's initial 640GB of headroom far faster than Tier STORAGE's read-only
  load had exhausted `/lake1`'s. A per-point disk-safety floor (40GB) was added and
  is what eventually stopped the tier cleanly, with 11/45 points completed — see
  "Tier ENDPOINT: partial" below.

## Reproducing

```bash
cd /mydata/jiyu/neon/experiments/1-branch-interference-local/scripts

# 0. Cluster is already built with:
#    PATH="$HOME/.local/protoc/bin:$PATH" BUILD_TYPE=release \
#      CARGO_BUILD_FLAGS="--features=testing" make -j$(nproc) -s

# 1. Tier STORAGE: one-time bring-up + full sweep (resumable)
python3 setup_cluster.py
python3 calibrate.py   # optional; prints a client-count recommendation
python3 sweep.py storage

# 2. Tier ENDPOINT: separate cluster on a roomier filesystem (see deviations above).
#    Stop Tier STORAGE's cluster first -- both use the same fixed ports.
NEON_REPO_DIR=/lake1/jiyu/neon-bench ../../../target/release/neon_local stop --mode immediate
python3 setup_cluster.py --repo-dir /home/jiyu/neon-bench-endpoint \
    --state-file ../data/cluster_state_endpoint.json
python3 sweep.py endpoint --repo-dir /home/jiyu/neon-bench-endpoint \
    --state-file ../data/cluster_state_endpoint.json

# 3. Figures + summary table (reads both tiers from the one raw.jsonl)
python3 analyze.py
```

Raw records: `data/raw.jsonl` (one JSON object per measurement point, both tiers).
Materialization manifests (resumable): `data/manifests/*.json`.
Cluster state: `data/cluster_state.json` (Tier STORAGE),
`data/cluster_state_endpoint.json` (Tier ENDPOINT).
Figures: `figures/*.png`. Summary table: `data/summary_table.md`.

## Environment

- Machine: 96-core Intel Xeon Gold 6418H, 251GB RAM.
- Source tree + build artifacts: `/mydata` (nvme2n1).
- Tier STORAGE cluster: `/lake1/jiyu/neon-bench` (nvme1n1, a different physical
  device from the source tree, **shared with other users** — see risks below;
  65% used at experiment start, 93% used by the time Tier STORAGE completed).
- Tier ENDPOINT cluster: `/home/jiyu/neon-bench-endpoint` (root filesystem,
  `/dev/mapper/ubuntu--vg-ubuntu--lv`, 15% used at start of that tier, 96% used —
  36GB free — when the disk-safety floor stopped the tier).
- Build: `BUILD_TYPE=release`, `CARGO_BUILD_FLAGS="--features=testing"` (needed for
  the manual-checkpoint endpoint), `protoc 25.3` (not the distro's 3.12.4).
- Commit: `fa504217c` (neon, `JiyuuuHuuu/neon` fork) + upstream
  `neondatabase/postgres` submodules at the versions pinned in `.gitmodules`.

## Risks / limitations carried into the results

- `/lake1` is shared with other users; their I/O is noise this experiment does not
  control for. The working set (~750MB) fits entirely in the 251GB of RAM after
  warmup, so in practice this mattered more as *disk space* pressure (see below)
  than as I/O noise in the read-only Tier STORAGE measurements.
- **Disk was the dominant operational constraint of this experiment, on both
  filesystems, for the same underlying reason**: independently-seeded entities
  (A1b's sibling timelines, A2's separate tenants) each hold a ~750MB pgbench
  dataset, `local_fs` remote storage duplicates every layer byte, and with
  `gc_period=0s`/`compaction_period=0s` (needed to make GC pinning observable at
  all — see finding 5) nothing is ever reclaimed automatically. Manual
  checkpoint+compact+do_gc was tried as a mid-run remedy and measured **net
  negative** for disk (compaction writes new layers before GC can remove old ones,
  and removal was evidently incomplete, plausibly because `local_fs` remote storage
  retains historical copies past what the live index needs) — so it was dropped in
  favor of cutting write volume and adding a hard safety floor. A production
  deployment with a real object-storage backend and normal GC/compaction periods
  would not face this specific constraint; it is an artifact of this local,
  disk-based, manually-GC'd setup, not of the interference phenomenon itself.
- **All three Tier STORAGE arms' entities are materialized once, up front, and
  coexist for the tier's entire duration** — main's tenant always has all 256 A1
  branches and 64 A1b siblings present, and 64 independent A2 tenants exist
  throughout, regardless of which arm's `N` is currently being probed. This does
  **not** undermine the A1-vs-A1b-vs-A2 comparison (main's own tenant carries the
  identical 256+64 background timelines throughout, whichever arm is under test —
  only which subset is *actively loaded* differs), but it does mean every arm's own
  N-sweep is really "with a constant background of the arm's full entity count
  existing, vary how many are loaded" rather than "vary how many exist" — the
  `existvsload` cell makes this distinction explicit for one case (see Results).
- `neon_local` sets `shared_buffers=1MB` per endpoint with the local file cache off
  (`control_plane/src/endpoint.rs:471,475`) — deliberate, since it's what makes reads
  actually reach the pageserver, but it is not a production compute configuration.
- pagebench has no p50 and a right-censored tail under open-loop overrun (`MISSED`
  requests are not latency-corrected) — p95/p99/p99.9 plus the MISSED count are the
  headline, not a full distribution.
- The `GcResult` mechanism check (`layers_needed_by_branches`) is only informative
  under active writes. Tier STORAGE's load is pure-read by design (finding on
  read-only vs. write-only mechanisms, see Design), so after the first GC call ever
  made, main has nothing new to reconsider and every subsequent `GcResult` in Tier
  STORAGE is all-zeros (confirmed in the data) — it does *not* mean `retain_lsn`
  pinning isn't happening, only that this tier can't observe it. Tier ENDPOINT's
  (sparse) data does show it: `layers_needed_by_branches` is consistently 4-8 for
  A1 there, confirming the mechanism is real once writes occur.

## Results

### Tier STORAGE: complete (86/86 points, 0 health-check violations)

Full table in `data/summary_table.md`. Headline numbers (p99 GetPage@LSN latency on
`main`, mean of 5 reps):

| N | A1 (branches) | A1b (siblings) | A2 (separate tenants) |
|---:|---:|---:|---:|
| 0 | 0.63ms | 0.57ms | 0.65ms |
| 1 | 0.67ms | 0.66ms | 0.64ms |
| 4 | 1.31ms | 1.31ms | 0.57ms |
| 16 | 5.18ms | 5.26ms | 0.61ms |
| 64 | 20.78ms | 18.93ms | 0.57ms |
| 128 | 40.47ms | — | — |
| 256 | 79.53ms | — | — |

**A1 and A1b are statistically indistinguishable at every shared N** (overlapping or
near-overlapping 95% bootstrap CIs throughout — see `data/summary_table.md`), while
**A2 stays flat at its N=0 baseline all the way to N=64**, with the same aggregate
read load the other two arms were serving. `figures/latency_vs_n.png` shows this
directly; `figures/decomposition.png` turns it into the three-way split described in
the headline. `figures/missed_vs_n.png` shows the open-loop probe's overrun count
staying at 0 through N=64 and then exploding at N=128/256 (a mean of ~1150 and ~1990
missed requests respectively, out of an offered ~3000) — the machine was genuinely
saturated at the highest N, not just slower.

**Exist-vs-loaded cell**: with all 256 A1 branches existing, loading only 4 of them
gave a p99 of 1.28ms — statistically the same as the N=4 point on A1's own curve
(1.31ms) where only 4 branches existed at all. Merely having 252 additional, unloaded
branches around cost nothing measurable; the effect tracks *loaded* concurrency, not
branch count.

**Mechanism check**: `layers_needed_by_branches` (the `GcResult` field that would
show branch-induced GC pinning) was 0 throughout Tier STORAGE for all three arms —
expected and uninformative here, since this tier's load is pure-read (see
Limitations). This means the ~91%-of-effect "tenant-scoped" component demonstrated
above is **not** explained by GC pinning in this data; the most likely candidates
given what the design review surfaced are the single serialized WAL-redo process per
tenant (relevant once writes occur, so more pertinent to Tier ENDPOINT) and
tenant-shared read-path resource contention (page cache entries are shared and
unpartitioned by design, `pageserver/src/page_cache.rs:130-131`, and reads across
many same-tenant timelines compete for the pageserver's `VirtualFile` slot cache and
task-scheduling capacity) — this experiment locates the effect as tenant-scoped but
does not fully attribute it to one specific pageserver subsystem; that would need a
follow-up experiment instrumented with the relevant per-subsystem metrics
(`pageserver_io_operations_seconds`, walredo timing, tokio runtime queue depth).

### Tier ENDPOINT: partial (11/45 points, 0 errors, disk-constrained)

Coverage: A1 got 6 points (N=0,2×2,8,64×2), A1b got 2 (N=32×2), A2 got 4
(N=2,8,32×1 each) — too sparse and uneven for the same rigorous per-N comparison as
Tier STORAGE. What the 11 points do show, consistently with Tier STORAGE:

- A1 at N=64 (2 reps): pagebench p99 on main 10.7ms and 29.1ms, pgbench read latency
  18.0ms and 2.1ms, pgbench write latency 19.0ms and 1.5ms — noisy (small N of reps,
  and real write load is inherently noisier than read-only probing) but clearly
  elevated over A1 at N≤8 (pagebench p99 0.9–1.6ms).
- A1b at N=32 (2 reps): pagebench p99 ~5.2ms, pgbench read/write latency ~9–10ms —
  in the same direction and rough magnitude as A1 would be expected to show at a
  comparable N, consistent with the tenant-scoped story, though the grids don't
  overlap at a common N to compare directly.
- A2's points (N=2,8,32) stay under 3.5ms pagebench p99 throughout — again consistent
  with Tier STORAGE's finding that separate tenants don't transmit load to main the
  way same-tenant timelines do.
- **`layers_needed_by_branches` is 4-8 for every A1 point** (vs. 0 or incidental
  small values for A2) — this is the direct confirmation, under real writes, that
  branch-induced GC pinning on main is a real, measurable phenomenon; Tier STORAGE
  could not show this (see Limitations) but Tier ENDPOINT does, even from a sparse
  sample.

This tier is a real-Postgres, real-write sanity check that points the same direction
as Tier STORAGE; it does not have the statistical power to stand on its own, and a
rerun with more disk headroom (see Takeaways) is the natural next step to firm it up.

## Takeaways

1. **The user's hypothesis is confirmed, but the mechanism is not what "branching
   causes interference" naively suggests.** Branches do make main slower — severely,
   at scale — but so do same-tenant sibling timelines with no ancestry at all, to
   within measurement noise. What matters is *sharing a pageserver tenant with
   main and being actively read from or written to*, not the parent/child
   relationship specifically. Separate tenants serving the identical aggregate load
   left main untouched.
2. **The effect is highly nonlinear and only shows up at real scale.** N=16 already
   produces a visible ~8x latency increase; N=64 produces ~33x; N=256 produces
   ~126x and outright saturates the probe. A small-scale test (single digits of
   branches) would have missed this entirely.
3. **Existence is cheap; load is what costs.** 252 unloaded branches sitting next to
   4 loaded ones cost nothing measurable over the 4-loaded-out-of-4-existing case.
4. **GC-pinning (`retain_lsn`) is real but was only observable under write load.**
   The pure-read Tier STORAGE design (deliberately chosen to scale to N=256 cheaply)
   cannot exercise it; Tier ENDPOINT's sparse data confirms
   `layers_needed_by_branches` is consistently nonzero for branches under write
   load. A follow-up should prioritize *finishing* Tier ENDPOINT's grid (with more
   disk headroom and/or a smaller write volume per point from the start) to test
   whether GC-pinning adds a *further*, separately attributable effect on top of
   the tenant-scoped one identified here, rather than being subsumed by it.
5. **Disk, not CPU, was this experiment's dominant operational constraint** — driven
   by `local_fs` remote storage's byte duplication and the deliberate choice to
   disable automatic GC/compaction (needed to make manual GC pinning measurements
   meaningful). This is a property of this local/manually-GC'd setup, not of the
   phenomenon under test, and shouldn't be read as a claim about Neon's disk
   footprint in production (real object storage, real GC/compaction periods).
6. **Practical implication for Neon operators**: this data suggests the operative
   safeguard against branch-induced interference is not a cap on branch *count* but
   on concurrently-*active* branches per tenant (or per pageserver-visible resource
   pool) — and that spreading heavily-branched, heavily-loaded workloads across
   separate tenants (where isolation is actually enforced by the current
   architecture) sidesteps the effect entirely, at least for the read-dominated
   workload tested here.
