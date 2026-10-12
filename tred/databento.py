"""Verified Databento GLBX trades export for MNQ/NQ research.

Streams the official historical HTTP API's CSV encoding (no pandas or DBN
dependency) into a canonical signed tape plus a tred.futures-tape.v1 coverage
manifest. The user's DATABENTO_API_KEY is used only for HTTP Basic auth and is
never written to disk, emitted, or placed in a URL.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .futures import MAX_BYTES, MAX_CLOSURES, MAX_RECORDS, timestamp
from .strategy import parse_time

GATEWAY = "https://hist.databento.com"
API_VERSION = "0"
DATASET = "GLBX.MDP3"
SYMBOL = re.compile(r"(?:MNQ|NQ)[HMUZ](?:\d{1,2}|\d{4})")
REQUIRED_FIELDS = {"ts_event", "ts_recv", "instrument_id", "action", "side", "price", "size", "sequence"}
MAX_JSON = 65536
CREATE = {"available", "missing"}
UTC = timezone.utc


def _request(method, url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        detail = error.read(4096).decode("utf-8", "replace").strip()
        try:
            detail = json.loads(detail).get("detail") or detail
        except (ValueError, AttributeError):
            pass
        raise ValueError(f"Databento HTTP {error.code}: {str(detail)[:300]}") from None


class _Bounded(io.RawIOBase):
    """Count response bytes and fail before a tape can exhaust memory."""

    def __init__(self, response, limit):
        self._response = response
        self._limit = limit
        self.total = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        chunk = self._response.read(len(buffer))
        if not chunk:
            return 0
        self.total += len(chunk)
        if self.total > self._limit:
            raise ValueError("Tape exceeds 256 MiB; export a shorter complete window")
        buffer[:len(chunk)] = chunk
        return len(chunk)


def _find_count(payload):
    if isinstance(payload, int) and not isinstance(payload, bool):
        return payload
    if isinstance(payload, dict):
        for key in ("count", "record_count"):
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        if "result" in payload:
            return _find_count(payload["result"])
    return None


def _condition_map(payload):
    """Accept both dict-of-dicts and list-of-records condition responses."""
    result = payload.get("result", payload) if isinstance(payload, dict) else payload
    if isinstance(result, dict) and DATASET in result:
        result = result[DATASET]
    condition = {}
    if isinstance(result, dict):
        entries = [dict(entry, **{"date": date}) for date, entry in result.items() if isinstance(entry, dict)]
    elif isinstance(result, list):
        entries = [entry for entry in result if isinstance(entry, dict)]
    else:
        entries = []
    for entry in entries:
        date = next((str(entry[key]) for key in ("date", "start_date", "day")
                     if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(entry.get(key, "")))), None)
        value = next((entry[key] for key in ("condition", "status") if key in entry), None)
        if date and value is not None:
            condition[date] = str(value).lower()
    return condition


class Databento:
    """Minimal client for the endpoints used by futures-fetch."""

    def __init__(self, key, *, gateway=GATEWAY, timeout=60, request=None):
        cleaned = key.strip() if isinstance(key, str) else ""
        if not cleaned or cleaned == "YOUR_API_KEY":
            raise ValueError("A Databento API key is required (set DATABENTO_API_KEY)")
        self.headers = {"authorization": "Basic " + base64.b64encode((cleaned + ":").encode()).decode(),
                        "user-agent": "tred-futures-fetch/1.0"}
        self._gateway = gateway.rstrip("/")
        self._timeout = timeout
        self._request = request or _request

    def _url(self, path, params=None):
        url = f"{self._gateway}/v{API_VERSION}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _json(self, method, path, data=None, params=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        headers = dict(self.headers, accept="application/json")
        with self._request(method, self._url(path, params), headers, body, self._timeout) as response:
            payload = response.read(MAX_JSON + 1)
        if len(payload) > MAX_JSON:
            raise ValueError("Databento JSON response exceeds the size budget")
        try:
            return json.loads(payload)
        except ValueError:
            raise ValueError("Databento returned an unreadable JSON response") from None

    def conditions(self, start_date, end_date):
        payload = self._json("GET", "metadata.get_dataset_condition",
                             params=[("dataset", DATASET), ("start_date", start_date), ("end_date", end_date)])
        return _condition_map(payload)

    def record_count(self, symbol, start, end):
        payload = self._json("POST", "metadata.get_record_count", data={
            "dataset": DATASET, "symbols": symbol, "schema": "trades", "stype_in": "raw_symbol",
            "start": _iso(start), "end": _iso(end)})
        return _find_count(payload)

    def resolve(self, symbol, start_date, end_date):
        payload = self._json("POST", "symbology.resolve", data={
            "dataset": DATASET, "symbols": symbol, "stype_in": "raw_symbol",
            "stype_out": "instrument_id", "start_date": start_date, "end_date": end_date})
        result = payload.get("result", payload) if isinstance(payload, dict) else None
        entries = result.get(symbol) if isinstance(result, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"Databento did not resolve {symbol}; check the contract and date range")
        ids = {str(entry["s"]) for entry in entries if isinstance(entry, dict) and entry.get("s") is not None}
        if len(ids) != 1:
            raise ValueError(f"{symbol} maps to multiple instruments in this window; use one dated outright contract")
        return ids.pop()

    def download(self, symbol, start, end, stream, gap_seconds):
        body = urllib.parse.urlencode({
            "dataset": DATASET, "symbols": symbol, "schema": "trades", "stype_in": "raw_symbol",
            "stype_out": "instrument_id", "start": _iso(start), "end": _iso(end), "encoding": "csv",
            "compression": "none", "pretty_px": "false", "pretty_ts": "false", "limit": str(MAX_RECORDS)}).encode()
        headers = dict(self.headers, accept="text/csv")
        rows, price_format, previous, previous_ns, closures = 0, "fixed9", None, None, []
        with self._request("POST", self._url("timeseries.get_range"), headers, body, self._timeout) as response:
            text = io.TextIOWrapper(io.BufferedReader(_Bounded(response, MAX_BYTES)), encoding="utf-8", newline="")
            reader = csv.DictReader(text)
            fields = list(reader.fieldnames or ())
            if not REQUIRED_FIELDS <= set(fields):
                raise ValueError(f"Databento CSV is missing columns: {sorted(REQUIRED_FIELDS - set(fields))}")
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            try:
                for row in reader:
                    rows += 1
                    if rows > MAX_RECORDS:
                        raise ValueError("Tape exceeds one million records; export a shorter complete window")
                    if row.get("action") != "T" or row.get("side") not in {"B", "A"}:
                        raise ValueError("Tape contains non-trade or unknown-side records; refusing to infer order flow")
                    stamp, ns = timestamp(row["ts_event"])
                    if previous_ns is not None:
                        if ns < previous_ns:
                            raise ValueError("Databento CSV is not sorted by event timestamp")
                        if (stamp - previous).total_seconds() > gap_seconds:
                            if len(closures) >= MAX_CLOSURES:
                                raise ValueError(f"Tape has more than {MAX_CLOSURES} no-trade gaps; export a shorter window")
                            closures.append((previous + timedelta(microseconds=1), stamp))
                    if rows == 1 and "." in row["price"]:
                        price_format = "decimal"
                    previous, previous_ns = stamp, ns
                    writer.writerow(row)
            except csv.Error as error:
                raise ValueError(f"Malformed tape CSV: {error}") from None
        return {"records": rows, "price_format": price_format, "closures": closures}


def export_trades(key, symbol, start, end, output, *, gap_seconds=300, client=None):
    """Download one dated contract window and publish a verified tape pair."""
    symbol = symbol.strip() if isinstance(symbol, str) else ""
    if not SYMBOL.fullmatch(symbol):
        raise ValueError("--symbol must be one dated outright contract, e.g. MNQZ6")
    start, end = parse_time(start), parse_time(end)
    if not timedelta(0) < end - start <= timedelta(days=7):
        raise ValueError("--start/--end must span more than zero and at most seven days")
    if type(gap_seconds) is not int or not 300 <= gap_seconds <= 86400:
        raise ValueError("--gap-seconds must be an integer between 300 and 86400")
    client = client or Databento(key)

    days = _days(start, end)
    mapped = client.resolve(symbol, _date(start), _resolve_end(end))
    conditions = client.conditions(_date(start), _date(end - timedelta(microseconds=1)))
    if not conditions:
        raise ValueError("Databento returned no dataset conditions; refusing to mark a tape complete")
    unusable = {day: conditions[day] for day in days if conditions.get(day) not in CREATE | {None}}
    if unusable:
        raise ValueError(f"Databento dataset conditions are not usable for: {unusable}; split or move the window")
    expected = client.record_count(symbol, start, end)
    if expected is None:
        raise ValueError("Databento did not report a record count; refusing to mark a tape complete")
    if expected > MAX_RECORDS:
        raise ValueError(f"Window has {expected:,} records, above the {MAX_RECORDS:,} research budget; "
                         "export a shorter complete window")
    if expected == 0:
        raise ValueError("Window contains no trades; check the contract month and dates")

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    stem = f"{symbol.lower()}-{start:%Y%m%d}-{end:%Y%m%d}"
    csv_path, manifest_path = output / f"{stem}-trades.csv", output / f"{stem}-coverage.json"
    csv_fd, csv_temp = tempfile.mkstemp(prefix=".futures-fetch-", suffix=".csv", dir=output)
    manifest_fd, manifest_temp = tempfile.mkstemp(prefix=".futures-fetch-", suffix=".json", dir=output)
    published_csv = False
    try:
        with os.fdopen(csv_fd, "w", newline="") as stream:
            result = client.download(symbol, start, end, stream, gap_seconds)
            stream.flush()
            os.fsync(stream.fileno())
        if result["records"] != expected:
            raise ValueError(f"Download is incomplete: {result['records']:,} rows but Databento reported "
                             f"{expected:,} records; nothing was published")
        manifest = {
            "schema": "tred.futures-tape.v1",
            "source": f"Databento {DATASET} trades via timeseries.get_range (raw_symbol -> instrument_id)",
            "format": "databento", "symbol": symbol, "instrument_id": int(mapped),
            "price_format": result["price_format"], "start": _iso(start), "end": _iso(end),
            "complete": True, "trades_file": csv_path.name,
            "closed_intervals": [[_iso(a), _iso(b)] for a, b in result["closures"]],
            "provider_conditions": {day: conditions.get(day, "not_reported") for day in days},
        }
        manifest_fd = _write_json(manifest_fd, manifest)
        for source, destination in ((csv_temp, csv_path), (manifest_temp, manifest_path)):
            os.link(source, destination)
            published_csv = published_csv or destination == csv_path
        next_step = f"python3 -m tred futures-compare --manifest {manifest_path} " \
                    "--commission-per-side <USD fee> --margin-per-contract <USD margin>"
        return {"manifest": str(manifest_path), "trades": str(csv_path), "symbol": symbol,
                "instrument_id": manifest["instrument_id"], "records": result["records"],
                "expected_records": expected, "price_format": result["price_format"],
                "closed_intervals": len(result["closures"]), "provider_conditions": manifest["provider_conditions"],
                "next": next_step}
    except Exception:
        if published_csv:
            csv_path.unlink(missing_ok=True)
        raise
    finally:
        if manifest_fd is not None:
            os.close(manifest_fd)
        Path(csv_temp).unlink(missing_ok=True)
        Path(manifest_temp).unlink(missing_ok=True)


def _date(value):
    return value.astimezone(UTC).date().isoformat()


def _resolve_end(end):
    return ((end - timedelta(microseconds=1)).astimezone(UTC).date() + timedelta(days=1)).isoformat()


def _write_json(fd, payload):
    try:
        with os.fdopen(fd, "w") as stream:
            fd = None
            json.dump(payload, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd is not None:
            os.close(fd)
    return None


def _days(start, end):
    last = (end - timedelta(microseconds=1)).astimezone(UTC).date()
    day, result = start.astimezone(UTC).date(), []
    while day <= last:
        result.append(day.isoformat())
        day += timedelta(days=1)
    return result


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
