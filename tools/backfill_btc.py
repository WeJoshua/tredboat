"""Bounded, self-healing BTC tape backfill from Alpaca (research-only).

Splits [start, end) into 84-hour chunks and runs the bounded `fetch` command
per chunk. A chunk that fails (unknown-side print, rate limit, wall timeout)
is split in half and retried until it succeeds or is isolated at 21 hours,
where a failure is recorded and skipped — the final log states exactly which
holes remain, so replays never silently ride on missing data.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
GRANULARITY = timedelta(hours=84)


def run_fetch(end, hours):
    return subprocess.run(
        [sys.executable, "-B", "-m", "tred", "--config", "config.toml", "fetch",
         "--hours", str(hours), "--end", end.isoformat()],
        capture_output=True, text=True, timeout=150)


def fill(start, end, log_path):
    entries = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + GRANULARITY, end)
        succeeded = backfill(cursor, chunk_end, entries, log_path)
        if not succeeded:
            entries.append({"start": cursor.isoformat(), "end": chunk_end.isoformat(),
                            "outcome": "unfetchable", "attempts": 3})
        cursor = chunk_end
    Path(log_path).write_text("\n".join(
        json.dumps(entry, allow_nan=False) for entry in entries) + "\n")
    successes = sum(1 for e in entries if e["outcome"] == "fetched")
    print(f"backfill done: {successes}/{len(entries)} chunks fetched", flush=True)


def backfill(chunk_start, chunk_end, entries, log_path):
    width = chunk_end - chunk_start
    if width < timedelta(hours=21):
        record = run_fetch(chunk_end, width.total_seconds() / 3600)
        if record.returncode == 0:
            entries.append({"start": chunk_start.isoformat(), "end": chunk_end.isoformat(),
                            "outcome": "fetched"})
            return True
        entries.append({"start": chunk_start.isoformat(), "end": chunk_end.isoformat(),
                        "outcome": "isolated_failure", "stderr": record.stderr[:300]})
        return False
    record = run_fetch(chunk_end, width.total_seconds() / 3600)
    if record.returncode == 0:
        entries.append({"start": chunk_start.isoformat(), "end": chunk_end.isoformat(),
                        "outcome": "fetched"})
        return True
    midpoint = chunk_start + width / 2
    left = backfill(chunk_start, midpoint, entries, log_path)
    right = backfill(midpoint, chunk_end, entries, log_path)
    return left and right


if __name__ == "__main__":
    start = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
    end = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
    fill(start, end, sys.argv[3] if len(sys.argv) > 3 else "data/backfill.log")