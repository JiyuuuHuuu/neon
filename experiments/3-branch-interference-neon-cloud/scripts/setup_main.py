"""
One-time setup of `main`'s probe data. Idempotent (checks data/manifests/main.json).

    python3 setup_main.py
"""
from __future__ import annotations

import random
import sys

sys.path.insert(0, ".")
import db
import lib

READ_ROWS = 300_000         # ~ padded to land around 45-60MB, well under the 512MB/branch cap
WRITE_ROWS = 2_000
N_READ_BLOCKS = 200          # pool of distinct probe pages picked once, reused every window
N_WRITE_IDS = 200


def main():
    sec = lib.load_secret("main_project")
    conn = db.connect(sec["host"], sec["database"], sec["role"], sec["password"])
    db.ensure_neon_extension(conn)

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS probe_main")
        cur.execute("""
            CREATE TABLE probe_main (
                id bigint PRIMARY KEY,
                payload text
            )
        """)
        cur.execute(
            "INSERT INTO probe_main SELECT g, repeat(md5(g::text), 4) FROM generate_series(1, %s) g",
            (READ_ROWS,),
        )
        cur.execute("VACUUM (ANALYZE) probe_main")
        cur.execute("SELECT relpages FROM pg_class WHERE relname = 'probe_main'")
        relpages = cur.fetchone()[0]
        cur.execute("SELECT pg_size_pretty(pg_relation_size('probe_main'))")
        size_pretty = cur.fetchone()[0]

        cur.execute("DROP TABLE IF EXISTS probe_write_main")
        cur.execute("CREATE TABLE probe_write_main (id bigint PRIMARY KEY, v bigint DEFAULT 0)")
        cur.execute(
            "INSERT INTO probe_write_main SELECT g, 0 FROM generate_series(1, %s) g",
            (WRITE_ROWS,),
        )
        cur.execute("VACUUM (ANALYZE) probe_write_main")

    print(f"probe_main: {relpages} pages, {size_pretty}")

    rng = random.Random(42)
    read_blocks = rng.sample(range(0, relpages), min(N_READ_BLOCKS, relpages))
    write_ids = rng.sample(range(1, WRITE_ROWS + 1), N_WRITE_IDS)

    lib.save_manifest("main_probe", {
        "read_blocks": read_blocks,
        "write_ids": write_ids,
        "relpages": relpages,
        "size_pretty": size_pretty,
    })
    conn.close()
    print(f"saved {len(read_blocks)} read blocks, {len(write_ids)} write ids to manifest")


if __name__ == "__main__":
    main()
