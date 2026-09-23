# 3 — Branch interference on real Neon cloud (Free plan)

Status: **complete, but inconclusive by construction** — the Free plan's branch-count
cap keeps N far below the range (16–32+) where experiments 1 and 2 found the effect
become visible. See "Headline result" and "Neon-side blockers" below.

## Headline result

At the largest N the Free plan allows (**N=9** children, the hard branch-count cap —
see "Neon-side blockers"), **no interference on `main` was detectable above
measurement noise**, in any of the three arms:

| Metric | A0 (N=0) | A1 N=9 (branches) | A1b N=2 (sibling roots) | A2 N=9 (separate projects) |
|---|---:|---:|---:|---:|
| GetPage p99 (ms) | 0.095 [0.089, 0.099] | 0.110 [0.089, 0.133] | 0.185 [0.104, 0.305] | 0.152 [0.097, 0.222] |
| GetPage mean (ms) | 0.0267 [0.0259, 0.0276] | 0.0267 [0.0256, 0.0279] | 0.0308 [0.0260, 0.0386] | 0.0306 [0.0269, 0.0363] |
| Commit p99 (ms) | 2.44 [1.90, 3.47] | 2.16 [1.92, 2.59] | 1.91 [1.84, 1.97] | 2.59 [1.85, 3.94] |

Every arm's 95% CI at its maximum N overlaps A0's. A one-sided Mann-Whitney U test
(N=0 vs. each arm's max N, 5 reps each side, no multiple-comparison correction) found
one nominally significant result — A2 (separate projects) at N=9 on GetPage p99,
p=0.038 — and five non-significant ones (A1 p=0.30, A1b p=0.15, and all three commit-
latency comparisons p>0.34). A single p=0.038 out of six comparisons, on the *control*
arm rather than the branch arm, and not surviving a Bonferroni correction
(threshold 0.05/6≈0.008), is not evidence of a Free-plan-scale branch effect — see
"Why this isn't evidence of an effect" below.

**This is not "branches don't interfere on Neon cloud."** It's "9 branches isn't
enough load, on infrastructure this large and multi-tenant, to move the needle above
noise" — a materially different, much weaker claim. See "Neon-side blockers".

## Hypothesis

Same as experiments 1 and 2: creating N branches of `main` and driving load against
them degrades `main`'s latency, relative to no branches — and the four-arm design
(`A0`/`A1`/`A1b`/`A2`) again isolates branch-specific ancestry effects from generic
tenant-scoped or machine-level contention. See
`experiments/1-branch-interference-local/README.md` for the full mechanism rationale
(copy-on-write sharing, `retain_lsn` GC pinning, single-per-tenant WAL-redo).

## Why real Neon cloud is a different, harder test than experiments 1/2

Experiments 1 and 2 ran on hardware the experiment fully controlled (a 96-core box;
6 dedicated CloudLab nodes) and could push to N=256. Neon cloud on the Free plan is
the opposite on every axis that matters here:

- **N is capped hard.** Confirmed live via `GET /organizations/{org}/limits`:
  `max_branches: 10` (main + at most 9 children) and, more restrictively,
  `max_root_branches: 3` (main + at most **2** sibling root branches — capping the
  A1b arm at N≤2, not N≤9). Both experiments 1 and 2 needed N in the tens before the
  effect was clearly visible; N=9 is roughly the *baseline* end of their grids, not
  the interesting end.
- **The pageserver is not dedicated.** It's shared across an unknown, presumably
  large number of other Neon customers' tenants. Nine tiny (0.25 CU) child computes
  are a rounding error in that fleet's total load — very different from experiment
  2's 8-core pageserver, which N=16-32 *tenants* could visibly saturate on its own.
- **No storage-layer visibility or control.** No pageserver/safekeeper metrics,
  scrubber access, or placement control — the mechanism instrumentation experiments
  1/2 used (`pageserver_wal_redo_seconds`, `GcResult.layers_needed_by_branches`,
  `pageserver_layers_per_read`) isn't reachable at all from a Postgres connection.
  Every number in this experiment is an *end-to-end* proxy for storage-layer
  behavior, not a direct measurement of it.

Given that, the honest expectation going in should have been "probably too small an
N to see anything" — worth confirming empirically (below), not worth skipping.

## Design

### Measurement: e2e, but with the client-network RTT engineered out

The plan's original concern was that client round-trip time (Champaign, IL <->
`aws-us-east-2`, ~15-25ms) would swamp any storage-layer signal if measured as plain
client wall-clock latency. The actual mechanism used is better than the
`clock_timestamp()`-in-plpgsql approach sketched in planning:
**`CREATE EXTENSION neon`** exposes `neon_perf_counters`, a Prometheus-style
histogram view measured *inside the compute*:

