import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from futures_probe import __main__ as cli


class ProbeSequenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(cli, "REPORTS", Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_market(self, gate, hours_ago=0):
        report = {
            "model_gate_passed": gate,
            "checked_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)).isoformat(),
        }
        (cli.REPORTS / "market-latest.json").write_text(json.dumps(report), encoding="utf-8")

    @patch("futures_probe.model.run_model_probe")
    def test_missing_market_result_cannot_call_model(self, probe):
        self.assertEqual(cli.check_model({})["error_code"], "MARKET_VALIDATION_REQUIRED")
        probe.assert_not_called()

    @patch("futures_probe.model.run_model_probe")
    def test_partial_market_result_cannot_call_model(self, probe):
        self.write_market(False)
        self.assertEqual(cli.check_model({})["error_code"], "MARKET_VALIDATION_REQUIRED")
        probe.assert_not_called()

    @patch("futures_probe.model.run_model_probe")
    def test_old_market_result_cannot_call_model(self, probe):
        self.write_market(True, hours_ago=25)
        self.assertEqual(cli.check_model({})["error_code"], "MARKET_VALIDATION_REQUIRED")
        probe.assert_not_called()

    @patch("futures_probe.model.run_model_probe")
    @patch("futures_probe.secrets.get_secret", return_value=None)
    def test_missing_key_after_market_pass_does_not_call_model(self, _secret, probe):
        self.write_market(True)
        self.assertEqual(cli.check_model({})["error_code"], "MISSING_MODEL_API_KEY")
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
