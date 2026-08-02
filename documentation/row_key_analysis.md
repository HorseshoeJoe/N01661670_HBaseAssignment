# Row-Key Design Analysis

**Part 4 of the assignment specification.**

Four candidate designs were considered. Two are evaluated in full as required;
two more are included because they illuminate the trade-off. One is selected and
justified.

---

## Candidates

| | Design | Example key |
|---|---|---|
| **A** | `EventID` | `EVT-000034` |
| **B** | `DeviceID#ReverseTimestamp#EventID` | `AP-OTT-026#9223370269593014807#EVT-000034` |
| C | `ReverseTimestamp#DeviceID#EventID` | `9223370269593014807#AP-OTT-026#EVT-000034` |
| D | `Region#DeviceID#ReverseTimestamp#EventID` | `Eastern#AP-OTT-026#9223370269593014807#EVT-000034` |

---

## Design A — `EventID`

```
EVT-000001
EVT-000002
EVT-000003
...
EVT-000500
```

### Evaluation

**Required access patterns.** Serves exactly one of eleven: point lookup by
event ID. Every other question in Part 8 degrades to a full-table scan.

**Key-based retrieval.** Excellent *if and only if* the caller already knows the
EventID. `get 'network_events', 'EVT-000034'` is O(1). In practice an operator
rarely starts from an event ID — they start from a device that is alarming, or a
region that is degrading. The event ID is what a ticketing system quotes back
*after* the investigation.

**Device-history retrieval.** **This is where the design fails.** A device's
events are assigned event IDs as they arrive, interleaved with every other
device's events, so they scatter uniformly across the entire key space. The 18
events of `RTR-EDM-020` land at `EVT-000018`, `EVT-000059`, `EVT-000066`,
`EVT-000076`, `EVT-000115`, `EVT-000139` and so on — 18 rows spread across 500,
in 18 different blocks, potentially in different regions on different physical
servers.

Retrieving them requires either 18 separate `get` calls (each a network round
trip, and the caller must already know all 18 IDs, which is circular) or a
full-table scan with a filter on `device:id`. Both are wrong: the first is
impossible without prior knowledge, the second is O(500) to return 18 rows. At
production scale — 18 rows out of 500 million — it is unusable.

**Chronological ordering.** Accidental and unreliable. `EVT-000034` has timestamp
`2026-01-01 10:02:41`, `EVT-000151` has `2026-01-01 12:28:11` — but in the
supplied dataset the IDs are *not* issued in timestamp order overall
(`EVT-000009` occurs after `EVT-000151`). Even if they were, ID order is only
global order; it says nothing about the ordering of one device's events without
reading all of them.

**Recent-event retrieval.** No efficient path. "Five most recent events for
RTR-EDM-020" means scanning the table, filtering to the device, parsing 18
timestamps, sorting, taking five. O(table) to answer a question about one device.

**Data distribution.** Uniform across regions if IDs are assigned randomly. The
one genuine strength of this design.

**Scalability.** The key space is bounded by the ID format — `EVT-` plus six
digits caps out at one million events. A national carrier reaches that in hours.
The format would have to change, and changing a row-key format means rewriting
every row.

**Hotspotting.** *Severe, and this is the subtle failure.* `EVT-000501`,
`EVT-000502`, `EVT-000503` are monotonically increasing. Every new event's key
sorts higher than every existing key, so **every write lands in the last
region**. One RegionServer absorbs the entire national write load while the rest
of the cluster idles. That region splits, and the new upper region immediately
becomes the sole write target again. This is the textbook sequential-key
anti-pattern, and it is exactly why the HBase Reference Guide warns against
monotonically increasing row keys.

**Advantages** — trivially simple; guaranteed unique; ideal if event ID is the
only lookup path; even read distribution.

**Disadvantages** — no device locality; no chronological ordering; no recent-event
support; severe write hotspotting; bounded key space; ten of eleven access
patterns become full scans.

---

## Design B — `DeviceID#ReverseTimestamp#EventID`  ← **SELECTED**

```
AP-OTT-026#9223370269593014807#EVT-000034
RTR-EDM-020#9223370254536156807#EVT-000018   <- 2026-06-24 (newest)
RTR-EDM-020#9223370256272105807#EVT-000195   <- 2026-06-04
RTR-EDM-020#9223370256775207807#EVT-000302   <- 2026-05-29
```

### Evaluation

**Required access patterns.** Serves five of eleven directly and cheaply — and
they are the five the specification names as primary: point lookup, full device
history, recent device events, per-device time window, and status mutation.

**Key-based retrieval.** O(1) when the full key is known. Marginally more
awkward than Design A because the caller must construct a 41-42 byte composite key rather than
quote a 10-byte ID — which is why `device:id` and `event:timestamp` are also
stored as columns, so an operator holding only an EventID can find the row with
one filtered scan and then use the real key thereafter.

