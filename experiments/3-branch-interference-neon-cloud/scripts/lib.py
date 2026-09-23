"""
Shared config/helpers for experiment 3 (branch interference on real Neon cloud, Free plan).
See ../README.md for the design. Ported from experiments/2-branch-interference-cluster/
scripts/lib.py's load/CI helper style, but the infra layer here is api.py (Neon REST API)
instead of SSH + neon_local/storage_controller.

Secrets (API key, connection passwords) never get written under EXPERIMENT_DIR (which is
inside the git repo). They live in SECRETS_DIR, outside the repo, and manifests under
data/manifests/ store only IDs -- never passwords or full connection URIs.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path("/mydata/jiyu/neon")
EXPERIMENT_DIR = REPO_ROOT / "experiments" / "3-branch-interference-neon-cloud"
DATA_DIR = EXPERIMENT_DIR / "data"
MANIFEST_DIR = DATA_DIR / "manifests"
FIG_DIR = EXPERIMENT_DIR / "figures"
for d in (DATA_DIR, MANIFEST_DIR, FIG_DIR):
    d.mkdir(parents=True, exist_ok=True)

SECRETS_DIR = Path("/tmp/claude-1002/-mydata-jiyu-neon/5d7e4dea-402d-4fb4-be57-662a9eaa5043/scratchpad/secrets")
SECRETS_DIR.mkdir(parents=True, exist_ok=True)

ORG_ID = "org-silent-mode-40875047"
REGION_ID = "aws-us-east-2"
PG_VERSION = 17

# Free-plan account limits, confirmed live via GET /organizations/{org}/limits on 2026-09-22:
MAX_BRANCHES_PER_PROJECT = 10       # -> N <= 9 children in the A1 (branch-of-main) arm
MAX_ROOT_BRANCHES_PER_PROJECT = 3   # -> N <= 2 extra roots in the A1b (sibling-root) arm
MAX_CU = 2.0                        # both autoscaling and fixed-size cap
MIN_CU = 0.25
MAX_ACTIVE_ENDPOINTS = 20
PROJECT_STORAGE_LIMIT_BYTES = 512 * 1024 * 1024  # branch_logical_size_limit_bytes

MAIN_CU = 2.0        # main's compute is pinned min=max so it is never the bottleneck
CHILD_CU = 0.25      # children are load generators, not the system under test

# Budget guardrail: stop the sweep before any one project's active/compute time
# gets close to the org's per-project cap (360000s = 100h, confirmed live).
COMPUTE_TIME_BUDGET_SECONDS = 0.7 * 360_000


def load_secret(name: str) -> dict:
    path = SECRETS_DIR / f"{name}.json"
    return json.loads(path.read_text())


def save_secret(name: str, data: dict) -> None:
    path = SECRETS_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)


def save_manifest(name: str, data: dict) -> None:
    path = MANIFEST_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, default=str))


def load_manifest(name: str) -> Optional[dict]:
    path = MANIFEST_DIR / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def append_jsonl(path: Path, record: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
        f.flush()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def bootstrap_ci(values: list[float], n_boot: int = 2000, alpha: float = 0.05):
    if len(values) == 0:
        return (float("nan"), float("nan"), float("nan"))
    arr = np.array(values)
    if len(arr) == 1:
        return (float(arr[0]), float(arr[0]), float(arr[0]))
    rng = np.random.default_rng(42)
    boots = [np.mean(rng.choice(arr, size=len(arr), replace=True)) for _ in range(n_boot)]
    lo, hi = np.percentile(boots, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return (float(np.mean(arr)), float(lo), float(hi))