- `getpage_wait_seconds_{count,sum,bucket}` — time a backend spent waiting on a
  GetPage@LSN round trip to the pageserver. This is the read-path number the
  hypothesis is actually about, with the client<->compute RTT subtracted out for
  free.
- `quorum_commit_latency_seconds_{count,sum,bucket}` — time walproposer spent
  waiting for a safekeeper write quorum. The equivalent write-path number.

Both are read via one `SELECT` before and after each probe window; `scripts/db.py`'s
`histogram_delta_mean`/`histogram_delta_percentile` turn the bucket deltas into a
per-op mean and a linearly-interpolated p50/p99. Client wall-clock is also recorded
(`client_seconds_total`/`client_ms_per_op`) but only as a supplementary number.

### Getting a genuinely cold read (a real gotcha, found and fixed during calibration)

The plan anticipated that a 2 CU compute's local file cache (LFC) would hold the
whole ≤0.5GB dataset, defeating any attempt to read from the pageserver at all — and
called for restarting the endpoint before each read window to force a cold read.
**Restarting alone turned out not to be enough**, for a reason the plan didn't
anticipate: LFC is disk-backed and **survives a compute restart** (only Postgres'
in-memory `shared_buffers`, 230MB at 2 CU, gets wiped by a restart). A restart-only
probe of the fixed, already-seeded probe blocks came back **203 LFC hits / 2 misses
out of 200 reads** — almost entirely served from LFC. The fix: call
`SELECT neon_clear_lfc()` after every restart, before the probe window. Verified
empirically that 5 known-untouched blocks read right after `neon_clear_lfc()`
produced exactly 5 new misses and 0 new hits. The final per-window protocol is:
restart (empties `shared_buffers`) → reconnect → warm up the planner's catalog
caches with one throwaway query (so catalog GetPages don't pollute the probe's own
delta) → `neon_clear_lfc()` → snapshot counters → P-read (150 distinct never-since-
restart heap pages, one round trip via `ctid = ANY(...)`) → snapshot → P-write (200
single-row UPDATE+commit round trips) → snapshot. P-start is the restart itself:
`restart_elapsed_s` (API issue-to-active) and `first_query_elapsed_s` (issue-to-
first-successful-query).

### Four arms, same semantics as experiments 1/2

| Arm | What exists | N grid | Isolates |
|---|---|---|---|
| A0 | `main` only | N=0 (shared baseline) | baseline |
| A1 | `main` + N branches of `main`, same project | 1, 3, 6, 9 | the hypothesis |
| A1b | `main` + N sibling **root** branches (`init_source=schema-only`), same project, no ancestor | 1, 2 (capped — see above) | tenant-scoped sharing, no ancestry |
| A2 | `main`'s project + N separate **projects** (separate tenants), each with one branch | 1, 3, 6, 9 | account/pageserver-fleet-level contention only |
| A0_end | `main` only, re-measured after every other arm | N=0 | drift check (below), not a hypothesis arm |

