Neon cluster 1
```bash
ssh -i ~/.ssh/dassl_rsa JiyuHu23@ms0603.utah.cloudlab.us
ssh -i ~/.ssh/dassl_rsa JiyuHu23@ms0607.utah.cloudlab.us
ssh -i ~/.ssh/dassl_rsa JiyuHu23@ms0610.utah.cloudlab.us
ssh -i ~/.ssh/dassl_rsa JiyuHu23@ms0622.utah.cloudlab.us
ssh -i ~/.ssh/dassl_rsa JiyuHu23@ms0644.utah.cloudlab.us
ssh -i ~/.ssh/dassl_rsa JiyuHu23@ms0629.utah.cloudlab.us
```
Neon cluster 2
```bash
ssh JiyuHu23@c220g5-110915.wisc.cloudlab.us
ssh JiyuHu23@c220g5-110923.wisc.cloudlab.us
ssh JiyuHu23@c220g5-110924.wisc.cloudlab.us
ssh JiyuHu23@c220g5-110909.wisc.cloudlab.us
ssh JiyuHu23@c220g5-110920.wisc.cloudlab.us
ssh JiyuHu23@c220g5-110916.wisc.cloudlab.us
ssh JiyuHu23@c220g5-110913.wisc.cloudlab.us
ssh JiyuHu23@c220g5-110908.wisc.cloudlab.us
```

The ssh key is `~/.ssh/dassl_rsa`

between the cluster machines, they can be referred to as nodeX, where X is 0-X.

`ms` machines are `m400` machines: 	aarch64, 8 cores, in CloudLab Utah
`c220` machines: 	x86_64, 2×10-core (40 threads w/ HT), in CloudLab Wisconsin (`c220g5`).

Bulk disk storage at `/mydata`, best for storing bulk experiment data.
`/proj/rasl-PG0/JiyuHu23/` is a distributed file system mount shared within a cluster region. You should put code there and it will be auto-synced and persisted after node tear-down.

---

# Verified environment details

Measured by SSH on 2026-09-22 during experiment 2 setup. Re-check if the experiment is
re-instantiated — CloudLab reprovisioning can change hostnames, disk sizes and free space.

## m400-SPECIFIC (does not generalize to other CloudLab hardware types)

- **CPU/RAM:** aarch64, vendor APM (Applied Micro X-Gene), 8 cores, **1 thread per core**, 1 NUMA
  node, 62 GB usable RAM. All six machines identical.
- **OS:** Ubuntu 22.04.1 LTS (jammy), kernel 5.15, passwordless `sudo`.
- **Disk layout** — one 120 GB SATA **SSD** (`sda`, model XR0120GEBLT), **fully partitioned with no
  spare LVM extents** (`vgs` shows `VFree 0`), so you cannot grow `/mydata`:
  | mount | size | free | notes |
  |---|---|---|---|
  | `/` | 63 G | 57 G | `$HOME` = `/users/<user>` lives here, and it is **local disk, not NFS** |
  | `/mydata` | 39 G | 37 G | LVM `emulab-nodeN--bs`, local |
  | swap | 8 G | — | |
- **Disk performance:** ~**94 MB/s** sequential write (`dd conv=fsync`), ~**414 MB/s** read. This is
  a low-end SATA SSD — roughly 5–10x slower on writes than a typical NVMe. Plan write-heavy
  experiments around it.
