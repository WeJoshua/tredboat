from contextlib import nullcontext
import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tred.cli import main
from tred.futures import (FuturesModel, compare, continuity, in_window, load_bars,
                          read_manifest, replay_bars, timestamp)
from tred.strategy import Bar, StrategyConfig

UTC = timezone.utc
START = datetime(2026, 1, 5, 14, 15, tzinfo=UTC)  # 09:15 New York, winter.


class FuturesTapeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manifest_path = self.root / "coverage.json"
        self.manifest = {"schema": "tred.futures-tape.v1", "source": "synthetic unit test",
                         "format": "canonical", "symbol": "MNQH6", "complete": True,
                         "start": START.isoformat(), "end": (START + timedelta(hours=1)).isoformat(),
                         "trades_file": "tape.csv"}
        self.config = StrategyConfig(symbol="MNQH6", row_size=.25, profile_bars=3)

    def write(self, rows, fields=None):
        self.manifest_path.write_text(json.dumps(self.manifest))
        with (self.root / "tape.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields or list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def row(self, offset=0, **changes):
        return {"timestamp": (START + timedelta(seconds=offset)).isoformat(),
                "trade_id": str(offset), "symbol": "MNQH6", "price": "20000.25",
                "size": "2", "side": "B", **changes}

    def test_signed_rows_build_real_contract_volume_and_delta(self):
        self.write([self.row(), self.row(1, price="20000.00", size="5", side="S"),
                    self.row(2, price="20000.50", size="3")])
        bars, info, _ = load_bars(self.manifest_path, self.config)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].open, 20000.25)
        self.assertEqual(bars[0].close, 20000.5)
        self.assertEqual(bars[0].footprint["buy_volume"], 5)
        self.assertEqual(bars[0].footprint["sell_volume"], 5)
        self.assertEqual(bars[0].footprint["delta"], 0)
        self.assertEqual(info["records"], 3)

    def test_bad_tick_fractional_volume_unknown_side_and_wrong_contract_fail(self):
        for changes in ({"price": "20000.10"}, {"size": ".5"}, {"side": "N"},
                        {"symbol": "NQH6"}, {"price": "NaN"}, {"size": "Infinity"}, {"price": "bad"}):
            with self.subTest(changes=changes):
                self.write([self.row(**changes)])
                with self.assertRaises(ValueError):
                    load_bars(self.manifest_path, self.config)

    def test_duplicates_and_unsorted_tape_fail(self):
        for rows in ([self.row(), self.row()], [self.row(2), self.row(1)]):
            self.write(rows)
            with self.assertRaises(ValueError):
                load_bars(self.manifest_path, self.config)

    def test_unknown_or_continuous_contract_manifest_fails(self):
        for symbol in ("MNQ", "MNQ.c.0", "MNQH6-MNQM6", "BTC/USD"):
            self.manifest["symbol"] = symbol
            self.write([self.row()])
            with self.assertRaises(ValueError):
                read_manifest(self.manifest_path)

    def test_incomplete_manifest_and_trade_inside_declared_closure_fail(self):
        self.manifest["complete"] = False
        self.write([self.row()])
        with self.assertRaises(ValueError):
            load_bars(self.manifest_path, self.config)
        self.manifest["complete"] = True
        self.manifest["closed_intervals"] = [[START.isoformat(), (START + timedelta(minutes=5)).isoformat()]]
        self.write([self.row()])
        with self.assertRaisesRegex(ValueError, "exchange closure"):
            load_bars(self.manifest_path, self.config)

    def test_partial_first_and_last_bars_excluded(self):
        self.manifest.update(start=(START + timedelta(seconds=1)).isoformat(),
                             end=(START + timedelta(minutes=12)).isoformat())
        self.write([self.row(2), self.row(301), self.row(601)])
        bars, _, _ = load_bars(self.manifest_path, self.config)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].start, START + timedelta(minutes=5))

    def test_nanosecond_order_not_lost(self):
        prefix = START.strftime("%Y-%m-%dT%H:%M:%S")
        self.write([self.row(timestamp=prefix + ".000000002Z"),
                    self.row(1, timestamp=prefix + ".000000001Z")])
        with self.assertRaisesRegex(ValueError, "sorted"):
            load_bars(self.manifest_path, self.config)
        self.assertEqual(timestamp("1000000001")[1], 1000000001)

    def test_databento_aggressor_mapping_and_fixed_point_prices(self):
        self.manifest.update(format="databento", price_format="fixed9", instrument_id=123)
        rows = [{"ts_event": START.isoformat(), "ts_recv": START.isoformat(), "instrument_id": "123",
                 "sequence": "10", "action": "T", "rtype": "0", "side": "B", "price": "20000250000000", "size": "3"},
                {"ts_event": (START + timedelta(seconds=1)).isoformat(), "ts_recv": (START + timedelta(seconds=1)).isoformat(),
                 "instrument_id": "123", "sequence": "11", "action": "T", "rtype": "0", "side": "A", "price": "20000000000000", "size": "7"}]
        self.write(rows)
        bars, _, _ = load_bars(self.manifest_path, self.config)
        self.assertEqual(bars[0].footprint["delta"], -4)
        self.assertEqual(bars[0].open, 20000.25)
        rows[1]["side"] = "N"
        self.write(rows)
        with self.assertRaisesRegex(ValueError, "aggressor"):
            load_bars(self.manifest_path, self.config)

    def test_reader_budget_rejects_instead_of_truncating(self):
        self.write([self.row(), self.row(1)])
        with patch("tred.futures.MAX_RECORDS", 1), self.assertRaisesRegex(ValueError, "million"):
            load_bars(self.manifest_path, self.config)

    def synthetic_pattern(self):
        from tests.test_strategy import setup_tape
        rows = []
        for item in setup_tape():
            bar, offset = map(int, item.id.split("-"))
            size = max(1, int(item.size))
            if bar == 4 and size == 3:
                size = 5
            for repeat in range(20):
                stamp = START + timedelta(minutes=5 * bar, seconds=offset, microseconds=repeat)
                rows.append(self.row(trade_id=f"{item.id}-{repeat}", timestamp=stamp.isoformat(),
                                     price=str(round(item.price * 4) / 4), size=str(size), side=item.side))
        rows += [self.row(trade_id="entry", timestamp=(START + timedelta(minutes=25, seconds=1)).isoformat(), price="105.25", size="1"),
                 self.row(trade_id="target", timestamp=(START + timedelta(minutes=25, seconds=2)).isoformat(), price="121.25", size="1")]
        self.write(rows)

    def test_real_entry_engine_is_shared_with_four_way_futures_comparison(self):
        self.synthetic_pattern()
        report = compare(self.manifest_path, FuturesModel("MNQ", .1, 1000), profile_bars=3, row_ticks=4)
        self.assertEqual(len(report["results"]), 4)
        long = report["results"][0]
        self.assertEqual(long["summary"]["trade_count"], 1, long["summary"])
        self.assertGreater(long["summary"]["net_pnl"], 0)
        self.assertEqual(long["trades"][0]["quantity"], 1)
        flipped = report["results"][2]
        self.assertEqual(flipped["trades"][0]["direction"], "short")
        self.assertLess(flipped["summary"]["net_pnl"], 0)

    def test_cli_publishes_complete_report_without_broker_or_crypto_database(self):
        self.synthetic_pattern()
        output = self.root / "report.json"
        args = ["futures-compare", "--manifest", str(self.manifest_path), "--commission-per-side", ".1",
                "--margin-per-contract", "1000", "--minimum-bar-contracts", "0", "--profile-bars", "3",
                "--row-ticks", "4", "--output", str(output)]
        with patch("tred.research.research_limits", return_value=nullcontext()), \
                patch("tred.cli.load_env", side_effect=AssertionError("credentials read")), \
                patch("tred.cli.Store", side_effect=AssertionError("crypto database opened")), \
                patch("tred.cli.emit"):
            self.assertEqual(main(args), 0)
        before = output.read_bytes()
        self.assertEqual(json.loads(before)["schema"], "tred.futures-comparison.v1")
        with patch("tred.research.research_limits", return_value=nullcontext()), patch("tred.cli.emit"):
            self.assertEqual(main(args), 2)
        self.assertEqual(output.read_bytes(), before)


