"""
Shared helpers for the branch-interference experiment.

Design notes (see ../README.md for full rationale):
- Cluster entities (tenants, timelines, endpoints) are created via the `neon_local` CLI.
  Every invocation serializes on an exclusive flock over the repo dir
  (control_plane/src/bin/neon_local.rs), so setup is sequential by construction. That
  is a wall-clock cost, not a correctness problem, and this experiment runs unattended,
  so we accept it rather than hand-rolling the pageserver/storage-controller wire
  protocol to bypass it.
- Manual checkpoint/compact/gc and metrics scraping go straight to the pageserver's
  HTTP management API (no flock involved there).
- pgbench and pagebench are invoked as subprocesses against the already-built
  pg_install/ and target/release/ artifacts.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

REPO_ROOT = Path("/mydata/jiyu/neon")
EXPERIMENT_DIR = REPO_ROOT / "experiments" / "1-branch-interference-local"
NEON_REPO_DIR = Path("/lake1/jiyu/neon-bench")

NEON_LOCAL_BIN = REPO_ROOT / "target" / "release" / "neon_local"
PAGEBENCH_BIN = REPO_ROOT / "target" / "release" / "pagebench"
PG_BIN_DIR = REPO_ROOT / "pg_install" / "v17" / "bin"
PGBENCH_BIN = PG_BIN_DIR / "pgbench"
PSQL_BIN = PG_BIN_DIR / "psql"

PS_HTTP_BASE = "http://127.0.0.1:9898"
PS_PG_CONNSTRING = "postgres://cloud_admin@127.0.0.1:64000"

LOG_DIR = EXPERIMENT_DIR / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _env():
    env = os.environ.copy()
    env["NEON_REPO_DIR"] = str(NEON_REPO_DIR)
    env["LD_LIBRARY_PATH"] = str(PG_BIN_DIR.parent / "lib") + ":" + env.get("LD_LIBRARY_PATH", "")
    return env


def run(cmd: list[str], timeout: Optional[float] = None, check: bool = True,
        log_name: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a command, capturing stdout/stderr, optionally teeing to a log file."""
    t0 = time.time()
    proc = subprocess.run(
        cmd, cwd=REPO_ROOT, env=_env(), capture_output=True, text=True, timeout=timeout,
    )
    dt = time.time() - t0
    if log_name:
        with open(LOG_DIR / f"{log_name}.log", "a") as f:
            f.write(f"\n$ {shlex.join(cmd)}  ({dt:.1f}s, rc={proc.returncode})\n")
            f.write(proc.stdout)
            f.write(proc.stderr)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed (rc={proc.returncode}, {dt:.1f}s): {shlex.join(cmd)}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


def neon_local(*args, timeout: float = 180, check: bool = True) -> subprocess.CompletedProcess:
    cmd = [str(NEON_LOCAL_BIN), *[str(a) for a in args]]
    return run(cmd, timeout=timeout, check=check, log_name="neon_local")


# --------------------------------------------------------------------------
# Cluster lifecycle
# --------------------------------------------------------------------------

def cluster_init(config_path: Path):
    NEON_REPO_DIR.parent.mkdir(parents=True, exist_ok=True)
    neon_local("init", "--config", str(config_path), "--force", "empty-dir-ok", timeout=60)


def cluster_start(start_timeout: str = "60s"):
    neon_local("start", "--start-timeout", start_timeout, timeout=120)


def cluster_stop(mode: str = "immediate"):
    neon_local("stop", "--mode", mode, timeout=60, check=False)


def tenant_create(tenant_id: Optional[str] = None, pg_version: int = 17,
                   set_default: bool = False,
                   extra_config: Optional[dict] = None) -> str:
    args = ["tenant", "create", "--pg-version", str(pg_version)]
    if tenant_id:
        args += ["--tenant-id", tenant_id]
    if set_default:
        args.append("--set-default")
    for k, v in (extra_config or {}).items():
        args += ["-c", f"{k}:{v}"]
    proc = neon_local(*args)
    # neon_local prints: "tenant <hex> successfully created on the pageserver"
    m = re.search(r"tenant ([0-9a-f]{32}) successfully created", proc.stdout)
    if m:
        return m.group(1)
    if tenant_id:
        return tenant_id
    raise RuntimeError(f"could not parse tenant id from: {proc.stdout}")


