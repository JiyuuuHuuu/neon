#!/usr/bin/env python3
"""
Pageserver remote-storage backend management: localfs (node0:~/ps-remote, experiment 4's
original setup), MinIO (single-node, node3:/mydata/minio), or AWS S3 (us-east-2).

    python3 storage_backend.py setup-minio
    python3 storage_backend.py switch {localfs,minio,s3}   # copy data, restart pageserver, time attach
    python3 storage_backend.py probe  {localfs,minio,s3}   # raw backend latency / bandwidth from node0
    python3 storage_backend.py current

`switch` mirrors node0:~/ps-remote (the authoritative copy; the pageserver is stopped so it
is quiescent) into the target bucket under BUCKET_PREFIX, whose key layout is identical to
the local_fs tree. s3 needs EXP4_S3_BUCKET set and the coordinator's ~/.aws/credentials
profile `neon-exp4`.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import deploy
import lib

DATA_DIR = lib.EXPERIMENT_DIR / "data"
STATE_FILE = DATA_DIR / "cluster_state_prod.json"
SWITCH_LOG = DATA_DIR / "backend_switch_prod.jsonl"
PROBE_LOG = DATA_DIR / "backend_probe_prod.jsonl"

BIN_CACHE = lib.SECRETS_DIR / "bin"
MINIO_ENV = lib.SECRETS_DIR / "minio.env"
# dl.min.io stopped serving community binaries (HTTP 410), so the MinIO server is built
# from source into BIN_CACHE (go install github.com/minio/minio@master) and node0 uses the AWS CLI v2 instead of mc.
AWSCLI_URL = "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
REMOTE_AWS = f"{lib.REMOTE_BIN}/aws"
PS = 0  # lib.PAGESERVER_NODE


def _private_write(path: Path, text: str):
    lib.SECRETS_DIR.mkdir(mode=0o700, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)
    path.write_text(text)


def _scp_private(node: int, local: Path, remote: str):
    lib.ssh(node, f"mkdir -p $(dirname {remote}) && touch {remote} && chmod 600 {remote}", timeout=20)
    lib.scp_to(node, local, remote)


def append_jsonl(path: Path, rec: dict):
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def prod_state() -> dict:
    return json.loads(STATE_FILE.read_text())


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------

def minio_creds() -> tuple[str, str]:
    if not MINIO_ENV.exists():
        user, pw = "exp4admin", secrets.token_urlsafe(24)
        _private_write(MINIO_ENV, f"MINIO_ROOT_USER={user}\nMINIO_ROOT_PASSWORD={pw}\n")
    kv = dict(line.split("=", 1) for line in MINIO_ENV.read_text().split())
    return kv["MINIO_ROOT_USER"], kv["MINIO_ROOT_PASSWORD"]


def s3_creds() -> tuple[str, str] | None:
    cfg = configparser.ConfigParser()
    cfg.read(Path.home() / ".aws" / "credentials")
    if lib.S3_PROFILE not in cfg:
        return None
    sec = cfg[lib.S3_PROFILE]
    return sec["aws_access_key_id"], sec["aws_secret_access_key"]


def push_node0_creds():
    """~/.aws/credentials + ~/.aws/config on node0 (chmod 600), used by the pageserver via
    AWS_PROFILE and by the aws CLI for mirroring and presigning."""
    creds = {"minio": minio_creds()}
    s3 = s3_creds()
    if s3:
        creds["s3"] = s3

    aws = "".join(f"[{lib.NODE0_AWS_PROFILE[b]}]\naws_access_key_id = {ak}\naws_secret_access_key = {sk}\n\n"
                  for b, (ak, sk) in creds.items())
    local = lib.SECRETS_DIR / "node0_aws_credentials"
    _private_write(local, aws)
    _scp_private(PS, local, f"{lib.REMOTE_HOME}/.aws/credentials")

    conf = (f"[profile {lib.NODE0_AWS_PROFILE['minio']}]\nregion = {lib.S3_REGION}\n"
            f"endpoint_url = {lib.minio_endpoint()}\ns3 =\n  addressing_style = path\n\n"
            f"[profile {lib.NODE0_AWS_PROFILE['s3']}]\nregion = {lib.S3_REGION}\n")
    local = lib.SECRETS_DIR / "node0_aws_config"
    _private_write(local, conf)
    _scp_private(PS, local, f"{lib.REMOTE_HOME}/.aws/config")


# --------------------------------------------------------------------------
# long remote commands: detach + poll, since long-held SSH sessions drop
# --------------------------------------------------------------------------

def run_remote_long(node: int, cmd: str, name: str, timeout_s: float) -> tuple[int, float, str]:
    marker = f"{lib.REMOTE_SVC}/{name}.rc"
    log = f"{lib.REMOTE_SVC}/{name}.log"
    lib.ssh(node, f"mkdir -p {lib.REMOTE_SVC} && rm -f {marker}", timeout=20)
    t0 = time.time()
    lib.ssh_background(node, f"{cmd}; echo $? > {marker}", log)
    deadline = t0 + timeout_s
    while time.time() < deadline:
        proc = lib.ssh(node, f"cat {marker} 2>/dev/null || true", timeout=20, check=False)
        if proc.stdout.strip():
            dt = time.time() - t0
            tail = lib.ssh(node, f"tail -c 3000 {log}", timeout=20, check=False).stdout
            return int(proc.stdout.strip()), dt, tail
        time.sleep(10)
    raise TimeoutError(f"{name} on node{node} did not finish within {timeout_s}s")


# --------------------------------------------------------------------------
# setup-minio
# --------------------------------------------------------------------------

def install_awscli_node0():
    if lib.ssh(PS, f"test -x {REMOTE_AWS} && echo y", timeout=20, check=False).stdout.strip() == "y":
        return
    BIN_CACHE.mkdir(parents=True, exist_ok=True)
    z = BIN_CACHE / "awscliv2.zip"
    if not z.exists():
        subprocess.run(["curl", "-fsSL", "-o", str(z), AWSCLI_URL], check=True, timeout=600)
    lib.rsync_to(PS, str(z), f"{lib.REMOTE_HOME}/awscliv2.zip")
    lib.ssh(PS, f"cd {lib.REMOTE_HOME} && rm -rf aws && unzip -q awscliv2.zip && "
                f"./aws/install -i {lib.REMOTE_HOME}/aws-cli -b {lib.REMOTE_BIN} --update && "
                f"{REMOTE_AWS} --version", timeout=300)


def aws_node0(backend: str, args: str) -> str:
    return f"{REMOTE_AWS} --profile {lib.NODE0_AWS_PROFILE[backend]} {args}"


def minio_live() -> bool:
    proc = lib.ssh(PS, f"curl -s -o /dev/null -w '%{{http_code}}' {lib.minio_endpoint()}/minio/health/live",
                   timeout=20, check=False)
    return proc.stdout.strip() == "200"


def setup_minio():
    deploy.step(f"MinIO on node{lib.MINIO_NODE}:{lib.MINIO_PORT}, data {lib.MINIO_DATA}")
    if not (BIN_CACHE / "minio").exists():
        raise RuntimeError(f"build MinIO into {BIN_CACHE}/minio first (go install github.com/minio/minio@master)")
    lib.rsync_to(lib.MINIO_NODE, str(BIN_CACHE / "minio"), f"{lib.REMOTE_BIN}/minio")
    install_awscli_node0()
    minio_creds()
    remote_env = f"{lib.REMOTE_SVC}/minio.env"
    _scp_private(lib.MINIO_NODE, MINIO_ENV, remote_env)
    push_node0_creds()
    if not deploy.is_running(lib.MINIO_NODE, "minio server"):
        lib.ssh(lib.MINIO_NODE, f"mkdir -p {lib.MINIO_DATA}", timeout=20)
        cmd = (f"env MINIO_CONFIG_ENV_FILE={remote_env} {lib.REMOTE_BIN}/minio server {lib.MINIO_DATA} "
               f"--address 0.0.0.0:{lib.MINIO_PORT} --console-address 127.0.0.1:9001")
        lib.ssh_background(lib.MINIO_NODE, cmd, f"{lib.REMOTE_SVC}/minio.log")
    for _ in range(30):
        if minio_live():
            break
        time.sleep(2)
    else:
        raise RuntimeError("MinIO never became live; see node3:~/svc/minio.log")
    lib.ssh(PS, aws_node0("minio", f"s3 mb s3://{lib.MINIO_BUCKET}") + " || true", timeout=60, check=False)
    lib.ssh(PS, aws_node0("minio", f"s3 ls s3://{lib.MINIO_BUCKET}"), timeout=60)
    print("  MinIO live, bucket ready")


# --------------------------------------------------------------------------
# switch
# --------------------------------------------------------------------------

def current_backend() -> str:
    text = lib.ssh(PS, "grep '^remote_storage' /mydata/ps/pageserver.toml", timeout=20).stdout
    for b in lib.BACKENDS:
        try:
            if lib.remote_storage_toml(b) in text:
                return b
        except RuntimeError:
            continue
    raise RuntimeError(f"unrecognised remote_storage line: {text.strip()}")


def list_index_parts(backend: str, tenant_id: str, timeline_id: str) -> list[str]:
    rel = f"tenants/{tenant_id}/timelines/{timeline_id}/"
    if backend == "localfs":
        cmd = f"ls {lib.LOCALFS_REMOTE_DIR}/{rel}"
    else:
        cmd = aws_node0(backend, f"s3 ls s3://{lib.backend_bucket(backend)}/{lib.BUCKET_PREFIX}/{rel}")
    out = lib.ssh(PS, cmd, timeout=60, check=False).stdout
    return sorted(w for w in out.split() if w.startswith("index_part.json"))


def tenant_state(tenant_id: str) -> str | None:
    try:
        body, status = lib._curl_raw(lib.STORCON_NODE, "GET", f"{lib.PS_HTTP_BASE}/v1/tenant/{tenant_id}",
                                     timeout=15)
    except RuntimeError:  # connection refused while the pageserver is still starting
        return None
    if status != "200":
        return None
    return (json.loads(body).get("state") or {}).get("slug")


def switch(backend: str):
    st = prod_state()
    tenant_id, root_tl = st["tenant_id"], st["root_timeline_id"]
    if backend != "localfs":
        push_node0_creds()
        install_awscli_node0()
        if backend == "minio" and not minio_live():
            raise RuntimeError("MinIO not live -- run setup-minio first")
    idx_before = list_index_parts(backend, tenant_id, root_tl) if backend != "localfs" else None

    deploy.stop_pageserver()

    copy_s = None
    if backend != "localfs":
        deploy.step(f"mirroring {lib.LOCALFS_REMOTE_DIR} -> {backend}")
        dst = f"s3://{lib.backend_bucket(backend)}/{lib.BUCKET_PREFIX}/"
        rc, copy_s, tail = run_remote_long(
            PS, aws_node0(backend, f"s3 sync --only-show-errors {lib.LOCALFS_REMOTE_DIR}/ {dst}"),
            f"mirror_{backend}", timeout_s=4 * 3600)
        if rc != 0:
            raise RuntimeError(f"aws s3 sync failed rc={rc}:\n{tail}")
        print(f"  mirrored in {copy_s:.0f}s", flush=True)
        idx_before = list_index_parts(backend, tenant_id, root_tl)

    deploy.sync_config(backend)
    t0 = time.time()
    deploy.start_pageserver(backend)
    deploy.wait_for_registration(timeout_s=300)
    while tenant_state(tenant_id) != "Active":
        if time.time() - t0 > 600:
            raise TimeoutError(f"tenant {tenant_id} not Active after 600s on {backend}")
        time.sleep(0.5)
    attach_s = time.time() - t0
    print(f"  tenant Active {attach_s:.1f}s after pageserver start", flush=True)

    time.sleep(10)
    idx_after = list_index_parts(backend, tenant_id, root_tl)
    errors = lib.ssh(PS, f"grep -cE ' (ERROR|WARN) .*(remote|s3|S3|download|upload)' "
                         f"{lib.REMOTE_SVC}/pageserver.log || true", timeout=20, check=False).stdout.strip()
    rec = {"backend": backend, "copy_s": copy_s, "attach_s": attach_s,
           "index_parts_before": idx_before, "index_parts_after": idx_after,
           "remote_error_lines": int(errors or 0), "ts": time.time()}
    append_jsonl(SWITCH_LOG, rec)
    print(json.dumps(rec, indent=1))
    if rec["remote_error_lines"]:
        print("  WARNING: remote-storage errors in pageserver.log -- inspect before sweeping")


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------

def _pick_objects(tenant_id: str, root_tl: str) -> tuple[str, str]:
    """(small, large) keys relative to the backend root, taken from the localfs tree whose
    layout every backend shares: the root timeline's newest index_part and the tenant's
    largest layer file."""
    idx = [p for p in list_index_parts("localfs", tenant_id, root_tl)]
    small = f"tenants/{tenant_id}/timelines/{root_tl}/{idx[-1]}"
    out = lib.ssh(PS, f"cd {lib.LOCALFS_REMOTE_DIR} && find tenants/{tenant_id} -type f -printf '%s %p\\n' "
                      f"| sort -n | tail -1", timeout=60).stdout.split()
    return small, out[1]


def _url(backend: str, key: str) -> str:
    """localfs: file:// URL. minio/s3: presigned GET URL (1h) from the aws CLI on node0 --
    curl 7.81's --aws-sigv4 omits x-amz-content-sha256, which both MinIO and S3 reject (403)."""
    if backend == "localfs":
        return f"file://{lib.LOCALFS_REMOTE_DIR}/{key}"
    s3url = f"s3://{lib.backend_bucket(backend)}/{lib.BUCKET_PREFIX}/{key}"
    return lib.ssh(PS, aws_node0(backend, f"s3 presign {shlex.quote(s3url)} --expires-in 3600"),
                   timeout=60).stdout.strip()


