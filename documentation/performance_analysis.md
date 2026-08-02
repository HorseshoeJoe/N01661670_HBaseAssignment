# Performance and Scalability Analysis

**Part 11 of the assignment specification.**

A note on evidence: this prototype holds 500 rows in a single region on a
sandbox cluster. Every query below returns in well under a second, and the
measured differences between a bounded scan and a filtered full scan are within
noise. The analysis is therefore *architectural* — it reasons about how each
access path behaves as the table grows toward the volume the design is meant to
model. Where a claim can be demonstrated at 500 rows, the demonstrating command
is given.

---

## 1. Row-key efficiency

Selected key: `DeviceID#ReverseTimestamp#EventID`.

HBase maintains exactly one index — the row key — and sorts the table by it in
ascending lexicographic byte order. Every performance property below follows from
that single fact.

| Business access pattern | Mechanism | Complexity | Grows with table size? |
|---|---|---|---|
| Specific event, full key known | `get` | O(1) | **No** |
| All events for one device | Scan `DeviceID#` → `DeviceID$` | O(matches) | **No** |
| N most recent for one device | Same + `LIMIT` | O(N) | **No** |
| Device events in a time window | Scan between two reverse timestamps | O(window) | **No** |
| All devices of one type | Scan `RTR-` → `RTS-` | O(matches) | **No** |
| By severity / status / region / city / error code | Full scan + filter | **O(table)** | **Yes** |
| Global time window | Full scan + value filter | **O(table)** | **Yes** |

Five of twelve business requirements are answered without touching a
non-matching row. The other seven read every row in the table.

That ratio is the design decision, not an accident. The five cheap ones are the
device-scoped questions the specification names as primary and that an operations
console fires continuously; the seven expensive ones are reporting questions run
occasionally. Spending the single available index on the high-frequency path is
the correct allocation.

**Demonstrable at 500 rows** — same 107 rows, two different plans:

```ruby
# O(table): reads all 500 rows, evaluates a predicate on each
count 'network_events', {FILTER => "SingleColumnValueFilter('device','type',=,'binary:Router')"}

# O(matches): opens only the 'RTR-' key block
count 'network_events', {STARTROW => 'RTR-', STOPROW => 'RTS-'}
```

HBase prints elapsed time for both. Screenshot them side by side — at 500 rows
the gap is small, but the row-count difference in what each plan touches (500 vs
107) is the whole argument in miniature.

---

## 2. Device event history

**Suitable — this is the pattern the key exists to serve.**

Because `DeviceID` is the leading component, a device's events occupy one
contiguous byte range. All 18 events of `RTR-EDM-020` sit consecutively, almost
certainly within a single HFile block:

```
RTR-EDM-020#9223370254536156807#EVT-000018   2026-06-24 16:30:19
RTR-EDM-020#9223370256272105807#EVT-000195   2026-06-04 14:17:50
RTR-EDM-020#9223370256775207807#EVT-000302   2026-05-29 18:32:48
RTR-EDM-020#9223370258555656807#EVT-000066   2026-05-09 03:58:39
...
```

```ruby
scan 'network_events', {STARTROW => 'RTR-EDM-020#', STOPROW => 'RTR-EDM-020$'}
```

Execution: locate the region from the meta table → seek to the first key ≥
`RTR-EDM-020#` → stream forward → stop at `STOPROW`. Rows outside the range are
never read off disk. Cost is proportional to the answer.

**Contrast with a sequential-ID key.** Those 18 events would scatter across 500
rows in 18 unrelated positions, requiring 18 network round trips (if the IDs were
somehow known in advance) or a full-table scan. At 500 million rows the scan is
minutes; the range read stays milliseconds.

**One real limitation.** A device with a very long history — five years of a
chatty access point — accumulates a large contiguous range that could grow past a
region boundary and eventually dominate a region. Two standard remedies: a TTL on
the column families to age out events beyond the retention window, or bucketing
the key by period (`DeviceID#YYYYMM#ReverseTimestamp#EventID`) so each device's
history splits naturally by month. Neither is needed at 500 rows; both are
one-line schema changes when the volume warrants.

