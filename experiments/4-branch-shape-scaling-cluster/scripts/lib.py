"""
Shared helpers for experiment 4 (branch-tree shape vs read scaling), cluster
variant. Ported from ../../2-branch-interference-cluster/scripts/lib.py -- see that
file's docstring for the general cluster-deployment rationale (no neon_local against
a real multi-node cluster, HTTP-over-SSH because the coordinator isn't on the
cluster's private LAN, long-running remote processes must be detached with
ssh_background, etc). This file keeps all of that unchanged and adds:

- NODES/roles retargeted at CloudLab cluster 2 (8x c220g5, Wisconsin) instead of
  cluster 1's 6x m400 -- see agent/cloudlab.md for the hardware writeup.
- timeline_branch() gained `ancestor_start_lsn`, needed for a reproducible vertical
  chain (experiment 2 only ever branched at the ancestor's tip).
- wait_for_last_flush_lsn(): flush a compute's WAL through to the pageserver and
  return the resulting LSN, so branch points are reproducible and don't silently
  miss just-written data (neon_local doesn't do this for you either -- see the
  explore notes on branch_timeline_impl's tip-LSN behavior).
- Static (read-only, pinned-LSN) compute config support, for Tier COMPUTE.
- tenant_config_patch(), to flip `image_creation_threshold` between "let the root
  build real image layers" (small) and "never image-compact a child" (huge) --
  see the experiment README's compaction-discipline section for why this matters:
  an image layer on a child would flatten the ancestor chain and invalidate the
  vertical arm entirely.
- divergence_write(): the page-scattered UPDATE that gives every branch its own
  real delta layers (5% of pages by default).
- has_image_layer(): reads the pageserver's layer-map-info endpoint to verify a
  child has no image layers (the disqualifying condition above).
- No `probe_rate` / open-loop-probe machinery -- this experiment has no separate
  probe target; every timeline in the sweep is measured directly.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

REPO_ROOT = Path("/mydata/jiyu/neon")
EXPERIMENT_DIR = REPO_ROOT / "experiments" / "4-branch-shape-scaling-cluster"
CONFIG_DIR = EXPERIMENT_DIR / "config"
LOG_DIR = EXPERIMENT_DIR / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SSH_KEY = Path.home() / ".ssh" / "dassl_rsa"
SSH_USER = "JiyuHu23"

# node index -> (public hostname for SSH from the coordinator, LAN IP for in-cluster
# connstrings). Verified 2026-09-23 -- re-check against agent/cloudlab.md on
# reprovision, CloudLab hostnames/IPs change.
NODES = {
    0: ("c220g5-110915.wisc.cloudlab.us", "10.10.1.1"),
    1: ("c220g5-110923.wisc.cloudlab.us", "10.10.1.2"),
    2: ("c220g5-110924.wisc.cloudlab.us", "10.10.1.3"),
    3: ("c220g5-110909.wisc.cloudlab.us", "10.10.1.4"),
    4: ("c220g5-110920.wisc.cloudlab.us", "10.10.1.5"),
    5: ("c220g5-110916.wisc.cloudlab.us", "10.10.1.6"),
    6: ("c220g5-110913.wisc.cloudlab.us", "10.10.1.7"),
    7: ("c220g5-110908.wisc.cloudlab.us", "10.10.1.8"),
}
PAGESERVER_NODE = 0
STORCON_NODE = 1
ROOT_NODE = 2                    # root's persistent compute + serial vertical chain
DIVERGENCE_NODES = [2, 3]        # horizontal-branch divergence writes, parallel
COMPUTE_TIER_NODES = [2, 3]      # Tier COMPUTE static read-only endpoints
LOAD_NODES = [4, 5, 6, 7]        # pagebench load generators (Tier STORAGE)

PS_HTTP_BASE = f"http://{NODES[PAGESERVER_NODE][1]}:9898"
PS_PG_CONNSTRING = f"postgres://no_user@{NODES[PAGESERVER_NODE][1]}:64000"
STORCON_BASE = f"http://{NODES[STORCON_NODE][1]}:1234"
SAFEKEEPER_CONNSTR = f"{NODES[STORCON_NODE][1]}:5454"

# Absolute, not "~/...": several call sites build a command as a list of args and
# shlex.quote() each one individually -- shlex.quote treats '~' as unsafe and wraps
# it in single quotes, which *disables* tilde expansion server-side. $HOME is
# verified /users/<user> on every c220g5 node (same as cluster 1's m400s).
REMOTE_HOME = f"/users/{SSH_USER}"
REMOTE_SVC = f"{REMOTE_HOME}/svc"
REMOTE_BIN = f"{REMOTE_HOME}/neon-bin"
REMOTE_PG_BIN = f"{REMOTE_BIN}/pg_install/v17/bin"
REMOTE_LD_LIBRARY_PATH = f"{REMOTE_BIN}/pg_install/v17/lib"

PG_VERSION = 17


def node_host(node: int) -> str:
    return NODES[node][0]


def node_ip(node: int) -> str:
    return NODES[node][1]


# --------------------------------------------------------------------------
# SSH fan-out (verbatim from experiment 2 -- these three gotchas each cost a real
# debugging cycle there and must survive the port unchanged)
# --------------------------------------------------------------------------

def ssh(node: int, remote_cmd: str, timeout: Optional[float] = 60, check: bool = True,
        log_name: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run `remote_cmd` on `node` via a short-lived SSH connection. NOT for
    long-running/background remote processes -- use ssh_background."""
    cmd = ["ssh", "-i", str(SSH_KEY), "-o", "ConnectTimeout=15",
           "-o", "ServerAliveInterval=20", f"{SSH_USER}@{node_host(node)}", remote_cmd]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    dt = time.time() - t0
    if log_name:
        with open(LOG_DIR / f"{log_name}.log", "a") as f:
            f.write(f"\n$ ssh node{node} {remote_cmd!r}  ({dt:.1f}s, rc={proc.returncode})\n")
            f.write(proc.stdout)
            f.write(proc.stderr)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"ssh node{node} failed (rc={proc.returncode}, {dt:.1f}s): {remote_cmd}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


