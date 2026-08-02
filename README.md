# Telecommunications Network Event Management with Apache HBase and Zeppelin

| | |
|---|---|
| **Student name** | *Henry Dineros* |
| **Student ID** | *N01661670* |
| **Course** | Big Data |
| **Assignment** | Telecommunications Network Event Management with Apache HBase and Zeppelin |
| **Business context** | Bell Canada (synthetic — see disclaimer) |
| **Work mode** | Individual |
| **HDFS path** | `/user/<username>/bell/network_events/network_events.csv` |
| **HBase table** | `network_events` |
| **Row key** | `DeviceID#ReverseTimestamp#EventID` |
| **Zeppelin notebook** | `Bell Network Event Management – HBase Analysis` |

> **Before submitting:** replace the name and ID above, and replace `<username>`
> in the HDFS path with the actual value from your cluster (the scripts use
> `$USER`, so run `whoami` and paste the result).

---

## 1. Business-case summary

A national telecommunications provider operates a geographically distributed
network of routers, switches, wireless infrastructure, access points, gateways
and service infrastructure. Every component emits operational events
continuously — connectivity loss, performance degradation, hardware failure,
configuration change, service interruption, high latency, packet loss, device
recovery and scheduled maintenance.

As the fleet grows, event volume outgrows a relational store. The organisation
needs a **scalable historical repository** that still gives **millisecond access
to the event history of any individual device**, so Network Operations can answer:

- What events has this device generated?
- What are its most recent events?
- Which devices produced Critical events?
- Which events remain unresolved?
- Are particular error codes recurring?
- Which geographic areas are degrading?

This repository implements a prototype answer using HDFS, HBase and Zeppelin.

**The central point it demonstrates:** in HBase the row key is the *only* index,
so the schema must be designed around the questions that will be asked —
**access patterns, not relational normalisation**.

---

## 2. Solution architecture

```
              network_events.csv
                      │
                      ▼
        ┌─────────────────────────────┐
        │          Hadoop HDFS          │   Distributed source storage
        │   immutable system of record   │   replicated, rebuildable
        └───────────────┬──────────────┘
                        ▼
        ┌─────────────────────────────┐
        │         Apache HBase          │   Network event data store
        │  distributed NoSQL, key-based  │   O(1) get, ordered scans
        └───────────────┬──────────────┘
                        ▼
        ┌─────────────────────────────┐
        │       Apache Zeppelin         │   Interactive analysis,
        │  documentation · results · demo │   documentation, demonstration
        └─────────────────────────────┘
```

| Component | Responsibility |
|---|---|
| **HDFS** | Stores the original CSV. Replicated, immutable, optimised for large sequential reads. The system of record — if the HBase table is dropped or re-keyed, it is rebuilt from here. |
| **HBase** | Serving layer, stored on HDFS. Key-based retrieval, device event history, row-level mutation with cell versioning. |
| **HBase Shell** | Administrative and data interface: `create`, `describe`, `put`, `get`, `scan`, `delete`, filters. |
| **Zeppelin** | Interactive notebook. Executes shell operations via `%sh`, documents the reasoning next to the commands, and is the demonstration artefact. |

---

## 3. Environment requirements

Developed against the course Big Data environment accessed through **VMware Cloud
Director**, managed by **Apache Ambari**.

| Requirement | Notes |
|---|---|
| Apache Hadoop HDFS | 2.7+ — running, `NameNode` and `DataNode` green in Ambari |
| Apache HBase | 1.1+ — `HBase Master` and `RegionServer` green in Ambari |
| Apache ZooKeeper | Required by HBase |
| Apache Zeppelin | 0.7+ — `%sh` interpreter enabled |
| Python | 2.7 or 3.x — `generate_puts.py` supports both |
| Shell access | SSH to a cluster node, or Ambari → Hosts → *Web Terminal* |

**Ambari service check before starting:** HDFS, HBase and Zeppelin must all show
green. If HBase is red, start it from Ambari → HBase → *Service Actions* → *Start*
and wait for the RegionServer to register.

---

## 4. Repository structure

```
hbase-network-event-assignment/
│
├── README.md                       <- this file
├── WALKTHROUGH.md                  <- step-by-step execution order + screenshot checklist
├── REQUIREMENTS_COVERAGE.md        <- maps every specification requirement to its artefact
│
├── dataset/
│   └── network_events.csv          <- 500 synthetic records
│
├── scripts/
│   ├── hdfs_setup.sh               <- Part 2: HDFS mkdir / put / ls / cat
│   ├── create_table.hbase          <- Part 5: create, list, describe
│   ├── create_table_presplit.hbase <- optional: pre-split hotspot mitigation
│   ├── data_load/
│   │   ├── generate_puts.py        <- CSV -> row keys -> put statements
│   │   └── load_data.hbase         <- GENERATED: 5,500 puts
│   ├── crud_operations.hbase       <- Part 7: insert / get / update / delete / scan
│   ├── business_queries.hbase      <- Part 8: 12 business requirements
│   └── filters.hbase               <- Part 9: 6 filter techniques
│
├── zeppelin/
│   ├── bell_network_event_analysis.json   <- importable notebook, 11 sections
│   └── build_notebook.py                  <- regenerates the JSON
│
├── documentation/
│   ├── business_analysis.md        <- Part 1
│   ├── hbase_data_model.md         <- Part 3
│   ├── row_key_analysis.md         <- Part 4
│   └── performance_analysis.md     <- Part 11
│
└── screenshots/
    ├── hdfs/  hbase_table/  data_loading/
    ├── crud_operations/  business_queries/
    └── filters/  zeppelin/
```

