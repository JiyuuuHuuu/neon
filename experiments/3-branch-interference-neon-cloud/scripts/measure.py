"""
One measurement point against `main`: restart -> warm-up -> P-read -> P-write,
each timed both server-side (neon_perf_counters delta) and client-side (wall clock).

P-start (cold-start time) is the restart itself: `restart_elapsed_s` (API-observed,
issue-to-active) and `first_query_elapsed_s` (issue-to-first-successful-query, the
number that matters end to end).
"""
from __future__ import annotations

import time

import api
import db
import lib

READ_TABLE = "probe_main"
WRITE_TABLE = "probe_write_main"

N_READ_OPS = 150
N_WRITE_OPS = 60


def restart_and_reconnect(project_id: str, endpoint_id: str, host: str, dbname: str,
                           user: str, password: str) -> tuple:
    t0 = time.perf_counter()
    api.restart_endpoint(project_id, endpoint_id)
    api.wait_for_operations(project_id, timeout_s=120)
    restart_elapsed_s = time.perf_counter() - t0
    conn = db.connect_retry(host, dbname, user, password, timeout_s=60)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    first_query_elapsed_s = time.perf_counter() - t0
    return conn, restart_elapsed_s, first_query_elapsed_s


def run_point(project_id: str, endpoint_id: str, host: str, dbname: str, user: str,
              password: str, read_blocks: list[int], write_ids: list[int]) -> dict:
    conn, restart_s, first_query_s = restart_and_reconnect(
        project_id, endpoint_id, host, dbname, user, password)
    try:
        db.ensure_neon_extension(conn)
        # Warm-up: touch catalogs (planner, pg_class, etc.) so that cost doesn't
        # pollute the probe-table-only latency we care about below.
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM pg_class WHERE relname IN ('{READ_TABLE}', '{WRITE_TABLE}')")
            cur.fetchone()

        # neon_clear_lfc() is required, not optional: calibration showed a plain
        # restart clears Postgres shared_buffers (a brand-new process) but NOT the
        # local file cache, which is disk-backed and survives a restart -- a
        # restart-only probe of our fixed, already-seeded probe_main blocks came
        # back >99% LFC *hits* (203 hits / 2 misses out of 200 reads), because the
        # seeding INSERT itself had already populated LFC for those exact pages.
        # Explicit clear_lfc() after the restart (shared_buffers empty) + before the
        # snapshot (so LFC is empty too) is what actually makes each block's first
        # touch a genuine pageserver GetPage@LSN -- confirmed empirically: 5 known-
        # untouched blocks read right after clear_lfc() produced exactly 5 new LFC
        # misses and 0 new hits.
        with conn.cursor() as cur:
            cur.execute("SELECT neon_clear_lfc()")

        before_read = db.snapshot_counters(conn)
        read_result = db.read_probe(conn, READ_TABLE, read_blocks)
        after_read = db.snapshot_counters(conn)

        before_write = after_read
        write_result = db.write_probe(conn, WRITE_TABLE, write_ids)
        after_write = db.snapshot_counters(conn)

        record = {
            "ts": lib.now_iso(),
            "restart_elapsed_s": restart_s,
            "first_query_elapsed_s": first_query_s,
            "read": {
                "n_ops": read_result["n_ops"],
                "client_seconds_total": read_result["client_seconds"],
                "client_ms_per_op": 1000 * read_result["client_seconds"] / read_result["n_ops"],
                "getpage_mean_ms": _ms(db.histogram_delta_mean(before_read, after_read, "getpage_wait_seconds")),
                "getpage_p50_ms": _ms(db.histogram_delta_percentile(before_read, after_read, "getpage_wait_seconds", 0.50)),
                "getpage_p99_ms": _ms(db.histogram_delta_percentile(before_read, after_read, "getpage_wait_seconds", 0.99)),
                "getpage_op_count": after_read.get("getpage_wait_seconds_count", 0) - before_read.get("getpage_wait_seconds_count", 0),
                "lfc_misses_delta": after_read.get("lfc_misses", 0) - before_read.get("lfc_misses", 0),
                "lfc_hits_delta": after_read.get("lfc_hits", 0) - before_read.get("lfc_hits", 0),
            },
            "write": {
                "n_ops": write_result["n_ops"],
                "client_seconds_total": write_result["client_seconds"],
                "client_ms_per_op": 1000 * write_result["client_seconds"] / write_result["n_ops"],
                "commit_mean_ms": _ms(db.histogram_delta_mean(before_write, after_write, "quorum_commit_latency_seconds")),
                "commit_p50_ms": _ms(db.histogram_delta_percentile(before_write, after_write, "quorum_commit_latency_seconds", 0.50)),
                "commit_p99_ms": _ms(db.histogram_delta_percentile(before_write, after_write, "quorum_commit_latency_seconds", 0.99)),
                "commit_op_count": after_write.get("quorum_commit_latency_seconds_count", 0) - before_write.get("quorum_commit_latency_seconds_count", 0),
            },
        }
        return record
    finally:
        conn.close()


def _ms(seconds):
    return None if seconds is None else seconds * 1000.0