def ssh_background(node: int, remote_cmd: str, log_path_remote: str):
    """Launch remote_cmd fully detached (nohup + disown) on `node`. Long-running
    processes MUST be launched this way -- long-held SSH sessions to these nodes
    drop with 'Broken pipe' periodically."""
    wrapped = f"nohup bash -c {shlex.quote(remote_cmd)} > {log_path_remote} 2>&1 < /dev/null & disown; echo launched"
    return ssh(node, wrapped, timeout=30)


def ssh_pkill(node: int, pattern: str):
    """pkill -f a remote process, avoiding the self-match footgun (ssh's own cmdline
    contains the literal pattern text): wrap in a single-char bracket class."""
    bracketed = f"[{pattern[0]}]{pattern[1:]}"
    ssh(node, f"pkill -f {shlex.quote(bracketed)} || true", timeout=20, check=False)


def scp_to(node: int, local_path: Path, remote_path: str, timeout: float = 60):
    cmd = ["scp", "-i", str(SSH_KEY), "-o", "ConnectTimeout=15",
           str(local_path), f"{SSH_USER}@{node_host(node)}:{remote_path}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"scp to node{node} failed: {proc.stdout}\n{proc.stderr}")


def rsync_to(node: int, local_path: str, remote_path: str, timeout: float = 1800,
             extra_args: Optional[list[str]] = None):
    """rsync a file or directory (trailing '/' on local_path syncs contents, matching
    normal rsync semantics) to `node`. Used for the one-time binary/pg_install
    deployment (Phase 1) -- too large for scp_to's use case."""
    cmd = ["rsync", "-az", "--progress", "-e", f"ssh -i {shlex.quote(str(SSH_KEY))} -o ConnectTimeout=15"]
    if extra_args:
        cmd += extra_args
    cmd += [local_path, f"{SSH_USER}@{node_host(node)}:{remote_path}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"rsync to node{node} failed (rc={proc.returncode}):\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")


# --------------------------------------------------------------------------
# HTTP-over-SSH: the coordinator is NOT on the cluster's private 10.10.1.x LAN, so
# every call to storage_controller/pageserver management APIs is curl'd from
# STORCON_NODE (node1), which is on the LAN and can reach every other node.
# --------------------------------------------------------------------------

def _curl_raw(node: int, method: str, url: str, json_body: Optional[dict] = None,
              timeout: float = 60) -> tuple[str, str]:
    cmd = ["curl", "-s", "-w", "\n%{http_code}", "--max-time", str(int(timeout)), "-X", method]
    if json_body is not None:
        cmd += ["-H", "content-type: application/json", "-d", json.dumps(json_body)]
    cmd.append(url)
    remote_cmd = " ".join(shlex.quote(c) for c in cmd)
    proc = ssh(node, remote_cmd, timeout=timeout + 15)
    text = proc.stdout
    idx = text.rfind("\n")
    if idx == -1:
        return text, "000"
    return text[:idx], text[idx + 1:].strip()


def curl_json(node: int, method: str, url: str, json_body: Optional[dict] = None,
              timeout: float = 60, ok_statuses=(200, 201, 409)):
    """Returns (parsed_json_or_None, status_code). Raises on an unexpected status."""
    body, status = _curl_raw(node, method, url, json_body, timeout)
    status_i = int(status) if status.isdigit() else 0
    if status_i not in ok_statuses:
        raise RuntimeError(f"{method} {url} -> HTTP {status}: {body[:2000]}")
    if not body.strip():
        return None, status_i
    return json.loads(body), status_i


def curl_text(node: int, url: str, timeout: float = 60) -> str:
    body, status = _curl_raw(node, "GET", url, timeout=timeout)
    if status != "200":
        raise RuntimeError(f"GET {url} -> HTTP {status}: {body[:500]}")
    return body


# --------------------------------------------------------------------------
# Tenant / timeline lifecycle via storage_controller HTTP
# --------------------------------------------------------------------------

def gen_tenant_id() -> str:
    return uuid.uuid4().hex


def gen_timeline_id() -> str:
    return uuid.uuid4().hex


DETERMINISM_CONFIG = {
    "gc_period": "0s",
    "compaction_period": "0s",
    "pitr_interval": "0s",
    "gc_horizon": 0,
    "checkpoint_timeout": "10years",
    "lsn_lease_length": "0s",
    "heatmap_period": "0s",
}


def tenant_create(tenant_id: Optional[str] = None, extra_config: Optional[dict] = None) -> str:
    tenant_id = tenant_id or gen_tenant_id()
    body = {"new_tenant_id": tenant_id, "placement_policy": {"Attached": 0},
            **DETERMINISM_CONFIG, **(extra_config or {})}
    curl_json(STORCON_NODE, "POST", f"{STORCON_BASE}/v1/tenant", json_body=body, timeout=60)
    return tenant_id


def tenant_config_patch(tenant_id: str, patch: dict):
    """PATCH /v1/tenant/config -- FieldPatch semantics: present key with a value sets
    it, present key with null clears back to default, absent key is left alone. Used
    to flip `image_creation_threshold` between phases (see the compaction-discipline
    section of README.md): a small value while building the root's image layers, an
    enormous one while writing branch divergence so children never get an image layer
    of their own (which would flatten the ancestor chain)."""
    body = {"tenant_id": tenant_id, **patch}
    curl_json(STORCON_NODE, "PATCH", f"{STORCON_BASE}/v1/tenant/config", json_body=body, timeout=60)


def timeline_create_root(tenant_id: str, pg_version: int = PG_VERSION,
                          timeline_id: Optional[str] = None) -> str:
    """Bootstrap timeline: its own initdb, no ancestor."""
    timeline_id = timeline_id or gen_timeline_id()
    body = {"new_timeline_id": timeline_id, "pg_version": pg_version}
    curl_json(STORCON_NODE, "POST", f"{STORCON_BASE}/v1/tenant/{tenant_id}/timeline",
              json_body=body, timeout=120)
    return timeline_id


def timeline_branch(tenant_id: str, ancestor_timeline_id: str,
                     ancestor_start_lsn: Optional[str] = None,
                     timeline_id: Optional[str] = None) -> str:
    """Create a child timeline. `ancestor_start_lsn` (a Postgres 'X/Y' hex LSN
    string, as returned by wait_for_last_flush_lsn) should almost always be passed
    explicitly here -- omitting it means 'branch at the ancestor's current tip',
    which (a) isn't reproducible across re-runs of this script and (b) triggers
    storage_controller's shard-0-first serialization path instead of parallel
    per-shard creation. Experiment 2 never had a reason to pass this; this
    experiment's vertical chain is the reason it exists now."""
    timeline_id = timeline_id or gen_timeline_id()
    body = {"new_timeline_id": timeline_id, "ancestor_timeline_id": ancestor_timeline_id}
    if ancestor_start_lsn is not None:
        body["ancestor_start_lsn"] = ancestor_start_lsn
    curl_json(STORCON_NODE, "POST", f"{STORCON_BASE}/v1/tenant/{tenant_id}/timeline",
              json_body=body, timeout=60)
    return timeline_id


def register_pageserver_node(node_id: int = 1):
    """Register the pageserver as a node with storage_controller. NOT automatic:
    the pageserver's own /upcall/v1/re-attach call (made at its startup) is for
    tenant-attachment bookkeeping only, and returns 'register: None' when the node
    isn't already known -- a real deployment's provisioning tooling is expected to
    call this once per pageserver, which is exactly what experiment 2's lost
    runbook must have done and this script now does explicitly."""
    body = {
        "node_id": node_id,
        "listen_pg_addr": node_ip(PAGESERVER_NODE), "listen_pg_port": 64000,
        "listen_grpc_addr": None, "listen_grpc_port": None,
        "listen_http_addr": node_ip(PAGESERVER_NODE), "listen_http_port": 9898,
        "listen_https_port": None,
        "availability_zone_id": "lan",
        "node_ip_addr": None,
    }
    curl_json(STORCON_NODE, "POST", f"{STORCON_BASE}/control/v1/node", json_body=body, timeout=30)


def timeline_list(tenant_id: str) -> list[dict]:
    j, _ = curl_json(STORCON_NODE, "GET", f"{STORCON_BASE}/v1/tenant/{tenant_id}/timeline",
                      timeout=30)
    return j or []


def timeline_info(tenant_id: str, timeline_id: str) -> dict:
    """Via the pageserver directly (tenant_shard_id == tenant_id, unsharded)."""
    j, _ = curl_json(STORCON_NODE, "GET",
                      f"{PS_HTTP_BASE}/v1/tenant/{tenant_id}/timeline/{timeline_id}",
                      timeout=30)
    return j


def wait_for_last_flush_lsn(tenant_id: str, timeline_id: str, node: int, port: int,
                             timeout_s: float = 120) -> str:
    """CHECKPOINT the compute, then poll the pageserver's last_record_lsn until it
    reaches (or passes) pg_current_wal_flush_lsn(). Returns the reached LSN as an
    'X/Y' string, suitable for ancestor_start_lsn. Without this, branching at the
    ancestor's tip can silently miss WAL the compute has written but the pageserver
    hasn't ingested yet -- neon_local's own branch path has the same gap; the test
    suite's wait_for_last_flush_lsn is the canonical fix and this is a port of it."""
    connstr = endpoint_connstr(node, port)
    checkpoint_and_get_lsn = (
        f"env LD_LIBRARY_PATH={REMOTE_LD_LIBRARY_PATH} {REMOTE_PG_BIN}/psql "
        f"{shlex.quote(connstr)} -Atc "
        f"\"CHECKPOINT; SELECT pg_current_wal_flush_lsn();\""
    )
    proc = ssh(node, checkpoint_and_get_lsn, timeout=60)
    target_lsn = proc.stdout.strip().splitlines()[-1].strip()

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        info = timeline_info(tenant_id, timeline_id)
        reached = info.get("last_record_lsn")
        if reached is not None and _lsn_ge(reached, target_lsn):
            return target_lsn
        time.sleep(1)
    raise TimeoutError(f"pageserver last_record_lsn did not reach {target_lsn} "
                        f"for {tenant_id}/{timeline_id} within {timeout_s}s")


def _lsn_to_int(lsn: str) -> int:
    hi, lo = lsn.split("/")
    return (int(hi, 16) << 32) | int(lo, 16)


def _lsn_ge(a: str, b: str) -> bool:
    return _lsn_to_int(a) >= _lsn_to_int(b)


class DiskLowError(RuntimeError):
    pass


def free_gb_remote(node: int, path: str = "/mydata") -> float:
    proc = ssh(node, f"df -k --output=avail {path} | tail -1", timeout=20)
    kb = int(proc.stdout.strip())
    return kb / (1024 ** 2)


def check_disk_headroom(node: int = PAGESERVER_NODE, min_gb: float = 150.0):
    """node0's /mydata (1.4TB) holds every materialized timeline's layers. 150GB
    floor per the plan's disk-budget discussion (concern #1): leaves real headroom
    below the projected worst case rather than the 5GB floor exp 2 used on a 37GB
    disk, which would leave no margin at all at this scale."""
    g = free_gb_remote(node)
    if g < min_gb:
        raise DiskLowError(f"free space on node{node}:/mydata is {g:.1f}GB, below the "
                            f"{min_gb}GB safety floor -- stopping before creating more "
                            f"entities. Free space or lower this run's N and resume.")


# --------------------------------------------------------------------------
# Endpoints (computes) -- hand-deployed compute_ctl over SSH
# --------------------------------------------------------------------------

_CONFIG_TEMPLATE = json.loads((CONFIG_DIR / "compute_config_template.json").read_text())


def _build_compute_config(tenant_id: str, timeline_id: str, endpoint_id: str, port: int,
                           static_lsn: Optional[str] = None) -> dict:
    """static_lsn=None -> Primary (read-write) compute, as in experiment 2.
    static_lsn='X/Y' -> read-only compute pinned at that LSN (ComputeMode::Static):
    empty safekeeper_connstrings, `mode: {"Static": lsn}`, and
    `recovery_target_lsn='X/Y'` injected into postgresql.conf (mirroring
    compute_ctl's own config.rs generation for this mode -- see the explore notes)."""
    cfg = json.loads(json.dumps(_CONFIG_TEMPLATE))  # deep copy
    spec = cfg["spec"]
    spec["tenant_id"] = tenant_id
    spec["timeline_id"] = timeline_id
    spec["endpoint_id"] = endpoint_id
    spec["cluster"]["name"] = endpoint_id
    spec["pageserver_connstring"] = PS_PG_CONNSTRING
    spec["pageserver_connection_info"]["shards"]["0000"]["pageservers"][0]["libpq_url"] = PS_PG_CONNSTRING
    spec["pageserver_connection_info"]["shards"]["0000"]["pageservers"][0]["grpc_url"] = None
    # Unset -- non-null enables the generation-gated walproposer protocol, which
    # requires the timeline to be pre-registered on the safekeeper via storage
    # controller's timelines_onto_safekeepers flow, which this deployment doesn't use.
    spec["safekeepers_generation"] = None

    conf = spec["cluster"]["postgresql_conf"]
    conf = re.sub(r"listen_addresses='[^']*'", "listen_addresses='0.0.0.0'", conf)
    conf = re.sub(r"port=\d+", f"port={port}", conf)

    if static_lsn is None:
        spec["mode"] = "Primary"
        spec["safekeeper_connstrings"] = [SAFEKEEPER_CONNSTR]
        conf = re.sub(r"neon\.safekeepers='[^']*'", f"neon.safekeepers='{SAFEKEEPER_CONNSTR}'", conf)
    else:
        spec["mode"] = {"Static": static_lsn}
        spec["safekeeper_connstrings"] = []
        conf = re.sub(r"neon\.safekeepers='[^']*'\n?", "", conf)
        conf += f"hot_standby=on\nrecovery_target_lsn='{static_lsn}'\n"

    spec["cluster"]["postgresql_conf"] = conf
    return cfg


def endpoint_start(node: int, endpoint_id: str, tenant_id: str, timeline_id: str,
                    port: int = 55432, ext_http_port: int = 3080, int_http_port: int = 3081,
                    static_lsn: Optional[str] = None):
    """Deploy config + launch compute_ctl on `node`, detached. Idempotent: stops any
    existing compute_ctl for this endpoint_id first."""
    endpoint_stop(node, endpoint_id, check=False)
    cfg = _build_compute_config(tenant_id, timeline_id, endpoint_id, port, static_lsn=static_lsn)
    local_tmp = CONFIG_DIR / f"_tmp_config_{endpoint_id}.json"
    local_tmp.write_text(json.dumps(cfg, indent=2))
    remote_dir = f"{REMOTE_HOME}/compute-{endpoint_id}"
    remote_config = f"{remote_dir}/config.json"
    ssh(node, f"mkdir -p {remote_dir} {REMOTE_SVC}", timeout=20)
    scp_to(node, local_tmp, remote_config)
    local_tmp.unlink()
    # compute_ctl needs LD_LIBRARY_PATH in its OWN process environment, not merely
    # baked into the postgres binary's rpath, or neon.so fails to load with
    # "libpq.so.5: cannot open shared object file" -- see agent/CLAUDE.md.
    remote_cmd = (
        f"rm -rf {remote_dir}/pgdata && "
        f"env LD_LIBRARY_PATH={REMOTE_LD_LIBRARY_PATH} {REMOTE_BIN}/compute_ctl "
        f"--external-http-port {ext_http_port} --internal-http-port {int_http_port} "
        f"--pgdata {remote_dir}/pgdata "
        f"--connstr postgres://cloud_admin@localhost:{port}/postgres "
        f"--config {remote_config} "
        f"--pgbin {REMOTE_PG_BIN}/postgres "
        f"--compute-id {endpoint_id} --dev"
    )
    ssh_background(node, remote_cmd, f"{REMOTE_SVC}/compute-{endpoint_id}.log")


def endpoint_wait_ready(node: int, endpoint_id: str, port: int = 55432,
                         timeout_s: float = 60) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        proc = ssh(node,
                   f"env LD_LIBRARY_PATH={REMOTE_LD_LIBRARY_PATH} {REMOTE_PG_BIN}/pg_isready "
                   f"-h localhost -p {port} -q",
                   timeout=15, check=False)
        if proc.returncode == 0:
            return True
        time.sleep(2)
    return False


def endpoint_stop(node: int, endpoint_id: str, check: bool = False):
    """Trailing anchors on both patterns matter: with up to 64 numerically-suffixed
    endpoint IDs (ct-0..ct-63, h/v branch endpoints, etc.), an unanchored pkill -f
    'compute-id ct-1' would also match 'compute-id ct-10'..'ct-19' (pkill -f does
    unanchored regex/substring matching against the whole cmdline) -- experiment 2
    never hit this because it only ever ran one endpoint_id at a time. The trailing
    space / '/' anchors the match to the exact ID."""
    ssh_pkill(node, f"compute-id {endpoint_id} ")
    ssh(node, f"pkill -9 -f {shlex.quote('[p]gdata=.*compute-' + endpoint_id + '/')} || true",
        timeout=20, check=False)


def endpoint_connstr(node: int, port: int, dbname: str = "postgres") -> str:
    """`node` is unused for the address itself -- every caller runs pgbench/psql via
    SSH *on* that same node, so the connection is always local (localhost, not the
    node's LAN IP -- pg_hba only trusts loopback without a password)."""
    return f"postgresql://cloud_admin@localhost:{port}/{dbname}"


# --------------------------------------------------------------------------
# Pageserver HTTP management API (routed via STORCON_NODE, same as above)
# --------------------------------------------------------------------------

class PSHttp:
    def __init__(self, base_url: str = PS_HTTP_BASE):
        self.base_url = base_url

    def metrics_text(self) -> str:
        return curl_text(STORCON_NODE, f"{self.base_url}/metrics", timeout=30)

    def checkpoint(self, tenant_shard_id: str, timeline_id: str, timeout: float = 300):
        j, _ = curl_json(STORCON_NODE, "PUT",
                          f"{self.base_url}/v1/tenant/{tenant_shard_id}/timeline/{timeline_id}/checkpoint",
                          timeout=timeout)
        return j

    def compact(self, tenant_shard_id: str, timeline_id: str, timeout: float = 600,
                force_l0_compaction: bool = False, force_repartition: bool = False,
                force_image_layer_creation: bool = False):
        """A bare PUT .../compact does NOT create image layers even when
        image_creation_threshold is exceeded: image-layer creation only happens
        after repartitioning, which -- like everything else -- is normally
        timer-driven and never fires here since compaction_period=0s. Without
        force_repartition=true&force_image_layer_creation=true a manual compact call
        silently does L0->L1 only, forever (discovered empirically: 5 rounds of the
        bare call on the root produced 39 delta layers and exactly 0 image layers).
        setup_root.py's root-build pass must set both; materialize.py's per-branch
        calls after every divergence write must NOT (that's what keeps children
        delta-only -- see README's compaction-discipline section)."""
        params_q = []
        if force_l0_compaction:
            params_q.append("force_l0_compaction=true")
        if force_repartition:
            params_q.append("force_repartition=true")
        if force_image_layer_creation:
            params_q.append("force_image_layer_creation=true")
        qs = ("?" + "&".join(params_q)) if params_q else ""
        j, _ = curl_json(STORCON_NODE, "PUT",
                          f"{self.base_url}/v1/tenant/{tenant_shard_id}/timeline/{timeline_id}/compact{qs}",
                          timeout=timeout)
        return j

    def do_gc(self, tenant_shard_id: str, timeline_id: str, gc_horizon: int = 0,
              timeout: float = 300):
        j, _ = curl_json(STORCON_NODE, "PUT",
                          f"{self.base_url}/v1/tenant/{tenant_shard_id}/timeline/{timeline_id}/do_gc",
                          json_body={"gc_horizon": gc_horizon}, timeout=timeout)
        return j

    def timeline_status(self, tenant_shard_id: str, timeline_id: str) -> dict:
        j, _ = curl_json(STORCON_NODE, "GET",
                          f"{self.base_url}/v1/tenant/{tenant_shard_id}/timeline/{timeline_id}",
                          timeout=30)
        return j

    def layer_map_info(self, tenant_shard_id: str, timeline_id: str) -> dict:
        j, _ = curl_json(STORCON_NODE, "GET",
                          f"{self.base_url}/v1/tenant/{tenant_shard_id}/timeline/{timeline_id}/layer",
                          timeout=60)
        return j or {}

    def keyspace(self, tenant_shard_id: str, timeline_id: str) -> dict:
        j, _ = curl_json(STORCON_NODE, "GET",
                          f"{self.base_url}/v1/tenant/{tenant_shard_id}/timeline/{timeline_id}/keyspace",
                          timeout=120)
        return j or {}


def has_image_layer(ps: "PSHttp", tenant_id: str, timeline_id: str) -> bool:
    """True if this timeline's own layer map contains an Image-kind layer. Used as a
    hard check after branch materialization: if a child ever got an image layer, a
    background/manual compaction flattened the ancestor chain for the keys it
    covers, invalidating the depth measurement for this branch (see the
    compaction-discipline section of README.md)."""
    info = ps.layer_map_info(tenant_id, timeline_id)
    for layer in info.get("historic_layers", []):
        if layer.get("kind") == "Image":
            return True
    return False


def diskstats_now(node: int) -> dict:
    """Snapshot /proc/diskstats -> {device: (io_ticks_ms, wall_time_s)}. io_ticks_ms
    is field 13 (1-indexed) per kernel Documentation/iostats.txt -- 'milliseconds
    spent doing I/Os'; comparing its delta to the wall-clock delta between two
    snapshots gives the standard iostat %util formula. Used purely as a disk-bound
    sanity signal (see README.md verification: is the sweep disk-bound or not),
    not a precise per-device attribution -- /mydata's LVM backing (sda4+sdb, see
    agent/cloudlab.md) means the real bottleneck device could be either physical
    disk or the dm-* logical volume; analyze.py takes the max across all of them."""
    proc = ssh(node, "cat /proc/diskstats", timeout=15)
    now = time.time()
    out = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 13:
            continue
        name = parts[2]
        if re.match(r"^(loop|ram)", name):
            continue
        io_ticks_ms = float(parts[12])
        out[name] = io_ticks_ms
    return {"ts": now, "devices": out}


_METRIC_LINE_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9eE+.\-nafNAFI]+)\s*$')


