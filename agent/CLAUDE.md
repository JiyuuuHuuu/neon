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
- The raw experiment data (or a pointer to where it lives, if too large to commit).
- Figures, if requested or if they materially clarify the result.

This is a standing policy for this directory, not a one-off — apply it to every future experiment
without being asked again.
