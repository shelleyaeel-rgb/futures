from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from futures_app.ai import LunaAnalyst, validate_decision


def context():
    return {"data_ready": True, "strategy": {"version": "test-v1"},
            "quote": {"ask_price1": 4000, "bid_price1": 3999.8, "price_tick": 0.2},
            "eligible_actions": ["hold", "open_long"]}


def decision():
    return {"action": "open_long", "stop_loss": 3990, "take_profit": 4020,
            "reason": "synthetic decision", "risk_level": "medium", "closure_assessment": "unknown events",
            "strategy_version": "test-v1"}


def response(value=None, status="completed"):
    return {"status": status, "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps(value or decision())}]}],
        "usage": {"input_tokens": 100, "output_tokens": 40, "output_tokens_details": {"reasoning_tokens": 30}}}


class FakeOpener:
    def __init__(self, data):
        self.data = data
        self.request = None

    def open(self, request, timeout):
        self.request = request
        return io.BytesIO(json.dumps(self.data).encode())


class AnalystTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="futures-ai-test-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test.sqlite3"
        self.analyst = LunaAnalyst(self.path, {"model": "gpt-6-luna", "daily_budget_usd": 2})

    def test_structured_call_and_reasoning_cost_are_counted_once(self):
        opener = FakeOpener(response())
        with patch("urllib.request.build_opener", return_value=opener):
            result = self.analyst.analyze(context(), "synthetic-key-only")
        self.assertEqual(result["status"], "ok")
        request = json.loads(opener.request.data)
        self.assertFalse(request["store"])
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertEqual(request["reasoning"], {"effort": "low"})
        usage = self.analyst.usage_today()
        self.assertEqual(usage["output_tokens"], 40)
        self.assertEqual(usage["reasoning_tokens"], 30)
        self.assertAlmostEqual(usage["spent_usd"], 0.00003)

    def test_timeout_retains_budget_and_does_not_echo_exception(self):
        with patch("urllib.request.build_opener") as factory:
            factory.return_value.open.side_effect = urllib.error.URLError("synthetic-secret-do-not-echo")
            result = self.analyst.analyze(context(), "synthetic-key-only")
        self.assertEqual(result["action"], "hold")
        self.assertNotIn("synthetic-secret", json.dumps(result))
        usage = self.analyst.usage_today()
        self.assertGreater(usage["reserved_or_uncertain_usd"], 0)
        self.assertEqual(usage["requests"], 1)

    def test_atomic_reservation_across_independent_instances(self):
        config = {"model": "gpt-6-luna", "daily_budget_usd": 0.002}
        first, second = LunaAnalyst(self.path, config), LunaAnalyst(self.path, config)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda analyst: analyst._reserve("test", b"x" * 1024, 2048, (0.1, 0.5)), (first, second)))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertLessEqual(first.usage_today()["accounted_usd"], 0.002)

    def test_invalid_direction_nonfinite_and_wrong_version_are_rejected(self):
        for update in ({"stop_loss": 4010}, {"take_profit": float("nan")}, {"strategy_version": "unrequested"}, {"stop_loss": 3990.1}):
            with self.assertRaises(ValueError):
                validate_decision({**decision(), **update}, context())

    def test_incomplete_response_and_refusal_become_hold(self):
        incomplete = response(status="incomplete")
        refusal = response()
        refusal["output"][0]["content"] = [{"type": "refusal", "refusal": "synthetic refusal"}]
        for value in (incomplete, refusal):
            with patch("urllib.request.build_opener", return_value=FakeOpener(value)):
                result = self.analyst.analyze(context(), "synthetic-key-only")
            self.assertEqual(result["action"], "hold")

    def test_unknown_model_and_missing_history_do_not_call_network(self):
        analyst = LunaAnalyst(self.path, {"model": "unknown-model"})
        with patch("urllib.request.build_opener") as factory:
            result = analyst.analyze(context(), "synthetic-key-only")
            empty = self.analyst.analyze({"data_ready": False, "eligible_actions": ["hold"]}, "synthetic-key-only")
        self.assertEqual(result["error_code"], "MODEL_PRICING_UNKNOWN")
        self.assertEqual(empty["error_code"], "INSUFFICIENT_DATA")
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