def parse_prometheus(text: str) -> list[tuple[str, dict, float]]:
    out = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _METRIC_LINE_RE.match(line)
        if not m:
            continue
        name, labelstr, valstr = m.groups()
        labels = {}
        if labelstr:
            for kv in re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', labelstr):
                labels[kv[0]] = kv[1]
        try:
            val = float(valstr)
        except ValueError:
            continue
        out.append((name, labels, val))
    return out


def metric_sum(parsed, name: str, label_filter: Optional[dict] = None) -> float:
    total = 0.0
    for n, labels, val in parsed:
        if n != name:
            continue
        if label_filter and any(labels.get(k) != v for k, v in label_filter.items()):
            continue
        total += val
    return total


# --------------------------------------------------------------------------
# pgbench (remote, via SSH)
# --------------------------------------------------------------------------

def pgbench_init(node: int, connstr: str, scale: int, timeout: float = 21600):
    """Builds the pgbench_accounts primary key with a direct SQL statement instead
    of pgbench's own `-I p` step. Root-caused after two confusing failures: `-I p`
    is NOT idempotent and NOT scoped to one table -- it (re)creates primary keys on
    ALL FOUR standard pgbench tables every time it's invoked, in a fixed order. The
    first (interrupted) run of this pilot got far enough to create
    pgbench_branches'/pgbench_tellers' tiny primary keys (near-instant, 1 and 10
    rows/scale respectively) before being cut off, but never reached
    pgbench_accounts' (100,000 rows/scale, the slow one). Every subsequent `-I p`
    retry then failed immediately with 'multiple primary keys for table
    "pgbench_branches" are not allowed' -- before ever attempting the one index this
    experiment actually needs. A direct, idempotent
    `CREATE UNIQUE INDEX IF NOT EXISTS` sidesteps the ordering/idempotency issue
    entirely and only touches the one table this experiment reads from."""
    cmd = (f"env LD_LIBRARY_PATH={REMOTE_LD_LIBRARY_PATH} {REMOTE_PG_BIN}/pgbench "
           f"-i -I dtGv -s {scale} {shlex.quote(connstr)}")
    ssh(node, cmd, timeout=timeout, log_name="pgbench_init")
    sql = ("CREATE UNIQUE INDEX IF NOT EXISTS pgbench_accounts_pkey "
           "ON pgbench_accounts USING btree (aid)")
    cmd_p = (f"env LD_LIBRARY_PATH={REMOTE_LD_LIBRARY_PATH} {REMOTE_PG_BIN}/psql "
             f"{shlex.quote(connstr)} -c {shlex.quote(sql)}")
    ssh(node, cmd_p, timeout=timeout, log_name="pgbench_init_pkey")
    verify_pgbench_pkey_exists(node, connstr)