def timeline_branch(branch_name: str, ancestor_branch_name: str = "main",
                     tenant_id: Optional[str] = None) -> str:
    args = ["timeline", "branch", "--branch-name", branch_name,
            "--ancestor-branch-name", ancestor_branch_name]
    if tenant_id:
        args += ["--tenant-id", tenant_id]
    proc = neon_local(*args)
    m = re.search(r"Created timeline '([0-9a-f]{32})'", proc.stdout)
    if not m:
        raise RuntimeError(f"could not parse timeline id from: {proc.stdout}")
    return m.group(1)


def timeline_create(branch_name: str, pg_version: int = 17,
                     tenant_id: Optional[str] = None) -> str:
    """Blank/bootstrap timeline: its own initdb, no ancestor."""
    args = ["timeline", "create", "--branch-name", branch_name, "--pg-version", str(pg_version)]
    if tenant_id:
        args += ["--tenant-id", tenant_id]
    proc = neon_local(*args)
    m = re.search(r"Created timeline '([0-9a-f]{32})'", proc.stdout)
    if not m:
        raise RuntimeError(f"could not parse timeline id from: {proc.stdout}")
    return m.group(1)


def endpoint_create(endpoint_id: str, branch_name: str, pg_port: int,
                     ext_http_port: int, int_http_port: int,
                     tenant_id: Optional[str] = None, pg_version: int = 17):
    args = ["endpoint", "create", endpoint_id, "--branch-name", branch_name,
            "--pg-port", str(pg_port),
            "--external-http-port", str(ext_http_port),
            "--internal-http-port", str(int_http_port),
            "--pg-version", str(pg_version)]
    if tenant_id:
        args += ["--tenant-id", tenant_id]
    neon_local(*args, timeout=60)


def endpoint_start(endpoint_id: str, start_timeout: str = "120s", retries: int = 3):
    """Retries on transient 'Address already in use' -- observed when a just-stopped
    endpoint's socket is still draining (TIME_WAIT) at the moment the next endpoint
    on a reused port (see materialize.py's port allocation) tries to bind it."""
    last_exc = None
    for attempt in range(retries):
        try:
            neon_local("endpoint", "start", endpoint_id, "-t", start_timeout, timeout=150)
            return
        except RuntimeError as e:
            last_exc = e
            if "Address already in use" not in str(e):
                raise
            print(f"endpoint_start({endpoint_id}): port race, retrying "
                  f"({attempt + 1}/{retries}) after backoff", flush=True)
            time.sleep(5 * (attempt + 1))
    raise last_exc


def endpoint_stop(endpoint_id: str, destroy: bool = False, check: bool = False):
    args = ["endpoint", "stop", endpoint_id, "--mode", "immediate"]
    if destroy:
        args.append("--destroy")
    neon_local(*args, timeout=60, check=check)


def endpoint_connstr(pg_port: int, dbname: str = "postgres") -> str:
    return f"postgresql://cloud_admin@127.0.0.1:{pg_port}/{dbname}"


def endpoint_list(tenant_id: Optional[str] = None) -> dict[str, dict]:
    """Parse `neon_local endpoint list` table -> {endpoint_id: {address, timeline, branch, lsn, status}}."""
    args = ["endpoint", "list"]
    if tenant_id:
        args += ["--tenant-id", tenant_id]
    proc = neon_local(*args)
    out = {}
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    if len(lines) < 2:
        return out
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        endpoint_id, address, timeline, branch, lsn, status = parts[:6]
        out[endpoint_id] = {"address": address, "timeline": timeline,
                             "branch": branch, "lsn": lsn, "status": status}
    return out


