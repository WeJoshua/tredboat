"""Conservative, single-position execution for an Alpaca paper client.

The caller owns the process lock. Every order intent is persisted before it is
sent; an ambiguous submit is only looked up, never automatically resubmitted.
This executor never adopts an existing position or sells an unowned position.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from .discipline import apply_halt_request, day_state, record_outcome


TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}
ZERO = Decimal("0")


def dec(value) -> Decimal:
    return Decimal(str(value or "0"))


def symbol_key(value: str) -> str:
    return value.replace("/", "").upper()


class ExecutionError(RuntimeError):
    pass


class EntrySkipped(ExecutionError):
    """An expected risk filter, not an unresolved execution error."""


class PaperExecutor:
    """One owned BTC/USD trade, with a durable intent and native stop limit."""

    poll_seconds = 0.5
    timeout_seconds = 10.0

    def __init__(self, client, state_path: str, max_notional: float = 100.0):
        self.client = client
        self.path = Path(state_path)
        self.max_notional = min(dec(max_notional), Decimal("100"))
        if self.max_notional <= 0:
            raise ValueError("max_notional must be positive")
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state.get("version") != 1:
                raise ExecutionError("Unknown execution-state version; manual review required")
        else:
            self.state = {"version": 1, "instance": uuid.uuid4().hex[:10],
                          "phase": "idle", "trade": None, "seen_signals": []}
        self.state.setdefault("closed_trades", [])
        if "discipline" not in self.state:
            discipline = self.state["discipline"] = {}
            now = datetime.now(timezone.utc)
            day_state(discipline, now)
            seen = set()
            for closed in self.state["closed_trades"]:
                if (closed.get("mode") != "strategy" or not closed.get("closed_at")
                        or closed.get("signal_id") in seen):
                    continue
                stamp = datetime.fromisoformat(closed["closed_at"].replace("Z", "+00:00"))
                if stamp.astimezone(timezone.utc).date() == now.date():
                    record_outcome(discipline, closed["net_pnl"], now,
                                   self.state.get("max_consecutive_losses", 2))
                    seen.add(closed.get("signal_id"))
        self._asset_cache = None

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".execution-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.state, stream, sort_keys=True, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _result(self, ok=True, action="noop", **details):
        return {"ok": ok, "action": action, "phase": self.state["phase"], **details}

    def _fail(self, error):
        self.state["last_error"] = str(error)
        self._save()
        return self._result(False, "blocked", reason=str(error))

    def _request(self, method, path, payload=None, query=None):
        return self.client.request(method, path, payload=payload, query=query)

    def _position(self):
        positions = self._request("GET", "/positions")
        matches = [p for p in positions if symbol_key(p["symbol"]) == "BTCUSD"]
        if len(matches) > 1:
            raise ExecutionError("Multiple BTC positions reported")
        return matches[0] if matches else None

    def _asset(self):
        if self._asset_cache is None:
            asset = self._request("GET", "/assets/BTCUSD")
            if not asset.get("tradable") or asset.get("status") != "active":
                raise ExecutionError("BTC/USD is not an active tradable asset")
            if asset.get("class", "crypto") != "crypto":
                raise ExecutionError("Expected a crypto asset")
            if not asset.get("min_trade_increment") or not asset.get("min_order_size"):
                raise ExecutionError("Asset sizing increments missing")
            self._asset_cache = asset
        return self._asset_cache

    def _rounded(self, value, field="min_trade_increment"):
        increment = dec(self._asset().get(field, "0.01"))
        if increment <= 0:
            raise ExecutionError("Invalid asset increment")
        return (dec(value) / increment).to_integral_value(rounding=ROUND_DOWN) * increment

    def _open_orders(self):
        orders = self._request("GET", "/orders", query={"status": "open", "limit": 500})
        # Fail closed if the endpoint may have truncated an account's order list.
        if len(orders) >= 500:
            raise ExecutionError("Open-order list may be truncated")
        return [o for o in orders if symbol_key(o["symbol"]) == "BTCUSD"]

    def _known_orders(self):
        trade = self.state.get("trade") or {}
        return trade.get("orders", {})

    def _assert_orders_owned(self):
        known = {o["client_order_id"] for o in self._known_orders().values()}
        if any(o.get("client_order_id") not in known for o in self._open_orders()):
            raise ExecutionError("Unowned BTC order exists; refusing to change exposure")

    def _refresh(self, role):
        record = self._known_orders()[role]
        if record.get("request_rejected"):
            return record
        try:
            if record.get("id"):
                order = self._request("GET", "/orders/" + record["id"])
            else:
                order = self._request("GET", "/orders:by_client_order_id",
                                      query={"client_order_id": record["client_order_id"]})
        except Exception as exc:
            raise ExecutionError(f"Cannot reconcile {role} intent; no submit retry: {exc}") from exc
        if order.get("client_order_id") != record["client_order_id"]:
            raise ExecutionError("Broker client order ID does not match persisted intent")
        record.update({k: order.get(k) for k in
                       ("id", "status", "filled_qty", "filled_avg_price", "qty")})
        self._save()
        return record

    def _submit(self, role, payload):
        trade = self.state["trade"]
        if role in trade["orders"]:
            return self._refresh(role)
        digest = hashlib.sha256((self.state["instance"] + trade["id"] + role).encode()).hexdigest()[:30]
        cid = "tred-" + digest
        payload = {**payload, "client_order_id": cid}
        trade["orders"][role] = {"client_order_id": cid, "payload": payload,
                                  "status": "intent", "filled_qty": "0"}
        self._save()
        try:
            order = self._request("POST", "/orders", payload=payload)
        except Exception as submit_error:
            # Resolve by client ID even after HTTP rejection (a duplicate ID can
            # refer to an existing order). Only definite HTTP rejection plus an
            # explicit lookup 404 permits recording a nonexistent rejected order.
            try:
                existing = self._request("GET", "/orders:by_client_order_id",
                                         query={"client_order_id": cid})
            except Exception as lookup_error:
                if (getattr(submit_error, "status", None) in {400, 401, 403, 404, 405, 406, 415, 422}
                        and getattr(lookup_error, "status", None) == 404):
                    record = trade["orders"][role]
                    record.update(status="rejected", request_rejected=True,
                                  rejection=str(submit_error), filled_qty="0")
                    self._save()
                    return record
                raise ExecutionError(f"Cannot reconcile {role} intent; no submit retry: {lookup_error}") from lookup_error
            if existing.get("client_order_id") != cid or not existing.get("id"):
                raise ExecutionError("Reconciled order identity is missing or mismatched")
            trade["orders"][role].update({k: existing.get(k) for k in
                                         ("id", "status", "filled_qty", "filled_avg_price", "qty")})
            self._save()
            return trade["orders"][role]
        if order.get("client_order_id") != cid or not order.get("id"):
            raise ExecutionError("Order response identity is missing or mismatched")
        trade["orders"][role].update({k: order.get(k) for k in
                                     ("id", "status", "filled_qty", "filled_avg_price", "qty")})
        self._save()
        return trade["orders"][role]

    def _wait(self, role):
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            order = self._refresh(role)
            if order["status"] in TERMINAL:
                return order
            if time.monotonic() >= deadline:
                return order
            time.sleep(self.poll_seconds)

    def _cancel_confirm(self, role):
        order = self._refresh(role)
        if order["status"] not in TERMINAL:
            try:
                self._request("DELETE", "/orders/" + order["id"])
            except Exception:
                # An order may fill just before cancellation. Its final state,
                # not the cancellation HTTP response, is authoritative.
                pass
            order = self._wait(role)
        if order["status"] not in TERMINAL:
            raise ExecutionError(f"{role} cancellation not confirmed; no replacement sell")
        return order

    def _net_owned(self):
        orders = self._known_orders()
        buy = dec(orders.get("entry", {}).get("filled_qty"))
        sold = sum((dec(o.get("filled_qty")) for role, o in orders.items() if role != "entry"), ZERO)
        return buy - sold

    def _realized_pnl(self):
        trade = self.state.get("trade") or {}
        orders = trade.get("orders", {})
        entry = orders.get("entry") or {}
        bought = dec(entry.get("filled_qty"))
        entry_price = dec(entry.get("filled_avg_price"))
        if bought <= 0 or entry_price <= 0:
            return None
        exits = [(dec(o.get("filled_qty")), dec(o.get("filled_avg_price")))
                 for role, o in orders.items()
                 if role != "entry" and dec(o.get("filled_qty")) > 0
                 and dec(o.get("filled_avg_price", 0)) > 0]
        if not exits:
            return None
        sold = sum(q for q, _ in exits)
        proceeds = sum(q * p for q, p in exits)
        realized = min(bought - dec(trade.get("fee_qty", "0")), sold)
        if realized <= 0:
            return None
        cost = realized * entry_price
        fee = dec(trade.get("fee_qty", "0")) * entry_price
        def shown(value):
            return str(value.normalize())
        return {"quantity": shown(realized), "entry_price": shown(entry_price),
                "exit_price": shown(proceeds / realized),
                "proceeds": shown(proceeds), "cost": shown(cost),
                "estimated_fee_usd": shown(fee), "net_pnl": shown(proceeds - cost - fee)}

    def _note_closed(self, result):
        trade = self.state.get("trade") or {}
        if trade.get("pnl_recorded") or any(
                item.get("signal_id") == trade.get("id") for item in self.state["closed_trades"]):
            return result
        pnl = self._realized_pnl()
        if not pnl:
            return result
        pnl.update(signal_id=trade.get("id"), mode=trade.get("mode", "strategy"),
                   closed_at=datetime.now(timezone.utc).isoformat())
        closed = list(self.state.get("closed_trades", []))
        closed.append(pnl)
        self.state["closed_trades"] = closed[-200:]
        trade["pnl_recorded"] = True
        if pnl["mode"] == "strategy":
            record_outcome(self.state.setdefault("discipline", {}), pnl["net_pnl"],
                           datetime.now(timezone.utc), self.state.get("max_consecutive_losses", 2))
        self._save()
        return {**result, "pnl": pnl}

    def _owned_position(self, establishing=False):
        self._assert_orders_owned()
        position = self._position()
        actual = dec(position.get("qty")) if position else ZERO
        expected = self._net_owned()
        if actual < 0 or actual > expected:
            raise ExecutionError("BTC quantity exceeds owned fills or is short; manual reconciliation required")
        trade = self.state["trade"]
        if establishing:
            # Fees may be charged in purchased BTC. The freshly settled position
            # is the actual owned quantity; never use gross fill quantity to sell.
            if expected > 0 and (actual <= 0 or actual < expected * Decimal("0.98")):
                raise ExecutionError("Entry position has not settled or differs materially from filled quantity")
            trade["fee_qty"] = str(expected - actual)
            self._save()
        else:
            expected -= dec(trade.get("fee_qty"))
            if abs(actual - max(expected, ZERO)) > dec(self._asset()["min_trade_increment"]):
                raise ExecutionError("Owned BTC quantity changed unexpectedly; manual reconciliation required")
        return position, actual

    def _protect(self):
        trade = self.state["trade"]
        if "stop" in trade["orders"]:
            stop = self._refresh("stop")
            if stop["status"] in TERMINAL:
                raise ExecutionError("Native protective stop is no longer active")
            return self._confirm_protection()
        position, quantity = self._owned_position()
        available = dec(position.get("qty_available", quantity)) if position else ZERO
        qty = self._rounded(min(quantity, available))
        if qty < dec(self._asset()["min_order_size"]):
            raise ExecutionError("Owned fill is below minimum protective order size")
        stop_price = self._rounded(trade["stop"], "price_increment")
        limit_price = self._rounded(stop_price * Decimal("0.995"), "price_increment")
        if limit_price <= 0 or limit_price >= stop_price:
            raise ExecutionError("Invalid protective stop prices")
        self._submit("stop", {"symbol": "BTC/USD", "side": "sell", "type": "stop_limit",
                                       "time_in_force": "gtc", "qty": str(qty),
                                       "stop_price": str(stop_price), "limit_price": str(limit_price)})
        return self._confirm_protection()

    def _confirm_protection(self):
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            order = self._refresh("stop")
            if order["status"] in {"new", "accepted", "partially_filled", "accepted_for_bidding"}:
                return order
            if order["status"] in TERMINAL or time.monotonic() >= deadline:
                raise ExecutionError("Native protective stop not acknowledged")
            time.sleep(self.poll_seconds)

    def _finish_entry(self, protect=True):
        entry = self._wait("entry")
        if entry["status"] not in TERMINAL:
            entry = self._cancel_confirm("entry")
        if dec(entry.get("filled_qty")) <= 0:
            self.state["phase"] = "closed"
            self._save()
            return self._result(True, "entry_unfilled", entry_order_id=entry.get("id"))
        self._owned_position(establishing=True)
        self.state["phase"] = "open"
        self.state["trade"].setdefault("opened_at", datetime.now(timezone.utc).isoformat())
        if self.state["trade"].get("mode") == "smoke":
            # Exercise actual native protection in the bounded smoke cycle.
            # This is an execution test, not an order-flow strategy signal.
            self.state["trade"]["stop"] = str(dec(entry.get("filled_avg_price")) * Decimal(".98"))
        self._save()
        if protect:
            try:
                stop = self._protect()
            except Exception as exc:
                result = self.flatten_owned()
                return self._result(False, "protection_failed", reason=str(exc), emergency_exit=result)
            return self._result(True, "opened", entry_order_id=entry["id"], stop_order_id=stop["id"])
        return self._result(True, "opened", entry_order_id=entry["id"])

    def status(self):
        try:
            account = self._request("GET", "/account")
            position = self._position()
            return self._result(account={k: account.get(k) for k in
                                         ("status", "trading_blocked", "equity", "cash", "buying_power")},
                                symbol="BTC/USD", position_qty=(position or {}).get("qty", "0"),
                                owned_order_ids={r: o.get("id") for r, o in self._known_orders().items()},
                                last_error=self.state.get("last_error"))
        except Exception as exc:
            return self._fail(exc)

    def recover(self):
        try:
            if not self.state.get("trade") or self.state["phase"] in {"idle", "closed"}:
                if self._position() or self._open_orders():
                    raise ExecutionError("Preexisting BTC position/orders are not owned by this executor")
                return self._note_closed(self._result(action="ready"))
            for role in list(self._known_orders()):
                self._refresh(role)
            if self.state["phase"] == "entering":
                result = self._finish_entry()
                if result["ok"] and self.state["trade"].get("mode") == "smoke":
                    return self.flatten_owned()
                return result
            if self.state["phase"] == "exiting":
                return self.flatten_owned()
            if self.state["phase"] == "dust":
                return self._result(False, "dust_remaining", reason="Owned BTC dust blocks new entries")
            if self.state["trade"].get("mode") == "smoke":
                return self.flatten_owned()
            position, qty = self._owned_position()
            if qty == 0:
                self.state["phase"] = "closed"
                self._save()
                return self._note_closed(self._result(action="closed"))
            stop = self._known_orders().get("stop")
            if stop and stop["status"] in TERMINAL:
                return self.flatten_owned()
            self._protect()
            return self._result(action="protected", position_qty=str(qty))
        except Exception as exc:
            return self._fail(exc)

    def _daily_baseline(self, account):
        equity = dec(account.get("equity"))
        if equity <= 0:
            raise ExecutionError("Account equity is not positive")
        today = datetime.now(timezone.utc).date().isoformat()
        if self.state.get("risk_day") != today:
            self.state["risk_day"] = today
            self.state["day_start_equity"] = str(equity)
            self._save()
        return dec(self.state["day_start_equity"])

    def _entry_checks(self, notional, quote=None, risk=None):
        risk = risk or {}
        discipline = self._discipline()
        if discipline["halted_reason"]:
            raise EntrySkipped(discipline["halted_reason"])
        account = self._request("GET", "/account")
        if account.get("trading_blocked") or account.get("account_blocked") or account.get("trade_suspended_by_user"):
            raise ExecutionError("Account trading is blocked")
        if account.get("status") != "ACTIVE":
            raise ExecutionError("Account is not active")
        # Crypto must be funded by cash. Stock margin buying power is irrelevant.
        cash = dec(account.get("cash"))
        buying_power = dec(account.get("non_marginable_buying_power", account.get("buying_power", cash)))
        cap = min(self.max_notional, dec(risk.get("max_notional", self.max_notional)))
        fee_rate = dec(risk.get("fee_rate", ".0025"))
        if notional <= 0 or notional > cap or notional * (1 + fee_rate) > min(cash, buying_power):
            raise EntrySkipped("Entry exceeds notional cap or available cash")
        if self._position() or self._open_orders():
            raise ExecutionError("Preexisting BTC position/orders; refusing to adopt or overwrite")
        equity = dec(account.get("equity"))
        baseline = self._daily_baseline(account)
        fraction = min(dec(risk.get("daily_loss_fraction", ".01")), Decimal(".01"))
        if fraction <= 0 or equity <= baseline * (1 - fraction):
            raise EntrySkipped("Daily equity loss limit reached")
        if quote is not None:
            if symbol_key(quote.get("symbol", "BTC/USD")) != "BTCUSD":
                raise ExecutionError("Expected BTC/USD quote")
            stamp = quote.get("timestamp", quote.get("t"))
            if not stamp:
                raise ExecutionError("Quote timestamp missing")
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ExecutionError("Quote timestamp lacks timezone")
            age = (datetime.now(timezone.utc) - parsed).total_seconds()
            if age < -5 or age > min(float(risk.get("max_quote_age_seconds", 60)), 60):
                raise EntrySkipped("Quote is stale or in the future")
            bid, ask = dec(quote.get("bid")), dec(quote.get("ask"))
            if bid <= 0 or ask < bid:
                raise ExecutionError("Invalid bid/ask quote")
            if (ask - bid) / ask * 10000 > dec(risk.get("max_spread_bps", 15)):
                raise EntrySkipped("Spread exceeds entry limit")
        self._asset()
        return account

    def _start(self, signal, qty=None, notional=None, mode="strategy"):
        self.state["trade"] = {"id": str(signal["id"]), "symbol": "BTC/USD", "orders": {},
                               "stop": str(signal.get("stop", 0)), "target": str(signal.get("target", 0)),
                               "mode": mode, "fee_qty": "0"}
        self.state["phase"] = "entering"
        self.state["seen_signals"] = (self.state.get("seen_signals", []) + [str(signal["id"])])[-1000:]
        self._save()
        payload = {"symbol": "BTC/USD", "side": "buy", "type": "market", "time_in_force": "gtc"}
        payload.update({"qty": str(qty)} if qty is not None else {"notional": str(notional)})
        self._submit("entry", payload)
        return self._finish_entry()

    def _discipline(self):
        state = self.state.setdefault("discipline", {})
        previous = dict(state)
        apply_halt_request(state, self.path, datetime.now(timezone.utc))
        if state != previous:
            self._save()
        return state

    def step(self, signal: dict | None, quote: dict, risk_settings: dict) -> dict:
        try:
            maximum = risk_settings.get("max_consecutive_losses", 2)
            if not isinstance(maximum, int) or maximum < 1:
                raise ExecutionError("Invalid consecutive-loss limit")
            self.state["max_consecutive_losses"] = maximum
            discipline = self._discipline()
            if self.state["phase"] not in {"idle", "closed"}:
                recovered = self.recover()
                if not recovered["ok"]:
                    return recovered
                if recovered.get("pnl"):
                    return recovered
                if self.state["phase"] == "open":
                    if discipline["halted_reason"]:
                        result = self.flatten_owned()
                        result["trigger"] = discipline["halted_reason"]
                        return result
                    trade = self.state["trade"]
                    bid = dec(quote.get("bid"))
                    account = self._request("GET", "/account")
                    baseline = self._daily_baseline(account)
                    daily_hit = baseline > 0 and dec(account.get("equity")) <= baseline * (
                        1 - min(dec(risk_settings.get("daily_loss_fraction", ".01")), Decimal(".01")))
                    opened_at = datetime.fromisoformat(trade.get("opened_at", datetime.now(timezone.utc).isoformat()))
                    holding_age = (datetime.now(timezone.utc) - opened_at).total_seconds()
                    max_holding = float(risk_settings.get("max_holding_seconds", 28800))
                    holding_hit = max_holding > 0 and holding_age >= max_holding
                    if daily_hit or holding_hit or (bid > 0 and (bid <= dec(trade["stop"]) or bid >= dec(trade["target"]))):
                        result = self.flatten_owned()
                        result["trigger"] = "daily_loss" if daily_hit else "max_holding" if holding_hit else "price_exit"
                        return result
                    return self._result(action="holding")
            if discipline["halted_reason"]:
                return self._result(action="entry_skipped", reason=discipline["halted_reason"])
            if not signal:
                return self._result(action="no_signal")
            if signal.get("direction", "long") != "long":
                raise EntrySkipped("Alpaca BTC/USD spot execution cannot open a short")
            if symbol_key(signal.get("symbol", "BTC/USD")) != "BTCUSD":
                raise ExecutionError("Only BTC/USD is supported")
            if str(signal["id"]) in self.state.get("seen_signals", []):
                return self._result(action="duplicate_signal")
            ask = dec(quote.get("ask"))
            stop, target = dec(signal["stop"]), dec(signal["target"])
            if not (0 < stop < ask < target):
                raise EntrySkipped("Signal must have stop below entry and target above entry")
            account = self._request("GET", "/account")
            risk_fraction = min(dec(risk_settings.get("risk_fraction", ".0025")), Decimal(".0025"))
            if risk_fraction <= 0:
                raise ExecutionError("Risk fraction must be positive")
            cost_fraction = max(dec(signal.get("expected_cost_fraction", ".005")),
                                dec(risk_settings.get("fee_rate", ".0025")) * 2)
            risk_per_unit = ask - stop + ask * cost_fraction
            budget = dec(account.get("equity")) * risk_fraction
            cap = min(self.max_notional, dec(risk_settings.get("max_notional", self.max_notional)))
            qty = self._rounded(min(budget / risk_per_unit, cap / ask))
            if qty < dec(self._asset()["min_order_size"]):
                raise EntrySkipped("Risk-sized quantity is below exchange minimum")
            notional = (qty * ask).quantize(Decimal(".01"), rounding=ROUND_DOWN)
            self._entry_checks(notional, quote, risk_settings)
            # A notional market buy caps the entry amount even if the fill price
            # moves while the request is in flight; a quantity order cannot.
            return self._start(signal, notional=notional)
        except EntrySkipped as exc:
            return self._result(True, "entry_skipped", reason=str(exc))
        except Exception as exc:
            return self._fail(exc)

    def flatten_owned(self):
        try:
            if not self.state.get("trade") or self.state["phase"] in {"idle", "closed"}:
                return self._result(action="nothing_owned")
            self._assert_orders_owned()
            for role in list(self._known_orders()):
                self._refresh(role)
            entry = self._known_orders().get("entry")
            if entry is None:
                raise ExecutionError("Missing entry intent; ownership cannot be established")
            if entry["status"] not in TERMINAL:
                self._cancel_confirm("entry")
            # Entry can settle during shutdown/restart, so establish net ownership
            # only if it was not previously recorded by the completed entry path.
            if self.state["phase"] == "entering" and dec(entry.get("filled_qty")) > 0:
                self._owned_position(establishing=True)
            self._owned_position()
            self.state["phase"] = "exiting"
            self._save()
            for role in list(self._known_orders()):
                if role != "entry":
                    self._cancel_confirm(role)
            position, quantity = self._owned_position()
            if quantity == 0:
                self.state["phase"] = "closed"
                self._save()
                return self._note_closed(self._result(action="closed", owned_order_ids={r: o.get("id") for r, o in self._known_orders().items()}))
            available = dec(position.get("qty_available", quantity))
            if available < quantity - dec(self._asset()["min_trade_increment"]):
                raise ExecutionError("Owned quantity is still reserved; no sell submitted")
            qty = self._rounded(min(quantity, available))
            if qty < dec(self._asset()["min_order_size"]):
                self.state["phase"] = "dust"
                self._save()
                return self._result(False, "dust_remaining", quantity=str(quantity),
                                    reason="Owned residual is below the exchange minimum; no new entries")
            # New role only after every previous sell is definitively terminal.
            count = sum(1 for role in self._known_orders() if role.startswith("exit"))
            role = "exit" + str(count + 1)
            order = self._submit(role, {"symbol": "BTC/USD", "side": "sell", "type": "market",
                                        "time_in_force": "gtc", "qty": str(qty)})
            order = self._wait(role)
            if order["status"] not in TERMINAL:
                self._cancel_confirm(role)
            _, residual = self._owned_position()
            if residual == 0:
                self.state["phase"] = "closed"
                self._save()
                return self._note_closed(self._result(action="closed", exit_order_id=order.get("id"),
                                                      owned_order_ids={r: o.get("id") for r, o in self._known_orders().items()}))
            self.state["phase"] = "dust" if residual < dec(self._asset()["min_order_size"]) else "exiting"
            self._save()
            return self._result(False, "residual_remaining", quantity=str(residual), exit_order_id=order.get("id"),
                                reason="Exit did not fully flatten owned quantity; recover/flatten required")
        except Exception as exc:
            return self._fail(exc)

    def shutdown(self):
        return self.flatten_owned()

    def smoke(self, notional: float = 10.0):
        """Explicit paper-only round trip; never manufacture a strategy signal."""
        try:
            if self.state["phase"] not in {"idle", "closed"}:
                raise ExecutionError("Smoke test requires no active owned trade")
            amount = dec(notional)
            self._entry_checks(amount)
            signal = {"id": "smoke-" + uuid.uuid4().hex, "stop": "0", "target": "0"}
            opened = self._start(signal, notional=amount, mode="smoke")
            if not opened["ok"]:
                return opened
            if opened["action"] != "opened":
                return self._result(False, "smoke_unfilled", entry=opened,
                                    reason="Smoke entry had no fill; broker execution was not verified")
            closed = self.flatten_owned()
            return self._result(closed["ok"], "smoke_complete" if closed["ok"] else "smoke_incomplete",
                                entry_order_id=opened.get("entry_order_id"),
                                stop_order_id=opened.get("stop_order_id"), exit=closed)
        except Exception as exc:
            return self._fail(exc)
