"""
Single measurement points for both tiers, plus the between-window GC/compaction
protocol that makes branch-induced GC pinning on main directly observable.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import lib

WARMUP_S = 10  # was 30; Tier STORAGE (read-only) is already complete and doesn't
# revisit this. Tier ENDPOINT's warmup runs live write load too, so this also cuts
# disk consumption per point there (see ENDPOINT_RUNTIME_S note in sweep.py).
KEYSPACE_CACHE_DIR = lib.EXPERIMENT_DIR / "data" / "keyspace_cache"
KEYSPACE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

PS = lib.PSHttp()


def ttid(tenant_id: str, timeline_id: str) -> str:
    return f"{tenant_id}/{timeline_id}"


def gc_protocol(tenant_shard_id: str, timeline_id: str) -> dict:
    """checkpoint -> compact -> do_gc, returning the GcResult (layers removed etc)."""
    PS.checkpoint(tenant_shard_id, timeline_id)
    PS.compact(tenant_shard_id, timeline_id)
    gc_result = PS.do_gc(tenant_shard_id, timeline_id, gc_horizon=0)
    return gc_result


_main_timeline_id_cache: dict[str, str] = {}


def gc_all_entries(entries: list[dict]) -> list[dict]:
    """Run the GC protocol on every load entry's own timeline (not just main).
    Under real write load (Tier ENDPOINT), background GC/compaction are off
    (gc_period=0s/compaction_period=0s) and nothing else ever reclaims the L0/WAL
    growth those writes produce -- without this, disk usage grows unboundedly across
    measurement points. A2 tenants never had their own initial timeline_id recorded
    (materialize_tenants stored None); resolve it once via `timeline_list` and cache
    it, rather than re-querying every call."""
    results = []
    for e in entries:
        tid = e["tenant_id"]
        timeline_id = e.get("timeline_id")
        if timeline_id is None:
            timeline_id = _main_timeline_id_cache.get(tid)
            if timeline_id is None:
                timeline_id = lib.timeline_list(tid).get(e.get("branch_name", "main"))
                if timeline_id:
                    _main_timeline_id_cache[tid] = timeline_id
        if not timeline_id:
            continue
        try:
            results.append(gc_protocol(tid, timeline_id))
        except Exception as ex:
            results.append({"error": str(ex)})
    return results


def assert_healthy(metrics_before_text: str, metrics_after_text: str) -> list[str]:
    """Per-window sanity checks from the design review. Returns a list of violations
    (empty = clean)."""
    problems = []
    before = lib.parse_prometheus(metrics_before_text)
    after = lib.parse_prometheus(metrics_after_text)

    def total(parsed, name):
        return lib.metric_sum(parsed, name)

    for metric in ["pageserver_evictions_total", "pageserver_remote_ondemand_downloaded_layers_total"]:
        b, a = total(before, metric), total(after, metric)
        if a > b:
            problems.append(f"{metric} moved {b}->{a} (eviction/on-demand download active)")

    # count_accounted_start increments for every request that enters the throttle's
    # accounting path regardless of whether it was actually rate-limited
    # (pageserver/src/tenant/throttle.rs) -- that's expected to be nonzero even with
    # throttling disabled. pageserver_tenant_throttling_count_global is the subset
    # that was actually throttled; that one should stay at 0.
    for metric in ["pageserver_tenant_throttling_count_global"]:
        b, a = total(before, metric), total(after, metric)
        if a > b:
            problems.append(f"{metric} moved {b}->{a} (throttle unexpectedly active)")

    return problems


def measure_storage_point(main_ttid: str, load_targets: list[str], num_clients: int,
                           runtime_s: int, probe_rate: float, keyspace_tag: str) -> dict:
    """Tier STORAGE: closed-loop (saturating) pagebench read load on `load_targets`
    (`num_clients` workers *per target*, so total load workers = num_clients *
    len(load_targets)), concurrent with an open-loop pagebench probe on `main_ttid`
    only (`probe_rate` requests/s, fixed regardless of N -- this is what makes the
    probe's own measurement immune to closed-loop bias as the system slows down)."""
    metrics_before = PS.metrics_text()

    load_proc = None
    load_log_path = lib.LOG_DIR / f"storage_load_{keyspace_tag}.log"
    if load_targets:
        load_cache = KEYSPACE_CACHE_DIR / f"load-{keyspace_tag}.json"
        load_args = [str(lib.PAGEBENCH_BIN), "get-page-latest-lsn",
                     "--mgmt-api-endpoint", lib.PS_HTTP_BASE,
                     "--page-service-connstring", lib.PS_PG_CONNSTRING,
                     "--num-clients", str(num_clients),
                     "--runtime", f"{runtime_s + WARMUP_S + 10}s",
                     "--keyspace-cache", str(load_cache)]
        load_args += load_targets
        load_log = open(load_log_path, "w")
        load_proc = subprocess.Popen(load_args, cwd=lib.REPO_ROOT, env=lib._env(),
                                      stdout=load_log, stderr=subprocess.STDOUT)
        time.sleep(WARMUP_S)

    probe_cache = KEYSPACE_CACHE_DIR / f"probe-main.json"
    probe = lib.pagebench_getpage([main_ttid], num_clients=1, per_client_rate=probe_rate,
                                   runtime_s=runtime_s, keyspace_cache=probe_cache)

    load_rps_samples = []
    if load_proc is not None:
        load_proc.terminate()
        try:
            load_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            load_proc.kill()
        # sample the load process's own per-second "RPS: N MISSED: M" log lines
        # emitted during the concurrent (post-warmup) window, as a saturation signal
        try:
            text = load_log_path.read_text()
            load_rps_samples = [int(m) for m in re.findall(r"RPS:\s*(\d+)", text)][-runtime_s:]
        except FileNotFoundError:
            pass

    metrics_after = PS.metrics_text()
    problems = assert_healthy(metrics_before, metrics_after)
    summary = lib.pagebench_summary(probe)
    summary["health_problems"] = problems
    summary["load_rps_mean"] = (sum(load_rps_samples) / len(load_rps_samples)
                                 if load_rps_samples else None)
    summary["load_rps_samples"] = load_rps_samples
    return summary


def measure_endpoint_point(main_connstr: str, load_connstrs: list[str],
                            load_clients: int, runtime_s: int,
                            main_ttid: Optional[str] = None,
                            probe_rate: Optional[float] = None) -> dict:
    """Tier ENDPOINT: pgbench TPC-B write load on `load_connstrs` (real endpoints),
    probed with pgbench -S (read) and -N (write) on main, plus an optional pagebench
    probe against main's pageserver timeline for a storage-layer number in the same
    window."""
    metrics_before = PS.metrics_text()

    load_procs = []
    for cs in load_connstrs:
        log = lib.LOG_DIR / f"endpoint_load_{abs(hash(cs))}.log"
        p = lib.pgbench_start_background(cs, "rw", load_clients, runtime_s + WARMUP_S + 20, log)
        load_procs.append(p)

    if load_procs:
        time.sleep(WARMUP_S)

    result = {}
    if main_ttid is not None:
        probe_cache = KEYSPACE_CACHE_DIR / "probe-main-endpoint-tier.json"
        pb = lib.pagebench_getpage([main_ttid], num_clients=1, per_client_rate=probe_rate,
                                    runtime_s=runtime_s, keyspace_cache=probe_cache)
        result["pagebench"] = lib.pagebench_summary(pb)

    result["pgbench_read"] = lib.pgbench_run_foreground(main_connstr, "ro", 1, runtime_s)
    result["pgbench_write"] = lib.pgbench_run_foreground(main_connstr, "wo", 1, runtime_s)

    for p in load_procs:
        try:
            p.wait(timeout=runtime_s + 60)
        except subprocess.TimeoutExpired:
            p.kill()

    metrics_after = PS.metrics_text()
    result["health_problems"] = assert_healthy(metrics_before, metrics_after)
    return result
