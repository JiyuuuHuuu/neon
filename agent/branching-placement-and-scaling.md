# Branching: pageserver & safekeeper placement and autoscaling

Scope: how the storage controller decides *where* a tenant's data and a timeline's WAL live, what
happens to those decisions when you create a branch, and whether a branch shares storage nodes with
its parent. All line references are against the tree at commit `fa504217c`.

## TL;DR

| | Unit that gets placed | New branch reuses parent's nodes? |
|---|---|---|
| **Pageserver** | the **tenant shard** | **Yes, always.** A branch is a new timeline inside an existing tenant; no placement decision is made at all. |
| **Safekeeper** | the **timeline** | **Not guaranteed.** The controller re-runs its selection for every timeline and never looks at the ancestor. Overlap is incidental, not enforced. |

The asymmetry follows from the data model: a branch is copy-on-write *against its parent's layers*,
so it must sit next to the parent on the pageserver; but on the safekeeper a branch is a brand-new,
empty WAL stream starting at the branch LSN, so it has no dependency on the parent at all.

---

## 1. Two independent placement domains

Neon has two placement problems with different granularity, both owned by the storage controller
(`storage_controller/`):

- **Pageserver placement** is *tenant-scoped*. The scheduling unit is the `TenantShard`
  (`TenantShardId` = tenant id + shard number + shard count). Timelines are not scheduled — they are
  objects that exist inside whatever shards the tenant already has.
- **Safekeeper placement** is *timeline-scoped*. The unit is the timeline, recorded as the `sk_set`
  column of the `timelines` table (`storage_controller/src/schema.rs`). Two timelines of the same
  tenant can legitimately live on disjoint safekeeper sets.

This is visible directly in the schema. The controller's `timelines` table is:

```
timelines (tenant_id, timeline_id) {
    tenant_id, timeline_id, start_lsn, generation,
    sk_set, new_sk_set,
    cplane_notified_generation, deleted_at, sk_set_notified_generation,
}
```

Note what is **absent**: there is no `ancestor_timeline_id`. The controller's safekeeper-side view of
a timeline does not know it is a branch.

## 2. Pageserver placement

### 2.1 What is scheduled, and how

`Scheduler::schedule_shard` (`storage_controller/src/scheduler.rs:733`) picks a node per shard
location by computing a score per candidate node and taking the minimum. Two score types, ordered
lexicographically by field declaration order:

`NodeAttachmentSchedulingScore` (`scheduler.rs:154`) — for the attached location:
1. `az_match` — prefer the shard's `preferred_az` (normally the AZ of the tenant's compute).
2. `affinity_score` — an *anti*-affinity: how many shards **of the same tenant** are already on this
   node in the current `ScheduleContext` (`scheduler.rs:318`, `ScheduleContext::avoid`). Spreads a
   sharded tenant across nodes.
3. `utilization_score` — combined shard count + disk utilisation, from pageserver-reported
   `PageserverUtilization`.
4. `total_attached_shard_count`, then `node_id` as a deterministic tiebreak.

`NodeSecondarySchedulingScore` (`scheduler.rs:224`) inverts the AZ rule: secondaries deliberately
*avoid* the preferred AZ, so an AZ outage does not take out both copies.

How many locations a shard wants is `PlacementPolicy`
(`libs/pageserver_api/src/controller_api.rs`): `Attached(n)` (one attached + n secondaries),
`Secondary` (warm, not serving), `Detached`.

A brand-new tenant's home AZ comes from `Scheduler::get_az_for_new_tenant` (`scheduler.rs:819`),
which picks the AZ with the lowest node-count-normalised `home_shard_count`.

### 2.2 Creating a branch on the pageservers

`Service::tenant_timeline_create` (`storage_controller/src/service.rs:3991`) →
`tenant_timeline_create_pageservers` (`service.rs:3838`).

That function does **no scheduling**. It calls `tenant_remote_mutation` to enumerate the tenant's
*existing* shards and their current attached locations, and issues `timeline_create` to each:

```rust
let (shard_zero_tid, shard_zero_locations) = targets.0.pop_first()...;
// create_one() → PageserverClient::new(latest.node ...).timeline_create(...)
```

