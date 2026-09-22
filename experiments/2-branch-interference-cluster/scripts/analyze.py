#!/usr/bin/env python3
"""
Aggregate data/raw.jsonl into figures/ and a summary table.

Run after (or during, incrementally) the sweep:
    python3 analyze.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

RAW_PATH = lib.EXPERIMENT_DIR / "data" / "raw.jsonl"
FIG_DIR = lib.EXPERIMENT_DIR / "figures"
FIG_DIR.mkdir(parents=True, exist_ok=True)

# Palette: colorblind-safe, one hue per arm, consistent across all figures.
ARM_COLOR = {"A1": "#2E5EAA", "A1b": "#DA7422", "A2": "#3A9278"}
ARM_LABEL = {"A1": "A1: branches of main", "A1b": "A1b: sibling timelines (same tenant)",
             "A2": "A2: separate tenants"}


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
    lo, hi = np.percentile(boots, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return (float(np.mean(arr)), float(lo), float(hi))


def group(records, tier, metric_key):
    """-> {arm: {n: [values]}}"""
    out = defaultdict(lambda: defaultdict(list))
    for r in records:
        if r.get("tier") != tier or "error" in r:
            continue
        v = r.get(metric_key)
        if v is None:
            continue
        out[r["arm"]][r["n"]].append(v)
    return out


def fig_latency_vs_n(records):
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax, metric, title in [(axes[0], "latency_p99_ms", "p99"),
                               (axes[1], "latency_p95_ms", "p95")]:
        g = group(records, "storage", metric)
        for arm in ["A1", "A1b", "A2"]:
            ns = sorted(g.get(arm, {}).keys())
            if not ns:
                continue
            means, los, his = [], [], []
            for n in ns:
                m, lo, hi = bootstrap_ci(g[arm][n])
                means.append(m)
                los.append(m - lo)
                his.append(hi - m)
            ax.errorbar([max(n, 0.5) for n in ns], means, yerr=[los, his],
                        marker="o", capsize=3, color=ARM_COLOR[arm], label=ARM_LABEL[arm])
        ax.set_xscale("symlog", linthresh=1)
        ax.set_xlabel("N (branches / siblings / tenants)")
        ax.set_ylabel(f"main GetPage@LSN {title} latency (ms)")
        ax.set_title(f"Tier STORAGE: main latency vs N ({title})")
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=9)
    fig.tight_layout()
    fig.savefig(FIG_DIR / "latency_vs_n.png", dpi=150)
    plt.close(fig)


def fig_decomposition(records, n_target=32):
    """A1-A1b, A1b-A2, A2-A0(n=0) at a fixed N, showing the 3-way split."""
    g = group(records, "storage", "latency_p99_ms")
    a0 = np.mean(g.get("A1", {}).get(0, [np.nan]))  # N=0 is arm-independent
    vals = {}
    for arm in ["A1", "A1b", "A2"]:
        v = g.get(arm, {}).get(n_target)
        vals[arm] = np.mean(v) if v else np.nan

    branch_specific = vals["A1"] - vals["A1b"]
    tenant_scoped = vals["A1b"] - vals["A2"]
    generic = vals["A2"] - a0

    fig, ax = plt.subplots(figsize=(7, 5))
    labels = ["A1 - A1b\n(branch-specific:\nancestry, retain_lsn,\nshared layers)",
              "A1b - A2\n(tenant-scoped:\nwalredo, serial\ncompaction/GC)",
              "A2 - A0\n(generic machine\ncontention)"]
    vals3 = [branch_specific, tenant_scoped, generic]
    colors = ["#2E5EAA", "#DA7422", "#3A9278"]
    bars = ax.bar(labels, vals3, color=colors)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel(f"p99 latency delta (ms) at N={n_target}")
    ax.set_title(f"Decomposition of main's latency increase at N={n_target}")
    for b, v in zip(bars, vals3):
        if not np.isnan(v):
            ax.annotate(f"{v:+.2f}", (b.get_x() + b.get_width() / 2, v),
                        ha="center", va="bottom" if v >= 0 else "top", fontsize=10)
    fig.tight_layout()
    fig.savefig(FIG_DIR / "decomposition.png", dpi=150)
    plt.close(fig)


def fig_mechanism_gc(records):
    fig, ax = plt.subplots(figsize=(7, 5))
    per_arm = defaultdict(lambda: defaultdict(list))
    for r in records:
        if r.get("tier") != "storage" or "error" in r:
            continue
        gc = r.get("gc_before")
        if not gc:
            continue
        per_arm[r["arm"]][r["n"]].append(gc.get("layers_needed_by_branches", 0))
    for arm in ["A1", "A1b", "A2"]:
        ns = sorted(per_arm.get(arm, {}).keys())
        if not ns:
            continue
        means = [np.mean(per_arm[arm][n]) for n in ns]
        ax.plot([max(n, 0.5) for n in ns], means, marker="o", color=ARM_COLOR[arm],
                label=ARM_LABEL[arm])
    ax.set_xscale("symlog", linthresh=1)
    ax.set_xlabel("N")
    ax.set_ylabel("layers_needed_by_branches on main (GcResult)")
    ax.set_title("Direct measurement of branch-induced GC pinning on main")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    fig.tight_layout()
    fig.savefig(FIG_DIR / "mechanism_gc.png", dpi=150)
    plt.close(fig)


def fig_percentile_ladder(records, n_target_options=(32, 256)):
    fig, ax = plt.subplots(figsize=(7, 5))
    percentiles = ["latency_p95_ms", "latency_p99_ms", "latency_p99.9_ms", "latency_p99.99_ms"]
    xlabels = ["p95", "p99", "p99.9", "p99.99"]
    for arm in ["A1", "A1b", "A2"]:
        for n_target in n_target_options:
            g = {p: group(records, "storage", p).get(arm, {}).get(n_target) for p in percentiles}
            if not g[percentiles[0]]:
                continue
            means = [np.mean(g[p]) if g[p] else np.nan for p in percentiles]
            style = "-" if n_target == max(n_target_options) else "--"
            ax.plot(xlabels, means, style, marker="o", color=ARM_COLOR[arm],
                    label=f"{arm} N={n_target}")
    ax.set_ylabel("main GetPage@LSN latency (ms)")
    ax.set_title("Percentile ladder (no p50 available from pagebench)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG_DIR / "percentile_ladder.png", dpi=150)
    plt.close(fig)


def fig_missed(records):
    fig, ax = plt.subplots(figsize=(7, 5))
    g = group(records, "storage", "missed")
    for arm in ["A1", "A1b", "A2"]:
        ns = sorted(g.get(arm, {}).keys())
        if not ns:
            continue
        means = [np.mean(g[arm][n]) for n in ns]
        ax.plot([max(n, 0.5) for n in ns], means, marker="o", color=ARM_COLOR[arm],
                label=ARM_LABEL[arm])
    ax.set_xscale("symlog", linthresh=1)
    ax.set_xlabel("N")
    ax.set_ylabel("MISSED requests (open-loop probe overruns)")
    ax.set_title("Probe overruns vs N (rising MISSED at constant offered rate = degradation)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    fig.tight_layout()
    fig.savefig(FIG_DIR / "missed_vs_n.png", dpi=150)
    plt.close(fig)


def write_summary_table(records):
    lines = ["| tier | arm | n | reps | p99 mean (ms) | p99 95% CI | missed mean |",
             "|---|---|---:|---:|---:|---|---:|"]
    for tier in ["storage", "endpoint"]:
        g_lat = group(records, tier, "latency_p99_ms")
        g_missed = group(records, tier, "missed")
        for arm in ["A1", "A1b", "A2"]:
            for n in sorted(g_lat.get(arm, {}).keys()):
                vals = g_lat[arm][n]
                m, lo, hi = bootstrap_ci(vals)
                missed_vals = g_missed.get(arm, {}).get(n, [])
                missed_m = np.mean(missed_vals) if missed_vals else float("nan")
                lines.append(f"| {tier} | {arm} | {n} | {len(vals)} | {m:.3f} | "
                             f"[{lo:.3f}, {hi:.3f}] | {missed_m:.1f} |")
    out = FIG_DIR.parent / "data" / "summary_table.md"
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}")


def main():
    records = load_records()
    print(f"loaded {len(records)} records from {RAW_PATH}")
    if not records:
        print("no data yet")
        return
    fig_latency_vs_n(records)
    fig_decomposition(records, n_target=32)
    fig_mechanism_gc(records)
    fig_percentile_ladder(records)
    fig_missed(records)
    write_summary_table(records)
    print(f"figures written to {FIG_DIR}")


if __name__ == "__main__":
    main()
