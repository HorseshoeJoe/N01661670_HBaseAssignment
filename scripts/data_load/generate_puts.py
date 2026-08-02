#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
generate_puts.py
================
Converts network_events.csv into an HBase Shell script of `put` commands.

Row-key strategy (selected design, see documentation/row_key_analysis.md):

    DeviceID # ReverseTimestamp # EventID

where

    ReverseTimestamp = (2**63 - 1) - epoch_millis(EventTimestamp)

zero-padded to 19 characters so that every key has identical width and
lexicographic byte ordering therefore equals reverse-chronological ordering.

Column families / qualifiers:

    event    : type, severity, error_code, description, timestamp
    device   : id, type
    location : region, city, province
    status   : state

Usage
-----
    python generate_puts.py \
        --input  ../../dataset/network_events.csv \
        --output load_data.hbase \
        --table  network_events

Python 2.7 and Python 3.x compatible (HDP sandboxes often ship Python 2.7).
"""

from __future__ import print_function

import argparse
import calendar
import csv
import io
import time

LONG_MAX = 2 ** 63 - 1          # 9223372036854775807
TS_FORMAT = "%Y-%m-%d %H:%M:%S"
KEY_WIDTH = 19                  # digits in Long.MAX_VALUE


def epoch_millis(ts_string):
    """'2026-01-01 10:02:41' -> epoch milliseconds (UTC)."""
    struct = time.strptime(ts_string.strip(), TS_FORMAT)
    return calendar.timegm(struct) * 1000


def reverse_timestamp(ts_string):
    """Zero-padded reverse timestamp, fixed width, newest sorts first."""
    return str(LONG_MAX - epoch_millis(ts_string)).zfill(KEY_WIDTH)


def build_row_key(device_id, ts_string, event_id):
    return "%s#%s#%s" % (device_id, reverse_timestamp(ts_string), event_id)


def esc(value):
    """Escape a value for a single-quoted HBase Shell (JRuby) string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# (column family, qualifier, csv column)
COLUMN_MAP = [
    ("event",    "type",        "EventType"),
    ("event",    "severity",    "Severity"),
    ("event",    "error_code",  "ErrorCode"),
    ("event",    "description", "Description"),
    ("event",    "timestamp",   "EventTimestamp"),
    ("device",   "id",          "DeviceID"),
    ("device",   "type",        "DeviceType"),
    ("location", "region",      "Region"),
    ("location", "city",        "City"),
    ("location", "province",    "Province"),
    ("status",   "state",       "Status"),
]


def main():
    parser = argparse.ArgumentParser(description="CSV -> HBase Shell puts")
    parser.add_argument("--input", default="../../dataset/network_events.csv")
    parser.add_argument("--output", default="load_data.hbase")
    parser.add_argument("--table", default="network_events")
    args = parser.parse_args()

    with open(args.input, "r") as fh:
        rows = list(csv.DictReader(fh))

    lines = [
        "# ------------------------------------------------------------------",
        "# load_data.hbase  --  GENERATED FILE, do not edit by hand.",
        "# Produced by scripts/data_load/generate_puts.py",
        "# Source dataset : network_events.csv (%d records)" % len(rows),
        "# Target table   : %s" % args.table,
        "# Row key        : DeviceID#ReverseTimestamp#EventID",
        "# ReverseTimestamp = (2^63 - 1) - epoch_millis, padded to 19 digits",
        "# Run with       : hbase shell load_data.hbase",
        "# ------------------------------------------------------------------",
        "",
    ]

    for row in rows:
        key = build_row_key(row["DeviceID"], row["EventTimestamp"], row["EventID"])
        lines.append("# %s  %s  %s" % (row["EventID"], row["DeviceID"], row["EventTimestamp"]))
        for family, qualifier, column in COLUMN_MAP:
            lines.append(
                "put '%s', '%s', '%s:%s', '%s'"
                % (args.table, esc(key), family, qualifier, esc(row[column]))
            )
        lines.append("")

    lines += [
        "# ---- verification -------------------------------------------------",
        "count '%s'" % args.table,
        "exit",
        "",
    ]

    with io.open(args.output, "w", newline="\n") as fh:
        fh.write(u"\n".join(lines))

    print("Wrote %s" % args.output)
    print("  records : %d" % len(rows))
    print("  puts    : %d" % (len(rows) * len(COLUMN_MAP)))
    if rows:
        sample = rows[0]
        print("  sample key : %s"
              % build_row_key(sample["DeviceID"], sample["EventTimestamp"], sample["EventID"]))


if __name__ == "__main__":
    main()
