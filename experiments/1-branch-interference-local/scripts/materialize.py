"""
Build the N-sized target sets for each arm, with resumable JSON manifests.

Arms:
  A1  - N branches of `main` in the *same tenant* as main. Free to create (metadata
        only) and inherit main's ~750MB pgbench dataset via copy-on-write, so no
        seeding endpoint is needed unless the arm is used for write load.
  A1b - N sibling *root* timelines in the same tenant (no ancestor). Each starts
        blank and needs its own `pgbench -i` through a transient endpoint to reach
        comparable data volume.
  A2  - N separate tenants, each with its own initial timeline ("main" alias scoped
        to that tenant id). Same seeding requirement as A1b.

`with_endpoints=True` leaves a persistent endpoint running per target (needed for
write load / Tier ENDPOINT); `with_endpoints=False` seeds (if needed) then stops the
endpoint, leaving only pageserver-level state (cheap, needed for Tier STORAGE at
large N).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

MANIFEST_DIR = lib.EXPERIMENT_DIR / "data" / "manifests"
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

SEED_SCALE = 50  # ~750MB pgbench dataset, matches main
# Deliberately BELOW the Linux ephemeral port range (32768-60999 on this machine,
# /proc/sys/net/ipv4/ip_local_port_range). A range inside it -- 56000+ was the
# original choice -- intermittently collides with the OS handing out one of "our"
# ports as an ephemeral *source* port for an unrelated outbound connection (observed:
# a pageserver->safekeeper connection transiently owned port 56176, racing a new
# endpoint's bind() on that same port and failing with "Address already in use").
# 20000-30000 is clear of both privileged ports and the ephemeral range, and clear of
# every fixed port this cluster uses (broker 50051, pageserver 9898/64000, safekeeper
# 5454/7676, storage controller 1234/1235, main endpoint 55432+).
BASE_PORT = 20000
PORT_STRIDE = 4
# Disjoint 1000-port block per manifest, so different arms' endpoints -- which, in
# Tier ENDPOINT, all stay alive *simultaneously* (unlike Tier STORAGE, where each
# entity's endpoint is stopped right after seeding) -- never collide on the same
# port. A1_endpoint's b0 and A1b_endpoint's s0 both computing "index 0 -> port
# 20000" was an actual bug this fixes (both endpoints came up "running" on the same
# port, and the second one starting failed with "duplicate primary endpoint").
# 1000 ports/block = 250 entries at stride 4, comfortably above any arm's N_max.
_MANIFEST_PORT_BLOCK = {
    "A1_storage": 0, "A1b_storage": 1, "A2_storage": 2,
    "A1_endpoint": 3, "A1b_endpoint": 4, "A2_endpoint": 5,
    "calib_branches": 6,
}


def _block_for_name(name: str) -> int:
    if name in _MANIFEST_PORT_BLOCK:
        return _MANIFEST_PORT_BLOCK[name]
    # stable fallback for any future/ad-hoc manifest name, kept out of the known
    # blocks' range
    return 7 + (hash(name) % 2)


def _load_manifest(name: str) -> list[dict]:
    p = MANIFEST_DIR / f"{name}.json"
    if p.exists():
        return json.loads(p.read_text())
    return []


def _save_manifest(name: str, entries: list[dict]):
    (MANIFEST_DIR / f"{name}.json").write_text(json.dumps(entries, indent=2))


def _port_for_index(i: int, name: str) -> tuple[int, int, int]:
    base = BASE_PORT + _block_for_name(name) * 1000 + i * PORT_STRIDE
    return base, base + 1, base + 2


def materialize_branches(main_tenant_id: str, n_max: int, with_endpoints: bool,
                          name: str) -> list[dict]:
    entries = _load_manifest(name)
    start = len(entries)
    for i in range(start, n_max):
        branch = f"b{i}"
        print(f"[{name}] creating branch {branch} ({i + 1}/{n_max})", flush=True)
        timeline_id = lib.get_or_create_branch(branch, main_tenant_id,
                                                ancestor_branch_name="main")
        entry = {"idx": i, "tenant_id": main_tenant_id, "timeline_id": timeline_id,
                  "branch_name": branch, "endpoint_id": None, "pg_port": None}
        if with_endpoints:
            ep = f"ep-{branch}"
            pg_port, ext_http, int_http = _port_for_index(i, name)
            lib.ensure_endpoint_created(ep, branch, pg_port, ext_http, int_http,
                                         tenant_id=main_tenant_id)
            lib.endpoint_start(ep)
            entry.update(endpoint_id=ep, pg_port=pg_port)
            # branch inherits main's pgbench_* tables via COW -- no -i needed.
        entries.append(entry)
        _save_manifest(name, entries)
    return entries


def materialize_siblings(main_tenant_id: str, n_max: int, with_endpoints: bool,
                          name: str) -> list[dict]:
    entries = _load_manifest(name)
    start = len(entries)
    for i in range(start, n_max):
        lib.check_disk_headroom()
        branch = f"s{i}"
        print(f"[{name}] creating sibling {branch} ({i + 1}/{n_max})", flush=True)
        timeline_id = lib.get_or_create_branch(branch, main_tenant_id, ancestor_branch_name=None)
        ep = f"ep-{branch}"
        pg_port, ext_http, int_http = _port_for_index(i, name)
        lib.ensure_endpoint_created(ep, branch, pg_port, ext_http, int_http,
                                     tenant_id=main_tenant_id)
        lib.endpoint_start(ep)
        lib.pgbench_init(lib.endpoint_connstr(pg_port), scale=SEED_SCALE)
        entry = {"idx": i, "tenant_id": main_tenant_id, "timeline_id": timeline_id,
                  "branch_name": branch, "endpoint_id": ep, "pg_port": pg_port}
        if not with_endpoints:
            lib.endpoint_stop(ep)
            entry["endpoint_id"] = None
            entry["pg_port"] = None
        entries.append(entry)
        _save_manifest(name, entries)
    return entries


def materialize_tenants(n_max: int, with_endpoints: bool, name: str) -> list[dict]:
    entries = _load_manifest(name)
    start = len(entries)
    for i in range(start, n_max):
        lib.check_disk_headroom()
        print(f"[{name}] creating tenant t{i} ({i + 1}/{n_max})", flush=True)
        tenant_id = lib.gen_tenant_id()
        lib.tenant_create(tenant_id=tenant_id,
                           extra_config={"gc_period": "0s", "compaction_period": "0s",
                                         "pitr_interval": "0s", "gc_horizon": "0",
                                         "checkpoint_timeout": "10years"})
        ep = f"ep-t{i}"
        pg_port, ext_http, int_http = _port_for_index(i, name)
        lib.ensure_endpoint_created(ep, "main", pg_port, ext_http, int_http, tenant_id=tenant_id)
        lib.endpoint_start(ep)
        lib.pgbench_init(lib.endpoint_connstr(pg_port), scale=SEED_SCALE)
        entry = {"idx": i, "tenant_id": tenant_id, "timeline_id": None,
                  "branch_name": "main", "endpoint_id": ep, "pg_port": pg_port}
        if not with_endpoints:
            lib.endpoint_stop(ep)
            entry["endpoint_id"] = None
            entry["pg_port"] = None
        entries.append(entry)
        _save_manifest(name, entries)
    return entries


def load_manifest(name: str) -> list[dict]:
    return _load_manifest(name)
