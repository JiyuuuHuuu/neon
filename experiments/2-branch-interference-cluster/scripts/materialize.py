"""
Build the N-sized target sets for each arm, with resumable JSON manifests. Cluster
variant of ../../1-branch-interference-local/scripts/materialize.py -- see that
file's docstring for the arm definitions (A1/A1b/A2), which are unchanged.

Read-tier (Phase 3) only ever needs `with_endpoints=False`: each A1b sibling / A2
tenant needs a *transient* compute just long enough to run `pgbench -i` and seed
data, then it's stopped -- storage-tier measurement itself talks to the pageserver
directly via pagebench, no compute involved. That lets materialization seed
everything sequentially through a single reused endpoint slot on PROBE_NODE (node2),
rather than the original's simultaneous-N-endpoints port-block bookkeeping (which
was only needed for Tier ENDPOINT, out of scope for Phase 3 -- see the plan's
"Scope: Read tier first" decision).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

MANIFEST_DIR = lib.EXPERIMENT_DIR / "data" / "manifests"
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

SEED_SCALE = 5  # ~75MB pgbench dataset -- shrunk from experiment 1's 50 (~750MB) per
# the plan's disk-budget decision (node0's /mydata is 37GB total, shared by every
# materialized entity's pageserver-side layers).
SEED_PORT = 55433  # distinct from main's persistent compute (55432), sequential reuse


def _load_manifest(name: str) -> list[dict]:
    p = MANIFEST_DIR / f"{name}.json"
    if p.exists():
        return json.loads(p.read_text())
    return []


def _save_manifest(name: str, entries: list[dict]):
    (MANIFEST_DIR / f"{name}.json").write_text(json.dumps(entries, indent=2))


def load_manifest(name: str) -> list[dict]:
    return _load_manifest(name)


def materialize_branches(main_tenant_id: str, main_timeline_id: str, n_max: int,
                          name: str) -> list[dict]:
    """A1: branches of main. Free to create (metadata only), inherit main's dataset
    via copy-on-write -- no seeding needed."""
    entries = _load_manifest(name)
    start = len(entries)
    for i in range(start, n_max):
        print(f"[{name}] creating branch b{i} ({i + 1}/{n_max})", flush=True)
        timeline_id = lib.timeline_branch(main_tenant_id, main_timeline_id)
        entries.append({"idx": i, "tenant_id": main_tenant_id, "timeline_id": timeline_id,
                         "branch_name": f"b{i}"})
        _save_manifest(name, entries)
    return entries


def _seed_via_transient_endpoint(tenant_id: str, timeline_id: str, endpoint_id: str):
    lib.endpoint_start(lib.PROBE_NODE, endpoint_id, tenant_id, timeline_id, port=SEED_PORT)
    if not lib.endpoint_wait_ready(lib.PROBE_NODE, endpoint_id, port=SEED_PORT, timeout_s=60):
        raise RuntimeError(f"endpoint {endpoint_id} did not become ready for seeding")
    connstr = lib.endpoint_connstr(lib.PROBE_NODE, SEED_PORT)
    lib.pgbench_init(lib.PROBE_NODE, connstr, scale=SEED_SCALE)
    lib.endpoint_stop(lib.PROBE_NODE, endpoint_id)


def materialize_siblings(main_tenant_id: str, n_max: int, name: str) -> list[dict]:
    """A1b: N sibling root timelines in the same tenant, each independently seeded."""
    entries = _load_manifest(name)
    start = len(entries)
    for i in range(start, n_max):
        lib.check_disk_headroom()
        print(f"[{name}] creating sibling s{i} ({i + 1}/{n_max})", flush=True)
        timeline_id = lib.timeline_create_root(main_tenant_id)
        ep = f"ep-{name}-s{i}"
        _seed_via_transient_endpoint(main_tenant_id, timeline_id, ep)
        entries.append({"idx": i, "tenant_id": main_tenant_id, "timeline_id": timeline_id,
                         "branch_name": f"s{i}"})
        _save_manifest(name, entries)
    return entries


def materialize_tenants(n_max: int, name: str) -> list[dict]:
    """A2: N separate tenants, each with its own initial (root) timeline."""
    entries = _load_manifest(name)
    start = len(entries)
    for i in range(start, n_max):
        lib.check_disk_headroom()
        print(f"[{name}] creating tenant t{i} ({i + 1}/{n_max})", flush=True)
        tenant_id = lib.tenant_create()
        timeline_id = lib.timeline_create_root(tenant_id)
        ep = f"ep-{name}-t{i}"
        _seed_via_transient_endpoint(tenant_id, timeline_id, ep)
        entries.append({"idx": i, "tenant_id": tenant_id, "timeline_id": timeline_id,
                         "branch_name": "main"})
        _save_manifest(name, entries)
    return entries
