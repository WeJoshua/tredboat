from contextlib import nullcontext
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tred.cli import main
from tred.equities import (EquityModel, compare, load_bars, read_manifest, replay_bars)
from tred.futures import MAX_RECORDS
from tred.strategy import Bar, StrategyConfig

UTC = timezone.utc
START = datetime(2026, 1, 5, 14, 15, tzinfo=UTC)  # 09:15 New York, winter Monday.


class EquityTapeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manifest_path = self.root / "coverage.json"
        self.manifest = {"schema": "tred.equity-tape.v1", "source": "synthetic unit test",
                         "format": "canonical", "symbol": "QQQ", "price_format": "decimal",
                         "side_inference": {"rule": "tick_prior_price", "note": "unit test"},
                         "complete": True, "start": START.isoformat(),
                         "end": (START + timedelta(hours=1)).isoformat(), "trades_file": "tape.csv"}
        self.config = StrategyConfig(symbol="QQQ", row_size=.01, profile_bars=3)

    def write(self, rows, fields=None):
        self.manifest_path.write_text(json.dumps(self.manifest))
        with (self.root / "tape.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields or list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def row(self, offset=0, **changes):
        return {"timestamp": (START + timedelta(seconds=offset)).isoformat(),
                "trade_id": str(offset), "symbol": "QQQ", "price": "511.23",
                "size": "100", "side": "B", **changes}

    def test_tape_requires_the_declared_side_inference(self):
        self.write([self.row()])
        bars, info, _ = load_bars(self.manifest_path, self.config)
        self.assertEqual(info["records"], 1)
        del self.manifest["side_inference"]
        self.write([self.row()])
        with self.assertRaisesRegex(ValueError, "side_inference"):
            read_manifest(self.manifest_path)
        self.manifest["side_inference"] = {"rule": "magic", "note": "x"}
        self.write([self.row()])
        with self.assertRaisesRegex(ValueError, "side_inference"):
            load_bars(self.manifest_path, self.config)

    def test_prices_whole_shares_and_sides_fail(self):
        for changes in ({"price": "NaN"}, {"price": "bad"}, {"price": "0"},
                        {"size": "1.5"}, {"side": "N"}, {"symbol": "SPY"}):
            with self.subTest(changes=changes):
                self.write([self.row(**changes)])
                with self.assertRaises(ValueError):
                    load_bars(self.manifest_path, self.config)
        self.write([self.row(price="511.231")])  # real IEX prints carry sub-cent prices
        bars, _, _ = load_bars(self.manifest_path, self.config)
        self.assertEqual(bars[0].open, 511.231)

    def test_duplicates_unsorted_and_coverage_breaks_fail(self):
        for rows in ([self.row(), self.row()], [self.row(2), self.row(1)],
                     [self.row(18000)]):
            with self.subTest():
                self.write(rows)
                with self.assertRaises(ValueError):
                    load_bars(self.manifest_path, self.config)

    def test_closure_and_budget_respect(self):
        self.manifest["closed_intervals"] = [[START.isoformat(), (START + timedelta(seconds=1)).isoformat()]]
        self.write([self.row()])
        with self.assertRaisesRegex(ValueError, "closure"):
            load_bars(self.manifest_path, self.config)
        self.manifest.pop("closed_intervals")
        self.write([self.row(), self.row(1)])
        with patch("tred.equities.MAX_RECORDS", 1) as _, self.assertRaisesRegex(ValueError, "million"):
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
                                     price=f"{round(item.price * 4) / 4:.2f}", size=str(size), side=item.side))
        rows += [self.row(trade_id="entry", timestamp=(START + timedelta(minutes=25, seconds=1)).isoformat(), price="105.25", size="1"),
                 self.row(trade_id="target", timestamp=(START + timedelta(minutes=25, seconds=2)).isoformat(), price="121.25", size="1")]
        self.write(rows)

    def test_shared_entry_engine_comparses_four_equity_variants(self):
        self.synthetic_pattern()
        report = compare(self.manifest_path, EquityModel("QQQ"), profile_bars=3, row_ticks=100)
        self.assertEqual(len(report["results"]), 4)
        long = report["results"][0]
        self.assertEqual(long["summary"]["trade_count"], 1, long["summary"])
        self.assertGreaterEqual(long["trades"][0]["quantity"], 1)
        self.assertGreater(long["summary"]["net_pnl"], 0)
        flipped = report["results"][2]["trades"]
        if flipped:
            self.assertEqual(flipped[0]["direction"], "short")
        self.assertIn("tick-rule inferred", report["performance_conclusion"])

    def test_cli_publishes_report_without_broker_or_crypto_database(self):
        self.synthetic_pattern()
        output = self.root / "equity-report.json"
        args = ["equity-compare", "--manifest", str(self.manifest_path), "--profile-bars", "3",
                "--row-ticks", "100", "--output", str(output)]
        with patch("tred.research.research_limits", return_value=nullcontext()), \
                patch("tred.cli.load_env", side_effect=AssertionError("credentials read")), \
                patch("tred.cli.Store", side_effect=AssertionError("crypto database opened")), \
                patch("tred.cli.emit"):
            self.assertEqual(main(args), 0)
        before = output.read_bytes()
        self.assertEqual(json.loads(before)["schema"], "tred.equity-comparison.v1")
        with patch("tred.research.research_limits", return_value=nullcontext()), patch("tred.cli.emit"):
            self.assertEqual(main(args), 2)
        self.assertEqual(output.read_bytes(), before)

    def test_model_validation_and_symbol_matching(self):
        with self.assertRaises(ValueError):
            EquityModel("qqq")
        model = EquityModel("QQQ", commission_fixed_usd=1.0, commission_bps=2.0)
        costs = model.costs(500, 495, 515)
        self.assertGreater(costs["risk_per_unit"], 5)
        self.assertGreater(costs["reward_per_unit"], 12)
        self.assertGreater(costs["net_reward_risk"], 1)

    def test_replay_sessions_honor_the_window(self):
        start = START + timedelta(minutes=15)  # 09:30
        bars = []
        for i in range(20):
            stamp = start + timedelta(minutes=i * 5)
            bars.append(Bar(stamp, stamp + timedelta(minutes=5), stamp + timedelta(seconds=1),
                            stamp + timedelta(seconds=4), 500, 501, 499, 500, 20,
                            {"buy_volume": 10000, "sell_volume": 10000}))
        def analyze(bars, config, as_of, **kwargs):
            if as_of == start + timedelta(minutes=10):
                return {"reason": "fixture", "signal": {"id": "s", "entry": 500, "direction": "long",
                        "stop": 498, "target": 510}}
            return {"reason": "fixture", "signal": None}
        with patch("tred.equities._analyze_bars", side_effect=analyze):
            report = replay_bars(bars, StrategyConfig(symbol="QQQ", profile_bars=1), EquityModel("QQQ"))
        self.assertEqual(report["summary"]["trade_count"], 1)
        self.assertEqual(report["trades"][0]["exit_reason"], "session_cutoff")
        self.assertEqual(report["trades"][0]["exit_time"], "2026-01-05T16:00:00Z")


if __name__ == "__main__":
    unittest.main()