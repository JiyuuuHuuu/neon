#!/usr/bin/env python3
"""
Saturation calibration: fix a small number of branches, sweep the pagebench
client/rate count driving load against them, and report aggregate RPS vs.
pageserver CPU so we can pick a client count near the knee for the real sweep.

Run this once, interactively, before the unattended sweep. Prints a
recommendation; does not write into data/raw.jsonl.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib
import materialize
import measure

STATE_FILE = lib.EXPERIMENT_DIR / "data" / "cluster_state.json"
N_CALIB = 8
CLIENT_COUNTS = [1, 2, 4, 8, 16, 32]
RUNTIME_S = 20


def pageserver_cpu_pct(sample_s: float = 1.0) -> float:
    """Approximate pageserver process CPU% via /proc, summed over threads / 100."""
    out = subprocess.run(["pgrep", "-f", "target/release/pageserver -D"],
                          capture_output=True, text=True)
    pids = [p for p in out.stdout.split() if p.isdigit()]
    if not pids:
        return float("nan")
    pid = pids[0]

    def read_total(pid):
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().split()
        utime, stime = int(fields[13]), int(fields[14])
        return utime + stime

    hz = 100  # sysconf(_SC_CLK_TCK), standard on Linux
    t0 = read_total(pid)
    time.sleep(sample_s)
    t1 = read_total(pid)
    return (t1 - t0) / hz / sample_s * 100.0


def main():
    state = json.loads(STATE_FILE.read_text())
    tenant_id = state["tenant_id"]
    main_ttid = measure.ttid(tenant_id, state["main_timeline_id"])

    print(f"Materializing {N_CALIB} calibration branches (read-only, no endpoints)...")
    entries = materialize.materialize_branches(tenant_id, N_CALIB, with_endpoints=False,
                                                name="calib_branches")
    load_targets = [measure.ttid(e["tenant_id"], e["timeline_id"]) for e in entries]

    print(f"{'clients':>8} {'load_rps':>10} {'probe_lat_ms':>13} {'ps_cpu%':>9}")
    results = []
    for c in CLIENT_COUNTS:
        cpu_samples = []

        import threading
        stop = threading.Event()

        def sampler():
            while not stop.is_set():
                cpu_samples.append(pageserver_cpu_pct(1.0))

        th = threading.Thread(target=sampler)
        th.start()
        res = measure.measure_storage_point(
            main_ttid=main_ttid, load_targets=load_targets, num_clients=c,
            runtime_s=RUNTIME_S, probe_rate=50.0,
            keyspace_tag=f"calib_{c}",
        )
        stop.set()
        th.join()
        avg_cpu = sum(cpu_samples) / len(cpu_samples) if cpu_samples else float("nan")
        load_rps = res.get("load_rps_mean") or 0.0
        # probe is rate-limited (fixed offered rate) so its own request_count is not
        # a saturation signal; its *latency* is -- watch it climb as load_rps' marginal
        # gain (vs. added clients) tapers off.
        print(f"{c:>8} {load_rps:>10.1f} "
              f"{res.get('latency_p99_ms', float('nan')):>13.3f} {avg_cpu:>9.1f}")
        results.append({"clients": c, **res, "ps_cpu_pct": avg_cpu})

    out_path = lib.EXPERIMENT_DIR / "data" / "calibration.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {out_path}")
    print("Pick the client count where marginal RPS/added-client drops sharply,")
    print("targeting ~60-80% pageserver CPU at the largest N in the real sweep.")


if __name__ == "__main__":
    main()
