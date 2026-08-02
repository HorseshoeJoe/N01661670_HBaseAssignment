# Business and Technology Analysis

**Part 1 of the assignment specification.**
Telecommunications Network Event Management with Apache HBase and Zeppelin.

> All data, device identifiers, error codes and operational scenarios in this
> document are synthetic and created for educational purposes. Bell Canada is
> referenced only as a recognisable telecommunications context.

---

## 1. Why telecommunications network-event processing is a Big Data use case

The prototype dataset holds 500 records from 50 devices over six months. That is
deliberately small — it is a teaching sample of a workload whose real shape is
defined by four properties.

**Volume.** A national carrier operates hundreds of thousands of routers,
switches, access points, gateways and wireless nodes. Each emits operational
events continuously — SNMP traps, syslog entries, threshold breaches, state
transitions. At even one event per device per minute, a 200,000-device fleet
produces roughly 288 million events per day. The 50 devices and 9 event types in
`network_events.csv` are a scale model of that; the schema is identical, only the
cardinality differs.

**Velocity.** Network events arrive as an unbounded stream, not a batch. During a
regional fibre cut or a firmware rollout, event rates spike by orders of
magnitude precisely when the operations centre most needs to read them. The store
must absorb a write burst while serving reads — a workload profile that
traditional OLTP databases handle poorly, because the same B-tree index that
serves reads is the structure contending under write load.

**Variety.** The nine `EventType` values here (`ConnectivityLoss`,
`PerformanceDegradation`, `HardwareFailure`, `ConfigurationChange`,
`ServiceInterruption`, `HighLatency`, `PacketLoss`, `DeviceRecovery`,
`Maintenance`) span six device classes. In production each event type carries
type-specific payload — a `PacketLoss` event has loss percentages and interface
counters; a `ConfigurationChange` has a diff. A fixed relational schema forces
either a very wide sparse table or a proliferation of per-type tables.

**Value with a time gradient.** An unresolved `Critical` event from the last hour
is operationally urgent. The same event from eight months ago is a data point in
a reliability trend. The store must serve both, cheaply, from one copy of the
data — which drives the reverse-timestamp row-key decision documented in
`row_key_analysis.md`.

The dataset already shows the recurrence pattern that motivates historical
retention: `RTR-EDM-020` generated 18 events, `RTR-QUE-031` and `AP-VAN-043` 16
each. Repeat-offender devices are only identifiable if history is kept and is
cheap to retrieve per device.

---

## 2. Why HBase is appropriate for large-scale operational event storage

**Linear horizontal scaling.** HBase partitions a table into regions by row-key
range and distributes regions across RegionServers. Growth is handled by adding
nodes; regions split and rebalance automatically. There is no re-sharding
exercise and no vertical scaling ceiling.

**Write-optimised storage engine.** HBase uses an LSM tree: writes go to a
write-ahead log and an in-memory MemStore, then flush sequentially to immutable
HFiles. Sequential writes suit the continuous event stream far better than the
random in-place page updates of a B-tree, which is what makes HBase absorb
ingestion spikes without read-path collapse.

**Millisecond point lookups at any table size.** A `get` on a known row key is
resolved through the meta table to one region, filtered by a Bloom filter, and
served from one block. The cost is independent of table size. This is what makes
"pull up event EVT-000018 on RTR-EDM-020" answerable in the same time against 500
rows or 500 million.

**Sparse, schema-flexible columns.** HBase stores nothing for an absent column —
no NULL placeholder, no reserved width. New event types can introduce new
qualifiers without an `ALTER TABLE` and without cost to existing rows. For a
schema that must accommodate device generations shipped years apart, this matters.

**Built-in versioning.** Each cell is versioned by timestamp. Configuring
`status` with `VERSIONS => 5` gives the incident lifecycle
(`Open → Investigating → Resolved → Closed`) an audit trail with no extra tables,
no triggers and no application code.

**Strong consistency per row.** Unlike eventually consistent stores, HBase gives
a single-row consistent read. When an operator marks an event `Resolved`, the
next read anywhere in the cluster sees `Resolved` — a requirement for an incident
system where two engineers must not both pick up the same ticket.

**Where HBase is a poor fit, stated honestly.** It has no joins, no secondary
indexes out of the box, no SQL, and no efficient aggregation. Seven of the twelve
business queries in Part 8 require full-table scans for exactly this reason. HBase
is the right choice here because the *primary* operational access pattern is
device-keyed lookup; the aggregate reporting queries are a secondary concern
better served by Hive (see §5).

---

