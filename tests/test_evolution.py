from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from futures_app.evolution import StrategyEvolution, _live_quote_valid, _new_book, _paper_step
from futures_app.strategy import default_strategy


def config():
    return {"account": {"initial_balance_cny": 1000000},
            "simulation_policy": {"fee_rates": {"open": 0.00005, "close_today": 0.0005, "close_previous": 0.00005},
                "broker_commission_cny_per_side": 20, "slippage_ticks": 2, "margin_rate": 0.2,
                "assumption_version": "test-cost-v1", "assumption_label": "synthetic assumptions"},
            "evolution": {"minimum_historical_bars": 600, "review_interval_hours": 24,
                          "min_validation_trades": 10, "min_shadow_days": 3, "min_shadow_trades": 10}}


def bars():
    end = int(time.time() // 300) * 300 - 300
    return [{"datetime": end - (600 - i) * 60, "open": 4000, "high": 4002, "low": 3998,
             "close": 4001, "volume": 10, "completed": True} for i in range(600)]


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="futures-evolution-test-")
        self.addCleanup(self.directory.cleanup)
        self.evolution = StrategyEvolution(Path(self.directory.name) / "test.sqlite3", config())
        self.analyst = Mock()
        self.analyst.propose_parameters.return_value = {"status": "ok", "result": {
            "reason": "synthetic hypothesis", "changes": [{"parameter": "entry_ma", "value": 6}]}}

    def test_insufficient_history_never_calls_model_or_changes_current(self):
        result = self.evolution.maybe_review([], bars()[:100], "synthetic", self.analyst)
        self.assertEqual(result["status"], "pending_history")
        self.analyst.propose_parameters.assert_not_called()
        self.assertEqual(self.evolution.current_strategy()["version"], "trend-v1")

    def test_proposer_cannot_see_holdout_and_low_trade_sample_never_promotes(self):
        history = bars()
        metric = {"net_pnl": 5000, "gross_pnl": 5500, "fees": 500, "trades": 2, "wins": 2, "max_drawdown": 100}
        with patch("futures_app.evolution.evaluate_historical", return_value=metric):
            result = self.evolution.maybe_review([], history, "synthetic", self.analyst)
        training = self.analyst.propose_parameters.call_args.args[0]
        candidate = self.evolution.state()["candidates"][0]
        self.assertLess(max(row["datetime"] for row in training["training_bars"]), candidate["metrics"]["validation_start"])
        self.assertEqual(result["status"], "pending_validation_samples")
        self.assertEqual(self.evolution.current_strategy()["version"], "trend-v1")

    def test_failed_or_out_of_bounds_proposal_cannot_upgrade(self):
        self.analyst.propose_parameters.return_value = {"status": "ok", "result": {
            "changes": [{"parameter": "max_position_contracts", "value": 2}]}}
        result = self.evolution.maybe_review([], bars(), "synthetic", self.analyst)
        self.assertEqual(result["status"], "candidate_rejected")
        self.assertFalse(self.evolution.state()["candidates"])

    def test_historical_success_only_starts_shadow_and_stale_quotes_do_not_count(self):
        baseline = {"net_pnl": 100, "gross_pnl": 600, "fees": 500, "trades": 20, "wins": 10, "max_drawdown": 500}
        candidate = {**baseline, "net_pnl": 1000, "max_drawdown": 300}
        with patch("futures_app.evolution.evaluate_historical", side_effect=[baseline, candidate]):
            result = self.evolution.maybe_review([], bars(), "synthetic", self.analyst)
        self.assertEqual(result["status"], "shadow")
        self.evolution.observe({"datetime": time.time() - 100, "symbol": "CFFEX.IF2611", "bid_price1": 4000, "ask_price1": 4000.2}, [], [])
        self.assertEqual(self.evolution.state()["candidates"][0]["shadow"]["ticks"], 0)
        self.assertEqual(self.evolution.current_strategy()["version"], "trend-v1")

    def test_gap_stop_uses_executable_quote_with_adverse_slippage(self):
        book = _new_book()
        book["position"] = {"side": "long", "quantity": 1, "entry_price": 4000,
            "entry_fee": 80, "stop_loss": 3990, "take_profit": 4020, "opened_day": "2000-01-01"}
        quote = {"datetime": time.time(), "symbol": "CFFEX.IF2611", "bid_price1": 3980, "ask_price1": 3980.2}
        _paper_step(book, default_strategy(), quote, [], [], config()["simulation_policy"], {})
        self.assertIsNone(book["position"])
        self.assertAlmostEqual(book["gross_pnl"], (3979.6 - 4000) * 300)
        self.assertGreater(book["fees"], 0)

    def test_shadow_requires_valid_unexpired_contract_and_executable_market(self):
        now = time.time()
        quote = {"datetime": now, "symbol": "CFFEX.IF2611", "expired": False,
                 "expire_datetime": now + 86400, "price_tick": 0.2, "volume_multiple": 300,
                 "session_open": True, "calendar_verified": True, "bid_volume1": 1, "ask_volume1": 1,
                 "last_price": 4000, "bid_price1": 3999.8, "ask_price1": 4000.2,
                 "lower_limit": 3600, "upper_limit": 4400}
        self.assertTrue(_live_quote_valid(quote, now))
        for update in ({"expire_datetime": None}, {"expired": True}, {"expire_datetime": now - 1},
                       {"ask_volume1": 0}, {"bid_volume1": None}, {"symbol": "KQ.m@CFFEX.IF"},
                       {"ask_price1": 4400.2}, {"calendar_verified": False}, {"lower_limit": None}):
            self.assertFalse(_live_quote_valid({**quote, **update}, now))

    def test_price_limit_prevents_impossible_shadow_exit(self):
        book = _new_book()
        book["position"] = {"side": "long", "quantity": 1, "entry_price": 4000,
            "entry_fee": 80, "stop_loss": 3990, "take_profit": 4020, "opened_day": "2000-01-01"}
        quote = {"datetime": time.time(), "symbol": "CFFEX.IF2611", "bid_price1": 3980,
                 "ask_price1": 3980.2, "lower_limit": 3980, "upper_limit": 4400}
        _paper_step(book, default_strategy(), quote, [], [], config()["simulation_policy"], {})
        self.assertIsNotNone(book["position"])
        self.assertEqual(book["trades"], 0)

    def test_promotion_and_rollback_keep_old_version_available(self):
        baseline = {"net_pnl": 100, "gross_pnl": 600, "fees": 500, "trades": 20, "wins": 10, "max_drawdown": 500}
        candidate = {**baseline, "net_pnl": 1000, "max_drawdown": 300}
        with patch("futures_app.evolution.evaluate_historical", side_effect=[baseline, candidate]):
            self.evolution.maybe_review([], bars(), "synthetic", self.analyst)
        row = self.evolution.state()["candidates"][0]
        shadow = row["shadow"]
        from datetime import datetime, timedelta
        from futures_app.strategy import CHINA
        today = datetime.now(CHINA).date()
        shadow["days"] = [(today - timedelta(days=n)).isoformat() for n in (2, 1, 0)]
        for name, pnl, drawdown in (("baseline", 100, 500), ("candidate", 1000, 300)):
            shadow[name].update(trades=10, realized_net=pnl, net_pnl=pnl, peak_pnl=pnl, max_drawdown=drawdown)
        with self.evolution._db() as db:
            db.execute("UPDATE strategy_candidates SET shadow_json=? WHERE version=?", (json.dumps(shadow), row["version"]))
        now = time.time() + 0.1
        quote = {"datetime": now, "symbol": "CFFEX.IF2611", "expired": False,
                 "expire_datetime": now + 86400, "price_tick": 0.2, "volume_multiple": 300,
                 "session_open": True, "calendar_verified": True, "bid_volume1": 1, "ask_volume1": 1,
                 "last_price": 4000, "bid_price1": 3999.8, "ask_price1": 4000.2,
                 "lower_limit": 3600, "upper_limit": 4400}
        self.evolution.observe(quote, [], [])
        self.assertEqual(self.evolution.current_strategy()["version"], row["version"])
        self.assertEqual(self.evolution.strategy_for_version("trend-v1")["version"], "trend-v1")
        shadow = self.evolution.state()["candidates"][0]["shadow"]
        shadow["candidate"].update(trades=20, realized_net=700)
        shadow["baseline"].update(trades=20, realized_net=200)
        with self.evolution._db() as db:
            db.execute("UPDATE strategy_candidates SET shadow_json=? WHERE version=?", (json.dumps(shadow), row["version"]))
        self.evolution.observe({**quote, "datetime": now + 0.1}, [], [])
        self.assertEqual(self.evolution.current_strategy()["version"], "trend-v1")
        self.assertEqual(self.evolution.state()["candidates"][0]["status"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
