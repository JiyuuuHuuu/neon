"""
Shared helpers for the branch-interference experiment, cluster (multi-node CloudLab)
variant. See ../README.md for full rationale and ../../1-branch-interference-local/
for the single-machine predecessor this is ported from.

Design notes (deltas from the local version):
- No `neon_local` for the real cluster (it's a static hand-deployment, not something
  neon_local can drive against 6 physically separate nodes -- see agent/cloudlab.md
  and agent/experiment-2-cluster-plan.md). Tenant/timeline lifecycle goes straight to
  the storage_controller's HTTP API on node1 (10.10.1.2:1234).
- Endpoints (computes) are started by hand-invoking `compute_ctl` over SSH on a chosen
  node, from a config.json built from a template (config/compute_config_template.json,
  itself generated once from a throwaway single-node `neon_local` stack -- see the
  progress log for why: it's the only reliable way to get a config.json shaped
  correctly for *this* checkout's ComputeConfig/ComputeSpec schema, which has diverged
  from the older docker-compose reference spec).
- pagebench and pgbench run on remote nodes (they need to physically originate load
  from node2-5, not the coordinator), so every invocation goes through SSH.
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
EXPERIMENT_DIR = REPO_ROOT / "experiments" / "2-branch-interference-cluster"
CONFIG_DIR = EXPERIMENT_DIR / "config"
LOG_DIR = EXPERIMENT_DIR / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SSH_KEY = Path.home() / ".ssh" / "dassl_rsa"
SSH_USER = "JiyuHu23"

# node index -> (public hostname for SSH from the coordinator, LAN IP for in-cluster connstrings)
NODES = {
    0: ("ms0603.utah.cloudlab.us", "10.10.1.1"),
    1: ("ms0607.utah.cloudlab.us", "10.10.1.2"),
    2: ("ms0610.utah.cloudlab.us", "10.10.1.3"),
    3: ("ms0622.utah.cloudlab.us", "10.10.1.4"),
    4: ("ms0644.utah.cloudlab.us", "10.10.1.5"),
    5: ("ms0629.utah.cloudlab.us", "10.10.1.6"),
}
PAGESERVER_NODE = 0
STORCON_NODE = 1
PROBE_NODE = 2  # "main"'s compute + isolated probe client
LOAD_NODES = [3, 4, 5]

PS_HTTP_BASE = f"http://{NODES[PAGESERVER_NODE][1]}:9898"
PS_PG_CONNSTRING = f"postgres://no_user@{NODES[PAGESERVER_NODE][1]}:64000"
STORCON_BASE = f"http://{NODES[STORCON_NODE][1]}:1234"
SAFEKEEPER_CONNSTR = f"{NODES[STORCON_NODE][1]}:5454"

# Absolute, not "~/...": several call sites below (pagebench_getpage_remote/
# background, read_remote_log) build a command as a list of args and shlex.quote()
# each one individually -- shlex.quote treats '~' as unsafe and wraps it in single
# quotes, which *disables* tilde expansion, so the remote shell ends up looking for
# a file literally named "~". $HOME is consistently /users/<user> on every node here.
REMOTE_HOME = f"/users/{SSH_USER}"
REMOTE_SVC = f"{REMOTE_HOME}/svc"
REMOTE_BIN = f"{REMOTE_HOME}/neon-bin"
REMOTE_PG_BIN = f"{REMOTE_BIN}/pg_install/v17/bin"
REMOTE_LD_LIBRARY_PATH = f"{REMOTE_BIN}/pg_install/v17/lib"


def node_host(node: int) -> str:
    return NODES[node][0]


def node_ip(node: int) -> str:
    return NODES[node][1]


# --------------------------------------------------------------------------
# SSH fan-out
# --------------------------------------------------------------------------

def ssh(node: int, remote_cmd: str, timeout: Optional[float] = 60, check: bool = True,
        log_name: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run `remote_cmd` on `node` via a short-lived SSH connection (resilient to the
    long-held-session drops observed against these CloudLab nodes -- see the progress
    log). NOT for long-running/background remote processes -- use ssh_background."""
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
    """Launch remote_cmd fully detached (nohup + disown) on `node`, redirecting to
    log_path_remote there. Returns immediately; caller polls/greps the remote log or
    uses ssh_pkill to stop it. Long-running services on these nodes MUST be launched
    this way, not as a foreground SSH command held open for the process's lifetime --
    long-held SSH sessions to these nodes drop with 'Broken pipe' periodically."""
    wrapped = f"nohup bash -c {shlex.quote(remote_cmd)} > {log_path_remote} 2>&1 < /dev/null & disown; echo launched"
    return ssh(node, wrapped, timeout=30)


