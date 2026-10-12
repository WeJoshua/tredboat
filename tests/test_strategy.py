import dataclasses
from datetime import datetime, timedelta, timezone
import json
import unittest

from tred.strategy import (StrategyConfig, Trade, _flip_signal, analyze, build_bars, footprint,
                           parse_time, position_size, replay, short_position_size)


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def trade(bar, offset, price, size=1.0, side="B", seconds=900):
    timestamp = START + timedelta(seconds=bar * seconds + offset)
    return Trade(f"{bar}-{offset}", timestamp.isoformat(), price, size, side)


def config(**changes):
    base = StrategyConfig(bar_seconds=900, profile_bars=3, row_size=1, zone_rows=0, minimum_cluster_bars=3,
                          minimum_bar_trades=1, minimum_level_volume=1, imbalance_ratio=3.0)
    return dataclasses.replace(base, **changes)


def setup_tape():
    tape = []
    for bar in range(3):
        tape += [trade(bar, 1, 100.2, 100), trade(bar, 2, 120.2, 2, "S")]
    tape += [trade(3, 1, 100.2, 1), trade(3, 2, 99.2, 5, "S"),
             trade(3, 3, 102.2, 1), trade(3, 4, 103.2, 1)]
    tape += [trade(4, 1, 103.2, 1), trade(4, 2, 101.2, 1, "S"),
             trade(4, 3, 102.2, 3), trade(4, 4, 103.2, 3), trade(4, 5, 104.2, 3),
             trade(4, 6, 105.2, 0.1)]
    return tape


def replace_setup(tape, trades):
    return [t for t in tape if not t.id.startswith("3-")] + trades


def setup_short_tape(with_node=False):
    tape = []
    for bar in range(3):
        tape += [trade(bar, 1, 100.2, 100, "S"), trade(bar, 2, 120.2, 2)]
        if with_node:
            tape += [trade(bar, 3, 80.2, 40, "S"), trade(bar, 4, 79.2, 1, "S")]
        else:
            tape += [trade(bar, 3, 79.2, 1, "S")]
    tape += [trade(3, 1, 100.2, 1, "S"), trade(3, 2, 101.2, 5),
             trade(3, 3, 98.2, 1, "S"), trade(3, 4, 97.2, 1, "S")]
    tape += [trade(4, 1, 97.8, 1, "S"), trade(4, 2, 98.8, 5),
             trade(4, 3, 97.0, 0.5), trade(4, 4, 96.7, 0.5), trade(4, 5, 95.5, 0.5),
             trade(4, 6, 94.5, 0.5), trade(4, 7, 95.7, 3, "S"), trade(4, 8, 94.7, 3, "S"),
             trade(4, 9, 93.7, 3, "S"), trade(4, 10, 92.7, 0.1, "S")]
    return tape