def verify_pgbench_pkey_exists(node: int, connstr: str):
    """`ssh()`'s default check=True means pgbench_init already raises on a nonzero
    exit code from a normal, uninterrupted run -- but this experiment's pilot run
    got its *local* ssh() call killed by the calling harness's own tool timeout
    while the primary-key-build step was still in flight (the detached remote
    pgbench kept running and reached completion on its own, unobserved). The result
    was a table with NO index on `aid`, silently turning every divergence UPDATE's
    plan into a full-table parallel seq scan + hash join -- cost scaling with total
    table size instead of divergence_fraction, which cost real wall-clock time to
    diagnose. This check makes that failure mode loud instead of silent, regardless
    of why the index might be missing."""
    sql = "SELECT 1 FROM pg_indexes WHERE tablename='pgbench_accounts' AND indexname='pgbench_accounts_pkey'"
    cmd = (f"env LD_LIBRARY_PATH={REMOTE_LD_LIBRARY_PATH} {REMOTE_PG_BIN}/psql "
           f"{shlex.quote(connstr)} -Atc {shlex.quote(sql)}")
    proc = ssh(node, cmd, timeout=30)
    if proc.stdout.strip() != "1":
        raise RuntimeError(
            "pgbench_accounts_pkey is missing after pgbench -i -- every subsequent "
            "divergence UPDATE would silently plan as a full-table seq scan instead "
            "of an index lookup. Run `pgbench -i -I p -s <scale> <connstr>` to build "
            "just the missing index, then retry."
        )