## 3. Why HDFS is appropriate for storing the original source dataset

HDFS and HBase serve different roles, and the architecture uses both rather than
choosing between them.

- **Durability through replication.** HDFS replicates each block (typically 3×)
  across nodes and racks. The immutable source dataset survives node loss without
  a backup process.
- **Immutable system of record.** HBase is the serving layer and is mutable —
  Part 7 deletes rows and rewrites statuses. If the HBase table is dropped,
  mis-keyed, or corrupted by a bad load, the HDFS copy allows a full rebuild. The
  row-key strategy can be changed and the table reloaded from source, which is
  the normal way a schema decision gets revised in production.
- **Large sequential reads.** HDFS is optimised for streaming whole files, which
  is what a load job or a Hive scan does. It is *not* optimised for random single-
  record access — that is precisely the gap HBase fills, and it is why HBase is
  layered on top of HDFS rather than replacing it.
- **Shared substrate.** HBase itself stores its HFiles in HDFS. The same cluster,
  the same replication guarantees, the same operational tooling.
- **Format-agnostic staging.** The raw CSV keeps every attribute exactly as
  received, including any the current HBase model does not use. A future
  requirement can be satisfied without re-collecting data.

HDFS path used in this implementation is recorded in `README.md` and in Section 3
of the Zeppelin notebook.

---

## 4. How HBase differs from a relational database for this use case

| Dimension | Relational (e.g. Oracle, PostgreSQL) | Apache HBase |
|---|---|---|
| Schema | Fixed, declared up front, `ALTER TABLE` to change | Column families fixed; qualifiers arbitrary per row |
| Design driver | Normalisation — eliminate redundancy | **Access patterns** — optimise the queries that matter |
| Sparse data | NULLs consume space in each row | Absent columns cost nothing |
| Joins | Native, optimised | None. Denormalise, or join in the application |
| Indexes | Multiple secondary indexes | One: the row key |
| Ad-hoc queries | Any `WHERE` clause is serviceable | Only row-key access is fast; everything else is a scan |
| Scaling | Vertical, then painful manual sharding | Horizontal, automatic region splits |
| Consistency | ACID across multiple rows and tables | Atomic per row only |
| Write path | In-place B-tree updates | Append-only LSM, sequential flush |
| Versioning | Application-implemented history tables | Native, per cell, configurable |

**The decisive difference for this project.** In a relational design, a normalised
schema would separate `Device`, `Location`, `EventType` and `Event` into four
tables, and the operator question *"show me this router's recent history with its
city and severity"* becomes a three-way join. That join is cheap at 500 rows and
ruinous at 500 million across a distributed cluster.

The HBase design does the opposite: it **denormalises deliberately**. City,
province, device type and description are stored redundantly on every one of the
500 rows. A relational reviewer would call that a normalisation violation. In
HBase it is the correct answer, because it converts the operator's question from
a distributed join into a single contiguous range read. Storage is cheap;
cross-node joins are not.

The second decisive difference is that the *row key is the only index*. In SQL,
adding `WHERE severity = 'Critical'` to a query is a matter of adding an index
later. In HBase, if severity is not in the key, that query is a full-table scan
forever — unless a second table is built keyed by severity. Schema design and
query performance are the same decision, made once, before any data is loaded.

---

## 5. How HBase differs from Hive for operational data access

Both sit on HDFS in the Hadoop ecosystem, and both would happily store these 500
records. They answer different questions.

| | HBase | Hive |
|---|---|---|
| Purpose | Operational / OLTP-like random access | Analytical / OLAP batch |
| Access unit | Single row by key | Whole table or partition |
| Latency | Milliseconds | Seconds to minutes |
| Interface | `get`, `scan`, `put`, `delete` | HiveQL (SQL-like) |
| Execution | Direct RegionServer read | Tez / MapReduce / Spark job |
| Updates | Row-level `put` / `delete`, native versioning | Append-oriented; updates awkward |
| Aggregation | Weak — no `GROUP BY`, client-side counting | Native and efficient |
| Joins | None | Full support |
| Concurrency | High, thousands of concurrent readers | Low, job-oriented |

**Applied to this business case:**

*Belongs in HBase* — "What is RTR-EDM-020's event history?" (BQ2), "What are its
five most recent events?" (BQ3), "Pull up EVT-000018" (BQ1), and the status
updates in Part 7. These are single-device, low-latency, high-concurrency
operations fired by an operations console while an engineer is on the phone. Each
is answered from the row key in milliseconds.

