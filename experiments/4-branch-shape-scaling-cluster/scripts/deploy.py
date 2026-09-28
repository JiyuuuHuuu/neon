#!/usr/bin/env python3
"""
Phase 1: deploy binaries + bring up services on CloudLab cluster 2 (8x c220g5).

No build happens on the cluster -- the coordinator is x86_64 Ubuntu 22.04 (glibc
2.35), ABI-identical to the c220g5 nodes, and /mydata/jiyu/neon/target/release
already holds a `features: ["testing"]` release build (required: the manual
checkpoint/compact/do_gc HTTP endpoints setup_root.py and materialize.py depend on
are testing_api_handler-gated). So this script rsyncs binaries instead of repeating
experiment 2's aarch64-build-plus-protoc saga.

The service bring-up order and exact command lines were recovered by SSHing into
CloudLab cluster 1 (still running experiment 2's stack) after discovering
agent/experiment-2-cluster-plan.md and agent/experiment-2-progress.md (which
originally documented this) no longer exist and aren't in git history -- see
README.md's "Recovered: the lost deployment runbook" section. This script commits
that runbook as code so it can't be lost again.

Usage:
    python3 deploy.py             # full bring-up, idempotent (safe to re-run)
    python3 deploy.py --binaries-only   # skip service bring-up, just sync binaries
    python3 deploy.py --status    # check what's currently running where
"""
from __future__ import annotations

import argparse
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import lib

REPO_RELEASE = lib.REPO_ROOT / "target" / "release"
REPO_PG_INSTALL_V17 = lib.REPO_ROOT / "pg_install" / "v17"

# binary -> list of nodes that need it
BINARY_PLACEMENT = {
    "pageserver": [lib.PAGESERVER_NODE],
    "safekeeper": [lib.STORCON_NODE],
    "storage_broker": [lib.STORCON_NODE],
    "storage_controller": [lib.STORCON_NODE],
    "storcon_cli": [lib.STORCON_NODE],
    "compute_ctl": sorted(set([lib.ROOT_NODE] + lib.DIVERGENCE_NODES + lib.COMPUTE_TIER_NODES)),
    "pagebench": lib.LOAD_NODES,
}
# pg_install/v17 (postgres, pgbench, psql, initdb, pg_isready, ...) needed wherever
# a compute or the storcon's own metadata Postgres runs.
PG_INSTALL_NODES = sorted(set([lib.PAGESERVER_NODE, lib.STORCON_NODE, lib.ROOT_NODE]
                               + lib.DIVERGENCE_NODES + lib.COMPUTE_TIER_NODES))

ALL_NODES = list(lib.NODES.keys())


def step(msg):
    print(f"\n== {msg} ==", flush=True)


def chown_mydata():
    step("chown /mydata on every node (fresh CloudLab instances default to root:root)")
    for node in ALL_NODES:
        lib.ssh(node, f"sudo chown -R {lib.SSH_USER}:$(id -gn {lib.SSH_USER}) /mydata", timeout=30)


def sync_binaries():
    step("rsync binaries + pg_install/v17")
    for node in ALL_NODES:
        lib.ssh(node, f"mkdir -p {lib.REMOTE_BIN} {lib.REMOTE_SVC}", timeout=20)

    for binary, nodes in BINARY_PLACEMENT.items():
        src = REPO_RELEASE / binary
        if not src.exists():
            raise RuntimeError(f"{src} does not exist -- build it first "
                                f"(BUILD_TYPE=release CARGO_BUILD_FLAGS=--features=testing make)")
        for node in nodes:
            print(f"  {binary} -> node{node}", flush=True)
            lib.rsync_to(node, str(src), f"{lib.REMOTE_BIN}/{binary}")
            lib.ssh(node, f"chmod +x {lib.REMOTE_BIN}/{binary}", timeout=15)

    for node in PG_INSTALL_NODES:
        print(f"  pg_install/v17 -> node{node}", flush=True)
        lib.ssh(node, f"mkdir -p {lib.REMOTE_BIN}/pg_install", timeout=15)
        lib.rsync_to(node, str(REPO_PG_INSTALL_V17) + "/", f"{lib.REMOTE_BIN}/pg_install/v17/")


