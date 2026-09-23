"""
Drive the full grid: A0 (baseline) -> A1 (branches of main) -> A1b (sibling roots,
capped at N<=2) -> A2 (separate projects). Resumable: skips any (arm, n, rep) point
already in data/raw.jsonl.

    python3 sweep.py
"""
from __future__ import annotations

import random
import sys
import time

sys.path.insert(0, ".")
import api
import lib
import loadgen
import materialize
import measure

RAW_PATH = lib.DATA_DIR / "raw.jsonl"

REPS = 5
A1_NS = [1, 3, 6, 9]
A1B_NS = [1, 2]
A2_NS = [1, 3, 6, 9]

# Calibration found successive restarts of the *same* compute can differ by >10x in
# mean GetPage latency even at matched N (see agent/experiment-3-progress notes in
# README "Deviations") -- plausibly warm vs. cold underlying VM/pageserver-connection
# state, not something the Free-plan API exposes or lets us control. Two defenses:
# 1. Randomize (not nested-ascending) execution order within each arm's block, so a
#    monotonic drift over wall-clock time doesn't get aliased onto N.
# 2. Re-measure the N=0 baseline (tagged "A0_end") after every other arm, so drift
#    across the whole sweep is directly visible by comparing A0 vs A0_end.
_RNG = random.Random(20260922)


def already_done() -> set[tuple[str, int, int]]:
    done = set()
    if RAW_PATH.exists():
        import json
        with open(RAW_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    done.add((r["arm"], r["n"], r["rep"]))
                except (json.JSONDecodeError, KeyError):
                    continue
    return done


def check_budget(project_id: str) -> None:
    s = api.project_consumption_summary(project_id)
    if s["compute_time_seconds"] > lib.COMPUTE_TIME_BUDGET_SECONDS:
        raise RuntimeError(f"compute_time_seconds={s['compute_time_seconds']} exceeds budget "
                            f"guardrail {lib.COMPUTE_TIME_BUDGET_SECONDS} on {project_id} -- stopping")
    if s["synthetic_storage_size"] > lib.PROJECT_STORAGE_LIMIT_BYTES * 0.85:
        raise RuntimeError(f"synthetic_storage_size={s['synthetic_storage_size']} is near the "
                            f"{lib.PROJECT_STORAGE_LIMIT_BYTES}-byte project cap on {project_id} -- stopping")


def run_measurement(sec: dict, probe: dict, children: list[dict], arm: str, n: int, rep: int) -> dict:
    procs = []
    if children:
        procs = loadgen.start_all([materialize.with_password(c) for c in children])
        time.sleep(1.5)  # let load ramp up before the probe window starts
    try:
        rec = measure.run_point(sec["project_id"], sec["endpoint_id"], sec["host"],
                                 sec["database"], sec["role"], sec["password"],
                                 probe["read_blocks"], probe["write_ids"])
    finally:
        if procs:
            loadgen.wait_all(procs)
    rec["arm"] = arm
    rec["n"] = n
    rec["rep"] = rep
    lib.append_jsonl(RAW_PATH, rec)
    print(f"  [{arm} n={n} rep={rep}] getpage_p99={rec['read']['getpage_p99_ms']}"
          f"ms getpage_mean={rec['read']['getpage_mean_ms']}ms "
          f"commit_p99={rec['write']['commit_p99_ms']}ms")
    return rec


def main():
    sec = lib.load_secret("main_project")
    probe = lib.load_manifest("main_probe")
    org_id = lib.ORG_ID
    done = already_done()

    print("=== A0: baseline (main only, no children) ===")
    for rep in range(REPS):
        if ("A0", 0, rep) in done:
            continue
        run_measurement(sec, probe, [], "A0", 0, rep)
        check_budget(sec["project_id"])

    print("=== A1: branches of main ===")
    materialize.teardown_a1b(sec["project_id"])  # free root-branch budget, in case of a resumed run
    children_by_n = {n: materialize.ensure_a1(sec["project_id"], sec["branch_id"], n) for n in A1_NS}
    points = [(n, rep) for n in A1_NS for rep in range(REPS) if ("A1", n, rep) not in done]
    _RNG.shuffle(points)
    for n, rep in points:
        run_measurement(sec, probe, children_by_n[n], "A1", n, rep)
        check_budget(sec["project_id"])
    materialize.teardown_a1(sec["project_id"])

    print("=== A1b: sibling root branches (schema-only, capped at N<=2) ===")
    children_by_n = {n: materialize.ensure_a1b(sec["project_id"], n) for n in A1B_NS}
    points = [(n, rep) for n in A1B_NS for rep in range(REPS) if ("A1b", n, rep) not in done]
    _RNG.shuffle(points)
    for n, rep in points:
        run_measurement(sec, probe, children_by_n[n], "A1b", n, rep)
        check_budget(sec["project_id"])
    materialize.teardown_a1b(sec["project_id"])

    print("=== A2: separate projects (separate tenants) ===")
    children_by_n = {n: materialize.ensure_a2(org_id, n) for n in A2_NS}
    points = [(n, rep) for n in A2_NS for rep in range(REPS) if ("A2", n, rep) not in done]
    _RNG.shuffle(points)
    for n, rep in points:
        run_measurement(sec, probe, children_by_n[n], "A2", n, rep)
        check_budget(sec["project_id"])

    print("=== A0_end: re-measure baseline after everything else, for drift check ===")
    for rep in range(REPS):
        if ("A0_end", 0, rep) in done:
            continue
        run_measurement(sec, probe, [], "A0_end", 0, rep)
        check_budget(sec["project_id"])

    print("=== sweep complete ===")


if __name__ == "__main__":
    main()