---

## 3. Recent event retrieval

**Timestamp ordering is the second reason this key was chosen.**

```
ReverseTimestamp = (2^63 - 1) - epoch_millis
```

Later real time → smaller reverse value → sorts earlier. HBase's native ascending
byte order therefore delivers reverse-chronological order with no sort step at
all.

```ruby
scan 'network_events', {STARTROW => 'RTR-EDM-020#', STOPROW => 'RTR-EDM-020$', LIMIT => 5}
```

The scanner opens at the first row of the device's range — which *is* the newest
event — reads five rows, and closes. **O(5), independent of how much history the
device has.**

With a forward timestamp the same question would require reading the device's
entire history, materialising it client-side, sorting, and discarding all but
five. O(all events for that device). For a device with five years of history that
is thousands of rows read to return five.

**Fixed width is load-bearing.** HBase compares row keys as byte arrays, not
integers. Without zero-padding to 19 characters, an 18-digit reverse timestamp
would sort *after* a 19-digit one, silently corrupting the ordering for a subset
of rows and producing a "most recent" answer that is quietly wrong. `zfill(19)`
in `generate_puts.py` enforces the invariant.

**Verification** — lexicographic key order equals reverse-chronological order,
checked across all 18 `RTR-EDM-020` events during generation. The `scan` output
above is the visible proof: timestamps descend without any `ORDER BY`.

---

## 4. Data distribution

HBase splits a table into regions by row-key range and assigns regions to
RegionServers. Distribution quality is entirely a function of how write traffic
maps onto key ranges.

**Under this design.** The leading component is `DeviceID`, which is uncorrelated
with write order. At any instant, events arriving from 50 devices target 50
distinct key ranges, so writes spread across whatever regions exist. Reads for
different devices likewise hit different regions and parallelise.

**Measured skew in the dataset:**

| Prefix | Device type | Events | Share |
|---|---|---|---|
| `SWT-` | Network Switch | 111 | 22.2% |
| `RTR-` | Router | 107 | 21.4% |
| `WLS-` | Wireless Infrastructure | 86 | 17.2% |
| `AP-` | Access Point | 81 | 16.2% |
| `SRV-` | Service Infrastructure | 64 | 12.8% |
| `GTW-` | Network Gateway | 51 | 10.2% |

Perfectly even would be 16.7% each. Observed range is 10.2%–22.2%, roughly 2:1
between the busiest and quietest prefix. Uneven, but bounded and manageable —
HBase's automatic splitting subdivides a hot range as it grows.

**Per-device skew is larger:** the mean device has 10 events; `RTR-EDM-020` has
18, `RTR-QUE-031` and `AP-VAN-043` 16 each. Roughly 1.8× the mean. In production
this is exactly what a failing device looks like, and it is precisely when the
device is failing that its history is being read most heavily — load correlates
with demand on the same region.

**Current state.** 500 rows fits comfortably in one region; the default split
threshold (10 GB in recent HBase) is nowhere near. The distribution argument is
about the growth path, and it is verifiable in the HBase Master UI
(Ambari → HBase → Quick Links → HBase Master UI → Tables → `network_events`),
where the region count and per-region request counts are shown.

---

## 5. Hotspotting

### Residual risks under the selected design

**Risk 1 — device-level write hotspot.** A single flapping device (repeated
`ConnectivityLoss` / `DeviceRecovery` cycles) concentrates writes into its narrow
key range on one RegionServer. Bounded — it is one device's share of traffic, not
the whole fleet's — and partly mitigated by the reverse timestamp, which spreads
even that device's writes across its own sub-range rather than appending at a
single edge.

**Risk 2 — prefix-level imbalance.** If regions split on device-type prefix
boundaries, the 2:1 skew above becomes a 2:1 RegionServer load imbalance.

**Risk 3 — cold start.** With one region at creation time, the initial load of
500 rows (5,500 puts) all lands on one RegionServer. Trivial here; significant
for a bulk load of millions.