def sync_config(backend: str = "localfs"):
    step(f"write pageserver.toml (backend={backend}) + identity.toml on node0, compute_hook_stub.py on node1")
    tmpl = (lib.CONFIG_DIR / "pageserver.toml.tmpl").read_text()
    ps_toml = (tmpl.replace("__REMOTE_BIN__", lib.REMOTE_BIN)
                    .replace("__REMOTE_HOME__", lib.REMOTE_HOME)
                    .replace("__REMOTE_STORAGE__", lib.remote_storage_toml(backend))
                    .replace("__STORCON_IP__", lib.node_ip(lib.STORCON_NODE)))
    local_tmp = lib.CONFIG_DIR / "_tmp_pageserver.toml"
    local_tmp.write_text(ps_toml)
    lib.ssh(lib.PAGESERVER_NODE, "mkdir -p /mydata/ps", timeout=20)
    lib.scp_to(lib.PAGESERVER_NODE, local_tmp, "/mydata/ps/pageserver.toml")
    local_tmp.unlink()
    lib.ssh(lib.PAGESERVER_NODE, "echo 'id=1' > /mydata/ps/identity.toml", timeout=15)

    lib.scp_to(lib.STORCON_NODE, lib.CONFIG_DIR / "compute_hook_stub.py",
               f"{lib.REMOTE_SVC}/compute_hook_stub.py")


def is_running(node: int, pattern: str) -> bool:
    bracketed = f"[{pattern[0]}]{pattern[1:]}"
    proc = lib.ssh(node, f"pgrep -f {shlex.quote(bracketed)} || true", timeout=15, check=False)
    return bool(proc.stdout.strip())


def start_broker():
    step(f"storage_broker on node{lib.STORCON_NODE}")
    if is_running(lib.STORCON_NODE, "storage_broker"):
        print("  already running")
        return
    cmd = f"{lib.REMOTE_BIN}/storage_broker --listen-addr=0.0.0.0:50051"
    lib.ssh_background(lib.STORCON_NODE, cmd, f"{lib.REMOTE_SVC}/broker.log")


def start_storcon_postgres():
    step(f"storcon's own metadata Postgres on node{lib.STORCON_NODE}:1235")
    if is_running(lib.STORCON_NODE, "storcon_db"):
        print("  already running")
        return
    pgbin = f"{lib.REMOTE_BIN}/pg_install/v17/bin"
    ld = f"{lib.REMOTE_BIN}/pg_install/v17/lib"
    lib.ssh(lib.STORCON_NODE, f"rm -rf /mydata/storcon_db && mkdir -p /mydata/storcon_db", timeout=20)
    lib.ssh(lib.STORCON_NODE,
            f"env LD_LIBRARY_PATH={ld} {pgbin}/initdb -D /mydata/storcon_db -U {lib.SSH_USER}",
            timeout=60)
    cmd = (f"env LD_LIBRARY_PATH={ld} {pgbin}/postgres -D /mydata/storcon_db "
           f"-p 1235 -c listen_addresses=localhost")
    lib.ssh_background(lib.STORCON_NODE, cmd, f"{lib.REMOTE_SVC}/storcon_pg.log")
    time.sleep(3)
    lib.ssh(lib.STORCON_NODE,
            f"env LD_LIBRARY_PATH={ld} {pgbin}/createdb -h localhost -p 1235 -U {lib.SSH_USER} "
            f"storage_controller", timeout=30, check=False)


def start_compute_hook_stub():
    step(f"compute-hook stub on node{lib.STORCON_NODE}:9999")
    if is_running(lib.STORCON_NODE, "compute_hook_stub.py"):
        print("  already running")
        return
    cmd = f"python3 {lib.REMOTE_SVC}/compute_hook_stub.py"
    lib.ssh_background(lib.STORCON_NODE, cmd, f"{lib.REMOTE_SVC}/compute_hook.log")


def start_storage_controller():
    step(f"storage_controller on node{lib.STORCON_NODE}:1234")
    if is_running(lib.STORCON_NODE, "storage_controller"):
        print("  already running")
        return
    ip = lib.node_ip(lib.STORCON_NODE)
    cmd = (f"{lib.REMOTE_BIN}/storage_controller --dev --listen 0.0.0.0:1234 "
           f"--database-url postgresql://{lib.SSH_USER}@localhost:1235/storage_controller "
           f"--control-plane-url http://{ip}:9999")
    lib.ssh_background(lib.STORCON_NODE, cmd, f"{lib.REMOTE_SVC}/storcon.log")