Shard 0 goes first so that the LSN it chooses can be propagated to the other shards
(`service.rs:3950`, `TimelineCreateRequestMode::Branch { ancestor_start_lsn }`), and so that
non-zero shards reuse shard 0's initdb rather than generating their own. The request is also sent to
*stale* attached locations (`locations.other`) so a compute can start regardless of reconciliation
state.

On the pageserver, `TenantShard::create_timeline` (`pageserver/src/tenant.rs:2694`) resolves the
ancestor **in the same process**:

```rust
let ancestor_timeline = self
    .get_timeline(ancestor_timeline_id, false)
    .context("Cannot branch off the timeline that's not present in pageserver")?;
```

`branch_timeline_impl` (`pageserver/src/tenant.rs:4985`) then:
- takes the tenant's `gc_cs` lock so GC can't race the branch point,
- defaults `start_lsn` to the parent's `last_record_lsn`,
- validates `start_lsn` against the parent's applied and planned GC cutoffs (branching below the
  PITR horizon is rejected),
- writes **only metadata** — `TimelineMetadata::new(start_lsn, dst_prev, Some(src_id), ...)` — and
  holds `Some(Arc::clone(src_timeline))` as the in-memory ancestor,
- schedules an index upload so other nodes attaching the tenant see the branch and don't over-GC.

No layer data is copied. Reads on the child fall through to the parent's layers, and the child
registers a `retain_lsn` on the parent that pins the parent's GC (`pageserver/src/tenant.rs:586`).
`timeline detach_ancestor` (`pageserver/src/tenant/timeline/detach_ancestor.rs`) is the explicit
operation to break that dependency.

### 2.3 Pageserver autoscaling

Three distinct mechanisms, all driven from `Service::background_reconcile` (`service.rs:1261`) and
the heartbeat driver (`service.rs:1284`):

**Shard autosplitting** — `autosplit_tenants` (`service.rs:9213`), run every ~20s. Two triggers:
- *Initial split*: a 1-shard tenant whose largest timeline exceeds `initial_split_threshold` splits
  into `initial_split_shards` (helps bulk-ingest throughput).
- *Size-based split*: `max_logical_size / shard_count > split_threshold` splits in powers of two
  until it's under the threshold, clamped to `max_split_shards`.

Directly relevant to branching, from the doc comment at `service.rs:9185`:

> Splits are based on max_logical_size, i.e. the logical size of the largest timeline in a tenant.
> We use this instead of the total logical size because **branches will duplicate logical size
> without actually using more storage**.

So creating many branches does **not** by itself push a tenant toward a shard split.

When a split happens (`service.rs:5935`), every child shard is initially placed on the **same
pageserver as the parent shard** and inherits its `preferred_az`; the optimizer spreads them later.
All timelines, including all branches, are split together — branches never diverge in shard layout.

**Continuous optimisation** — `optimize_all` (`service.rs:8810`) emits `ScheduleOptimizationAction`s
(`storage_controller/src/tenant_shard.rs:457`): `CreateSecondary`, `MigrateAttachment`,
`ReplaceSecondary`, `RemoveSecondary`. Migration is always via a warmed secondary, and
`for_optimization()` zeroes the utilisation fields so churn is driven only by AZ and affinity
mismatches, not by transient load.

**Failover** — the heartbeater marks unreachable nodes `Offline`
(`handle_node_availability_transition`, `service.rs:7980`) and shards fail over to their
secondary locations.

Note this is *rebalancing*, not elastic node provisioning: the controller schedules onto the
pageservers registered with it; adding/removing pageserver nodes is an external operation
(`node_upsert`, node delete / drain / fill in `background_node_operations.rs`).

## 3. Safekeeper placement

### 3.1 Selecting safekeepers for a timeline

`Service::tenant_timeline_create_safekeepers`
(`storage_controller/src/service/safekeeper_service.rs:282`) is called after the pageserver-side
creation succeeds, when `--timelines-onto-safekeepers` is on (`service.rs:4100`). It:

