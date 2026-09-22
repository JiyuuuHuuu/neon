```bash
ssh -i ~/.ssh/dassl_rsa JiyuHu23@ms0603.utah.cloudlab.us
ssh -i ~/.ssh/dassl_rsa JiyuHu23@ms0607.utah.cloudlab.us
ssh -i ~/.ssh/dassl_rsa JiyuHu23@ms0610.utah.cloudlab.us
ssh -i ~/.ssh/dassl_rsa JiyuHu23@ms0622.utah.cloudlab.us
ssh -i ~/.ssh/dassl_rsa JiyuHu23@ms0644.utah.cloudlab.us
ssh -i ~/.ssh/dassl_rsa JiyuHu23@ms0629.utah.cloudlab.us
```

between the cluster machines, they can be referred to as nodeX, where X is 0-5.

`ms` machines are `m400` machines: 	aarch64, 8 cores.

Bulk disk storage at `/mydata`, best for storing bulk experiment data.
`/proj/rasl-PG0/JiyuHu23/` is a distributed file system mount shared within the cluster. You should put code there and it will be auto-synced and persisted after node tear-down.

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