- **Only `git` is preinstalled.** No rust/cargo, clang, cmake, or protoc.
- **apt has every common build dep** (`build-essential clang cmake flex bison libseccomp-dev
  libreadline-dev libcurl4-openssl-dev libssl-dev zlib1g-dev pkg-config libicu-dev lsof`) **except a
  usable protoc**: the jammy package is 3.12.4, which predates proto3 `optional` and will fail any
  build needing ≥3.15 (e.g. Neon's `storage_broker`). Install an **aarch64** protoc from the
  protobuf GitHub releases and put it ahead of apt's on `PATH`.
- If two machines are needed for compute-heavy work, note 8 cores is small: ~20 concurrently
  pgbench-driven Postgres instances per node is already enough to make the *node* the bottleneck.

## Generic to this CloudLab project/profile (likely stable across hardware types)

- **Private LAN:** `10.10.1.1` … `10.10.1.6` on interface `enp1s0d1`, mapping to `node0` … `node5`
  (six nodes ⇒ indices 0–5). Resolvable by short name via `/etc/hosts`. Measured **10 Gbps**,
  **78 µs** RTT. The public interface (`enp1s0`, `128.110.x.x`) is also 10 Gbps. Use the `10.10.1.x`
  LAN for all inter-service traffic.
- **Hostname → node mapping** (verified; changes on reprovision):
  `ms0603`=node0, `ms0607`=node1, `ms0610`=node2, `ms0622`=node3, `ms0644`=node4, `ms0629`=node5.
- **`/proj/rasl-PG0`** — NFS (`ops.utah.cloudlab.us:/proj/rasl-PG0`), visible and writable from
  every node, persists across teardown. **100 GB total and was 93% full (7.5 GB free)**, shared with
  other project members, so check free space before assuming a large build or dataset will fit.
  ~316 MB/s writes — actually *faster* than the local disk.
- **`/share`** — NFS, 12 TB, **read-only**. Not usable for experiment output.
- First SSH to a node needs `-o StrictHostKeyChecking=accept-new` (or accept the key manually).

## Practical implications learned the hard way

- A full Neon build tree is ~8.7 GB (`target/release` alone is 6.5 GB) and **will not fit in
  `/proj`**. Keep source + final stripped binaries on `/proj`; build in-tree on a node's local `/`.
- Don't put build artifacts on `/mydata` if that node also stores experiment data — the two will
  compete for the same 37 GB.
- Because `$HOME` is on local `/` (57 GB) rather than NFS, it's a good place for per-node binaries
  and anything that shouldn't touch the data partition.

---

# Neon cluster 2 (CloudLab Wisconsin, `c220g5`, 8 nodes)

Measured by SSH on 2026-09-23. Node-to-hostname mapping and free-space numbers are only valid for
this provisioning — re-check on reprovision, same caveat as cluster 1.

## c220g5-SPECIFIC (does not generalize to `m400` — see above)

- **CPU/RAM:** x86_64, 2× Intel Xeon Silver 4114 @ 2.20GHz (10 cores/socket, 2 threads/core → **40
  logical CPUs**), 2 NUMA nodes (node0: cpus 0-9,20-29; node1: cpus 10-19,30-39), **187 GB** usable
  RAM. All 8 machines identical (verified `nproc`/`free` on every node).
- **OS:** Ubuntu 22.04.2 LTS (jammy), kernel `5.15.0-187-generic`, passwordless `sudo`.
- **Disk layout** — two physical disks combined into one LVM VG per node (`emulab-nodeN--bs`,
  `VFree 0`, no spare extents, same "can't grow /mydata" situation as `m400`):
  | mount | size | free | notes |
  |---|---|---|---|
  | `/` | 63 G | 57 G | `sda3`, local disk (not NFS) |
  | `/mydata` | **1.5 T** | 1.4 T | LVM over `sda4` (374.9G) + `sdb` (1.1T, a second physical disk), local |
  | swap | 8 G | — | |
  `/mydata` is **~40x bigger than `m400`'s 39 GB** — much more headroom for large datasets/N sweeps.
- **Disk performance:** ~**173 MB/s** sequential write (`dd conv=fsync`), ~**209 MB/s** read
  (measured after `drop_caches`). Faster than `m400`'s SATA SSD (~94 MB/s write) but still well
  short of NVMe — plan accordingly, don't assume this is a fast local disk.
- **apt has almost nothing preinstalled beyond `git`, `build-essential`, and `lsof`** — a much
  barer image than `m400`'s. Confirmed **missing**: `clang`, `cmake`, `flex`, `bison`,
  `pkg-config`, `libseccomp-dev`, `libreadline-dev`, `libcurl4-openssl-dev`, `libssl-dev`,
  `zlib1g-dev`, `libicu-dev` — all installable via apt, but budget time for it (don't assume the
  `m400` install script's apt line is a no-op here). `gcc` 11.4.0 and `python3` 3.10.12 are present
  as system defaults. No rust/cargo, same as `m400`.
- **protoc gotcha still applies, wrong architecture this time:** apt's jammy `protoc` (3.12.4)
  still predates proto3 `optional` and still breaks `storage_broker`. Install an **x86_64** (not
  aarch64) protoc release from the protobuf GitHub releases, with its `include/` dir, ahead of
  apt's on `PATH`.
- **`/mydata` is root-owned by default** here too — same `chown`-before-writing gotcha as `m400`.

## Node mapping (verified 2026-09-23; changes on reprovision)

| node | hostname |
|---|---|
| node0 | `c220g5-110915.wisc.cloudlab.us` |
| node1 | `c220g5-110923.wisc.cloudlab.us` |
| node2 | `c220g5-110924.wisc.cloudlab.us` |
| node3 | `c220g5-110909.wisc.cloudlab.us` |
| node4 | `c220g5-110920.wisc.cloudlab.us` |
| node5 | `c220g5-110916.wisc.cloudlab.us` |
| node6 | `c220g5-110913.wisc.cloudlab.us` |
| node7 | `c220g5-110908.wisc.cloudlab.us` |

## Generic to this cluster (region-specific — do not assume it matches cluster 1)

- **Private LAN:** `10.10.1.1`…`10.10.1.8` on interface **`enp94s0f1`** (not `m400`'s `enp1s0d1`),
  eight nodes ⇒ indices 0-7, resolvable via `/etc/hosts` as `nodeX`/`node<X>-0`/`node<X>-link-1`.
  Measured **10 Gbps** link (`ethtool`); RTT node0→node1 ~0.05-0.18ms (avg ~0.08ms) — same order of
  magnitude as `m400`'s 78µs. Public interface `eno1` is also 10 Gbps (`128.105.x.x`); a second NIC
  `eno2` exists on each node but is down/unused.
- **`/proj/rasl-PG0`** — NFS (`ops.wisc.cloudlab.us:/proj/rasl-PG0`). **This is a physically
  different mount from cluster 1's Utah `/proj/rasl-PG0`** despite the identical path — don't
  expect files placed on one cluster's `/proj` to be visible from the other. 100 GB total, 22 GB
  used / 79 GB free as of 2026-09-23 (more headroom than cluster 1 had at last check, but re-verify
  before assuming).
- **`/share`** — NFS, 50 GB, **read-only** (confirmed via `touch`). Same read-only caveat as
  cluster 1's `/share` (there, 12 TB).
- This session needed `-i ~/.ssh/dassl_rsa` **explicit** on every one of the 8 hosts (bare `ssh
  user@host` got `Permission denied (publickey)`), plus `-o StrictHostKeyChecking=accept-new` on
  first contact — same as cluster 1.

## Cluster 1 vs. cluster 2 at a glance

| | Cluster 1 (`m400`, Utah) | Cluster 2 (`c220g5`, Wisconsin) |
|---|---|---|
| Nodes | 6 | 8 |
| Arch | aarch64 | x86_64 |
| Cores | 8 (1 thread/core) | 40 logical (2×10 cores × 2 threads) |
| RAM | 62 GB | 187 GB |
| `/mydata` | 39 GB, SATA SSD | 1.5 TB, 2-disk LVM |
| Disk write/read | ~94 / ~414 MB/s | ~173 / ~209 MB/s |
| apt preinstalled deps | most build deps present, only protoc missing | almost nothing present except git/build-essential |
| protoc | missing, needs **aarch64** build | missing, needs **x86_64** build |
| Private LAN iface | `enp1s0d1` | `enp94s0f1` |