1. calls `safekeepers_for_new_timeline()` to choose the set,
2. inserts a `TimelinePersistence` row with `generation: 1` and that `sk_set`,
3. creates the timeline on a **quorum** of them synchronously
   (`tenant_timeline_create_safekeepers_quorum`, `safekeeper_service.rs:75`),
4. writes a `Pull` pending-op for the rest and hands it to the background
   `safekeeper_reconcilers` (`service/safekeeper_reconciler.rs`).

The selection itself, `safekeepers_for_new_timeline` (`safekeeper_service.rs:697`):

```rust
// Choose safekeepers for the new timeline in different azs.
// 3 are choosen by default, but may be configured via config (for testing).
```

- filter to safekeepers with `SkSchedulingPolicy::Active`
  (`libs/pageserver_api/src/controller_api.rs:437`; the others are `Activating`, `Pause`,
  `Decomissioned`),
- sort by `(utilization.timeline_count, node_id)` — unavailable safekeepers sort last via
  `u64::MAX` but stay eligible,
- walk that order taking **one safekeeper per availability zone** until
  `config.timeline_safekeeper_count` are found (default 3, `storage_controller/src/main.rs:218`),
- error out if fewer than that many distinct AZs can be covered.

**The ancestor timeline is never consulted.** The function takes no timeline argument other than
`&self`, and its only inputs are the live safekeeper roster and their utilisation. The comment at
the call site, "Choose initial set of safekeepers respecting affinity" (`safekeeper_service.rs:298`),
refers to AZ *anti*-affinity — `git log -L` on the function shows no ancestor-aware logic has ever
existed there; the only change since introduction was making the count configurable (#11483).

Two special cases:
- **Read-only branches** (`TimelineCreateRequestMode::Branch { read_only: true }`, `service.rs:4015`)
  get `sks = Vec::new()` — an empty `sk_set`. They never ingest WAL, so they need no safekeepers.
- **Imported timelines** defer safekeeper creation until the import is finalised and the start LSN is
  known (`finalize_timeline_import` → `tenant_timeline_create_safekeepers_until_success`,
  `safekeeper_service.rs:413`).

### 3.2 Why safekeepers have no notion of a branch

The safekeeper API confirms the branch is structurally independent.
`safekeeper_api::models::TimelineCreateRequest` carries `tenant_id`, `timeline_id`, `mconf`,
`pg_version`, `wal_seg_size`, `start_lsn`, `commit_lsn` — and **no ancestor field**. A branch's
safekeeper state is initialised empty at `start_lsn`
(`safekeeper/src/timelines_global_map.rs:294`), and all the parent's history is already durable in
the pageserver's layer files. A safekeeper for the child therefore never needs a byte of the
parent's WAL.

### 3.3 Membership, migration, and "autoscaling"

Placement is versioned rather than mutable. The row carries `generation`, `sk_set`, and
`new_sk_set`; `Configuration`/`MemberSet` (`libs/safekeeper_api/src/membership.rs`) is handed to the
safekeepers and to the compute, and computes learn their set via
`tenant_timeline_locate` (`safekeeper_service.rs:493`), which returns `{generation, sk_set,
new_sk_set}`.

Moving a timeline is a joint-consensus migration —
`tenant_timeline_safekeeper_migrate` (`safekeeper_service.rs:1127`): set `new_sk_set`, bump
generation, `tenant_timeline_set_membership_quorum`, `tenant_timeline_pull_from_peers`, notify the
control plane, then exclude the departed members. There is an abort path
(`safekeeper_service.rs:1628`).

**There is no automatic safekeeper rebalancer in this tree.** Migration is triggered only by the
HTTP endpoints `/v1/tenant/:tenant_id/timeline/:timeline_id/safekeeper_migrate[_abort]`
(`storage_controller/src/http.rs:2623`) or by the test-only chaos injector. Load levelling for
safekeepers happens at *placement time* (the `timeline_count` sort above), not continuously. Taking a
safekeeper out of rotation is done by setting its scheduling policy to `Pause`/`Decomissioned`,
which only affects *future* timelines.

## 4. The specific question

> Does a new branch and the old one get located on the same safekeeper or pageserver?

### Pageserver: **yes — necessarily the same**

Three independent reasons, each enforced in code:

1. **Nothing is scheduled.** `tenant_timeline_create_pageservers` (`service.rs:3838`) iterates the
   tenant's existing shards and their current attachments. The scheduler is not invoked on the
   timeline-create path at all.
2. **The pageserver requires co-location.** `create_timeline` looks the ancestor up in its own
   in-memory `timelines` map and fails with *"Cannot branch off the timeline that's not present in
   pageserver"* (`pageserver/src/tenant.rs:2734`) otherwise.
3. **The data model requires it.** The child stores only metadata plus an `Arc` to the parent
   `Timeline`; reads fall through to the parent's layers, and the child pins the parent's GC via
   `retain_lsn`. A child on a different node would have nothing to read.

If the tenant is sharded, "the same pageservers" means the same *set* of shards — the branch is
created on every shard, each on that shard's attached node. Later migrations and shard splits move
parent and child together, because they move the shard.

### Safekeeper: **not guaranteed, and not by design**

`safekeepers_for_new_timeline` re-runs from scratch for the branch, ranking the whole active
safekeeper roster by current `timeline_count` and taking one per AZ. Concretely:

- The parent occupies 3 safekeepers, one per AZ. Creating the branch bumps nothing that the
  selection reads *before* it runs, so in a small, evenly-loaded, exactly-3-AZ deployment the branch
  will very often land on **the same three safekeepers** — they are simply the lowest-loaded node in
  each AZ.
- But with more than one safekeeper per AZ, or uneven `timeline_count`, or a parent safekeeper that
  is currently `Pause`d/`Decomissioned`/unavailable, the branch will get a **different set**, wholly
  or partly. Nothing detects or prevents this.
- A **read-only branch gets no safekeepers at all** (empty `sk_set`).
- Even if the sets start out identical, they can diverge later: `safekeeper_migrate` operates on one
  `(tenant_id, timeline_id)` at a time and will happily move a branch off its parent's safekeepers.

So: **treat pageserver co-location as an invariant you can rely on, and safekeeper co-location as a
coincidence you must not rely on.**

## 5. Caveats

- **This is the storage-controller-managed path.** `--timelines-onto-safekeepers` defaults to `false`
  in `storage_controller/src/main.rs:190` (it is `true` for `neon_local`,
  `control_plane/src/local_env.rs:250`). In deployments where it is off, safekeepers for a timeline
  are chosen by the cloud control plane, which is not in this repository.
- **Fork-specific tables.** `hadron_safekeepers` / `hadron_timeline_safekeepers`
  (`storage_controller/migrations/2025-07-17-000001_hadron_safekeepers/up.sql`) are declared in
  `schema.rs` but have **no reader or writer anywhere in this tree** — per the migration comment they
  are populated by an out-of-repo "hadron cluster coordinator" that safekeepers register with at
  startup. If that coordinator is in play, its placement policy is not visible here. Note it is still
  keyed by `timeline_id` alone, with no ancestry column, so the per-timeline model holds.
- Line numbers drift; the function names are the stable handles.

## 6. Where to look next

| Question | File |
|---|---|
| Shard → node scoring | `storage_controller/src/scheduler.rs` |
| Shard intent/observed state, optimisations | `storage_controller/src/tenant_shard.rs` |
| Timeline create, autosplit, optimise loop | `storage_controller/src/service.rs` |
| Safekeeper set selection & migration | `storage_controller/src/service/safekeeper_service.rs` |
| Async safekeeper op reconciliation | `storage_controller/src/service/safekeeper_reconciler.rs` |
| Branch creation, GC cutoffs, retain_lsn | `pageserver/src/tenant.rs` |
| Breaking the parent dependency | `pageserver/src/tenant/timeline/detach_ancestor.rs` |
| Safekeeper membership types | `libs/safekeeper_api/src/membership.rs` |
| Migration behaviour, end to end | `test_runner/regress/test_safekeeper_migration.py` |
| Design background | `docs/storage_controller.md`, `docs/pageserver-tenant-migration.md`, `docs/safekeeper-protocol.md` |
