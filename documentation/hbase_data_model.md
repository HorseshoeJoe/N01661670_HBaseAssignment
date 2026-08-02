# HBase Data Model

**Part 3 of the assignment specification.**

---

## 1. Table

```
Table name : network_events
Namespace  : default
```

One table. HBase has no joins, so the model denormalises every attribute an
operator needs onto each event row. A relational design would split this into
`Device`, `Location`, `EventType` and `Event`; here that would turn the primary
operator question into a four-way distributed join.

---

## 2. Row key

```
DeviceID # ReverseTimestamp # EventID
```

Concrete example, from the first record in the dataset:

```
CSV row : EVT-000034 , AP-OTT-026 , 2026-01-01 10:02:41 , ...
Row key : AP-OTT-026#9223370269593014807#EVT-000034
```

### Construction

| Component | Width | Source | Purpose |
|---|---|---|---|
| `DeviceID` | 10–11 chars | `DeviceID` | Groups a device's events contiguously; leading component so it can anchor a scan |
| `#` | 1 char | literal `0x23` | Separator — sorts below `0`–`9` and `A`–`Z`, so it can never be confused with data |
| `ReverseTimestamp` | **19 chars, fixed** | derived | Orders a device's events newest-first |
| `#` | 1 char | literal | Separator |
| `EventID` | 10 chars | `EventID` | Guarantees uniqueness if two events share a device and a millisecond |

Total: 41–42 bytes. `AP-` device IDs are 10 characters (`AP-OTT-026`); the other
five prefixes are 11 (`RTR-EDM-020`), giving 81 keys of 41 bytes and 419 of 42.

**Why the variable first component is safe here.** Two conditions hold, and both
were checked against the data:

1. **No DeviceID is a prefix of another** (verified across all 50). If one were —
   say `RTR-EDM-02` and `RTR-EDM-020` — their key ranges would interleave and a
   `DeviceID#` → `DeviceID$` scan could return rows for the wrong device.
2. **The separator `#` (`0x23`) sorts below every character appearing in a
   DeviceID** (digits start at `0x30`, uppercase at `0x41`, `-` is `0x2D`). So
   `AP-OTT-026#...` always sorts before any longer ID beginning with the same
   characters.

Together these guarantee that all 50 devices occupy 50 contiguous blocks in the
globally sorted table — verified by sorting all 500 generated keys and confirming
exactly 50 runs.

Fixed width matters *within* the key, not across it: the **reverse timestamp must
be exactly 19 characters** (see below), because that component is compared
positionally against others of the same kind. The DeviceID component is compared
against a `#` terminator, so its length may vary.

### The reverse timestamp

```
ReverseTimestamp = (2^63 - 1) - epoch_millis(EventTimestamp)
                 = 9223372036854775807 - epoch_millis
```

zero-padded to 19 digits.

Worked example:

```
EventTimestamp   2026-01-01 10:02:41 UTC
epoch_millis     1767261761000
2^63 - 1         9223372036854775807
subtract         9223370269593014807
zfill(19)        9223370269593014807     <- already 19 digits
```

Two properties make this work, and both are required:

1. **Inversion.** A *later* real timestamp yields a *smaller* reverse value.
   HBase sorts row keys in ascending lexicographic byte order, so the newest
   event for a device sorts first. "Most recent five events" becomes a scan with
   `LIMIT => 5` — no sort step, no full history read.

2. **Fixed width.** Zero-padding to 19 characters is not cosmetic. HBase compares
   row keys as **byte arrays, not numbers**. Without padding, `999...` (18 chars)
   would sort after `1000...` (19 chars) and the ordering would silently break
   for a subset of rows. Since `2^63 - 1` has 19 digits and epoch milliseconds
   for any plausible date are far smaller, every value here is naturally 19
   digits — but `zfill(19)` is applied explicitly so the invariant is enforced
   rather than assumed.

Verified in `scripts/data_load/generate_puts.py`; the assertion that lexicographic
key order equals reverse-chronological order is checked against all 18 events of
`RTR-EDM-020` during generation.

### How EventTimestamp enters the key

Per §12 of the specification: `EventTimestamp` is parsed from
`'YYYY-MM-DD HH:MM:SS'` (treated as UTC), converted to epoch milliseconds,
subtracted from `2^63 - 1`, zero-padded to 19 characters, and placed as the
middle key component. It is **also** retained verbatim as `event:timestamp`, for
three reasons: the reverse form is unreadable to a human; global (cross-device)
time-range queries need a comparable value in a column; and it allows the row key
to be recomputed and the table reloaded if the key strategy is ever revised.

---

## 3. Column families

Four families. Each is stored as a separate set of HFiles, so a query touching
one family does not read the others off disk — family design is a physical I/O
decision, not a naming convention.

| Family | Versions | Bloom | Contents | Rationale |
|---|---|---|---|---|
| `event` | 1 | ROW | What happened | Immutable once emitted — an event's facts never change |
| `device` | 1 | ROW | Which device | Static reference data |
| `location` | 1 | ROW | Where | Static; separated so operational queries skip these bytes |
| `status` | **5** | ROW | Lifecycle state | **The only mutable attribute** — history is the audit trail |

**Why four and not one.** A single family would force every read to
deserialise all eleven columns. Operational triage reads `event` and `status`
constantly and `location` only when dispatching a technician; splitting them
means the common path reads roughly half the bytes.

