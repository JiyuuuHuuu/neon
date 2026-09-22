#!/usr/bin/env python3
"""
One-time cluster bring-up: create the `main` tenant with the deterministic tenant
config, its root timeline, a persistent compute on node2 (lib.PROBE_NODE), seeded
with pgbench -s 5 (~75MB, per the plan's disk-budget-driven scale-down from
experiment 1's -s 50).

Assumes the services themselves (storage_broker/storage_controller/safekeeper on
node1, pageserver on node0) are already deployed and Active -- see
agent/experiment-2-cluster-plan.md's appendix and the progress log; this script only
creates the experiment's tenant/timeline/compute, not the cluster's services.

Usage:
    python3 setup_cluster.py            # create if not already done (idempotent)
    python3 setup_cluster.py --reset    # drop the state file and start over with a
                                         # brand new tenant (does NOT delete the old
                                         # tenant from the pageserver -- do that by
                                         # hand via the storage_controller API if disk
                                         # space requires it)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

STATE_FILE = lib.EXPERIMENT_DIR / "data" / "cluster_state.json"
MAIN_PORT = 55432
SEED_SCALE = 5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()

    if STATE_FILE.exists():
        print("Cluster already set up per state file:", STATE_FILE)
        print(STATE_FILE.read_text())
        return

    print("== tenant create (main) ==")
    tenant_id = lib.tenant_create()
    print("tenant_id =", tenant_id)

    print("== root timeline create ==")
    timeline_id = lib.timeline_create_root(tenant_id, pg_version=17)
    print("main_timeline_id =", timeline_id)

    print("== starting persistent compute on node2 ==")
    lib.endpoint_start(lib.PROBE_NODE, "main", tenant_id, timeline_id, port=MAIN_PORT)
    if not lib.endpoint_wait_ready(lib.PROBE_NODE, "main", port=MAIN_PORT, timeout_s=60):
        raise RuntimeError("main endpoint did not become ready")

    print(f"== seeding main with pgbench -s {SEED_SCALE} ==")
    connstr = lib.endpoint_connstr(lib.PROBE_NODE, MAIN_PORT)
    lib.pgbench_init(lib.PROBE_NODE, connstr, scale=SEED_SCALE)

    state = {
        "tenant_id": tenant_id,
        "main_timeline_id": timeline_id,
        "main_pg_port": MAIN_PORT,
        "main_node": lib.PROBE_NODE,
        "main_endpoint_id": "main",
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print("== done ==")
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