**Device-history retrieval.** **The reason this design was selected.** DeviceID
leads the key, so all 18 events of `RTR-EDM-020` occupy one contiguous byte range
in one region, almost certainly in the same HFile block:

```ruby
scan 'network_events', {STARTROW => 'RTR-EDM-020#', STOPROW => 'RTR-EDM-020$'}
```

The scanner seeks directly to the first matching row and stops at `STOPROW`. Rows
for other devices are never read, never deserialised, never evaluated. Cost is
O(18), proportional to the answer, not to the table. This holds at 500 rows and
at 500 million.

(`STOPROW` is exclusive. `$` is `0x24`, the byte immediately after `#` at `0x23`,
so `RTR-EDM-020$` is the tightest legal upper bound on the `RTR-EDM-020#` block.)

**Chronological ordering.** Guaranteed *within each device*, newest first, by
construction. Not guaranteed globally — a global chronological scan is not
possible under this key, which is Design C's territory.

**Recent-event retrieval.** The strongest property. Because
`ReverseTimestamp = (2^63-1) - epoch_millis`, later events produce smaller values
and therefore sort earlier. "Five most recent" is:

```ruby
scan 'network_events', {STARTROW => 'RTR-EDM-020#', STOPROW => 'RTR-EDM-020$', LIMIT => 5}
```

The scanner reads five rows and stops. No sort, no full history read, O(5). With
a forward timestamp this same question would require reading the device's entire
history and sorting it client-side — the difference between O(5) and O(all events
for that device), which for a chatty device over five years is substantial.

**Data distribution.** Determined by DeviceID, which is effectively random with
respect to write order — 50 devices across 6 type prefixes and 10 cities. At any
instant, incoming events target many different key ranges, so writes spread
across regions. This is the property Design A lacks entirely.

**Scalability.** The key space is bounded only by the device namespace, which
grows with the physical network, not with time. Adding devices adds key ranges
and regions split naturally along device boundaries. Keys are 41–42 bytes
(`AP-` device IDs are 10 characters, the other five prefixes 11), so per-row key
overhead is predictable — relevant because HBase repeats the row key in every
cell, so at 11 cells per row the key is stored 11 times. That is roughly 460
bytes of key per event, against perhaps 150 bytes of actual values: **the key
outweighs the data it indexes by about 3:1.** Block compression would largely
absorb this, since consecutive keys share long prefixes, and it is a strong
argument for enabling `SNAPPY` in production.

**Hotspotting.** *Moderate and bounded, not eliminated.* Two residual risks:

1. **Device-level hotspot.** A single device flapping — repeatedly losing and
   regaining connectivity — writes a burst into one narrow key range on one
   RegionServer. Real: `RTR-EDM-020` (18), `RTR-QUE-031` (16) and `AP-VAN-043`
   (16) already show 3× the mean event rate. Mitigated by the reverse timestamp,
   which spreads that device's writes across its own sub-range rather than
   appending at one edge.

2. **Prefix-level imbalance.** DeviceIDs cluster on six type prefixes, and
   distribution is uneven — `SWT-` 111 events, `RTR-` 107, `WLS-` 86, `AP-` 81,
   `SRV-` 64, `GTW-` 51. Regions splitting on those boundaries inherit up to a
   2:1 load imbalance. Mitigated by pre-splitting
   (`scripts/create_table_presplit.hbase`) and, if it persisted, by a salt.

Neither approaches Design A's failure mode, where *100%* of writes hit *one*
region permanently.

**Advantages** — device locality; free reverse-chronological ordering; efficient
per-device time ranges; good write distribution; unbounded key space; directly
serves the specification's stated primary access patterns; prefix happens to
align with device type, making BQ11 key-servable as a bonus.

**Disadvantages** — 41-42 byte keys, repeated per cell, cost more storage than 10-byte
Design A; caller must construct or look up the composite key; severity, status,
region and error-code queries remain full scans; residual device-level hotspot
risk; the reverse timestamp is unreadable to a human without a conversion step.

---

## Design C — `ReverseTimestamp#DeviceID#EventID` (considered, rejected)

Global reverse-chronological order — "the 50 most recent events across the whole
network" becomes a scan of the first 50 rows, which is a genuinely useful
operations-centre dashboard query.

**Rejected because it hotspots as badly as Design A, in the opposite direction.**
Every new event has a *smaller* reverse timestamp than every existing event, so
every write lands in the *first* region. One RegionServer takes the entire write
load. Worse, it destroys device locality: `RTR-EDM-020`'s 18 events scatter
across the whole key space by time, so device history — the primary access
pattern — becomes a full scan.

Design C trades the primary access pattern for a secondary one and takes a
hotspot in the bargain. If the "most recent across the fleet" dashboard were
genuinely required, the right answer is a **second table** keyed this way and
written in parallel, not a compromise on the main table.

---

## Design D — `Region#DeviceID#ReverseTimestamp#EventID` (considered, rejected)

Makes BQ8 (events by region) a bounded scan instead of a full-table filter, and
preserves device locality within each region.

