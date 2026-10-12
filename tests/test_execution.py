"""Broker-state tests; no credentials or network access required."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tred.execution import PaperExecutor, TERMINAL


D = Decimal


class FakeApiError(RuntimeError):
    def __init__(self, status, message="broker error"):
        super().__init__(message)
        self.status = status


class Broker:
    def __init__(self):
        self.account = {"status": "ACTIVE", "equity": "10000", "cash": "10000",
                        "buying_power": "20000", "non_marginable_buying_power": "10000",
                        "trading_blocked": False}
        self.asset = {"class": "crypto", "status": "active", "tradable": True,
                      "min_order_size": "0.00001", "min_trade_increment": "0.00000001",
                      "price_increment": "0.01"}
        self.price = D("100000")
        self.quantity = D("0")
        self.fee = D("0.0025")
        self.other_positions = [{"symbol": "ETHUSD", "qty": "3"}]
        self.orders = {}
        self.events = []
        self.submit_hook = None
        self.timeout_role = None
        self.unknown_timeout = False
        self.rejected_role = None
        self.pending_stop = False
        self.pending_forever = False
        self.partial_entry = False
        self.partial_exit = False
        self.cancel_pending = False
        self.stop_fills_on_cancel = False

    def position(self):
        if not self.quantity:
            return None
        reserved = sum((D(o["qty"]) - D(o["filled_qty"]) for o in self.orders.values()
                        if o["side"] == "sell" and o["status"] not in TERMINAL), D("0"))
        return {"symbol": "BTCUSD", "qty": str(self.quantity),
                "qty_available": str(max(self.quantity - reserved, D("0")))}

    def fill(self, order, quantity, status="filled"):
        quantity = D(quantity)
        order["filled_qty"] = str(D(order["filled_qty"]) + quantity)
        order["filled_avg_price"] = str(self.price)
        order["status"] = status
        if order["side"] == "buy":
            self.quantity += quantity * (1 - self.fee)
        else:
            if quantity > self.quantity:
                raise AssertionError("Executor attempted to sell more than its position")
            self.quantity -= quantity

    def request(self, method, path, payload=None, query=None):
        self.events.append((method, path, copy.deepcopy(payload), copy.deepcopy(query)))
        if method == "GET" and path == "/account":
            return copy.deepcopy(self.account)
        if method == "GET" and path == "/assets/BTCUSD":
            return copy.deepcopy(self.asset)
        if method == "GET" and path == "/positions":
            p = self.position()
            return copy.deepcopy(self.other_positions + ([p] if p else []))
        if method == "GET" and path == "/orders":
            return [copy.deepcopy(o) for o in self.orders.values() if o["status"] not in TERMINAL]
        if method == "GET" and path == "/orders:by_client_order_id":
            for order in self.orders.values():
                if order["client_order_id"] == query["client_order_id"]:
                    return copy.deepcopy(order)
            raise FakeApiError(404, "Order not found")
        if method == "GET" and path.startswith("/orders/"):
            order = self.orders[path.rsplit("/", 1)[-1]]
            if order["status"] == "pending_new" and not self.pending_forever:
                order["status"] = "new"
            return copy.deepcopy(order)
        if method == "POST" and path == "/orders":
            if self.submit_hook:
                self.submit_hook(payload)
            role = "entry" if payload["side"] == "buy" else "stop" if payload["type"] == "stop_limit" else "exit"
            if self.rejected_role == role:
                raise FakeApiError(422, "Order rejected")
            if self.timeout_role == role and self.unknown_timeout:
                raise FakeApiError(None, "Response timeout")
            qty = D(payload.get("qty", "0")) or (D(payload["notional"]) / self.price)
            identity = str(len(self.orders) + 1)
            order = {**payload, "id": identity, "symbol": "BTCUSD", "qty": str(qty),
                     "filled_qty": "0", "filled_avg_price": None, "status": "new"}
            self.orders[identity] = order
            if role == "entry":
                self.fill(order, qty / 2 if self.partial_entry else qty,
                          "partially_filled" if self.partial_entry else "filled")
            elif role == "exit":
                self.fill(order, qty / 2 if self.partial_exit else qty,
                          "partially_filled" if self.partial_exit else "filled")
            elif self.pending_stop:
                order["status"] = "pending_new"
            if self.timeout_role == role:
                raise FakeApiError(None, "Response timeout")
            return copy.deepcopy(order)
        if method == "DELETE" and path.startswith("/orders/"):
            order = self.orders[path.rsplit("/", 1)[-1]]
            if order["type"] == "stop_limit" and self.stop_fills_on_cancel:
                self.fill(order, D(order["qty"]) - D(order["filled_qty"]))
                raise FakeApiError(422, "Order already filled")
            if not self.cancel_pending:
                order["status"] = "canceled"
            return None
        raise AssertionError(f"Unexpected API request: {method} {path}")

    def submissions(self, side=None):
        return [p for m, _, p, _ in self.events if m == "POST" and (side is None or p["side"] == side)]


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"
        self.broker = Broker()
        self.executor = self.restart()
        self.quote = {"symbol": "BTC/USD", "bid": 99999, "ask": 100000,
                      "timestamp": datetime.now(timezone.utc).isoformat()}
        self.signal = {"id": "signal-1", "symbol": "BTC/USD", "entry": 100000,
                       "stop": 99000, "target": 103000, "expected_cost_fraction": 0.005}
        self.risk = {"risk_fraction": 0.0025, "daily_loss_fraction": 0.01,
                     "max_notional": 100, "max_quote_age_seconds": 60,
                     "max_spread_bps": 15, "fee_rate": 0.0025,
                     "max_holding_seconds": 3600}

    def restart(self):
        executor = PaperExecutor(self.broker, str(self.path))
        executor.timeout_seconds = 0
        executor.poll_seconds = 0
        return executor

    def enter(self):
        return self.executor.step(self.signal, self.quote, self.risk)

    def assertClosed(self, result):
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.executor.state["phase"], "closed")
        self.assertEqual(self.broker.quantity, 0)

    def test_smoke_round_trip_preserves_other_asset_and_uses_actual_net_quantity(self):
        result = self.executor.smoke(10)
        self.assertClosed(result)
        self.assertEqual(result["action"], "smoke_complete")
        buys, sells = self.broker.submissions("buy"), self.broker.submissions("sell")
        self.assertEqual(buys[0]["notional"], "10")
        self.assertEqual(D(sells[0]["qty"]), D("0.00009975"))
        self.assertEqual([o["type"] for o in sells], ["stop_limit", "market"])
        self.assertEqual(D(sells[0]["stop_price"]), D("98000"))
        self.assertEqual(self.broker.orders[result["stop_order_id"]]["status"], "canceled")
        self.assertEqual(self.broker.other_positions, [{"symbol": "ETHUSD", "qty": "3"}])
        self.assertTrue(all(o["time_in_force"] == "gtc" for o in buys + sells))

    def test_intent_is_on_disk_before_every_post_and_order_ids_are_stable(self):
        def validate(payload):
            stored = json.loads(self.path.read_text())
            records = stored["trade"]["orders"].values()
            self.assertTrue(any(r["client_order_id"] == payload["client_order_id"] and
                                r["status"] == "intent" for r in records))
            self.assertTrue(payload["client_order_id"].startswith("tred-"))
        self.broker.submit_hook = validate
        self.assertClosed(self.executor.smoke())
        ids = [o["client_order_id"] for o in self.broker.submissions()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_preexisting_btc_position_blocks_smoke_without_mutation(self):
        self.broker.quantity = D("0.02")
        self.assertFalse(self.executor.smoke()["ok"])
        self.assertEqual(self.broker.quantity, D("0.02"))
        self.assertEqual(self.broker.submissions(), [])

    def test_preexisting_btc_order_blocks_smoke_without_cancellation(self):
        self.broker.orders["unowned"] = {"id": "unowned", "symbol": "BTCUSD", "client_order_id": "other",
                                          "side": "buy", "qty": "0.1", "filled_qty": "0", "status": "new"}
        self.assertFalse(self.executor.smoke()["ok"])
        self.assertEqual(self.broker.submissions(), [])
        self.assertFalse(any(m == "DELETE" for m, _, _, _ in self.broker.events))

    def test_lost_submit_response_is_reconciled_without_duplicate_post(self):
        self.broker.timeout_role = "entry"
        self.assertClosed(self.executor.smoke())
        self.assertEqual(len(self.broker.submissions("buy")), 1)
        self.assertTrue(any(p == "/orders:by_client_order_id" for _, p, _, _ in self.broker.events))

    def test_unknown_submit_blocks_restart_and_never_retries_post(self):
        self.broker.timeout_role = "entry"
        self.broker.unknown_timeout = True
        self.assertFalse(self.executor.smoke()["ok"])
        self.executor = self.restart()
        self.assertFalse(self.executor.recover()["ok"])
        self.assertFalse(self.executor.flatten_owned()["ok"])
        self.assertEqual(len(self.broker.submissions()), 1)
        self.assertEqual(self.executor.state["phase"], "entering")

    def test_definite_entry_rejection_is_terminal_without_ambiguous_intent(self):
        self.broker.rejected_role = "entry"
        result = self.enter()
        self.assertEqual(result["action"], "entry_unfilled")
        self.assertTrue(result["ok"])
        self.assertEqual(self.executor.state["phase"], "closed")
        self.assertTrue(self.executor.state["trade"]["orders"]["entry"]["request_rejected"])
        self.assertTrue(self.executor.recover()["ok"])

    def test_rejected_smoke_entry_does_not_claim_execution_was_verified(self):
        self.broker.rejected_role = "entry"
        result = self.executor.smoke()
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "smoke_unfilled")

    def test_rejected_protection_emergency_exits_owned_position(self):
        self.broker.rejected_role = "stop"
        result = self.enter()
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "protection_failed")
        self.assertClosed(result["emergency_exit"])
        self.assertEqual(len(self.broker.submissions("sell")), 2)

    def test_partial_entry_is_canceled_before_net_quantity_is_protected(self):
        self.broker.partial_entry = True
        result = self.enter()
        self.assertTrue(result["ok"], result)
        entry = self.executor.state["trade"]["orders"]["entry"]
        stop = self.broker.submissions("sell")[0]
        self.assertEqual(entry["status"], "canceled")
        self.assertEqual(D(stop["qty"]), self.broker.quantity)
        delete_index = next(i for i, e in enumerate(self.broker.events) if e[0] == "DELETE")
        stop_index = next(i for i, e in enumerate(self.broker.events) if e[0] == "POST" and e[2]["type"] == "stop_limit")
        self.assertLess(delete_index, stop_index)

    def test_entry_cancel_timeout_prevents_stop_and_exit_orders(self):
        self.broker.partial_entry = True
        self.broker.cancel_pending = True
        self.assertFalse(self.enter()["ok"])
        self.assertFalse(self.executor.shutdown()["ok"])
        self.assertEqual(len(self.broker.submissions()), 1)

    def test_pending_stop_is_confirmed_before_entry_returns_success(self):
        self.broker.pending_stop = True
        result = self.enter()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.executor.state["trade"]["orders"]["stop"]["status"], "new")

    def test_pending_stop_timeout_cancels_stop_before_emergency_exit(self):
        self.broker.pending_stop = True
        self.broker.pending_forever = True
        result = self.enter()
        self.assertFalse(result["ok"])
        self.assertClosed(result["emergency_exit"])
        self.assertEqual(self.broker.orders["2"]["status"], "canceled")

    def test_target_exit_cancels_and_confirms_stop_before_market_sell(self):
        self.assertTrue(self.enter()["ok"])
        self.quote["bid"] = 103100
        result = self.executor.step(None, self.quote, self.risk)
        self.assertClosed(result)
        self.assertEqual(result["trigger"], "price_exit")
        cancel_index = next(i for i, e in enumerate(self.broker.events) if e[0] == "DELETE")
        exit_index = next(i for i, e in enumerate(self.broker.events)
                          if e[0] == "POST" and e[2]["side"] == "sell" and e[2]["type"] == "market")
        self.assertLess(cancel_index, exit_index)

    def test_unconfirmed_stop_cancellation_never_submits_second_sell(self):
        self.assertTrue(self.enter()["ok"])
        self.broker.cancel_pending = True
        result = self.executor.shutdown()
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.broker.submissions("sell")), 1)
        self.assertGreater(self.broker.quantity, 0)

    def test_stop_filling_during_cancel_does_not_double_sell(self):
        self.assertTrue(self.enter()["ok"])
        self.broker.stop_fills_on_cancel = True
        self.assertClosed(self.executor.shutdown())
        self.assertEqual(len(self.broker.submissions("sell")), 1)

    def test_native_stop_fill_is_reconciled_without_new_sell(self):
        self.assertTrue(self.enter()["ok"])
        stop = self.broker.orders["2"]
        self.broker.fill(stop, stop["qty"])
        self.assertClosed(self.executor.recover())
        self.assertEqual(len(self.broker.submissions("sell")), 1)

    def test_restart_preserves_protection_without_duplicate_orders(self):
        self.assertTrue(self.enter()["ok"])
        self.executor = self.restart()
        result = self.executor.recover()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["action"], "protected")
        self.assertEqual(len(self.broker.submissions()), 2)
        self.assertClosed(self.executor.shutdown())

    def test_unexpected_position_change_blocks_flatten_without_selling_other_coins(self):
        self.assertTrue(self.enter()["ok"])
        self.broker.quantity += D("0.01")
        before = self.broker.quantity
        self.assertFalse(self.executor.shutdown()["ok"])
        self.assertEqual(self.broker.quantity, before)
        self.assertEqual(len(self.broker.submissions("sell")), 1)

    def test_unowned_order_appearing_during_trade_blocks_cancellation(self):
        self.assertTrue(self.enter()["ok"])
        self.broker.orders["external"] = {"id": "external", "symbol": "BTCUSD", "client_order_id": "outside",
                                          "side": "buy", "qty": "0.01", "filled_qty": "0", "status": "new"}
        self.assertFalse(self.executor.shutdown()["ok"])
        self.assertFalse(any(e[0] == "DELETE" for e in self.broker.events))

    def test_entry_notional_is_hard_capped_and_position_risk_reduces_size(self):
        self.executor.max_notional = D("100")
        self.risk["max_notional"] = 5000
        self.assertTrue(self.enter()["ok"])
        buy = self.broker.submissions("buy")[0]
        self.assertNotIn("qty", buy)
        self.assertLessEqual(D(buy["notional"]), 100)
        self.assertClosed(self.executor.shutdown())
        self.broker.account["equity"] = "10"
        self.executor.state["day_start_equity"] = "10"
        self.signal["id"] = "signal-2"
        self.assertTrue(self.enter()["ok"])
        buy = self.broker.submissions("buy")[-1]
        self.assertLessEqual(D(buy["notional"]) * D(".015"), D(".025"))

    def test_stale_quote_is_expected_skip_without_submission(self):
        self.quote["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        result = self.enter()
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "entry_skipped")
        self.assertEqual(self.broker.submissions(), [])

    def test_insufficient_cash_does_not_use_margin_buying_power(self):
        self.broker.account["cash"] = "1"
        result = self.enter()
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "entry_skipped")
        self.assertEqual(self.broker.submissions(), [])

    def test_daily_equity_loss_blocks_entry_after_restart(self):
        self.executor.state.update(risk_day=datetime.now(timezone.utc).date().isoformat(), day_start_equity="10000")
        self.executor._save()
        self.executor = self.restart()
        self.broker.account["equity"] = "9899"
        result = self.enter()
        self.assertEqual(result["action"], "entry_skipped")
        self.assertTrue(result["ok"])
        self.assertEqual(self.broker.submissions(), [])

    def test_daily_loss_closes_owned_open_position(self):
        self.assertTrue(self.enter()["ok"])
        self.broker.account["equity"] = "9900"
        result = self.executor.step(None, self.quote, self.risk)
        self.assertClosed(result)
        self.assertEqual(result["trigger"], "daily_loss")

    def test_daily_baseline_rolls_over_while_open(self):
        self.assertTrue(self.enter()["ok"])
        self.executor.state["risk_day"] = "2000-01-01"
        self.executor.state["day_start_equity"] = "50000"
        self.broker.account["equity"] = "9999"
        result = self.executor.step(None, self.quote, self.risk)
        self.assertEqual(result["action"], "holding")
        self.assertEqual(self.executor.state["day_start_equity"], "9999")

    def test_maximum_holding_time_closes_position(self):
        self.assertTrue(self.enter()["ok"])
        self.executor.state["trade"]["opened_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        result = self.executor.step(None, self.quote, self.risk)
        self.assertClosed(result)
        self.assertEqual(result["trigger"], "max_holding")

    def test_partial_exit_is_canceled_and_recover_sells_only_residual(self):
        self.assertTrue(self.enter()["ok"])
        self.broker.partial_exit = True
        first = self.executor.shutdown()
        self.assertFalse(first["ok"])
        remaining = self.broker.quantity
        self.broker.partial_exit = False
        self.executor = self.restart()
        self.assertClosed(self.executor.recover())
        exits = [o for o in self.broker.submissions("sell") if o["type"] == "market"]
        self.assertEqual(D(exits[-1]["qty"]), remaining)

    def test_fractional_dust_is_reported_and_blocks_new_entry(self):
        self.broker.fee = D("0.00251")
        result = self.executor.smoke(10)
        self.assertFalse(result["ok"])
        self.assertEqual(self.executor.state["phase"], "dust")
        self.assertGreater(self.broker.quantity, 0)
        self.assertLess(self.broker.quantity, D(self.broker.asset["min_trade_increment"]))
        self.assertFalse(self.executor.recover()["ok"])
        count = len(self.broker.submissions())
        self.assertFalse(self.enter()["ok"])
        self.assertEqual(len(self.broker.submissions()), count)

    def test_repeat_signal_does_not_reenter_after_shutdown(self):
        self.assertTrue(self.enter()["ok"])
        self.assertClosed(self.executor.shutdown())
        result = self.enter()
        self.assertEqual(result["action"], "duplicate_signal")
        self.assertEqual(len(self.broker.submissions("buy")), 1)

    def test_closed_trade_records_realized_pnl_and_survives_restart(self):
        self.assertTrue(self.enter()["ok"])
        result = self.executor.shutdown()
        self.assertClosed(result)
        self.assertEqual(result["pnl"]["net_pnl"], "-0.25")
        self.assertEqual(len(self.executor.state["closed_trades"]), 1)
        self.executor = self.restart()
        self.assertEqual(len(self.executor.state["closed_trades"]), 1)
        report = self.executor.state["closed_trades"][0]
        self.assertEqual(report["signal_id"], "signal-1")
        self.assertEqual(report["mode"], "strategy")
        self.assertEqual(report["estimated_fee_usd"], "0.25")
        self.assertEqual(report["quantity"], "0.0009975")

    def test_smoke_pnl_also_recorded(self):
        self.assertClosed(self.executor.smoke(10))
        self.assertEqual(len(self.executor.state["closed_trades"]), 1)
        self.assertEqual(self.executor.state["closed_trades"][0]["mode"], "smoke")
        self.assertEqual(self.executor.state["closed_trades"][0]["net_pnl"], "-0.025")


if __name__ == "__main__":
    unittest.main()
