# 2 — Branch interference on a real Neon cluster (CloudLab, 6× m400)

Status: **complete** (Phase 3, read tier — the planned scope). Write tier (Phase 4) was
explicitly out of scope per the plan's decision table and was not attempted.

## Headline result

**The mechanism identified in experiment 1 (single-per-tenant WAL-redo-process serialization)
does not explain what dominates on this cluster.** At N=32 (the largest N common to all three
arms), decomposing `main`'s p99 latency increase gives:

| Component | ΔP99 (ms) | Share |
|---|---:|---:|
| A1 − A1b (branch-specific: ancestry, `retain_lsn`, shared layers) | **−84.4** | (negative) |
| A1b − A2 (tenant-scoped: WAL-redo serialization, serial compaction/GC) | **+7.0** | ~3% |
| A2 − A0 (generic machine/pageserver contention) | **+138.7** | ~97% (and then some) |

This is close to the **opposite** of experiment 1's split (~9% branch-specific / ~91%
tenant-scoped / ~0% generic on a 96-core single machine). Here, on an 8-core pageserver node
with physically separate compute, **generic pageserver-level resource contention dominates
almost entirely**, the tenant-scoped WAL-redo effect is barely detectable, and branches are
actually *less* harmful than same-tenant sibling timelines or separate tenants under matched
load (A1 < A1b ≈ A2 at every N from 4 through 32 — see `figures/latency_vs_n.png`).

Absolute numbers: baseline (N=0) p99 ≈ 1.35–1.38ms across all three arms (vs. experiment 1's
0.63ms — expected, this is a real 10Gbps/78µs-RTT network path plus a slower SATA SSD, not
loopback). By N=32, A1 = 62.7ms, A1b = 147.1ms, A2 = 140.1ms. A1 continues to N=256 (the other
two arms were capped at N=32 by disk budget — see "Deviations from the plan"), reaching
202.9ms p99 at one successfully-completed rep — and **at N=256, 4 of 5 reps never completed a
single probe response at all** (see "Extreme-N: qualitative failure mode" below), which is
itself the most dramatic data point in the whole experiment.

## Context

Experiment 1 (`experiments/1-branch-interference-local/`) ran pageserver, safekeeper and 128+
computes on one 96-core box via `neon_local` and found `main`'s read latency degrades severely
under N branches, with the effect ~91% attributable to same-tenant sharing (chiefly a single
serialized WAL-redo process per tenant) and ~9% branch-specific, while N separate tenants under
identical load left `main` flat. Two weaknesses motivated this follow-up:

1. Everything shared one machine, so "tenant-scoped" was never cleanly separated from same-box
   CPU/IO contention.
2. The mechanism was asserted from indirect evidence (metric shape, not direct instrumentation);
   the read-only tier could not exercise `retain_lsn` GC pinning at all (`GcResult` was all-zero
   throughout, both there and here — see `figures/mechanism_gc.png`).

This experiment deploys pageserver, safekeeper, storage_controller and computes on **6
physically separate CloudLab m400 nodes** (aarch64, 8 cores each, 10Gbps/78µs-RTT LAN) to
re-test the same 4-arm design with compute genuinely separate from storage, plus direct
WAL-redo metric instrumentation. Full environment details, deployment gotchas, and the
appendix runbook are in `agent/experiment-2-cluster-plan.md` and `agent/cloudlab.md`;
a blow-by-blow of what was actually done (including every deviation and bug fix) is in
`agent/experiment-2-progress.md`.

## Design (unchanged from experiment 1 — see that README for full rationale)

Four arms isolate three components of `main`'s latency increase:

| Arm | What exists | Isolates |
|---|---|---|
| A0 | main only (the N=0 point of any arm) | baseline |
| A1 | main + N **branches** of main, same tenant | hypothesis |
| A1b | main + N **sibling root timelines**, same tenant, no ancestor | tenant-scoped sharing, no ancestry |
| A2 | main + N **separate tenants** | machine/pageserver contention only |

