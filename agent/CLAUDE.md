# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Neon: serverless Postgres that separates compute from storage. It is a Rust workspace (~50 crates),
a patched PostgreSQL tree (`vendor/postgres-v14..v17`, git submodules), C Postgres extensions
(`pgxn/`), and a Python integration test suite (`test_runner/`). All three build systems are wired
together through the top-level `Makefile`.

## Build

```bash
make -j`nproc` -s                       # debug build: postgres + neon extensions + all Rust crates
BUILD_TYPE=release make -j`nproc` -s    # release build
CARGO_BUILD_FLAGS="--features=testing" make   # required before running the Python test suite
make clean / make distclean             # distclean also removes pg_install/, build/ and runs cargo clean
```

`make` builds Postgres for **all four** supported versions (v14–v17) into `pg_install/`. This is slow;
once Postgres is installed and unchanged, iterate with plain `cargo build` / `cargo check`.
`libs/postgres_ffi` depends on the installed Postgres headers, so touching `vendor/postgres-*` forces
a rebuild of a large part of the workspace.

`PG_INSTALL_CACHED=1` skips the Postgres build entirely (used by CI when restoring a cache).

Cargo aliases (`.cargo/config.toml`):
- `cargo neon ...` → runs the `neon_local` binary
- `cargo build_testing` → `cargo build --features testing`

## Test

### Rust
Use `cargo nextest`, not `cargo test` — some crates no longer support plain `cargo test`.

```bash
cargo nextest run                                   # whole workspace
cargo nextest run -E 'package(pageserver)'          # one crate
cargo nextest run -E 'test(test_name_substring)'    # one test
```
Nextest is configured with a 60s slow-test timeout (`.config/nextest.toml`).

### Python integration tests
Requires a build with `--features testing` (failpoints, test-only HTTP APIs).

```bash
./scripts/pysync                                    # install/refresh the poetry venv
./scripts/pytest                                    # everything (debug+release × all PG versions)
DEFAULT_PG_VERSION=17 BUILD_TYPE=release ./scripts/pytest    # one permutation — what you usually want
./scripts/pytest test_runner/regress/test_compaction.py -k test_name -s --log-cli-level=INFO
./scripts/pytest -n4                                # parallel (pytest-xdist)
```
`./scripts/pytest` is a thin wrapper over `poetry run pytest`. Test state and logs land in
`test_output/`; performance tests under `test_runner/performance` are excluded by default
(`pytest.ini`), as are `remote_cluster`-marked tests.

Tests are built on the `neon_env_builder` fixture (`test_runner/fixtures/neon_fixtures.py`), which
spins up a full local cluster (pageserver(s), safekeepers, storage broker, storage controller,
endpoints) per test via `neon_local`. A test **fails if an unexpected ERROR/WARN appears in a service
log**; expected ones must be added to `env.pageserver.allowed_errors` (or the equivalent for other
services) — see `test_runner/fixtures/pageserver/allowed_errors.py`.

Remote storage in tests defaults to `LOCAL_FS`/`MOCK_S3` (moto); `ENABLE_REAL_S3_REMOTE_STORAGE`
switches to real S3. See `test_runner/README.md` for the full env-var list.

## Lint / format

```bash
./scripts/reformat        # cargo fmt + ruff check --fix + ruff format
./run_clippy.sh           # cargo clippy --all-features, with -D warnings -D clippy::todo
cargo fmt --all -- --check
poetry run ruff check . && poetry run ruff format --check . && poetry run mypy .   # mypy MUST run from repo root
cargo deny check          # dependency licenses/advisories
make lint-openapi-spec    # redocly lint on all openapi_spec.y*ml
```

`make setup-pre-commit-hook` installs `pre-commit.py` (rustfmt + Python checks on staged files).

Adding a Cargo dependency requires regenerating the workspace-hack crate, or CI fails:
```bash
cargo hakari generate && cargo hakari manage-deps   # commit Cargo.lock + workspace_hack/
```

## Running a local cluster

```bash
cargo neon init          # writes .neon/ with paths + config
cargo neon start         # storage broker, pageserver, safekeeper, storage controller
cargo neon tenant create --set-default
cargo neon endpoint create main && cargo neon endpoint start main
psql -p 55432 -h 127.0.0.1 -U cloud_admin postgres
cargo neon stop
```
If init/start misbehaves, `cargo neon stop` and delete `.neon/` before retrying. `cargo neon start`
also launches a vanilla Postgres on port 1235 hosting the storage controller's database.

## Architecture

Compute nodes are stateless Postgres. The `neon` extension (`pgxn/neon`) replaces Postgres' smgr:
instead of reading local files it issues **GetPage@LSN** requests to the pageserver, and instead of
writing WAL to local disk the walproposer (also in `pgxn/neon`, exposed to Rust as a static lib via
`libs/walproposer`) streams it to the safekeepers.