def pgbench_run_foreground(node: int, connstr: str, mode: str, clients: int,
                            duration_s: int) -> dict:
    flags = "-S" if mode == "ro" else ("-N" if mode == "wo" else "")
    cmd = (f"env LD_LIBRARY_PATH={REMOTE_LD_LIBRARY_PATH} {REMOTE_PG_BIN}/pgbench "
           f"-c {clients} -j {min(clients, 8)} -T {duration_s} {flags} {shlex.quote(connstr)}")
    proc = ssh(node, cmd, timeout=duration_s + 60, log_name="pgbench_probe")
    return parse_pgbench_stdout(proc.stdout)


_PGBENCH_LATENCY_RE = re.compile(r"latency average\s*=\s*([\d.]+)\s*ms")
_PGBENCH_STDDEV_RE = re.compile(r"latency stddev\s*=\s*([\d.]+)\s*ms")
_PGBENCH_TPS_RE = re.compile(r"tps = ([\d.]+)")


def parse_pgbench_stdout(stdout: str) -> dict:
    out = {}
    m = _PGBENCH_LATENCY_RE.search(stdout)
    if m:
        out["latency_avg_ms"] = float(m.group(1))
    m = _PGBENCH_STDDEV_RE.search(stdout)
    if m:
        out["latency_stddev_ms"] = float(m.group(1))
    tps_matches = _PGBENCH_TPS_RE.findall(stdout)
    if tps_matches:
        out["tps"] = float(tps_matches[0])
    return out


