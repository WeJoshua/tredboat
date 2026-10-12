import dataclasses
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace

from tred.client import ApiError, HttpClient, MarketData, PAPER_BASE, PaperClient
from tred.cli import (OPTIMIZE_GRID, aligned_start, execution_direction_error, execution_gate,
                      optimize_select, pnl_report, replay_input, run_loop, settings)
from tred.store import Store
from tred.strategy import StrategyConfig, Trade


class ClientTests(unittest.TestCase):
    def test_live_endpoint_and_live_key_rejected(self):
        with patch.dict(os.environ, {"APCA_API_BASE_URL": "https://api.alpaca.markets",
                                     "APCA_API_KEY_ID": "PK_TEST", "APCA_API_SECRET_KEY": "test-only"}):
            with self.assertRaisesRegex(ValueError, "restricted"):
                PaperClient()
        with patch.dict(os.environ, {"APCA_API_BASE_URL": "https://paper-api.alpaca.markets",
                                     "APCA_API_KEY_ID": "AK_TEST", "APCA_API_SECRET_KEY": "test-only"}):
            with self.assertRaisesRegex(ValueError, "paper API keys"):
                PaperClient()

    def test_paper_client_fixed_host_and_relative_paths_only(self):
        with patch.dict(os.environ, {"APCA_API_BASE_URL": PAPER_BASE,
                                     "APCA_API_KEY_ID": "PK_TEST", "APCA_API_SECRET_KEY": "test-only"}):
            client = PaperClient()
            self.assertEqual(client.base, PAPER_BASE)
            for path in ("https://api.alpaca.markets/v2/orders", "//attacker.example", "/../orders"):
                with self.assertRaises(ValueError):
                    client.request("POST", path)

    def test_unknown_post_outcome_never_retried(self):
        client = HttpClient(PAPER_BASE)
        with patch.object(client.opener, "open", side_effect=TimeoutError) as request:
            with self.assertRaises(ApiError):
                client.request("POST", "/orders", {"symbol": "BTC/USD"})
            self.assertEqual(request.call_count, 1)

    def test_secret_is_redacted_from_errors(self):
        client = HttpClient(PAPER_BASE, {"APCA-API-SECRET-KEY": "test-only-secret"})
        error = urllib.error.HTTPError(PAPER_BASE, 401, "bad", {}, io.BytesIO(b'test-only-secret'))
        with patch.object(client.opener, "open", side_effect=error):
            with self.assertRaises(ApiError) as caught:
                client.request("GET", "/account")
        self.assertNotIn("test-only-secret", str(caught.exception))

    def test_market_data_does_not_receive_account_headers(self):
        self.assertEqual(MarketData("us-1").headers, {})

    def test_trade_pagination_and_side_validation(self):
        client = MarketData("us-1")
        one = {"i": 9007199254740993, "p": 80000, "s": 0.001, "t": "2026-10-11T12:00:00.000000001Z", "tks": "B"}
        response = [{"trades": {"BTC/USD": [one]}, "next_page_token": "next"},
                    {"trades": {"BTC/USD": []}, "next_page_token": None}]
        now = datetime.now(timezone.utc)
        with patch.object(client, "request", side_effect=response) as request:
            pages = list(client.trade_pages("BTC/USD", now - timedelta(hours=1), now))
            self.assertEqual(pages[0][0].id, "9007199254740993")
            self.assertEqual(request.call_args.kwargs["query"]["page_token"], "next")
        one["tks"] = "unknown"
        with patch.object(client, "request", return_value={"trades": {"BTC/USD": [one]}}):
            with self.assertRaisesRegex(ValueError, "taker side"):
                list(client.trade_pages("BTC/USD", now - timedelta(hours=1), now))