def _curl_base(backend: str) -> list[str]:
    return ["curl", "-s", "-S"]


def drop_caches(nodes):
    for n in nodes:
        lib.ssh(n, "sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'", timeout=60)


def probe(backend: str, n_small: int = 200, n_large: int = 3):
    import numpy as np
    st = prod_state()
    small, large = _pick_objects(st["tenant_id"], st["root_timeline_id"])

    args = _curl_base(backend) + ["-w", "%{time_total} %{time_connect} %{http_code}\\n"]
    small_url = _url(backend, small)
    for _ in range(n_small):
        args += ["-o", "/dev/null", small_url]
    lines = lib.ssh(PS, " ".join(shlex.quote(a) for a in args), timeout=600).stdout.split("\n")
    rows = [l.split() for l in lines if l.strip()]
    codes = {r[2] for r in rows}
    if backend != "localfs" and codes != {"200"}:
        raise RuntimeError(f"small-object probe got HTTP codes {codes}")
    t_ms = np.array([float(r[0]) * 1e3 for r in rows])

    large_url = _url(backend, large)
    large_runs = []
    for _ in range(n_large):
        drop_caches([PS] + ([lib.MINIO_NODE] if backend == "minio" else []))
        a = _curl_base(backend) + ["-o", "/dev/null", "-w",
                                   "%{time_total} %{speed_download} %{size_download} %{http_code}",
                                   large_url]
        out = lib.ssh(PS, " ".join(shlex.quote(x) for x in a), timeout=1800).stdout.split()
        large_runs.append({"time_s": float(out[0]), "MBps": float(out[1]) / 1e6,
                           "bytes": int(out[2]), "http": out[3]})

    rec = {"backend": backend, "small_key": small, "large_key": large,
           "small_first_ms": float(t_ms[0]),
           "small_p50_ms": float(np.percentile(t_ms[1:], 50)),
           "small_p99_ms": float(np.percentile(t_ms[1:], 99)),
           "small_mean_ms": float(t_ms[1:].mean()),
           "large": large_runs, "ts": time.time()}
    append_jsonl(PROBE_LOG, rec)
    print(json.dumps(rec, indent=1))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup-minio")
    sub.add_parser("current")
    for c in ("switch", "probe"):
        sub.add_parser(c).add_argument("backend", choices=lib.BACKENDS)
    args = ap.parse_args()
    if args.cmd == "setup-minio":
        setup_minio()
    elif args.cmd == "current":
        print(current_backend())
    elif args.cmd == "switch":
        switch(args.backend)
    elif args.cmd == "probe":
        probe(args.backend)


if __name__ == "__main__":
    main()
