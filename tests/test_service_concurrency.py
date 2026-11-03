"""Model latency or a pause must never disable the independent quote/risk loop."""
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from futures_app.common import ROOT, now_china
from futures_app.service import TradingService
from futures_app.strategy import default_strategy


class Feed:
    def __init__(self):
        self.quote = {"symbol": "CFFEX.IF2612", "datetime": now_china().isoformat(), "last_price": 4000}

    def start(self, _symbol): pass
    def stop(self): pass
    def watch_position(self, _symbol): pass

    def snapshot(self):
        return {"status": "connected", "quote": copy.deepcopy(self.quote), "bars_1m": [{}], "bars_5m": [{}]}


class Broker:
    def __init__(self):
        self.quotes = 0
        self.executions = []

    def snapshot(self, _quote=None): return {"account": {}, "position": None}
    def on_quote(self, _quote):
        self.quotes += 1
        return self.snapshot()

    def execute(self, decision, _quote, _indicators=None):
        self.executions.append(decision)
        return {"status": "executed"}

    def record_event(self, *_args): pass
    def close(self): pass


class Analyst:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.contexts = []
        self.action = "open_long"

    def analyze(self, _context, _key):
        self.contexts.append(copy.deepcopy(_context))
        self.entered.set()
        self.release.wait(timeout=5)
        return {"status": "ok", "action": self.action, "reason": "synthetic-only", "strategy_version": "test-v1"}


class Evolution:
    def current_strategy(self): return {"version": "test-v1"}
    def observe(self, *_args): pass


class Calendar:
    def context(self):
        return {"session_open": True, "session": "synthetic session", "calendar_verified": True,
                "is_trading_day": False, "known_events_note": "unknown"}


class ServiceConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.config["automatic_trading_enabled"] = False
        (root / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        (root / "calendar.json").write_bytes((ROOT / "calendar.json").read_bytes())
        self.feed, self.broker, self.analyst = Feed(), Broker(), Analyst()
        self.service = TradingService(root, feed=self.feed, broker=self.broker, analyst=self.analyst, evolution=Evolution())
        self.service.calendar = Calendar()
        patcher = patch("futures_app.service.get_secret", return_value="synthetic-key-not-real")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.context = {"strategy": {**default_strategy(), "version": "test-v1"},
                        "eligible_actions": ["hold", "open_long"], "data_ready": True,
                        "signal": "long", "broker": {"position": None},
                        "quote": {"symbol": "CFFEX.IF2612", "ask_price1": 4000.2, "bid_price1": 4000, "price_tick": 0.2},
                        "indicators": {"atr1": 5, "last_closed_1m": {"datetime": 1800000000}}}
        patcher = patch("futures_app.service.build_context", side_effect=lambda *_args: copy.deepcopy(self.context))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.stop)

    def stop(self):
        self.analyst.release.set()
        self.service.stop()

    def test_slow_model_does_not_stop_quotes_and_pause_cancels_entry(self):
        self.service.control("start")
        self.service.start()
        self.assertTrue(self.analyst.entered.wait(timeout=2))
        before = self.broker.quotes
        self.feed.quote.update(datetime=now_china().isoformat(), last_price=3999)
        deadline = time.monotonic() + 2
        while self.broker.quotes <= before and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreater(self.broker.quotes, before)
        self.service.control("pause")
        self.analyst.release.set()
        deadline = time.monotonic() + 2
        while self.service.runtime["model_busy"] and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.broker.executions, [])

    def finish_analysis(self):
        self.analyst.release.set()
        for thread in self.service._threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_no_signal_does_not_call_model_or_read_key(self):
        self.context.update(signal="observe", eligible_actions=["hold"])
        with patch("futures_app.service.get_secret") as secret:
            self.assertEqual(self.service.request_analysis(signal_only=True)["status"], "skipped")
            secret.assert_not_called()
        self.assertEqual(self.analyst.contexts, [])

    def test_signal_review_is_deduplicated_until_new_closed_bar(self):
        self.service.control("start")
        self.analyst.action = "hold"
        self.assertEqual(self.service.request_analysis(signal_only=True)["status"], "accepted")
        self.finish_analysis()
        self.assertEqual(self.analyst.contexts[0]["eligible_actions"], ["hold", "open_long"])
        self.assertEqual(self.service.request_analysis(signal_only=True)["status"], "skipped")
        saved = json.loads(self.service.db.execute("SELECT value FROM app_settings WHERE key='last_signal_review'").fetchone()[0])
        self.assertEqual(saved, self.service._last_signal_review)
        self.context["indicators"]["last_closed_1m"]["datetime"] += 60
        self.assertEqual(self.service.request_analysis(signal_only=True)["status"], "accepted")
        self.finish_analysis()
        self.assertEqual(len(self.analyst.contexts), 2)

    def test_changed_signal_during_review_prevents_execution(self):
        self.service.control("start")
        self.service.request_analysis(signal_only=True)
        self.assertTrue(self.analyst.entered.wait(timeout=2))
        self.context.update(signal="observe", eligible_actions=["hold"])
        self.finish_analysis()
        self.assertEqual(self.broker.executions, [])

    def test_position_reversal_only_requests_close_review(self):
        self.context["broker"]["position"] = {"side": "short", "opened_at": "synthetic"}
        self.context["eligible_actions"] = ["hold", "close"]
        self.analyst.action = "close"
        self.service.request_analysis(signal_only=True)
        self.finish_analysis()
        self.assertEqual(self.analyst.contexts[0]["eligible_actions"], ["hold", "close"])
        self.assertEqual(self.analyst.contexts[0]["local_proposal"]["action"], "close")

    def test_same_direction_position_and_blocked_entry_skip_model(self):
        self.context["broker"]["position"] = {"side": "long", "opened_at": "synthetic"}
        self.context["eligible_actions"] = ["hold", "close"]
        self.assertEqual(self.service.request_analysis(signal_only=True)["status"], "skipped")
        self.context["broker"]["position"] = None
        self.context["eligible_actions"] = ["hold"]
        self.assertEqual(self.service.request_analysis(signal_only=True)["status"], "skipped")
        self.assertEqual(self.analyst.contexts, [])

    def test_manual_analysis_remains_available_without_signal(self):
        self.context.update(signal="observe", eligible_actions=["hold"])
        self.assertEqual(self.service.request_analysis()["status"], "accepted")
        self.finish_analysis()
        self.assertEqual(len(self.analyst.contexts), 1)

    def test_quote_expiring_while_model_runs_prevents_execution(self):
        self.service.control("start")
        self.service.start()
        self.assertTrue(self.analyst.entered.wait(timeout=2))
        self.feed.quote["datetime"] = (now_china() - dt.timedelta(seconds=30)).isoformat()
        self.analyst.release.set()
        deadline = time.monotonic() + 2
        while self.service.runtime["model_busy"] and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.broker.executions, [])


if __name__ == "__main__":
    unittest.main()