Each child (branch or project) is seeded once with `pgbench -i -s 1` (~16MB TPC-B
schema) and, during every rep that includes it, driven with `pgbench -T 18 -c 4 -j 2`
(TPC-B mix: reads + writes) starting ~1.5s before the probe window on `main` and
overlapping it. `main`'s own compute is pinned `autoscaling_limit_min_cu =
autoscaling_limit_max_cu = 2` (the Free-plan max) so its own compute sizing is never
the bottleneck; children run at a fixed 0.25 CU (load generators, not the system
under test).

### Order randomization and the drift check

Early manual testing surfaced real run-to-run variance in `getpage_wait_seconds`
(one early measurement came back ~20x higher than later ones taken minutes later, on
the *same* N) — plausibly a cold-vs-warm underlying VM/pageserver-connection effect
across a compute restart, not something the Free-plan API exposes or lets you
inspect or control. Two defenses, both applied in `scripts/sweep.py`:

1. **Randomized (not nested-ascending) execution order** within each arm's block —
   `(n, rep)` points are shuffled (fixed seed) before running, so a monotonic drift
   over wall-clock time can't get aliased onto N.
2. **Re-measured the N=0 baseline (`A0_end`) after every other arm** — comparing A0
   vs. A0_end directly shows whatever drift happened over the ~15 minutes the sweep
   ran. Result: A0 getpage-mean ranged 0.0258-0.0283ms, A0_end ranged 0.0258-0.0292ms
   (`figures/drift_check.png`) — a small (~5-10%), non-systematic difference, not a
   confound large enough to explain anything in the main results.

Arm-to-arm ordering (A0 → A1 → A1b → A2 → A0_end) is *not* randomized — A1 and A1b
share the same project's 10-branch budget as `main`, so A1's branches must be torn
down before A1b's siblings can be created, and vice versa; this makes them
inherently sequential blocks, not something that can be freely interleaved. A2 didn't
have this constraint but was still kept as its own block for simplicity, given the
data below.

## Neon-side blockers (as requested: reported here in full)

1. **The Free plan's branch cap makes N=9 the practical ceiling for the hypothesis
   arm, and N=2 for the sibling-root arm.** This is the dominant limitation of this
   experiment. Confirmed live via `GET /organizations/{org}/limits`:
   `max_branches: 10`, `max_root_branches: 3`. Experiments 1 and 2 both needed N in
   the tens to see a clear effect; this experiment's entire grid sits below or at
   the low end of where those experiments' effect started to separate from noise.
   Nothing about the read/write probing methodology here needed N>9 to work
   correctly — it's purely an account-tier limit, not a methodological one.
2. **No storage-layer visibility.** No pageserver metrics, no scrubber, no
   `GcResult`/`layers_needed_by_branches`-equivalent, no placement control. Every
   number here is end-to-end (compute-observed), which is a strictly weaker signal
   than experiments 1/2's direct pageserver instrumentation, and can't distinguish
   *why* a change happened even if one had been detected.
3. **Compute is capped at 2 CU** (`max_autoscaling_cu: 2`, `max_fixed_size_cu: 2`),
   confirmed live — consistent with the plan, main's compute was pinned there deliberately
   so this cap didn't accidentally become confounded with the branch effect.
4. **LFC survives a compute restart** (see "Getting a genuinely cold read" above) —
   not documented anywhere consulted during planning, discovered empirically, and
   would have silently invalidated every read-probe number if not caught.
5. **Storage is capped at 512MB per project** (`branch_logical_size_limit_bytes`).
   Not actually binding here (main topped out at 92MB including all A1/A1b branch
   metadata and probe/write tables; each A2 project's pgbench -s1 seed was ~47MB) but
   worth flagging as the reason the probe dataset was kept deliberately small (50MB)
   rather than large enough to make cache effects a non-issue on its own.
6. **Not a blocker, but a real gotcha:** `init_source=schema-only` combined with an
   *omitted* `parent_id` is what actually produces a true sibling root branch (no
   live ancestor, `parent_id: null` in the branch listing) — passing an explicit
   `parent_id` alongside `init_source=schema-only` instead produces a schema-only
   *child* of that parent (still ancestrally linked). This distinction isn't obvious
   from the API reference alone and was confirmed by creating one of each and
   inspecting `GET .../branches`.

## Why this isn't evidence of an effect (the A2 p=0.038 point)

The one nominally-significant comparison (A2 N=9 GetPage p99, p=0.038) is (a) on the
*generic-contention control* arm, not the branch-specific hypothesis arm A1, which
came back non-significant (p=0.30) at the same N; (b) one result out of six
comparisons run, not surviving Bonferroni correction; and (c) built on 5-vs-5 samples
with visibly overlapping bootstrap CIs in `figures/getpage_p99_vs_n.png` (A2's N=9
CI is [0.097, 0.222]ms, which contains A0's mean of 0.095ms). Read together with A1's
non-significant result at the same N, and A1b's equally noisy N=2 point (one outlier
rep pushed its mean p99 to 0.185ms against a CI of [0.104, 0.305]), the far more
likely explanation is ordinary measurement noise at small N on shared multi-tenant
infrastructure, not the hypothesized mechanism finally appearing at N=9 in the
control arm while staying absent in the treatment arm.

## Reproduction

```bash
cd experiments/3-branch-interference-neon-cloud
python3 -m venv .venv && source .venv/bin/activate
pip install "psycopg[binary]" requests numpy matplotlib scipy