def divergence_write(node: int, connstr: str, page_rows: int, n_pages: int, slot: int,
                      n_slots: int = 20, timeout: float = 1800) -> int:
    """Update one row per page across n_pages scattered pages (deterministic offset
    `slot`, wrapping across n_slots so consecutive branches touch disjoint pages).
    page_rows is pgbench_accounts' measured rows-per-page (see calibrate_page_rows).
    Scattered + index-driven (not a sequential scan) so this stays affordable at
    300GB scale and so the branch's delta layers spread across the whole keyspace
    rather than clustering in one physical region. Returns rows touched."""
    sql = (
        f"UPDATE pgbench_accounts SET abalance = abalance + 1 "
        f"WHERE aid IN (SELECT (g * {n_slots} + {slot}) * {page_rows} + 1 "
        f"FROM generate_series(0, {n_pages - 1}) g);"
    )
    cmd = (f"env LD_LIBRARY_PATH={REMOTE_LD_LIBRARY_PATH} {REMOTE_PG_BIN}/psql "
           f"{shlex.quote(connstr)} -Atc {shlex.quote(sql)}")
    ssh(node, cmd, timeout=timeout, log_name="divergence_write")
    return n_pages


def calibrate_page_rows(node: int, connstr: str) -> int:
    """Rows per 8KiB page for pgbench_accounts on this scale, from Postgres' own
    relpages estimate (accurate immediately after pgbench -i's ANALYZE, which
    `-I dtGvp` runs)."""
    sql = ("SELECT ceil(reltuples / greatest(relpages, 1))::bigint "
           "FROM pg_class WHERE relname = 'pgbench_accounts';")
    cmd = (f"env LD_LIBRARY_PATH={REMOTE_LD_LIBRARY_PATH} {REMOTE_PG_BIN}/psql "
           f"{shlex.quote(connstr)} -Atc {shlex.quote(sql)}")
    proc = ssh(node, cmd, timeout=30)
    return int(proc.stdout.strip())