class StoreTests(unittest.TestCase):
    def test_duplicate_overlap_and_separate_venues(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "test.sqlite3"))
            try:
                trade = Trade("1", "2026-10-11T12:00:00Z", 80000, 0.001, "B")
                self.assertEqual(store.add_trades("us", "BTC/USD", [trade]), 1)
                self.assertEqual(store.add_trades("us", "BTC/USD", [trade]), 0)
                self.assertEqual(store.add_trades("us-1", "BTC/USD", [trade]), 1)
                self.assertEqual(len(store.trades("us", "BTC/USD")), 1)
            finally:
                store.close()

    def test_coverage_does_not_hide_missing_intervals(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "test.sqlite3"))
            try:
                start = datetime(2026, 10, 11, tzinfo=timezone.utc)
                hour = timedelta(hours=1)
                store.mark_coverage("us-1", "BTC/USD", start, start + hour)
                store.mark_coverage("us-1", "BTC/USD", start + 2 * hour, start + 3 * hour)
                self.assertFalse(store.covers("us-1", "BTC/USD", start, start + 3 * hour))
                store.mark_coverage("us-1", "BTC/USD", start + hour, start + 2 * hour)
                self.assertTrue(store.covers("us-1", "BTC/USD", start, start + 3 * hour))
            finally:
                store.close()


class GateTests(unittest.TestCase):
    def setUp(self):
        config, self.strategy = settings("config.toml")
        self.market = config["market"]
        self.now = datetime.now(timezone.utc)
        self.quote = {"symbol": "BTC/USD", "timestamp": self.now.isoformat(), "bid": 79999, "ask": 80001}
        self.signal = {"id": "test", "entry": 80000, "stop": 79000, "target": 84000}

    def gate(self, signal=None, signal_quote=None, execution_quote=None):
        return execution_gate(signal or self.signal, signal_quote or self.quote,
                              execution_quote or self.quote, self.market, self.strategy, self.now)

    def test_valid_signal_and_quote(self):
        signal, reason = self.gate()
        self.assertEqual(reason, "allowed")
        self.assertEqual(signal["entry"], 80001)

    def test_costs_can_reject_positive_gross_reward(self):
        _, reason = self.gate(signal=dict(self.signal, target=80200))
        self.assertEqual(reason, "cost_adjusted_reward_risk")

    def test_cross_venue_basis_stale_and_spread_filters(self):
        _, reason = self.gate(signal_quote=dict(self.quote, bid=81000, ask=81001))
        self.assertEqual(reason, "venue_price_difference")
        _, reason = self.gate(execution_quote=dict(self.quote, timestamp=(self.now - timedelta(seconds=61)).isoformat()))
        self.assertEqual(reason, "stale_or_future_quote")
        _, reason = self.gate(execution_quote=dict(self.quote, bid=79500))
        self.assertEqual(reason, "spread_limit")

    def test_nonfinite_signal_never_passes(self):
        _, reason = self.gate(signal=dict(self.signal, target=float("inf")))
        self.assertEqual(reason, "invalid_signal_prices")

    def test_gate_rejects_short_execution_before_geometry(self):
        _, reason = self.gate(signal=dict(self.signal, direction="short"))
        self.assertEqual(reason, "short_execution_unavailable")

    def test_paper_direction_error(self):
        self.assertIsNone(execution_direction_error(self.strategy))
        short = dataclasses.replace(self.strategy, direction="short")
        self.assertIn("spot", execution_direction_error(short))
        flipped = dataclasses.replace(self.strategy, flip=True)
        self.assertIn("research-only", execution_direction_error(flipped))


