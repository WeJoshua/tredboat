"""Bounded QQQ/US-equity research on an explicitly downgraded, tick-rule tape.

Shares the causal entry engine with BTC and futures. The aggressor side is not
present in Alpaca equity prints; the user selected the explicit
`tick_prior_price` classification downgrade, which this module records in every
manifest and report instead of pretending the prints were signed.
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
from .futures import (MAX_BYTES, MAX_CLOSURES, MAX_MANIFEST_BYTES, MAX_RECORDS, MAX_ROWS,
                      continuity, in_window, timestamp)
from .strategy import (Bar, StrategyConfig, _analyze_bars, _bucket, _flip_signal,
                       _iso, footprint_rows, parse_time)

UTC = timezone.utc
CENTS = Decimal("0.01")
SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9]{0,5}")
SIDE_RULES = {"tick_prior_price"}


@dataclass(frozen=True)
class EquityModel:
    symbol: str
    commission_fixed_usd: float = 0.0
    commission_bps: float = 0.0
    slippage_bps: float = 1.0
    max_position_fraction: float = 0.25

    def __post_init__(self):
        if not SYMBOL_RE.fullmatch(self.symbol):
            raise ValueError("Equity symbol must be 1-6 uppercase letters/digits")
        for name in ("commission_fixed_usd", "commission_bps", "slippage_bps", "max_position_fraction"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0 or (name == "max_position_fraction" and not 0 < value <= 1):
                raise ValueError(f"Invalid {name}")

    def fee_per_share(self, price):
        return self.commission_fixed_usd + price * self.commission_bps / 10000

    def costs(self, entry, stop, target, config=None, *, entry_filled=False):
        slip = self.slippage_bps / 10000
        fill = entry if entry_filled else entry * (1 + slip)
        stopped, targeted = stop * (1 - slip), target * (1 - slip)
        risk = fill - stopped + self.fee_per_share(fill) + self.fee_per_share(stopped)
        reward = targeted - fill - self.fee_per_share(fill) - self.fee_per_share(targeted)
        return {"entry_fill": fill, "risk_per_unit": risk, "reward_per_unit": reward,
                "net_reward_risk": reward / risk if risk > 0 else 0.0,
                "expected_cost_fraction": (fill + self.fee_per_share(fill) - entry + target - targeted) / entry}


def read_manifest(path):
    path = Path(path)
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Manifest is too large")
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict) or manifest.get("schema") != "tred.equity-tape.v1" or manifest.get("complete") is not True:
        raise ValueError("A complete tred.equity-tape.v1 coverage manifest is required")
    if not SYMBOL_RE.fullmatch(str(manifest.get("symbol", ""))):
        raise ValueError("Manifest needs an uppercase equity symbol, e.g. QQQ")
    if manifest.get("format") != "canonical" or manifest.get("price_format", "decimal") != "decimal":
        raise ValueError("Equity tapes must be canonical CSV with decimal prices")
    inference = manifest.get("side_inference")
    if not isinstance(inference, dict) or inference.get("rule") not in SIDE_RULES or not isinstance(inference.get("note"), str):
        raise ValueError("Equity tapes require an explicit side_inference declaration; exchange prints carry no aggressor side")
    if not isinstance(manifest.get("source"), str) or not manifest["source"].strip():
        raise ValueError("Manifest must identify the tape source")
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
    return manifest, (path.parent / manifest["trades_file"]).resolve(), start, end, closed


def load_bars(manifest_path, config):
    """Read a sorted canonical CSV once; retain footprint rows, never the raw tape."""
    manifest, path, start, end, closures = read_manifest(manifest_path)
    if config.symbol != manifest["symbol"] or config.bar_seconds != 300:
        raise ValueError("Equity research requires the manifest symbol and five-minute bars")
    if Decimal(str(config.row_size)) % CENTS:
        raise ValueError("Footprint row size must be a multiple of 0.01 dollars")
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Tape exceeds 256 MiB; export a shorter complete window")
    bars, seen = [], set()
    previous_ns = None
    current, rows = None, None
    total_rows = 0
    records = 0
    digest = hashlib.sha256()

    def finish():
        if current is not None and current["start"] >= start and current["end"] <= end:
            bars.append(Bar(**current, footprint=footprint_rows(rows, config)))

    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or [])
        required = {"timestamp", "trade_id", "symbol", "price", "size", "side"}
        if not required <= fields:
            raise ValueError(f"Missing canonical equity CSV columns: {sorted(required - fields)}")
        for raw in reader:
            records += 1
            if records > MAX_RECORDS:
                raise ValueError("Tape exceeds one million records; export a shorter complete window")
            if raw.get("symbol") != manifest["symbol"]:
                raise ValueError("Mixed symbols are not allowed in an equity tape")
            identity = raw["trade_id"]
            if not identity:
                raise ValueError("Trade ID is required")
            stamp, ns = timestamp(raw["timestamp"])
            if previous_ns is not None and ns < previous_ns:
                raise ValueError("Tape must be sorted by full event timestamp")
            previous_ns = ns
            if not start <= stamp < end:
                raise ValueError("Tape record lies outside declared coverage")
            if any(lower <= stamp < upper for lower, upper in closures):
                raise ValueError("Trade appears inside a declared exchange closure")
            key = hashlib.sha256(json.dumps([identity, ns]).encode()).digest()[:16]
            if key in seen:
                raise ValueError("Duplicate/conflicting trade identity; supply a deduplicated tape")
            seen.add(key)
            try:
                price, quantity = Decimal(raw["price"]), Decimal(raw["size"])
            except (InvalidOperation, TypeError):
                raise ValueError(f"Invalid numeric trade fields on CSV record {records}") from None
            if not price.is_finite() or price <= 0:
                raise ValueError("Invalid price")
            if not quantity.is_finite() or quantity < 1 or quantity != quantity.to_integral_value():
                raise ValueError("Equity volume must be positive whole shares")
            side = raw["side"].strip().upper()
            if side not in {"B", "S"}:
                raise ValueError("Unknown aggressor side: the tape must carry declared tick-rule sides")
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
                  "coverage": "provider pagination assertion; tick-rule side classification, not exchange aggressor flags"}, closures


def replay_bars(bars, strategy, model, equity=10000.0, closures=()):
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("Equity must be positive and finite")
    if strategy.direction not in {"long", "short"}:
        raise ValueError("Compare long and short independently")
    if strategy.symbol != model.symbol:
        raise ValueError("Strategy symbol and cost model must describe the same equity instrument")
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
        exit_price = price * (1 - p["sign"] * model.slippage_bps / 10000)
        return cash + p["sign"] * (exit_price - p["entry"]) * p["quantity"] - p["quantity"] * model.fee_per_share(exit_price)

    def close(price, stamp, reason):
        nonlocal cash, position
        p = position
        fill = price * (1 - p["sign"] * model.slippage_bps / 10000)
        gross = p["sign"] * (fill - p["entry"]) * p["quantity"]
        fee = p["quantity"] * model.fee_per_share(fill)
        cash += gross - fee
        pnl = gross - fee - p["entry_fee"]
        completed.append({**p, "exit": fill, "exit_time": _iso(stamp), "exit_reason": reason,
                          "exit_fee": fee, "net_pnl": pnl, "net_r": pnl / p["planned_risk_usd"],
                          "equity_after": cash})
        record_outcome(discipline, pnl, day, strategy.max_consecutive_losses)
        position = None

    for index, bar in enumerate(bars):
        if previous and bar.start < previous.end:
            raise ValueError("Equity replay bars must be chronological and non-overlapping")
        local_day = bar.start.astimezone(ZoneInfo("America/New_York")).date()
        stamp_day = datetime(local_day.year, local_day.month, local_day.day, tzinfo=UTC)
        if day != stamp_day:
            day, day_start = stamp_day, mark(bar.open)
            day_state(discipline, day)
        if position and previous and previous.end != bar.start:
            raise ValueError("Missing bar while an equity position is open; cannot model an honest exit")
        if pending:
            signal = pending["signal"]
            sign = 1 if signal["direction"] == "long" else -1
            fill = bar.open * (1 + sign * model.slippage_bps / 10000)
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
                qty = min(math.floor(available_risk / costs["risk_per_unit"]),
                          math.floor(model.max_position_fraction * cash / fill))
                if costs["net_reward_risk"] < strategy.min_net_reward_risk:
                    skipped["entry_cost_gate"] += 1
                elif qty < 1:
                    skipped["whole_share_cap"] += 1
                else:
                    fee = qty * model.fee_per_share(fill)
                    cash -= fee
                    position = {"signal_id": signal["id"], "direction": signal["direction"], "sign": sign,
                                "signal_time": _iso(pending["end"]), "entry_time": _iso(bar.first_trade_time),
                                "entry": fill, "entry_fee": fee, "quantity": qty,
                                "stop": signal["stop"], "target": signal["target"], "entry_bar": index,
                                "planned_risk_usd": qty * costs["risk_per_unit"]}
            pending = None
        if position:
            p = position
            daily_price = p["entry"] + p["sign"] * (day_start * (1 - strategy.daily_loss_fraction) - cash
                                                    + p["quantity"] * model.fee_per_share(p["entry"])
                                                    + p["quantity"] * model.slippage_bps / 10000 * p["entry"]) / (p["quantity"])
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
    if model.symbol != manifest["symbol"]:
        raise ValueError("Cost model symbol must match the actual tape")
    if type(row_ticks) is not int or row_ticks < 1:
        raise ValueError("row_ticks must be a positive integer")
    strategy = StrategyConfig(symbol=manifest["symbol"], profile_bars=profile_bars,
                              row_size=row_ticks * 0.01, minimum_level_volume=1,
                              taker_fee=0, slippage_bps=0, max_holding_bars=18)
    bars, data, closures = load_bars(manifest_path, strategy)
    results = []
    for direction, flip in (("long", False), ("short", False), ("long", True), ("short", True)):
        variant = replace(strategy, direction=direction, flip=flip)
        report = replay_bars(bars, variant, model, equity, closures)
        results.append({"direction": direction, "flip": flip, "configuration": asdict(variant), **report})
    return {"schema": "tred.equity-comparison.v1", "data": data, "equity_model": asdict(model),
            "session": "09:30 <= entry < 11:00 America/New_York; flattened by 11:00",
            "results": results,
            "performance_conclusion": "Side classification is tick-rule inferred; results are not exchange-signed",
            "limitations": [
                "Aggressor side was inferred from prior printed price (tick rule), not exchange flags",
                "Existing POC-zone strategy adaptation; no gamma/Fibonacci/higher-timeframe filters",
                "Alpaca paper-entitled feed (IEX by default) covers one exchange's prints; SIP requires a paid entitlement",
                "Commission and slippage are user-supplied assumptions, not broker quotes",
                "Next-bar fills, stop-first OHLC exits, no queue or depth/liquidity model",
                "Drawdown uses bar-end marks; intrabar excursions may be worse",
                "The 09:30-11:00 NY weekday window is not an exchange holiday calendar; supply complete sessions and declared closures",
                "QQQ is an index proxy, not MNQ futures: whole-share sizing, no margin/contract math",
                "A supplied complete flag asserts exhausted pagination; it does not prove absence of dropped ticks"]}