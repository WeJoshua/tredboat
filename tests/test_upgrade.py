import dataclasses
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

from tred.cli import main, replay_input, settings
from tred.discipline import apply_halt_request, day_state, record_outcome, request_halt
from tred.execution import PaperExecutor
from tred.store import Store
from tred.strategy import StrategyConfig, Trade, replay
from tests.test_execution import Broker

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


class DisciplineTests(unittest.TestCase):
    def test_two_losses_halt_until_next_utc_day(self):
        state = {}
        record_outcome(state, -1, NOW)
        self.assertIsNone(state["halted_reason"])
        record_outcome(state, 0, NOW)
        record_outcome(state, -1, NOW)
        self.assertEqual(state["halted_reason"], "consecutive_loss_limit")
        record_outcome(state, 10, NOW)
        self.assertEqual(state["halted_reason"], "consecutive_loss_limit")
        day_state(state, NOW + timedelta(days=1))
        self.assertEqual(state["consecutive_losses"], 0)
        self.assertIsNone(state["halted_reason"])

    def test_win_resets_streak_before_lockout(self):
        state = {}
        for pnl in (-1, 2, -1):
            record_outcome(state, pnl, NOW)
        self.assertEqual(state["consecutive_losses"], 1)
        self.assertIsNone(state["halted_reason"])

    def test_manual_mistake_halt_expires_next_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            request_halt(path, "premature entry", NOW)
            state = {}
            apply_halt_request(state, path, NOW)
            self.assertEqual(state["halted_reason"], "execution_mistake")
            apply_halt_request(state, path, NOW + timedelta(days=1))
            self.assertIsNone(state["halted_reason"])
            self.assertFalse(path.exists())

    def test_invalid_outcome_rejected(self):
        for pnl in ("NaN", "Infinity"):
            with self.assertRaises(ValueError):
                record_outcome({}, pnl, NOW)


class PaperDisciplineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        self.broker = Broker()
        self.executor = self.restart()
        self.signal = {"id": "s1", "symbol": "BTC/USD", "direction": "long",
                       "entry": 100000, "stop": 99000, "target": 103000,
                       "expected_cost_fraction": .005}
        self.quote = {"symbol": "BTC/USD", "bid": 99999, "ask": 100000,
                      "timestamp": datetime.now(timezone.utc).isoformat()}
        self.risk = {"max_consecutive_losses": 2}

    def restart(self):
        executor = PaperExecutor(self.broker, str(self.path))
        executor.timeout_seconds = executor.poll_seconds = 0
        return executor

    def test_loss_streak_survives_restart_and_blocks_third_entry(self):
        for index in range(2):
            result = self.executor.step(dict(self.signal, id=str(index)), self.quote, self.risk)
            self.assertEqual(result["action"], "opened", result)
            self.assertEqual(self.executor.shutdown()["action"], "closed")
            self.executor = self.restart()
        before = len(self.broker.submissions())
        result = self.executor.step(dict(self.signal, id="third"), self.quote, self.risk)
        self.assertEqual(result["reason"], "consecutive_loss_limit")
        self.assertEqual(len(self.broker.submissions()), before)

    def test_closing_is_counted_only_once(self):
        self.executor.step(self.signal, self.quote, self.risk)
        self.executor.shutdown()
        self.executor._note_closed({})
        self.executor = self.restart()
        self.executor.recover()
        self.assertEqual(len(self.executor.state["closed_trades"]), 1)
        self.assertEqual(self.executor.state["discipline"]["consecutive_losses"], 1)

    def test_mistake_request_flattens_owned_trade_and_survives_restart(self):
        self.executor.step(self.signal, self.quote, self.risk)
        request_halt(self.path, "chased entry")
        result = self.executor.step(None, self.quote, self.risk)
        self.assertEqual(result["trigger"], "execution_mistake")
        self.assertEqual(self.broker.quantity, 0)
        self.executor = self.restart()
        result = self.executor.step(dict(self.signal, id="next"), self.quote, self.risk)
        self.assertEqual(result["reason"], "execution_mistake")

    def test_spot_executor_rejects_short_even_with_long_geometry(self):
        result = self.executor.step(dict(self.signal, direction="short"), self.quote, self.risk)
        self.assertEqual(result["action"], "entry_skipped")
        self.assertEqual(self.broker.submissions(), [])

    def test_smoke_does_not_count_as_strategy_loss(self):
        self.executor.smoke(10)
        self.assertEqual(self.executor.state["discipline"]["consecutive_losses"], 0)

    def test_old_state_migrates_todays_recorded_losses(self):
        state = self.executor.state
        state.pop("discipline")
        state["closed_trades"] = [
            {"signal_id": str(i), "mode": "strategy", "net_pnl": "-1",
             "closed_at": datetime.now(timezone.utc).isoformat()} for i in range(2)]
        self.path.write_text(json.dumps(state))
        self.executor = self.restart()
        result = self.executor.step(self.signal, self.quote, self.risk)
        self.assertEqual(result["reason"], "consecutive_loss_limit")