**Why four and not eight.** HBase flushes MemStores per region, not per family:
when one family flushes, all families in that region flush, producing small
HFiles for the low-traffic ones and increasing compaction pressure. The HBase
Reference Guide recommends staying at or below three families and treats more as
a smell. Four is a considered compromise — it is one above the guideline, chosen
because `status` genuinely needs a different `VERSIONS` setting from the other
three and that alone justifies a family boundary. A production revision would
likely merge `device` and `location` into a single `meta` family, since both are
static and always read together.

**Why `VERSIONS => 5` on `status` only.** Incident status moves
`Open → Investigating → Resolved → Closed`. Four states, plus headroom for a
re-open. Keeping five versions gives Network Operations a full audit trail of how
an incident progressed, with per-cell HBase timestamps, at no cost in extra
tables or application code. The other three families keep one version because
storing history of immutable data is pure waste.

**Why `BLOOMFILTER => 'ROW'`.** A row Bloom filter lets a `get` skip HFiles that
provably cannot contain the requested key. This is what keeps BQ1 O(1) as the
table grows and the number of HFiles per region rises.

---

## 4. Column qualifiers

| Family:Qualifier | Source CSV column | Example | Notes |
|---|---|---|---|
| `event:type` | `EventType` | `ConnectivityLoss` | 9 distinct values |
| `event:severity` | `Severity` | `Critical` | Informational / Warning / High / Critical |
| `event:error_code` | `ErrorCode` | `NET-503` | 9 distinct codes |
| `event:description` | `Description` | `Device connectivity interrupted` | Human-readable |
| `event:timestamp` | `EventTimestamp` | `2026-01-01 10:02:41` | Readable form of the key component |
| `device:id` | `DeviceID` | `AP-OTT-026` | Redundant with key prefix — see below |
| `device:type` | `DeviceType` | `Access Point` | 6 distinct values |
| `location:region` | `Region` | `Eastern` | Eastern / Western / Central / Atlantic |
| `location:city` | `City` | `Ottawa` | 10 cities |
| `location:province` | `Province` | `Ontario` | |
| `status:state` | `Status` | `Open` | Open / Investigating / Resolved / Closed |

`EventID` is not stored as its own qualifier — it is the key's third component
and is recoverable by splitting the key on `#`.

**On the deliberate redundancy.** `device:id` duplicates the key prefix and
`event:timestamp` duplicates the reverse-timestamp component. Both are stored
anyway. Parsing a row key client-side to recover them is possible but brittle,
and the readable timestamp is required for the cross-device time-range query
(BQ12b), which has no key-based equivalent. In HBase, storage is the cheap
resource; a redundant column that removes a parsing step or enables a query is a
good trade.

---

## 5. Complete conceptual record

```
Row Key: AP-OTT-026#9223370269593014807#EVT-000034

  Column Family: event
    event:type          -> ConnectivityLoss
    event:severity      -> High
    event:error_code    -> NET-503
    event:description   -> Device connectivity interrupted
    event:timestamp     -> 2026-01-01 10:02:41

  Column Family: device
    device:id           -> AP-OTT-026
    device:type         -> Access Point

  Column Family: location
    location:region     -> Eastern
    location:city       -> Ottawa
    location:province   -> Ontario

  Column Family: status
    status:state        -> Closed        (VERSIONS => 5)
```

11 cells per event. 500 events → 5,500 cells, which is exactly what
`scripts/data_load/load_data.hbase` writes.

---

## 6. Table creation statement

```ruby
create 'network_events',
  {NAME => 'event',    VERSIONS => 1, BLOOMFILTER => 'ROW'},
  {NAME => 'device',   VERSIONS => 1, BLOOMFILTER => 'ROW'},
  {NAME => 'location', VERSIONS => 1, BLOOMFILTER => 'ROW'},
  {NAME => 'status',   VERSIONS => 5, BLOOMFILTER => 'ROW'}
```

A pre-split variant is provided in `scripts/create_table_presplit.hbase` and
discussed in `performance_analysis.md`.

---

## 7. Access patterns this model supports

| Access pattern | Mechanism | Cost |
|---|---|---|
| Specific event by full key | `get` | O(1) |
| All events for a device | Scan `DeviceID#` → `DeviceID$` | O(matches) |
| N most recent for a device | Same scan + `LIMIT` | O(N) |
| Device events in a time window | Scan between two reverse timestamps | O(window) |
| All devices of one type | Scan `RTR-` → `RTS-` (prefix alignment) | O(matches) |
| Status change with audit trail | `put` to `status:state` | O(1) |
| By severity / status / region / city / error code | Full scan + `SingleColumnValueFilter` | O(table) |
| Global time window | Full scan + value filter on `event:timestamp` | O(table) |

The first six are served by the row key. The last two are not, and the model does
not pretend otherwise — see `row_key_analysis.md` §5 for what a schema optimised
for the reporting queries would look like, and why it was rejected.

---

## 8. Design principle

The specification's stated objective is that HBase modelling is driven by
**access patterns rather than relational normalisation**. Every decision above
follows from that:

- One denormalised table, because joins do not exist.
- Device first in the key, because device history is the primary operator question.
- Timestamp reversed, because "most recent" is asked far more often than "oldest".
- EventID last, because it contributes uniqueness but nothing to any query.
- Families split by mutability and read frequency, because families are physical.
- `VERSIONS => 5` only where history has business meaning.
- Attributes duplicated wherever redundancy removes work from the read path.
