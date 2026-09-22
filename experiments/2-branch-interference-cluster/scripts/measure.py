"""
Single measurement points for Tier STORAGE (read-only), plus the between-window
GC/compaction protocol. Cluster variant of
../../1-branch-interference-local/scripts/measure.py -- Tier ENDPOINT (write load)
is out of scope for Phase 3 (plan's "Scope: Read tier first" decision) and is not
ported here.

Load generation is spread round-robin across lib.LOAD_NODES (node3-5) so no single
load-generator node becomes its own confound at high N; the probe against `main`
always runs alone from lib.PROBE_NODE (node2), physically isolated from the load
generators per the plan's topology.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import lib

WARMUP_S = 10
KEYSPACE_CACHE_DIR = lib.EXPERIMENT_DIR / "data" / "keyspace_cache"
KEYSPACE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

PS = lib.PSHttp()


def ttid(tenant_id: str, timeline_id: str) -> str:
    return f"{tenant_id}/{timeline_id}"


def gc_protocol(tenant_shard_id: str, timeline_id: str) -> dict:
    PS.checkpoint(tenant_shard_id, timeline_id)
    PS.compact(tenant_shard_id, timeline_id)
    return PS.do_gc(tenant_shard_id, timeline_id, gc_horizon=0)


def gc_all_entries(entries: list[dict]) -> list[dict]:
    results = []
    for e in entries:
        try:
            results.append(gc_protocol(e["tenant_id"], e["timeline_id"]))
        except Exception as ex:
            results.append({"error": str(ex)})
    return results


def assert_healthy(metrics_before_text: str, metrics_after_text: str) -> list[str]:
    problems = []
    before = lib.parse_prometheus(metrics_before_text)
    after = lib.parse_prometheus(metrics_after_text)

    def total(parsed, name):
        return lib.metric_sum(parsed, name)

    for metric in ["pageserver_evictions_total", "pageserver_remote_ondemand_downloaded_layers_total"]:
        b, a = total(before, metric), total(after, metric)
        if a > b:
            problems.append(f"{metric} moved {b}->{a} (eviction/on-demand download active)")

    for metric in ["pageserver_tenant_throttling_count_global"]:
        b, a = total(before, metric), total(after, metric)
        if a > b:
            problems.append(f"{metric} moved {b}->{a} (throttle unexpectedly active)")

    return problems


# WAL-redo mechanism instrumentation (plan Phase 3) -- scraped alongside every point.
_REDO_METRICS = [
    "pageserver_wal_redo_seconds_sum", "pageserver_wal_redo_seconds_count",
    "pageserver_wal_redo_records_histogram_sum", "pageserver_wal_redo_records_histogram_count",
    "pageserver_layers_per_read_sum", "pageserver_layers_per_read_count",
    "pageserver_get_vectored_seconds_sum", "pageserver_get_vectored_seconds_count",
    "pageserver_page_cache_read_hits_total", "pageserver_page_cache_read_accesses_total",
]


def redo_metrics_snapshot(metrics_text: str) -> dict:
    parsed = lib.parse_prometheus(metrics_text)
    return {m: lib.metric_sum(parsed, m) for m in _REDO_METRICS}


def _split_round_robin(items: list, n_buckets: int) -> list[list]:
    buckets = [[] for _ in range(n_buckets)]
    for i, item in enumerate(items):
        buckets[i % n_buckets].append(item)
    return buckets


def measure_storage_point(main_ttid: str, load_targets: list[str], num_clients: int,
                           runtime_s: int, probe_rate: float, keyspace_tag: str) -> dict:
    """Tier STORAGE: closed-loop (saturating) pagebench read load on `load_targets`,
    spread round-robin across lib.LOAD_NODES, concurrent with an open-loop pagebench
    probe on `main_ttid` only from lib.PROBE_NODE (probe_rate req/s, fixed regardless
    of N so the probe's own measurement is immune to closed-loop bias)."""
    metrics_before = PS.metrics_text()
    redo_before = redo_metrics_snapshot(metrics_before)

    load_buckets = _split_round_robin(load_targets, len(lib.LOAD_NODES))
    active_load_nodes = []
    load_logs = {}  # node -> remote log path
    if load_targets:
        for node, targets in zip(lib.LOAD_NODES, load_buckets):
            if not targets:
                continue
            cache = f"{lib.REMOTE_SVC}/keyspace-load-{keyspace_tag}-n{node}.json"
            log_path = lib.pagebench_getpage_background(
                node, targets, num_clients=num_clients,
                runtime_s=runtime_s + WARMUP_S + 10,
                log_name=f"storage_load_{keyspace_tag}_n{node}",
                keyspace_cache_remote=cache,
            )
            active_load_nodes.append(node)
            load_logs[node] = log_path
        time.sleep(WARMUP_S)

    probe_cache_remote = f"{lib.REMOTE_SVC}/keyspace-probe-main.json"
    try:
        probe = lib.pagebench_getpage_remote(lib.PROBE_NODE, [main_ttid], num_clients=1,
                                              per_client_rate=probe_rate, runtime_s=runtime_s,
                                              keyspace_cache_remote=probe_cache_remote)
    finally:
        # Unconditional: a probe timeout/exception must not leave load generators
        # running past their point's window and bleeding into the next one. (The
        # background load processes are self-bounding via their own --runtime + the
        # _timeout_wrap hard-kill, so this is defense in depth, not the only guard.)
        for node in active_load_nodes:
            lib.ssh_pkill(node, "pagebench")

    load_rps_samples = []
    for node in active_load_nodes:
        text = lib.read_remote_log(node, load_logs[node])
        samples = [int(m) for m in re.findall(r"RPS:\s*(\d+)", text)][-runtime_s:]
        load_rps_samples.append(samples)

    metrics_after = PS.metrics_text()
    redo_after = redo_metrics_snapshot(metrics_after)
    problems = assert_healthy(metrics_before, metrics_after)
    summary = lib.pagebench_summary(probe)
    summary["health_problems"] = problems
    # sum concurrent per-node mean RPS as an aggregate saturation signal (not
    # perfectly time-aligned across nodes, but only used as a health/sanity signal,
    # not the headline p99/p95 metric which comes solely from the isolated probe)
    per_node_means = [sum(s) / len(s) for s in load_rps_samples if s]
    summary["load_rps_mean"] = sum(per_node_means) if per_node_means else None
    summary["load_rps_samples_per_node"] = load_rps_samples
    summary["redo_before"] = redo_before
    summary["redo_after"] = redo_after
    summary["redo_delta"] = {k: redo_after[k] - redo_before[k] for k in redo_before}
    return summary
