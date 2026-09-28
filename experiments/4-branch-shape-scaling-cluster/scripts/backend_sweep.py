#!/usr/bin/env python3
"""
Warm horizontal Tier STORAGE sweep against whichever remote-storage backend node0's
pageserver is currently configured with (see storage_backend.py switch). Same protocol
as sweep.run_storage_tier: same prod tenant/manifest, 4 clients/branch, 30s windows.

    python3 backend_sweep.py minio                    # grid 1..32 x 3 reps
    python3 backend_sweep.py localfs --session drift --reps 1 --grid 32
"""
from __future__ import annotations

import argparse
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
import storage_backend

RAW_PATH = lib.EXPERIMENT_DIR / "data" / "raw_backend_prod.jsonl"
GRID = [1, 2, 4, 8, 16, 32]


def done_keys() -> list[dict]:
    if not RAW_PATH.exists():
        return []
    out = []
    for line in RAW_PATH.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("backend", choices=lib.BACKENDS)
    ap.add_argument("--session", default="main")
    ap.add_argument("--reps", type=int, default=params.REPS)
    ap.add_argument("--grid", type=int, nargs="+", default=GRID)
    ap.add_argument("--no-warmup", action="store_true")
    args = ap.parse_args()

    if params.PILOT:
        raise SystemExit("run without PILOT=1: this sweep reuses the prod tenant")
    actual = storage_backend.current_backend()
    if actual != args.backend:
        raise SystemExit(f"pageserver is on {actual}, not {args.backend}; run storage_backend.py switch")

    manifest = materialize.load_manifest("horizontal")
    if len(manifest) < max(args.grid):
        raise SystemExit(f"horizontal manifest has only {len(manifest)} branches")

    if not args.no_warmup:
        print("=== warmup N=32 (untimed) ===", flush=True)
        w = measure.measure_storage_point(manifest[:max(args.grid)], tag=f"{args.backend}_warmup",
                                          num_clients=params.STORAGE_NUM_CLIENTS_PER_BRANCH,
                                          runtime_s=params.STORAGE_RUNTIME_S)
        print("  warmup health:", w["health_problems"], flush=True)

    existing = done_keys()
    for rep in range(args.reps):
        for n in args.grid:
            key = {"tier": "storage", "shape": "horizontal", "backend": args.backend,
                   "session": args.session, "n": n, "rep": rep}
            if any(all(r.get(k) == v for k, v in key.items()) and "error" not in r for r in existing):
                print(f"skip (done): {key}")
                continue
            print(f"=== {args.backend} horizontal N={n} rep={rep} ===", flush=True)
            try:
                t0 = time.time()
                res = measure.measure_storage_point(
                    manifest[:n], tag=f"{args.backend}_{args.session}_h_{n}_{rep}",
                    num_clients=params.STORAGE_NUM_CLIENTS_PER_BRANCH,
                    runtime_s=params.STORAGE_RUNTIME_S)
                res["wall_s"] = time.time() - t0
            except Exception as e:  # noqa: BLE001
                res = {"error": str(e), "traceback": traceback.format_exc()}
            with open(RAW_PATH, "a") as f:
                f.write(json.dumps({**key, **res, "ts": time.time()}) + "\n")
            if res.get("health_problems"):
                print("  HEALTH:", res["health_problems"], flush=True)


if __name__ == "__main__":
    main()