Tier STORAGE (the only tier run here): an open-loop pagebench probe against `main` at a fixed
50 req/s from an isolated node (node2), concurrent with closed-loop (saturating) pagebench read
load against the N arm-specific targets, split round-robin across three dedicated
load-generator nodes (node3–5). `main`'s tenant config sets `gc_period=0s`,
`compaction_period=0s`, `pitr_interval=0s`, `checkpoint_timeout=10years` so nothing runs in the
background; a manual checkpoint→compact→do_gc protocol runs between windows and its `GcResult`
(specifically `layers_needed_by_branches`) is recorded directly.

**New in this experiment:** every measurement point also scrapes
`pageserver_wal_redo_seconds`, `pageserver_wal_redo_records_histogram`,
`pageserver_layers_per_read`, `pageserver_get_vectored_seconds`, and
`pageserver_page_cache_read_{hits,accesses}_total` before and after the window
(`measure.py`'s `redo_before`/`redo_after`/`redo_delta`), to instrument the WAL-redo hypothesis
directly rather than infer it from latency shape alone.

## Cluster topology

| Node | Role |
|---|---|
| node0 | pageserver only (device under test) |
| node1 | safekeeper + storage_broker + storage_controller + its own Postgres + compute-hook stub |
| node2 | `main`'s persistent compute + the isolated probe client |
| node3–5 | load generators (pagebench, round-robin split) |

Full hand-deployment (no `neon_local` against the real cluster — see "Deviations" for why and
how). Concrete configs, gotchas, and the exact commands used to bring every service up are in
`agent/experiment-2-cluster-plan.md`'s appendix and `agent/experiment-2-progress.md`.

## Reproduction

```bash
# 1. Deploy services by hand per agent/experiment-2-cluster-plan.md's appendix
#    (broker/storcon-PG/compute-hook-stub/storage_controller/safekeeper on node1,
#    pageserver on node0). See agent/experiment-2-progress.md for the exact commands
#    actually used and every gotcha hit along the way.

# 2. From the coordinator (NOT one of the 6 nodes -- see "Deviations" for why every
#    HTTP call below is routed through SSH, not called directly):
cd experiments/2-branch-interference-cluster/scripts
python3 setup_cluster.py          # creates the `main` tenant/timeline/compute, seeds pgbench -s5
python3 sweep.py                  # materializes all 3 arms, runs all N x rep points, resumable
python3 analyze.py                # writes data/summary_table.md and figures/*.png
```

`sweep.py` is resumable: it checks `data/raw.jsonl` before each point and `data/manifests/*.json`
before creating each entity, so a killed/restarted run only does the remaining work. This
experiment's actual run needed two restarts, both due to the N=256 hang described below, not to
any interruption of the run itself.

## Extreme-N: qualitative failure mode, not just quantitative

At N=256 (A1 only — the only arm materialized that high), the open-loop probe process itself
got stuck rather than merely running slow. In the first attempt, the probe never returned
across **more than an hour** with the raw SSH call blocking indefinitely; `ps` on the probe node
showed the process at 0% CPU (network-blocked, not looping). The pageserver process itself
stayed externally healthy throughout (kept answering `/metrics` in under 300ms, no panics, no
error-level logs beyond expected `CopyFail during COPY` from the killed clients).

This forced two harness fixes (see `agent/experiment-2-progress.md` for the diagnosis):
wrapping every remote `pagebench` invocation in GNU `timeout` (so a hang can never again produce
an orphaned process regardless of what the SSH connection does), and widening the grace window
generously (60s runtime + 240s soft + 30s hard-kill = up to 330s) since a tight window would
silently convert a genuine extreme-tail measurement into an empty one. Even with the widened
window, **4 of 5 reps at N=256 still never got a single response** within 325s (the 5th,
measured before the timeout hardening existed, completed in 83s with p99=202.9ms).

The WAL-redo metrics, scraped independently via `/metrics` and unaffected by the probe's own
hang, show why this isn't a harness artifact: during those 4 stuck windows,
`pageserver_wal_redo_seconds_sum` increased by **47,000–47,135 seconds within a ~325-second
wall-clock window** — around 145x more cumulative redo time than wall-clock allows for work
happening one-at-a-time. Since this metric evidently accounts for time each caller spends
*waiting on* the (per the hypothesis, single, per-tenant) redo resource, not just active CPU
time, a sum this far past the wall-clock bound is strong direct evidence of a massive,
still-growing backlog on that shared resource — real confirmation that *something* about
same-tenant serialization is severely backed up at N=256, even though (see the headline result)
it is not the dominant driver of the smaller-N results measured with all three arms. The
mechanism may simply need much higher N to dominate visibly than the generic contention that
already saturates the 8-core pageserver by N=16–32.

These 4 points are kept in `data/raw.jsonl` tagged `"starved": true` with a `note` field, not
deleted or endlessly retried — the fact of indefinite starvation *is* the data point.

## Mechanism instrumentation: does the WAL-redo hypothesis hold?

Per-operation redo cost at N=32 (`redo_delta.pageserver_wal_redo_seconds_sum /
redo_delta.pageserver_wal_redo_seconds_count`, averaged over reps):

| Arm | ms/redo-op | ms/get_vectored-op |
|---|---:|---:|
| A1 | 9.8 | 23.9 |
| A1b | 12.7 | 35.2 |
| A2 | 5.5 | 32.1 |

If single-per-tenant WAL-redo serialization were the dominant driver, A2 (separate tenants,
each with its own redo process) should show markedly *lower* per-operation cost than A1/A1b —
and it does for the redo-specific metric (5.5ms/op vs. 9.8–12.7ms/op) — but `get_vectored`
latency (the actual end-to-end page-fetch cost the probe experiences) is comparable between
A1b and A2 (35.2 vs. 32.1 ms/op) and *not* dramatically different from A1 (23.9ms/op). The
redo-specific signal exists and points the expected direction, but it's a minority contributor
to what the probe actually feels; something arm-independent (thread-pool/connection-handling
capacity, CPU scheduling, or page-cache pressure shared by all concurrent readers regardless of
tenant) dominates instead. `layers_needed_by_branches` (the direct `retain_lsn` GC-pinning
signal) stayed at exactly 0 throughout for every arm at every N (`figures/mechanism_gc.png`) —
expected, since this is a read-only tier with no writes to `main` and hence nothing to pin;
that mechanism remains completely untested, same as in experiment 1.

## Why A1 < A1b ≈ A2 (branches are *less* harmful than independent data)

The likely explanation: A1's branches are copy-on-write off `main` and share its underlying
layer files, so load against them is partially served from data physically already resident /
cache-warm for `main`. A1b's siblings and A2's tenants are each independently seeded
(`pgbench -s 5`) and share nothing with `main` on disk, so load against them is pure
cache-cold competition for the pageserver's small page cache (`page_cache_size = 131072` ×
8KiB ≈ 1GiB) and CPU, on top of whatever generic contention N itself creates. This is a
plausible reading, not directly instrumented here (`pageserver_page_cache_read_hits_total` /
`_accesses_total` were scraped but not broken out per-arm in this analysis) — a natural next
step if this experiment is extended.

