"""Alpaca equity tape export with an explicit tick-rule side classification.

Alpaca equity prints carry timestamp, price, size, conditions and tape but no
aggressor flag. The user selected the `tick_prior_price` downgrade for QQQ
research: every CSV row and manifest declares this inference instead of
pretending the tape is signed by the exchange.
"""
from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .client import DATA_BASE, HttpClient
from .equities import CENTS, SYMBOL_RE
from .futures import MAX_BYTES, MAX_CLOSURES, MAX_RECORDS, timestamp
from .strategy import parse_time

FEEDS = {"iex", "sip"}
UTC = timezone.utc
MAX_PAGES = 2000


class EquityMarketData(HttpClient):
    """Authenticated historical stock trades from the Alpaca Data API."""

    def __init__(self, feed="iex", key_id=None, secret_key=None, *, timeout=30):
        if feed not in FEEDS:
            raise ValueError(f"Unsupported equity feed: {feed}")
        key_id = (key_id or os.environ.get("APCA_API_KEY_ID") or "").strip()
        secret_key = (secret_key or os.environ.get("APCA_API_SECRET_KEY") or "").strip()
        if not key_id or not secret_key or key_id == "YOUR_API_KEY_ID" or secret_key == "YOUR_API_SECRET_KEY":
            raise ValueError("Alpaca data credentials are required (APCA_API_KEY_ID / APCA_API_SECRET_KEY in .env)")
        self.feed = feed
        super().__init__(DATA_BASE, {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}, timeout=timeout)

    def trade_pages(self, symbol, start, end):
        token, seen = None, set()
        for _ in range(MAX_PAGES):
            result = self.request("GET", f"/v2/stocks/{symbol}/trades", query={
                "start": start.isoformat(), "end": end.isoformat(), "limit": 10000,
                "feed": self.feed, "sort": "asc", "page_token": token})
            trades = result.get("trades") or []
            if result.get("symbol", "").upper() != symbol.upper():
                raise ValueError("Alpaca returned trades for an unexpected symbol")
            yield trades
            token = result.get("next_page_token")
            if not token:
                return
            if token in seen:
                raise ValueError("Repeated market-data pagination token; data is incomplete")
            seen.add(token)
        raise ValueError("Page limit reached; shorten the requested range (data is incomplete)")


def _valid(raw):
    try:
        price = Decimal(str(raw["p"]))
        size = Decimal(str(raw["s"]))
    except (InvalidOperation, TypeError, KeyError):
        raise ValueError("Invalid trade price/size") from None
    if not price.is_finite() or price <= 0:
        raise ValueError("Invalid price")
    if not size.is_finite() or size < 1 or size != size.to_integral_value():
        raise ValueError("Equity volume must be positive whole shares")
    return float(price), int(size)


