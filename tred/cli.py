"""Command-line research, recording, and explicitly selected paper execution."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import math
import os
import sys
import time
import tempfile
import tomllib
from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .client import ApiError, MarketData, PaperClient, load_env, prompt_credentials, utc_now
from .store import Store
from .strategy import StrategyConfig, analyze, parse_time, replay


def emit(value):
    print(json.dumps(value, indent=2, allow_nan=False), flush=True)


def settings(path, feed=None, *, research_ok=False):
    with open(path, "rb") as source:
        config = tomllib.load(source)
    market = config["market"]
    if market.get("symbol") != "BTC/USD":
        raise ValueError("This first prototype supports BTC/USD only")
    if feed:
        market["signal_feed"] = feed
    if market["execution_feed"] != "us":
        raise ValueError("Paper execution quote feed must remain 'us'")
    for name in ("watermark_seconds", "max_quote_age_seconds", "max_spread_bps", "max_basis_bps", "poll_seconds"):
        if not math.isfinite(float(market[name])) or float(market[name]) <= 0:
            raise ValueError(f"market.{name} must be positive and finite")
    known = {f.name for f in dataclasses.fields(StrategyConfig)}
    unknown = set(config["strategy"]) - known
    if unknown:
        raise ValueError(f"Unknown strategy settings: {sorted(unknown)}")
    strategy = StrategyConfig(symbol=market["symbol"], **config["strategy"])
    if (not research_ok) and (strategy.max_notional > 100 or strategy.risk_fraction > 0.0025
                              or strategy.daily_loss_fraction > 0.01):
        raise ValueError("Prototype limits apply to paper execution: notional <= $100, "
                         "risk_fraction <= 0.0025, daily_loss_fraction <= 0.01")
    return config, strategy


@contextmanager
def execution_lock(state_path):
    path = Path(str(state_path) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another paper execution process already owns this state") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def fetch(store, feed, symbol, start, end):
    count = 0
    pages = 0
    for page in MarketData(feed).trade_pages(symbol, start, end):
        count += store.add_trades(feed, symbol, page)
        pages += 1
    # Never mark a partially downloaded range complete.
    store.mark_coverage(feed, symbol, start, end)
    result = {"feed": feed, "symbol": symbol, "new_trades": count, "pages": pages,
              "start": start.isoformat(), "end": end.isoformat()}
    store.journal("collection", result)
    return result


def aligned_start(as_of, strategy):
    end_epoch = int(as_of.timestamp()) // strategy.bar_seconds * strategy.bar_seconds
    return datetime.fromtimestamp(end_epoch - (strategy.profile_bars + 2) * strategy.bar_seconds, timezone.utc)


def inspect_signal(store, market, strategy, as_of):
    start = aligned_start(as_of, strategy)
    trades = store.trades(market["signal_feed"], market["symbol"], start, as_of)
    result = analyze(trades, strategy, as_of)
    cutoff = datetime.fromtimestamp(int(as_of.timestamp()) // strategy.bar_seconds * strategy.bar_seconds, timezone.utc)
    if not store.covers(market["signal_feed"], market["symbol"], start, cutoff):
        result.update(status="no_signal", reason="incomplete_download_coverage", signal=None)
    return result


def execution_gate(signal, signal_quote, execution_quote, market, strategy, now):
    for quote in (signal_quote, execution_quote):
        age = (now - parse_time(quote["timestamp"])).total_seconds()
        if age < -5 or age > market["max_quote_age_seconds"]:
            return None, "stale_or_future_quote"
        if not all(math.isfinite(quote[k]) and quote[k] > 0 for k in ("bid", "ask")):
            return None, "invalid_quote"
        if quote["ask"] < quote["bid"]:
            return None, "crossed_quote"
    bid, ask = execution_quote["bid"], execution_quote["ask"]
    mid = (bid + ask) / 2
    spread_bps = (ask - bid) / mid * 10000
    if spread_bps > market["max_spread_bps"]:
        return None, "spread_limit"
    signal_mid = (signal_quote["bid"] + signal_quote["ask"]) / 2
    if abs(signal_mid / mid - 1) * 10000 > market["max_basis_bps"]:
        return None, "venue_price_difference"
    if not signal:
        return None, "no_strategy_signal"
    if signal.get("direction", "long") != "long":
        return None, "short_execution_unavailable"
    if not all(math.isfinite(float(signal[k])) and float(signal[k]) > 0 for k in ("entry", "stop", "target")):
        return None, "invalid_signal_prices"
    if not signal["stop"] < bid <= ask < signal["target"]:
        return None, "invalid_entry_stop_target"
    if abs(ask / signal["entry"] - 1) * 10000 > market["max_basis_bps"]:
        return None, "entry_price_drift"
    cost = 2 * strategy.taker_fee + 2 * strategy.slippage_bps / 10000 + spread_bps / 10000
    risk = (ask - signal["stop"]) / ask + cost
    reward = (signal["target"] - ask) / ask - cost
    if reward <= 0 or reward / risk < strategy.min_net_reward_risk:
        return None, "cost_adjusted_reward_risk"
    return dict(signal, entry=ask, expected_cost_fraction=cost, net_reward_risk=reward / risk), "allowed"


def risk_settings(market, strategy):
    return {"risk_fraction": strategy.risk_fraction, "daily_loss_fraction": strategy.daily_loss_fraction,
            "max_notional": strategy.max_notional, "max_quote_age_seconds": market["max_quote_age_seconds"],
            "max_spread_bps": market["max_spread_bps"], "max_basis_bps": market["max_basis_bps"],
            "fee_rate": strategy.taker_fee, "max_holding_seconds": strategy.bar_seconds * strategy.max_holding_bars,
            "max_consecutive_losses": strategy.max_consecutive_losses}


def execution_direction_error(strategy, command=None, mode=None):
    if strategy.flip:
        return ("Flipped (contrarian) execution is research-only; paper execution does not invert signals.")
    if strategy.direction != "long":
        return ("Paper execution supports direction 'long' only; Alpaca crypto is spot "
                "(no short). Direction '%s' is research-only (backtest/optimize/analyze)." % strategy.direction)
    return None


def pnl_report(state_path: str) -> dict:
    """Aggregate realized paper P&L from the durable executor state."""
    path = Path(state_path)
    if not path.is_file():
        return {"closed_trades": [], "realized_pnl": 0.0, "trade_count": 0,
                "win_rate": None, "profit_factor": None}
    state = json.loads(path.read_text())
    closed = state.get("closed_trades", [])
    wins = [float(t.get("net_pnl", 0)) for t in closed if float(t.get("net_pnl", 0)) > 0]
    losses = [-float(t.get("net_pnl", 0)) for t in closed if float(t.get("net_pnl", 0)) < 0]
    return {"closed_trades": closed, "realized_pnl": sum(wins) - sum(losses),
            "trade_count": len(closed),
            "win_rate": len(wins) / len(closed) if closed else None,
            "profit_factor": sum(wins) / sum(losses) if losses else None}


OPTIMIZE_GRID = [
    {"row_size": row, "imbalance_ratio": ratio, "zone_rows": zone,
     "minimum_cluster_bars": cluster, "min_net_reward_risk": reward}
    for row in (12.5, 25.0, 50.0)
    for ratio in (4.0, 6.0)
    for zone in (0, 2)
    for cluster in (2, 3)
    for reward in (1.0, 1.2)
]


def optimize_replay(trades, base, equity: float) -> list[dict]:
    rows = []
    for combo in OPTIMIZE_GRID:
        strategy = dataclasses.replace(base, **combo)
        summary = replay(trades, strategy, initial_equity=equity)["summary"]
        rows.append({"config": combo, "net_pnl": summary["net_pnl"],
                     "return_fraction": summary["return_fraction"],
                     "signal_count": summary["signal_count"], "trade_count": summary["trade_count"],
                     "win_rate": summary["win_rate"], "profit_factor": summary["profit_factor"],
                     "max_drawdown_fraction": summary["max_drawdown_fraction"],
                     "skipped_entries": summary["skipped_entries"]})
    return rows


def optimize_select(rows: list[dict], min_signals=5, min_trades=2, max_drawdown=0.05):
    eligible = [row for row in rows
                if row["signal_count"] >= min_signals and row["trade_count"] >= min_trades
                and row["max_drawdown_fraction"] is not None
                and row["max_drawdown_fraction"] <= max_drawdown]
    eligible.sort(key=lambda row: (-row["net_pnl"], -(row["profit_factor"] or 0.0),
                                   row["max_drawdown_fraction"] or 0.0))
    return (eligible[0] if eligible else None), eligible


def bar_start(value, seconds: int) -> datetime:
    return datetime.fromtimestamp(int(parse_time(value).timestamp()) // seconds * seconds, timezone.utc)


def replay_input(store, market, strategy, start=None, end=None, *, max_trades=100000):
    seconds = strategy.bar_seconds
    if (start is None) != (end is None):
        raise ValueError("Provide both --start and --end for a bounded replay")
    if start is None and end is None:
        trades = store.trades(market["signal_feed"], market["symbol"], max_trades=max_trades)
        if not trades:
            raise ValueError("No recorded trades; run fetch first")
        first = bar_start(trades[0].timestamp, seconds)
        last_end = bar_start(trades[-1].timestamp, seconds)
        if not store.covers(market["signal_feed"], market["symbol"], first, first + timedelta(seconds=seconds)):
            # The initial fetch commonly begins partway through a bar. Exclude it.
            first += timedelta(seconds=seconds)
        if first >= last_end or not store.covers(market["signal_feed"], market["symbol"], first, last_end):
            raise ValueError("Backtest download coverage is incomplete; fetch the missing range before replay")
        return [trade for trade in trades if parse_time(trade.timestamp) >= first]
    first = bar_start(start, seconds)
    last_end = bar_start(end, seconds)
    if last_end <= first:
        raise ValueError("Backtest window end must be after start")
    trades = store.trades(market["signal_feed"], market["symbol"], first, last_end, max_trades=max_trades)
    if not store.covers(market["signal_feed"], market["symbol"], first, last_end):
        raise ValueError("Backtest download coverage is incomplete; fetch the missing range before replay")
    return trades


def run_loop(args, config, strategy, store, executor=None):
    market = config["market"]
    started = time.monotonic()
    iterations = 0
    last_decision = None
    error = None
    try:
        if executor:
            recovered = executor.recover()
            if recovered.get("ok") is False:
                raise ValueError(f"Paper recovery halted: {recovered.get('reason', 'unknown')}")
        current = utc_now()
        warmup_start = aligned_start(current - timedelta(seconds=market["watermark_seconds"]), strategy)
        if not store.covers(market["signal_feed"], market["symbol"], warmup_start, current - timedelta(seconds=60)):
            emit({"bootstrap": fetch(store, market["signal_feed"], market["symbol"], warmup_start, current)})
        last_end = current - timedelta(seconds=60)
        started = time.monotonic()
        while time.monotonic() - started < args.seconds:
            current = utc_now()
            fetch(store, market["signal_feed"], market["symbol"], last_end - timedelta(seconds=60), current)
            last_end = current
            signal_quote = MarketData(market["signal_feed"]).quote(market["symbol"])
            execution_quote = MarketData(market["execution_feed"]).quote(market["symbol"])
            store.quote(signal_quote)
            store.quote(execution_quote)
            as_of = current - timedelta(seconds=market["watermark_seconds"])
            result = inspect_signal(store, market, strategy, as_of)
            signal = result.get("signal")
            gate_time = utc_now()
            if signal and (gate_time - parse_time(result["bar_end"])).total_seconds() > 90:
                signal = None
                result["execution_gate"] = "signal_expired"
            allowed, gate = execution_gate(signal, signal_quote, execution_quote, market, strategy, gate_time)
            result.setdefault("execution_gate", gate)
            result["signal_feed"] = market["signal_feed"]
            result["execution_feed"] = market["execution_feed"]
            result["mode"] = "paper" if executor else "observe"
            if executor:
                execution = executor.step(allowed, execution_quote, risk_settings(market, strategy))
                store.journal("execution", execution)
                result["execution"] = execution
                if execution.get("pnl"):
                    store.journal("pnl", execution["pnl"])
                    emit({"realized_pnl": execution["pnl"]})
                if execution.get("ok") is False:
                    emit(result)
                    raise ValueError(f"Paper execution halted: {execution.get('reason', execution.get('error', 'unknown'))}")
            decision_key = (result.get("bar_end"), result.get("reason"), result.get("execution_gate"))
            store.journal("decision", result)
            if decision_key != last_decision:
                emit(result)
                last_decision = decision_key
            iterations += 1
            remaining = args.seconds - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(min(market["poll_seconds"], remaining))
    except KeyboardInterrupt:
        error = "interrupted"
    except (ApiError, ValueError, OSError) as caught:
        error = str(caught)
    finally:
        if executor:
            cleanup = executor.shutdown()
            store.journal("shutdown", cleanup)
            emit({"shutdown": cleanup})
            if cleanup.get("ok") is False:
                error = error or "Paper cleanup unresolved: inspect status before restarting"
    result = {"mode": "paper" if executor else "observe", "iterations": iterations,
              "elapsed_seconds": round(time.monotonic() - started, 1), "error": error}
    emit(result)
    return 2 if error else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="BTC/USD order-flow research and paper-only Alpaca execution")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--feed", choices=("us", "us-1", "eu-1"))
    parser.add_argument("--prompt-credentials", action="store_true", help="Read paper credentials without echo; never save them")
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("fetch", help="Record paginated historical signed trades")
    download.add_argument("--hours", type=float, default=30)
    download.add_argument("--end", help="UTC ISO timestamp; defaults to now")
    inspect = commands.add_parser("analyze", help="Explain the most recent closed-bar signal")
    inspect.add_argument("--as-of")
    backtest = commands.add_parser("backtest", help="Replay locally recorded trades with modeled execution costs")
    backtest.add_argument("--equity", type=float, default=10000)
    backtest.add_argument("--start", help="UTC ISO start bound for the replay window")
    backtest.add_argument("--end", help="UTC ISO end bound for the replay window")
    backtest.add_argument("--direction", choices=("long", "short"), default=None,
                          help="Direction to replay (default: config.toml)")
    backtest.add_argument("--flip", action="store_true",
                          help="Trade each signal on the opposite side (contrarian, research-only)")
    scan = commands.add_parser("backtest-scan", help="Chunked low-priority long-window replay; logs every losing trade")
    scan.add_argument("--start", help="UTC ISO start; defaults to the earliest stored trades")
    scan.add_argument("--end", help="UTC ISO end; defaults to the last closed bar in storage")
    scan.add_argument("--direction", choices=("long", "short", "all"), default="all")
    scan.add_argument("--execution", choices=("taker", "limit"), default=None,
                      help="Fill model: taker (market, fees both sides) or limit (maker entry at signal level, market stops, limit targets); declared experiment, default: config.toml")
    scan.add_argument("--max-notional", type=float, default=None,
                      help="Research-only notional cap override (default 100 from config.toml; the $100 prototype cap applies to paper execution only)")
    scan.add_argument("--eval-hours", type=float, default=6, help="Evaluation slice per chunk (multiple of 5 minutes)")
    scan.add_argument("--warmup-hours", type=float, default=24, help="Profile warmup carried before each evaluation slice")
    scan.add_argument("--losses", help="JSON-lines path for losing trades; defaults to data/losses-*.jsonl")
    scan.add_argument("--wins", help="JSON-lines path for winning trades (feature comparison); defaults to data/wins-*.jsonl")
    scan.add_argument("--max-losses", type=int, default=2000)
    scan.add_argument("--max-trades", type=int, default=500000,
                      help="Reads per chunk, 100,000..1,000,000; the research 100k surface cap would truncate real chunks")
    scan.add_argument("--equity", type=float, default=10000)
    scan.add_argument("--cpu-budget", type=int, default=4500,
                      help="Cumulative CPU-seconds ceiling for the whole scan (nice 19, one core; default 4500)")
    scan.add_argument("--report", help="New JSON report path; existing files are never overwritten")
    optimize = commands.add_parser("optimize", help="Grid-search strategy settings on recorded trades to improve P&L")
    optimize.add_argument("--equity", type=float, default=10000)
    optimize.add_argument("--min-signals", type=int, default=2)
    optimize.add_argument("--min-trades", type=int, default=2)
    optimize.add_argument("--max-drawdown", type=float, default=0.03)
    optimize.add_argument("--start", help="UTC ISO start bound for the search window")
    optimize.add_argument("--end", help="UTC ISO end bound for the search window")
    optimize.add_argument("--direction", choices=("long", "short"), default=None,
                          help="Direction to optimize (default: config.toml)")
    optimize.add_argument("--flip", action="store_true",
                          help="Optimize contrarian trade sides (research-only)")
    futures = commands.add_parser("futures-compare", help="Compare long/short/flipped rules on a complete signed MNQ or NQ CSV tape")
    futures.add_argument("--manifest", required=True, help="tred.futures-tape.v1 JSON coverage manifest")
    futures.add_argument("--commission-per-side", required=True, type=float, help="All-in USD commission/fees per contract per side")
    futures.add_argument("--margin-per-contract", required=True, type=float, help="Explicit initial margin assumption in USD")
    futures.add_argument("--equity", type=float, default=10000)
    futures.add_argument("--slippage-ticks", type=int, default=1)
    futures.add_argument("--max-contracts", type=int, default=1)
    futures.add_argument("--minimum-bar-contracts", type=int, default=None,
                         help="Setup/confirmation volume floor (default MNQ=20000, NQ=0; no assumed MNQ equivalence)")
    futures.add_argument("--profile-bars", type=int, default=288)
    futures.add_argument("--row-ticks", type=int, default=1)
    futures.add_argument("--output", help="New JSON report path; existing reports will not be overwritten")
    export = commands.add_parser("futures-fetch", help="Export a verified Databento GLBX trades tape and coverage manifest")
    export.add_argument("--symbol", required=True, help="One dated outright contract, e.g. MNQZ6")
    export.add_argument("--start", required=True, help="Inclusive UTC start timestamp")
    export.add_argument("--end", required=True, help="Exclusive UTC end timestamp, at most seven days later")
    export.add_argument("--output", default="data/futures", help="Directory for the tape pair; existing files are never overwritten")
    export.add_argument("--gap-seconds", type=int, default=300,
                        help="Declare a no-trade gap as an exchange closure above this many seconds")
    equity_export = commands.add_parser("equity-fetch", help="Export an Alpaca equity tape (e.g. QQQ) with explicit tick-rule side classification")
    equity_export.add_argument("--symbol", required=True, help="Uppercase equity symbol, e.g. QQQ")
    equity_export.add_argument("--start", required=True, help="Inclusive UTC start timestamp")
    equity_export.add_argument("--end", required=True, help="Exclusive UTC end timestamp, at most seven days later")
    equity_export.add_argument("--feed", choices=("iex", "sip"), default="iex",
                               help="iex covers Alpaca paper entitlements; sip needs a paid data license")
    equity_export.add_argument("--output", default="data/equity", help="Directory for the tape pair; existing files are never overwritten")
    equity_export.add_argument("--gap-seconds", type=int, default=300,
                               help="Declare a no-trade gap as an exchange closure above this many seconds")
    equity_compare = commands.add_parser("equity-compare", help="Compare long/short/flipped rules on a declared equity tape")
    equity_compare.add_argument("--manifest", required=True, help="tred.equity-tape.v1 JSON coverage manifest")
    equity_compare.add_argument("--commission-fixed", type=float, default=0.0, help="USD fixed commission per order")
    equity_compare.add_argument("--commission-bps", type=float, default=0.0, help="Commission in basis points of notional")
    equity_compare.add_argument("--slippage-bps", type=float, default=1.0)
    equity_compare.add_argument("--max-position-fraction", type=float, default=0.25)
    equity_compare.add_argument("--equity", type=float, default=10000)
    equity_compare.add_argument("--profile-bars", type=int, default=288)
    equity_compare.add_argument("--row-ticks", type=int, default=1)
    equity_compare.add_argument("--output", help="New JSON report path; existing reports will not be overwritten")
    commands.add_parser("pnl", help="Report realized paper P&L from the durable executor state")
    halt = commands.add_parser("halt", help="Record an execution mistake; runner flattens its trade and halts for the UTC day")
    halt.add_argument("--reason", required=True)
    loop = commands.add_parser("run", help="Bounded recording and signal loop; paper mode may place orders")
    loop.add_argument("--mode", choices=("observe", "paper"), default="observe")
    loop.add_argument("--seconds", type=int, default=60)
    commands.add_parser("status", help="Inspect paper account and this bot's execution state")
    smoke = commands.add_parser("smoke", help="Buy then sell a small PAPER position to verify execution")
    smoke.add_argument("--notional", type=float, default=10)
    session = commands.add_parser("paper-test", help="Check account, run a small paper round trip, then bounded paper signals")
    session.add_argument("--smoke-notional", type=float, default=10)
    session.add_argument("--seconds", type=int, default=60)
    commands.add_parser("flatten", help="Close only this bot's owned paper position and orders")
    args = parser.parse_args(argv)
    store = None
    limits = ExitStack()
    try:
        if args.command in {"backtest", "optimize", "futures-compare", "futures-fetch",
                            "equity-compare", "equity-fetch"}:
            from .research import research_limits
            wall = 300 if args.command in {"futures-fetch", "equity-fetch"} else 60
            limits.enter_context(research_limits(wall_seconds=wall))
        if args.command == "backtest-scan":
            from .research import scan_limits
            if not 300 <= args.cpu_budget <= 86400:
                raise ValueError("--cpu-budget must be between 300 and 86400 CPU-seconds")
            limits.enter_context(scan_limits(cpu_budget=args.cpu_budget))
        if args.command == "futures-fetch":
            from .databento import export_trades
            load_env()
            key = os.environ.get("DATABENTO_API_KEY")
            if not key:
                raise ValueError("Set DATABENTO_API_KEY in the environment or .env; no data was requested")
            emit(export_trades(key, args.symbol, args.start, args.end, args.output,
                               gap_seconds=args.gap_seconds))
            return 0
        if args.command == "equity-fetch":
            from .equity_data import export_tape
            load_env()
            key = os.environ.get("APCA_API_KEY_ID")
            secret = os.environ.get("APCA_API_SECRET_KEY")
            if not key or not secret:
                raise ValueError("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in .env; no data was requested")
            emit(export_tape(key, secret, args.symbol, args.start, args.end, args.output,
                             feed=args.feed, gap_seconds=args.gap_seconds))
            return 0
        if args.command == "equity-compare":
            from .equities import EquityModel, compare as equity_compare, read_manifest
            manifest, _, _, _, _ = read_manifest(args.manifest)
            model = EquityModel(manifest["symbol"], args.commission_fixed, args.commission_bps,
                                args.slippage_bps, args.max_position_fraction)
            report = equity_compare(args.manifest, model, args.equity,
                                    profile_bars=args.profile_bars, row_ticks=args.row_ticks)
            destination = Path(args.output) if args.output else Path("data") / ("equity-" + manifest["symbol"] + "-" + utc_now().strftime("%Y%m%dT%H%M%SZ") + ".json")
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".equity-", dir=destination.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(report, stream, indent=2, allow_nan=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, destination)  # Atomic publication; refuses overwrite.
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            emit({"report": str(destination), "data": report["data"],
                  "results": [{k: r[k] for k in ("direction", "flip", "summary")} for r in report["results"]],
                  "performance_conclusion": report["performance_conclusion"]})
            return 0
        if args.command == "futures-compare":
            from .futures import FuturesModel, compare, read_manifest
            manifest, _, _, _, _ = read_manifest(args.manifest)
            volume = args.minimum_bar_contracts
            if volume is None:
                volume = 20000 if manifest["root"] == "MNQ" else 0
            model = FuturesModel(manifest["root"], args.commission_per_side, args.margin_per_contract,
                                 args.slippage_ticks, args.max_contracts, volume)
            report = compare(args.manifest, model, args.equity, profile_bars=args.profile_bars, row_ticks=args.row_ticks)
            destination = Path(args.output) if args.output else Path("data") / ("futures-" + manifest["symbol"] + "-" + utc_now().strftime("%Y%m%dT%H%M%SZ") + ".json")
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".futures-", dir=destination.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(report, stream, indent=2, allow_nan=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, destination)  # Atomic publication; refuses overwrite.
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            emit({"report": str(destination), "data": report["data"],
                  "results": [{k: r[k] for k in ("direction", "flip", "summary")} for r in report["results"]],
                  "performance_conclusion": report["performance_conclusion"]})
            return 0
        config, strategy = settings(args.config, args.feed,
                                research_ok=args.command in {"backtest", "optimize", "backtest-scan"})
        if args.command == "halt":
            from .discipline import request_halt
            request = request_halt(config["storage"]["execution_state"], args.reason)
            emit({"halt_requested": request, "action": "Active runner will process this on its next step; this command submits no orders"})
            return 0
        if (args.command in {"smoke", "paper-test"}
                or (args.command == "run" and args.mode == "paper")):
            error = execution_direction_error(strategy, args.command, getattr(args, "mode", None))
            if error:
                raise ValueError(error)
        load_env()
        if args.prompt_credentials:
            prompt_credentials()
        market = config["market"]
        if getattr(args, "seconds", 1) <= 0:
            raise ValueError("--seconds must be positive; runs are intentionally bounded")
        store = Store(config["storage"]["database"])
        if args.command == "fetch":
            if not math.isfinite(args.hours) or not 0 < args.hours <= 24 * 31:
                raise ValueError("--hours must be positive and at most 744")
            end = parse_time(args.end) if args.end else utc_now()
            emit(fetch(store, market["signal_feed"], market["symbol"], end - timedelta(hours=args.hours), end))
        elif args.command == "analyze":
            as_of = parse_time(args.as_of) if args.as_of else utc_now() - timedelta(seconds=market["watermark_seconds"])
            emit(inspect_signal(store, market, strategy, as_of))
        elif args.command == "backtest":
            if not math.isfinite(args.equity) or args.equity <= 0:
                raise ValueError("--equity must be positive and finite")
            trades = replay_input(store, market, strategy, args.start, args.end)
            replay_strategy = strategy if args.direction is None else dataclasses.replace(strategy, direction=args.direction)
            if args.flip:
                replay_strategy = dataclasses.replace(replay_strategy, flip=True)
            report = replay(trades, replay_strategy, initial_equity=args.equity)
            report["signal_feed"] = market["signal_feed"]
            report["execution_assumption"] = "Local replay on signal venue, modeled taker fees/slippage; NOT actual Alpaca fills or a validated cross-venue backtest"
            report["configuration"] = dataclasses.asdict(replay_strategy)
            destination = Path(config["storage"]["database"]).parent / ("replay-" + utc_now().strftime("%Y%m%dT%H%M%SZ") + ".json")
            destination.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
            store.journal("backtest", {"report": str(destination), "summary": report.get("summary", {})})
            emit({"report": str(destination), **{k: v for k, v in report.items() if k not in ("trades", "configuration")}})
        elif args.command == "backtest-scan":
            from .scan_history import scan
            if not math.isfinite(args.equity) or args.equity <= 0:
                raise ValueError("--equity must be positive and finite")
            eval_seconds = round(args.eval_hours * 3600)
            warmup_seconds = round(args.warmup_hours * 3600)
            if eval_seconds % 300 or warmup_seconds % 300 or not 300 <= eval_seconds <= 48 * 3600:
                raise ValueError("--eval-hours/--warmup-hours must land on whole 5-minute intervals")
            if not 100000 <= args.max_trades <= 1_000_000:
                raise ValueError("--max-trades must be between 100,000 and 1,000,000")
            lower = store.connection.execute(
                "SELECT MIN(epoch), MAX(epoch) FROM trades WHERE feed=? AND symbol=?",
                (market["signal_feed"], market["symbol"])).fetchone()
            if not lower or lower[0] is None:
                raise ValueError("No stored trades to scan; run fetch first")
            start = parse_time(args.start) if args.start else datetime.fromtimestamp(math.floor(lower[0] / 300) * 300, timezone.utc)
            end = parse_time(args.end) if args.end else datetime.fromtimestamp(max(lower[0], lower[1] - 600) / 300 * 300, timezone.utc)
            directions = ("long", "short") if args.direction == "all" else (args.direction,)
            outcomes = {}
            for direction in directions:
                scan_strategy = dataclasses.replace(strategy, direction=direction) if direction != strategy.direction else strategy
                if args.execution is not None:
                    scan_strategy = dataclasses.replace(scan_strategy, execution=args.execution)
                if args.max_notional is not None:
                    if not 100 <= args.max_notional <= 10000:
                        raise ValueError("--max-notional must be between 100 and 10,000 (research override)")
                    scan_strategy = dataclasses.replace(scan_strategy, max_notional=args.max_notional)
                if args.losses:
                    loss_path = Path(args.losses)
                    loss_path = loss_path.with_name(f"{loss_path.stem}-{direction}{loss_path.suffix}")
                else:
                    loss_path = Path(f"data/losses-{direction}-{start:%Y%m%d}-{end:%Y%m%d}.jsonl")
                if args.wins:
                    wins_path = Path(args.wins)
                    wins_path = wins_path.with_name(f"{wins_path.stem}-{direction}{wins_path.suffix}")
                else:
                    wins_path = Path(f"data/wins-{direction}-{start:%Y%m%d}-{end:%Y%m%d}.jsonl")
                outcome = scan(store, scan_strategy, start, end, feed=market["signal_feed"], symbol=market["symbol"],
                               eval_seconds=eval_seconds, warmup_seconds=warmup_seconds,
                               initial_equity=args.equity, loss_path=loss_path, wins_path=wins_path,
                               max_losses=args.max_losses, max_trades=args.max_trades)
                outcome["loss_path"] = str(loss_path)
                outcome["wins_path"] = str(wins_path)
                outcomes[direction] = outcome
            destination = Path(args.report) if args.report else Path("data") / ("scan-" + utc_now().strftime("%Y%m%dT%H%M%SZ") + ".json")
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".scan-", dir=destination.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump({"schema": "tred.scan.v1", "window": {"start": start.isoformat(), "end": end.isoformat()},
                               "strategy": dataclasses.asdict(strategy), "directions": outcomes}, stream,
                              indent=2, allow_nan=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            store.journal("backtest-scan", {"report": str(destination), "directions": {
                name: {k: v for k, v in outcome.items() if k not in ("reason_counts", "loss_reasons")}
                for name, outcome in outcomes.items()}})
            emit({"report": str(destination), "window": {"start": start.isoformat(), "end": end.isoformat()},
                  "directions": {name: {k: v for k, v in outcome.items()
                                        if k not in ("reason_counts", "loss_reasons")}
                                 for name, outcome in outcomes.items()}})
        elif args.command == "run" and args.mode == "observe":
            return run_loop(args, config, strategy, store)
        elif args.command == "pnl":
            emit(pnl_report(config["storage"]["execution_state"]))
        elif args.command == "optimize":
            if not math.isfinite(args.equity) or args.equity <= 0:
                raise ValueError("--equity must be positive and finite")
            trades = replay_input(store, market, strategy, args.start, args.end)
            search = strategy if args.direction is None else dataclasses.replace(strategy, direction=args.direction)
            if args.flip:
                search = dataclasses.replace(search, flip=True)
            rows = optimize_replay(trades, search, args.equity)
            selected, eligible = optimize_select(rows, args.min_signals, args.min_trades, args.max_drawdown)
            report = {"grid_size": len(rows), "selected": selected, "eligible_count": len(eligible),
                      "criteria": {"min_signals": args.min_signals, "min_trades": args.min_trades,
                                   "max_drawdown_fraction": args.max_drawdown}, "rows": rows}
            destination = Path(config["storage"]["database"]).parent / ("optimization-" + utc_now().strftime("%Y%m%dT%H%M%SZ") + ".json")
            destination.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
            store.journal("optimization", {"report": str(destination), "selected": selected})
            emit({"report": str(destination), "selected": selected, "eligible_count": len(eligible),
                  "rows": rows})
        elif args.command == "run" and args.mode == "paper":
            from .execution import PaperExecutor
            client = PaperClient()
            state_path = config["storage"]["execution_state"]
            with execution_lock(state_path):
                executor = PaperExecutor(client, state_path, max_notional=strategy.max_notional)
                return run_loop(args, config, strategy, store, executor)
        else:
            from .execution import PaperExecutor
            client = PaperClient()
            state_path = config["storage"]["execution_state"]
            with execution_lock(state_path):
                executor = PaperExecutor(client, state_path, max_notional=strategy.max_notional)
                if args.command == "status":
                    emit(executor.status())
                elif args.command == "flatten":
                    result = executor.flatten_owned()
                    store.journal("flatten", result)
                    emit(result)
                    return 0 if result.get("ok", True) else 2
                elif args.command in ("smoke", "paper-test"):
                    notional = args.notional if args.command == "smoke" else args.smoke_notional
                    if not math.isfinite(notional) or not 0 < notional <= min(25, strategy.max_notional):
                        raise ValueError("Smoke notional must be positive and no more than $25")
                    account_status = executor.status()
                    emit({"paper_account": account_status})
                    if account_status.get("ok") is False:
                        return 2
                    try:
                        result = executor.smoke(notional=notional)
                    finally:
                        cleanup = executor.shutdown()
                        store.journal("smoke_cleanup", cleanup)
                        emit({"smoke_cleanup": cleanup})
                    store.journal("smoke", result)
                    emit({"smoke": result})
                    if result.get("ok") is False or cleanup.get("ok") is False:
                        return 2
                    if args.command == "paper-test":
                        return run_loop(args, config, strategy, store, executor)
                elif args.command == "run":
                    return run_loop(args, config, strategy, store, executor)
        return 0
    except MemoryError:
        emit({"error": "Research memory limit reached; choose a shorter --start/--end window", "type": "ResearchLimitError"})
        return 2
    except (RuntimeError, ValueError, OSError, KeyError, TypeError) as error:
        emit({"error": str(error), "type": type(error).__name__})
        return 2
    finally:
        if store:
            store.close()
        limits.close()