def table_row_count(node: int, connstr: str, table: str = "pgbench_accounts") -> int:
    cmd = (f"env LD_LIBRARY_PATH={REMOTE_LD_LIBRARY_PATH} {REMOTE_PG_BIN}/psql "
           f"{shlex.quote(connstr)} -Atc {shlex.quote(f'SELECT count(*) FROM {table};')}")
    proc = ssh(node, cmd, timeout=1800)
    return int(proc.stdout.strip())


# --------------------------------------------------------------------------
# pagebench (remote, via SSH)
# --------------------------------------------------------------------------

def _timeout_wrap(args: list[str], runtime_s: int, soft_grace: int = 240,
                   hard_grace: int = 30) -> list[str]:
    """Prefix a remote command with GNU `timeout`. Kept verbatim from experiment 2 --
    this is the direct fix for the N=256 indefinite-hang failure mode documented
    there (a pagebench probe can block forever on an in-flight request under severe
    pageserver contention; its own --runtime deadline does not abort a request
    already issued)."""
    return ["timeout", "-k", f"{hard_grace}s", f"{runtime_s + soft_grace}s", *args]


def pagebench_getpage_remote(node: int, targets: list[str], num_clients: int = 1,
                              per_client_rate: Optional[float] = None,
                              runtime_s: int = 60,
                              keyspace_cache_remote: Optional[str] = None) -> dict:
    """Run pagebench get-page-latest-lsn on `node` against node0's pageserver,
    blocking until it completes (bounded by _timeout_wrap)."""
    args = [f"{REMOTE_BIN}/pagebench", "get-page-latest-lsn",
            "--mgmt-api-endpoint", PS_HTTP_BASE,
            "--page-service-connstring", PS_PG_CONNSTRING,
            "--num-clients", str(num_clients),
            "--runtime", f"{runtime_s}s"]
    if per_client_rate is not None:
        args += ["--per-client-rate", str(int(round(per_client_rate)))]
    if keyspace_cache_remote is not None:
        args += ["--keyspace-cache", keyspace_cache_remote]
    args += targets
    args = _timeout_wrap(args, runtime_s)
    cmd = " ".join(shlex.quote(a) for a in args)
    proc = ssh(node, cmd, timeout=runtime_s + 300, check=False, log_name="pagebench")
    result = {"stdout": proc.stdout, "stderr": proc.stderr}
    try:
        result["json"] = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result["json"] = None
    missed = 0
    for m in re.finditer(r"MISSED:\s*(\d+)", proc.stderr):
        missed += int(m.group(1))
    result["missed"] = missed
    return result