def gen_tenant_id() -> str:
    return uuid.uuid4().hex


class DiskLowError(RuntimeError):
    pass


def free_gb(path: Optional[Path] = None) -> float:
    # NEON_REPO_DIR resolved at call time (not as a bound default) since callers may
    # reassign the module-level lib.NEON_REPO_DIR to point Tier ENDPOINT at a
    # different filesystem after this module has already been imported.
    p = path if path is not None else NEON_REPO_DIR
    st = os.statvfs(p if p.exists() else p.parent)
    return st.f_bavail * st.f_frsize / (1024 ** 3)


def check_disk_headroom(min_gb: float = 40.0):
    """Materialization (independently-seeded siblings/tenants) is what actually
    consumes disk at scale -- each is an independent ~750MB pgbench dataset. Called
    before creating each new one so a tight run stops cleanly (raise, resumable later)
    rather than risking a disk-full failure mid-write to the pageserver/safekeeper."""
    g = free_gb()
    if g < min_gb:
        raise DiskLowError(f"free space on {NEON_REPO_DIR} is {g:.1f}GB, below the "
                            f"{min_gb}GB safety floor -- stopping before creating more "
                            f"entities. Free space or lower this arm's N and resume.")


_TIMELINE_LIST_RE = re.compile(r"([A-Za-z0-9_.\-]+)\s*\[([0-9a-f]{32})\]")