- **safekeeper/** — Paxos-based quorum WAL service. A WAL record is durable once a majority of
  safekeepers have it. Protocol: `docs/safekeeper-protocol.md`, `docs/walservice.md`.
- **pageserver/** — ingests WAL from safekeepers (`walingest.rs`, `tenant/timeline/walreceiver/`),
  reorders it per key, and materializes pages on demand (`walredo/` runs a real Postgres process in
  WAL-redo mode via `pgxn/neon_walredo`). Storage is immutable **layer files**: in-memory → L0 delta
  layers (whole keyspace, LSN range) → compacted into L1 (narrow keyspace, wide LSN range) → uploaded
  to S3/GCS/Azure via `libs/remote_storage`, with GC removing layers no timeline needs.
  See `docs/pageserver-storage.md`, `docs/pageserver-compaction.md`.
- **storage_controller/** — maps tenants to pageserver *shards*, owns generations, secondary
  locations, live migration and shard splits via an intent/reconcile loop. Keeps most state in memory
  and only persists objects (not relationships) in Postgres via `diesel`; see `persistence.rs` and
  `docs/storage_controller.md`. `storcon_cli` is its admin CLI.
- **storage_broker/** — pub/sub between safekeepers and pageservers (who has what WAL).
- **proxy/** — Postgres wire-protocol proxy: SNI/password-based routing, auth against the control
  plane, connection pooling, plus the HTTP/WebSocket "serverless" SQL-over-HTTP driver endpoint.
- **compute_tools/** (`compute_ctl`) — supervises Postgres inside a compute container: applies the
  compute spec, runs catalog migrations, installs extensions, exposes an HTTP control API.
- **control_plane/** (`neon_local`) — dev/test-only control plane; the thing behind `cargo neon`.
- **storage_scrubber/** — offline consistency checks + garbage collection over remote storage.
- **endpoint_storage/** — object storage for per-endpoint data (e.g. LFC prewarm state).
- **libs/** — shared crates. Most load-bearing: `pageserver_api` (keyspace, shard identity, HTTP/
  wire models — changing it affects both pageserver and storage controller), `postgres_ffi`
  (version-gated bindings to the Postgres on-disk formats), `wal_decoder`, `utils` (Lsn, ids, tracing,
  http helpers), `remote_storage`, `pq_proto`/`postgres_backend` (server side of the wire protocol),
  `libs/proxy/*` (forked postgres-protocol/tokio-postgres used by proxy only).

Key vocabulary: *tenant* (a Neon project's storage), *timeline* (a branch, identified by a timeline
id, with an optional ancestor + branchpoint LSN), *shard* (a slice of a tenant's keyspace on one
pageserver), *basebackup* (tarball the pageserver generates to boot a compute — unrelated to
`pg_basebackup`), *generation* (fencing number preventing split-brain writes to S3).
`docs/glossary.md` is the authoritative list.

## Conventions

- **`/* BEGIN_HADRON */ ... /* END_HADRON */` markers** delimit this fork's divergence from upstream
  `neondatabase/neon` (~100 sites across pageserver, safekeeper, proxy, compute_tools, tests). Keep
  new fork-local changes inside such blocks so upstream merges stay tractable.
- **Error logging** (`docs/error-handling.md`): log an error where you *handle* it, never where you
  merely propagate it. `anyhow` is used widely; use typed errors where callers must distinguish cases.
- Postgres-derived code and docs use `MB` to mean 1024*1024 (matching Postgres, not SI).
- `clippy.toml` disallows `tokio::task::block_in_place`, `futures::pin_mut`, and
  `tokio_epoll_uring::thread_local_system` (use pageserver's `tokio_epoll_uring_ext`).
- Rust toolchain is pinned to the version in `rust-toolchain.toml`; edition 2024.
- Test-only code paths sit behind the `testing` cargo feature (failpoints via the `fail` crate).
- Storage controller schema changes need a `diesel migration generate` + committed `schema.rs`.
- Major design changes are written up as RFCs in `docs/rfcs/`.

## Experiments

Any exploratory/benchmark/performance-investigation task (not a regular code change) gets its own
numbered directory under `experiments/`, named `<n>-<short-title>` (e.g. `1-branch-interference-local`),
where `<n>` is the next unused integer. Each experiment directory contains:

- A markdown doc (typically `README.md`) recording: what the experiment does, the end goal /
  hypothesis, exact steps to reproduce the results (commands, configuration, environment), and
  takeaways (including negative or inconclusive results — don't omit them).
- A `log.md` recording the detailed *process*, as distinct from `README.md`'s outcome-oriented
  narrative: every setup/modification step taken (scripts written or changed, configs deployed,
  commands run), and *why* each step was taken — including false starts, bugs hit and how they were
  diagnosed/fixed, and deviations from the original plan, in roughly chronological order. Update it
  as the experiment progresses, not just at the end.
- The raw experiment data (or a pointer to where it lives, if too large to commit).
- Figures, if requested or if they materially clarify the result.

This is a standing policy for this directory, not a one-off — apply it to every future experiment
without being asked again.

## CloudLab multi-node cluster deployment (lessons from experiment 2)

Hand-deploying Neon's services across several physically separate CloudLab nodes (as opposed to
a single-box `neon_local` stack) is meaningfully different and has real gotchas. Full narrative,
topology and scripts: `experiments/2-branch-interference-cluster/README.md` and its `scripts/`
(`lib.py`, `setup_cluster.py`). Hardware/network/filesystem specifics for the `m400` CloudLab
profile: `agent/cloudlab.md` — re-verify those numbers on every fresh provision, they change
(hostnames, free disk) on reprovision.

### Topology used (6 nodes, adapt node count/roles as needed)

| Node | Role |
|---|---|
| node0 | pageserver only (device under test) |
| node1 | storage_broker + storage_controller + its own Postgres (storcon's metadata DB) + safekeeper + a compute-hook stub |
| node2 | a persistent compute (endpoint) |
| node3–5 | load-generator / client nodes |

Bring-up order: storage_broker → storage_controller's own Postgres → compute-hook stub →
storage_controller → safekeeper → pageserver → create tenant/timeline via the storage_controller
HTTP API → hand-launch `compute_ctl` over SSH on the compute node(s). No `neon_local` is involved
once services are spread across real nodes — everything is driven by hand-built `config.json`s and
direct HTTP/SSH calls (see `lib.py`'s `tenant_create`/`timeline_create_root`/`endpoint_start`).

### Watch-outs

- **Do management-API calls from a node with private-LAN access, not an off-cluster
  coordinator.** Plain `requests` calls from a coordinator machine outside the CloudLab private LAN
  hang/timeout against the storage_controller/pageserver HTTP APIs; route every management call
  through `curl` over SSH to a LAN-resident node instead.
- **`/mydata` is root-owned by default** on a fresh CloudLab instance, on every node — `chown` it
  before anything tries to write there.
- **Install protoc yourself; don't trust apt.** Ubuntu 22.04's `protoc` package (3.12.4) predates
  proto3 `optional` and fails `storage_broker`'s build. Install a matching-arch (e.g. aarch64) protoc
  from the protobuf GitHub releases, **including its `include/` dir of well-known types** (not just
  the binary — a bare binary copy is not enough), and put it ahead of apt's on `PATH`.
- **`compute_ctl` needs `LD_LIBRARY_PATH` set in its own process environment**, not only baked into
  the `postgres` binary's rpath — otherwise the `neon.so` extension fails to load
  (`libpq.so.5: cannot open shared object file`).
- **`spec.safekeepers_generation` must be `null`**, not a real generation number, for a hand-deployed
  compute talking to a safekeeper that was never registered via `timelines_onto_safekeepers` —
  a real value makes walproposer send `allow_timeline_creation=false` and the safekeeper refuses the
  timeline permanently.
- **Generate a fresh `config.json` template from a throwaway single-node `neon_local` stack on the
  coordinator**, matching the exact checkout's schema — don't trust an older docker-compose
  reference spec as a working template as-is; the shape drifts across versions.
- **Wrap every remote load-generator invocation (e.g. `pagebench`) in GNU `timeout`.** Under severe
  pageserver contention a client can block indefinitely waiting on a request that will never be
  serviced; the tool's own `--runtime`/deadline flag does not protect against this, and an unwrapped
  SSH call can hang for over an hour with the remote process idling at 0% CPU (network-blocked, not
  looping). Use a generous grace window (e.g. runtime + soft timeout + hard-kill) so a genuine
  extreme-tail measurement isn't silently turned into an empty one.
- **A handful of 8-core nodes saturate fast.** On `m400` hardware, ~20 concurrently pgbench-driven
  Postgres instances on one node is already enough to make the node itself the bottleneck — budget
  load-generator placement accordingly, and expect generic node-level contention to dominate over
  more subtle tenant/branch-scoped effects at this scale (see experiment 2's headline result).
- **`/proj/<project>` (NFS) is small and shared** — check free space before assuming a large build or
  dataset fits; keep large build trees (a full Neon build is ~8.7 GB) on a node's local disk and only
  keep source + stripped binaries on the NFS mount so they survive teardown.
