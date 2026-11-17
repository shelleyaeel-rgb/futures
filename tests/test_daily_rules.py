from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from futures_app.daily_rules import DailyRules, apply_daily_changes
from futures_app.evolution import StrategyEvolution
from futures_app.strategy import base_strategy_v2


def market_snapshot():
    return {"available_count": 1, "factors": [{"id": "vix", "status": "available", "value": 25}]}


def daily_result(changes=None):
    return {"status": "ok", "request_id": "synthetic-daily",
            "result": {"regime": "volatile", "summary": "波动上升，增加入场趋势要求。",
                       "changes": changes if changes is not None else [{"parameter": "min_trend_atr", "value": 0.2}],
                       "evidence": [{"factor": "vix", "interpretation": "波动率25。"}],
                       "limitations": ["新闻信息未知。"]}}


class DailyRulesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "daily.sqlite3"
        self.evolution = StrategyEvolution(self.path, {})
        self.settings = {"max_attempts_per_day": 3, "retry_interval_seconds": 300}
        self.rules = DailyRules(self.path, self.settings)
        self.day = "2026-11-17"

    def finish(self, response=None):
        token = self.rules.claim(self.day)
        self.assertIsNotNone(token)
        return self.rules.finish(self.day, token, market_snapshot(), response or daily_result())

    def test_same_day_restart_reuses_version_and_next_day_starts_from_base(self):
        self.assertFalse(self.rules.state(self.day)["active"])
        today = self.finish()
        self.assertEqual(today["status"], "ready")
        self.assertEqual(today["strategy"]["parameters"]["min_trend_atr"], 0.2)
        restarted = DailyRules(self.path, self.settings)
        self.assertIsNone(restarted.claim(self.day, force=True))
        self.assertEqual(restarted.state(self.day)["strategy"], today["strategy"])
        next_day = "2026-11-18"
        tomorrow = restarted.finish(next_day, restarted.claim(next_day), market_snapshot(), daily_result([]))
        self.assertNotEqual(tomorrow["strategy"]["version"], today["strategy"]["version"])
        self.assertEqual(tomorrow["strategy"]["parameters"], base_strategy_v2()["parameters"])
        self.assertEqual(self.evolution.strategy_for_version(today["strategy"]["version"]), today["strategy"])
        self.assertNotIn("execution", self.evolution.strategy_for_version("trend-v1"))

    def test_concurrent_claim_has_one_owner_and_old_lease_cannot_overwrite(self):
        other = DailyRules(self.path, self.settings)
        with ThreadPoolExecutor(max_workers=2) as pool:
            tokens = list(pool.map(lambda rules: rules.claim(self.day), (self.rules, other)))
        self.assertEqual(sum(token is not None for token in tokens), 1)
        old_token = next(token for token in tokens if token)
        with patch("futures_app.daily_rules.time.time", return_value=9_000_000_000):
            new_token = other.claim(self.day)
            self.assertIsNotNone(new_token)
            ready = other.finish(self.day, new_token, market_snapshot(), daily_result())
        old = self.rules.finish(self.day, old_token, market_snapshot(), {"status": "failed"})
        self.assertEqual(old["status"], "ready")
        self.assertEqual(old["strategy"], ready["strategy"])

    def test_failure_falls_back_retries_and_preserves_fallback_version(self):
        with patch("futures_app.daily_rules.time.time", return_value=1000):
            fallback = self.finish({"status": "failed", "error_code": "TIMEOUT"})
            self.assertTrue(fallback["active"])
            self.assertEqual(fallback["status"], "fallback")
            self.assertEqual(fallback["strategy"]["execution"], base_strategy_v2()["execution"])
            self.assertIsNone(self.rules.claim(self.day))
        with patch("futures_app.daily_rules.time.time", return_value=1300):
            ready = self.finish()
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["attempts"], 2)
        self.assertEqual(self.evolution.strategy_for_version(fallback["strategy"]["version"]), fallback["strategy"])

    def test_retry_limit_manual_retry_and_expired_generation_recovery(self):
        for attempt in range(3):
            with patch("futures_app.daily_rules.time.time", return_value=1000 + 300 * attempt):
                self.finish({"status": "failed"})
        with patch("futures_app.daily_rules.time.time", return_value=2000):
            self.assertIsNone(self.rules.claim(self.day))
            self.assertIsNotNone(self.rules.claim(self.day, force=True))
        with patch("futures_app.daily_rules.time.time", return_value=2200):
            self.assertIsNotNone(self.rules.claim(self.day))

    def test_invalid_results_fall_back_without_writing_untrusted_parameters(self):
        bad_results = [
            {"changes": [{"parameter": "max_contracts", "value": 2}]},
            {"changes": [{"parameter": "loss_cooldown_minutes", "value": 0}]},
            {"changes": [{"parameter": "minimum_net_reward_risk", "value": True}]},
            {"changes": [None]}, {"changes": [{"parameter": [], "value": 1}]},
            {"evidence": []}, {"evidence": [{"factor": "sp500", "interpretation": "missing"}]},
            {"regime": "unsupported"},
        ]
        for index, invalid in enumerate(bad_results):
            with self.subTest(invalid=invalid):
                response = daily_result()
                response["result"].update(invalid)
                day = f"2026-11-{index + 1:02d}"
                saved = self.rules.finish(day, self.rules.claim(day), market_snapshot(), response)
                self.assertEqual(saved["status"], "fallback")
                self.assertEqual(saved["error_code"], "DAILY_RULES_INVALID")
                self.assertEqual(saved["strategy"]["parameters"], self.rules.base["parameters"])

    def test_daily_changes_are_unique_bounded_and_do_not_mutate_base(self):
        base = base_strategy_v2()
        before = deepcopy(base)
        result = daily_result([{"parameter": "loss_cooldown_minutes", "value": 20},
                               {"parameter": "reward_risk", "value": 2.5}])["result"]
        strategy = apply_daily_changes(base, result)
        self.assertEqual(strategy["execution"]["loss_cooldown_minutes"], 20)
        self.assertEqual(base, before)
        for changes in ([{"parameter": "stop_atr", "value": 2}] * 2,
                        [{"parameter": "stop_atr", "value": 2}] * 3):
            with self.assertRaises(ValueError):
                apply_daily_changes(base, {"changes": changes})
