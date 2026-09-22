#!/usr/bin/env python3
"""
Full sweep orchestrator for Tier STORAGE (Phase 3, read tier -- see the plan's
"Scope: Read tier first" decision; Tier ENDPOINT / write-load is Phase 4, out of
scope here and not ported). Resumable: materialization manifests and the raw
results JSONL are both checked before doing work, so a restart skips completed work.

Usage:
    python3 sweep.py
"""
from __future__ import annotations

import json
import random
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib
import materialize
import measure

STATE_FILE = lib.EXPERIMENT_DIR / "data" / "cluster_state.json"
RAW_PATH = lib.EXPERIMENT_DIR / "data" / "raw.jsonl"

# Per the plan's decision table: shrink dataset (scale 50->5), keep N high for A1
# (up to 256), lower A1b/A2's max to 32 (node0's /mydata is 37GB vs experiment 1's
# much larger local NVMe budget).
STORAGE_N_GRID = {
    "A1": [0, 1, 4, 16, 32, 64, 128, 256],
    "A1b": [0, 1, 4, 16, 32],
    "A2": [0, 1, 4, 16, 32],
}
STORAGE_REPS = 5
STORAGE_N_MAX = {"A1": 256, "A1b": 32, "A2": 32}

LOAD_CLIENTS_PER_TARGET = 4
PROBE_RATE = 50.0
STORAGE_RUNTIME_S = 60


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


def get_or_materialize(arm: str, n_max: int, tenant_id: str, main_timeline_id: str) -> list[dict]:
    name = f"{arm}_storage"
    if arm == "A1":
        return materialize.materialize_branches(tenant_id, main_timeline_id, n_max, name)
    elif arm == "A1b":
        return materialize.materialize_siblings(tenant_id, n_max, name)
    elif arm == "A2":
        return materialize.materialize_tenants(n_max, name)
    raise ValueError(arm)


def run_storage_tier():
    state = json.loads(STATE_FILE.read_text())
    tenant_id = state["tenant_id"]
    main_timeline_id = state["main_timeline_id"]
    main_ttid = measure.ttid(tenant_id, main_timeline_id)

    for arm, n_max in STORAGE_N_MAX.items():
        print(f"=== materializing STORAGE/{arm} up to N={n_max} ===", flush=True)
        get_or_materialize(arm, n_max, tenant_id, main_timeline_id)

    points = []
    for arm, grid in STORAGE_N_GRID.items():
        for n in grid:
            for rep in range(STORAGE_REPS):
                points.append((arm, n, rep))
    random.Random("branch-interference-storage-cluster").shuffle(points)

    for arm, n, rep in points:
        key = {"tier": "storage", "arm": arm, "n": n, "rep": rep}
        if already_done(key):
            print(f"skip (done): {key}")
            continue
        print(f"=== STORAGE {arm} N={n} rep={rep} ===", flush=True)
        gc_before = measure.gc_protocol(tenant_id, main_timeline_id)

        entries = materialize.load_manifest(f"{arm}_storage")[:n]
        load_targets = [measure.ttid(e["tenant_id"], e["timeline_id"]) for e in entries]

        try:
            t0 = time.time()
            res = measure.measure_storage_point(
                main_ttid=main_ttid, load_targets=load_targets,
                num_clients=LOAD_CLIENTS_PER_TARGET, runtime_s=STORAGE_RUNTIME_S,
                probe_rate=PROBE_RATE, keyspace_tag=f"{arm}_{n}",
            )
            res["wall_s"] = time.time() - t0
            res["gc_before"] = gc_before
        except Exception as e:
            res = {"error": str(e), "traceback": traceback.format_exc()}
        append_result({**key, **res, "ts": time.time()})


def main():
    run_storage_tier()


if __name__ == "__main__":
    main()