### Mitigation implemented

Pre-splitting on device-type prefixes — `scripts/create_table_presplit.hbase`:

```ruby
create 'network_events',
  {NAME => 'event',    VERSIONS => 1, BLOOMFILTER => 'ROW'},
  {NAME => 'device',   VERSIONS => 1, BLOOMFILTER => 'ROW'},
  {NAME => 'location', VERSIONS => 1, BLOOMFILTER => 'ROW'},
  {NAME => 'status',   VERSIONS => 5, BLOOMFILTER => 'ROW'},
  {SPLITS => ['GTW-', 'RTR-', 'SRV-', 'SWT-', 'WLS-']}
```

Six regions from the outset, each owning one device-type range. Writes distribute
across all six from the first put. Confirm with `list_regions 'network_events'`
or the Master UI.

### Alternative if hotspotting persisted: salted key

```
SaltByte # DeviceID # ReverseTimestamp # EventID
```

where `SaltByte = hash(DeviceID) mod N`, `N` = region count. Example:

```
03#RTR-EDM-020#9223370254536156807#EVT-000018
```

Because the salt derives from `DeviceID` and not from time, a given device's
events all carry the *same* salt — so device locality survives, and device-history
retrieval stays a single bounded scan on `03#RTR-EDM-020#`. Writes distribute
across `N` region groups.

**Cost, stated plainly.** Any query that is not device-scoped must now be issued
`N` times, once per salt, and the results merged client-side. Prefix scans such as
`RTR-` → `RTS-` (BQ11b) stop working entirely, because the salt sits in front of
the device type.

**Recommendation for this prototype: do not salt.** Pre-splitting addresses the
realistic risk at this scale, and the selected key already avoids the
catastrophic monotonic-write pattern. Salting is the right answer only if
production telemetry shows sustained per-region imbalance that splitting cannot
resolve — it is a fix for a measured problem, not a precaution.

### For contrast: the anti-pattern this design avoids

A key of `EventID` alone (`EVT-000501`, `EVT-000502`, …) is monotonically
increasing. Every write sorts above every existing key and lands in the last
region — **100% of writes on one RegionServer, permanently**. Splitting does not
help: the new upper region immediately becomes the sole target. Adding nodes does
not help: the key space itself serialises the writes.

The difference is qualitative, not quantitative. Design B's hotspot risk is
partial and self-limiting; the sequential key's is total and structural.

---

## 6. Get vs. scan

| | `get` | Bounded `scan` | Filtered full `scan` |
|---|---|---|---|
| Input | One exact row key | `STARTROW` / `STOPROW` | Predicate, no bounds |
| Regions contacted | 1 | 1 (or few) | **All** |
| Rows read from disk | 1 | Rows in range | **Every row in table** |
| Bloom filter helps | **Yes** | Partially | No |
| Complexity | **O(1)** | O(range) | **O(table)** |
| Grows with table? | No | No | **Yes** |

**Why `get` is O(1).** HBase resolves the row key against the meta table to
identify exactly one region, consults the row Bloom filter to skip HFiles that
provably cannot contain the key, and reads a single block. A `get` against 500
rows and a `get` against 500 million rows do the same work. This is HBase's
defining capability.

**Why a bounded scan is nearly as good.** `STARTROW`/`STOPROW` restrict the
scanner to a contiguous byte range. The seek is a binary search within the region;
the read is sequential, which is the access pattern disks and the block cache
both favour. Cost tracks the answer, not the table.

**Why a filtered full scan is different in kind.** Without bounds, the scanner
opens at row 1 of every region and walks to the end. Every row is read off disk,
deserialised, and evaluated against the predicate. Non-matching rows are
discarded — **server-side, after being read**.

This is the single most important performance point in the assignment: **a filter
reduces network transfer, not disk I/O.** BQ4 returns 96 of 500 rows, so 404 rows
are read and thrown away. The client sees a small fast result; the RegionServer
did the full work.

**Practical guidance:**

