import base64
from contextlib import contextmanager, nullcontext
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from tred.cli import main
from tred.databento import Databento, _condition_map, _find_count, _request, export_trades
from tred.futures import MAX_RECORDS, load_bars
from tred.strategy import StrategyConfig

UTC = timezone.utc
START = datetime(2026, 10, 5, 13, 30, tzinfo=UTC)  # Monday.
END = START + timedelta(hours=3)
FIELDS = ["ts_recv", "ts_event", "rtype", "publisher_id", "instrument_id", "action", "side",
          "price", "size", "flags", "ts_in_delta", "sequence"]
KEY = "sekrit"


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def make_row(stamp, side="B", price="20150000000000", size="3", sequence=1):
    return {"ts_recv": iso(stamp), "ts_event": iso(stamp), "rtype": "0", "publisher_id": "1",
            "instrument_id": "4200001", "action": "T", "side": side, "price": price,
            "size": size, "flags": "0", "ts_in_delta": "0", "sequence": str(sequence)}


def csv_bytes(rows):
    return (",".join(FIELDS) + "\n" + "\n".join(",".join(row[field] for field in FIELDS) for row in rows) + "\n").encode()


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeServer:
    def __init__(self, rows, *, count=None, conditions=None, ids=("4200001",)):
        self.rows = rows
        self.count = len(rows) if count is None else count
        self.conditions = conditions
        self.ids = ids
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(url.split("?" )[0].rsplit("/", 1)[-1])
        self.assert_auth(headers)
        path = self.calls[-1]
        if path == "symbology.resolve":
            payload = {"result": {"MNQZ6": [{"d0": "2026-10-05", "d1": "2026-12-18", "s": value} for value in self.ids]}}
        elif path == "metadata.get_dataset_condition":
            conditions = self.conditions or {"2026-10-05": {"condition": "available"}}
            payload = {"result": {"GLBX.MDP3": conditions}}
        elif path == "metadata.get_record_count":
            payload = {"result": {"count": self.count}}
        elif path == "timeseries.get_range":
            return Response(csv_bytes(self.rows))
        else:
            raise AssertionError(f"unexpected endpoint {path}")
        return Response(json.dumps(payload).encode())

    def assert_auth(self, headers):
        assert headers["authorization"] == "Basic " + base64.b64encode((KEY + ":").encode()).decode()


def default_rows():
    return [make_row(START), make_row(START + timedelta(seconds=1), "A", size="2", sequence=2),
            make_row(START + timedelta(minutes=30), price="20151000000000", size="5", sequence=3),
            make_row(START + timedelta(minutes=31), "A", size="1", sequence=4)]


class FuturesFetchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "futures"

    def export(self, **changes):
        arguments = {"rows": default_rows(), "ids": ("4200001",)}
        arguments.update(changes)
        fake = FakeServer(**arguments)
        result = export_trades(KEY, "MNQZ6", iso(START), iso(END), self.output,
                               gap_seconds=300, client=Databento(KEY, request=fake))
        return fake, result

    def test_verified_export_publishes_tape_pair_the_loader_accepts(self):
        fake, result = self.export()
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(manifest["schema"], "tred.futures-tape.v1")
        self.assertEqual(manifest["format"], "databento")
        self.assertEqual(manifest["price_format"], "fixed9")
        self.assertEqual(manifest["instrument_id"], 4200001)
        self.assertIs(manifest["complete"], True)
        self.assertIn("Databento GLBX.MDP3", manifest["source"])
        self.assertEqual(manifest["provider_conditions"], {"2026-10-05": "available"})
        self.assertEqual(len(manifest["closed_intervals"]), 1)
        lower, upper = manifest["closed_intervals"][0]
        self.assertEqual(lower, iso(START + timedelta(seconds=1, microseconds=1)))
        self.assertEqual(upper, iso(START + timedelta(minutes=30)))
        self.assertEqual(result["records"], 4)
        self.assertIn("futures-compare", result["next"])
        self.assertNotIn(KEY, Path(result["manifest"]).read_text())
        self.assertNotIn(KEY, Path(result["trades"]).read_text())
        bars, info, closures = load_bars(Path(result["manifest"]),
                                         StrategyConfig(symbol="MNQZ6", row_size=.25, profile_bars=2))
        self.assertEqual(info["records"], 4)
        self.assertEqual(len(bars), 2)
        self.assertEqual(len(closures), 1)
        self.assertEqual(bars[0].footprint["delta"], 1)

    def test_validates_request_before_any_network_call(self):
        class Never:
            def __getattr__(self, name):
                raise AssertionError(f"network call attempted: {name}")

        for symbol, start, end, gap in (("mnqz6", START, END, 300), ("MNQ.c.0", START, END, 300),
                                        ("MNQZ6", START, START, 300), ("MNQZ6", START, START + timedelta(days=8), 300),
                                        ("MNQZ6", END, START, 300), ("MNQZ6", START, END, 299)):
            with self.subTest(symbol=symbol, gap=gap), self.assertRaises(ValueError):
                export_trades(KEY, symbol, iso(start), iso(end), self.output, gap_seconds=gap, client=Never())

    def test_record_budget_refusal_happens_before_download(self):
        with self.assertRaisesRegex(ValueError, "research budget"):
            self.export(count=MAX_RECORDS + 1)
        self.assertFalse(self.output.exists())

    def test_truncated_download_publishes_nothing(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.export(count=5)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_degraded_and_missing_conditions(self):
        conditions = {"2026-10-05": {"condition": "degraded"}}
        with self.assertRaisesRegex(ValueError, "degraded"):
            self.export(conditions=conditions)
        self.assertFalse(self.output.exists())
        self.export(conditions={"2026-10-05": {"condition": "missing"}})

    def test_multiple_instruments_in_window_are_refused(self):
        with self.assertRaisesRegex(ValueError, "multiple instruments"):
            self.export(ids=("4200001", "4200002"))

    def test_existing_destination_is_never_overwritten(self):
        self.output.mkdir()
        existing = self.output / "mnqz6-20261005-20261005-trades.csv"
        existing.write_text("original")
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(existing.read_text(), "original")
        self.assertEqual([path.name for path in self.output.iterdir()], ["mnqz6-20261005-20261005-trades.csv"])

    def test_decimal_prices_are_detected(self):
        rows = [make_row(START, price="20150.25", sequence=1),
                make_row(START + timedelta(seconds=1), "A", price="20150.00", size="2", sequence=2)]
        _, result = self.export(rows=rows)
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(manifest["price_format"], "decimal")
        bars, _, _ = load_bars(Path(result["manifest"]),
                               StrategyConfig(symbol="MNQZ6", row_size=.25, profile_bars=1))
        self.assertEqual(bars[0].close, 20150.0)

    def test_unknown_aggressor_side_publishes_nothing(self):
        rows = default_rows()
        rows[1]["side"] = "N"
        with self.assertRaisesRegex(ValueError, "unknown-side"):
            self.export(rows=rows)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_missing_or_placeholder_key_never_touches_network(self):
        def explode(*args, **kwargs):
            raise AssertionError("network call attempted")

        for key in ("", "   ", "YOUR_API_KEY"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "API key"):
                Databento(key, request=explode)

    def test_http_errors_report_status_without_the_key(self):
        error = urllib.error.HTTPError("https://hist.databento.com", 401, "Unauthorized", {},
                                       io.BytesIO(b'{"detail":"invalid api key"}'))
        with patch("urllib.request.urlopen", side_effect=error), self.assertRaises(ValueError) as raised:
            _request("GET", "https://hist.databento.com/v0/x", {}, None, 5)
        self.assertIn("401", str(raised.exception))
        self.assertIn("invalid api key", str(raised.exception))
        self.assertNotIn(KEY, str(raised.exception))

    def test_metadata_response_shapes_are_tolerated(self):
        self.assertEqual(_find_count(7), 7)
        self.assertEqual(_find_count({"count": 3}), 3)
        self.assertEqual(_find_count({"result": {"record_count": 9}}), 9)
        self.assertIsNone(_find_count({"result": {}}))
        self.assertEqual(_condition_map({"result": [{"dataset": "GLBX.MDP3", "date": "2026-10-05",
                                                     "condition": "Available"}]}),
                         {"2026-10-05": "available"})
        self.assertEqual(_condition_map({"GLBX.MDP3": {"2026-10-06": {"condition": "missing"}}}),
                         {"2026-10-06": "missing"})
        self.assertEqual(_condition_map({"weird": True}), {})


class FuturesFetchCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_cli_wires_limits_key_and_summary_without_broker_or_database(self):
        recorded, limits = {}, {}
        received = {}

        def export(*args, **kwargs):
            received["args"], received["kwargs"] = args, kwargs
            return {"manifest": "data/futures/mnqz6-coverage.json", "records": 4}

        @contextmanager
        def fake_limits(**kwargs):
            limits.update(kwargs)
            yield

        arguments = ["futures-fetch", "--symbol", "MNQZ6", "--start", iso(START), "--end", iso(END),
                     "--output", self.directory.name]
        with patch.dict(os.environ, {"DATABENTO_API_KEY": KEY}), \
                patch("tred.cli.load_env") as load_env, \
                patch("tred.research.research_limits", side_effect=fake_limits), \
                patch("tred.databento.export_trades", side_effect=export), \
                patch("tred.cli.Store", side_effect=AssertionError("crypto database opened")), \
                patch("tred.cli.settings", side_effect=AssertionError("crypto settings read")), \
                patch("tred.cli.emit", side_effect=lambda value: recorded.update(value)):
            self.assertEqual(main(arguments), 0)
        load_env.assert_called_once()
        self.assertEqual(limits, {"wall_seconds": 300})
        self.assertEqual(received["args"][:3], (KEY, "MNQZ6", iso(START)))
        self.assertEqual(received["kwargs"], {"gap_seconds": 300})
        self.assertEqual(recorded, {"manifest": "data/futures/mnqz6-coverage.json", "records": 4})

    def test_cli_refuses_bad_window_and_missing_key_before_exporting(self):
        arguments = ["futures-fetch", "--symbol", "MNQZ6", "--start", iso(START), "--end", iso(START)]
        with patch.dict(os.environ, {"DATABENTO_API_KEY": KEY}), \
                patch("tred.research.research_limits", return_value=nullcontext()), \
                patch("tred.cli.load_env"), \
                patch("tred.databento.Databento", side_effect=AssertionError("network client built")), \
                patch("tred.cli.Store", side_effect=AssertionError("crypto database opened")), \
                patch("tred.cli.emit") as emitted:
            self.assertEqual(main(arguments), 2)
        self.assertIn("seven days", emitted.call_args.args[0]["error"])
        environment = {name: value for name, value in os.environ.items() if name != "DATABENTO_API_KEY"}
        with patch.dict(os.environ, environment, clear=True), \
                patch("tred.research.research_limits", return_value=nullcontext()), \
                patch("tred.cli.load_env"), \
                patch("tred.databento.Databento", side_effect=AssertionError("network client built")), \
                patch("tred.cli.emit") as emitted:
            self.assertEqual(main(arguments[:-1] + [iso(END)]), 2)
        self.assertIn("DATABENTO_API_KEY", emitted.call_args.args[0]["error"])


if __name__ == "__main__":
    unittest.main()