def pagebench_getpage_background(node: int, targets: list[str], num_clients: int,
                                  runtime_s: int, log_name: str,
                                  keyspace_cache_remote: Optional[str] = None) -> str:
    """Launch a load-generating pagebench process detached on `node`. Returns the
    remote log path (poll/grep it, then ssh_pkill(node, 'pagebench') to stop)."""
    remote_log = f"{REMOTE_SVC}/{log_name}.log"
    args = [f"{REMOTE_BIN}/pagebench", "get-page-latest-lsn",
            "--mgmt-api-endpoint", PS_HTTP_BASE,
            "--page-service-connstring", PS_PG_CONNSTRING,
            "--num-clients", str(num_clients),
            "--runtime", f"{runtime_s}s"]
    if keyspace_cache_remote is not None:
        args += ["--keyspace-cache", keyspace_cache_remote]
    args += targets
    args = _timeout_wrap(args, runtime_s)
    cmd = " ".join(shlex.quote(a) for a in args)
    ssh(node, f"mkdir -p {REMOTE_SVC}", timeout=20)
    ssh_background(node, cmd, remote_log)
    return remote_log


def read_remote_log(node: int, remote_path: str) -> str:
    proc = ssh(node, f"cat {shlex.quote(remote_path)} 2>/dev/null || true", timeout=20, check=False)
    return proc.stdout


_HUMANTIME_COMPONENT_RE = re.compile(r"([\d.]+)\s*(ns|us|µs|ms|s)")


def humantime_to_ms(s: str) -> float:
    s = s.strip()
    matches = _HUMANTIME_COMPONENT_RE.findall(s)
    if not matches:
        return float("nan")
    factor = {"ns": 1e-6, "us": 1e-3, "µs": 1e-3, "ms": 1, "s": 1e3}
    total = 0.0
    for val, unit in matches:
        total += float(val) * factor[unit]
    return total


def pagebench_summary(result: dict) -> dict:
    j = result.get("json")
    if not j:
        return {"missed": result.get("missed", 0), "request_count": 0}
    total = j.get("total", {})
    out = {
        "request_count": total.get("request_count", 0),
        "latency_mean_ms": humantime_to_ms(total["latency_mean"]) if "latency_mean" in total else None,
        "missed": result.get("missed", 0),
    }
    for p, v in (total.get("latency_percentiles") or {}).items():
        out[f"latency_{p}_ms"] = humantime_to_ms(v)
    return out