class FuturesExecutionModelTests(unittest.TestCase):
    def bars(self, prices=None, start=None):
        start = start or START
        result = []
        for i in range(8):
            stamp = start + timedelta(minutes=i * 5)
            o, h, low, c = (prices or {}).get(i, (20000, 20001, 19999, 20000))
            result.append(Bar(stamp, stamp + timedelta(minutes=5), stamp + timedelta(seconds=1),
                              stamp + timedelta(seconds=4), o, h, low, c, 20,
                              {"buy_volume": 10000, "sell_volume": 10000}))
        return result

    def run_case(self, direction="long", prices=None, model=None, every=False, start=None):
        start = start or START
        model = model or FuturesModel("MNQ", .5, 1000)
        strategy = StrategyConfig(symbol=model.root + "H6", direction=direction, profile_bars=1)
        def analyze(bars, config, as_of, **kwargs):
            signal = None
            if every or as_of == start + timedelta(minutes=25):
                signal = {"id": as_of.isoformat(), "entry": 20000, "direction": direction,
                          "stop": 19998 if direction == "long" else 20002,
                          "target": 20010 if direction == "long" else 19990}
            return {"reason": "fixture", "signal": signal}
        with patch("tred.futures._analyze_bars", side_effect=analyze):
            return replay_bars(self.bars(prices, start), strategy, model)

    def test_cme_contract_values_and_symmetric_costs(self):
        for root, value in (("MNQ", 2), ("NQ", 20)):
            model = FuturesModel(root, 1, 1000)
            self.assertEqual(model.point_value, value)
            long = model.costs(20000, 19990, 20020)
            short = model.costs(20000, 20010, 19980)
            self.assertAlmostEqual(long["risk_per_unit"], 10.5 * value + 2)
            self.assertAlmostEqual(long["reward_per_unit"], 19.5 * value - 2)
            self.assertEqual(long["net_reward_risk"], short["net_reward_risk"])

    def test_long_and_short_target_pnl_includes_both_fees_and_slippage(self):
        for direction, prices in (("long", {5: (20000, 20011, 19999, 20010)}),
                                  ("short", {5: (20000, 20001, 19989, 19990)})):
            report = self.run_case(direction, prices)
            self.assertEqual(report["summary"]["trade_count"], 1)
            self.assertAlmostEqual(report["summary"]["net_pnl"], 18)
            self.assertEqual(report["summary"]["fees"], 1)
            self.assertEqual(report["trades"][0]["exit_reason"], "target")

    def test_stop_first_if_stop_and_target_both_touched(self):
        for direction in ("long", "short"):
            report = self.run_case(direction, {5: (20000, 20011, 19989, 20000)})
            self.assertEqual(report["trades"][0]["exit_reason"], "stop")
            self.assertAlmostEqual(report["summary"]["net_pnl"], -6)

    def test_short_adverse_gap_buys_back_at_higher_open(self):
        report = self.run_case("short", {6: (20010, 20011, 20009, 20010)})
        self.assertEqual(report["trades"][0]["exit"], 20010.25)
        self.assertAlmostEqual(report["summary"]["net_pnl"], -22)

    def test_whole_contract_margin_and_risk_do_not_round_up(self):
        report = self.run_case(model=FuturesModel("MNQ", .5, 100000))
        self.assertEqual(report["summary"]["trade_count"], 0)
        self.assertEqual(report["summary"]["skipped_entries"]["whole_contract_risk_or_margin"], 1)
        report = self.run_case(model=FuturesModel("NQ", .5, 1000))
        self.assertEqual(report["summary"]["trade_count"], 0)

    def test_two_losses_stop_both_directions(self):
        prices = {i: (20000, 20003, 19997, 20000) for i in range(8)}
        for direction in ("long", "short"):
            report = self.run_case(direction, prices, every=True)
            self.assertEqual(report["summary"]["trade_count"], 2)

    def test_mnq_contract_volume_filter_rejects_below_twenty_thousand(self):
        model = FuturesModel("MNQ", .5, 1000, minimum_bar_contracts=20001)
        report = self.run_case(model=model)
        self.assertEqual(report["summary"]["signal_count"], 0)
        self.assertGreater(report["summary"]["reason_counts"]["insufficient_contract_volume"], 0)

    def test_missing_data_while_open_does_not_invent_exit(self):
        strategy = StrategyConfig(symbol="MNQH6", profile_bars=1)
        bars = self.bars()
        bars.pop(6)
        def signal(bars, config, as_of, **kwargs):
            return {"reason": "fixture", "signal": {"id": "s", "entry": 20000, "direction": "long",
                    "stop": 19998, "target": 20010} if as_of == START + timedelta(minutes=25) else None}
        with patch("tred.futures._analyze_bars", side_effect=signal), self.assertRaisesRegex(ValueError, "Missing bar"):
            replay_bars(bars, strategy, FuturesModel("MNQ", .5, 1000))

    def test_nq_cannot_be_reported_with_mnq_dollar_multiplier(self):
        with self.assertRaisesRegex(ValueError, "same futures root"):
            replay_bars(self.bars(), StrategyConfig(symbol="NQH6"), FuturesModel("MNQ", 1, 1000))

    def test_new_york_window_handles_dst_and_excludes_weekends(self):
        for stamp in ("2026-01-05T14:30:00Z", "2026-07-06T13:30:00Z"):
            self.assertTrue(in_window(datetime.fromisoformat(stamp.replace("Z", "+00:00"))))
        for stamp in ("2026-01-05T16:00:00Z", "2026-07-06T15:00:00Z", "2026-07-05T13:30:00Z"):
            self.assertFalse(in_window(datetime.fromisoformat(stamp.replace("Z", "+00:00"))))

    def test_session_cutoff_closes_at_eleven(self):
        start = datetime(2026, 1, 5, 15, 30, tzinfo=UTC)  # 10:30 local.
        report = self.run_case(start=start)
        self.assertEqual(report["trades"][0]["exit_reason"], "session_cutoff")
        self.assertEqual(report["trades"][0]["exit_time"], "2026-01-05T16:00:00Z")

    def test_only_explicit_exchange_closures_bridge_profile_gaps(self):
        end = START + timedelta(hours=1)
        linked = continuity([(START, end)])
        self.assertTrue(linked(START, end))
        self.assertTrue(linked(end, end))
        self.assertFalse(linked(START - timedelta(minutes=5), end))
        self.assertFalse(linked(START, end + timedelta(minutes=5)))


if __name__ == "__main__":
    unittest.main()
