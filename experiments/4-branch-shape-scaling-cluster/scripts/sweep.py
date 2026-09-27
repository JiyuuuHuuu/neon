#!/usr/bin/env python3
"""
Phase 3+4 orchestrator: materialize both trees, then sweep N x rep for Tier
STORAGE (primary) and Tier COMPUTE (secondary, run after storage tier succeeds).

The two shapes are NEVER measured concurrently -- horizontal.py and vertical.py load
phases are fully separated in time, and within the sweep loop the block order
alternates (H-first on even reps, V-first on odd) so time-dependent drift (e.g.
pageserver warm-up, thermal effects) cannot masquerade as a shape difference. Fully
resumable: checks data/raw_{tag}.jsonl before each point, manifests before each
materialized entity.

Usage:
    PILOT=1 python3 sweep.py     # Phase 0 pilot
    python3 sweep.py             # full run (only after the pilot's projections are
                                  # reviewed -- see README.md's "Plan of work")
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib
import materialize
import measure
import params

STATE_FILE = lib.EXPERIMENT_DIR / "data" / f"cluster_state_{params.TAG}.json"
RAW_PATH = lib.EXPERIMENT_DIR / "data" / f"raw_{params.TAG}.jsonl"


def append_result(record: dict):
    with open(RAW_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def already_done(key: dict) -> bool:
    if not RAW_PATH.exists():
        return False
    with open(RAW_PATH) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if all(rec.get(k) == v for k, v in key.items()):
                return True
    return False


def materialize_all(state: dict):
    tenant_id = state["tenant_id"]
    root_timeline_id = state["root_timeline_id"]
    root_lsn = state["root_lsn"]
    page_rows = state["page_rows"]
    row_count = state["row_count"]

    h_manifest = materialize.load_manifest("horizontal")
    v_manifest = materialize.load_manifest("vertical")
    if len(h_manifest) < params.HORIZONTAL_N_MAX or len(v_manifest) < params.VERTICAL_N_MAX:
        print("== flipping image_creation_threshold before any branch write ==", flush=True)
        materialize.prepare_for_branching(tenant_id)

    print(f"== materializing HORIZONTAL up to N={params.HORIZONTAL_N_MAX} ==", flush=True)
    materialize.materialize_horizontal(tenant_id, root_timeline_id, root_lsn,
                                        params.HORIZONTAL_N_MAX, page_rows, row_count, "horizontal")
    print(f"== materializing VERTICAL up to N={params.VERTICAL_N_MAX} ==", flush=True)
    materialize.materialize_vertical(tenant_id, root_timeline_id, root_lsn,
                                      params.VERTICAL_N_MAX, page_rows, row_count, "vertical")


def run_storage_tier():
    grids = {"horizontal": params.HORIZONTAL_N_GRID, "vertical": params.VERTICAL_N_GRID}
    for rep in range(params.REPS):
        shape_order = ["horizontal", "vertical"] if rep % 2 == 0 else ["vertical", "horizontal"]
        for shape in shape_order:
            manifest = materialize.load_manifest(shape)
            for n in grids[shape]:
                key = {"tag": params.TAG, "tier": "storage", "shape": shape, "n": n, "rep": rep}
                if already_done(key):
                    print(f"skip (done): {key}")
                    continue
                print(f"=== STORAGE {shape} N={n} rep={rep} ===", flush=True)
                entries = manifest[:n]
                try:
                    t0 = time.time()
                    res = measure.measure_storage_point(
                        entries, tag=f"{shape}_{n}_{rep}",
                        num_clients=params.STORAGE_NUM_CLIENTS_PER_BRANCH,
                        runtime_s=params.STORAGE_RUNTIME_S,
                    )
                    res["wall_s"] = time.time() - t0
                except Exception as e:  # noqa: BLE001
                    res = {"error": str(e), "traceback": traceback.format_exc()}
                append_result({**key, **res, "ts": time.time()})


def run_compute_tier():
    for rep in range(params.COMPUTE_TIER_REPS):
        shape_order = ["horizontal", "vertical"] if rep % 2 == 0 else ["vertical", "horizontal"]
        for shape in shape_order:
            manifest = materialize.load_manifest(shape)
            for n in params.COMPUTE_TIER_N:
                key = {"tag": params.TAG, "tier": "compute", "shape": shape, "n": n, "rep": rep}
                if already_done(key):
                    print(f"skip (done): {key}")
                    continue
                print(f"=== COMPUTE {shape} N={n} rep={rep} ===", flush=True)
                entries = manifest[:n]
                try:
                    t0 = time.time()
                    res = measure.measure_compute_point(
                        entries, clients=params.COMPUTE_TIER_CLIENTS,
                        runtime_s=params.COMPUTE_TIER_RUNTIME_S,
                    )
                    res["wall_s"] = time.time() - t0
                except Exception as e:  # noqa: BLE001
                    res = {"error": str(e), "traceback": traceback.format_exc()}
                append_result({**key, **res, "ts": time.time()})


def main():
    state = json.loads(STATE_FILE.read_text())
    materialize_all(state)
    print("== Tier STORAGE sweep ==", flush=True)
    run_storage_tier()
    print("== Tier COMPUTE sweep ==", flush=True)
    run_compute_tier()


if __name__ == "__main__":
    main()