---

## 5. Data model

**Table:** `network_events`

### Row key

```
DeviceID # ReverseTimestamp # EventID

AP-OTT-026#9223370269593014807#EVT-000034
└───┬────┘ └────────┬───────┘ └────┬────┘
 DeviceID    ReverseTimestamp    EventID
10-11 chars   19 chars, fixed    10 chars
```

Keys are 41 or 42 bytes — `AP-` device IDs are 10 characters, the other five
prefixes are 11. That variation is safe because no DeviceID is a prefix of
another and `#` (`0x23`) sorts below every character used in a DeviceID, so all
50 devices still occupy 50 contiguous blocks. See
[`documentation/hbase_data_model.md`](documentation/hbase_data_model.md) §2.

```
ReverseTimestamp = (2^63 - 1) - epoch_millis(EventTimestamp)
```

zero-padded to 19 characters.

**Why reversed** — a later real timestamp yields a *smaller* reverse value, so
HBase's ascending byte order puts the newest event for a device **first**. "Five
most recent" is a scan with `LIMIT => 5`: no sort, no full history read.

**Why zero-padded** — HBase compares row keys as **byte arrays, not numbers**.
Without fixed width, an 18-digit value would sort after a 19-digit one and the
ordering would silently break.

### Column families

| Family | Versions | Qualifiers |
|---|---|---|
| `event` | 1 | `type`, `severity`, `error_code`, `description`, `timestamp` |
| `device` | 1 | `id`, `type` |
| `location` | 1 | `region`, `city`, `province` |
| `status` | **5** | `state` |

`VERSIONS => 5` on `status` gives the incident lifecycle
(`Open → Investigating → Resolved → Closed`) a free audit trail. The other three
families keep one version because an event's facts never change once emitted.

11 cells per event × 500 events = **5,500 cells**.

Full detail: [`documentation/hbase_data_model.md`](documentation/hbase_data_model.md)
· [`documentation/row_key_analysis.md`](documentation/row_key_analysis.md)

---

## 6. Execution instructions

Full step-by-step with screenshot checkpoints: **[`WALKTHROUGH.md`](WALKTHROUGH.md)**.
Condensed version:

```bash
# ---- 0. Get the repo onto the cluster node --------------------------
git clone <your-repo-url>
cd hbase-network-event-assignment

# ---- 1. Part 2: HDFS ------------------------------------------------
chmod +x scripts/hdfs_setup.sh
cd dataset && ../scripts/hdfs_setup.sh && cd ..
#   creates /user/$USER/bell/network_events/ and uploads the CSV

# ---- 2. Part 5: create the HBase table ------------------------------
hbase shell scripts/create_table.hbase
#   the leading disable/drop will error on a first run -- expected

# ---- 3. Part 6: generate and load the data --------------------------
cd scripts/data_load
python generate_puts.py --input ../../dataset/network_events.csv \
                        --output load_data.hbase --table network_events
hbase shell load_data.hbase
cd ../..
#   verify: count 'network_events'  ->  500 row(s)

# ---- 4. Part 7: CRUD ------------------------------------------------
hbase shell scripts/crud_operations.hbase

# ---- 5. Part 8: business queries ------------------------------------
hbase shell scripts/business_queries.hbase

# ---- 6. Part 9: filters ---------------------------------------------
hbase shell scripts/filters.hbase
```

**Run order matters.** `business_queries.hbase` states expected counts for the
clean 500-record load. `crud_operations.hbase` leaves 502 rows behind (it inserts
3 test events and deletes 1). Either run the business queries **first**, or
subtract the 2 surviving test events when comparing counts.

To reset at any point:

```bash
hbase shell scripts/create_table.hbase          # drops and recreates
hbase shell scripts/data_load/load_data.hbase   # reloads 500 records
```

---

## 7. Zeppelin notebook instructions

**Import:**

1. Ambari → Zeppelin Notebook → *Quick Links* → **Zeppelin UI**
   (or `http://<zeppelin-host>:9995`)
2. On the home screen click **Import note**
3. **Select JSON File** → choose `zeppelin/bell_network_event_analysis.json`
4. The note appears as **Bell Network Event Management – HBase Analysis**

