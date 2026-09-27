"""
Phase 4: single measurement points for Tier STORAGE (pagebench, primary) and Tier
COMPUTE (pgbench -S over static read-only endpoints, secondary).

Tier STORAGE launches ONE pagebench process PER BRANCH (not one process covering
many targets, unlike experiment 2's load generators) so each branch's own
p50/p95/p99 is directly available -- this is what makes the "latency vs depth"
figure possible for the vertical arm, and what "aggregate" is computed FROM here
rather than measured directly. Every branch is pinned to a fixed load node
(idx % len(LOAD_NODES)), independent of N and rep, so its keyspace-cache file is
built once and reused for the life of the experiment.
"""
from __future__ import annotations

import concurrent.futures
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib
import params

PS = lib.PSHttp()


def ttid(tenant_id: str, timeline_id: str) -> str:
    return f"{tenant_id}/{timeline_id}"


def node_for_idx(idx: int) -> int:
    return lib.LOAD_NODES[idx % len(lib.LOAD_NODES)]


_REDO_METRICS = [
    "pageserver_wal_redo_seconds_sum", "pageserver_wal_redo_seconds_count",
    "pageserver_layers_per_read_sum", "pageserver_layers_per_read_count",
    "pageserver_get_vectored_seconds_sum", "pageserver_get_vectored_seconds_count",
    "pageserver_page_cache_read_hits_total", "pageserver_page_cache_read_accesses_total",
]


def redo_metrics_snapshot(metrics_text: str) -> dict:
    parsed = lib.parse_prometheus(metrics_text)
    return {m: lib.metric_sum(parsed, m) for m in _REDO_METRICS}


def assert_healthy(before_text: str, after_text: str) -> list[str]:
    problems = []
    before = lib.parse_prometheus(before_text)
    after = lib.parse_prometheus(after_text)
    for metric in ["pageserver_evictions_total", "pageserver_remote_ondemand_downloaded_layers_total"]:
        b, a = lib.metric_sum(before, metric), lib.metric_sum(after, metric)
        if a > b:
            problems.append(f"{metric} moved {b}->{a}")
    for metric in ["pageserver_tenant_throttling_count_global"]:
        b, a = lib.metric_sum(before, metric), lib.metric_sum(after, metric)
        if a > b:
            problems.append(f"{metric} moved {b}->{a} (throttle active)")
    return problems


def measure_storage_point(entries: list[dict], tag: str, num_clients: int,
                           runtime_s: int) -> dict:
    """entries: manifest slice [:n] for this point, each with idx/depth/tenant_id/
    timeline_id. Launches one pagebench process per entry, all concurrently, each
    on its fixed load node, then collects per-branch stats."""
    metrics_before = PS.metrics_text()
    redo_before = redo_metrics_snapshot(metrics_before)
    diskstats_before = lib.diskstats_now(lib.PAGESERVER_NODE)

    settle_s = 15
    remote_logs = {}  # idx -> (node, remote_log_path)

    def launch(e: dict):
        node = node_for_idx(e["idx"])
        target = ttid(e["tenant_id"], e["timeline_id"])
        cache = f"{lib.REMOTE_SVC}/keyspace-{e['idx']}.json"
        log_name = f"storage_{tag}_{e['idx']}"
        remote_log = lib.pagebench_getpage_background(
            node, [target], num_clients=num_clients, runtime_s=runtime_s + settle_s,
            log_name=log_name, keyspace_cache_remote=cache,
        )
        return e["idx"], (node, remote_log)

    active_nodes = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, max(1, len(entries)))) as ex:
        for idx, (node, remote_log) in ex.map(launch, entries):
            remote_logs[idx] = (node, remote_log)
            active_nodes.add(node)

    t_launch_done = time.time()
    # Each background process was launched with --runtime (runtime_s + settle_s);
    # it must be allowed to reach that deadline AND finish printing its JSON
    # summary before we pkill it, or ssh_pkill kills it mid-flight and its log
    # never gets a parseable result (found empirically: subtracting 2s here meant
    # every branch silently returned request_count=0 despite real traffic showing
    # up in the pageserver's own metrics). A few extra seconds of margin costs
    # nothing since collect() reads the log after this sleep anyway.
    time.sleep(runtime_s + settle_s + 5)

    def collect(idx: int):
        node, remote_log = remote_logs[idx]
        text = lib.read_remote_log(node, remote_log)
        j = None
        # pretty-printed JSON: find the last line that is exactly "{" and parse from there.
        lines = text.splitlines()
        start_i = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == "{":
                start_i = i
                break
        if start_i is not None:
            import json as _json
            try:
                j = _json.loads("\n".join(lines[start_i:]))
            except _json.JSONDecodeError:
                j = None
        missed = sum(int(m) for m in re.findall(r"MISSED:\s*(\d+)", text))
        summary = lib.pagebench_summary({"json": j, "missed": missed})
        return idx, summary

    per_branch = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, max(1, len(entries)))) as ex:
        for idx, summary in ex.map(collect, [e["idx"] for e in entries]):
            per_branch[idx] = summary

    for node in active_nodes:
        lib.ssh_pkill(node, "pagebench")

    metrics_after = PS.metrics_text()
    redo_after = redo_metrics_snapshot(metrics_after)
    diskstats_after = lib.diskstats_now(lib.PAGESERVER_NODE)
    problems = assert_healthy(metrics_before, metrics_after)

    branches_out = []
    for e in entries:
        s = per_branch.get(e["idx"], {})
        branches_out.append({"idx": e["idx"], "depth": e["depth"], **s})

    return {
        "branches": branches_out,
        "health_problems": problems,
        "redo_before": redo_before, "redo_after": redo_after,
        "redo_delta": {k: redo_after[k] - redo_before[k] for k in redo_before},
        "diskstats_before": diskstats_before, "diskstats_after": diskstats_after,
    }


# --------------------------------------------------------------------------
# Tier COMPUTE: pgbench -S over static, read-only, pinned-LSN endpoints.
# --------------------------------------------------------------------------

def measure_compute_point(entries: list[dict], clients: int, runtime_s: int) -> dict:
    node_slots = lib.COMPUTE_TIER_NODES
    started = []  # (node, port, endpoint_id)
    try:
        for i, e in enumerate(entries):
            node = node_slots[i % len(node_slots)]
            port = params.COMPUTE_TIER_BASE_PORT + (i // len(node_slots))
            endpoint_id = f"ct-{e['idx']}"
            lib.endpoint_start(node, endpoint_id, e["tenant_id"], e["timeline_id"],
                                port=port, static_lsn=e["branch_lsn"])
            started.append((node, port, endpoint_id, e["idx"]))
        for node, port, endpoint_id, idx in started:
            if not lib.endpoint_wait_ready(node, endpoint_id, port=port, timeout_s=120):
                raise RuntimeError(f"compute-tier endpoint {endpoint_id} not ready")

        def run_one(item):
            node, port, endpoint_id, idx = item
            connstr = lib.endpoint_connstr(node, port)
            res = lib.pgbench_run_foreground(node, connstr, mode="ro", clients=clients,
                                              duration_s=runtime_s)
            return idx, res

        per_branch = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(started)) as ex:
            for idx, res in ex.map(run_one, started):
                per_branch[idx] = res
        return {"branches": [{"idx": e["idx"], "depth": e["depth"], **per_branch.get(e["idx"], {})}
                              for e in entries]}
    finally:
        for node, port, endpoint_id, idx in started:
            lib.endpoint_stop(node, endpoint_id)
