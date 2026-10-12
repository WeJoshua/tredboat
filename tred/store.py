"""Local raw trades, quotes, collection coverage, and decision journal."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                feed TEXT, symbol TEXT, trade_id TEXT, timestamp TEXT,
                epoch REAL, price REAL, size REAL, side TEXT, raw TEXT,
                PRIMARY KEY(feed, symbol, trade_id));
            CREATE INDEX IF NOT EXISTS trades_by_time ON trades(feed, symbol, epoch);
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY, received_at TEXT, feed TEXT, symbol TEXT, raw TEXT);
            CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY, timestamp TEXT, kind TEXT, raw TEXT);
            CREATE TABLE IF NOT EXISTS coverage (
                feed TEXT, symbol TEXT, start REAL, end REAL);
        """)

    def add_trades(self, feed: str, symbol: str, trades) -> int:
        from .strategy import parse_time
        before = self.connection.total_changes
        self.connection.executemany("INSERT OR IGNORE INTO trades VALUES(?,?,?,?,?,?,?,?,?)", [
            (feed, symbol, str(t.id), t.timestamp, parse_time(t.timestamp).timestamp(),
             t.price, t.size, t.side, json.dumps(asdict(t))) for t in trades
        ])
        self.connection.commit()
        return self.connection.total_changes - before

    def mark_coverage(self, feed, symbol, start, end):
        self.connection.execute("INSERT INTO coverage VALUES(?,?,?,?)", (feed, symbol, start.timestamp(), end.timestamp()))
        self.connection.commit()

    def covers(self, feed, symbol, start, end) -> bool:
        cursor = start.timestamp()
        for lo, hi in self.connection.execute(
                "SELECT start,end FROM coverage WHERE feed=? AND symbol=? AND end>=? ORDER BY start",
                (feed, symbol, cursor)):
            if lo > cursor + 0.001:
                return False
            cursor = max(cursor, hi)
            if cursor >= end.timestamp():
                return True
        return False

    def trades(self, feed, symbol, start=None, end=None, *, max_trades=None):
        from .strategy import Trade
        query = "SELECT trade_id,timestamp,price,size,side FROM trades WHERE feed=? AND symbol=?"
        args = [feed, symbol]
        if start:
            query += " AND epoch>=?"
            args.append(start.timestamp())
        if end:
            query += " AND epoch<?"
            args.append(end.timestamp())
        query += " ORDER BY epoch,trade_id"
        if max_trades is not None:
            if not isinstance(max_trades, int) or max_trades < 1:
                raise ValueError("max_trades must be a positive integer")
            query += " LIMIT ?"
            args.append(max_trades + 1)
        result = [Trade(*row) for row in self.connection.execute(query, args)]
        if max_trades is not None and len(result) > max_trades:
            raise ValueError(f"Trade input exceeds the {max_trades:,}-trade resource limit; choose a shorter --start/--end window")
        return result

    def quote(self, quote):
        from .client import utc_now
        self.connection.execute("INSERT INTO quotes(received_at,feed,symbol,raw) VALUES(?,?,?,?)",
                                (utc_now().isoformat(), quote["feed"], quote["symbol"], json.dumps(quote)))
        self.connection.commit()

    def journal(self, kind, data):
        from .client import utc_now
        self.connection.execute("INSERT INTO journal(timestamp,kind,raw) VALUES(?,?,?)",
                                (utc_now().isoformat(), kind, json.dumps(data, allow_nan=False)))
        self.connection.commit()

    def close(self):
        self.connection.close()
