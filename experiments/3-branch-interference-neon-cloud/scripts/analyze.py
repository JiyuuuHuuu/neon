#!/usr/bin/env python3
"""
Aggregate data/raw.jsonl into figures/ and a summary table.

    python3 analyze.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
import lib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy import stats

RAW_PATH = lib.DATA_DIR / "raw.jsonl"
FIG_DIR = lib.FIG_DIR

ARM_COLOR = {"A0": "#555555", "A1": "#2E5EAA", "A1b": "#DA7422", "A2": "#3A9278", "A0_end": "#AAAAAA"}
ARM_LABEL = {"A0": "A0: baseline", "A1": "A1: branches of main", "A1b": "A1b: sibling roots (N<=2)",
             "A2": "A2: separate projects", "A0_end": "A0 (re-measured at the end)"}


def load_records() -> list[dict]:
    out = []
    if not RAW_PATH.exists():
        return out
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


def group(records: list[dict], metric_path: tuple[str, ...]) -> dict:
    """{(arm, n): [values]}"""
    out: dict = {}
    for r in records:
        v = r
        for k in metric_path:
            v = v[k]
        out.setdefault((r["arm"], r["n"]), []).append(v)
    return out


def summary_rows(records: list[dict]) -> list[dict]:
    rows = []
    keys = sorted({(r["arm"], r["n"]) for r in records}, key=lambda x: (x[0], x[1]))
    for arm, n in keys:
        sub = [r for r in records if r["arm"] == arm and r["n"] == n]
        for label, path in [("getpage_p99_ms", ("read", "getpage_p99_ms")),
                             ("getpage_mean_ms", ("read", "getpage_mean_ms")),
                             ("commit_p99_ms", ("write", "commit_p99_ms")),
                             ("commit_mean_ms", ("write", "commit_mean_ms"))]:
            vals = [_get(r, path) for r in sub]
            vals = [v for v in vals if v is not None]
            mean, lo, hi = lib.bootstrap_ci(vals)
            rows.append({"arm": arm, "n": n, "metric": label, "mean": mean, "ci_lo": lo,
                         "ci_hi": hi, "reps": len(vals)})
    return rows


def _get(r, path):
    v = r
    for k in path:
        v = v.get(k) if isinstance(v, dict) else None
        if v is None:
            return None
    return v


def write_summary_table(rows: list[dict]) -> None:
    path = lib.DATA_DIR / "summary_table.md"
    with open(path, "w") as f:
        f.write("| arm | n | metric | mean | 95% CI | reps |\n|---|---:|---|---:|---|---:|\n")
        for r in rows:
            f.write(f"| {r['arm']} | {r['n']} | {r['metric']} | {r['mean']:.4f} | "
                     f"[{r['ci_lo']:.4f}, {r['ci_hi']:.4f}] | {r['reps']} |\n")
    print(f"wrote {path}")


def mann_whitney_n0_vs_n9(records: list[dict], arm: str, metric_path: tuple) -> tuple:
    n0 = [_get(r, metric_path) for r in records if r["arm"] == "A0"]
    n0 = [v for v in n0 if v is not None]
    max_n = max((r["n"] for r in records if r["arm"] == arm), default=0)
    nmax = [_get(r, metric_path) for r in records if r["arm"] == arm and r["n"] == max_n]
    nmax = [v for v in nmax if v is not None]
    if len(n0) < 2 or len(nmax) < 2:
        return (max_n, float("nan"), float("nan"))
    u, p = stats.mannwhitneyu(n0, nmax, alternative="less")
    return (max_n, float(u), float(p))


def plot_latency_vs_n(records: list[dict], metric_path: tuple, ylabel: str, fname: str) -> None:
    fig, ax = plt.subplots(figsize=(7, 5))
    for arm in ["A1", "A1b", "A2"]:
        pts = sorted({r["n"] for r in records if r["arm"] == arm})
        means, los, his = [], [], []
        for n in pts:
            vals = [_get(r, metric_path) for r in records if r["arm"] == arm and r["n"] == n]
            vals = [v for v in vals if v is not None]
            m, lo, hi = lib.bootstrap_ci(vals)
            means.append(m); los.append(lo); his.append(hi)
        # prepend the A0 baseline (n=0) so every arm's line starts at the shared origin
        base_vals = [_get(r, metric_path) for r in records if r["arm"] == "A0"]
        base_vals = [v for v in base_vals if v is not None]
        bm, blo, bhi = lib.bootstrap_ci(base_vals)
        xs = [0] + pts
        means = [bm] + means
        los = [blo] + los
        his = [bhi] + his
        ax.plot(xs, means, "o-", color=ARM_COLOR[arm], label=ARM_LABEL[arm])
        ax.fill_between(xs, los, his, color=ARM_COLOR[arm], alpha=0.15)
    ax.set_xlabel("N (children)")
    ax.set_ylabel(ylabel)
    ax.set_title(ylabel + " vs N")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIG_DIR / fname, dpi=150)
    plt.close(fig)
    print(f"wrote {FIG_DIR / fname}")


def plot_drift(records: list[dict]) -> None:
    fig, ax = plt.subplots(figsize=(6, 4))
    for arm, color, label in [("A0", "#2E5EAA", "A0 (start)"), ("A0_end", "#DA7422", "A0_end (after full sweep)")]:
        vals = [r["read"]["getpage_mean_ms"] for r in records if r["arm"] == arm]
        ax.scatter([label] * len(vals), vals, color=color)
    ax.set_ylabel("getpage mean (ms)")
    ax.set_title("Baseline drift check: A0 (start) vs A0_end (~30min later)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(FIG_DIR / "drift_check.png", dpi=150)
    plt.close(fig)
    print(f"wrote {FIG_DIR / 'drift_check.png'}")


def main():
    records = load_records()
    print(f"loaded {len(records)} records")
    rows = summary_rows(records)
    write_summary_table(rows)
    plot_latency_vs_n(records, ("read", "getpage_p99_ms"), "GetPage p99 latency (ms)", "getpage_p99_vs_n.png")
    plot_latency_vs_n(records, ("read", "getpage_mean_ms"), "GetPage mean latency (ms)", "getpage_mean_vs_n.png")
    plot_latency_vs_n(records, ("write", "commit_p99_ms"), "Quorum-commit p99 latency (ms)", "commit_p99_vs_n.png")
    plot_drift(records)

    print("\n=== Mann-Whitney U, A0 (n=0) vs each arm's max N (one-sided: n=0 < max-N) ===")
    for arm in ["A1", "A1b", "A2"]:
        max_n, u, p = mann_whitney_n0_vs_n9(records, arm, ("read", "getpage_p99_ms"))
        print(f"  {arm} (max N={max_n}) getpage_p99: U={u:.1f} p={p:.4f}")
        max_n, u, p = mann_whitney_n0_vs_n9(records, arm, ("write", "commit_p99_ms"))
        print(f"  {arm} (max N={max_n}) commit_p99:  U={u:.1f} p={p:.4f}")


if __name__ == "__main__":
    main()