**Before running:**

1. Open the first paragraph and fill in **student name and ID**
2. Replace `${USER}` in the HDFS paragraphs if your shell does not expand it —
   run `whoami` on the cluster and hard-code the value
3. Confirm the `sh` interpreter is bound: notebook gear icon → *Interpreter
   binding* → `sh` enabled

**Running:** execute paragraphs top to bottom with `Shift+Enter`, or *Run all
paragraphs*. Markdown paragraphs render immediately; `%sh` paragraphs invoke
HBase Shell on the cluster and return its output.

**Export for submission:** notebook menu → *Export this note* → commit the
resulting JSON back to `zeppelin/`, so the committed file contains your executed
output.

**Regenerate the notebook** after editing `build_notebook.py`:

```bash
cd zeppelin && python build_notebook.py
```

### Honest scoping of Zeppelin execution

Zeppelin in most Ambari/HDP environments has **no HBase interpreter** enabled by
default. This implementation therefore runs HBase Shell through the **`%sh`
interpreter** — genuinely executed from Zeppelin, not merely pasted. Paragraphs
are labelled:

- **[EXECUTED IN ZEPPELIN]** — runs in the notebook via `%sh`
- **[EXECUTED EXTERNALLY — DOCUMENTED HERE]** — run in a terminal; command and
  verified output reproduced, with a screenshot committed

Specification §16 requires this distinction. Nothing is claimed to have run in
Zeppelin unless it did.

---

## 8. Results summary

| # | Business requirement | Access path | Result |
|---|---|---|---|
| BQ1 | Specific event record | **[KEY]** `get` | 1 row |
| BQ2 | Device event history (`RTR-EDM-020`) | **[KEY]** range scan | 18 rows |
| BQ3 | Five most recent for that device | **[KEY]** range + `LIMIT` | 5 rows |
| BQ4 | Critical severity | [FILTER] | 96 rows |
| BQ5 | High or Critical | [FILTER] | 247 rows |
| BQ6 | Open status | [FILTER] | 122 rows |
| BQ7 | Investigating status | [FILTER] | 92 rows |
| BQ8 | Eastern region | [FILTER] | 192 rows |
| BQ9 | Ottawa | [FILTER] | 90 rows |
| BQ10 | Error code NET-503 | [FILTER] | 50 rows |
| BQ11 | Device type Router | **[KEY]** *or* [FILTER] | 107 rows |
| BQ12a | `RTR-EDM-020`, February 2026 | **[KEY]** range scan | 4 rows |
| BQ12b | All devices, February 2026 | [FILTER] | 77 rows |

**5 of 12 served directly by the row key** — and they are precisely the
device-centric questions the specification names as the primary operational
access patterns. The remaining 7 are aggregate reporting questions; at production
volume they belong in Hive over the HDFS copy.

Unresolved backlog: **214 events** (122 Open + 92 Investigating).

---

## 9. Key design decisions

1. **One denormalised table**, not four normalised ones — HBase has no joins, and
   a distributed four-way join is ruinous at scale.
2. **`DeviceID` leads the row key**, because device history is the primary
   operator question and the row key is the only index available to serve it.
3. **Timestamp reversed and zero-padded**, so "most recent" is free rather than a
   sort.
4. **`EventID` last** — it contributes uniqueness but nothing to any query.
5. **Four column families split by mutability and read frequency**, because
   families are a physical I/O boundary, not a naming convention.
6. **`VERSIONS => 5` on `status` only**, where version history has business
   meaning.
7. **A sequential event ID was rejected** as a row key — no device locality, and
   a monotonic key sends 100% of writes to one region permanently.

Reasoning: [`documentation/row_key_analysis.md`](documentation/row_key_analysis.md)
· [`documentation/performance_analysis.md`](documentation/performance_analysis.md)

---

## 10. Known limitations

- 500 rows in a single region — distribution and split behaviour are reasoned
  about rather than observed, and timing differences between access paths are
  within noise at this scale.
- Loading via 5,500 individual `put` RPCs; production would use `ImportTsv` +
  `completebulkload`.
- No secondary index, so 7 of 12 queries are full scans.
- No TTL and no compression configured.
- Zeppelin runs HBase Shell via `%sh` rather than a native interpreter.

Improvements are set out in
[`documentation/performance_analysis.md`](documentation/performance_analysis.md) §9.

---

## 11. Academic integrity and business disclaimer

This assignment was completed individually. All submitted code, commands, data
models and technical decisions are understood and explainable by the author.

Bell Canada is referenced solely to provide a recognisable real-world
telecommunications business context for this educational assignment. All
datasets, network events, device identifiers, error codes, system requirements,
architectures, technical designs and operational scenarios are **synthetic and
created exclusively for educational purposes**. Nothing here represents,
reproduces or implies knowledge of Bell Canada's actual internal systems, network
infrastructure, data architecture, technology implementation or operational
practices.
