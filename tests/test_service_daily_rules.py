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
from futures_app.broker import PaperBroker
from futures_app.service import TradingService
from futures_app.strategy import build_context
from test_daily_rules import daily_result, market_snapshot
from test_strategy import make_bars


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
        self.position = None
        self.executions = []
        self.quotes = 0

    def snapshot(self, _quote=None): return {"account": {}, "position": copy.deepcopy(self.position)}
    def on_quote(self, _quote):
        self.quotes += 1
        return self.snapshot()

    def execute(self, decision, _quote, _indicators=None):
        self.executions.append(copy.deepcopy(decision))
        return {"status": "executed", "reason": "synthetic"}

    def record_event(self, *_args): pass
    def close(self): pass


class Analyst:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def generate_daily_rules(self, _context, _key):
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=5)
        return daily_result()

    def analyze(self, *_args):
        raise AssertionError("Daily mode must execute rules directly")


class GlobalFeed:
    def collect(self): return market_snapshot()


class Calendar:
    def context(self):
        return {"session_open": True, "session": "synthetic", "calendar_verified": True,
                "is_trading_day": True, "known_events_note": "unknown"}


class DailyServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        config["daily_rules"]["enabled"] = True
        config["automatic_trading_enabled"] = False
        config["model"]["analysis_interval_seconds"] = 0.1
        config["app"]["review_time"] = "23:59"
        config["evolution"]["enabled"] = False
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        (self.root / "calendar.json").write_bytes((ROOT / "calendar.json").read_bytes())
        self.feed, self.broker, self.analyst = Feed(), Broker(), Analyst()
        self.service = self.make_service()
        self.bar = 1_800_000_000
        self.signal = "long"
        patcher = patch("futures_app.service.get_secret", return_value="synthetic-key")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("futures_app.service.build_context", side_effect=self.build_context)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.stop)

    def make_service(self):
        service = TradingService(self.root, feed=self.feed, broker=self.broker,
                                 analyst=self.analyst, global_feed=GlobalFeed())
        service.calendar = Calendar()
        return service

    def stop(self):
        self.analyst.release.set()
        self.service.stop()

    def build_context(self, quote, _one, _five, broker, _calendar, strategy):
        return {"quote": {**quote, "ask_price1": 4000, "bid_price1": 3999.8, "price_tick": 0.2},
                "strategy": strategy, "broker": broker, "data_ready": True, "signal": self.signal,
                "eligible_actions": ["hold", "close"] if broker["position"] else ["hold", "open_long", "open_short"],
                "indicators": {"atr1": 5, "last_closed_1m": {"datetime": self.bar}}}

    def finish_generation(self):
        self.analyst.release.set()
        for thread in self.service._threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_start_generates_and_slow_model_keeps_risk_loop_live(self):
        self.service.control("start")
        self.service.start()
        self.assertTrue(self.analyst.entered.wait(timeout=2))
        self.assertEqual(self.service.request_analysis(signal_only=True)["status"], "skipped")
        before = self.broker.quotes
        self.feed.quote["datetime"] = now_china().isoformat()
        deadline = time.monotonic() + 2
        while self.broker.quotes <= before and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreater(self.broker.quotes, before)
        self.assertEqual(self.broker.executions, [])
        self.service.control("pause")
        self.finish_generation()
        self.assertEqual(self.service.daily_rules.state(now_china().date().isoformat())["status"], "ready")
        self.assertEqual(self.service.request_analysis(signal_only=True)["status"], "skipped")

    def test_ready_rules_execute_without_model_and_reuse_after_restart(self):
        self.service.request_daily_rules()
        self.finish_generation()
        self.service.control("start")
        self.assertEqual(self.service.request_analysis(signal_only=True)["status"], "executed")
        self.assertEqual(self.broker.executions[0]["origin"], "rules")
        self.assertEqual(self.broker.executions[0]["rule_plan"]["parameters"]["min_trend_atr"], 0.2)
        self.assertEqual(self.service.request_analysis(signal_only=True)["status"], "skipped")
        self.service.stop()
        self.service = self.make_service()
        self.assertEqual(self.service.request_daily_rules()["status"], "ok")
        self.assertEqual(self.service.request_analysis(signal_only=True)["status"], "skipped")
        self.assertEqual(self.analyst.calls, 1)
        self.bar += 60
        self.assertEqual(self.service.request_analysis(signal_only=True)["status"], "executed")

    def test_daily_signal_flows_through_real_indicators_and_paper_broker(self):
        self.broker = PaperBroker(self.service.db_path, self.service.config)
        self.service.broker = self.broker
        current = dt.datetime(2026, 11, 17, 10, tzinfo=now_china().tzinfo)
        self.broker._clock = lambda: current
        quote = {"symbol": "CFFEX.IF2612", "datetime": current.isoformat(),
                 "last_price": 4060, "bid_price1": 4059.8, "ask_price1": 4060.2,
                 "bid_volume1": 10, "ask_volume1": 10, "price_tick": 0.2, "volume_multiple": 300,
                 "upper_limit": 5000, "lower_limit": 3000, "expired": False,
                 "expire_datetime": dt.datetime(2026, 12, 18, 15, tzinfo=current.tzinfo).timestamp()}
        one = make_bars(current.timestamp() - 60 * 60, 60)
        for bar in one:
            bar.update(high=bar["open"] + 3, low=bar["open"] - 3)
        market = {"status": "connected", "quote": quote, "bars_1m": one,
                  "bars_5m": make_bars(current.timestamp() - 50 * 300, 50, 300)}
        with patch("futures_app.service.now_china", return_value=current), \
                patch("futures_app.service.build_context", wraps=build_context), \
                patch.object(self.feed, "snapshot", return_value=market):
            self.service.request_daily_rules()
            self.finish_generation()
            self.service.control("start")
            result = self.service.request_analysis(signal_only=True)
        self.assertEqual(result["status"], "executed", result["reason"])
        self.assertEqual(result["position"]["entry_price"], 4060.6)
        self.assertEqual(result["position"]["stop_loss"], 4049.8)
        self.assertEqual(result["position"]["take_profit"], 4082.2)
        self.assertTrue(result["position"]["strategy_version"].startswith("trend-v2-20261117-"))
        self.assertEqual(self.service.ai_state["last_decision"]["stop_loss"], 4049.8)
        self.assertEqual(self.analyst.calls, 1)

    def test_old_position_uses_opening_version_while_daily_generation_waits(self):
        self.broker.position = {"strategy_version": "trend-v1", "side": "short", "quantity": 1, "opened_at": "synthetic"}
        self.service.control("start")
        self.service.request_daily_rules()
        self.assertTrue(self.analyst.entered.wait(timeout=2))
        self.assertEqual(self.service.request_analysis(signal_only=True)["status"], "executed")
        self.assertEqual(self.broker.executions[0]["action"], "close")
        self.assertEqual(self.broker.executions[0]["strategy_version"], "trend-v1")
        self.finish_generation()

    def test_missing_global_data_activates_base_and_next_day_generates_again(self):
        with patch.object(self.service.global_feed, "collect", return_value={"available_count": 0}):
            self.service.request_daily_rules()
            self.finish_generation()
        day = now_china().date().isoformat()
        self.assertEqual(self.service.daily_rules.state(day)["status"], "fallback")
        self.assertEqual(self.analyst.calls, 0)
        next_day = now_china() + dt.timedelta(days=1)
        with patch("futures_app.service.now_china", return_value=next_day):
            self.service.request_daily_rules()
            self.finish_generation()
        self.assertEqual(self.service.daily_rules.state(next_day.date().isoformat())["status"], "ready")
        self.assertEqual(self.analyst.calls, 1)