def ssh_pkill(node: int, pattern: str):
    """pkill -f a remote process, avoiding the classic self-match footgun: `pkill -f
    X` run as `ssh host 'pkill -f X'` matches its own cmdline (which contains the
    literal string X) and kills the SSH session's shell before pkill even signals the
    target. Wrap the pattern in a single-char bracket class so the process's own
    argv matches but this invocation's text doesn't."""
    bracketed = f"[{pattern[0]}]{pattern[1:]}"
    ssh(node, f"pkill -f {shlex.quote(bracketed)} || true", timeout=20, check=False)


def scp_to(node: int, local_path: Path, remote_path: str, timeout: float = 60):
    cmd = ["scp", "-i", str(SSH_KEY), "-o", "ConnectTimeout=15",
           str(local_path), f"{SSH_USER}@{node_host(node)}:{remote_path}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"scp to node{node} failed: {proc.stdout}\n{proc.stderr}")


# --------------------------------------------------------------------------
# HTTP-over-SSH: the coordinator is NOT on the cluster's private 10.10.1.x LAN (only
# the 6 nodes are, on enp1s0d1 -- see agent/cloudlab.md), so every call to the
# storage_controller or pageserver management API has to be curl'd from a node that
# IS on that LAN, not `requests` directly from here. Routed through STORCON_NODE
# (node1), which can reach every other node's management port over the LAN.
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


def timeline_create_root(tenant_id: str, pg_version: int = 17,
                          timeline_id: Optional[str] = None) -> str:
    """Bootstrap timeline: its own initdb, no ancestor."""
    timeline_id = timeline_id or gen_timeline_id()
    body = {"new_timeline_id": timeline_id, "pg_version": pg_version}
    curl_json(STORCON_NODE, "POST", f"{STORCON_BASE}/v1/tenant/{tenant_id}/timeline",
              json_body=body, timeout=120)
    return timeline_id


def timeline_branch(tenant_id: str, ancestor_timeline_id: str,
                     timeline_id: Optional[str] = None) -> str:
    timeline_id = timeline_id or gen_timeline_id()
    body = {"new_timeline_id": timeline_id, "ancestor_timeline_id": ancestor_timeline_id}
    curl_json(STORCON_NODE, "POST", f"{STORCON_BASE}/v1/tenant/{tenant_id}/timeline",
              json_body=body, timeout=60)
    return timeline_id


def timeline_list(tenant_id: str) -> list[dict]:
    j, _ = curl_json(STORCON_NODE, "GET", f"{STORCON_BASE}/v1/tenant/{tenant_id}/timeline",
                      timeout=30)
    return j or []


class DiskLowError(RuntimeError):
    pass


def free_gb_remote(node: int, path: str = "/mydata") -> float:
    proc = ssh(node, f"df -k --output=avail {path} | tail -1", timeout=20)
    kb = int(proc.stdout.strip())
    return kb / (1024 ** 2)


def check_disk_headroom(node: int = PAGESERVER_NODE, min_gb: float = 5.0):
    """node0's /mydata is where pageserver tenant data lives and is the tightest
    budget in this topology (37GB total). Called before creating each new
    materialized entity so a tight run stops cleanly rather than risking a
    disk-full failure mid-write."""
    g = free_gb_remote(node)
    if g < min_gb:
        raise DiskLowError(f"free space on node{node}:/mydata is {g:.1f}GB, below the "
                            f"{min_gb}GB safety floor -- stopping before creating more "
                            f"entities. Free space or lower this arm's N and resume.")


# --------------------------------------------------------------------------
# Endpoints (computes) -- hand-deployed compute_ctl over SSH
# --------------------------------------------------------------------------

_CONFIG_TEMPLATE = json.loads((CONFIG_DIR / "compute_config_template.json").read_text())


def _build_compute_config(tenant_id: str, timeline_id: str, endpoint_id: str, port: int) -> dict:
    cfg = json.loads(json.dumps(_CONFIG_TEMPLATE))  # deep copy
    spec = cfg["spec"]
    spec["tenant_id"] = tenant_id
    spec["timeline_id"] = timeline_id
    spec["endpoint_id"] = endpoint_id
    spec["cluster"]["name"] = endpoint_id
    spec["pageserver_connstring"] = PS_PG_CONNSTRING
    spec["pageserver_connection_info"]["shards"]["0000"]["pageservers"][0]["libpq_url"] = PS_PG_CONNSTRING
    spec["pageserver_connection_info"]["shards"]["0000"]["pageservers"][0]["grpc_url"] = None
    spec["safekeeper_connstrings"] = [SAFEKEEPER_CONNSTR]
    # Unset -- non-null enables the generation-gated walproposer protocol, which
    # requires the timeline to be pre-registered on the safekeeper via the storage
    # controller's timelines_onto_safekeepers flow, which this deployment doesn't use.
    # See the progress log ("bug 2") for the full diagnosis.
    spec["safekeepers_generation"] = None
    conf = spec["cluster"]["postgresql_conf"]
    conf = re.sub(r"neon\.safekeepers='[^']*'", f"neon.safekeepers='{SAFEKEEPER_CONNSTR}'", conf)
    conf = re.sub(r"listen_addresses='[^']*'", "listen_addresses='0.0.0.0'", conf)
    conf = re.sub(r"port=\d+", f"port={port}", conf)
    spec["cluster"]["postgresql_conf"] = conf
    return cfg


def endpoint_start(node: int, endpoint_id: str, tenant_id: str, timeline_id: str,
                    port: int = 55432, ext_http_port: int = 3080, int_http_port: int = 3081):
    """Deploy config + launch compute_ctl on `node`, detached. Idempotent: stops any
    existing compute_ctl for this endpoint_id first."""
    endpoint_stop(node, endpoint_id, check=False)
    cfg = _build_compute_config(tenant_id, timeline_id, endpoint_id, port)
    local_tmp = CONFIG_DIR / f"_tmp_config_{endpoint_id}.json"
    local_tmp.write_text(json.dumps(cfg, indent=2))
    remote_dir = f"~/compute-{endpoint_id}"
    remote_config = f"{remote_dir}/config.json"
    ssh(node, f"mkdir -p {remote_dir} ~/svc", timeout=20)
    scp_to(node, local_tmp, remote_config)
    local_tmp.unlink()
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
    ssh_background(node, remote_cmd, f"~/svc/compute-{endpoint_id}.log")


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
    ssh_pkill(node, f"compute-id {endpoint_id}")
    ssh(node, f"pkill -9 -f {shlex.quote('[p]gdata=.*compute-' + endpoint_id)} || true",
        timeout=20, check=False)


def endpoint_connstr(node: int, port: int, dbname: str = "postgres") -> str:
    """`node` is unused for the address itself -- every caller of this connstr runs
    pgbench/psql via SSH *on* that same node (see pgbench_init etc.), so the
    connection is always local. Using `localhost` (not the node's LAN IP) matters:
    the compute's pg_hba only trusts loopback without a password; connecting via the
    node's own LAN-facing address hits a different pg_hba rule that demands one."""
    return f"postgresql://cloud_admin@localhost:{port}/{dbname}"


# --------------------------------------------------------------------------
# Pageserver HTTP management API (direct from the coordinator -- node0's HTTP port
# is reachable from anywhere the coordinator can route to, no SSH needed for this)
# --------------------------------------------------------------------------

class PSHttp:
    """All calls routed via SSH+curl through STORCON_NODE (node1), which can reach
    the pageserver's management API over the cluster LAN -- see the HTTP-over-SSH
    note above `curl_json`."""

    def __init__(self, base_url: str = PS_HTTP_BASE):
        self.base_url = base_url

    def metrics_text(self) -> str:
        return curl_text(STORCON_NODE, f"{self.base_url}/metrics", timeout=30)

    def checkpoint(self, tenant_shard_id: str, timeline_id: str, timeout: float = 120):
        j, _ = curl_json(STORCON_NODE, "PUT",
                          f"{self.base_url}/v1/tenant/{tenant_shard_id}/timeline/{timeline_id}/checkpoint",
                          timeout=timeout)
        return j

    def compact(self, tenant_shard_id: str, timeline_id: str, timeout: float = 120):
        j, _ = curl_json(STORCON_NODE, "PUT",
                          f"{self.base_url}/v1/tenant/{tenant_shard_id}/timeline/{timeline_id}/compact",
                          timeout=timeout)
        return j

    def do_gc(self, tenant_shard_id: str, timeline_id: str, gc_horizon: int = 0,
              timeout: float = 120):
        j, _ = curl_json(STORCON_NODE, "PUT",
                          f"{self.base_url}/v1/tenant/{tenant_shard_id}/timeline/{timeline_id}/do_gc",
                          json_body={"gc_horizon": gc_horizon}, timeout=timeout)
        return j

    def timeline_status(self, tenant_shard_id: str, timeline_id: str) -> dict:
        j, _ = curl_json(STORCON_NODE, "GET",
                          f"{self.base_url}/v1/tenant/{tenant_shard_id}/timeline/{timeline_id}",
                          timeout=30)
        return j


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

def pgbench_init(node: int, connstr: str, scale: int = 5, timeout: float = 300):
    cmd = (f"env LD_LIBRARY_PATH={REMOTE_LD_LIBRARY_PATH} {REMOTE_PG_BIN}/pgbench "
           f"-i -I dtGvp -s {scale} {shlex.quote(connstr)}")
    ssh(node, cmd, timeout=timeout, log_name="pgbench_init")


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


# --------------------------------------------------------------------------
# pagebench (remote, via SSH)
# --------------------------------------------------------------------------

def _timeout_wrap(args: list[str], runtime_s: int, soft_grace: int = 240,
                   hard_grace: int = 30) -> list[str]:
    """Prefix a remote command with GNU `timeout` so it's force-killed if it outlives
    its own --runtime. Observed necessary at N=256: under severe pageserver
    contention, a pagebench probe blocked waiting on an in-flight request that never
    got serviced -- pagebench's own --runtime deadline does not abort an
    already-issued request, so the process hung indefinitely (still at 0% CPU an hour
    later) rather than exiting anywhere near --runtime. `timeout` guarantees the
    remote process (and hence this SSH call) terminates within
    runtime_s + soft_grace + hard_grace regardless -- and critically, this guarantee
    holds even if the SSH connection itself drops, since `timeout` runs entirely on
    the remote host and doesn't depend on the client staying connected.
    soft_grace defaults generously (4 min): at N=256 tail latency is itself the
    thing being measured (rep=1 in the first run legitimately took ~70s of probe
    time to finish with real p99=203ms data), so cutting the grace window too tight
    silently converts a genuine extreme-tail measurement into an empty one -- worse
    than just running long. Pass a tighter grace explicitly for calls where that
    tradeoff is wrong (e.g. short interactive/smoke-test calls)."""
    return ["timeout", "-k", f"{hard_grace}s", f"{runtime_s + soft_grace}s", *args]


def pagebench_getpage_remote(node: int, targets: list[str], num_clients: int = 1,
                              per_client_rate: Optional[float] = None,
                              runtime_s: int = 60,
                              keyspace_cache_remote: Optional[str] = None) -> dict:
    """Run pagebench get-page-latest-lsn on `node` against node0's pageserver.
    Returns {'json': {...}, 'missed': int, 'stdout', 'stderr'}."""
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
    """Parse humantime-formatted durations, which may have multiple components
    (e.g. '1ms 459us', '1s 200ms') -> total milliseconds."""
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
