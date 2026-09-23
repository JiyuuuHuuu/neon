"""
Incremental, resumable materialization of the three load arms:

  A1  -- N child branches of `main`, same tenant/project (the hypothesis)
  A1b -- N sibling ROOT branches in the same project (schema-only init_source,
         so no live ancestor / no retain_lsn pin on main -- isolates tenant-scoped
         sharing from branch ancestry). Capped at 2 by the account's
         max_root_branches=3 limit (main counts as root #1).
  A2  -- N separate PROJECTS (separate tenants), each with one branch (control
         for machine/pageserver/account-level contention only)

Each ensure_* function only ADDS entities to reach the target N (never removes
mid-arm), matching experiments 1/2's incremental-materialization style. Call
teardown_arm() when switching arms, since A1 and A1b share the same project's
10-branch budget as `main`.

Every created branch/project gets its own pgbench_accounts schema
(`pgbench -i -s 1`, ~16MB) exactly once -- that's the L-write load generator
data; branches also inherit main's (much smaller) probe_main/probe_write_main
tables via copy-on-write, but nothing ever runs load against those on a child.
"""
from __future__ import annotations

import subprocess
import sys

sys.path.insert(0, ".")
import api
import lib

PGBENCH = "/mydata/jiyu/neon/pg_install/v17/bin/pgbench"


def _creds_from_branch_response(r: dict) -> dict:
    """Split into (manifest-safe IDs, secret password) -- the manifest half is
    written under data/manifests/ (inside the git repo), so it must never carry a
    password; the password half is stashed in lib.SECRETS_DIR (outside the repo),
    keyed by branch_id."""
    p = r["connection_uris"][0]["connection_parameters"]
    ids = {
        "branch_id": r["branch"]["id"],
        "endpoint_id": r["endpoints"][0]["id"],
        "host": p["host"],
        "database": p["database"],
        "role": p["role"],
    }
    lib.save_secret(f"pw_{ids['branch_id']}", {"password": p["password"]})
    return ids


def with_password(creds: dict) -> dict:
    pw = lib.load_secret(f"pw_{creds['branch_id']}")["password"]
    return {**creds, "password": pw}


def _pgbench_init(creds: dict) -> None:
    creds = with_password(creds)
    connstr = (f"postgresql://{creds['role']}:{creds['password']}@{creds['host']}/"
               f"{creds['database']}?sslmode=require")
    subprocess.run([PGBENCH, "-i", "-s", "1", "-q", connstr], check=True,
                    capture_output=True, timeout=120)


def ensure_a1(project_id: str, main_branch_id: str, n: int) -> list[dict]:
    state = lib.load_manifest("arm_a1_branches") or {"items": []}
    items = state["items"]
    for i in range(len(items), n):
        r = api.create_branch(project_id, name=f"a1-child-{i}", parent_id=main_branch_id,
                               endpoint_min_cu=lib.CHILD_CU, endpoint_max_cu=lib.CHILD_CU)
        api.wait_for_operations(project_id, timeout_s=120)
        creds = _creds_from_branch_response(r)
        _pgbench_init(creds)
        items.append(creds)
        lib.save_manifest("arm_a1_branches", {"items": items})
    return items[:n]


def ensure_a1b(project_id: str, n: int) -> list[dict]:
    assert n <= lib.MAX_ROOT_BRANCHES_PER_PROJECT - 1, "A1b capped by max_root_branches"
    state = lib.load_manifest("arm_a1b_siblings") or {"items": []}
    items = state["items"]
    for i in range(len(items), n):
        r = api.create_branch(project_id, name=f"a1b-sib-{i}", parent_id=None,
                               init_source="schema-only",
                               endpoint_min_cu=lib.CHILD_CU, endpoint_max_cu=lib.CHILD_CU)
        api.wait_for_operations(project_id, timeout_s=120)
        creds = _creds_from_branch_response(r)
        _pgbench_init(creds)
        items.append(creds)
        lib.save_manifest("arm_a1b_siblings", {"items": items})
    return items[:n]


def ensure_a2(org_id: str, n: int) -> list[dict]:
    state = lib.load_manifest("arm_a2_projects") or {"items": []}
    items = state["items"]
    for i in range(len(items), n):
        r = api.create_project(f"exp3-a2-tenant-{i}", org_id, region_id=lib.REGION_ID,
                                pg_version=lib.PG_VERSION, min_cu=lib.CHILD_CU, max_cu=lib.CHILD_CU)
        p = r["connection_uris"][0]["connection_parameters"]
        creds = {
            "project_id": r["project"]["id"],
            "branch_id": r["branch"]["id"],
            "endpoint_id": r["endpoints"][0]["id"],
            "host": p["host"], "database": p["database"], "role": p["role"],
        }
        lib.save_secret(f"pw_{creds['branch_id']}", {"password": p["password"]})
        api.wait_for_operations(creds["project_id"], timeout_s=120)
        _pgbench_init(creds)
        items.append(creds)
        lib.save_manifest("arm_a2_projects", {"items": items})
    return items[:n]


def teardown_a1(project_id: str) -> None:
    state = lib.load_manifest("arm_a1_branches")
    if not state:
        return
    for item in state["items"]:
        try:
            api.delete_branch(project_id, item["branch_id"])
        except api.NeonAPIError:
            pass
    api.wait_for_operations(project_id, timeout_s=120)
    lib.save_manifest("arm_a1_branches", {"items": []})


def teardown_a1b(project_id: str) -> None:
    state = lib.load_manifest("arm_a1b_siblings")
    if not state:
        return
    for item in state["items"]:
        try:
            api.delete_branch(project_id, item["branch_id"])
        except api.NeonAPIError:
            pass
    api.wait_for_operations(project_id, timeout_s=120)
    lib.save_manifest("arm_a1b_siblings", {"items": []})


def teardown_a2() -> None:
    state = lib.load_manifest("arm_a2_projects")
    if not state:
        return
    for item in state["items"]:
        try:
            api.delete_project(item["project_id"])
        except api.NeonAPIError:
            pass
    lib.save_manifest("arm_a2_projects", {"items": []})
