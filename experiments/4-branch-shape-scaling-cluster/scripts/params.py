"""
Single source of truth for the scale-dependent knobs, so the exact same
setup_root.py / materialize.py / measure.py / sweep.py code paths serve both the
Phase 0 pilot (small, fast, a gate before committing machine-days) and the full
production run. Select with the PILOT env var:

    PILOT=1 python3 setup_root.py     # ~1/40 scale, small N
    python3 setup_root.py             # full scale, production N

See README.md's "Design" and "Phase 0 pilot" sections for the rationale behind
every number here -- in particular, DIVERGENCE_FRACTION and VERTICAL_N_MAX were
both revised down from the original plan after the pilot found the divergence-write
mechanism's true cost (see README's "Phase 0 pilot" section): each branch's
divergence write is bounded by a real per-page GetPage@LSN round-trip cost
(~20-40 pages/sec per connection, empirically), and the vertical chain is strictly
serial, so wall-clock scales as
`(root_scale x divergence_fraction) x vertical_n_max / per_connection_page_rate`.
"""
from __future__ import annotations

import os

PILOT = os.environ.get("PILOT", "0") == "1"

if PILOT:
    TAG = "pilot"
    ROOT_SCALE = 500          # ~7.5GB, 50M rows
    HORIZONTAL_N_MAX = 8
    VERTICAL_N_MAX = 4
    HORIZONTAL_N_GRID = [1, 2, 4, 8]
    VERTICAL_N_GRID = [1, 2, 4]
    REPS = 3
else:
    TAG = "prod"
    # Revised down TWICE from the plan's original 20000 (~300GB): first to 3000
    # (~59GB) after Phase 0 found the root's own one-time primary-key-index build
    # is a slow, whole-table operation whose cost scales with root_scale alone
    # (projected ~2.74 days total at 20000); then, per the user, the goal was
    # reframed as a proof-of-concept ("verify branching causes interference +
    # explore depth vs width", not a rigorous disk-bound benchmark) with a hard
    # same-day deadline, so root_scale was cut again to exactly match the
    # already-fully-characterized PILOT scale (500, ~9.8GB) -- this is the one
    # value in this whole file with real, direct, non-extrapolated timing data
    # from Phase 0 (pgbench-i ~37min, pkey index 400s-2724s depending on cache
    # warmth, compaction ~995s), so root build is bounded by known numbers, not a
    # fresh extrapolation. HORIZONTAL_N_MAX/VERTICAL_N_MAX/grids stay at the full
    # originally-planned values -- at this small scale, divergence writes are
    # cheap (819 pages/branch), so the full N-grid costs minutes, not hours, and
    # still gives the complete depth-vs-width comparison.
    ROOT_SCALE = 500          # ~9.8GB, 50M rows -- same as the pilot
    HORIZONTAL_N_MAX = 64
    VERTICAL_N_MAX = 16       # capped well below horizontal: the vertical chain is
                              # strictly serial (level k+1 needs level k's data),
                              # so it is the long pole regardless of divergence %.
    HORIZONTAL_N_GRID = [1, 2, 4, 8, 16, 32, 64]
    VERTICAL_N_GRID = [1, 2, 4, 8, 16]
    REPS = 3                  # trimmed from 5 -- this is a POC, not a
                               # publication-grade CI; 3 reps still gives a
                               # bootstrap CI and cuts measurement wall-clock ~40%

N_MAX = max(HORIZONTAL_N_MAX, VERTICAL_N_MAX)  # for code that needs a single ceiling

# Divergence: one row updated per touched page (see lib.divergence_write's
# docstring for why "fraction of rows" and "fraction of pages" are very different
# for pgbench_accounts). Revised down from the plan's original 5% to 0.1% after the
# pilot showed 5% would need ~2.3TB of disk (only 1.4TB available) and put the
# vertical chain's wall-clock in the tens-of-days range. At 0.1%, per-branch page
# count at full scale is close to what the pilot directly measured at 5% and pilot
# scale, so the timing is a direct empirical result, not an extrapolation.
DIVERGENCE_FRACTION = 0.001
DIVERGENCE_N_SLOTS = 20  # wraps if N_MAX > 20; fine, just means some branches share
                          # a divergence offset (only affects a decorrelation nicety)

STORAGE_NUM_CLIENTS_PER_BRANCH = 4
STORAGE_RUNTIME_S = 30    # trimmed from 60s for the POC/same-day deadline; pagebench
                           # still aggregates plenty of requests/branch (~100+ RPS
                           # observed) in 30s -- enough for a clear signal, if not a
                           # publication-grade sample size.
STORAGE_WARMUP_S = 10

COMPUTE_TIER_N = [n for n in [1, 4, 16] if n <= VERTICAL_N_MAX]
COMPUTE_TIER_REPS = 2 if not PILOT else 3
COMPUTE_TIER_CLIENTS = 8
COMPUTE_TIER_RUNTIME_S = 30 if not PILOT else 60

# Disk safety floor, node0:/mydata. Pilot uses a much smaller floor since the pilot
# itself only needs ~tens of GB; prod's floor reflects the corrected (much smaller,
# post-divergence-fix) disk projection -- see README's "Phase 0 pilot" section.
DISK_FLOOR_GB = 20.0 if PILOT else 100.0

MAIN_PORT = 55432       # root's persistent compute
DIVERGENCE_PORT = 55433  # transient divergence-writer compute, reused sequentially
COMPUTE_TIER_BASE_PORT = 55440  # static read-only computes, one port per concurrent endpoint
