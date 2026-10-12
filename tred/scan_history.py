"""Chunked, low-priority long-window backtest with a losing-trade failure log.

Processes the tape in warmup+eval chunks so every bar's signal belongs to
exactly one evaluation window (per-chunk fresh equity, matching the earlier
six-month chunked runs). Winning and losing trades keep the setup features and
R multiples now recorded by the shared replay; every net loser is appended to
a JSON-lines file for failure analysis, capped and never truncated silently.
"""
from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .strategy import _iso, parse_time, replay

UTC = timezone.utc


def scan(store, strategy, start, end, *, feed, symbol, eval_seconds=6 * 3600,
         warmup_seconds=24 * 3600, max_trades=500000, initial_equity=10000.0,
         loss_path=None, wins_path=None, max_losses=2000, pause=0.02):
    """Replay [start, end) in eval windows, logging net losers and winners.

    Reads up to max_trades rows per chunk (the CLI's 100k surface limit would
    truncate a 24-30h chunk of real tape); scan_limits' 512 MiB address space
    remains the hard memory guard. Both logs carry full setup features and R
    multiples; truncation is counted, never silent.
    """
    if not end > start:
        raise ValueError("scan window must be positive and chronological")
    if type(eval_seconds) is not int or not 300 <= eval_seconds <= 48 * 3600 or eval_seconds % 300:
        raise ValueError("eval_seconds must be a multiple of 300 between 300 and 172800")
    if type(warmup_seconds) is not int or warmup_seconds <= 0 or warmup_seconds % 300:
        raise ValueError("warmup_seconds must be a positive multiple of 300")
    if type(max_trades) is not int or not 1 <= max_trades <= 1_000_000:
        raise ValueError("max_trades must be between 1 and 1,000,000")
    if type(max_losses) is not int or max_losses < 0:
        raise ValueError("max_losses must be a nonnegative integer")
    if not math.isfinite(initial_equity) or initial_equity <= 0:
        raise ValueError("initial_equity must be positive and finite")
    totals = {"start": _iso(start), "end": _iso(end), "direction": strategy.direction,
              "execution": strategy.execution, "max_notional": strategy.max_notional,
              "chunks": 0, "trade_count": 0, "signal_count": 0, "net_pnl": 0.0,
              "fees": 0.0, "win_count": 0, "loss_count": 0, "breakeven_count": 0,
              "wins_logged": 0, "wins_truncated": 0, "losses_logged": 0, "losses_truncated": 0,
              "skipped_entries": 0, "limit_unfilled": 0,
              "reason_counts": defaultdict(int), "loss_reasons": defaultdict(int),
              "win_reasons": defaultdict(int)}
    cursor = _align(start, 300)
    written_losses, written_wins = 0, 0
    loss_log = wins_log = None
    try:
        if loss_path is not None:
            loss_log = open(loss_path, "w", newline="")
        if wins_path is not None:
            wins_log = open(wins_path, "w", newline="")
        while cursor < end:
            chunk_start = max(start, cursor - timedelta(seconds=warmup_seconds))
            chunk_end = min(end, cursor + timedelta(seconds=eval_seconds))
            chunk = store.trades(feed, symbol, chunk_start, chunk_end, max_trades=max_trades)
            report = replay(chunk, strategy, initial_equity)
            totals["chunks"] += 1
            totals["skipped_entries"] += report["summary"].get("skipped_entries", 0)
            totals["limit_unfilled"] += report["summary"].get("limit_unfilled", 0)
            def log_line(trade, bucket):
                line = {key: trade.get(key) for key in (
                    "signal_id", "signal_time", "entry_time", "entry", "stop", "target",
                    "exit_time", "exit", "exit_reason", "net_pnl", "net_r",
                    "entry_fee", "exit_fee", "quantity", "features")}
                line["eval_start"] = _iso(cursor)
                line["direction"] = strategy.direction
                return json.dumps(line, allow_nan=True, separators=(",", ":")) + "\n"
            for trade in report["trades"]:
                if parse_time(trade["signal_time"]) < cursor:
                    continue  # warmup duplicate of an earlier evaluation window
                totals["signal_count"] += 1
                totals["trade_count"] += 1
                totals["net_pnl"] += trade["net_pnl"]
                totals["fees"] += trade["entry_fee"] + trade["exit_fee"]
                if trade["net_pnl"] > 1e-9:
                    totals["win_count"] += 1
                    totals["win_reasons"][trade["exit_reason"]] += 1
                    if written_wins < max_losses:
                        wins_log.write(log_line(trade, "win"))
                        written_wins += 1
                    else:
                        totals["wins_truncated"] += 1
                elif trade["net_pnl"] < -1e-9:
                    totals["loss_count"] += 1
                    totals["loss_reasons"][trade["exit_reason"]] += 1
                    if written_losses < max_losses:
                        loss_log.write(log_line(trade, "loss"))
                        written_losses += 1
                    else:
                        totals["losses_truncated"] += 1
                else:
                    totals["breakeven_count"] += 1
            for reason, count in report["summary"].get("reason_counts", {}).items():
                totals["reason_counts"][reason] += count
            cursor += timedelta(seconds=eval_seconds)
            if pause:
                time.sleep(pause)
        totals["losses_logged"], totals["wins_logged"] = written_losses, written_wins
    finally:
        if loss_log is not None:
            loss_log.close()
        if wins_log is not None:
            wins_log.close()
    return totals


def _align(value, seconds):
    epoch = int(value.astimezone(UTC).timestamp() / seconds) * seconds
    return datetime.fromtimestamp(epoch, UTC)