def start_safekeeper():
    step(f"safekeeper on node{lib.STORCON_NODE}:5454")
    if is_running(lib.STORCON_NODE, "safekeeper"):
        print("  already running")
        return
    ip = lib.node_ip(lib.STORCON_NODE)
    lib.ssh(lib.STORCON_NODE, "mkdir -p /mydata/sk /mydata/sk-remote", timeout=20)
    # ssh_background wraps this in an explicit `bash -c '<remote_cmd>'`, which is
    # itself parsed by a shell (in addition to sshd's own implicit shell parse of
    # the outer command) -- a literal '"' in remote_cmd would be consumed as shell
    # quoting syntax by that inner parse and never reach safekeeper's argv. Escaping
    # it as \" survives both parses (shlex.quote's outer single-quotes protect the
    # backslash from the first parse; the second parse's backslash-escape then
    # yields a literal '"' in the final argv) -- see the comment this cost in
    # lib.py's REMOTE_HOME docstring for the sibling gotcha with '~'.
    cmd = (f"{lib.REMOTE_BIN}/safekeeper -D /mydata/sk --id=1 --listen-pg=0.0.0.0:5454 "
           f"--advertise-pg={ip}:5454 --listen-http=0.0.0.0:7676 "
           f"--broker-endpoint=http://{ip}:50051 --availability-zone=lan "
           f'--remote-storage={{local_path=\\"/mydata/sk-remote\\"}}')
    lib.ssh_background(lib.STORCON_NODE, cmd, f"{lib.REMOTE_SVC}/safekeeper.log")


def start_pageserver(backend: str = "localfs"):
    step(f"pageserver on node{lib.PAGESERVER_NODE}:9898/64000 (backend={backend})")
    if is_running(lib.PAGESERVER_NODE, "pageserver -D"):
        print("  already running")
        return
    lib.ssh(lib.PAGESERVER_NODE, "mkdir -p /mydata/ps/tenants", timeout=20)
    cmd = f"{lib.pageserver_env(backend)}{lib.REMOTE_BIN}/pageserver -D /mydata/ps"
    lib.ssh_background(lib.PAGESERVER_NODE, cmd, f"{lib.REMOTE_SVC}/pageserver.log")


def stop_pageserver(timeout_s: float = 180):
    step(f"stopping pageserver on node{lib.PAGESERVER_NODE}")
    lib.ssh_pkill(lib.PAGESERVER_NODE, "pageserver -D")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not is_running(lib.PAGESERVER_NODE, "pageserver -D"):
            return
        time.sleep(2)
    raise TimeoutError("pageserver did not exit after SIGTERM")


def wait_for_registration(timeout_s: float = 120):
    step("registering pageserver node with storage_controller and waiting for Active")
    j, _ = lib.curl_json(lib.STORCON_NODE, "GET", f"{lib.STORCON_BASE}/control/v1/node", timeout=15)
    if not j:
        lib.register_pageserver_node(node_id=1)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            j, _ = lib.curl_json(lib.STORCON_NODE, "GET", f"{lib.STORCON_BASE}/control/v1/node",
                                  timeout=15)
            if j and any(n.get("availability") == "Active" for n in j):
                print("  pageserver Active:", j)
                return
        except Exception as e:  # noqa: BLE001
            print("  (not ready yet:", e, ")")
        time.sleep(3)
    raise TimeoutError("pageserver never registered as Active with storage_controller")


def status():
    for node in ALL_NODES:
        for pat in ["pageserver -D", "safekeeper", "storage_broker", "storage_controller",
                    "storcon_db", "compute_hook_stub.py"]:
            if is_running(node, pat):
                print(f"node{node}: {pat} RUNNING")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binaries-only", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--skip-binaries", action="store_true")
    ap.add_argument("--backend", choices=lib.BACKENDS, default="localfs",
                    help="remote storage backend; minio/s3 need storage_backend.py setup first")
    args = ap.parse_args()

    if args.status:
        status()
        return

    chown_mydata()
    if not args.skip_binaries:
        sync_binaries()
    if args.binaries_only:
        return
    sync_config(args.backend)
    start_broker()
    start_storcon_postgres()
    start_compute_hook_stub()
    time.sleep(2)
    start_storage_controller()
    time.sleep(2)
    start_safekeeper()
    start_pageserver(args.backend)
    wait_for_registration()
    print("\n== deploy complete ==")


if __name__ == "__main__":
    main()
