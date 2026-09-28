#!/usr/bin/env python3
"""
Storage-backend comparison: data/raw_backend_prod.jsonl (warm horizontal sweep per backend)
+ data/backend_switch_prod.jsonl (copy/attach timing) + data/backend_probe_prod.jsonl (raw
backend latency/bandwidth from node0) -> figures/latency_vs_n_by_backend_prod.png and
data/summary_table_backend_prod.md. Experiment 4's original horizontal points
(raw_prod.jsonl, localfs) are overlaid as a historical reference.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import analyze
import lib

import matplotlib.pyplot as plt

DATA = lib.EXPERIMENT_DIR / "data"
GRID = [1, 2, 4, 8, 16, 32]
BACKEND_STYLE = {
    "localfs": ("#2E5EAA", "local_fs (node0 SSD)"),
    "minio": ("#DA7422", "MinIO (node3, LAN)"),
    "s3": ("#3A9A5B", "AWS S3 (us-east-2)"),
    "exp4": ("#9A9A9A", "exp 4 original (local_fs)"),
}


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def valid(records):
    return [r for r in records if "error" not in r and not r.get("health_problems")]


def pooled(records, n, metric):
    return [b[metric] for r in records if r["n"] == n
            for b in r.get("branches", []) if b.get(metric) is not None]


def series(records):
    by = {}
    for r in valid(records):
        if r.get("session", "main") != "main":
            continue
        by.setdefault(r["backend"], []).append(r)
    exp4 = [r for r in load(DATA / "raw_prod.jsonl")
            if r.get("tier") == "storage" and r.get("shape") == "horizontal" and "error" not in r
            and r["n"] in GRID]
    if exp4:
        by["exp4"] = exp4
    return by


def fig(by):
    f, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax, metric, title in [(axes[0], "latency_mean_ms", "mean"), (axes[1], "latency_p99_ms", "p99")]:
        for backend in ["exp4", "localfs", "minio", "s3"]:
            if backend not in by:
                continue
            color, label = BACKEND_STYLE[backend]
            xs, ms, lo, hi = [], [], [], []
            for n in GRID:
                vals = pooled(by[backend], n, metric)
                if not vals:
                    continue
                m, l, h = analyze.bootstrap_ci(vals)
                xs.append(n); ms.append(m); lo.append(m - l); hi.append(h - m)
            ax.errorbar(xs, ms, yerr=[lo, hi], marker="o", capsize=3, color=color, label=label,
                        linestyle="--" if backend == "exp4" else "-", alpha=0.6 if backend == "exp4" else 1)
        ax.set_xscale("log", base=2)
        ax.set_xlabel("N (horizontal branches under load)")
        ax.set_ylabel(f"GetPage@LSN {title} latency (ms)")
        ax.set_title(f"Warm read latency vs N by remote-storage backend ({title})")
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=9)
    f.tight_layout()
    out = lib.EXPERIMENT_DIR / "figures" / "latency_vs_n_by_backend_prod.png"
    f.savefig(out, dpi=150)
    plt.close(f)
    return out


def table(by, raw):
    lines = ["## Warm horizontal GetPage latency by backend", "",
             "| backend | n | points | mean (ms) | mean 95% CI | p99 (ms) | p99 95% CI |",
             "|---|---:|---:|---:|---|---:|---|"]
    for backend in ["localfs", "minio", "s3", "exp4"]:
        for n in GRID:
            recs = [r for r in by.get(backend, []) if r["n"] == n]
            if not recs:
                continue
            m, ml, mh = analyze.bootstrap_ci(pooled(recs, n, "latency_mean_ms"))
            p, pl, ph = analyze.bootstrap_ci(pooled(recs, n, "latency_p99_ms"))
            lines.append(f"| {backend} | {n} | {len(recs)} | {m:.1f} | [{ml:.1f}, {mh:.1f}] | "
                         f"{p:.1f} | [{pl:.1f}, {ph:.1f}] |")

    bad = [r for r in raw if "error" in r or r.get("health_problems")]
    lines += ["", f"Excluded points (error or health problem): {len(bad)}"]
    for r in bad:
        lines.append(f"- {r['backend']} n={r['n']} rep={r['rep']}: "
                     f"{r.get('error', '')[:120] or r.get('health_problems')}")

    lines += ["", "## Per-rep p99 at each N (the rep, not the branch, is the independent unit)", "",
              "| backend | " + " | ".join(f"n={n}" for n in GRID) + " |", "|---|" + "---|" * len(GRID)]
    for backend in ["localfs", "minio", "s3"]:
        cells = []
        for n in GRID:
            reps = sorted((r for r in by.get(backend, []) if r["n"] == n), key=lambda r: r["rep"])
            cells.append(" / ".join(f"{sum(pooled([r], n, 'latency_p99_ms')) / max(len(pooled([r], n, 'latency_p99_ms')), 1):.0f}"
                                    for r in reps))
        lines.append(f"| {backend} | " + " | ".join(cells) + " |")

    drift = [r for r in valid(raw) if r.get("session", "").startswith("drift")]
    if drift:
        lines += ["", "## Drift control (localfs, end of run)", "",
                  "| session | n | requests | mean (ms) | p99 (ms) | layers/read |", "|---|---:|---:|---:|---:|---:|"]
        for r in drift:
            b = r["branches"]
            d = r["redo_delta"]
            lpr = d["pageserver_layers_per_read_sum"] / max(d["pageserver_layers_per_read_count"], 1)
            lines.append(f"| {r['session']} | {r['n']} | {sum(x['request_count'] for x in b)} | "
                         f"{sum(x['latency_mean_ms'] for x in b) / len(b):.1f} | "
                         f"{sum(x['latency_p99_ms'] for x in b) / len(b):.1f} | {lpr:.2f} |")

    lines += ["", "## Backend switch (pageserver restart onto backend)", "",
              "| backend | data copy (s) | restart -> tenant Active (s) | index_parts after | remote error lines |",
              "|---|---:|---:|---|---:|"]
    for s in load(DATA / "backend_switch_prod.jsonl"):
        copy = f"{s['copy_s']:.0f}" if s.get("copy_s") is not None else "-"
        lines.append(f"| {s['backend']} | {copy} | {s['attach_s']:.1f} | "
                     f"{', '.join(s.get('index_parts_after') or [])} | {s['remote_error_lines']} |")

    lines += ["", "## Raw backend probe from node0", "",
              "| backend | small GET first (ms) | small GET p50 (ms) | small GET p99 (ms) | layer GET MB/s (cold, per run) | layer size (MiB) |",
              "|---|---:|---:|---:|---|---:|"]
    for p in load(DATA / "backend_probe_prod.jsonl"):
        mbps = ", ".join(f"{x['MBps']:.0f}" for x in p["large"])
        size = p["large"][0]["bytes"] / 2**20 if p["large"] else 0
        lines.append(f"| {p['backend']} | {p['small_first_ms']:.1f} | {p['small_p50_ms']:.2f} | "
                     f"{p['small_p99_ms']:.2f} | {mbps} | {size:.0f} |")
    out = DATA / "summary_table_backend_prod.md"
    out.write_text("\n".join(lines) + "\n")
    return out


def main():
    raw = load(DATA / "raw_backend_prod.jsonl")
    by = series(raw)
    print(fig(by))
    print(table(by, raw))


if __name__ == "__main__":
    main()
