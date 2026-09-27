#!/usr/bin/env python3
"""
Phase 2: create the tenant + root timeline, seed it with pgbench -i -s ROOT_SCALE,
flush, and drive it through the root compaction discipline (checkpoint -> compact ->
do_gc) so its layers are image-dominated before any branch exists. See README.md's
"Compaction discipline" section for why this ordering matters: image layers must
exist on the ROOT (so children have a clean, compact ancestor to walk to) but must
NEVER be created on a CHILD (that would flatten the ancestor chain) -- this script
is what builds the root; materialize.py flips image_creation_threshold to a huge
value before creating any branch.

Idempotent via data/cluster_state.json, same pattern as experiment 2's
setup_cluster.py. Honors PILOT=1 (see params.py).

Usage:
    python3 setup_root.py            # create if not already done
    PILOT=1 python3 setup_root.py    # pilot scale
    python3 setup_root.py --reset    # drop state and start over (does not delete the
                                      # old tenant from the pageserver)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib
import params

STATE_FILE = lib.EXPERIMENT_DIR / "data" / f"cluster_state_{params.TAG}.json"

PS = lib.PSHttp()


def compact_to_images(tenant_id: str, timeline_id: str, rounds: int = 5) -> dict:
    """Checkpoint, then run `compact` a few times (compaction is incremental and
    L0->L1->image promotion may take more than one call to fully settle at this
    data size), then do_gc to drop now-covered deltas. Returns a layer-map summary
    for the record, not a strict image-purity assertion -- at 300GB with a single
    compact cycle it is normal for some delta layers to remain below the image
    threshold; what matters is that the dominant volume is in image layers, which
    the recorded summary lets a human verify."""
    print(f"  checkpoint...", flush=True)
    PS.checkpoint(tenant_id, timeline_id, timeout=1800)
    for i in range(rounds):
        print(f"  compact round {i + 1}/{rounds} (force_repartition+force_image_layer_creation)...",
              flush=True)
        PS.compact(tenant_id, timeline_id, timeout=3600, force_l0_compaction=True,
                   force_repartition=True, force_image_layer_creation=True)
    print(f"  do_gc...", flush=True)
    gc_result = PS.do_gc(tenant_id, timeline_id, gc_horizon=0, timeout=1800)
    info = PS.layer_map_info(tenant_id, timeline_id)
    n_image = sum(1 for l in info.get("historic_layers", []) if l.get("kind") == "Image")
    n_delta = sum(1 for l in info.get("historic_layers", []) if l.get("kind") == "Delta")
    bytes_image = sum(l.get("layer_file_size", 0) for l in info.get("historic_layers", [])
                       if l.get("kind") == "Image")
    bytes_delta = sum(l.get("layer_file_size", 0) for l in info.get("historic_layers", [])
                       if l.get("kind") == "Delta")
    summary = {
        "gc_result": gc_result,
        "n_image_layers": n_image, "n_delta_layers": n_delta,
        "bytes_image": bytes_image, "bytes_delta": bytes_delta,
    }
    print(f"  layer map: {n_image} image layers ({bytes_image / 1e9:.2f} GB), "
          f"{n_delta} delta layers ({bytes_delta / 1e9:.2f} GB)", flush=True)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()

    if STATE_FILE.exists():
        print("Root already set up per state file:", STATE_FILE)
        print(STATE_FILE.read_text())
        return

    print(f"== [{params.TAG}] tenant create ==", flush=True)
    tenant_id = lib.tenant_create()
    print("tenant_id =", tenant_id)

    print("== root timeline create ==", flush=True)
    timeline_id = lib.timeline_create_root(tenant_id, pg_version=lib.PG_VERSION)
    print("root_timeline_id =", timeline_id)

    print(f"== starting root's persistent compute on node{lib.ROOT_NODE} ==", flush=True)
    lib.endpoint_start(lib.ROOT_NODE, "root", tenant_id, timeline_id, port=params.MAIN_PORT)
    if not lib.endpoint_wait_ready(lib.ROOT_NODE, "root", port=params.MAIN_PORT, timeout_s=120):
        raise RuntimeError("root endpoint did not become ready")

    print(f"== seeding root with pgbench -s {params.ROOT_SCALE} (this is the long step) ==",
          flush=True)
    connstr = lib.endpoint_connstr(lib.ROOT_NODE, params.MAIN_PORT)
    t0 = time.time()
    lib.pgbench_init(lib.ROOT_NODE, connstr, scale=params.ROOT_SCALE, timeout=6 * 3600)
    init_wall_s = time.time() - t0
    print(f"  pgbench -i done in {init_wall_s:.0f}s", flush=True)

    page_rows = lib.calibrate_page_rows(lib.ROOT_NODE, connstr)
    row_count = lib.table_row_count(lib.ROOT_NODE, connstr)
    print(f"  pgbench_accounts: {row_count} rows, ~{page_rows} rows/page", flush=True)

    print("== flush to pageserver ==", flush=True)
    lib.wait_for_last_flush_lsn(tenant_id, timeline_id, lib.ROOT_NODE,
                                 params.MAIN_PORT, timeout_s=1800)

    print("== root compaction discipline (checkpoint -> compact -> do_gc) ==", flush=True)
    t0 = time.time()
    layer_summary = compact_to_images(tenant_id, timeline_id)
    compact_wall_s = time.time() - t0

    # IMPORTANT: root_lsn (used as ancestor_start_lsn for every branch) must be read
    # AFTER do_gc, not before. gc_horizon=0 collapses the GC cutoff to the tenant's
    # current tip at the moment do_gc runs, which is normally slightly *past* the
    # pre-compaction flush LSN (background compute activity, or the compaction pass
    # itself advancing things) -- branching at that now-stale earlier LSN then fails
    # with "406 invalid branch start lsn: less than latest GC cutoff". Discovered
    # empirically during the pilot: every one of the first 8 horizontal branch
    # attempts failed this way. The tip read here is always >= the cutoff that
    # produced it, by construction, so it's always valid to branch from.
    info = lib.timeline_info(tenant_id, timeline_id)
    root_lsn = info["last_record_lsn"]
    print("root_lsn (post-GC tip) =", root_lsn)

    disk_root_node = lib.free_gb_remote(lib.PAGESERVER_NODE)
    print(f"node{lib.PAGESERVER_NODE}:/mydata free after root build: {disk_root_node:.1f} GB",
          flush=True)

    state = {
        "tag": params.TAG,
        "tenant_id": tenant_id,
        "root_timeline_id": timeline_id,
        "root_lsn": root_lsn,
        "root_port": params.MAIN_PORT,
        "root_node": lib.ROOT_NODE,
        "root_scale": params.ROOT_SCALE,
        "page_rows": page_rows,
        "row_count": row_count,
        "pgbench_init_wall_s": init_wall_s,
        "compact_wall_s": compact_wall_s,
        "layer_summary": layer_summary,
        "disk_free_gb_after_root": disk_root_node,
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print("== done ==")
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
