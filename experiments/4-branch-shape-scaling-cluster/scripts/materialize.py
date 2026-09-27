"""
Phase 3: build the two branch trees off the root.

HORIZONTAL (arm "h"): N branches, all forked directly from the root at root_lsn,
each independently diverged. Fanned out across a small pool of (node, port) compute
slots on lib.DIVERGENCE_NODES so branch population isn't fully serial.

VERTICAL (arm "v"): a chain root -> v1 -> v2 -> ... -> vN, each level branched from
the previous level's post-divergence LSN. Strictly serial by construction (level
k+1 cannot exist until level k has been written and flushed) -- runs on
lib.ROOT_NODE.

Both write a resumable JSON manifest after every entry
(data/manifests/{shape}_{tag}.json), so a crash mid-run costs at most one branch,
not the whole tree. See README.md's "Compaction discipline" section for why
`image_creation_threshold` must be pushed huge (via lib.tenant_config_patch) before
any of this runs -- an image layer on a child flattens the ancestor chain for the
keys it covers, invalidating that branch for the depth measurement.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib
import params

MANIFEST_DIR = lib.EXPERIMENT_DIR / "data" / "manifests"
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

# huge => compaction never promotes a child's deltas to an image layer.
NEVER_IMAGE_THRESHOLD = 1_000_000_000

PS = lib.PSHttp()

SLOTS_PER_NODE = 4  # horizontal divergence parallelism: len(DIVERGENCE_NODES) * this


def manifest_path(name: str) -> Path:
    return MANIFEST_DIR / f"{name}_{params.TAG}.json"


def load_manifest(name: str) -> list[dict]:
    p = manifest_path(name)
    if p.exists():
        return json.loads(p.read_text())
    return []


def _save_manifest(name: str, entries: list[dict]):
    manifest_path(name).write_text(json.dumps(entries, indent=2))


def n_pages_for_divergence(row_count: int, page_rows: int) -> int:
    total_pages = max(1, row_count // page_rows)
    return max(1, int(total_pages * params.DIVERGENCE_FRACTION))


def _populate_branch(node: int, port: int, endpoint_id: str, tenant_id: str,
                      timeline_id: str, page_rows: int, n_pages: int, slot: int) -> str:
    """Start a transient Primary compute on (node, port), write divergence, flush,
    stop. Returns the post-divergence LSN."""
    lib.endpoint_start(node, endpoint_id, tenant_id, timeline_id, port=port)
    if not lib.endpoint_wait_ready(node, endpoint_id, port=port, timeout_s=120):
        raise RuntimeError(f"endpoint {endpoint_id} on node{node} did not become ready")
    connstr = lib.endpoint_connstr(node, port)
    lib.divergence_write(node, connstr, page_rows=page_rows, n_pages=n_pages, slot=slot,
                          n_slots=params.DIVERGENCE_N_SLOTS)
    branch_lsn = lib.wait_for_last_flush_lsn(tenant_id, timeline_id, node, port, timeout_s=600)
    lib.endpoint_stop(node, endpoint_id)
    return branch_lsn


def _compact_and_check(tenant_id: str, timeline_id: str) -> bool:
    """checkpoint + compact (L0->L1 only, given NEVER_IMAGE_THRESHOLD is in effect);
    returns True if the timeline is clean (no image layer of its own)."""
    PS.checkpoint(tenant_id, timeline_id, timeout=600)
    PS.compact(tenant_id, timeline_id, timeout=1200)
    return not lib.has_image_layer(PS, tenant_id, timeline_id)


def materialize_horizontal(tenant_id: str, root_timeline_id: str, root_lsn: str,
                            n_max: int, page_rows: int, row_count: int, name: str) -> list[dict]:
    entries = load_manifest(name)
    start = len(entries)
    if start >= n_max:
        return entries

    n_pages = n_pages_for_divergence(row_count, page_rows)
    slots = [(node, params.DIVERGENCE_PORT + i)
             for node in lib.DIVERGENCE_NODES for i in range(SLOTS_PER_NODE)]
    slot_q: queue.Queue = queue.Queue()
    for s in slots:
        slot_q.put(s)

    lock = threading.Lock()
    errors: list[Exception] = []

    def worker(idx: int):
        node, port = slot_q.get()
        try:
            lib.check_disk_headroom(min_gb=params.DISK_FLOOR_GB)
            print(f"[{name}] h{idx} ({idx + 1}/{n_max}) on node{node}:{port}", flush=True)
            timeline_id = lib.timeline_branch(tenant_id, root_timeline_id,
                                               ancestor_start_lsn=root_lsn)
            endpoint_id = f"div-n{node}-p{port}"
            branch_lsn = _populate_branch(node, port, endpoint_id, tenant_id, timeline_id,
                                           page_rows, n_pages, slot=idx % params.DIVERGENCE_N_SLOTS)
            clean = _compact_and_check(tenant_id, timeline_id)
            entry = {"idx": idx, "depth": 1, "tenant_id": tenant_id, "timeline_id": timeline_id,
                     "ancestor_timeline_id": root_timeline_id, "branch_lsn": branch_lsn,
                     "pages_touched": n_pages, "node": node, "clean_no_image_layer": clean}
            with lock:
                entries.append(entry)
                entries.sort(key=lambda e: e["idx"])
                _save_manifest(name, entries)
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(e)
            print(f"[{name}] h{idx} FAILED: {e}", flush=True)
        finally:
            slot_q.put((node, port))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(start, n_max)]
    # Bound live threads to the slot count so we never oversubscribe computes.
    for batch_start in range(0, len(threads), len(slots)):
        batch = threads[batch_start:batch_start + len(slots)]
        for t in batch:
            t.start()
        for t in batch:
            t.join()
        if errors:
            raise errors[0]
    return entries


def materialize_vertical(tenant_id: str, root_timeline_id: str, root_lsn: str,
                          n_max: int, page_rows: int, row_count: int, name: str) -> list[dict]:
    entries = load_manifest(name)
    start = len(entries)
    n_pages = n_pages_for_divergence(row_count, page_rows)

    parent_timeline_id = entries[-1]["timeline_id"] if entries else root_timeline_id
    parent_lsn = entries[-1]["branch_lsn"] if entries else root_lsn

    for idx in range(start, n_max):
        lib.check_disk_headroom(min_gb=params.DISK_FLOOR_GB)
        print(f"[{name}] v{idx} depth={idx + 1} ({idx + 1}/{n_max})", flush=True)
        timeline_id = lib.timeline_branch(tenant_id, parent_timeline_id,
                                           ancestor_start_lsn=parent_lsn)
        endpoint_id = f"div-n{lib.ROOT_NODE}-vchain"
        branch_lsn = _populate_branch(lib.ROOT_NODE, params.DIVERGENCE_PORT, endpoint_id,
                                       tenant_id, timeline_id, page_rows, n_pages,
                                       slot=idx % params.DIVERGENCE_N_SLOTS)
        clean = _compact_and_check(tenant_id, timeline_id)
        entry = {"idx": idx, "depth": idx + 1, "tenant_id": tenant_id, "timeline_id": timeline_id,
                 "ancestor_timeline_id": parent_timeline_id, "branch_lsn": branch_lsn,
                 "pages_touched": n_pages, "node": lib.ROOT_NODE, "clean_no_image_layer": clean}
        entries.append(entry)
        _save_manifest(name, entries)
        parent_timeline_id, parent_lsn = timeline_id, branch_lsn
    return entries


def prepare_for_branching(tenant_id: str):
    """Flip image_creation_threshold to an enormous value tenant-wide before any
    branch is created. Must be called exactly once before materialize_horizontal /
    materialize_vertical, and must NOT be undone until measurement is complete."""
    lib.tenant_config_patch(tenant_id, {"image_creation_threshold": NEVER_IMAGE_THRESHOLD})