*Belongs in Hive* — "What is the mean time to resolution by device type per
region, quarter over quarter?", "Which error codes correlate with subsequent
hardware failure within 48 hours?", "Rank cities by unresolved Critical events."
These scan the entire dataset, aggregate, and join. In HBase they are full-table
scans that compete with live event ingestion for RegionServer resources. In Hive
they are ordinary SQL over the HDFS copy, running on batch resources that never
touch the serving path.

Note that BQ4–BQ10 in `scripts/business_queries.hbase` are implemented as HBase
filter scans because the assignment requires it — and the scripts document
honestly that at production volume those particular queries belong in Hive. That
is the analytical takeaway, not a defect in the implementation.

Hive can also read an HBase table through `HBaseStorageHandler`, giving SQL over
live operational data — convenient, but the scan cost is HBase's, not Hive's.

---

## 6. What role Zeppelin provides in the proposed solution

Zeppelin is the presentation and reasoning layer. HDFS and HBase have no user
interface beyond a shell and an admin web page; neither produces an artefact a
manager or an assessor can read.

- **Executable documentation.** Markdown narrative and runnable code sit in one
  document, so the business justification travels with the command that
  implements it. A reader sees *why* `DeviceID#ReverseTimestamp#EventID` was
  chosen next to the scan that proves it works.
- **Reproducible demonstration.** Paragraphs re-run on demand. The Part 12
  demonstration is a linear pass through one notebook rather than a sequence of
  remembered shell commands.
- **Multi-interpreter workspace.** `%sh`, `%md`, `%spark`, `%jdbc` and `%angular`
  in one notebook — shell commands against HBase, HDFS inspection, and Spark
  analysis without leaving the page.
- **Visualisation.** Severity distributions and regional breakdowns render as
  charts directly from query output. HBase Shell prints text; a stakeholder
  reads a bar chart.
- **Collaboration and export.** The notebook exports to a single JSON file, which
  is what is committed to `zeppelin/bell_network_event_analysis.json` and what
  the assessor imports.

**Honest scoping.** The Zeppelin distribution in most Ambari/HDP course
environments has no HBase interpreter enabled by default. This implementation
therefore executes HBase Shell operations via the `%sh` interpreter — genuinely
executed from Zeppelin — and, where a paragraph documents a command that was run
in a terminal instead, the notebook says so explicitly. Section 16 of the
specification requires that distinction, and the notebook honours it.

---

## 7. Data-access patterns the system must support

Derived from the Network Operations questions in §2 of the specification and
mapped to the implementation.

| # | Access pattern | Business question | Row-key served? | Implementation |
|---|---|---|---|---|
| P1 | Point lookup by full key | "Show me event EVT-000018." | **Yes** — O(1) | `get` (BQ1) |
| P2 | All events for one device | "What has this router reported?" | **Yes** — O(matches) | Bounded scan (BQ2) |
| P3 | N most recent for one device | "What happened just before the outage?" | **Yes** — O(N) | Bounded scan + `LIMIT` (BQ3) |
| P4 | Time window for one device | "What did it do in February?" | **Yes** — O(window) | Key-range scan (BQ12a) |
| P5 | Filter by severity | "Which events are Critical?" | No — O(table) | `SingleColumnValueFilter` (BQ4, BQ5) |
| P6 | Filter by status | "What is unresolved?" | No — O(table) | `SingleColumnValueFilter` (BQ6, BQ7) |
| P7 | Filter by geography | "Which areas are degrading?" | No — O(table) | `SingleColumnValueFilter` (BQ8, BQ9) |
| P8 | Filter by error code | "Is NET-503 recurring?" | No — O(table) | `SingleColumnValueFilter` (BQ10) |
| P9 | Filter by device type | "Are routers worse than switches?" | **Partly** — prefix aligns | Key range *or* filter (BQ11) |
| P10 | Global time window | "Everything in February." | No — O(table) | Value filter on `event:timestamp` (BQ12b) |
| P11 | Status mutation with history | "Move this to Investigating." | **Yes** — O(1) | `put` + `VERSIONS` (Part 7) |

**The pattern that decides the schema.** P1–P4 and P11 are device-scoped, latency-
sensitive, and fired continuously by the operations console. P5–P10 are
aggregate reporting questions, run occasionally, tolerant of seconds of latency.
A row key can be optimised for one class or the other, not both. This design
optimises for P1–P4 and P11 — 5 of 11 patterns, but the 5 that the specification
identifies as the primary operational need — and accepts full scans for the rest.

`row_key_analysis.md` sets out the alternatives that were considered and rejected,
including what the design would look like if regional reporting were the dominant
pattern instead.
