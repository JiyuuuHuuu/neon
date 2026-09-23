"""Concurrent pgbench TPC-B load against the N children, overlapping the probe window."""
from __future__ import annotations

import subprocess
import sys

sys.path.insert(0, ".")

PGBENCH = "/mydata/jiyu/neon/pg_install/v17/bin/pgbench"

LOAD_DURATION_S = 18
LOAD_CLIENTS = 4
LOAD_JOBS = 2


def connstr(creds: dict) -> str:
    return (f"postgresql://{creds['role']}:{creds['password']}@{creds['host']}/"
            f"{creds['database']}?sslmode=require")


def start_all(children: list[dict], duration_s: int = LOAD_DURATION_S) -> list[subprocess.Popen]:
    procs = []
    for c in children:
        p = subprocess.Popen(
            [PGBENCH, "-T", str(duration_s), "-c", str(LOAD_CLIENTS), "-j", str(LOAD_JOBS),
             connstr(c)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(p)
    return procs


def wait_all(procs: list[subprocess.Popen], timeout_s: float = 60) -> None:
    for p in procs:
        try:
            p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            p.kill()