def export_tape(key_id, secret_key, symbol, start, end, output, *, feed="iex", gap_seconds=300, client=None):
    """Download one window and publish a canonical equity tape pair."""
    symbol = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("--symbol must be an uppercase equity symbol, e.g. QQQ")
    if feed not in FEEDS:
        raise ValueError("--feed must be iex or sip")
    start, end = parse_time(start), parse_time(end)
    if not timedelta(0) < end - start <= timedelta(days=7):
        raise ValueError("--start/--end must span more than zero and at most seven days")
    if type(gap_seconds) is not int or not 300 <= gap_seconds <= 86400:
        raise ValueError("--gap-seconds must be an integer between 300 and 86400")
    client = client or EquityMarketData(feed, key_id=key_id, secret_key=secret_key)

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    stem = f"{symbol.lower()}-{start:%Y%m%d}-{end:%Y%m%d}"
    csv_path, manifest_path = output / f"{stem}-trades.csv", output / f"{stem}-coverage.json"
    csv_fd, csv_temp = tempfile.mkstemp(prefix=".equity-fetch-", suffix=".csv", dir=output)
    manifest_fd, manifest_temp = tempfile.mkstemp(prefix=".equity-fetch-", suffix=".json", dir=output)
    published_csv = False
    try:
        with os.fdopen(csv_fd, "w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["timestamp", "trade_id", "symbol", "price", "size", "side"])
            writer.writeheader()
            rows = 0
            bytes_written = stream.tell()
            last_price, side, pending, closures = None, None, [], []
            resolved = False

            def emit(raw, assigned):
                nonlocal rows, bytes_written
                rows += 1
                if rows > MAX_RECORDS:
                    raise ValueError("Tape exceeds one million records; export a shorter complete window")
                row = {"timestamp": raw["t"], "trade_id": str(raw.get("i") or ""), "symbol": symbol,
                       "price": f"{Decimal(str(raw['p'])):.2f}", "size": str(int(Decimal(str(raw['s'])))), "side": assigned}
                if not row["trade_id"]:
                    raise ValueError("Alpaca trade without an id cannot be deduplicated; refusing the tape")
                writer.writerow(row)
                bytes_written = stream.tell()
                if bytes_written > MAX_BYTES:
                    raise ValueError("Tape exceeds 256 MiB; export a shorter complete window")

            for page in client.trade_pages(symbol, start, end):
                for raw in page:
                    price, _ = _valid(raw)
                    if not resolved:
                        pending.append(raw)
                        if len(pending) > 1000:
                            raise ValueError("Tape prices never change; the tick rule cannot classify a direction")
                        if len(pending) >= 2 and float(pending[0]["p"]) != price:
                            side = "B" if price > float(pending[0]["p"]) else "S"
                            for buffered in pending:
                                emit(buffered, side)
                            pending = []
                            resolved = True
                            last_price = price
                        continue
                    if price > last_price:
                        side = "B"
                    elif price < last_price:
                        side = "S"
                    emit(raw, side)
                    last_price = price
            if rows == 0 and not pending:
                raise ValueError("Window contains no trades; check the symbol and dates")
            if not resolved:
                raise ValueError("Tape prices never change; the tick rule cannot classify a direction")
            stream.flush()
            os.fsync(stream.fileno())
        closures = _gaps(csv_temp, gap_seconds)
        manifest = {
            "schema": "tred.equity-tape.v1",
            "source": f"Alpaca Data API v2 /v2/stocks/{symbol}/trades (feed {feed}); pagination exhausted",
            "format": "canonical", "symbol": symbol, "price_format": "decimal",
            "side_inference": {"rule": "tick_prior_price",
                               "note": "User-selected downgrade: Alpaca equity prints carry no aggressor flag; "
                                       "side classified from the prior printed price"},
            "start": _iso(start), "end": _iso(end), "complete": True, "feed": feed,
            "trades_file": csv_path.name, "closed_intervals": [[_iso(a), _iso(b)] for a, b in closures],
        }
        with os.fdopen(manifest_fd, "w") as stream:
            manifest_fd = None
            json.dump(manifest, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for source, destination in ((csv_temp, csv_path), (manifest_temp, manifest_path)):
            os.link(source, destination)
            published_csv = published_csv or destination == csv_path
        next_step = f"python3 -m tred equity-compare --manifest {manifest_path} " \
                    "--commission-fixed 0 --commission-bps 0 --slippage-bps 1"
        return {"manifest": str(manifest_path), "trades": str(csv_path), "symbol": symbol,
                "feed": feed, "records": rows, "closed_intervals": len(closures),
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


def _gaps(path, gap_seconds):
    closures = []
    previous = previous_ns = None
    with Path(path).open(newline="") as stream:
        for row in csv.DictReader(stream):
            stamp, ns = timestamp(row["timestamp"])
            if previous_ns is not None:
                if ns < previous_ns:
                    raise ValueError("Equity tape is not sorted by event timestamp")
                if (stamp - previous).total_seconds() > gap_seconds:
                    if len(closures) >= MAX_CLOSURES:
                        raise ValueError(f"Tape has more than {MAX_CLOSURES} no-trade gaps; shorten the window")
                    closures.append((previous + timedelta(microseconds=1), stamp))
            previous, previous_ns = stamp, ns
    return closures


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")