**Rejected for three reasons.** Four regions means four top-level key ranges, and
they are badly skewed — Eastern 192 events, Western 170, Atlantic 96, Central 42.
That is a 4.5:1 imbalance baked into the region boundaries. Second, region is a
property of the *device*, not of the *event*, so it is effectively duplicated key
material adding 8–9 bytes to every key for one query's benefit. Third, and
decisively, it does not remove any full scan except BQ8 — severity, status, city
and error code are all still filter-only, so the cost is paid for a single query.

Design D would be correct if regional operations centres each owned their own
territory and virtually every query were region-scoped. That is not the access
profile the specification describes.

---

## Comparison

| Criterion | A `EventID` | **B (selected)** | C `RevTS#Device` | D `Region#Device#RevTS` |
|---|---|---|---|---|
| Point lookup by event ID | **O(1)** | O(1)* | O(table) | O(table) |
| Device event history | O(table) | **O(matches)** | O(table) | **O(matches)** |
| Recent events per device | O(table) + sort | **O(N)** | O(table) | **O(N)** |
| Device time window | O(table) | **O(window)** | O(table) | **O(window)** |
| Global recent events | O(table) + sort | O(table) + sort | **O(N)** | O(table) + sort |
| Events by region | O(table) | O(table) | O(table) | **O(matches)** |
| Events by device type | O(table) | **O(matches)** | O(table) | O(table) |
| Events by severity/status | O(table) | O(table) | O(table) | O(table) |
| Write distribution | **Catastrophic** | Good | **Catastrophic** | Poor (4.5:1 skew) |
| Key size | 10 B | 41-42 B | 41-42 B | ~50 B |
| Key space bound | 1M events | Unbounded | Unbounded | Unbounded |

\* requires constructing the composite key, or one filtered scan to find it.

---

## Selection and justification

**Design B — `DeviceID#ReverseTimestamp#EventID` — is implemented.**

1. **It matches the stated primary access pattern.** §2 of the specification
   leads with *"What events have been generated by a particular device?"* and
   *"What are the most recent events associated with a device?"* Both are
   device-scoped and both are O(answer) under Design B and O(table) under every
   alternative. When the row key is the only index, it must be spent on the query
   that runs most often.

2. **It gives reverse-chronological ordering for free.** Not a sort applied at
   read time — a property of the physical byte layout. "Most recent five" costs
   five row reads because the newest rows are literally the first bytes in the
   device's range.

3. **It distributes writes.** DeviceID as the leading component means concurrent
   events from 50 devices target 50 different key ranges. Designs A and C both
   funnel 100% of writes into a single region indefinitely.

4. **It scales along the right axis.** The key space grows with the device fleet,
   which grows slowly and predictably, not with elapsed time, which grows without
   bound.

5. **It accepts a real cost with open eyes.** Severity, status, geography and
   error-code queries remain full scans — seven of twelve business requirements.
   That is the correct trade because those are *reporting* questions: run
   occasionally, tolerant of latency, and at production volume better served by
   Hive over the HDFS copy or by a purpose-keyed secondary table. Optimising the
   row key for them would sacrifice the operational path that runs thousands of
   times an hour to speed up a query that runs twice a day.

---

## Why a sequential event identifier is the wrong key here

The specification asks this explicitly. Three independent failures:

**1. No device locality — the primary access pattern breaks.** Event IDs are
assigned in arrival order across the whole fleet, so one device's events scatter
uniformly through the key space. `RTR-EDM-020`'s 18 events sit at 18 unrelated
key positions. There is no key range that contains them and excludes everything
else, so the primary operator question becomes a full-table scan. Under Design B
it is a contiguous range read. This alone is disqualifying.

**2. Monotonic keys create a permanent write hotspot.** HBase assigns rows to
regions by key range. A strictly increasing key means every new row sorts above
every existing row, so every write goes to the region holding the upper bound —
one RegionServer, while the rest of the cluster is idle. When that region splits,
the new upper half immediately becomes the sole target again. The cluster cannot
scale writes no matter how many nodes are added, because the key space itself
serialises them. For a system whose defining characteristic is a continuous
high-velocity write stream, this is the worst possible property.

Note the asymmetry: Design B's residual hotspot risk is *bounded* (one busy
device's share of traffic) and *self-limiting* (the reverse timestamp spreads
even that device's writes across its own range). Design A's is *total* and
*permanent*.

**3. No temporal semantics.** Event ID encodes arrival order at the collector,
not occurrence time at the device. In a real network these differ — events queue,
batch, and arrive out of order after a link is restored. Sorting by EventID is
therefore not sorting by time even approximately, so "most recent" cannot be
answered from the key at all, and the ordering illusion is actively misleading.

A sequential ID is a fine *attribute* — unique, compact, quotable in a ticket. It
is a poor *row key*, because a row key's job in HBase is to encode the access
pattern, and a counter encodes nothing except the order things happened to arrive.