class ReplayDisciplineTests(unittest.TestCase):
    def test_long_and_short_replays_stop_after_two_losses(self):
        tape = []
        for bar in range(7):
            for offset, price in enumerate((100, 97, 103, 100)):
                stamp = NOW + timedelta(minutes=bar * 5, seconds=offset)
                tape.append(Trade(f"{bar}-{offset}", stamp.isoformat(), price, 1, "B"))
        for direction in ("long", "short"):
            config = StrategyConfig(direction=direction, profile_bars=1,
                                    taker_fee=0, slippage_bps=0)
            stop, target = (98, 110) if direction == "long" else (102, 90)
            def signal(bars, config, as_of):
                return {"status": "signal", "reason": "fixture", "features": {},
                        "bar_end": as_of.isoformat(), "signal": {
                            "id": as_of.isoformat(), "entry": 100, "stop": stop,
                            "target": target, "direction": direction}}
            with self.subTest(direction=direction), patch("tred.strategy._analyze_bars", side_effect=signal):
                report = replay(tape, config)
            self.assertEqual(report["summary"]["trade_count"], 2)
            self.assertTrue(all(t["net_pnl"] < 0 for t in report["trades"]))


class ResearchBoundaryTests(unittest.TestCase):
    def test_research_limits_and_timeout_in_isolated_process(self):
        code = '''
import os, resource, signal
from tred.research import ResearchLimitError, research_limits
old_memory = resource.getrlimit(resource.RLIMIT_AS)
try:
    with research_limits():
        assert len(os.sched_getaffinity(0)) == 1
        assert os.nice(0) == 19
        assert 0 < resource.getrlimit(resource.RLIMIT_AS)[0] <= 512 * 1024 * 1024
        assert 0 < resource.getrlimit(resource.RLIMIT_CPU)[0] <= 31
        signal.raise_signal(signal.SIGALRM)
except ResearchLimitError:
    assert resource.getrlimit(resource.RLIMIT_AS) == old_memory
    print('bounded and restored')
else:
    raise AssertionError('timeout did not stop research')
'''
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                                text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "bounded and restored")

    def test_report_records_effective_short_flip_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            config, strategy = settings("config.toml")
            config["storage"]["database"] = str(Path(directory) / "t.sqlite3")
            with patch("tred.cli.settings", return_value=(config, strategy)), \
                    patch("tred.research.research_limits", return_value=nullcontext()), \
                    patch("tred.cli.load_env"), patch("tred.cli.emit"), \
                    patch("tred.cli.replay_input", return_value=[]), \
                    patch("tred.cli.replay", return_value={"summary": {}, "trades": []}):
                self.assertEqual(main(["backtest", "--direction", "short", "--flip"]), 0)
            reports = list(Path(directory).glob("replay-*.json"))
            self.assertEqual(len(reports), 1)
            recorded = json.loads(reports[0].read_text())["configuration"]
            self.assertEqual(recorded["direction"], "short")
            self.assertTrue(recorded["flip"])

    def test_spot_short_cli_rejects_before_credentials_and_store(self):
        config, strategy = settings("config.toml")
        strategy = dataclasses.replace(strategy, direction="short")
        with patch("tred.cli.settings", return_value=(config, strategy)), \
                patch("tred.cli.load_env", side_effect=AssertionError("credentials accessed")), \
                patch("tred.cli.Store", side_effect=AssertionError("store opened")), \
                patch("tred.cli.emit") as emit:
            self.assertEqual(main(["run", "--mode", "paper"]), 2)
        self.assertIn("no short", emit.call_args.args[0]["error"])

    def test_library_and_shipped_time_defaults_agree(self):
        _, configured = settings("config.toml")
        defaults = StrategyConfig()
        for field in ("bar_seconds", "profile_bars", "max_holding_bars"):
            self.assertEqual(getattr(defaults, field), getattr(configured, field))

    def test_query_limit_fails_instead_of_silently_truncating(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "t.sqlite3"))
            try:
                tape = [Trade(str(i), (NOW + timedelta(minutes=i)).isoformat(), 100, 1, "B") for i in range(5)]
                store.add_trades("us-1", "BTC/USD", tape)
                with self.assertRaisesRegex(ValueError, "resource limit"):
                    store.trades("us-1", "BTC/USD", max_trades=3)
                self.assertEqual(len(store.trades("us-1", "BTC/USD", max_trades=5)), 5)
            finally:
                store.close()

    def test_partial_window_fails_before_query(self):
        config, strategy = settings("config.toml")
        for start, end in ((NOW, None), (None, NOW)):
            with self.assertRaisesRegex(ValueError, "both --start and --end"):
                replay_input(None, config["market"], strategy, start, end)

    def test_halt_cli_does_not_load_credentials_or_open_broker(self):
        with tempfile.TemporaryDirectory() as directory:
            config, strategy = settings("config.toml")
            config["storage"]["execution_state"] = str(Path(directory) / "state.json")
            with patch("tred.cli.settings", return_value=(config, strategy)), \
                    patch("tred.cli.load_env", side_effect=AssertionError("credentials accessed")), \
                    patch("tred.cli.PaperClient", side_effect=AssertionError("broker opened")), \
                    patch("tred.cli.emit"):
                self.assertEqual(main(["halt", "--reason", "early entry"]), 0)


if __name__ == "__main__":
    unittest.main()