class RunnerTests(unittest.TestCase):
    def test_optimize_grid_is_constrained(self):
        self.assertEqual(len(OPTIMIZE_GRID), 48)
        for combo in OPTIMIZE_GRID:
            self.assertIn(combo["row_size"], (12.5, 25.0, 50.0))
            self.assertIn(combo["imbalance_ratio"], (4.0, 6.0))
            self.assertIn(combo["zone_rows"], (0, 2))
            self.assertIn(combo["minimum_cluster_bars"], (2, 3))
            self.assertIn(combo["min_net_reward_risk"], (1.0, 1.2))

    def test_optimize_select_requires_filters_and_sorts(self):
        rows = [
            {"net_pnl": -10, "signal_count": 20, "trade_count": 5,
             "profit_factor": 1.2, "max_drawdown_fraction": 0.01},
            {"net_pnl": 50, "signal_count": 3, "trade_count": 2,
             "profit_factor": 3.0, "max_drawdown_fraction": 0.02},
            {"net_pnl": 100, "signal_count": 30, "trade_count": 8,
             "profit_factor": 2.5, "max_drawdown_fraction": 0.01},
            {"net_pnl": 200, "signal_count": 40, "trade_count": 10,
             "profit_factor": 3.0, "max_drawdown_fraction": 0.08},
        ]
        selected, eligible = optimize_select(rows, 5, 2, 0.05)
        self.assertEqual(selected["net_pnl"], 100)
        self.assertEqual(len(eligible), 2)
        self.assertIsNone(optimize_select(rows, 1000, 2, 0.05)[0])

    def test_pnl_report_aggregates_and_defaults_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            Path(path).write_text(json.dumps({"closed_trades": [
                {"net_pnl": "25.0"}, {"net_pnl": "-10.0"}]}))
            report = pnl_report(path)
            self.assertEqual(report["realized_pnl"], 15.0)
            self.assertEqual(report["trade_count"], 2)
            self.assertEqual(report["win_rate"], 0.5)
            self.assertAlmostEqual(report["profit_factor"], 2.5)
        empty = pnl_report("/nonexistent/state.json")
        self.assertEqual(empty["realized_pnl"], 0.0)
        self.assertEqual(empty["trade_count"], 0)

    def test_aligned_start_requests_two_extra_bars(self):
        config, strategy = settings("config.toml")
        now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        end = datetime.fromtimestamp(int(now.timestamp()) // strategy.bar_seconds * strategy.bar_seconds,
                                     timezone.utc)
        start = aligned_start(now, strategy)
        self.assertEqual(end - start, timedelta(seconds=(strategy.profile_bars + 2) * strategy.bar_seconds))

    def test_replay_rejects_partially_downloaded_history(self):
        config, strategy = settings("config.toml")
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "test.sqlite3"))
            try:
                start = datetime(2026, 10, 11, tzinfo=timezone.utc)
                span = timedelta(seconds=strategy.bar_seconds)
                trades = [Trade(str(i), (start + i * span).isoformat(), 80000, .001, "B") for i in range(5)]
                store.add_trades("us-1", "BTC/USD", trades)
                store.mark_coverage("us-1", "BTC/USD", start, start + span)
                with self.assertRaisesRegex(ValueError, "coverage is incomplete"):
                    replay_input(store, config["market"], strategy)
                store.mark_coverage("us-1", "BTC/USD", start + span, start + 5 * span)
                self.assertEqual(len(replay_input(store, config["market"], strategy)), 5)
            finally:
                store.close()

    def test_replay_input_honors_bound_window(self):
        config, strategy = settings("config.toml")
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "test.sqlite3"))
            try:
                start = datetime(2026, 10, 11, tzinfo=timezone.utc)
                span = timedelta(seconds=strategy.bar_seconds)
                trades = [Trade(str(i), (start + i * span).isoformat(), 80000, .001, "B") for i in range(5)]
                store.add_trades("us-1", "BTC/USD", trades)
                store.mark_coverage("us-1", "BTC/USD", start, start + 5 * span)
                bounded = replay_input(store, config["market"], strategy, start + span, start + 3 * span)
                self.assertEqual(len(bounded), 2)
                with self.assertRaisesRegex(ValueError, "end must be after start"):
                    replay_input(store, config["market"], strategy, start + 3 * span, start + span)
            finally:
                store.close()

    def test_bootstrap_failure_still_cleans_up_owned_paper_state(self):
        config, strategy = settings("config.toml")
        store = Mock()
        store.covers.return_value = False
        executor = Mock()
        executor.recover.return_value = {"ok": True}
        executor.shutdown.return_value = {"ok": True}
        with patch("tred.cli.fetch", side_effect=ApiError(None, "download failed")), patch("tred.cli.emit"):
            result = run_loop(SimpleNamespace(seconds=1), config, strategy, store, executor)
        self.assertEqual(result, 2)
        executor.recover.assert_called_once()
        executor.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
