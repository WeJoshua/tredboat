import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tred.cli import main
from tred.equity_data import export_tape
from tred.equities import load_bars
from tred.strategy import StrategyConfig

UTC = timezone.utc
START = datetime(2026, 10, 5, 13, 30, tzinfo=UTC)  # Monday 09:30 New York.
END = START + timedelta(hours=2)
KEY, SECRET = "PK_TEST", "test-only-secret"


def raw_trade(stamp, price, size=100, identity=None, **extra):
    return {"t": stamp.isoformat().replace("+00:00", "Z"), "p": float(price), "s": size,
            "i": identity or str(int(stamp.timestamp() * 100)), "x": "V", "c": [], "z": "C", **extra}


class FakeDataClient:
    def __init__(self, pages, symbol="QQQ"):
        self.pages = pages
        self.symbol = symbol
        self.calls = []

    def trade_pages(self, symbol, start, end):
        for index, trades in enumerate(self.pages):
            self.calls.append(index)
            yield trades


class EquityExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "equity"

    def seeds(self):
        return [raw_trade(START, 511.23, 100, "1"),
                raw_trade(START + timedelta(seconds=1), 511.23, 50, "2"),
                raw_trade(START + timedelta(seconds=2), 511.30, 25, "3"),
                raw_trade(START + timedelta(minutes=30), 511.10, 10, "4"),
                raw_trade(START + timedelta(minutes=31), 511.05, 5, "5")]

    def export(self, **changes):
        pages = [self.seeds()]
        arguments = {"pages": pages, "symbol": "QQQ"}
        arguments.update(changes)
        client = FakeDataClient(arguments.pop("pages"), arguments.pop("symbol", "QQQ"))
        result = export_tape(KEY, SECRET, "QQQ", START.isoformat(), END.isoformat(), self.output,
                             feed="iex", gap_seconds=300, client=client)
        return client, result

    def test_export_publishes_declared_tape_the_loader_accepts(self):
        client, result = self.export()
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(manifest["schema"], "tred.equity-tape.v1")
        self.assertEqual(manifest["symbol"], "QQQ")
        self.assertEqual(manifest["feed"], "iex")
        self.assertEqual(manifest["side_inference"]["rule"], "tick_prior_price")
        self.assertEqual(len(manifest["closed_intervals"]), 1)
        lines = Path(result["trades"]).read_text().strip().splitlines()
        self.assertEqual(len(lines), 6)  # header + 5 trades
        first, second, third = lines[1].split(","), lines[2].split(","), lines[3].split(",")
        self.assertEqual((first[5], second[5], third[5]), ("B", "B", "B"))  # flat then uptick
        self.assertEqual(lines[4].split(",")[5], "S")
        self.assertNotIn(KEY, Path(result["manifest"]).read_text())
        self.assertNotIn(SECRET, Path(result["trades"]).read_text())
        bars, info, closures = load_bars(Path(result["manifest"]),
                                         StrategyConfig(symbol="QQQ", row_size=.01, profile_bars=1))
        self.assertEqual(info["records"], 5)
        self.assertEqual(len(bars), 2)
        self.assertEqual(len(closures), 1)

    def test_two_page_pagination_exhausts_and_marks_complete(self):
        seeds = self.seeds()
        client, result = self.export(pages=[seeds[:2], seeds[2:]])
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertIs(manifest["complete"], True)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result["records"], 5)

    def test_repeated_pagination_token_is_refused(self):
        from tred.equity_data import EquityMarketData
        pages = iter([[raw_trade(START + timedelta(seconds=1), 511.0, identity="1")],
                      [raw_trade(START + timedelta(seconds=2), 511.1, identity="2")]])
        def looping(method, path, payload=None, query=None):
            try:
                trades = next(pages)
            except StopIteration:
                trades = []
            return {"symbol": "QQQ", "trades": trades, "next_page_token": "SAME"}
        client = EquityMarketData("iex", KEY, SECRET)
        with patch("tred.equity_data.HttpClient.request", side_effect=looping), \
                self.assertRaisesRegex(ValueError, "incomplete"):
            list(client.trade_pages("QQQ", START, END))

    def test_bad_symbol_and_window_fail_before_publication(self):
        for symbol in ("QQQ/US", "1QQQ", "A" * 7, "QQ Q"):
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                export_tape(KEY, SECRET, symbol, START.isoformat(), END.isoformat(), self.output,
                            client=FakeDataClient([self.seeds()]))
        with self.assertRaisesRegex(ValueError, "seven days"):
            export_tape(KEY, SECRET, "QQQ", START.isoformat(), (START + timedelta(days=8)).isoformat(),
                        self.output, client=FakeDataClient([self.seeds()]))

    def test_flat_tape_and_empty_tape_are_refused(self):
        flat = [raw_trade(START + timedelta(seconds=i), 511.23, identity=str(i)) for i in range(4)]
        with self.assertRaisesRegex(ValueError, "never change"):
            self.export(pages=[flat])
        with self.assertRaisesRegex(ValueError, "no trades"):
            self.export(pages=[[]])

    def test_existing_destination_is_never_overwritten(self):
        self.output.mkdir()
        existing = self.output / "qqq-20261005-20261005-trades.csv"
        existing.write_text("original")
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(existing.read_text(), "original")

    def test_missing_credentials_rejected_before_network(self):
        def explode(*args, **kwargs):
            raise AssertionError("network call attempted")
        with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(ValueError, "credentials"):
            export_tape("", "", "QQQ", START.isoformat(), END.isoformat(), self.output, client=None)
        with patch.dict("os.environ", {"APCA_API_KEY_ID": KEY, "APCA_API_SECRET_KEY": SECRET}), \
                self.assertRaisesRegex(ValueError, "credentials"):
            export_tape("YOUR_API_KEY_ID", "", "QQQ", START.isoformat(), END.isoformat(), self.output, client=None)

    def test_cli_wires_credentials_and_limits_without_broker_or_database(self):
        received = {}
        with patch.dict("os.environ", {"APCA_API_KEY_ID": KEY, "APCA_API_SECRET_KEY": SECRET}), \
                patch("tred.cli.load_env") as load_env, \
                patch("tred.research.research_limits", return_value=__import__("contextlib").nullcontext()), \
                patch("tred.equity_data.EquityMarketData", side_effect=lambda *a, **k: {"sent": True, **k}) as md, \
                patch("tred.equity_data.export_tape", side_effect=lambda *a, **k: received.__setitem__("call", (a, k)) or {"manifest": "x"}) as export, \
                patch("tred.cli.Store", side_effect=AssertionError("crypto database opened")), \
                patch("tred.cli.settings", side_effect=AssertionError("crypto settings read")), \
                patch("tred.cli.emit"):
            self.assertEqual(main(["equity-fetch", "--symbol", "QQQ", "--start", START.isoformat(),
                                   "--end", END.isoformat(), "--output", str(self.output)]), 0)
        load_env.assert_called_once()
        self.assertEqual(received["call"][0][:3], (KEY, SECRET, "QQQ"))
        self.assertEqual(received["call"][1], {"feed": "iex", "gap_seconds": 300})
        md.assert_not_called()


if __name__ == "__main__":
    unittest.main()