#!/usr/bin/env python3
"""
Full sweep orchestrator. Resumable: materialization manifests and the raw results
JSONL are both checked before doing work, so a restart skips completed work.

Usage:
    python3 sweep.py storage   # Tier STORAGE only
    python3 sweep.py endpoint  # Tier ENDPOINT only
    python3 sweep.py all       # both, storage first
"""
from __future__ import annotations

import argparse
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

STORAGE_N_GRID = {
    "A1": [0, 1, 4, 16, 64, 128, 256],
    "A1b": [0, 1, 4, 16, 64],
    "A2": [0, 1, 4, 16, 64],
}
STORAGE_REPS = 5
STORAGE_N_MAX = {"A1": 256, "A1b": 64, "A2": 64}

ENDPOINT_N_GRID = {"A1": [0, 2, 8, 32, 64], "A1b": [0, 2, 8, 32, 64], "A2": [0, 2, 8, 32, 64]}
ENDPOINT_REPS = 3
ENDPOINT_N_MAX = 64

# Calibrated separately via calibrate.py; conservative default if not yet calibrated.
LOAD_CLIENTS_PER_TARGET_STORAGE = 4
PROBE_RATE = 50.0
STORAGE_RUNTIME_S = 60
ENDPOINT_LOAD_CLIENTS = 2  # was 4 -- halved, see disk-budget note on ENDPOINT_RUNTIME_S
# Was 30. With gc_period=0s/compaction_period=0s and manual GC/compaction observed
# to be *net-negative* for disk (compact() writes new layers before do_gc() removes
# old ones, and removal is evidently incomplete -- likely local_fs remote storage
# retaining historical copies, a documented limitation), write volume from real
# pgbench load across up to 64 endpoints per point is the actual disk constraint
# for this tier. Cut 3x (compounding with LOAD_CLIENTS above to ~6x total) after the
# first 7 points consumed ~14GB/point average and disk fell from 188GB to 58GB free.
ENDPOINT_RUNTIME_S = 10


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


def get_or_materialize(arm: str, tier: str, n_max: int, with_endpoints: bool,
                        tenant_id: str) -> list[dict]:
    name = f"{arm}_{tier}"
    if arm == "A1":
        return materialize.materialize_branches(tenant_id, n_max, with_endpoints, name)
    elif arm == "A1b":
        return materialize.materialize_siblings(tenant_id, n_max, with_endpoints, name)
    elif arm == "A2":
        return materialize.materialize_tenants(n_max, with_endpoints, name)
    raise ValueError(arm)


def run_storage_tier():
    state = json.loads(STATE_FILE.read_text())
    tenant_id = state["tenant_id"]
    main_timeline_id = state["main_timeline_id"]
    main_ttid = measure.ttid(tenant_id, main_timeline_id)

    for arm, n_max in STORAGE_N_MAX.items():
        print(f"=== materializing STORAGE/{arm} up to N={n_max} ===", flush=True)
        get_or_materialize(arm, "storage", n_max, with_endpoints=False, tenant_id=tenant_id)

    points = []
    for arm, grid in STORAGE_N_GRID.items():
        for n in grid:
            for rep in range(STORAGE_REPS):
                points.append((arm, n, rep))
    random.Random("branch-interference-storage").shuffle(points)

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
                num_clients=LOAD_CLIENTS_PER_TARGET_STORAGE, runtime_s=STORAGE_RUNTIME_S,
                probe_rate=PROBE_RATE, keyspace_tag=f"{arm}_{n}",
            )
            res["wall_s"] = time.time() - t0
            res["gc_before"] = gc_before
        except Exception as e:
            res = {"error": str(e), "traceback": traceback.format_exc()}
        append_result({**key, **res, "ts": time.time()})

    # exist-vs-loaded cell: at N_max=256 (A1), load only 4 of the 256 existing branches
    key = {"tier": "storage_existvsload", "arm": "A1", "n": 4, "rep": 0}
    if not already_done(key):
        print("=== exist-vs-loaded: 256 exist, 4 loaded ===", flush=True)
        entries = materialize.load_manifest("A1_storage")[:4]
        load_targets = [measure.ttid(e["tenant_id"], e["timeline_id"]) for e in entries]
        try:
            res = measure.measure_storage_point(
                main_ttid=main_ttid, load_targets=load_targets,
                num_clients=LOAD_CLIENTS_PER_TARGET_STORAGE, runtime_s=STORAGE_RUNTIME_S,
                probe_rate=PROBE_RATE, keyspace_tag="existvsload_256exist_4load",
            )
        except Exception as e:
            res = {"error": str(e), "traceback": traceback.format_exc()}
        append_result({**key, **res, "ts": time.time(), "exist_n": 256})