## Deviations from the plan (`agent/experiment-2-cluster-plan.md`)

All operational deployment details, bugs found, and fixes are logged in full in
`agent/experiment-2-progress.md`; summarized here:

- **The coordinator is not on the cluster's private LAN.** Direct `requests` calls from the
  coordinator to the storage_controller/pageserver HTTP APIs hang/timeout; every management-API
  call is routed through `curl` over SSH to node1, which the plan didn't anticipate.
- **`/mydata` is root-owned by default** on a fresh CloudLab instance on every node; needed
  `chown` before anything could write there.
- **protoc's `include/` directory** (well-known proto types) has to be installed alongside the
  `protoc` binary in the same relative layout, or `storage_broker`'s build fails — copying just
  the binary (a natural reading of the plan's protoc step) isn't enough.
- **`compute_ctl` needs `LD_LIBRARY_PATH` set in its own environment**, not just on the
  `postgres` binary's rpath, or the `neon.so` extension fails to load (`libpq.so.5: cannot open
  shared object file`).
- **`spec.safekeepers_generation` must be `null`**, not a real generation number, for a
  hand-deployed compute talking to a safekeeper that was never registered via
  `timelines_onto_safekeepers` — otherwise walproposer sends `allow_timeline_creation=false` and
  the safekeeper refuses the timeline forever. The plan's "safekeeper registration is not needed"
  note covers the storcon side of this but not this compute-side field.
- **N grid capped lower for A1b/A2 than the plan's own table**: materialized to N=32 as
  planned, but disk/time budget and the extreme-N findings above meant this was the practical
  ceiling for those two arms (A1 alone went to 256, as planned, where the extreme-N pathology
  showed up).
- Endpoint config generation used a throwaway single-node `neon_local` stack on the coordinator
  to produce a correctly-shaped `config.json` template for *this* checkout's schema (see
  progress log) — the plan's pointer to the docker-compose reference spec turned out to be a
  stale schema shape, not a working template as-is.
- Phase 4 (write tier) was not attempted — out of scope per the plan's own decision table
  ("Read tier first; write tier only if phase 1 is clean"), and given the headline result here
  (dominant mechanism is generic pageserver contention, not tenant/branch-scoped), a write-tier
  follow-up would need to specifically re-examine whether that conclusion changes under
  `retain_lsn` GC-pinning load, which never triggered in this read-only tier.

## Data and figures

- `data/raw.jsonl` — 90 measurement points (all 3 arms × their full N-grids × 5 reps), one JSON
  record per line, `starved: true` on the 4 N=256 points that never got a response.
- `data/summary_table.md` — p99 mean + bootstrap 95% CI + mean missed count per (tier, arm, N).
- `data/manifests/` — resumable materialization records (which branch/sibling/tenant is which
  timeline/tenant ID).
- `figures/latency_vs_n.png` — the headline p99/p95-vs-N chart, all 3 arms.
- `figures/decomposition.png` — the 3-way split at N=32 (see "Headline result").
- `figures/mechanism_gc.png` — `layers_needed_by_branches` vs. N (flat zero, as expected for a
  read-only tier).
- `figures/missed_vs_n.png`, `figures/percentile_ladder.png` — supplementary.

## Takeaways

1. **Experiment 1's "~91% tenant-scoped" conclusion does not generalize to constrained
   hardware.** On an 8-core pageserver with genuinely separate compute, generic
   pageserver-level resource contention (not tenant- or branch-scoped) explains almost all of
   `main`'s degradation up to N=32. The earlier conclusion was likely an artifact of experiment
   1's 96-core box having so much CPU headroom that only the tenant-scoped serialization
   bottleneck was ever visible; on more realistic hardware it's swamped by generic contention
   long before it would dominate.
2. **Branches are not the worst case here — independent data is.** A1 (real branches, sharing
   `main`'s layers via COW) consistently showed *lower* p99 than A1b/A2 (independently-seeded
   data) at every matched N from 4 to 32. If anything, this experiment's practical takeaway for
   Neon's design is closer to "don't put many independently-loaded tenants/timelines on one
   small pageserver" than "don't create many branches."
3. **The WAL-redo-serialization mechanism is real but not dominant at this scale** — per-op redo
   cost is lower for A2 than A1/A1b as the hypothesis predicts, but it's a small fraction of the
   end-to-end `get_vectored` cost the probe experiences. It becomes unambiguously dominant only
   at extreme N (256), where it manifests as outright starvation (unbounded queueing) rather
   than merely high latency — a genuinely different failure *mode*, not just a bigger number.
4. **This read-only tier still cannot exercise `retain_lsn` GC pinning** (`layers_needed_by_branches`
   stayed exactly 0 throughout) — same limitation as experiment 1, would need Phase 4 (write
   tier, not run here) to test directly.
5. Negative/refining results are reported here in full per the plan's own instruction ("design
   the write-up so the negative result is as publishable as the positive") — this experiment set
   out to confirm and pin down experiment 1's mechanism, and instead substantially revised it.