- Full key known → `get`.
- Question expressible as a key range → bounded `scan`.
- Neither → filter, and accept O(table).
- Filtered query becomes a *primary* access pattern → change the schema. Add a
  secondary index table keyed for it. Maintaining a second table with a different
  row key is idiomatic HBase, not a workaround.

---

## 7. Filters

A filter is a server-side predicate. It executes on the RegionServer *after* each
row is read, so:

- **Reduced:** bytes over the network, client CPU and memory.
- **Not reduced:** disk reads, block-cache pressure, RegionServer CPU.

### Cost by filter type

| Filter | Operates on | Can skip rows unread? | Cost |
|---|---|---|---|
| `PrefixFilter` (with `STARTROW`) | Row key | **Yes** | O(matches) |
| `PrefixFilter` (bare) | Row key | No — starts at row 1 | O(table) |
| `SingleColumnValueFilter` | One column value | No | O(table) |
| `ValueFilter` | Every cell value | No | O(table) × cells |
| `ColumnPrefixFilter` | Qualifier | Partially — family pruning | O(table), fewer bytes |
| `RowFilter` + regex | Row key, regex | No | O(table) + regex per row |
| `KeyOnlyFilter` | Strips values | No | O(table), minimal transfer |

### Three practical consequences

**1. `PrefixFilter` without `STARTROW` is a trap.** These return identical rows:

```ruby
scan 'network_events', {FILTER => "PrefixFilter('WLS-OTT-019#')"}                    # O(table)
scan 'network_events', {STARTROW => 'WLS-OTT-019#', STOPROW => 'WLS-OTT-019$'}       # O(matches)
```

The first begins at row 1 of the table and walks all 500 rows discarding
non-matches. Same 15 rows returned, ~33× the rows read. Combining both is best:

```ruby
scan 'network_events', {STARTROW => 'WLS-OTT-019#', FILTER => "PrefixFilter('WLS-OTT-019#')"}
```

**2. `SingleColumnValueFilter` emits rows missing the tested column.** By default,
a row lacking the column *passes* the filter. In a sparse NoSQL table that
silently pollutes results. Use the six-argument form:

```ruby
SingleColumnValueFilter('event','severity',=,'binary:Critical',true,true)
```

(`filterIfColumnMissing = true`, `latestVersionOnly = true`.)

**3. Predicate order matters.** A `FilterList` evaluates left to right and
short-circuits, so the most selective predicate should come first — `Critical`
(96 rows) before `Eastern` (192 rows).

### Selectivity and the crossover

| Query | Returns | Reads | Selectivity |
|---|---|---|---|
| BQ4 Critical | 96 | 500 | 19% |
| BQ5 High or Critical | 247 | 500 | **49%** |
| BQ8 Eastern | 192 | 500 | 38% |
| BQ10 NET-503 | 50 | 500 | 10% |

BQ5 returns nearly half the table. Below roughly 20–30% selectivity a filtered
scan is at least saving meaningful network transfer; above it, the filter is
barely earning its cost and the query is effectively a full-table read with
extra CPU. At production volume BQ5 is a Hive query, not an HBase scan.

**Column projection is the cheap win.** `COLUMNS => ['device:id','event:type']`
restricts the read to specific families. Because HBase stores each family in
separate HFiles, this genuinely reduces disk I/O — one of the few client-side
options that does.

---

## 8. HBase vs. Hive

Both sit on HDFS. They are complementary, not alternatives.

### Belongs in HBase — operational, low-latency, high-concurrency

| Requirement | Why HBase |
|---|---|
| Look up event EVT-000018 (BQ1) | O(1) key lookup, milliseconds |
| Device event history (BQ2) | Contiguous range read |
| Five most recent for a device (BQ3) | Key ordering, no sort |
| Update status `Open → Investigating` (Part 7) | Row-level mutation with native versioning |
| Device time window (BQ12a) | Bounded key range |
| Ingest continuous event stream | LSM write path absorbs bursts |
| Thousands of concurrent console reads | Designed for high read concurrency |