def run_endpoint_tier():
    state = json.loads(STATE_FILE.read_text())
    tenant_id = state["tenant_id"]
    main_timeline_id = state["main_timeline_id"]
    main_ttid = measure.ttid(tenant_id, main_timeline_id)
    main_connstr = lib.endpoint_connstr(state["main_pg_port"])

    for arm in ENDPOINT_N_GRID:
        print(f"=== materializing ENDPOINT/{arm} up to N={ENDPOINT_N_MAX} ===", flush=True)
        get_or_materialize(arm, "endpoint", ENDPOINT_N_MAX, with_endpoints=True,
                            tenant_id=tenant_id)

    points = []
    for arm, grid in ENDPOINT_N_GRID.items():
        for n in grid:
            for rep in range(ENDPOINT_REPS):
                points.append((arm, n, rep))
    random.Random("branch-interference-endpoint").shuffle(points)

    for arm, n, rep in points:
        key = {"tier": "endpoint", "arm": arm, "n": n, "rep": rep}
        if already_done(key):
            print(f"skip (done): {key}")
            continue

        # Real write load with gc_period=0s/compaction_period=0s never gets cleaned up
        # on its own; without an explicit stop here a disk-full failure mid-write
        # could corrupt cluster state, so stop cleanly (data already collected is
        # safe; resume after freeing space or lowering N) rather than risk that.
        free = lib.free_gb()
        if free < 40.0:
            print(f"free space is {free:.1f}GB, below the 40GB safety floor -- "
                  f"stopping before {key}. Free space and resume.", flush=True)
            break

        print(f"=== ENDPOINT {arm} N={n} rep={rep} ===", flush=True)
        gc_before = measure.gc_protocol(tenant_id, main_timeline_id)

        entries = materialize.load_manifest(f"{arm}_endpoint")[:n]
        load_connstrs = [lib.endpoint_connstr(e["pg_port"]) for e in entries if e["pg_port"]]

        try:
            res = measure.measure_endpoint_point(
                main_connstr=main_connstr, load_connstrs=load_connstrs,
                load_clients=ENDPOINT_LOAD_CLIENTS, runtime_s=ENDPOINT_RUNTIME_S,
                main_ttid=main_ttid, probe_rate=PROBE_RATE,
            )
            res["gc_before"] = gc_before
        except Exception as e:
            res = {"error": str(e), "traceback": traceback.format_exc()}
        append_result({**key, **res, "ts": time.time()})
        # (Tried GC'ing the load entities here too, to reclaim the write-driven
        # growth; measured net *negative* on disk -- compact() writes new layers
        # before do_gc() removes old ones, and removal was evidently incomplete --
        # so relying on the runtime/client cuts above plus the safety floor instead.)
        print(f"    free space now: {lib.free_gb():.1f}GB", flush=True)


def main():
    global STATE_FILE
    ap = argparse.ArgumentParser()
    ap.add_argument("tier", choices=["storage", "endpoint", "all"])
    ap.add_argument("--repo-dir", default=None,
                    help="Override NEON_REPO_DIR (e.g. to run Tier ENDPOINT against a "
                         "separate cluster on a roomier filesystem). Requires "
                         "--state-file too, since the two clusters' tenant/timeline "
                         "ids differ.")
    ap.add_argument("--state-file", default=None,
                    help="Override the cluster_state.json path to match --repo-dir.")
    args = ap.parse_args()
    if args.repo_dir:
        lib.NEON_REPO_DIR = Path(args.repo_dir)
    if args.state_file:
        STATE_FILE = Path(args.state_file)
    if args.tier in ("storage", "all"):
        run_storage_tier()
    if args.tier in ("endpoint", "all"):
        run_endpoint_tier()


if __name__ == "__main__":
    main()