def timeline_list(tenant_id: str) -> dict[str, str]:
    """{branch_name: timeline_id} for the given tenant, parsed from `timeline list`
    (e.g. '(L) main [id]', tree entries '┣━ @lsn: name [id]', root siblings 'name [id]')."""
    proc = neon_local("timeline", "list", "--tenant-id", tenant_id)
    out = {}
    for line in proc.stdout.splitlines():
        m = _TIMELINE_LIST_RE.search(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def get_or_create_branch(branch_name: str, tenant_id: str,
                          ancestor_branch_name: Optional[str] = None,
                          pg_version: int = 17) -> str:
    """Idempotent branch/timeline creation: if `branch_name` already exists for this
    tenant (e.g. left over from a step that failed *after* creation on a previous,
    interrupted run), reuse it instead of erroring out. Otherwise create it (a branch
    of `ancestor_branch_name` if given, else a blank bootstrap timeline)."""
    existing = timeline_list(tenant_id)
    if branch_name in existing:
        return existing[branch_name]
    if ancestor_branch_name is not None:
        return timeline_branch(branch_name, ancestor_branch_name=ancestor_branch_name,
                                tenant_id=tenant_id)
    return timeline_create(branch_name, pg_version=pg_version, tenant_id=tenant_id)


def ensure_endpoint_created(endpoint_id: str, branch_name: str, pg_port: int,
                             ext_http_port: int, int_http_port: int,
                             tenant_id: Optional[str] = None, pg_version: int = 17):
    """Idempotent endpoint_create: skip if the endpoint object already exists on disk
    (same recovery scenario as get_or_create_branch)."""
    existing = endpoint_list(tenant_id)
    if endpoint_id in existing:
        return
    endpoint_create(endpoint_id, branch_name, pg_port, ext_http_port, int_http_port,
                     tenant_id=tenant_id, pg_version=pg_version)


# --------------------------------------------------------------------------
# Pageserver HTTP management API
# --------------------------------------------------------------------------

class PSHttp:
    def __init__(self, base_url: str = PS_HTTP_BASE):
        self.base_url = base_url

    def metrics_text(self) -> str:
        r = requests.get(f"{self.base_url}/metrics", timeout=30)
        r.raise_for_status()
        return r.text

    def checkpoint(self, tenant_shard_id: str, timeline_id: str, timeout: float = 120):
        r = requests.put(
            f"{self.base_url}/v1/tenant/{tenant_shard_id}/timeline/{timeline_id}/checkpoint",
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json() if r.text else None

    def compact(self, tenant_shard_id: str, timeline_id: str, timeout: float = 120):
        r = requests.put(
            f"{self.base_url}/v1/tenant/{tenant_shard_id}/timeline/{timeline_id}/compact",
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json() if r.text else None

    def do_gc(self, tenant_shard_id: str, timeline_id: str, gc_horizon: int = 0,
              timeout: float = 120):
        r = requests.put(
            f"{self.base_url}/v1/tenant/{tenant_shard_id}/timeline/{timeline_id}/do_gc",
            json={"gc_horizon": gc_horizon}, timeout=timeout,
        )
        r.raise_for_status()
        return r.json()

    def timeline_status(self, tenant_shard_id: str, timeline_id: str) -> dict:
        r = requests.get(
            f"{self.base_url}/v1/tenant/{tenant_shard_id}/timeline/{timeline_id}", timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def tenant_status(self, tenant_shard_id: str) -> dict:
        r = requests.get(f"{self.base_url}/v1/tenant/{tenant_shard_id}", timeout=30)
        r.raise_for_status()
        return r.json()


_METRIC_LINE_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9eE+.\-nafNAFI]+)\s*$')


def parse_prometheus(text: str) -> list[tuple[str, dict, float]]:
    """Return [(metric_name, {label: value}, value), ...] for non-comment lines."""
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
# pgbench
# --------------------------------------------------------------------------

def pgbench_init(connstr: str, scale: int = 50, timeout: float = 300):
    run([str(PGBENCH_BIN), "-i", "-I", "dtGvp", "-s", str(scale), connstr],
        timeout=timeout, log_name="pgbench_init")


def pgbench_start_background(connstr: str, mode: str, clients: int,
                              duration_s: int, log_path: Path) -> subprocess.Popen:
    """mode: 'rw' (default TPC-B), 'ro' (-S), 'wo' (-N). Runs for duration_s in the background."""
    args = [str(PGBENCH_BIN), "-c", str(clients), "-j", str(min(clients, 8)),
            "-T", str(duration_s)]
    if mode == "ro":
        args.append("-S")
    elif mode == "wo":
        args.append("-N")
    args.append(connstr)
    f = open(log_path, "w")
    return subprocess.Popen(args, cwd=REPO_ROOT, env=_env(), stdout=f, stderr=subprocess.STDOUT)


def pgbench_run_foreground(connstr: str, mode: str, clients: int, duration_s: int) -> dict:
    args = [str(PGBENCH_BIN), "-c", str(clients), "-j", str(min(clients, 8)),
            "-T", str(duration_s)]
    if mode == "ro":
        args.append("-S")
    elif mode == "wo":
        args.append("-N")
    args.append(connstr)
    proc = run(args, timeout=duration_s + 60, log_name="pgbench_probe")
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
# pagebench
# --------------------------------------------------------------------------

def pagebench_getpage(targets: list[str], num_clients: int = 1,
                       per_client_rate: Optional[float] = None,
                       runtime_s: int = 60, keyspace_cache: Optional[Path] = None,
                       page_service_connstring: str = PS_PG_CONNSTRING,
                       mgmt_api: str = PS_HTTP_BASE) -> dict:
    """Run pagebench get-page-latest-lsn. Returns {'json': {...}, 'missed': int, 'stderr': str}."""
    args = [str(PAGEBENCH_BIN), "get-page-latest-lsn",
            "--mgmt-api-endpoint", mgmt_api,
            "--page-service-connstring", page_service_connstring,
            "--num-clients", str(num_clients),
            "--runtime", f"{runtime_s}s"]
    if per_client_rate is not None:
        args += ["--per-client-rate", str(int(round(per_client_rate)))]
    if keyspace_cache is not None:
        args += ["--keyspace-cache", str(keyspace_cache)]
    args += targets
    proc = run(args, timeout=runtime_s + 60, log_name="pagebench")
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