class StrategyTests(unittest.TestCase):
    def test_rfc3339_nanosecond_timestamp_and_timezone(self):
        parsed = parse_time("2026-01-01T00:00:00.123456789Z")
        self.assertEqual(parsed.microsecond, 123456)
        self.assertEqual(parsed.tzinfo, timezone.utc)
        with self.assertRaises(ValueError):
            parse_time("2026-01-01T00:00:00")

    def test_price_rows_use_decimal_floor(self):
        fp = footprint([trade(0, 1, 0.3)], config(row_size=0.1))
        self.assertEqual(fp["rows"][0]["row"], 3)

    def test_nanosecond_tape_order_controls_open_and_close(self):
        tape = [Trade("first-id", "2026-01-01T00:00:01.123456900Z", 110, 1, "B"),
                Trade("last-id", "2026-01-01T00:00:01.123456100Z", 100, 1, "B")]
        bar = build_bars(tape, config(), START + timedelta(minutes=15))[0]
        self.assertEqual(bar.open, 100)
        self.assertEqual(bar.close, 110)

    def test_diagonal_not_horizontal_comparison(self):
        tape = [trade(0, 1, 100.1, 1, "S"), trade(0, 2, 101.1, 10, "S"),
                trade(0, 3, 101.1, 3, "B")]
        fp = footprint(tape, config())
        self.assertIn(101, fp["buy_imbalance_rows"])
        self.assertEqual(fp["delta"], -8)

    def test_four_to_one_imbalance_boundary(self):
        tape = [trade(0, 1, 100.2, 1), trade(0, 2, 99.2, 4, "S")]
        self.assertEqual(footprint(tape, config(imbalance_ratio=4.0))["sell_imbalance_rows"], [99])
        self.assertEqual(footprint(tape, config(imbalance_ratio=4.1))["sell_imbalance_rows"], [])

    def test_missing_adjacent_row_does_not_create_infinite_imbalance(self):
        fp = footprint([trade(0, 1, 100.1, 5), trade(0, 2, 102.1, 5)], config())
        self.assertEqual(fp["buy_imbalance_rows"], [])

    def test_observed_zero_denominator_requires_minimum_volume(self):
        tape = [trade(0, 1, 100.1, 5), trade(0, 2, 101.1, 0.1), trade(0, 3, 102.1, 3)]
        fp = footprint(tape, config())
        self.assertEqual(fp["buy_imbalance_rows"], [102])

    def test_duplicates_are_not_double_counted_and_conflicts_rejected(self):
        item = trade(0, 1, 100, 3)
        self.assertEqual(footprint([item, item], config())["buy_volume"], 3)
        with self.assertRaises(ValueError):
            footprint([item, dataclasses.replace(item, size=4)], config())

    def test_invalid_tape_rejected(self):
        for item in (trade(0, 1, float("nan")), trade(0, 2, 100, 0), trade(0, 3, 100, side="?")):
            with self.subTest(item=item), self.assertRaises(ValueError):
                footprint([item], config())

    def test_synthetic_setup_generates_deterministic_signal(self):
        as_of = START + timedelta(minutes=75)
        result = analyze(setup_tape(), config(), as_of)
        self.assertEqual(result["status"], "signal", result)
        self.assertEqual(result["reason"], "absorption_second_failure_confirmation")
        self.assertEqual(result["signal"]["stop"], 98.2)
        self.assertEqual(result["signal"]["target"], 120.2)
        self.assertTrue(result["features"]["sell_absorption"])
        self.assertTrue(result["features"]["higher_second_failure"])
        self.assertTrue(result["features"]["bullish_dominance_shift"])
        self.assertEqual(result["features"]["profile_poc"], 100)
        self.assertEqual(result["features"]["cluster_bars"], 3)
        self.assertEqual(result["features"]["profile_end"], "2026-01-01T00:45:00Z")
        self.assertEqual(analyze(list(reversed(setup_tape())), config(), as_of), result)
        json.dumps(result, allow_nan=False)

    def test_future_trades_cannot_change_past_signal_or_profile(self):
        as_of = START + timedelta(minutes=75)
        baseline = analyze(setup_tape(), config(), as_of)
        future = [trade(5, 1, 9999, 999999), trade(6, 1, 1, 99999, "S")]
        self.assertEqual(analyze(setup_tape() + future, config(), as_of), baseline)

    def test_open_bar_excluded_and_stale_latest_bar_rejected(self):
        result = analyze(setup_tape(), config(), START + timedelta(minutes=59, seconds=59))
        self.assertEqual(result["reason"], "insufficient_history")
        result = analyze(setup_tape(), config(), START + timedelta(minutes=90))
        self.assertEqual(result["reason"], "missing_latest_closed_bar")

    def test_setup_bar_alone_is_insufficient(self):
        tape = [t for t in setup_tape() if parse_time(t.timestamp) < START + timedelta(minutes=60)]
        result = analyze(tape, config(), START + timedelta(minutes=60))
        self.assertEqual(result["reason"], "insufficient_history")

    def test_absorption_imbalance_in_body_rejected(self):
        tape = replace_setup(setup_tape(), [
            trade(3, 1, 100.2, 1), trade(3, 2, 99.2, 1, "S"),
            trade(3, 3, 103.2, 6, "S"), trade(3, 4, 104.2, 1)])
        result = analyze(tape, config(), START + timedelta(minutes=75))
        self.assertEqual(result["reason"], "no_bullish_absorption")

    def test_absorption_requires_lower_wick_and_delta(self):
        no_wick = replace_setup(setup_tape(), [trade(3, 1, 100.2, 1), trade(3, 2, 103.2, 1)])
        self.assertEqual(analyze(no_wick, config(), START + timedelta(minutes=75))["reason"],
                         "no_bullish_absorption")
        negative_delta_no_wick = replace_setup(setup_tape(), [
            trade(3, 1, 100.2, 1), trade(3, 2, 103.2, 6, "S"), trade(3, 3, 104.2, 1)])
        self.assertEqual(analyze(negative_delta_no_wick, config(), START + timedelta(minutes=75))["reason"],
                         "no_bullish_absorption")
        positive_delta = replace_setup(setup_tape(), [
            trade(3, 1, 100.2, 1), trade(3, 2, 99.2, 3, "S"), trade(3, 3, 103.2, 100)])
        self.assertEqual(analyze(positive_delta, config(), START + timedelta(minutes=75))["reason"],
                         "no_bullish_absorption")

    def test_second_failure_must_hold_above_setup_low(self):
        tape = [dataclasses.replace(t, price=99.2) if t.id == "4-2" else t for t in setup_tape()]
        result = analyze(tape, config(), START + timedelta(minutes=75))
        self.assertEqual(result["reason"], "no_higher_second_failure")
        shallow = [t for t in setup_tape() if not t.id.startswith("4-")]
        shallow += [trade(4, 1, 105.2, 1), trade(4, 2, 104.2, 1, "S"),
                    trade(4, 3, 105.2, 3), trade(4, 4, 106.2, 3), trade(4, 5, 107.2, 3),
                    trade(4, 6, 108.2, 0.1)]
        self.assertEqual(analyze(shallow, config(), START + timedelta(minutes=75))["reason"],
                         "no_higher_second_failure")

    def test_confirmation_must_pull_back_below_open(self):
        tape = [t for t in setup_tape() if not t.id.startswith("4-")]
        tape += [trade(4, 1, 102.2, 1), trade(4, 2, 103.2, 3),
                 trade(4, 3, 104.2, 3), trade(4, 4, 105.2, 0.1)]
        result = analyze(tape, config(), START + timedelta(minutes=75))
        self.assertEqual(result["reason"], "no_higher_second_failure")

    def test_dominance_shift_requires_buy_stack(self):
        tape = [t for t in setup_tape() if not t.id.startswith("4-")]
        tape += [trade(4, 1, 103.2, 1), trade(4, 2, 101.2, 1, "S"),
                 trade(4, 3, 102.2, 3), trade(4, 4, 102.2, 0.5, "S"),
                 trade(4, 5, 104.2, 3), trade(4, 6, 105.2, 0.1)]
        result = analyze(tape, config(), START + timedelta(minutes=75))
        self.assertEqual(result["reason"], "no_bullish_dominance_shift")

    def test_dominance_requires_bullish_close(self):
        tape = [t for t in setup_tape() if not t.id.startswith("4-")]
        tape += [trade(4, 1, 106.2, 1), trade(4, 2, 101.2, 1, "S"),
                 trade(4, 3, 102.2, 3), trade(4, 4, 103.2, 3), trade(4, 5, 104.2, 3),
                 trade(4, 6, 105.4, 0.1)]
        result = analyze(tape, config(), START + timedelta(minutes=75))
        self.assertEqual(result["reason"], "no_bullish_dominance_shift")

    def test_profile_excludes_setup_and_confirmation(self):
        tape = [dataclasses.replace(t, price=110.2, size=50000) if t.id == "4-5" else t
                for t in setup_tape()]
        result = analyze(tape, config(), START + timedelta(minutes=75))
        self.assertEqual(result["features"]["profile_poc"], 100)
        self.assertEqual(result["features"]["profile_end"], "2026-01-01T00:45:00Z")

    def test_profile_gap_blocks_signal(self):
        tape = [dataclasses.replace(t, timestamp=(parse_time(t.timestamp) + timedelta(minutes=15)).isoformat())
                if parse_time(t.timestamp) >= START + timedelta(minutes=30) else t for t in setup_tape()]
        result = analyze(tape, config(), START + timedelta(minutes=90))
        self.assertEqual(result["reason"], "gaps_in_profile")

    def test_activity_threshold_and_fee_hurdle_block_signal(self):
        as_of = START + timedelta(minutes=75)
        self.assertEqual(analyze(setup_tape(), config(minimum_bar_trades=3), as_of)["reason"],
                         "insufficient_trade_activity")
        self.assertEqual(analyze(setup_tape(), config(taker_fee=0.1), as_of)["reason"],
                         "insufficient_reward_after_costs")

    def test_per_bar_activity_floor_blocks_signal(self):
        tape = []
        for bar in range(3):
            tape += [trade(bar, 1, 100.2, 100), trade(bar, 2, 120.2, 2, "S"),
                     trade(bar, 3, 100.2, 1), trade(bar, 4, 100.2, 1),
                     trade(bar, 5, 100.2, 1), trade(bar, 6, 100.2, 1)]
        tape += [trade(3, 1, 100.2, 1), trade(3, 2, 99.2, 5, "S"),
                 trade(3, 3, 102.2, 1), trade(3, 4, 103.2, 1)]
        tape += [trade(4, 1, 103.2, 1), trade(4, 2, 101.2, 1, "S"),
                 trade(4, 3, 102.2, 3), trade(4, 4, 103.2, 3), trade(4, 5, 104.2, 3),
                 trade(4, 6, 105.2, 0.1)]
        result = analyze(tape, config(minimum_bar_trades=6), START + timedelta(minutes=75))
        self.assertEqual(result["reason"], "insufficient_trade_activity")

    def test_profile_node_target_selected_when_qualifying(self):
        tape = []
        for bar in range(3):
            tape += [trade(bar, 1, 100.2, 100), trade(bar, 2, 120.2, 40, "S")]
        tape += [trade(3, 1, 100.2, 1), trade(3, 2, 99.2, 5, "S"),
                 trade(3, 3, 102.2, 1), trade(3, 4, 103.2, 1)]
        tape += [trade(4, 1, 103.2, 1), trade(4, 2, 101.2, 1, "S"),
                 trade(4, 3, 102.2, 3), trade(4, 4, 103.2, 3), trade(4, 5, 104.2, 3),
                 trade(4, 6, 105.2, 0.1)]
        result = analyze(tape, config(), START + timedelta(minutes=75))
        self.assertEqual(result["status"], "signal", result)
        self.assertEqual(result["features"]["target_source"], "prior_profile_node")
        self.assertEqual(result["signal"]["target"], 120.0)

    def _escalation_tape(self, with_swing=True):
        tape = []
        for bar in range(3):
            tape += [trade(bar, 1, 100.2, 100), trade(bar, 2, 108.2, 40, "S")]
            if with_swing:
                tape += [trade(bar, 3, 120.2, 1)]
        tape += [trade(3, 1, 100.2, 1), trade(3, 2, 99.2, 5, "S"),
                 trade(3, 3, 103.2, 1), trade(3, 4, 105.2, 1)]
        tape += [trade(4, 1, 103.2, 1), trade(4, 2, 101.2, 1, "S"),
                 trade(4, 3, 102.2, 3), trade(4, 4, 103.2, 3), trade(4, 5, 104.2, 3),
                 trade(4, 6, 105.2, 0.1)]
        return tape

    def test_target_escalates_to_prior_high_when_node_too_close(self):
        result = analyze(self._escalation_tape(True), config(), START + timedelta(minutes=75))
        self.assertEqual(result["status"], "signal", result)
        self.assertEqual(result["features"]["target_source"], "prior_high")
        self.assertEqual(result["signal"]["target"], 120.2)

    def test_target_without_swing_escalation_rejected(self):
        result = analyze(self._escalation_tape(False), config(), START + timedelta(minutes=75))
        self.assertEqual(result["reason"], "insufficient_reward_after_costs")

    def test_position_size_includes_fees_and_notional_cap(self):
        capped = position_size(100, 99, 10000, config())
        self.assertAlmostEqual(capped * 100 * 1.0005, 100)
        risk_limited = position_size(100, 99, 10000, config(max_notional=100000))
        modeled_unit_risk = 100 * 1.0005 * 1.0025 - 99 * 0.9995 * 0.9975
        self.assertAlmostEqual(risk_limited * modeled_unit_risk, 25)
        self.assertEqual(position_size(100, 101, 10000, config()), 0)

    def test_replay_enters_only_after_signal_and_accounts_for_costs(self):
        tape = setup_tape() + [trade(5, 1, 105), trade(5, 2, 121), trade(5, 3, 110), trade(6, 1, 110)]
        result = replay(tape, config())
        self.assertEqual(result["summary"]["trade_count"], 1, result)
        executed = result["trades"][0]
        self.assertEqual(executed["exit_reason"], "target")
        self.assertGreater(parse_time(executed["entry_time"]), parse_time(executed["signal_time"]))
        self.assertAlmostEqual(executed["entry"], 105 * 1.0005)
        self.assertAlmostEqual(executed["exit"], 120.2 * 0.9995)
        expected_pnl = executed["quantity"] * (executed["exit"] - executed["entry"]) - executed["entry_fee"] - executed["exit_fee"]
        self.assertAlmostEqual(executed["net_pnl"], expected_pnl)
        self.assertAlmostEqual(result["summary"]["final_equity"], 10000 + expected_pnl)
        json.dumps(result, allow_nan=False)

    def test_no_fill_on_signal_close_or_unfinished_next_bar(self):
        tape = setup_tape() + [trade(5, 1, 105)]
        result = replay(tape, config())
        self.assertEqual(result["summary"]["signal_count"], 1)
        self.assertEqual(result["trades"], [])

    def test_stop_wins_when_both_bounds_touched(self):
        tape = setup_tape() + [trade(5, 1, 100), trade(5, 2, 125), trade(5, 3, 90), trade(6, 1, 105)]
        result = replay(tape, config())
        self.assertEqual(result["trades"][0]["exit_reason"], "stop")
        self.assertAlmostEqual(result["trades"][0]["exit"], 98.2 * 0.9995)

    def test_stop_gap_fills_at_worse_open(self):
        tape = setup_tape() + [trade(5, 1, 105), trade(6, 1, 90), trade(7, 1, 90)]
        executed = replay(tape, config())["trades"][0]
        self.assertEqual(executed["exit_reason"], "stop")
        self.assertAlmostEqual(executed["exit"], 90 * 0.9995)

    def test_entry_gap_that_invalidates_reward_is_rejected(self):
        tape = setup_tape() + [trade(5, 1, 119), trade(6, 1, 119)]
        result = replay(tape, config())
        self.assertEqual(result["trades"], [])
        self.assertEqual(result["summary"]["skipped_entries"], 1)

    def test_daily_loss_limit_caps_entry_risk(self):
        tape = setup_tape() + [trade(5, 1, 105), trade(5, 2, 98), trade(6, 1, 99)]
        result = replay(tape, config(max_notional=100000, risk_fraction=0.5, daily_loss_fraction=0.001))
        self.assertEqual(len(result["trades"]), 1)
        self.assertAlmostEqual(result["summary"]["net_pnl"], -10, places=6)

    def test_end_of_data_liquidation_includes_exit_fee(self):
        tape = setup_tape() + [trade(5, 1, 105), trade(5, 2, 106), trade(6, 1, 106)]
        result = replay(tape, config())
        self.assertEqual(result["trades"][0]["exit_reason"], "end_of_data")
        self.assertGreater(result["trades"][0]["exit_fee"], 0)

    def test_replay_max_holding_closes_position(self):
        tape = setup_tape() + [trade(5, 1, 105), trade(5, 2, 106), trade(6, 1, 106)]
        executed = replay(tape, config(max_holding_bars=1))["trades"][0]
        self.assertEqual(executed["exit_reason"], "max_holding")
        self.assertAlmostEqual(executed["exit"], 106 * 0.9995)

    def test_closed_bars_have_no_future_or_partial_trades(self):
        bars = build_bars(setup_tape(), config(), START + timedelta(minutes=59))
        self.assertEqual(len(bars), 3)
        self.assertEqual(bars[-1].end, START + timedelta(minutes=45))

    def test_direction_validation(self):
        with self.assertRaises(ValueError):
            StrategyConfig(direction="sideways")
        self.assertEqual(StrategyConfig().direction, "long")

    def test_synthetic_short_setup_generates_deterministic_signal(self):
        as_of = START + timedelta(minutes=75)
        result = analyze(setup_short_tape(), config(direction="short"), as_of)
        self.assertEqual(result["status"], "signal", result)
        self.assertEqual(result["reason"], "absorption_second_failure_confirmation")
        self.assertEqual(result["signal"]["direction"], "short")
        self.assertEqual(result["signal"]["stop"], 102.2)
        self.assertEqual(result["signal"]["entry"], 92.7)
        self.assertEqual(result["signal"]["target"], 79.2)
        self.assertTrue(result["features"]["buy_absorption"])
        self.assertTrue(result["features"]["lower_second_failure"])
        self.assertTrue(result["features"]["bearish_dominance_shift"])
        self.assertEqual(result["features"]["target_source"], "prior_low")
        self.assertEqual(result["features"]["profile_poc"], 100)
        self.assertEqual(result["features"]["cluster_bars"], 3)
        self.assertEqual(result["features"]["profile_end"], "2026-01-01T00:45:00Z")
        self.assertEqual(analyze(list(reversed(setup_short_tape())), config(direction="short"), as_of), result)
        json.dumps(result, allow_nan=False)

    def test_short_profile_node_target_selected_when_qualifying(self):
        result = analyze(setup_short_tape(with_node=True), config(direction="short"),
                         START + timedelta(minutes=75))
        self.assertEqual(result["status"], "signal", result)
        self.assertEqual(result["features"]["target_source"], "prior_profile_node")
        self.assertEqual(result["signal"]["target"], 80.0)

    def test_short_future_trades_cannot_change_past_signal(self):
        as_of = START + timedelta(minutes=75)
        baseline = analyze(setup_short_tape(), config(direction="short"), as_of)
        future = [trade(5, 1, 1, 99999), trade(6, 1, 9999, 999999, "S")]
        self.assertEqual(analyze(setup_short_tape() + future, config(direction="short"), as_of), baseline)

    def test_short_setup_bar_alone_is_insufficient(self):
        tape = [t for t in setup_short_tape() if parse_time(t.timestamp) < START + timedelta(minutes=60)]
        result = analyze(tape, config(direction="short"), START + timedelta(minutes=60))
        self.assertEqual(result["reason"], "insufficient_history")

    def test_bearish_absorption_conditions(self):
        bullish_close = replace_setup(setup_short_tape(), [
            trade(3, 1, 99.5, 1, "S"), trade(3, 2, 101.2, 5),
            trade(3, 3, 98.2, 1, "S"), trade(3, 4, 99.9, 1)])
        self.assertEqual(analyze(bullish_close, config(direction="short"),
                                 START + timedelta(minutes=75))["reason"], "no_bearish_absorption")
        negative_delta = replace_setup(setup_short_tape(), [
            trade(3, 1, 100.2, 1, "S"), trade(3, 2, 101.2, 2),
            trade(3, 3, 99.2, 1, "S"), trade(3, 4, 97.2, 1, "S")])
        self.assertEqual(analyze(negative_delta, config(direction="short"),
                                 START + timedelta(minutes=75))["reason"], "no_bearish_absorption")
        imbalance_in_body = replace_setup(setup_short_tape(), [
            trade(3, 1, 100.2, 2, "S"), trade(3, 2, 101.2, 5),
            trade(3, 3, 98.2, 0.5, "S"), trade(3, 4, 99.2, 5), trade(3, 5, 96.2, 1, "S")])
        self.assertEqual(analyze(imbalance_in_body, config(direction="short"),
                                 START + timedelta(minutes=75))["reason"], "no_bearish_absorption")

    def test_upper_wick_guard_binding_at_row_edge(self):
        tape = [t for t in setup_short_tape() if not t.id.startswith("3-")]
        tape += [trade(3, 1, 100.0, 1, "S"), trade(3, 2, 100.0, 5),
                 trade(3, 3, 99.0, 0.5, "S"), trade(3, 4, 99.9, 1, "S")]
        result = analyze(tape, config(direction="short"), START + timedelta(minutes=75))
        self.assertEqual(result["reason"], "no_bearish_absorption")

    def test_lower_second_failure_conditions(self):
        high_at_setup_high = [dataclasses.replace(t, price=101.9) if t.id == "4-2" else t
                              for t in setup_short_tape()]
        self.assertEqual(analyze(high_at_setup_high, config(direction="short"),
                                 START + timedelta(minutes=75))["reason"], "no_lower_second_failure")
        shallow_retry = [t for t in setup_short_tape() if not t.id.startswith("4-")]
        shallow_retry += [trade(4, 1, 96.8, 1, "S"), trade(4, 2, 96.9, 0.5),
                          trade(4, 3, 95.7, 3, "S"), trade(4, 4, 94.7, 3, "S"),
                          trade(4, 5, 93.7, 3, "S"), trade(4, 6, 92.7, 0.1, "S")]
        self.assertEqual(analyze(shallow_retry, config(direction="short"),
                                 START + timedelta(minutes=75))["reason"], "no_lower_second_failure")

    def test_bearish_dominance_requires_sell_stack(self):
        tape = [t for t in setup_short_tape() if not t.id.startswith("4-")]
        tape += [trade(4, 1, 97.8, 1, "S"), trade(4, 2, 101.1, 0.5),
                 trade(4, 3, 95.7, 5, "S"), trade(4, 4, 92.7, 0.1, "S")]
        result = analyze(tape, config(direction="short"), START + timedelta(minutes=75))
        self.assertEqual(result["reason"], "no_bearish_dominance_shift")

    def test_bearish_dominance_requires_bearish_close(self):
        tape = [t for t in setup_short_tape() if not t.id.startswith("4-")]
        tape += [trade(4, 1, 97.8, 1, "S"), trade(4, 2, 101.1, 0.5),
                 trade(4, 3, 96.7, 0.5), trade(4, 4, 95.5, 0.5), trade(4, 5, 94.5, 0.5),
                 trade(4, 6, 95.7, 3, "S"), trade(4, 7, 94.7, 3, "S"), trade(4, 8, 93.7, 3, "S"),
                 trade(4, 9, 98.0, 0.5)]
        result = analyze(tape, config(direction="short"), START + timedelta(minutes=75))
        self.assertEqual(result["reason"], "no_bearish_dominance_shift")

    def test_sell_stack_boundary_qualifies(self):
        tape = [t for t in setup_short_tape() if not t.id.startswith("4-")]
        tape += [trade(4, 1, 97.8, 1, "S"), trade(4, 2, 98.8, 5),
                 trade(4, 3, 97.0, 0.5), trade(4, 4, 96.7, 0.5), trade(4, 5, 95.5, 0.5),
                 trade(4, 6, 94.5, 0.5), trade(4, 7, 95.7, 3, "S"), trade(4, 8, 94.7, 3, "S"),
                 trade(4, 9, 93.7, 3, "S"), trade(4, 10, 94.0, 0.1, "S")]
        result = analyze(tape, config(direction="short"), START + timedelta(minutes=75))
        self.assertEqual(result["status"], "signal", result)
        self.assertTrue(result["features"]["bearish_dominance_shift"])

    def test_both_mode_selects_qualifying_side(self):
        as_of = START + timedelta(minutes=75)
        long_result = analyze(setup_tape(), config(direction="both"), as_of)
        self.assertEqual(long_result["signal"]["direction"], "long")
        short_result = analyze(setup_short_tape(), config(direction="both"), as_of)
        self.assertEqual(short_result["signal"]["direction"], "short")
        self.assertEqual(short_result["signal"]["entry"], 92.7)
        self.assertIsNone(analyze(setup_tape(), config(direction="short"), as_of)["signal"])

    def test_short_position_size_includes_notional_cap(self):
        capped = short_position_size(92.7, 102.2, 10000, config())
        self.assertAlmostEqual(capped * 92.7 * 0.9995, 100, places=5)
        risk_limited = short_position_size(92.7, 102.2, 10000, config(max_notional=100000))
        modeled_unit_risk = 102.2 * 1.0005 * 1.0025 - 92.7 * 0.9995 * 0.9975
        self.assertAlmostEqual(risk_limited * modeled_unit_risk, 25, places=5)
        self.assertEqual(short_position_size(92.7, 90, 10000, config()), 0)

    def test_short_replay_enters_only_after_signal_and_accounts_for_costs(self):
        tape = setup_short_tape() + [trade(5, 1, 95), trade(5, 2, 79), trade(6, 1, 79.1)]
        result = replay(tape, config(direction="short"))
        self.assertEqual(result["summary"]["trade_count"], 1, result)
        executed = result["trades"][0]
        self.assertEqual(executed["exit_reason"], "target")
        self.assertGreater(parse_time(executed["entry_time"]), parse_time(executed["signal_time"]))
        self.assertAlmostEqual(executed["entry"], 95 * 0.9995)
        self.assertAlmostEqual(executed["exit"], 79.2 * 1.0005)
        expected_pnl = executed["entry_cost"] - executed["quantity"] * executed["exit"] - executed["exit_fee"]
        self.assertAlmostEqual(executed["net_pnl"], expected_pnl)
        self.assertAlmostEqual(result["summary"]["final_equity"], 10000 + expected_pnl)
        json.dumps(result, allow_nan=False)

    def test_short_no_fill_on_signal_close_or_unfinished_next_bar(self):
        tape = setup_short_tape() + [trade(5, 1, 95)]
        result = replay(tape, config(direction="short"))
        self.assertEqual(result["summary"]["signal_count"], 1)
        self.assertEqual(result["trades"], [])

    def test_short_stop_wins_when_both_bounds_touched(self):
        tape = setup_short_tape() + [trade(5, 1, 95), trade(5, 2, 103), trade(5, 3, 78), trade(6, 1, 90)]
        result = replay(tape, config(direction="short"))
        self.assertEqual(result["trades"][0]["exit_reason"], "stop")
        self.assertAlmostEqual(result["trades"][0]["exit"], 102.2 * 1.0005)

    def test_short_stop_gap_fills_at_worse_open(self):
        tape = setup_short_tape() + [trade(5, 1, 95), trade(6, 1, 103), trade(6, 2, 104), trade(7, 1, 104)]
        executed = replay(tape, config(direction="short"))["trades"][0]
        self.assertEqual(executed["exit_reason"], "stop")
        self.assertAlmostEqual(executed["exit"], 103 * 1.0005)

    def test_short_entry_gap_invalidates_reward(self):
        tape = setup_short_tape() + [trade(5, 1, 75), trade(5, 2, 75.5), trade(6, 1, 76)]
        result = replay(tape, config(direction="short"))
        self.assertEqual(result["trades"], [])
        self.assertEqual(result["summary"]["skipped_entries"], 1)

    def test_short_daily_loss_limit_caps_entry_risk(self):
        tape = setup_short_tape() + [trade(5, 1, 95), trade(5, 2, 103), trade(6, 1, 105)]
        result = replay(tape, config(direction="short", max_notional=100000, risk_fraction=0.5,
                                     daily_loss_fraction=0.001))
        self.assertEqual(len(result["trades"]), 1)
        self.assertLess(abs(result["summary"]["net_pnl"] + 10), 0.02)

    def test_short_replay_max_holding_closes_position(self):
        tape = setup_short_tape() + [trade(5, 1, 95), trade(5, 2, 94), trade(6, 1, 94.5)]
        executed = replay(tape, config(direction="short", max_holding_bars=1))["trades"][0]
        self.assertEqual(executed["exit_reason"], "max_holding")
        self.assertAlmostEqual(executed["exit"], 94 * 1.0005)

    def test_short_end_of_data_buyback_includes_fee(self):
        tape = setup_short_tape() + [trade(5, 1, 95), trade(5, 2, 94.5), trade(6, 1, 94.5)]
        result = replay(tape, config(direction="short"))
        self.assertEqual(result["trades"][0]["exit_reason"], "end_of_data")
        self.assertGreater(result["trades"][0]["exit_fee"], 0)

    def test_replay_rejects_both_direction(self):
        with self.assertRaisesRegex(ValueError, "one direction"):
            replay(setup_short_tape(), config(direction="both"))

    def test_flip_signal_mirrors_geometry(self):
        flipped = _flip_signal({"id": "x", "entry": 100.0, "stop": 99.0, "target": 103.0,
                                "direction": "long"})
        self.assertEqual(flipped["stop"], 101.0)
        self.assertEqual(flipped["target"], 97.0)
        self.assertEqual(flipped["direction"], "short")
        self.assertEqual(flipped["id"], "x-flip")
        back = _flip_signal(flipped)
        self.assertEqual(back["stop"], 99.0)
        self.assertEqual(back["target"], 103.0)
        self.assertEqual(back["direction"], "long")

    def test_flip_replays_long_signals_as_shorts(self):
        tape = setup_tape() + [trade(5, 1, 105), trade(5, 2, 90), trade(6, 1, 90.5)]
        result = replay(tape, config(flip=True))
        self.assertEqual(result["summary"]["trade_count"], 1, result)
        executed = result["trades"][0]
        self.assertEqual(executed["exit_reason"], "target")
        self.assertAlmostEqual(executed["entry"], 105 * 0.9995)
        self.assertAlmostEqual(executed["exit"], 90.2 * 1.0005)
        expected_pnl = executed["entry_cost"] - executed["quantity"] * executed["exit"] - executed["exit_fee"]
        self.assertAlmostEqual(executed["net_pnl"], expected_pnl)

    def test_flip_replays_short_signals_as_longs(self):
        tape = setup_short_tape() + [trade(5, 1, 92), trade(5, 2, 107), trade(5, 3, 96), trade(6, 1, 96)]
        result = replay(tape, config(direction="short", flip=True))
        self.assertEqual(result["summary"]["trade_count"], 1, result)
        executed = result["trades"][0]
        self.assertEqual(executed["exit_reason"], "target")
        self.assertAlmostEqual(executed["entry"], 92 * 1.0005)
        self.assertAlmostEqual(executed["exit"], 106.2 * 0.9995)

    def test_no_flip_short_signal_without_flip_behaves_as_before(self):
        tape = setup_short_tape() + [trade(5, 1, 95), trade(5, 2, 79), trade(6, 1, 79.1)]
        flipped = replay(tape, config(direction="short", flip=True))
        direct = replay(tape, config(direction="short"))
        self.assertEqual(flipped["summary"]["signal_count"], direct["summary"]["signal_count"])
        self.assertEqual(flipped["trades"], [])


if __name__ == "__main__":
    unittest.main()
