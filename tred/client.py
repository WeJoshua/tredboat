"""Small stdlib Alpaca clients. Credentials never go to market-data requests."""

from __future__ import annotations

import getpass
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA_BASE = "https://data.alpaca.markets"
PAPER_BASE = "https://paper-api.alpaca.markets/v2"
FEEDS = {"us", "us-1", "eu-1"}


class ApiError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        self.status = status
        super().__init__(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Never forward authentication headers to a redirect destination.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_env(path: str = ".env") -> None:
    """Read only the supported settings; never execute shell syntax."""
    source = Path(path)
    if not source.is_file():
        return
    allowed = {"APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "APCA_API_BASE_URL", "DATABENTO_API_KEY"}
    for line in source.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in allowed and value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def prompt_credentials() -> None:
    """TTY input is hidden and kept in this process only."""
    for key, label in (("APCA_API_KEY_ID", "Paper API key: "),
                       ("APCA_API_SECRET_KEY", "Paper API secret: ")):
        if not os.environ.get(key):
            os.environ[key] = getpass.getpass(label)


class HttpClient:
    def __init__(self, base: str, headers: dict | None = None, timeout: float = 15):
        self.base = base
        self.headers = headers or {}
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method: str, path: str, payload: dict | None = None,
                query: dict | None = None):
        if not path.startswith("/") or path.startswith("//") or ".." in path or "://" in path:
            raise ValueError("Only relative API paths are accepted")
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": "application/json", "User-Agent": "tred-paper/0.1", **self.headers}
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(3 if method == "GET" else 1):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with self.opener.open(req, timeout=self.timeout) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                message = error.read(4096).decode(errors="replace")
                for value in self.headers.values():
                    if value:
                        message = message.replace(value, "[redacted]")
                if method == "GET" and attempt < 2 and error.code in (429, 500, 502, 503, 504):
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise ApiError(error.code, f"Alpaca HTTP {error.code}: {message[:500]}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if method == "GET" and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise ApiError(None, f"Alpaca connection failed ({type(error).__name__}); request outcome may be unknown") from None


class PaperClient(HttpClient):
    def __init__(self):
        configured = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
        if configured not in ("https://paper-api.alpaca.markets", PAPER_BASE):
            raise ValueError("Trading is restricted to https://paper-api.alpaca.markets; live/custom URLs are rejected")
        key = os.environ.get("APCA_API_KEY_ID", "")
        secret = os.environ.get("APCA_API_SECRET_KEY", "")
        if not key or not secret:
            raise ValueError("Missing paper credentials. Set .env or use --prompt-credentials.")
        if not key.startswith("PK"):
            raise ValueError("Only Alpaca paper API keys (PK prefix) are accepted")
        super().__init__(PAPER_BASE, {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})


class MarketData(HttpClient):
    def __init__(self, feed: str = "us-1"):
        if feed not in FEEDS:
            raise ValueError(f"Unsupported crypto feed: {feed}")
        self.feed = feed
        super().__init__(DATA_BASE)

    def trade_pages(self, symbol: str, start: datetime, end: datetime, max_pages: int = 500):
        from .strategy import Trade

        token = None
        seen_tokens = set()
        for _ in range(max_pages):
            result = self.request("GET", f"/v1beta3/crypto/{self.feed}/trades", query={
                "symbols": symbol, "start": start.isoformat(), "end": end.isoformat(),
                "limit": 10000, "sort": "asc", "page_token": token,
            })
            trades = []
            for raw in result.get("trades", {}).get(symbol, []) or []:
                if raw.get("tks") not in ("B", "S"):
                    raise ValueError("Trade lacks valid taker side; refusing synthetic order-flow classification")
                if float(raw["p"]) <= 0 or float(raw["s"]) <= 0:
                    raise ValueError("Non-positive trade price/size")
                trades.append(Trade(str(raw["i"]), raw["t"], float(raw["p"]), float(raw["s"]), raw["tks"]))
            yield trades
            token = result.get("next_page_token")
            if not token:
                return
            if token in seen_tokens:
                raise ValueError("Repeated market-data pagination token; data is incomplete")
            seen_tokens.add(token)
        raise ValueError("Page limit reached; shorten the requested range (data is incomplete)")

    def quote(self, symbol: str) -> dict:
        response = self.request("GET", f"/v1beta3/crypto/{self.feed}/latest/quotes", query={"symbols": symbol})
        raw = response.get("quotes", {}).get(symbol)
        if not raw:
            raise ValueError(f"No quote for {symbol} on {self.feed}")
        return {"symbol": symbol, "feed": self.feed, "bid": float(raw["bp"]),
                "ask": float(raw["ap"]), "bid_size": float(raw["bs"]),
                "ask_size": float(raw["as"]), "timestamp": raw["t"]}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
