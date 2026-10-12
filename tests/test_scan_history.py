from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tred.cli import main
from tred.scan_history import scan
from tred.store import Store
from tred.strategy import StrategyConfig

UTC = timezone.utc
START = datetime(2026, 10, 1, 13, 30, tzinfo=UTC)


def tape_rows(hours=36):
    rows = []
    minutes = 0
    while minutes < hours * 12:
        if 60 <= minutes < 65:
            price = 29900.0  # a dip that reaches the fixtures' stops
        else:
            price = 30000.0 + (minutes % 3) * 5
        rows.append((price, (START + timedelta(minutes=5 * minutes)).timestamp()))
        minutes += 1
    return rows


def seed_store(path, rows):
    from tred.strategy import Trade
    store = Store(str(path))
    store.add_trades("us-1", "BTC/USD", [
        Trade(f"t{epoch}", datetime.fromtimestamp(epoch, UTC).isoformat(), price, 1.0, "B")
        for price, epoch in rows
    ])
    return store


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = seed_store(Path(self.directory.name) / "scan.sqlite3", tape_rows())
        self.addCleanup(self.store.close)

    def scan(self, **changes):
        arguments = {"eval_seconds": 6 * 3600, "warmup_seconds": 24 * 3600, "max_losses": 100}
        arguments.update(changes)
        return scan(self.store, StrategyConfig(symbol="BTC/USD", profile_bars=3, bar_seconds=300,
                                               taker_fee=0, slippage_bps=0),
                    START, START + timedelta(hours=12), feed="us-1", symbol="BTC/USD", **arguments)

    def falling(self, bars, config, as_of, **kwargs):
        if as_of == START + timedelta(minutes=20):
            return {"reason": "fixture", "features": {"setup_delta": 1.5, "zone_low": 29900, "zone_high": 30100},
                    "signal": {"id": "s1", "entry": 30005, "direction": "long", "stop": 29980, "target": 30150}}
        return {"reason": "fixture", "features": {}, "signal": None}

    def test_scan_logs_only_losing_trades_with_features(self):
        loss_file = Path(self.directory.name) / "losses.jsonl"
        with patch("tred.strategy._analyze_bars", side_effect=self.falling):
            result = self.scan(loss_path=str(loss_file))
        self.assertGreater(result["trade_count"], 0)
        self.assertGreater(result["loss_count"], 0)
        lines = [json.loads(line) for line in loss_file.read_text().splitlines() if line.strip()]
        self.assertTrue(lines)
        for line in lines:
            self.assertLess(line["net_pnl"], 0)
            self.assertEqual(line["direction"], "long")
            self.assertEqual(line["features"]["setup_delta"], 1.5)
        self.assertEqual(result["losses_logged"], result["loss_count"])
        self.assertEqual(result["losses_truncated"], 0)

    def test_warmup_duplicates_are_counted_once(self):
        def signal(bars, config, as_of, **kwargs):
            if as_of in {START + timedelta(minutes=30), START + timedelta(minutes=325)}:
                return {"reason": "fixture", "features": {},
                        "signal": {"id": str(as_of), "entry": 30005, "direction": "long", "stop": 29990, "target": 30080}}
            return {"reason": "fixture", "features": {}, "signal": None}

        with patch("tred.strategy._analyze_bars", side_effect=signal):
            result = self.scan(eval_seconds=6 * 3600, warmup_seconds=6 * 3600,
                               loss_path=None, max_losses=0)
        self.assertEqual(result["trade_count"], 2)
        self.assertEqual(result["chunks"], 2)
        self.assertEqual(result["trade_count"], result["loss_count"] + result["win_count"] + result["breakeven_count"])

    def test_loss_cap_is_explicit_not_silent(self):
        loss_file = Path(self.directory.name) / "capped.jsonl"
        with patch("tred.strategy._analyze_bars", side_effect=self.falling):
            result = self.scan(loss_path=str(loss_file), max_losses=0)
        self.assertEqual(result["losses_logged"], 0)
        self.assertEqual(result["losses_truncated"], result["loss_count"])

    def test_limit_execution_fills_at_level_without_entry_fee(self):
        def signal(bars, config, as_of, **kwargs):
            if as_of == START + timedelta(minutes=20):
                return {"reason": "fixture", "features": {"setup_delta": -2.0},
                        "signal": {"id": "s", "entry": 30005, "direction": "long", "stop": 29980, "target": 30150}}
            return {"reason": "fixture", "features": {}, "signal": None}

        with patch("tred.strategy._analyze_bars", side_effect=signal):
            strategy = StrategyConfig(symbol="BTC/USD", profile_bars=3, bar_seconds=300,
                                      taker_fee=0.0025, slippage_bps=5, execution="limit", max_notional=2000)
            result = scan(self.store, strategy, START, START + timedelta(hours=12),
                          feed="us-1", symbol="BTC/USD", eval_seconds=6 * 3600, warmup_seconds=24 * 3600,
                          loss_path=str(Path(self.directory.name) / "losses.jsonl"),
                          wins_path=str(Path(self.directory.name) / "wins.jsonl"))
        self.assertEqual(result["execution"], "limit")
        self.assertEqual(result["max_notional"], 2000)
        self.assertEqual(result["loss_count"], 1)  # maker entry, market stop exit
        loss = json.loads(Path(self.directory.name, "losses.jsonl").read_text().splitlines()[0])
        self.assertEqual(loss["entry"], 30005.0)
        self.assertEqual(loss["entry_fee"], 0.0)
        self.assertEqual(loss["exit_reason"], "stop")
        self.assertGreater(loss["exit_fee"], 0.0)
        self.assertLess(loss["net_pnl"], 0)
        self.assertEqual((Path(self.directory.name) / "wins.jsonl").read_text().strip(), "")

    def test_limit_targets_exit_without_fee_and_are_logged_as_wins(self):
        def signal(bars, config, as_of, **kwargs):
            if as_of == START + timedelta(minutes=20):
                return {"reason": "fixture", "features": {},
                        "signal": {"id": "s", "entry": 30004, "direction": "long", "stop": 29980, "target": 30150}}
            return {"reason": "fixture", "features": {}, "signal": None}

        spike = [(30160.0 if 50 <= minute <= 52 else 30000.0 + minute % 5,
                  (START + timedelta(minutes=5 * minute)).timestamp()) for minute in range(60)]
        store = seed_store(Path(self.directory.name) / "spike.sqlite3", spike)
        wins_file = Path(self.directory.name) / "wins.jsonl"
        with patch("tred.strategy._analyze_bars", side_effect=signal):
            strategy = StrategyConfig(symbol="BTC/USD", profile_bars=3, bar_seconds=300,
                                      execution="limit", taker_fee=0.0025, slippage_bps=5)
            result = scan(store, strategy, START, START + timedelta(hours=6),
                          feed="us-1", symbol="BTC/USD", eval_seconds=6 * 3600, warmup_seconds=24 * 3600,
                          loss_path=str(Path(self.directory.name) / "losses.jsonl"), wins_path=str(wins_file))
        self.assertEqual(result["win_count"], 1)
        self.assertEqual(result["loss_count"], 0)
        win = json.loads(wins_file.read_text().splitlines()[0])
        self.assertEqual(win["entry"], 30004.0)
        self.assertEqual(win["exit_reason"], "target")
        self.assertEqual(win["entry_fee"], 0.0)
        self.assertEqual(win["exit_fee"], 0.0)
        self.assertGreater(win["net_pnl"], 0)
        store.close()

    def test_limit_entries_require_intrabar_coverage(self):
        def gap(bars, config, as_of, **kwargs):
            if as_of == START + timedelta(minutes=20):
                return {"reason": "fixture", "features": {},
                        "signal": {"id": "s", "entry": 31000.0, "direction": "long", "stop": 30900, "target": 31500}}
            return {"reason": "fixture", "features": {}, "signal": None}

        with patch("tred.strategy._analyze_bars", side_effect=gap):
            strategy = StrategyConfig(symbol="BTC/USD", profile_bars=3, bar_seconds=300,
                                      taker_fee=0.0025, slippage_bps=5, execution="limit")
            result = scan(self.store, strategy, START, START + timedelta(hours=6),
                          feed="us-1", symbol="BTC/USD", eval_seconds=6 * 3600,
                          warmup_seconds=24 * 3600, loss_path=None,
                          wins_path=None, max_losses=0)
        self.assertEqual(result["trade_count"], 0)  # price never trades through 31000
        self.assertGreater(result["limit_unfilled"], 0)

    def test_replay_trades_carry_features_and_r_multiples(self):
        from tred.strategy import replay
        with patch("tred.strategy._analyze_bars", side_effect=self.falling):
            report = replay(self.store.trades("us-1", "BTC/USD", START, START + timedelta(hours=6), max_trades=100000),
                            StrategyConfig(symbol="BTC/USD", profile_bars=3, bar_seconds=300,
                                           taker_fee=0, slippage_bps=0))
        self.assertEqual(report["summary"]["trade_count"], 1)
        trade = report["trades"][0]
        self.assertEqual(trade["features"], {"setup_delta": 1.5, "zone_low": 29900, "zone_high": 30100})
        self.assertGreater(trade["planned_risk_usd"], 0)
        self.assertIsNotNone(trade["net_r"])
        self.assertIsNotNone(trade["exit_reason"])


class ScanCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        seed_store(self.root / "scan.sqlite3", tape_rows(hours=40))
        config = Path("/home/weanswer-itool/tred/config.toml").read_text().replace(
            "database = \"data/tred.sqlite3\"", f"database = \"{self.root}/scan.sqlite3\"")
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(config)

    def test_cli_runs_both_directions_and_publishes_report(self):
        emitted = {}
        args = ["--config", str(self.config_path), "backtest-scan", "--start", "2026-10-01T13:30:00Z",
                "--end", "2026-10-01T19:30:00Z", "--direction", "all",
                "--execution", "limit", "--max-notional", "2000",
                "--losses", str(self.root / "losses.jsonl"), "--wins", str(self.root / "wins.jsonl"),
                "--report", str(self.root / "scan.json")]
        with patch("tred.research.scan_limits", return_value=nullcontext()), \
                patch("tred.cli.emit", side_effect=lambda value: emitted.update(value)):
            self.assertEqual(main(args), 0)
        self.assertEqual(set(emitted["directions"]), {"long", "short"})
        report = json.loads((self.root / "scan.json").read_text())
        self.assertEqual(report["schema"], "tred.scan.v1")
        self.assertEqual(report["directions"]["long"]["execution"], "limit")
        self.assertEqual(report["directions"]["long"]["max_notional"], 2000)
        self.assertTrue((self.root / "losses-long.jsonl").exists())
        self.assertTrue((self.root / "losses-short.jsonl").exists())
        self.assertTrue((self.root / "wins-long.jsonl").exists())
        self.assertFalse((self.root / "losses.jsonl").exists())


if __name__ == "__main__":
    unittest.main()