"""
Data-plane helpers: psycopg connections, the neon_perf_counters histogram parser,
and the three probes run against `main`'s compute (read / write / start).

Why server-side counters, not client wall-clock, for P-read/P-write:
`neon_perf_counters` (view, from `CREATE EXTENSION neon`) exposes Prometheus-style
histograms measured *inside the compute*: `getpage_wait_seconds_*` is the time a
backend spent waiting on a GetPage@LSN round-trip to the pageserver, and
`quorum_commit_latency_seconds_*` is the time walproposer spent waiting for a
safekeeper write quorum. Both are the actual storage-layer latency the hypothesis is
about, with the Champaign<->aws-us-east-2 client RTT (and Python overhead) subtracted
out for free -- exactly the "e2e without a compute-layer confound" measurement the
plan called for, and better than the clock_timestamp()-in-plpgsql approach originally
sketched. Client wall-clock is still recorded (`client_*` fields) as a supplementary,
not primary, number.

Why a full endpoint restart, not just `neon_clear_lfc()`: calibration on this account
showed a single-row ctid read right after `neon_clear_lfc()` (no restart) produced NO
new getpage_wait_seconds count at all -- the page was still resident in Postgres'
*shared_buffers* (230MB at 2 CU, easily bigger than our probe table), which
neon_clear_lfc() does not touch. Only a full restart guarantees an empty
shared_buffers too, so the first touch of each probe block after restart is a genuine
storage-layer fetch. Confirmed empirically: right after restart, one ctid read
increments getpage_wait_seconds_count; touching the same ctid again does not.
"""
from __future__ import annotations

import time
from typing import Optional

import psycopg

HIST_METRICS = ("getpage_wait_seconds", "quorum_commit_latency_seconds")


def connect(host: str, dbname: str, user: str, password: str, connect_timeout: int = 10,
            autocommit: bool = True) -> psycopg.Connection:
    conn = psycopg.connect(
        host=host, dbname=dbname, user=user, password=password,
        sslmode="require", connect_timeout=connect_timeout,
        autocommit=autocommit,
    )
    return conn


def connect_retry(host: str, dbname: str, user: str, password: str,
                   timeout_s: float = 60, autocommit: bool = True) -> psycopg.Connection:
    """Retry connecting -- used right after a restart, while the compute is still coming up."""
    deadline = time.time() + timeout_s
    last_err = None
    while time.time() < deadline:
        try:
            return connect(host, dbname, user, password, connect_timeout=5, autocommit=autocommit)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(0.5)
    raise TimeoutError(f"could not connect within {timeout_s}s: {last_err}")


def ensure_neon_extension(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS neon;")


def snapshot_counters(conn: psycopg.Connection) -> dict:
    """One dict: per-histogram count/sum plus {bucket_le: cumulative_count}."""
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute("SELECT metric, bucket_le, value FROM neon_perf_counters "
                     "WHERE metric ~ '^(getpage_wait_seconds|quorum_commit_latency_seconds)_'")
        rows = cur.fetchall()
    for metric, bucket_le, value in rows:
        for h in HIST_METRICS:
            if metric == f"{h}_count":
                out[f"{h}_count"] = float(value)
            elif metric == f"{h}_sum":
                out[f"{h}_sum"] = float(value)
            elif metric == f"{h}_bucket":
                out.setdefault(f"{h}_buckets", {})[float(bucket_le)] = float(value)
    with conn.cursor() as cur:
        cur.execute("SELECT file_cache_misses, file_cache_hits, file_cache_used, file_cache_writes "
                     "FROM neon_stat_file_cache")
        row = cur.fetchone()
        if row:
            out["lfc_misses"], out["lfc_hits"], out["lfc_used"], out["lfc_writes"] = map(float, row)
    return out


def histogram_delta_percentile(before: dict, after: dict, prefix: str, q: float) -> Optional[float]:
    """Linear-interpolated percentile (seconds) of the *delta* between two snapshots."""
    b_buckets = before.get(f"{prefix}_buckets", {})
    a_buckets = after.get(f"{prefix}_buckets", {})
    total = after.get(f"{prefix}_count", 0) - before.get(f"{prefix}_count", 0)
    if total <= 0:
        return None
    bounds = sorted(a_buckets.keys())
    target = q * total
    prev_bound, prev_cum = 0.0, 0.0
    for le in bounds:
        cum = a_buckets.get(le, 0.0) - b_buckets.get(le, 0.0)
        cum = max(cum, prev_cum)  # deltas must be monotonic; guard tiny races
        if cum >= target:
            if cum == prev_cum:
                return le
            frac = (target - prev_cum) / (cum - prev_cum)
            lo = prev_bound if prev_bound > 0 else 0.0
            hi = le if le != float("inf") else lo * 2 + 1e-6
            return lo + frac * (hi - lo)
        prev_bound, prev_cum = le, cum
    return bounds[-1] if bounds else None


def histogram_delta_mean(before: dict, after: dict, prefix: str) -> Optional[float]:
    dcount = after.get(f"{prefix}_count", 0) - before.get(f"{prefix}_count", 0)
    dsum = after.get(f"{prefix}_sum", 0) - before.get(f"{prefix}_sum", 0)
    if dcount <= 0:
        return None
    return dsum / dcount


# --- Probe operations ---

def read_probe(conn: psycopg.Connection, table: str, blocks: list[int]) -> dict:
    """One round trip: fetch `blocks` distinct heap pages by ctid. Each is a first
    touch since the last restart, so each is a genuine pageserver GetPage@LSN."""
    ctid_list = ",".join(f"'({b},1)'::tid" for b in blocks)
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE ctid = ANY(ARRAY[{ctid_list}])")
        cur.fetchone()
    client_seconds = time.perf_counter() - t0
    return {"client_seconds": client_seconds, "n_ops": len(blocks)}


def write_probe(conn: psycopg.Connection, table: str, ids: list[int]) -> dict:
    """`len(ids)` separate single-row UPDATE+commit round trips (autocommit connection),
    each its own walproposer quorum-commit event."""
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        for i in ids:
            cur.execute(f"UPDATE {table} SET v = v + 1 WHERE id = %s", (i,))
    client_seconds = time.perf_counter() - t0
    return {"client_seconds": client_seconds, "n_ops": len(ids)}