These run while an engineer is on a call. Latency budget: milliseconds.

### Belongs in Hive — analytical, batch, aggregate

| Requirement | Why Hive |
|---|---|
| Mean time to resolution by device type per region | `GROUP BY` across the full dataset |
| Quarter-over-quarter severity trends | Time-series aggregation |
| Which error codes precede hardware failure within 48h | Self-join with a time window |
| Rank cities by unresolved Critical events | Sort + aggregate |
| Correlate events with maintenance windows | Join against an external table |
| Monthly executive reporting | Scheduled batch, latency-tolerant |

Latency budget: minutes. Run against the HDFS copy on batch resources, so they
never compete with live ingestion.

### The honest assessment of BQ4–BQ10

Business requirements 4 through 10 — severity, status, region, city, error code —
are implemented in this prototype as HBase filter scans, because the assignment
requires HBase operations. **At production volume they are Hive queries.** Each
one reads the entire table to return an aggregate; run frequently against a
live-ingesting cluster, they would degrade the write path and the operational
read path simultaneously.

The architecturally correct production split:

```
Network devices
      │
      ▼
  Event stream ──────────┬──────────────────────┐
                         ▼                      ▼
                   HBase (serving)        HDFS (system of record)
                   device-keyed                 │
                   ms lookups                   ▼
                   live console          Hive (analytics)
                                         aggregates, joins, trends
                                         batch reporting
```

The same data, two access paths, each keyed for its own workload. Hive can also
read the HBase table directly via `HBaseStorageHandler`, giving SQL over live
data — but the scan cost is HBase's, so this is for convenience and small
lookups, not for reporting.

---

## 9. Scalability summary

| Dimension | Assessment |
|---|---|
| Read scaling (device-keyed) | **Excellent** — O(1) / O(matches), independent of table size |
| Read scaling (filtered) | **Poor** — O(table); migrate to Hive or a secondary index |
| Write scaling | **Good** — device-distributed keys; no monotonic hotspot |
| Storage scaling | **Excellent** — add nodes, regions split automatically |
| Key-space growth | **Bounded by fleet size**, not by elapsed time |
| Hotspot risk | **Low–moderate** — bounded, pre-split mitigates, salt available |
| Schema evolution | **Excellent** — new qualifiers need no `ALTER TABLE` |

### Limitations of this prototype

- 500 rows in one region — distribution and split behaviour are reasoned about
  rather than observed.
- Single-node sandbox: no real replication, no multi-RegionServer parallelism, no
  network cost between client and server.
- Loading via 5,500 individual `put` statements in HBase Shell. Correct and fully
  transparent, but every put is a separate RPC; production would use `ImportTsv`
  with `completebulkload`, which writes HFiles directly and bypasses the write
  path entirely.
- No secondary index, so seven of twelve queries are full scans.
- No TTL, so history grows without bound.
- No compression configured (`SNAPPY` or `GZ` would typically halve storage on
  this highly repetitive data — nine distinct descriptions across 500 rows).

### Improvements for a production deployment

1. **Bulk load** via `ImportTsv` + `completebulkload` — orders of magnitude
   faster than per-row puts.
2. **Enable compression** — `COMPRESSION => 'SNAPPY'` on all families. The
   dataset is extremely repetitive; block compression is nearly free.
3. **Secondary index table** — `network_events_by_severity`, keyed
   `Severity#ReverseTimestamp#DeviceID#EventID`, written in parallel. Converts
   BQ4/BQ5 from O(table) to O(matches). Costs a second write per event and gives
   up cross-table atomicity; that is the standard HBase trade.
4. **TTL on column families** — age out events past the retention window
   automatically.
5. **Hive external table** over the HDFS copy for all aggregate reporting.
6. **Pre-split at creation** and monitor per-region request counts in the Master
   UI; introduce a salt only if measured imbalance persists.
7. **Phoenix** for SQL over HBase with real secondary index support, if the
   organisation needs ad-hoc querying without hand-maintained index tables.
