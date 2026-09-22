#!/usr/bin/env python3
"""
One-time cluster bring-up: init, start, create the default tenant with the
deterministic tenant config, create+start the `main` endpoint, seed it with
pgbench -s 50 (~750MB).

Usage:
    python3 setup_cluster.py            # fresh bring-up
    python3 setup_cluster.py --reset    # stop + wipe NEON_REPO_DIR first
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

CONFIG_PATH = lib.EXPERIMENT_DIR / "config" / "bench.conf"
DEFAULT_TENANT_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
STATE_FILE = lib.EXPERIMENT_DIR / "data" / "cluster_state.json"

TENANT_CONFIG = {
    "gc_period": "0s",
    "compaction_period": "0s",
    "pitr_interval": "0s",
    "gc_horizon": "0",
    "checkpoint_timeout": "10years",
}


def main():
    global STATE_FILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--repo-dir", default=None,
                    help="Override NEON_REPO_DIR, e.g. to bring up a second cluster on "
                         "a different filesystem (Tier ENDPOINT on root storage).")
    ap.add_argument("--state-file", default=None,
                    help="Override the cluster_state.json path to match --repo-dir.")
    args = ap.parse_args()
    if args.repo_dir:
        lib.NEON_REPO_DIR = Path(args.repo_dir)
    if args.state_file:
        STATE_FILE = Path(args.state_file)

    if args.reset and lib.NEON_REPO_DIR.exists():
        print(f"Stopping cluster and removing {lib.NEON_REPO_DIR}")
        lib.cluster_stop()
        shutil.rmtree(lib.NEON_REPO_DIR, ignore_errors=True)
        if STATE_FILE.exists():
            STATE_FILE.unlink()

    if STATE_FILE.exists():
        print("Cluster already set up per state file:", STATE_FILE)
        print(STATE_FILE.read_text())
        return

    print("== init ==")
    lib.cluster_init(CONFIG_PATH)
    print("== start ==")
    lib.cluster_start()

    print("== tenant create (default) ==")
    tenant_id = lib.tenant_create(tenant_id=DEFAULT_TENANT_ID, set_default=True,
                                   extra_config=TENANT_CONFIG)
    print("tenant_id =", tenant_id)

    print("== endpoint create/start: main ==")
    lib.neon_local("endpoint", "create", "main", "--branch-name", "main",
                   "--tenant-id", tenant_id)
    lib.endpoint_start("main")

    eps = lib.endpoint_list(tenant_id)
    main_pg_port = int(eps["main"]["address"].rsplit(":", 1)[1])
    print("main pg_port =", main_pg_port)

    print("== seeding main with pgbench -s 50 (~750MB) ==")
    lib.pgbench_init(lib.endpoint_connstr(main_pg_port), scale=50)

    # main's timeline id, needed for the pagebench probe target and GC protocol
    tl_list = lib.neon_local("timeline", "list", "--tenant-id", tenant_id).stdout
    main_timeline_id = None
    for line in tl_list.splitlines():
        if "main" in line:
            # format: "(L) main [<timeline_id>]" or "(L) <indent>main [<id>]"
            import re
            m = re.search(r"\[([0-9a-f]{32})\]", line)
            if m:
                main_timeline_id = m.group(1)
                break
    if not main_timeline_id:
        raise RuntimeError(f"could not find main timeline id in:\n{tl_list}")
    print("main_timeline_id =", main_timeline_id)

    state = {
        "tenant_id": tenant_id,
        "main_timeline_id": main_timeline_id,
        "main_pg_port": main_pg_port,
        "main_endpoint_id": "main",
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print("== done ==")
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
