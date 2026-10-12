"""Bounded MNQ/NQ signed-tape research, with futures contract accounting.

No broker connections. Reuses the existing causal entry rules with a futures
cost callback; execution is a next-bar OHLC model, not an order-book simulator.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import json
import math
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .discipline import day_state, record_outcome
from .strategy import (Bar, StrategyConfig, _analyze_bars, _bucket, _flip_signal,
                       _iso, footprint_rows, parse_time)

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
POINT_VALUE = {"MNQ": 2.0, "NQ": 20.0}
TICK = Decimal("0.25")
MAX_RECORDS = 1_000_000
MAX_BYTES = 256 * 1024 * 1024
MAX_ROWS = 250_000
MAX_CLOSURES = 1024
MAX_MANIFEST_BYTES = 1024 * 1024


@dataclass(frozen=True)
class FuturesModel:
    root: str
    commission_per_side: float
    margin_per_contract: float
    slippage_ticks: int = 1
    max_contracts: int = 1
    minimum_bar_contracts: int = 0

    def __post_init__(self):
        if self.root not in POINT_VALUE:
            raise ValueError("Futures root must be MNQ or NQ")
        if not math.isfinite(self.commission_per_side) or self.commission_per_side < 0:
            raise ValueError("Commission must be finite and nonnegative, in USD per contract per side")
        if not math.isfinite(self.margin_per_contract) or self.margin_per_contract <= 0:
            raise ValueError("An explicit positive margin assumption is required")
        for name in ("slippage_ticks", "max_contracts", "minimum_bar_contracts"):
            value = getattr(self, name)
            if type(value) is not int or value < (1 if name == "max_contracts" else 0):
                raise ValueError(f"Invalid {name}")

    @property
    def point_value(self):
        return POINT_VALUE[self.root]

    @property
    def slippage(self):
        return self.slippage_ticks * float(TICK)

    def costs(self, entry, stop, target, config=None, *, entry_filled=False):
        direction = 1 if stop < entry else -1
        fill = entry if entry_filled else entry + direction * self.slippage
        risk = direction * (fill - (stop - direction * self.slippage)) * self.point_value + 2 * self.commission_per_side
        reward = direction * ((target - direction * self.slippage) - fill) * self.point_value - 2 * self.commission_per_side
        return {"entry_fill": fill, "risk_per_unit": risk, "reward_per_unit": reward,
                "net_reward_risk": reward / risk if risk > 0 else 0.0,
                "expected_cost_fraction": (2 * self.commission_per_side + 2 * self.slippage * self.point_value) / (entry * self.point_value)}


def read_manifest(path):
    path = Path(path)
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Manifest is too large")
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict) or manifest.get("schema") != "tred.futures-tape.v1" or manifest.get("complete") is not True:
        raise ValueError("A complete tred.futures-tape.v1 coverage manifest is required")
    symbol = manifest.get("symbol", "")
    match = re.fullmatch(r"(MNQ|NQ)[HMUZ](?:\d{1,2}|\d{4})", symbol)
    if not match:
        raise ValueError("Use one dated outright contract, e.g. MNQZ6; continuous or mixed contracts are not accepted")
    if manifest.get("format") not in {"canonical", "databento"}:
        raise ValueError("Tape format must be canonical or databento")
    if not isinstance(manifest.get("source"), str) or not manifest["source"].strip():
        raise ValueError("Manifest must identify the tape source")
    if manifest.get("price_format", "decimal") not in {"decimal", "fixed9"}:
        raise ValueError("price_format must be decimal or fixed9")
    start, end = parse_time(manifest["start"]), parse_time(manifest["end"])
    if not timedelta(0) < end - start <= timedelta(days=7):
        raise ValueError("Use a complete tape window of at most seven days")
    closed = []
    for interval in manifest.get("closed_intervals", []):
        lower, upper = map(parse_time, interval)
        if not start <= lower < upper <= end:
            raise ValueError("Invalid declared exchange closure")
        closed.append((lower, upper))
    closed.sort()
    if len(closed) > MAX_CLOSURES:
        raise ValueError(f"At most {MAX_CLOSURES} exchange closures are supported; shorten the window")
    if any(b[0] < a[1] for a, b in zip(closed, closed[1:])):
        raise ValueError("Exchange closures must be bounded and non-overlapping")
    manifest = dict(manifest, root=match.group(1))
    return manifest, (path.parent / manifest["trades_file"]).resolve(), start, end, closed


def timestamp(value):
    """Return UTC datetime and full nanosecond ordering key."""
    if value.isdigit():
        ns = int(value)
        return datetime.fromtimestamp(ns // 1_000_000_000, UTC).replace(microsecond=(ns % 1_000_000_000) // 1000), ns
    parsed = parse_time(value)
    fraction = re.search(r"\.(\d+)", value)
    remainder = int((fraction.group(1)[6:] + "000")[:3]) if fraction else 0
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = parsed - epoch
    return parsed, (delta.days * 86400 + delta.seconds) * 1_000_000_000 + parsed.microsecond * 1000 + remainder


def load_bars(manifest_path, config):
    """Read a sorted CSV once; retain footprint rows, never the raw trade tape."""
    manifest, path, start, end, closures = read_manifest(manifest_path)
    if config.symbol != manifest["symbol"] or config.bar_seconds != 300:
        raise ValueError("Futures research requires the manifest contract and five-minute bars")
    if Decimal(str(config.row_size)) % TICK:
        raise ValueError("Footprint row size must be a multiple of 0.25 points")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Tape exceeds 256 MiB; export a shorter complete window")
    bars, seen = [], set()
    previous_ns = None
    current, rows = None, None
    total_rows = 0
    records = 0
    digest = hashlib.sha256()
    scale = Decimal(1_000_000_000 if manifest.get("price_format") == "fixed9" else 1)

    def finish():
        if current is not None and current["start"] >= start and current["end"] <= end:
            bars.append(Bar(**current, footprint=footprint_rows(rows, config)))

    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or [])
        required = {"price", "size", "side"} | ({"timestamp", "trade_id", "symbol"} if manifest["format"] == "canonical" else {"ts_event", "ts_recv", "action", "sequence", "instrument_id"})
        if not required <= fields:
            raise ValueError(f"Missing signed-tape CSV columns: {sorted(required - fields)}")
        if manifest["format"] == "databento" and "symbol" not in fields and not manifest.get("instrument_id"):
            raise ValueError("Databento CSV needs a symbol column or an explicit instrument_id mapping in the manifest")
        for raw in reader:
            records += 1
            if records > MAX_RECORDS:
                raise ValueError("Tape exceeds one million records; export a shorter complete window")
            if raw.get("symbol", manifest["symbol"]) != manifest["symbol"]:
                raise ValueError("Mixed contracts or rolls are not allowed in a tape")
            if manifest.get("instrument_id") is not None and str(raw.get("instrument_id")) != str(manifest["instrument_id"]):
                raise ValueError("Instrument ID differs from the manifest contract mapping")
            side = raw["side"]
            if manifest["format"] == "databento":
                if raw["action"] != "T" or ("rtype" in raw and raw["rtype"] != "0"):
                    raise ValueError("Only Databento trades schema records are accepted")
                side = {"B": "B", "A": "S"}.get(side)
                identity = tuple(raw.get(k, "") for k in ("publisher_id", "instrument_id", "sequence", "ts_recv", "ts_event", "price", "size", "side"))
                stamp, ns = timestamp(raw["ts_event"])
            else:
                identity = raw["trade_id"]
                if not identity:
                    raise ValueError("Trade ID is required")
                stamp, ns = timestamp(raw["timestamp"])
            if side not in {"B", "S"}:
                raise ValueError("Unknown aggressor side: cannot manufacture or discard order flow")
            if previous_ns is not None and ns < previous_ns:
                raise ValueError("Tape must be sorted by full event timestamp")
            previous_ns = ns
            if not start <= stamp < end:
                raise ValueError("Tape record lies outside declared coverage")
            if any(lower <= stamp < upper for lower, upper in closures):
                raise ValueError("Trade appears inside a declared exchange closure")
            key = hashlib.sha256(json.dumps(identity).encode()).digest()[:16]
            if key in seen:
                raise ValueError("Duplicate/conflicting trade identity; supply a deduplicated tape")
            seen.add(key)
            try:
                price, quantity = Decimal(raw["price"]) / scale, Decimal(raw["size"])
            except (InvalidOperation, TypeError):
                raise ValueError(f"Invalid numeric trade fields on CSV record {records}") from None
            if not price.is_finite() or price <= 0 or price % TICK:
                raise ValueError("Invalid price or non-CME tick increment")
            if not quantity.is_finite() or quantity < 1 or quantity != quantity.to_integral_value():
                raise ValueError("Futures volume must be positive whole contracts")
            digest.update(json.dumps([ns, str(price), int(quantity), side, identity], separators=(",", ":")).encode() + b"\n")
            bucket = _bucket(stamp, 300)
            value = float(price)
            if current is None or bucket != current["start"]:
                finish()
                current = {"start": bucket, "end": bucket + timedelta(seconds=300),
                           "first_trade_time": stamp, "last_trade_time": stamp,
                           "open": value, "high": value, "low": value, "close": value, "trade_count": 0}
                rows = defaultdict(lambda: {"buy": 0, "sell": 0})
            current.update(high=max(current["high"], value), low=min(current["low"], value),
                           close=value, last_trade_time=stamp, trade_count=current["trade_count"] + 1)
            row = int(price // Decimal(str(config.row_size)))
            if row not in rows:
                total_rows += 1
                if total_rows > MAX_ROWS:
                    raise ValueError("Footprint exceeds row memory budget; shorten the window")
            rows[row]["buy" if side == "B" else "sell"] += int(quantity)
        finish()
    if not bars:
        raise ValueError("No fully covered five-minute bars in the input")
    return bars, {"manifest": manifest, "normalized_tape_sha256": digest.hexdigest(),
                  "records": records, "bars": len(bars), "footprint_rows": total_rows,
                  "coverage": "provider/exporter assertion; not independently verified against exchange tape"}, closures


def in_window(stamp):
    local = stamp.astimezone(NY)
    minute = local.hour * 60 + local.minute
    return local.weekday() < 5 and 570 <= minute < 660


def continuity(closures):
    def linked(left_end, right_start):
        if right_start < left_end:
            return False
        cursor = left_end
        for lower, upper in closures:
            if lower <= cursor < upper:
                cursor = upper
            if cursor >= right_start:
                return True
        return cursor == right_start
    return linked


def replay_bars(bars, strategy, model, equity=10000.0, closures=()):
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("Equity must be positive and finite")
    if strategy.direction not in {"long", "short"}:
        raise ValueError("Compare long and short independently")
    match = re.fullmatch(r"(MNQ|NQ)[HMUZ](?:\d{1,2}|\d{4})", strategy.symbol)
    if not match or match.group(1) != model.root:
        raise ValueError("Strategy contract and cost model must describe the same futures root")
    cash = peak = day_start = equity
    drawdown = 0.0
    position = pending = previous = None
    day = None
    discipline = {}
    reasons, skipped = Counter(), Counter()
    completed = []
    signals = 0
    linked = continuity(closures)

    def mark(price):
        if position is None:
            return cash
        p = position
        exit_price = price - p["sign"] * model.slippage
        return cash + p["sign"] * (exit_price - p["entry"]) * p["quantity"] * model.point_value - p["quantity"] * model.commission_per_side

    def close(price, stamp, reason):
        nonlocal cash, position
        p = position
        fill = price - p["sign"] * model.slippage
        gross = p["sign"] * (fill - p["entry"]) * p["quantity"] * model.point_value
        fee = p["quantity"] * model.commission_per_side
        cash += gross - fee
        pnl = gross - fee - p["entry_fee"]
        completed.append({**p, "exit": fill, "exit_time": _iso(stamp), "exit_reason": reason,
                          "exit_fee": fee, "net_pnl": pnl, "net_r": pnl / p["planned_risk_usd"],
                          "equity_after": cash})
        record_outcome(discipline, pnl, day, strategy.max_consecutive_losses)
        position = None

    for index, bar in enumerate(bars):
        if previous and bar.start < previous.end:
            raise ValueError("Futures replay bars must be chronological and non-overlapping")
        local_day = bar.start.astimezone(NY).date()
        stamp_day = datetime(local_day.year, local_day.month, local_day.day, tzinfo=UTC)
        if day != stamp_day:
            day, day_start = stamp_day, mark(bar.open)
            day_state(discipline, day)
        if position and previous and previous.end != bar.start:
            raise ValueError("Missing bar while a futures position is open; cannot model an honest exit")
        if pending:
            signal = pending["signal"]
            sign = 1 if signal["direction"] == "long" else -1
            fill = bar.open + sign * model.slippage
            valid = signal["stop"] < fill < signal["target"] if sign == 1 else signal["target"] < fill < signal["stop"]
            if bar.start != pending["end"] or not in_window(bar.first_trade_time):
                skipped["entry_outside_window_or_gap"] += 1
            elif discipline["halted_reason"] or cash <= day_start * (1 - strategy.daily_loss_fraction):
                skipped["day_halted"] += 1
            elif not valid:
                skipped["entry_gap_geometry"] += 1
            else:
                costs = model.costs(fill, signal["stop"], signal["target"], entry_filled=True)
                available_risk = min(cash * strategy.risk_fraction, max(0, cash - day_start * (1 - strategy.daily_loss_fraction)))
                qty = min(model.max_contracts, math.floor(available_risk / costs["risk_per_unit"]),
                          math.floor(cash / (model.margin_per_contract + model.commission_per_side)))
                if costs["net_reward_risk"] < strategy.min_net_reward_risk:
                    skipped["entry_cost_gate"] += 1
                elif qty < 1:
                    skipped["whole_contract_risk_or_margin"] += 1
                else:
                    fee = qty * model.commission_per_side
                    cash -= fee
                    position = {"signal_id": signal["id"], "direction": signal["direction"], "sign": sign,
                                "signal_time": _iso(pending["end"]), "entry_time": _iso(bar.first_trade_time),
                                "entry": fill, "entry_fee": fee, "quantity": qty,
                                "stop": signal["stop"], "target": signal["target"], "entry_bar": index,
                                "planned_risk_usd": qty * costs["risk_per_unit"]}
            pending = None
        if position:
            p = position
            daily_price = p["entry"] + p["sign"] * (day_start * (1 - strategy.daily_loss_fraction) - cash + p["quantity"] * model.commission_per_side) / (p["quantity"] * model.point_value) + p["sign"] * model.slippage
            stop = max(p["stop"], daily_price) if p["sign"] == 1 else min(p["stop"], daily_price)
            stop_hit = bar.low <= stop if p["sign"] == 1 else bar.high >= stop
            target_hit = bar.high >= p["target"] if p["sign"] == 1 else bar.low <= p["target"]
            if stop_hit:
                price = min(bar.open, stop) if p["sign"] == 1 else max(bar.open, stop)
                close(price, bar.end, "daily_loss_limit" if stop != p["stop"] else "stop")
            elif target_hit:
                close(p["target"], bar.end, "target")
            elif not in_window(bar.end):
                close(bar.close, bar.end, "session_cutoff")
            elif index - p["entry_bar"] + 1 >= strategy.max_holding_bars:
                close(bar.close, bar.end, "max_holding")
        marked = mark(bar.close)
        peak = max(peak, marked)
        drawdown = max(drawdown, (peak - marked) / peak)
        if marked <= day_start * (1 - strategy.daily_loss_fraction):
            discipline["halted_reason"] = discipline["halted_reason"] or "daily_loss_limit"
        if index < 1 or not in_window(bar.end) or not in_window(bars[index - 1].start):
            reasons["outside_entry_window"] += 1
        elif any(b.footprint["buy_volume"] + b.footprint["sell_volume"] < model.minimum_bar_contracts for b in bars[index - 1:index + 1]):
            reasons["insufficient_contract_volume"] += 1
        else:
            observation = _analyze_bars(bars[max(0, index - strategy.profile_bars - 1):index + 1], strategy, bar.end,
                                        cost_model=model.costs, continuity=linked)
            reasons[observation["reason"]] += 1
            if observation["signal"]:
                signals += 1
                signal = _flip_signal(observation["signal"]) if strategy.flip else observation["signal"]
                if not position and not discipline["halted_reason"]:
                    pending = {"signal": signal, "end": bar.end}
        previous = bar
    if position:
        close(bars[-1].close, bars[-1].end, "end_of_data")
        peak = max(peak, cash)
        drawdown = max(drawdown, (peak - cash) / peak)
    wins = [t["net_pnl"] for t in completed if t["net_pnl"] > 0]
    losses = [-t["net_pnl"] for t in completed if t["net_pnl"] < 0]
    return {"summary": {"initial_equity": equity, "final_equity": cash, "net_pnl": cash - equity,
                        "trade_count": len(completed), "signal_count": signals,
                        "win_rate": len(wins) / len(completed) if completed else None,
                        "profit_factor": sum(wins) / sum(losses) if losses else None,
                        "net_expectancy_usd": (cash - equity) / len(completed) if completed else None,
                        "mean_net_r": sum(t["net_r"] for t in completed) / len(completed) if completed else None,
                        "fees": sum(t["entry_fee"] + t["exit_fee"] for t in completed),
                        "max_drawdown_fraction": drawdown, "reason_counts": dict(reasons),
                        "skipped_entries": dict(skipped)}, "trades": completed}


def compare(manifest_path, model, equity=10000.0, *, profile_bars=288, row_ticks=1):
    manifest, _, _, _, _ = read_manifest(manifest_path)
    if model.root != manifest["root"]:
        raise ValueError("Cost model root must match the actual tape")
    if type(row_ticks) is not int or row_ticks < 1:
        raise ValueError("row_ticks must be a positive integer")
    strategy = StrategyConfig(symbol=manifest["symbol"], profile_bars=profile_bars,
                              row_size=row_ticks * float(TICK), minimum_level_volume=1,
                              taker_fee=0, slippage_bps=0, max_holding_bars=18)
    bars, data, closures = load_bars(manifest_path, strategy)
    results = []
    # Fixed comparisons, not a parameter search. Each starts from the same equity.
    for direction, flip in (("long", False), ("short", False), ("long", True), ("short", True)):
        variant = replace(strategy, direction=direction, flip=flip)
        report = replay_bars(bars, variant, model, equity, closures)
        results.append({"direction": direction, "flip": flip, "configuration": asdict(variant), **report})
    return {"schema": "tred.futures-comparison.v1", "data": data, "contract_model": asdict(model),
            "point_value_usd": model.point_value, "tick_size": float(TICK), "tick_value_usd": float(TICK) * model.point_value,
            "session": "09:30 <= entry < 11:00 America/New_York; flatten by 11:00",
            "results": results, "performance_conclusion": "No market-superiority claim: requires matched dates, risk and out-of-sample validation against BTC",
            "limitations": ["Existing POC-zone strategy adaptation; no gamma/Fibonacci/higher-timeframe filters",
                            "Commission and margin are user-supplied assumptions, not verified broker schedules",
                            "Margin is an entry collateral check; no broker-specific variation-margin/liquidation model",
                            "Next-bar fills, stop-first OHLC exits, no queue or depth/liquidity model",
                            "Drawdown uses bar-end marks; intrabar excursions may be worse",
                            "NY weekday window is not an exchange holiday calendar; supply complete sessions and declared closures",
                            "MNQ and NQ must use their own tapes; no volume or price substitution",
                            "A supplied complete flag asserts source coverage; it does not prove absence of dropped ticks"]}
