"""Exploratory, causal order-flow rules and a conservative single-venue replay.

This is an explicit BTC/USD adaptation of the supplied order-flow material (the
course transcript and the champion strategy digest), not a claim that either
source defines or validates these exact parameters. The confirmation sequence
is a two-bar absorption/second-failure proxy; see SPEC_ORDERFLOW_UPGRADE.md for
the full rule set and the deferred requirements.
All volume is base-asset volume from one venue. No network or file side effects.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
import math
import re

from .discipline import day_state, record_outcome

UTC = timezone.utc


@dataclass(frozen=True)
class Trade:
    id: str
    timestamp: str
    price: float
    size: float
    side: str


@dataclass(frozen=True)
class StrategyConfig:
    symbol: str = "BTC/USD"
    direction: str = "long"
    flip: bool = False
    bar_seconds: int = 300
    profile_bars: int = 288
    row_size: float = 25.0
    imbalance_ratio: float = 4.0
    stacked_levels: int = 3
    minimum_level_volume: float = 0.001
    minimum_bar_trades: int = 20
    zone_rows: int = 2
    minimum_cluster_bars: int = 3
    minimum_node_fraction: float = 0.1
    min_net_reward_risk: float = 1.2
    taker_fee: float = 0.0025
    slippage_bps: float = 5.0
    risk_fraction: float = 0.0025
    max_notional: float = 100.0
    daily_loss_fraction: float = 0.01
    max_holding_bars: int = 96
    stop_buffer_rows: int = 1
    max_consecutive_losses: int = 2
    execution: str = "taker"

    def __post_init__(self):
        if self.direction not in ("long", "short", "both"):
            raise ValueError("direction must be 'long', 'short', or 'both'")
        if self.execution not in ("taker", "limit"):
            raise ValueError("execution must be 'taker' or 'limit'")
        if not isinstance(self.flip, bool):
            raise ValueError("flip must be a boolean")
        for name in ("bar_seconds", "profile_bars", "stacked_levels", "minimum_bar_trades",
                     "minimum_cluster_bars", "max_holding_bars", "stop_buffer_rows",
                     "max_consecutive_losses"):
            if not isinstance(getattr(self, name), int) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.zone_rows, int) or self.zone_rows < 0:
            raise ValueError("zone_rows must be a nonnegative integer")
        for name in ("row_size", "minimum_level_volume", "max_notional", "min_net_reward_risk"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if not math.isfinite(self.imbalance_ratio) or self.imbalance_ratio <= 1:
            raise ValueError("imbalance_ratio must exceed 1")
        for name in ("risk_fraction", "daily_loss_fraction", "minimum_node_fraction"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if not 0 <= self.taker_fee < 1 or not 0 <= self.slippage_bps < 10000:
            raise ValueError("invalid fee or slippage")


def parse_time(value: str | datetime) -> datetime:
    """Parse RFC3339, truncating fractional precision beyond microseconds."""
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return result.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _bucket(value: datetime, seconds: int) -> datetime:
    return datetime.fromtimestamp(math.floor(value.timestamp() / seconds) * seconds, UTC)


def _row(price: float, config: StrategyConfig) -> int:
    return int((Decimal(str(price)) / Decimal(str(config.row_size))).to_integral_value(rounding=ROUND_FLOOR))


def _row_price(row: int, config: StrategyConfig) -> float:
    return float(Decimal(row) * Decimal(str(config.row_size)))


def _clean_trades(trades: list[Trade]) -> list[Trade]:
    unique = {}
    for trade in trades:
        if trade.side not in ("B", "S"):
            raise ValueError(f"unknown aggressor side: {trade.side!r}")
        if not math.isfinite(trade.price) or trade.price <= 0 or not math.isfinite(trade.size) or trade.size <= 0:
            raise ValueError("trade prices and sizes must be positive and finite")
        parse_time(trade.timestamp)
        key = trade.id or (trade.timestamp, trade.price, trade.size, trade.side)
        if key in unique and unique[key] != trade:
            raise ValueError("conflicting duplicate trade ID")
        unique[key] = trade
    def order_key(trade):
        fraction = re.search(r"\.(\d+)", trade.timestamp)
        remainder = fraction.group(1)[6:] if fraction else ""
        # datetime stores microseconds; exchange tape can distinguish trades
        # inside that microsecond. Preserve that ordering when deriving OHLC.
        submicrosecond = Decimal("0." + remainder) if remainder else Decimal(0)
        return parse_time(trade.timestamp), submicrosecond, trade.id

    return sorted(unique.values(), key=order_key)


def _footprint(trades: list[Trade], config: StrategyConfig) -> dict:
    rows = defaultdict(lambda: {"buy": 0.0, "sell": 0.0})
    for trade in trades:
        rows[_row(trade.price, config)]["buy" if trade.side == "B" else "sell"] += trade.size
    return footprint_rows(rows, config)


def footprint_rows(rows: dict, config: StrategyConfig) -> dict:
    """Finalize signed volume rows, also used by the streaming futures reader."""
    buy_rows, sell_rows = [], []
    for row in sorted(rows):
        buy, sell = rows[row]["buy"], rows[row]["sell"]
        # A zero denominator is valid only at an adjacent observed price row.
        # Missing rows and outside-range zeros cannot manufacture imbalances.
        if row - 1 in rows and buy >= config.minimum_level_volume:
            if buy >= config.imbalance_ratio * rows[row - 1]["sell"]:
                buy_rows.append(row)
        if row + 1 in rows and sell >= config.minimum_level_volume:
            if sell >= config.imbalance_ratio * rows[row + 1]["buy"]:
                sell_rows.append(row)
    result_rows = [dict(row=row, price=_row_price(row, config), **rows[row]) for row in sorted(rows)]
    poc_row = min(rows, key=lambda row: (-(rows[row]["buy"] + rows[row]["sell"]), row)) if rows else None
    return {
        "rows": result_rows,
        "poc_row": poc_row,
        "poc": _row_price(poc_row, config) if poc_row is not None else None,
        "buy_imbalance_rows": buy_rows,
        "sell_imbalance_rows": sell_rows,
        "buy_stacks": _stacks(buy_rows, config.stacked_levels),
        "sell_stacks": _stacks(sell_rows, config.stacked_levels),
        "buy_volume": sum(row["buy"] for row in rows.values()),
        "sell_volume": sum(row["sell"] for row in rows.values()),
        "delta": sum(row["buy"] - row["sell"] for row in rows.values()),
    }


def footprint(trades: list[Trade], config: StrategyConfig) -> dict:
    """Aggregate diagonal footprint imbalances; row price is its lower edge."""
    return _footprint(_clean_trades(trades), config)


def _stacks(rows: list[int], minimum: int) -> list[list[int]]:
    runs, current = [], []
    for row in rows:
        if current and row != current[-1] + 1:
            if len(current) >= minimum:
                runs.append(current)
            current = []
        current.append(row)
    if len(current) >= minimum:
        runs.append(current)
    return runs


@dataclass
class Bar:
    start: datetime
    end: datetime
    first_trade_time: datetime
    last_trade_time: datetime
    open: float
    high: float
    low: float
    close: float
    trade_count: int
    footprint: dict


def build_bars(trades: list[Trade], config: StrategyConfig, as_of: datetime) -> list[Bar]:
    """Return populated, fully closed UTC bars; empty intervals remain gaps."""
    cutoff = parse_time(as_of)
    groups = defaultdict(list)
    for trade in _clean_trades(trades):
        start = _bucket(parse_time(trade.timestamp), config.bar_seconds)
        if start + timedelta(seconds=config.bar_seconds) <= cutoff:
            groups[start].append(trade)
    result = []
    for start, group in sorted(groups.items()):
        result.append(Bar(
            start=start, end=start + timedelta(seconds=config.bar_seconds),
            first_trade_time=parse_time(group[0].timestamp), last_trade_time=parse_time(group[-1].timestamp),
            open=group[0].price, high=max(t.price for t in group), low=min(t.price for t in group),
            close=group[-1].price, trade_count=len(group), footprint=_footprint(group, config),
        ))
    return result


def _costs(entry: float, stop: float, target: float, config: StrategyConfig, *, entry_filled=False) -> dict:
    slip = config.slippage_bps / 10000
    if config.execution == "limit":
        fill = entry  # maker fill at the level; no entry fee or adverse entry slippage
        stopped = stop * (1 - slip)  # market exit
        sold = target  # limit exit
        risk = fill - stopped + stopped * config.taker_fee
        reward = sold - fill
        return {
            "entry_fill": fill, "risk_per_unit": risk, "reward_per_unit": reward,
            "net_reward_risk": reward / risk if risk > 0 else 0.0,
            "expected_cost_fraction": stopped * config.taker_fee / entry,
        }
    fill = entry if entry_filled else entry * (1 + slip)
    purchase = fill * (1 + config.taker_fee)
    stopped = stop * (1 - slip) * (1 - config.taker_fee)
    sold = target * (1 - slip) * (1 - config.taker_fee)
    risk, reward = purchase - stopped, sold - purchase
    return {
        "entry_fill": fill, "risk_per_unit": risk, "reward_per_unit": reward,
        "net_reward_risk": reward / risk if risk > 0 else 0.0,
        "expected_cost_fraction": (purchase - entry + target - sold) / entry,
    }


def position_size(entry: float, stop: float, equity: float, config: StrategyConfig) -> float:
    """Base-asset quantity bounded by stop risk, cash including fees and notional."""
    if not all(math.isfinite(v) for v in (entry, stop, equity)) or equity <= 0 or not 0 < stop < entry:
        return 0.0
    costs = _costs(entry, stop, entry, config)
    return max(0.0, min(
        equity * config.risk_fraction / costs["risk_per_unit"],
        config.max_notional / costs["entry_fill"],
        equity / (costs["entry_fill"] * (1 + config.taker_fee)),
    ))


def _sell_absorption(setup: Bar, config: StrategyConfig) -> bool:
    body_low = min(setup.open, setup.close)
    return (
        setup.close > setup.open
        and body_low > setup.low
        and setup.footprint["delta"] < 0
        and any(_row_price(row, config) <= body_low
                for row in setup.footprint["sell_imbalance_rows"])
    )


def _higher_second_failure(setup: Bar, confirmation: Bar) -> bool:
    return (
        confirmation.low < confirmation.open
        and confirmation.low < setup.close
        and confirmation.low > setup.low
    )


def _bullish_dominance_shift(confirmation: Bar, config: StrategyConfig) -> bool:
    return confirmation.close > confirmation.open and any(
        confirmation.close >= _row_price(stack[-1], config)
        for stack in confirmation.footprint["buy_stacks"])


def _analyze_long(bars: list[Bar], config: StrategyConfig, as_of: datetime, *, cost_model=None, continuity=None) -> dict:
    costs_for = cost_model or _costs
    expected_end = _bucket(parse_time(as_of), config.bar_seconds)
    result = {"status": "no_signal", "reason": "insufficient_history", "bar_end": _iso(expected_end),
              "features": {}, "signal": None}
    if not bars or bars[-1].end != expected_end:
        result["reason"] = "missing_latest_closed_bar"
        return result
    if len(bars) < config.profile_bars + 2:
        result["features"] = {"available_prior_bars": max(0, len(bars) - 2),
                              "required_prior_bars": config.profile_bars}
        return result
    window = bars[-config.profile_bars - 2:]
    if any(not (continuity(left.end, right.start) if continuity else right.start == left.end)
           for left, right in zip(window, window[1:])):
        result["reason"] = "gaps_in_profile"
        return result
    prior, setup, confirmation = window[:-2], window[-2], window[-1]
    profile = defaultdict(float)
    pocs = []
    for bar in prior:
        pocs.append(bar.footprint["poc_row"])
        for row in bar.footprint["rows"]:
            profile[row["row"]] += row["buy"] + row["sell"]
    profile_poc = min(profile, key=lambda row: (-profile[row], row))
    setup_fp, confirmation_fp = setup.footprint, confirmation.footprint
    absorption = _sell_absorption(setup, config)
    second_failure = _higher_second_failure(setup, confirmation)
    dominance = _bullish_dominance_shift(confirmation, config)
    # CVD is explicitly reset at this rolling window's start, not a swing-divergence signal.
    features = {
        "open": confirmation.open, "high": confirmation.high,
        "low": confirmation.low, "close": confirmation.close,
        "trade_count": confirmation.trade_count,
        "setup_open": setup.open, "setup_high": setup.high,
        "setup_low": setup.low, "setup_close": setup.close,
        "setup_trade_count": setup.trade_count, "setup_delta": setup_fp["delta"],
        "setup_poc": setup_fp["poc"], "setup_sell_imbalances": setup_fp["sell_imbalance_rows"],
        "profile_trade_count": sum(b.trade_count for b in prior),
        "profile_start": _iso(prior[0].start), "profile_end": _iso(prior[-1].end),
        "profile_poc": _row_price(profile_poc, config), "bar_poc": confirmation_fp["poc"],
        "buy_volume": confirmation_fp["buy_volume"], "sell_volume": confirmation_fp["sell_volume"],
        "delta": confirmation_fp["delta"],
        "window_cvd": sum(b.footprint["delta"] for b in window),
        "cvd_reset": _iso(prior[0].start), "buy_stacks": confirmation_fp["buy_stacks"],
        "sell_stacks": confirmation_fp["sell_stacks"],
        "sell_absorption": absorption, "higher_second_failure": second_failure,
        "bullish_dominance_shift": dominance,
    }
    result["features"] = features
    if (setup.trade_count < config.minimum_bar_trades
            or confirmation.trade_count < config.minimum_bar_trades
            or features["profile_trade_count"] < config.profile_bars * config.minimum_bar_trades):
        result["reason"] = "insufficient_trade_activity"
        return result
    candidates = []
    for center in sorted(set(pocs)):
        count = sum(abs(row - center) <= config.zone_rows for row in pocs)
        if count < config.minimum_cluster_bars:
            continue
        lower = _row_price(center - config.zone_rows, config)
        upper = _row_price(center + config.zone_rows + 1, config)
        if setup.low <= upper and setup.high >= lower and setup.close > upper:
            volume = sum(v for row, v in profile.items() if abs(row - center) <= config.zone_rows)
            candidates.append((count, volume, center, lower, upper))
    if not candidates:
        result["reason"] = "no_poc_zone_reaction"
        return result
    count, _, center, zone_low, zone_high = max(candidates, key=lambda value: (value[0], value[1], -value[2]))
    features.update({"zone_low": zone_low, "zone_high": zone_high, "cluster_bars": count,
                     "zone_center": _row_price(center, config)})
    if not absorption:
        result["reason"] = "no_bullish_absorption"
        return result
    if not second_failure:
        result["reason"] = "no_higher_second_failure"
        return result
    if not dominance:
        result["reason"] = "no_bullish_dominance_shift"
        return result
    entry = confirmation.close
    stop = min(setup.low, zone_low) - config.stop_buffer_rows * config.row_size
    upper_nodes = [row for row, volume in profile.items()
                   if _row_price(row, config) > entry
                   and volume >= profile[profile_poc] * config.minimum_node_fraction
                   and volume >= profile.get(row - 1, 0)
                   and volume >= profile.get(row + 1, 0)]
    swing = max(bar.high for bar in prior)
    node = _row_price(min(upper_nodes), config) if upper_nodes else None
    if node is not None and costs_for(entry, stop, node, config)["net_reward_risk"] >= config.min_net_reward_risk:
        target, target_source = node, "prior_profile_node"
    else:
        target, target_source = swing, "prior_high"
    features.update({"proposed_entry": entry, "proposed_stop": stop, "proposed_target": target,
                     "target_source": target_source})
    if not 0 < stop < entry < target:
        result["reason"] = "no_valid_target_or_stop"
        return result
    costs = costs_for(entry, stop, target, config)
    features["net_reward_risk"] = costs["net_reward_risk"]
    features["expected_cost_fraction"] = costs["expected_cost_fraction"]
    if costs["net_reward_risk"] < config.min_net_reward_risk:
        result["reason"] = "insufficient_reward_after_costs"
        return result
    signature = json.dumps({"config": asdict(config), "end": result["bar_end"],
                            "entry": entry, "stop": stop, "target": target}, sort_keys=True)
    result.update(status="signal", reason="absorption_second_failure_confirmation", signal={
        "id": "of-" + hashlib.sha256(signature.encode()).hexdigest()[:24],
        "symbol": config.symbol, "entry": entry, "stop": stop, "target": target,
        "direction": "long",
        "expected_cost_fraction": costs["expected_cost_fraction"], "net_reward_risk": costs["net_reward_risk"],
    })
    return result


def _bearish_absorption(setup: Bar, config: StrategyConfig) -> bool:
    body_high = max(setup.open, setup.close)
    return (
        setup.close < setup.open
        and body_high < setup.high
        and setup.footprint["delta"] > 0
        and any(_row_price(row + 1, config) >= body_high
                for row in setup.footprint["buy_imbalance_rows"])
    )


def _lower_second_failure(setup: Bar, confirmation: Bar) -> bool:
    return (
        confirmation.high > confirmation.open
        and confirmation.high > setup.close
        and confirmation.high < setup.high
    )


def _bearish_dominance_shift(confirmation: Bar, config: StrategyConfig) -> bool:
    return confirmation.close < confirmation.open and any(
        confirmation.close <= _row_price(stack[0] + 1, config)
        for stack in confirmation.footprint["sell_stacks"])


def _short_costs(entry: float, stop: float, target: float, config: StrategyConfig, *, entry_filled=False) -> dict:
    slip = config.slippage_bps / 10000
    if config.execution == "limit":
        fill = entry  # maker short at the level; no entry fee or adverse entry slippage
        stop_buyback = stop * (1 + slip) * (1 + config.taker_fee)  # market exit
        target_buyback = target  # limit exit
        risk, reward = stop_buyback - fill, fill - target_buyback
        return {
            "entry_fill": fill, "risk_per_unit": risk, "reward_per_unit": reward,
            "net_reward_risk": reward / risk if risk > 0 else 0.0,
            "expected_cost_fraction": stop * config.taker_fee / entry,
        }
    fill = entry if entry_filled else entry * (1 - slip)
    proceeds = fill * (1 - config.taker_fee)
    stop_buyback = stop * (1 + slip) * (1 + config.taker_fee)
    target_buyback = target * (1 + slip) * (1 + config.taker_fee)
    risk, reward = stop_buyback - proceeds, proceeds - target_buyback
    return {
        "entry_fill": fill, "risk_per_unit": risk, "reward_per_unit": reward,
        "net_reward_risk": reward / risk if risk > 0 else 0.0,
        "expected_cost_fraction": ((entry - proceeds) + (target_buyback - target)) / entry,
    }


def short_position_size(entry: float, stop: float, equity: float, config: StrategyConfig) -> float:
    if not all(math.isfinite(v) for v in (entry, stop, equity)) or equity <= 0 or not 0 < entry < stop:
        return 0.0
    costs = _short_costs(entry, stop, entry, config)
    return max(0.0, min(
        equity * config.risk_fraction / costs["risk_per_unit"],
        config.max_notional / costs["entry_fill"],
    ))


def _replay_short(trades: list[Trade], config: StrategyConfig, initial_equity: float = 10000.0,
                  flip_signals=False) -> dict:
    """Mirrored short replay: sells at the next bar's open, buys back on exits.

    Cash increases by the credited entry proceeds; exits buy back at
    `reference * (1 + slip)` plus fee. Stop-first when both bounds are touched.
    Last open position is bought back at the final closed bar's close.
    """
    if not math.isfinite(initial_equity) or initial_equity <= 0:
        raise ValueError("initial_equity must be positive and finite")
    clean = _clean_trades(trades)
    if not clean:
        return {"summary": {"initial_equity": initial_equity, "final_equity": initial_equity,
                            "net_pnl": 0.0, "trade_count": 0, "signal_count": 0}, "trades": []}
    cutoff = parse_time(clean[-1].timestamp)
    bars = build_bars(clean, config, cutoff)
    cash, peak, drawdown = initial_equity, initial_equity, 0.0
    position, pending, day, day_start, day_halted = None, None, None, initial_equity, False
    completed, reasons = [], defaultdict(int)
    signal_count, skipped_entries, limit_unfilled = 0, 0, 0
    discipline = {}
    slip = config.slippage_bps / 10000

    def mark(price):
        if position is None:
            return cash
        return cash - position["quantity"] * price * (1 + slip) * (1 + config.taker_fee)

    def close_position(reference, timestamp, reason):
        nonlocal cash, position
        if config.execution == "limit" and reason == "target":
            fill, fee = reference, 0.0
        else:
            fill = reference * (1 + slip)
            fee = position["quantity"] * fill * config.taker_fee
        cost = position["quantity"] * fill
        cash -= cost + fee
        completed.append({**position, "exit_time": _iso(timestamp), "exit": fill, "exit_fee": fee,
                          "exit_reason": reason, "net_pnl": position["entry_cost"] - cost - fee,
                          "net_r": (position["entry_cost"] - cost - fee) / position["planned_risk_usd"]
                          if position["planned_risk_usd"] > 0 else None,
                          "equity_after": cash})
        record_outcome(discipline, completed[-1]["net_pnl"], bar.start, config.max_consecutive_losses)
        position = None

    for index, bar in enumerate(bars):
        bar_day = bar.start.date()
        if day != bar_day:
            day, day_start, day_halted = bar_day, mark(bar.open), False
        day_state(discipline, bar.start)
        day_halted = day_halted or bool(discipline["halted_reason"])
        if mark(bar.open) <= day_start * (1 - config.daily_loss_fraction):
            day_halted = True
        if position is not None and day_halted:
            close_position(bar.open, bar.first_trade_time, "daily_loss_limit")
        if pending is not None and position is None:
            signal = pending["signal"]
            if not day_halted and bar.start == pending["end"]:
                if config.execution == "limit":
                    fill = signal["entry"]
                else:
                    fill = bar.open * (1 - slip)
                costs = _short_costs(fill, signal["stop"], signal["target"], config, entry_filled=True)
                if config.execution == "limit" and not (bar.low <= fill <= bar.high):
                    limit_unfilled += 1
                elif signal["target"] < fill < signal["stop"] and costs["net_reward_risk"] >= config.min_net_reward_risk:
                    available_risk = min(cash * config.risk_fraction,
                                         max(0.0, cash - day_start * (1 - config.daily_loss_fraction)))
                    qty = max(0.0, min(available_risk / costs["risk_per_unit"], config.max_notional / fill))
                    if qty > 0:
                        if config.execution == "limit":
                            entry_cost = qty * fill
                            fee = 0.0
                        else:
                            entry_cost = qty * fill * (1 - config.taker_fee)
                            fee = qty * fill * config.taker_fee
                        position = {"signal_id": signal["id"], "signal_time": _iso(pending["end"]),
                                    "entry_time": _iso(bar.first_trade_time), "entry": fill,
                                    "quantity": qty, "stop": signal["stop"], "target": signal["target"],
                                    "entry_fee": fee, "entry_cost": entry_cost,
                                    "entry_bar_index": index, "planned_risk_usd": qty * costs["risk_per_unit"],
                                    "features": pending["features"]}
                        cash += entry_cost
                    else:
                        skipped_entries += 1
                else:
                    skipped_entries += 1
            else:
                skipped_entries += 1
            pending = None
        if position is not None:
            # A daily-loss liquidation threshold includes the estimated buyback
            # fee and slippage; gap fills remain worse than the threshold.
            daily_price = (cash - day_start * (1 - config.daily_loss_fraction)) / (
                position["quantity"] * (1 + slip) * (1 + config.taker_fee))
            effective_stop = min(position["stop"], daily_price)
            if bar.high >= effective_stop:
                daily_exit = daily_price < position["stop"]
                reference = max(bar.open, effective_stop)
                close_position(reference,
                               bar.first_trade_time if bar.open >= effective_stop else bar.end,
                               "daily_loss_limit" if daily_exit else "stop")
                day_halted = day_halted or daily_exit
            elif bar.low <= position["target"]:
                close_position(position["target"], bar.end, "target")
            elif index - position["entry_bar_index"] + 1 >= config.max_holding_bars:
                close_position(bar.close, bar.end, "max_holding")
        day_halted = day_halted or bool(discipline["halted_reason"])
        equity = mark(bar.close)
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
        if equity <= day_start * (1 - config.daily_loss_fraction):
            day_halted = True
        observation = _analyze_bars(bars[max(0, index - config.profile_bars - 1):index + 1], config, bar.end)
        reasons[observation["reason"]] += 1
        if observation["signal"] is not None:
            signal_count += 1
            if flip_signals:
                observation = {**{k: observation[k] for k in ("status", "reason", "bar_end", "features")},
                               "signal": _flip_signal(observation["signal"])}
            if position is None and not day_halted:
                pending = {"signal": observation["signal"], "end": bar.end, "features": observation.get("features")}
    if position is not None:
        close_position(bars[-1].close, bars[-1].end, "end_of_data")
    wins = [trade["net_pnl"] for trade in completed if trade["net_pnl"] > 0]
    losses = [-trade["net_pnl"] for trade in completed if trade["net_pnl"] < 0]
    return {"summary": {
        "initial_equity": initial_equity, "final_equity": cash, "net_pnl": cash - initial_equity,
        "return_fraction": cash / initial_equity - 1, "max_drawdown_fraction": drawdown,
        "drawdown_basis": "closed-bar equity after estimated liquidation costs",
        "trade_count": len(completed), "signal_count": signal_count, "skipped_entries": skipped_entries,
        "limit_unfilled": limit_unfilled,
        "win_rate": len(wins) / len(completed) if completed else None,
        "profit_factor": sum(wins) / sum(losses) if losses else None,
        "net_expectancy": (cash - initial_equity) / len(completed) if completed else None,
        "fees": sum(t["entry_fee"] + t["exit_fee"] for t in completed),
        "bars": len(bars), "start": _iso(bars[0].start) if bars else None,
        "end": _iso(bars[-1].end) if bars else None, "reason_counts": dict(reasons),
        "assumptions": "single-venue OHLC short replay; next-bar first-trade entries; stop first if both touched; "
                       "taker fees and adverse slippage on both sides; no liquidity/queue model; "
                       "final partial bar excluded; remaining position liquidated at end",
    }, "trades": completed}


def _analyze_short(bars: list[Bar], config: StrategyConfig, as_of: datetime, *, cost_model=None, continuity=None) -> dict:
    costs_for = cost_model or _short_costs
    expected_end = _bucket(parse_time(as_of), config.bar_seconds)
    result = {"status": "no_signal", "reason": "insufficient_history", "bar_end": _iso(expected_end),
              "features": {}, "signal": None}
    if not bars or bars[-1].end != expected_end:
        result["reason"] = "missing_latest_closed_bar"
        return result
    if len(bars) < config.profile_bars + 2:
        result["features"] = {"available_prior_bars": max(0, len(bars) - 2),
                              "required_prior_bars": config.profile_bars}
        return result
    window = bars[-config.profile_bars - 2:]
    if any(not (continuity(left.end, right.start) if continuity else right.start == left.end)
           for left, right in zip(window, window[1:])):
        result["reason"] = "gaps_in_profile"
        return result
    prior, setup, confirmation = window[:-2], window[-2], window[-1]
    profile = defaultdict(float)
    pocs = []
    for bar in prior:
        pocs.append(bar.footprint["poc_row"])
        for row in bar.footprint["rows"]:
            profile[row["row"]] += row["buy"] + row["sell"]
    profile_poc = min(profile, key=lambda row: (-profile[row], row))
    setup_fp, confirmation_fp = setup.footprint, confirmation.footprint
    absorption = _bearish_absorption(setup, config)
    second_failure = _lower_second_failure(setup, confirmation)
    dominance = _bearish_dominance_shift(confirmation, config)
    # CVD is explicitly reset at this rolling window's start, not a swing-divergence signal.
    features = {
        "open": confirmation.open, "high": confirmation.high,
        "low": confirmation.low, "close": confirmation.close,
        "trade_count": confirmation.trade_count,
        "setup_open": setup.open, "setup_high": setup.high,
        "setup_low": setup.low, "setup_close": setup.close,
        "setup_trade_count": setup.trade_count, "setup_delta": setup_fp["delta"],
        "setup_poc": setup_fp["poc"], "setup_buy_imbalances": setup_fp["buy_imbalance_rows"],
        "profile_trade_count": sum(b.trade_count for b in prior),
        "profile_start": _iso(prior[0].start), "profile_end": _iso(prior[-1].end),
        "profile_poc": _row_price(profile_poc, config), "bar_poc": confirmation_fp["poc"],
        "buy_volume": confirmation_fp["buy_volume"], "sell_volume": confirmation_fp["sell_volume"],
        "delta": confirmation_fp["delta"],
        "window_cvd": sum(b.footprint["delta"] for b in window),
        "cvd_reset": _iso(prior[0].start), "buy_stacks": confirmation_fp["buy_stacks"],
        "sell_stacks": confirmation_fp["sell_stacks"],
        "buy_absorption": absorption, "lower_second_failure": second_failure,
        "bearish_dominance_shift": dominance,
    }
    result["features"] = features
    if (setup.trade_count < config.minimum_bar_trades
            or confirmation.trade_count < config.minimum_bar_trades
            or features["profile_trade_count"] < config.profile_bars * config.minimum_bar_trades):
        result["reason"] = "insufficient_trade_activity"
        return result
    candidates = []
    for center in sorted(set(pocs)):
        count = sum(abs(row - center) <= config.zone_rows for row in pocs)
        if count < config.minimum_cluster_bars:
            continue
        lower = _row_price(center - config.zone_rows, config)
        upper = _row_price(center + config.zone_rows + 1, config)
        if setup.high >= lower and setup.low <= upper and setup.close < lower:
            volume = sum(v for row, v in profile.items() if abs(row - center) <= config.zone_rows)
            candidates.append((count, volume, center, lower, upper))
    if not candidates:
        result["reason"] = "no_poc_zone_rejection"
        return result
    count, _, center, zone_low, zone_high = max(candidates, key=lambda value: (value[0], value[1], -value[2]))
    features.update({"zone_low": zone_low, "zone_high": zone_high, "cluster_bars": count,
                     "zone_center": _row_price(center, config)})
    if not absorption:
        result["reason"] = "no_bearish_absorption"
        return result
    if not second_failure:
        result["reason"] = "no_lower_second_failure"
        return result
    if not dominance:
        result["reason"] = "no_bearish_dominance_shift"
        return result
    entry = confirmation.close
    stop = max(setup.high, zone_high) + config.stop_buffer_rows * config.row_size
    lower_nodes = [row for row, volume in profile.items()
                   if _row_price(row + 1, config) < entry
                   and volume >= profile[profile_poc] * config.minimum_node_fraction
                   and volume >= profile.get(row - 1, 0)
                   and volume >= profile.get(row + 1, 0)]
    swing = min(bar.low for bar in prior)
    node = _row_price(max(lower_nodes), config) if lower_nodes else None
    if node is not None and costs_for(entry, stop, node, config)["net_reward_risk"] >= config.min_net_reward_risk:
        target, target_source = node, "prior_profile_node"
    else:
        target, target_source = swing, "prior_low"
    features.update({"proposed_entry": entry, "proposed_stop": stop, "proposed_target": target,
                     "target_source": target_source})
    if not 0 < target < entry < stop:
        result["reason"] = "no_valid_target_or_stop"
        return result
    costs = costs_for(entry, stop, target, config)
    features["net_reward_risk"] = costs["net_reward_risk"]
    features["expected_cost_fraction"] = costs["expected_cost_fraction"]
    if costs["net_reward_risk"] < config.min_net_reward_risk:
        result["reason"] = "insufficient_reward_after_costs"
        return result
    signature = json.dumps({"config": asdict(config), "end": result["bar_end"],
                            "entry": entry, "stop": stop, "target": target}, sort_keys=True)
    result.update(status="signal", reason="absorption_second_failure_confirmation", signal={
        "id": "of-" + hashlib.sha256(signature.encode()).hexdigest()[:24],
        "symbol": config.symbol, "entry": entry, "stop": stop, "target": target,
        "direction": "short",
        "expected_cost_fraction": costs["expected_cost_fraction"], "net_reward_risk": costs["net_reward_risk"],
    })
    return result


def _analyze_bars(bars: list[Bar], config: StrategyConfig, as_of: datetime, *, cost_model=None, continuity=None) -> dict:
    options = {"cost_model": cost_model, "continuity": continuity}
    if config.direction == "short":
        return _analyze_short(bars, config, as_of, **options)
    long_result = _analyze_long(bars, config, as_of, **options)
    if config.direction == "long" or long_result["status"] == "signal":
        return long_result
    short_result = _analyze_short(bars, config, as_of, **options)
    if short_result["status"] == "signal":
        return short_result
    return long_result


def analyze(trades: list[Trade], config: StrategyConfig, as_of: datetime) -> dict:
    """Analyze the setup and confirmation bars against strictly earlier profile bars."""
    return _analyze_bars(build_bars(trades, config, as_of), config, as_of)


def _flip_signal(signal: dict) -> dict:
    """Contrarian mirror of a signal: same entry, stop/target distances inverted."""
    entry = signal["entry"]
    stop = entry + (entry - signal["stop"])
    target = entry - (signal["target"] - entry)
    direction = "short" if signal.get("direction", "long") == "long" else "long"
    return {**signal, "stop": stop, "target": target, "direction": direction,
            "id": signal["id"] + "-flip"}


def replay(trades: list[Trade], config: StrategyConfig, initial_equity: float = 10000.0) -> dict:
    """Fee/slippage-aware bar replay, next-bar entry, one position.

    Direction comes from config: "long" (default) or "short" (mirror). "both"
    is rejected because a replay must run one direction at a time. With
    config.flip, each signal is traded on the opposite side (contrarian,
    research-only): the stop/target distances are mirrored around the entry.
    """
    if config.direction == "both":
        raise ValueError("replay supports one direction; run long and short separately")
    if config.flip:
        if config.direction == "long":
            return _replay_short(trades, config, initial_equity, flip_signals=True)
        return _replay_long(trades, config, initial_equity, flip_signals=True)
    if config.direction == "short":
        return _replay_short(trades, config, initial_equity)
    return _replay_long(trades, config, initial_equity)


def _replay_long(trades: list[Trade], config: StrategyConfig, initial_equity: float = 10000.0,
                 flip_signals=False) -> dict:
    """Fee/slippage-aware bar replay, next-bar entry, long-only and one position.

    Despite starting from trades, this replay deliberately resolves bars with both
    stop and target touches as stop first. It assumes unlimited simulated fills,
    not an order-book or cross-venue fill model. Last open position is liquidated
    at the final closed bar's close solely to make the report comparable.
    """
    if not math.isfinite(initial_equity) or initial_equity <= 0:
        raise ValueError("initial_equity must be positive and finite")
    clean = _clean_trades(trades)
    if not clean:
        return {"summary": {"initial_equity": initial_equity, "final_equity": initial_equity,
                            "net_pnl": 0.0, "trade_count": 0, "signal_count": 0}, "trades": []}
    # The last tape bar may be partial: only an exact interval boundary in the
    # observed tape proves the previous bar has ended.
    cutoff = parse_time(clean[-1].timestamp)
    bars = build_bars(clean, config, cutoff)
    cash, peak, drawdown = initial_equity, initial_equity, 0.0
    position, pending, day, day_start, day_halted = None, None, None, initial_equity, False
    completed, reasons = [], defaultdict(int)
    signal_count, skipped_entries, limit_unfilled = 0, 0, 0
    discipline = {}
    slip = config.slippage_bps / 10000

    def mark(price):
        if position is None:
            return cash
        return cash + position["quantity"] * price * (1 - slip) * (1 - config.taker_fee)

    def close_position(reference, timestamp, reason):
        nonlocal cash, position
        if config.execution == "limit" and reason == "target":
            fill, fee = reference, 0.0
        else:
            fill = reference * (1 - slip)
            fee = position["quantity"] * fill * config.taker_fee
        proceeds = position["quantity"] * fill
        cash += proceeds - fee
        completed.append({**position, "exit_time": _iso(timestamp), "exit": fill, "exit_fee": fee,
                          "exit_reason": reason, "net_pnl": proceeds - fee - position["entry_cost"],
                          "net_r": (proceeds - fee - position["entry_cost"]) / position["planned_risk_usd"]
                          if position["planned_risk_usd"] > 0 else None,
                          "equity_after": cash})
        record_outcome(discipline, completed[-1]["net_pnl"], bar.start, config.max_consecutive_losses)
        position = None

    for index, bar in enumerate(bars):
        bar_day = bar.start.date()
        if day != bar_day:
            day, day_start, day_halted = bar_day, mark(bar.open), False
        day_state(discipline, bar.start)
        day_halted = day_halted or bool(discipline["halted_reason"])
        if mark(bar.open) <= day_start * (1 - config.daily_loss_fraction):
            day_halted = True
        if position is not None and day_halted:
            close_position(bar.open, bar.first_trade_time, "daily_loss_limit")
        if pending is not None and position is None:
            signal = pending["signal"]
            if not day_halted and bar.start == pending["end"]:
                if config.execution == "limit":
                    fill = signal["entry"]
                else:
                    fill = bar.open * (1 + slip)
                costs = _costs(fill, signal["stop"], signal["target"], config, entry_filled=True)
                if config.execution == "limit" and not (bar.low <= fill <= bar.high):
                    limit_unfilled += 1
                elif signal["stop"] < fill < signal["target"] and costs["net_reward_risk"] >= config.min_net_reward_risk:
                    available_risk = min(cash * config.risk_fraction,
                                         max(0.0, cash - day_start * (1 - config.daily_loss_fraction)))
                    qty = max(0.0, min(available_risk / costs["risk_per_unit"], config.max_notional / fill))
                    if qty > 0:
                        fee = 0.0 if config.execution == "limit" else qty * fill * config.taker_fee
                        position = {"signal_id": signal["id"], "signal_time": _iso(pending["end"]),
                                    "entry_time": _iso(bar.first_trade_time), "entry": fill,
                                    "quantity": qty, "stop": signal["stop"], "target": signal["target"],
                                    "entry_fee": fee, "entry_cost": qty * fill + fee, "entry_bar_index": index,
                                    "planned_risk_usd": qty * costs["risk_per_unit"],
                                    "features": pending["features"]}
                        cash -= position["entry_cost"]
                    else:
                        skipped_entries += 1
                else:
                    skipped_entries += 1
            else:
                skipped_entries += 1
            pending = None
        if position is not None:
            # A daily-loss liquidation threshold includes the estimated exit fee
            # and slippage; gap fills remain worse than the threshold.
            daily_price = (day_start * (1 - config.daily_loss_fraction) - cash) / (
                position["quantity"] * (1 - slip) * (1 - config.taker_fee))
            effective_stop = max(position["stop"], daily_price)
            if bar.low <= effective_stop:
                daily_exit = daily_price > position["stop"]
                close_position(min(bar.open, effective_stop), bar.first_trade_time if bar.open <= effective_stop else bar.end,
                               "daily_loss_limit" if daily_exit else "stop")
                day_halted = day_halted or daily_exit
            elif bar.high >= position["target"]:
                close_position(position["target"], bar.end, "target")
            elif index - position["entry_bar_index"] + 1 >= config.max_holding_bars:
                close_position(bar.close, bar.end, "max_holding")
        day_halted = day_halted or bool(discipline["halted_reason"])
        equity = mark(bar.close)
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
        if equity <= day_start * (1 - config.daily_loss_fraction):
            day_halted = True
        observation = _analyze_bars(bars[max(0, index - config.profile_bars - 1):index + 1], config, bar.end)
        reasons[observation["reason"]] += 1
        if observation["signal"] is not None:
            signal_count += 1
            if flip_signals:
                observation = {**{k: observation[k] for k in ("status", "reason", "bar_end", "features")},
                               "signal": _flip_signal(observation["signal"])}
            if position is None and not day_halted:
                pending = {"signal": observation["signal"], "end": bar.end, "features": observation.get("features")}
    if position is not None:
        close_position(bars[-1].close, bars[-1].end, "end_of_data")
    wins = [trade["net_pnl"] for trade in completed if trade["net_pnl"] > 0]
    losses = [-trade["net_pnl"] for trade in completed if trade["net_pnl"] < 0]
    return {"summary": {
        "initial_equity": initial_equity, "final_equity": cash, "net_pnl": cash - initial_equity,
        "return_fraction": cash / initial_equity - 1, "max_drawdown_fraction": drawdown,
        "drawdown_basis": "closed-bar equity after estimated liquidation costs",
        "trade_count": len(completed), "signal_count": signal_count, "skipped_entries": skipped_entries,
        "limit_unfilled": limit_unfilled,
        "win_rate": len(wins) / len(completed) if completed else None,
        "profit_factor": sum(wins) / sum(losses) if losses else None,
        "net_expectancy": (cash - initial_equity) / len(completed) if completed else None,
        "fees": sum(t["entry_fee"] + t["exit_fee"] for t in completed),
        "bars": len(bars), "start": _iso(bars[0].start) if bars else None,
        "end": _iso(bars[-1].end) if bars else None, "reason_counts": dict(reasons),
        "assumptions": "single-venue OHLC replay; next-bar first-trade entries; stop first if both touched; "
                       "taker fees and adverse slippage on both sides; no liquidity/queue model; "
                       "final partial bar excluded; remaining position liquidated at end",
    }, "trades": completed}
