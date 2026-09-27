#!/usr/bin/env python3
"""
Phase 5: aggregate data/raw_{tag}.jsonl into figures/ and a summary table.

Usage:
    PILOT=1 python3 analyze.py     # pilot data
    python3 analyze.py             # prod data
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib
import params

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

RAW_PATH = lib.EXPERIMENT_DIR / "data" / f"raw_{params.TAG}.jsonl"
FIG_DIR = lib.EXPERIMENT_DIR / "figures"
FIG_DIR.mkdir(parents=True, exist_ok=True)

SHAPE_COLOR = {"horizontal": "#2E5EAA", "vertical": "#DA7422"}
SHAPE_LABEL = {"horizontal": "horizontal (N branches off root)",
               "vertical": "vertical (chain, depth = N)"}
SHAPE_GRID = {"horizontal": params.HORIZONTAL_N_GRID, "vertical": params.VERTICAL_N_GRID}


def load_records() -> list[dict]:
    if not RAW_PATH.exists():
        return []
    out = []
    with open(RAW_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def bootstrap_ci(values: list[float], n_boot: int = 2000, alpha: float = 0.05):
    if len(values) == 0:
        return (float("nan"), float("nan"), float("nan"))
    arr = np.array(values)
    if len(arr) == 1:
        return (arr[0], arr[0], arr[0])
    rng = np.random.default_rng(42)
    boots = [np.mean(rng.choice(arr, size=len(arr), replace=True)) for _ in range(n_boot)]
    lo, hi = np.percentile(boots, [100 * (alpha / 2), 100 * (1 - alpha / 2)])
    return (float(np.mean(arr)), float(lo), float(hi))


def storage_points(records):
    return [r for r in records if r.get("tier") == "storage" and "error" not in r]


def pooled_branch_metric(records, shape, n, metric_key) -> list[float]:
    """Pool the metric across every branch, in every rep, at this (shape, n)."""
    out = []
    for r in storage_points(records):
        if r["shape"] != shape or r["n"] != n:
            continue
        for b in r.get("branches", []):
            v = b.get(metric_key)
            if v is not None:
                out.append(v)
    return out


def fig_latency_vs_n(records):
    """The deliverable: N vs latency, one curve per shape. pagebench does not report
    p50 (see experiment 2's own note on this), so we use latency_mean_ms as the
    'typical' curve alongside p99 for the tail, rather than p50/p99 as originally
    sketched."""
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax, metric, title in [(axes[0], "latency_mean_ms", "mean"),
                               (axes[1], "latency_p99_ms", "p99")]:
        for shape in ["horizontal", "vertical"]:
            ns = sorted(SHAPE_GRID[shape])
            means, los, his = [], [], []
            plotted_ns = []
            for n in ns:
                vals = pooled_branch_metric(records, shape, n, metric)
                if not vals:
                    continue
                m, lo, hi = bootstrap_ci(vals)
                plotted_ns.append(n)
                means.append(m)
                los.append(m - lo)
                his.append(hi - m)
            if not plotted_ns:
                continue
            ax.errorbar(plotted_ns, means, yerr=[los, his], marker="o", capsize=3,
                        color=SHAPE_COLOR[shape], label=SHAPE_LABEL[shape])
        ax.set_xscale("log", base=2)
        ax.set_xlabel("N (branches)")
        ax.set_ylabel(f"GetPage@LSN {title} latency (ms)")
        ax.set_title(f"Tier STORAGE: latency vs N ({title})")
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=9)
    fig.suptitle(f"Branch-tree shape vs read scaling ({params.TAG})")
    fig.tight_layout()
    fig.savefig(FIG_DIR / f"latency_vs_n_{params.TAG}.png", dpi=150)
    plt.close(fig)


def fig_latency_vs_depth(records):
    """Vertical arm only: p99 vs depth, pooled from the largest-N sweep point (which
    contains branches at every depth 1..N in one shot) across all its reps."""
    n_max_measured = max((r["n"] for r in storage_points(records) if r["shape"] == "vertical"),
                          default=None)
    if n_max_measured is None:
        return
    per_depth = defaultdict(list)
    for r in storage_points(records):
        if r["shape"] != "vertical" or r["n"] != n_max_measured:
            continue
        for b in r.get("branches", []):
            v = b.get("latency_p99_ms")
            if v is not None:
                per_depth[b["depth"]].append(v)
    if not per_depth:
        return
    depths = sorted(per_depth.keys())
    means = [np.mean(per_depth[d]) for d in depths]
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot(depths, means, marker="o", color=SHAPE_COLOR["vertical"])
    ax.set_xlabel("ancestor depth")
    ax.set_ylabel("GetPage@LSN p99 latency (ms)")
    ax.set_title(f"Per-branch p99 vs depth within the vertical chain (N={n_max_measured})")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIG_DIR / f"latency_vs_depth_{params.TAG}.png", dpi=150)
    plt.close(fig)


def fig_layers_per_read(records):
    """Mechanism check: pageserver_layers_per_read should be higher for the vertical
    arm than horizontal at matched N -- if it isn't, the shape difference never
    reached the storage engine (README.md verification list, item 6)."""
    fig, ax = plt.subplots(figsize=(7, 5))
    for shape in ["horizontal", "vertical"]:
        ns, means = [], []
        for n in sorted(SHAPE_GRID[shape]):
            vals = []
            for r in storage_points(records):
                if r["shape"] != shape or r["n"] != n:
                    continue
                d = r.get("redo_delta", {})
                num = d.get("pageserver_layers_per_read_sum")
                den = d.get("pageserver_layers_per_read_count")
                if num is not None and den:
                    vals.append(num / den)
            if vals:
                ns.append(n)
                means.append(np.mean(vals))
        if ns:
            ax.plot(ns, means, marker="o", color=SHAPE_COLOR[shape], label=SHAPE_LABEL[shape])
    ax.set_xscale("log", base=2)
    ax.set_xlabel("N")
    ax.set_ylabel("mean layers touched per GetPage read")
    ax.set_title("Mechanism: layers-per-read vs N")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    fig.tight_layout()
    fig.savefig(FIG_DIR / f"layers_per_read_{params.TAG}.png", dpi=150)
    plt.close(fig)


def _disk_util_pct(before: dict, after: dict) -> float:
    """Max %util across all reported block devices between two diskstats
    snapshots -- see lib.diskstats_now's docstring for why 'max across devices'
    rather than a single named device."""
    if not before or not after:
        return float("nan")
    dt_ms = (after["ts"] - before["ts"]) * 1000.0
    if dt_ms <= 0:
        return float("nan")
    best = 0.0
    for dev, ticks_after in after.get("devices", {}).items():
        ticks_before = before.get("devices", {}).get(dev)
        if ticks_before is None:
            continue
        util = (ticks_after - ticks_before) / dt_ms * 100.0
        best = max(best, util)
    return best


def fig_disk_utilization(records):
    """Is the sweep disk-bound? node0's max block-device %util per point, vs N."""
    fig, ax = plt.subplots(figsize=(7, 5))
    for shape in ["horizontal", "vertical"]:
        ns, means = [], []
        for n in sorted(SHAPE_GRID[shape]):
            vals = []
            for r in storage_points(records):
                if r["shape"] != shape or r["n"] != n:
                    continue
                u = _disk_util_pct(r.get("diskstats_before"), r.get("diskstats_after"))
                if not np.isnan(u):
                    vals.append(u)
            if vals:
                ns.append(n)
                means.append(np.mean(vals))
        if ns:
            ax.plot(ns, means, marker="o", color=SHAPE_COLOR[shape], label=SHAPE_LABEL[shape])
    ax.axhline(100, color="red", linestyle="--", linewidth=0.8, label="100% (saturated)")
    ax.set_xscale("log", base=2)
    ax.set_xlabel("N")
    ax.set_ylabel("node0 max block-device %util")
    ax.set_title("Is the pageserver disk saturated during the sweep?")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    fig.tight_layout()
    fig.savefig(FIG_DIR / f"disk_utilization_{params.TAG}.png", dpi=150)
    plt.close(fig)


def write_summary_table(records):
    lines = ["| tier | shape | n | reps | branch-points | mean latency (ms) | p99 mean (ms) | p99 95% CI | missed sum |",
             "|---|---|---:|---:|---:|---:|---:|---|---:|"]
    for shape in ["horizontal", "vertical"]:
        for n in sorted(SHAPE_GRID[shape]):
            p99_vals = pooled_branch_metric(records, shape, n, "latency_p99_ms")
            mean_vals = pooled_branch_metric(records, shape, n, "latency_mean_ms")
            missed_vals = pooled_branch_metric(records, shape, n, "missed")
            if not p99_vals:
                continue
            reps = len({r["rep"] for r in storage_points(records)
                        if r["shape"] == shape and r["n"] == n})
            m, lo, hi = bootstrap_ci(p99_vals)
            lines.append(f"| storage | {shape} | {n} | {reps} | {len(p99_vals)} | "
                         f"{np.mean(mean_vals):.3f} | {m:.3f} | [{lo:.3f}, {hi:.3f}] | "
                         f"{sum(missed_vals):.0f} |")
    out = FIG_DIR.parent / "data" / f"summary_table_{params.TAG}.md"
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}")


def main():
    records = load_records()
    print(f"loaded {len(records)} records from {RAW_PATH}")
    if not records:
        print("no data yet")
        return
    fig_latency_vs_n(records)
    fig_latency_vs_depth(records)
    fig_layers_per_read(records)
    fig_disk_utilization(records)
    write_summary_table(records)
    print(f"figures written to {FIG_DIR}")


if __name__ == "__main__":
    main()
