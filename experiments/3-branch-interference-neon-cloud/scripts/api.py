"""
Thin wrapper over the Neon REST API (v2) for experiment 3.

Auth: reads the API key from $NEON_API_KEY, falling back to ~/.neon_api_key
(chmod 600, one line, no trailing newline required). Never logs the key or
any connection string containing a password; callers that need a password
get it back as a return value, not printed.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

import requests

BASE = "https://console.neon.tech/api/v2"


def _load_api_key() -> str:
    key = os.environ.get("NEON_API_KEY")
    if key:
        return key.strip()
    keyfile = Path.home() / ".neon_api_key"
    if keyfile.exists():
        return keyfile.read_text().strip()
    raise RuntimeError("No Neon API key found in $NEON_API_KEY or ~/.neon_api_key")


_API_KEY = _load_api_key()
_HEADERS = {
    "Authorization": f"Bearer {_API_KEY}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


class NeonAPIError(RuntimeError):
    pass


def _req(method: str, path: str, json: Optional[dict] = None, params: Optional[dict] = None) -> dict:
    url = f"{BASE}{path}"
    for attempt in range(5):
        resp = requests.request(method, url, headers=_HEADERS, json=json, params=params, timeout=30)
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 2 * (attempt + 1)))
            time.sleep(wait)
            continue
        break
    if resp.status_code >= 400:
        raise NeonAPIError(f"{method} {path} -> {resp.status_code}: {resp.text}")
    if resp.text.strip() == "":
        return {}
    return resp.json()


# --- Org / account ---

def get_org_id() -> str:
    data = _req("GET", "/users/me/organizations")
    orgs = data["organizations"]
    assert len(orgs) == 1, f"expected exactly one org, got {orgs}"
    return orgs[0]["id"]


def get_org_limits(org_id: str) -> dict:
    return _req("GET", f"/organizations/{org_id}/limits")


# --- Projects ---

def create_project(name: str, org_id: str, region_id: str = "aws-us-east-2",
                    pg_version: int = 17, min_cu: float = 2, max_cu: float = 2) -> dict:
    body = {
        "project": {
            "name": name,
            "region_id": region_id,
            "org_id": org_id,
            "pg_version": pg_version,
            "default_endpoint_settings": {
                "autoscaling_limit_min_cu": min_cu,
                "autoscaling_limit_max_cu": max_cu,
                "suspend_timeout_seconds": 0,
            },
        }
    }
    return _req("POST", "/projects", json=body)


def get_project(project_id: str) -> dict:
    return _req("GET", f"/projects/{project_id}")["project"]


def delete_project(project_id: str) -> dict:
    return _req("DELETE", f"/projects/{project_id}")


# --- Branches ---

def create_branch(project_id: str, name: str, parent_id: Optional[str] = None,
                   init_source: Optional[str] = None,
                   endpoint_min_cu: Optional[float] = None,
                   endpoint_max_cu: Optional[float] = None,
                   with_endpoint: bool = True) -> dict:
    branch: dict[str, Any] = {"name": name}
    if parent_id:
        branch["parent_id"] = parent_id
    if init_source:
        branch["init_source"] = init_source
    body: dict[str, Any] = {"branch": branch}
    if with_endpoint:
        ep: dict[str, Any] = {"type": "read_write"}
        if endpoint_min_cu is not None:
            ep["autoscaling_limit_min_cu"] = endpoint_min_cu
        if endpoint_max_cu is not None:
            ep["autoscaling_limit_max_cu"] = endpoint_max_cu
        body["endpoints"] = [ep]
    return _req("POST", f"/projects/{project_id}/branches", json=body)


def get_branch(project_id: str, branch_id: str) -> dict:
    return _req("GET", f"/projects/{project_id}/branches/{branch_id}")["branch"]


def list_branches(project_id: str) -> list[dict]:
    return _req("GET", f"/projects/{project_id}/branches")["branches"]


def delete_branch(project_id: str, branch_id: str) -> dict:
    return _req("DELETE", f"/projects/{project_id}/branches/{branch_id}")


# --- Endpoints (computes) ---

def create_endpoint(project_id: str, branch_id: str, min_cu: float, max_cu: float,
                     endpoint_type: str = "read_write") -> dict:
    body = {
        "endpoint": {
            "branch_id": branch_id,
            "type": endpoint_type,
            "autoscaling_limit_min_cu": min_cu,
            "autoscaling_limit_max_cu": max_cu,
        }
    }
    return _req("POST", f"/projects/{project_id}/endpoints", json=body)


def get_endpoint(project_id: str, endpoint_id: str) -> dict:
    return _req("GET", f"/projects/{project_id}/endpoints/{endpoint_id}")["endpoint"]


def restart_endpoint(project_id: str, endpoint_id: str) -> dict:
    return _req("POST", f"/projects/{project_id}/endpoints/{endpoint_id}/restart")


def suspend_endpoint(project_id: str, endpoint_id: str) -> dict:
    return _req("POST", f"/projects/{project_id}/endpoints/{endpoint_id}/suspend")


def start_endpoint(project_id: str, endpoint_id: str) -> dict:
    return _req("POST", f"/projects/{project_id}/endpoints/{endpoint_id}/start")


def delete_endpoint(project_id: str, endpoint_id: str) -> dict:
    return _req("DELETE", f"/projects/{project_id}/endpoints/{endpoint_id}")


# --- Connections ---

def get_connection_uri(project_id: str, branch_id: str, database_name: str, role_name: str,
                        pooled: bool = False) -> str:
    params = {
        "branch_id": branch_id,
        "database_name": database_name,
        "role_name": role_name,
        "pooled": str(pooled).lower(),
    }
    data = _req("GET", f"/projects/{project_id}/connection_uri", params=params)
    return data["uri"]


def get_role_password(project_id: str, branch_id: str, role_name: str) -> str:
    data = _req("GET", f"/projects/{project_id}/branches/{branch_id}/roles/{role_name}/reveal_password")
    return data["password"]


# --- Operations ---

def list_operations(project_id: str, limit: int = 20) -> list[dict]:
    return _req("GET", f"/projects/{project_id}/operations", params={"limit": limit})["operations"]


def wait_for_operations(project_id: str, timeout_s: float = 180, poll_s: float = 2.0) -> None:
    """Poll until no operation for this project is running/scheduling."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ops = list_operations(project_id, limit=30)
        pending = [o for o in ops if o["status"] in ("running", "scheduling")]
        if not pending:
            return
        time.sleep(poll_s)
    raise TimeoutError(f"operations still pending on {project_id} after {timeout_s}s")


# --- Consumption / budget guardrails ---

def project_consumption_summary(project_id: str) -> dict:
    """Cheap snapshot of the fields that matter for the Free-plan budget guardrail."""
    p = get_project(project_id)
    return {
        "compute_time_seconds": p.get("compute_time_seconds", 0),
        "active_time_seconds": p.get("active_time_seconds", 0),
        "synthetic_storage_size": p.get("synthetic_storage_size", 0),
        "data_transfer_bytes": p.get("data_transfer_bytes", 0),
        "written_data_bytes": p.get("written_data_bytes", 0),
    }