export NEON_API_KEY=...   # or ~/.neon_api_key, chmod 600
cd scripts
python3 setup_main.py     # one-time: seeds main's probe_main/probe_write_main, picks probe blocks
python3 sweep.py          # resumable: A0 -> A1 -> A1b -> A2 -> A0_end, skips (arm,n,rep) already in data/raw.jsonl
python3 analyze.py        # writes data/summary_table.md and figures/*.png
```

Secrets: the Neon API key and every branch/project's database password live under
`lib.SECRETS_DIR` (a path outside this repo, in the session's scratchpad), never
under `data/manifests/` — see the docstrings in `scripts/lib.py` and
`scripts/materialize.py`. A prior version of `materialize.py` briefly wrote a branch
password into `data/manifests/arm_a1_branches.json` before this repo directory was
first inspected for a commit; it was caught and fixed (password moved to
`SECRETS_DIR`, manifest rewritten) before anything was committed — mentioned here
for the record, not because it reached git history.

## Data and figures

- `data/raw.jsonl` — 60 measurement points (5 arms/baselines × their N-grid × 5 reps).
- `data/summary_table.md` — mean + bootstrap 95% CI for GetPage/commit mean and p99,
  per (arm, N).
- `figures/getpage_p99_vs_n.png`, `figures/getpage_mean_vs_n.png`,
  `figures/commit_p99_vs_n.png` — latency vs. N, all three arms, A0 as the shared
  N=0 origin.
- `figures/drift_check.png` — A0 (start) vs. A0_end (after the full sweep).

## Takeaways

1. **The hypothesis is neither confirmed nor refuted by this experiment** — the
   Free-plan branch cap (N≤9, N≤2 for sibling roots) keeps the grid below the N
   range where experiments 1 and 2 needed to go before the effect separated from
   noise. Report this as "inconclusive due to an account-tier limit," not as a
   negative result about Neon cloud's behavior at scale.
2. **A paid plan (Launch: 10 included + $1.50/branch-month overage, or Scale: 25
   included) would very plausibly change this outcome** — it would allow N well into
   the range (20-30+) where experiments 1/2's effect became clearly visible, at
   modest incremental cost given how cheap each 0.25 CU child and each ~7-10s
   measurement window were here.
3. **The real methodological contribution of this experiment is the recipe**, not
   the (inconclusive) numbers: `neon_perf_counters` for e2e-but-storage-layer-only
   latency without a client-RTT confound, `neon_clear_lfc()` + restart for a
   genuinely cold read (restart alone is insufficient), and
   `init_source=schema-only` with no `parent_id` for a true sibling root branch. All
   three would carry over unchanged to a re-run on a paid plan at higher N.
4. Per this repo's standing experiment policy, this negative/inconclusive result is
   reported in full rather than omitted or reframed as a positive finding.
