#!/usr/bin/env python3
"""Cut a time window out of a SensorForge log, optionally removing personal details.

    python tools/trim_log.py <log folder> <from> <to> <output folder> [--anonymize]

<from> and <to> are ISO times ("2026-10-31T10:50:00+02:00", "2026-10-31T08:50:00Z").
Every CSV listed in session.json keeps the rows whose phone receive time (rx_unix_ms,
or unix_time_ms for markers) falls inside the window; values are copied byte for byte.
session.json gets the new start/end times, row counts and only the events in the window.

--anonymize removes the subject details (age, height, weight, sex, skin type), subject id,
custom fields, notes and the location from the environment snapshots. Weather and phone
state are kept.
"""
import argparse
import csv
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(t):
    return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1000


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log")
    ap.add_argument("start")
    ap.add_argument("end")
    ap.add_argument("out")
    ap.add_argument("--anonymize", action="store_true")
    args = ap.parse_args()

    src, out = Path(args.log), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    a_ms, b_ms = parse(args.start), parse(args.end)
    m = json.loads((src / "session.json").read_text())

    for stream in m["streams"]:
        with open(src / stream["file"], newline="") as fin, open(out / stream["file"], "w", newline="") as fout:
            reader, writer = csv.reader(fin), csv.writer(fout, lineterminator="\n")
            header = next(reader)
            writer.writerow(header)
            col = header.index("rx_unix_ms" if "rx_unix_ms" in header else "unix_time_ms")
            n = 0
            for row in reader:
                if a_ms <= int(row[col]) < b_ms:
                    writer.writerow(row)
                    n += 1
        stream["rowCount"] = n
        print(f"{stream['file']:24s} {n:>9,} rows")

    m["startedAt"], m["endedAt"] = iso(a_ms), iso(b_ms)
    m["events"] = [e for e in m.get("events", []) if a_ms <= parse(e["at"]) < b_ms]
    m["id"] = str(uuid.uuid4()).upper()
    excerpt = f"Excerpt {iso(a_ms)} – {iso(b_ms)} of a longer log."
    m["notes"] = f"{m['notes']}\n{excerpt}" if m.get("notes") else excerpt

    if args.anonymize:
        ctx = m.get("context") or {}
        for key in ("subject", "subjectId", "customFields"):
            ctx.pop(key, None)
        for key in ("environmentAtStart", "environmentAtEnd"):
            env = m.get(key) or {}
            env.pop("location", None)
            env["notes"] = []
        m["notes"] = excerpt

    (out / "session.json").write_text(json.dumps(m, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    print("wrote", out)


if __name__ == "__main__":
    main()
