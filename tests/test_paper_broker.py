"""Deterministic ledger/risk tests. No real or remote market orders are sent."""

import copy
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from futures_app.broker import PaperBroker, TZ


def configuration():
    return {
        "account": {"initial_balance_cny": 1_000_000, "max_position_contracts": 1,
                    "planned_risk_per_trade_cny": 5000, "daily_loss_stop_new_entries_cny": 20000},
        "simulation_policy": {
            "fee_rates": {"open": 0.00005, "close_today": 0.0005, "close_previous": 0.00005},
            "broker_commission_cny_per_side": 20, "slippage_ticks": 2,
            "margin_rate": 0.20, "assumption_version": "conservative-v1",
        },
        "risk_policy": {"contract_expiry": {
            "block_new_entries_minutes_before_expiry": 1440,
            "close_position_minutes_before_expiry": 60,
        }},
    }


class PaperBrokerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "ledger.sqlite3"
        self.config = configuration()
        self.now = datetime(2026, 11, 2, 10, 0, tzinfo=TZ)
        self.broker = PaperBroker(self.path, self.config)
        self.broker._clock = lambda: self.now

    def tearDown(self):
        self.broker.close()
        self.folder.cleanup()

    def restart(self, config=None):
        self.broker.close()
        self.broker = PaperBroker(self.path, config or self.config)
        self.broker._clock = lambda: self.now

    def quote(self, price=4000, **changes):
        q = {"symbol": "CFFEX.IF2612", "datetime": self.now.isoformat(),
             "last_price": price, "bid_price1": round(price - 0.2, 1), "ask_price1": price,
             "bid_volume1": 10, "ask_volume1": 10, "price_tick": 0.2, "volume_multiple": 300,
             "upper_limit": 6000, "lower_limit": 2000, "expired": False,
             "expire_datetime": datetime(2026, 12, 18, 15, tzinfo=TZ).timestamp(),
             "trading_day": self.now.date().isoformat(), "session_open": True,
             "calendar_verified": True}
        q.update(changes)
        return q

    def decision(self, action="open_long", stop=3990, **changes):
        d = {"action": action, "stop_loss": stop, "take_profit": None,
             "reason": "测试策略信号", "strategy_version": "baseline-v1"}
        d.update(changes)
        return d

    def advance(self, seconds=1):
        self.now += timedelta(seconds=seconds)

    def open_long(self, **changes):
        result = self.broker.execute(self.decision(**changes), self.quote())
        self.assertEqual(result["status"], "executed", result["reason"])
        return result

    def close_position(self, price=4010, **quote_changes):
        self.advance()
        return self.broker.execute(self.decision("close", stop=None), self.quote(price, **quote_changes))

    def test_long_pnl_fees_margin_and_spread_are_not_double_counted(self):
        opened = self.open_long()
        self.assertEqual(opened["position"]["entry_price"], 4000.4)
        self.assertEqual(opened["account"]["cash"], 999919.99)
        self.assertEqual(opened["account"]["unrealized_pnl"], -120)
        self.assertEqual(opened["account"]["margin"], 240000)
        self.assertEqual(opened["account"]["equity"], 999799.99)
        self.assertEqual(opened["account"]["available"], 759799.99)
        closed = self.close_position(4010)
        # Sell: bid 4009.8 minus 0.4 slippage = 4009.4, gross 2700.
        self.assertEqual(closed["trade"]["price"], 4009.4)
        self.assertEqual(closed["trade"]["gross_pnl"], 2700)
        self.assertEqual(closed["trade"]["fees"]["total"], 621.41)
        self.assertEqual(closed["trade"]["net_pnl"], 1998.58)
        self.assertEqual(closed["account"]["cash"], 1001998.58)
        self.assertEqual(closed["account"]["realized_pnl"], 1998.58)
        self.assertEqual(closed["account"]["margin"], 0)
        self.assertIsNone(closed["position"])

    def test_short_pnl_uses_buy_to_close_and_its_fee(self):
        opened = self.broker.execute(self.decision("open_short", stop=4010), self.quote())
        self.assertEqual(opened["position"]["entry_price"], 3999.4)
        self.assertEqual(opened["trade"]["fees"]["total"], 80)
        closed = self.close_position(3990)
        self.assertEqual(closed["trade"]["price"], 3990.4)
        self.assertEqual(closed["trade"]["gross_pnl"], 2700)
        self.assertEqual(closed["trade"]["fees"]["total"], 618.56)
        self.assertEqual(closed["trade"]["net_pnl"], 2001.44)
        self.assertEqual(closed["account"]["equity"], 1002001.44)

    def test_cross_day_official_reference_settlement_does_not_double_count(self):
        self.open_long()
        self.advance()
        self.broker.on_quote(self.quote(4010))
        self.now = datetime(2026, 11, 3, 10, tzinfo=TZ)
        settled = self.broker.on_quote(self.quote(4020, pre_settlement=4008))
        self.assertEqual(settled["account"]["cash"], 1002199.99)
        self.assertEqual(settled["account"]["unrealized_pnl"], 3600)
        self.assertEqual(settled["account"]["daily_pnl"], 3600)
        self.assertEqual(settled["position"]["entry_price"], 4000.4)
        self.assertEqual(settled["position"]["settlement_basis"], 4008)
        closed = self.close_position(4020)
        self.assertEqual(closed["trade"]["fees"]["fee_kind"], "close_previous")
        self.assertEqual(closed["trade"]["gross_pnl"], 5700)
        self.assertEqual(closed["account"]["cash"], 1005539.69)
        self.assertEqual(closed["account"]["closed_trade_net_pnl"], 5539.69)
        self.assertEqual(len(self.broker.history()["settlements"]), 1)
        self.broker.on_quote(self.quote(4020, pre_settlement=4008))
        self.assertEqual(len(self.broker.history()["settlements"]), 1)

    def test_missing_official_settlement_is_explicit_estimate_and_gap_counts_today(self):
        self.open_long()
        self.advance()
        self.broker.on_quote(self.quote(4005))
        self.now = datetime(2026, 11, 3, 10, tzinfo=TZ)
        result = self.broker.on_quote(self.quote(4002))
        settlement = self.broker.history()["settlements"][0]
        self.assertEqual(settlement["source"], "last_observed_price_assumption")
        self.assertIn("非官方", settlement["label"])
        self.assertEqual(result["account"]["daily_pnl"], -900)
        self.assertEqual(result["account"]["equity"], 1000399.99)

    def test_restart_preserves_position_cash_protection_and_does_not_reset_initial(self):
        self.open_long()
        before = self.broker.snapshot()
        changed = copy.deepcopy(self.config)
        changed["account"]["initial_balance_cny"] = 2000000
        self.restart(changed)
        after = self.broker.snapshot()
        self.assertEqual(before["account"], after["account"])
        self.assertEqual(before["position"], after["position"])
        self.assertEqual(after["account"]["initial_balance"], 1000000)

    def test_daily_loss_stop_is_sticky_after_recovery_and_restart(self):
        self.open_long()
        self.advance()
        loss = self.broker.on_quote(self.quote(3900, bid_volume1=0))
        self.assertTrue(loss["account"]["entry_blocked"])
        self.assertIsNotNone(loss["position"])
        self.assertEqual(loss["position"]["pending_exit"], "stop_loss")
        self.restart()
        self.advance()
        recovered = self.broker.on_quote(self.quote(4050))
        self.assertIsNone(recovered["position"])
        self.assertGreater(recovered["account"]["daily_pnl"], 0)
        self.assertTrue(recovered["account"]["entry_blocked"])
        self.advance()
        blocked = self.broker.execute(self.decision(stop=4040), self.quote(4050))
        self.assertEqual(blocked["status"], "blocked")
        self.now = datetime(2026, 11, 3, 10, tzinfo=TZ)
        new_day = self.broker.on_quote(self.quote(4050))
        self.assertFalse(new_day["account"]["entry_blocked"])
        self.assertEqual(new_day["account"]["daily_pnl"], 0)

    def test_daily_loss_threshold_only_blocks_entries_does_not_itself_close(self):
        self.config["account"]["daily_loss_stop_new_entries_cny"] = 1000
        self.restart()
        self.open_long()
        self.advance()
        result = self.broker.on_quote(self.quote(3996))
        self.assertTrue(result["account"]["entry_blocked"])
        self.assertIsNotNone(result["position"])
        self.assertIsNone(result["risk_action"])

    def test_stop_gap_fills_at_available_quote_not_stop_price(self):
        self.open_long()
        self.advance()
        result = self.broker.on_quote(self.quote(3900))
        self.assertIsNone(result["position"])
        self.assertEqual(result["risk_action"]["trade"]["price"], 3899.4)
        self.assertEqual(result["risk_action"]["trade"]["gross_pnl"], -30300)
        self.assertLess(result["account"]["daily_pnl"], -20000)
        self.assertEqual(result["risk_action"]["trade"]["origin"], "program_risk")

    def test_take_profit_is_independent_of_model(self):
        self.open_long(take_profit=4010)
        self.advance()
        result = self.broker.on_quote(self.quote(4012))
        self.assertIsNone(result["position"])
        self.assertIn("止盈", result["risk_action"]["reason"])

    def test_margin_shortfall_is_independently_protected(self):
        self.broker.close()
        self.path = Path(self.folder.name) / "small.sqlite3"
        smaller = copy.deepcopy(self.config)
        smaller["account"]["initial_balance_cny"] = 240500
        self.broker = PaperBroker(self.path, smaller)
        self.broker._clock = lambda: self.now
        self.open_long()
        self.advance()
        result = self.broker.on_quote(self.quote(3998))
        self.assertIsNone(result["position"])
        self.assertIn("保证金", result["risk_action"]["reason"])

    def test_planned_risk_counts_both_fees_and_stop_slippage(self):
        # Bare price distance 15 points is 4500, but costs/slippage exceed 5000.
        result = self.broker.execute(self.decision(stop=3985.4), self.quote())
        self.assertEqual(result["status"], "blocked")
        self.assertIn("计划风险", result["reason"])
        self.assertIsNone(result["position"])
        self.assertEqual(result["account"]["cash"], 1000000)

    def test_model_cannot_raise_risk_or_quantity_limit(self):
        rejected = self.broker.execute(self.decision(stop=3900, risk_limit=999999, quantity=100), self.quote())
        self.assertEqual(rejected["status"], "blocked")
        self.open_long(quantity=100)
        self.advance()
        second = self.broker.execute(self.decision("open_short", stop=4010), self.quote())
        self.assertEqual(second["status"], "blocked")
        self.assertEqual(second["position"]["quantity"], 1)

    def test_missing_costs_block_entry_instead_of_using_zero(self):
        no_costs = copy.deepcopy(self.config)
        no_costs["simulation_policy"] = {}
        self.restart(no_costs)
        result = self.broker.execute(self.decision(), self.quote())
        self.assertEqual(result["status"], "blocked")
        self.assertIn("成本假设", result["reason"])

    def test_live_position_keeps_original_cost_version_after_config_change(self):
        self.open_long()
        changed = copy.deepcopy(self.config)
        changed["simulation_policy"]["fee_rates"]["close_today"] = 0
        changed["simulation_policy"]["broker_commission_cny_per_side"] = 0
        changed["simulation_policy"]["assumption_version"] = "lower-v2"
        self.restart(changed)
        closed = self.close_position()
        self.assertEqual(closed["trade"]["assumption_version"], "conservative-v1")
        self.assertEqual(closed["trade"]["fees"]["total"], 621.41)

    def test_stale_future_invalid_and_expired_quotes_cannot_open(self):
        cases = [
            {"datetime": (self.now - timedelta(seconds=11)).isoformat()},
            {"datetime": (self.now + timedelta(seconds=2.01)).isoformat()},
            {"datetime": "2026-11-02 10:00:00"},
            {"last_price": float("nan")},
            {"volume_multiple": 100},
            {"expired": True},
            {"expire_datetime": self.now.timestamp()},
            {"symbol": "KQ.m@CFFEX.IF"},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                result = self.broker.execute(self.decision(), self.quote(**changes))
                self.assertEqual(result["status"], "blocked")
                self.assertIsNone(result["position"])
                json.dumps(result, allow_nan=False)

    def test_stale_quote_cannot_stop_out_at_old_price(self):
        self.open_long()
        self.advance(20)
        result = self.broker.on_quote(self.quote(3900, datetime=(self.now - timedelta(seconds=20)).isoformat()))
        self.assertFalse(result["quote_accepted"])
        self.assertIsNotNone(result["position"])
        self.assertEqual(len(self.broker.history()["trades"]), 1)

    def test_new_entry_expiry_window_includes_exact_1440_minute_boundary(self):
        for seconds in (1440 * 60 - 1, 1440 * 60):
            with self.subTest(seconds=seconds):
                result = self.broker.execute(self.decision(), self.quote(
                    expire_datetime=(self.now + timedelta(seconds=seconds)).timestamp()))
                self.assertEqual(result["status"], "blocked")
                self.assertIn("1440分钟", result["reason"])
                self.assertIsNone(result["position"])
        expiry = (self.now + timedelta(minutes=1440, seconds=1)).timestamp()
        allowed = self.broker.execute(self.decision(), self.quote(expire_datetime=expiry))
        self.assertEqual(allowed["status"], "executed")
        self.assertEqual(allowed["position"]["expire_datetime"], expiry)

    def test_expiry_exit_starts_at_exact_60_minutes_and_uses_live_book(self):
        expiry = self.now + timedelta(hours=25)
        opened = self.broker.execute(self.decision(), self.quote(expire_datetime=expiry.timestamp()))
        self.assertEqual(opened["status"], "executed")
        self.now = expiry - timedelta(minutes=60, seconds=1)
        holding = self.broker.on_quote(self.quote(4001, expire_datetime=expiry.timestamp()))
        self.assertIsNotNone(holding["position"])
        self.assertIsNone(holding["position"]["pending_exit"])
        self.advance()
        exited = self.broker.on_quote(self.quote(4002, expire_datetime=expiry.timestamp()))
        self.assertIsNone(exited["position"])
        self.assertEqual(exited["risk_action"]["trade"]["price"], 4001.4)
        self.assertEqual(exited["risk_action"]["trade"]["symbol"], "CFFEX.IF2612")
        self.assertIn("临近到期", exited["risk_action"]["reason"])

    def test_expiry_pending_waits_for_original_contract_liquidity(self):
        expiry = self.now + timedelta(hours=25)
        self.broker.execute(self.decision(), self.quote(expire_datetime=expiry.timestamp()))
        self.now = expiry - timedelta(minutes=60)
        other = self.broker.on_quote(self.quote(5000, symbol="CFFEX.IF2703"))
        self.assertEqual(other["position"]["pending_exit"], "contract_expiry")
        self.assertEqual(other["position"]["mark_price"], 4000)
        self.assertEqual(len(self.broker.history()["trades"]), 1)
        no_liquidity = self.broker.on_quote(self.quote(4002, bid_volume1=0, expire_datetime=expiry.timestamp()))
        self.assertEqual(no_liquidity["risk_action"]["status"], "blocked")
        self.assertEqual(no_liquidity["position"]["pending_exit"], "contract_expiry")
        self.advance()
        filled = self.broker.on_quote(self.quote(4003, expire_datetime=expiry.timestamp()))
        self.assertIsNone(filled["position"])
        self.assertEqual(filled["risk_action"]["trade"]["price"], 4002.4)

    def test_expiry_pending_never_executes_stale_or_closed_session_quote(self):
        expiry = self.now + timedelta(hours=25)
        original_quote = self.quote(expire_datetime=expiry.timestamp())
        self.broker.execute(self.decision(), original_quote)
        self.now = expiry - timedelta(minutes=60)
        stale = self.broker.on_quote(original_quote)
        self.assertFalse(stale["quote_accepted"])
        self.assertEqual(stale["position"]["pending_exit"], "contract_expiry")
        closed = self.broker.on_quote(self.quote(expire_datetime=expiry.timestamp(), session_open=False))
        self.assertIsNotNone(closed["position"])
        self.assertEqual(closed["risk_action"]["status"], "blocked")
        self.assertEqual(len(self.broker.history()["trades"]), 1)
        self.advance()
        filled = self.broker.on_quote(self.quote(expire_datetime=expiry.timestamp()))
        self.assertIsNone(filled["position"])

    def test_stop_loss_has_priority_over_expiry_exit(self):
        expiry = self.now + timedelta(hours=25)
        self.broker.execute(self.decision(), self.quote(expire_datetime=expiry.timestamp()))
        self.now = expiry - timedelta(minutes=60)
        stopped = self.broker.on_quote(self.quote(3989, expire_datetime=expiry.timestamp()))
        self.assertIsNone(stopped["position"])
        self.assertIn("止损", stopped["risk_action"]["reason"])

    def test_margin_shortfall_has_priority_over_expiry_exit(self):
        self.broker.close()
        self.path = Path(self.folder.name) / "expiry_margin.sqlite3"
        smaller = copy.deepcopy(self.config)
        smaller["account"]["initial_balance_cny"] = 240500
        self.broker = PaperBroker(self.path, smaller)
        self.broker._clock = lambda: self.now
        expiry = self.now + timedelta(hours=25)
        self.broker.execute(self.decision(), self.quote(expire_datetime=expiry.timestamp()))
        self.now = expiry - timedelta(minutes=60)
        result = self.broker.on_quote(self.quote(3998, expire_datetime=expiry.timestamp()))
        self.assertIsNone(result["position"])
        self.assertIn("保证金", result["risk_action"]["reason"])

    def test_exact_expiry_freezes_position_without_fill_or_guessing_final_settlement(self):
        expiry = self.now + timedelta(hours=25)
        self.broker.execute(self.decision(), self.quote(expire_datetime=expiry.timestamp()))
        before = self.broker.snapshot()
        self.now = expiry
        result = self.broker.execute(self.decision("close", stop=None), self.quote(
            3900, expire_datetime=expiry.timestamp(), pre_settlement=3890, settlement=3900))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["position"]["pending_exit"], "expired_requires_settlement")
        self.assertIn("官方最终结算价", result["position"]["expiry_warning"])
        self.assertEqual(result["position"]["mark_price"], before["position"]["mark_price"])
        self.assertEqual(result["account"]["cash"], before["account"]["cash"])
        self.assertTrue(result["account"]["entry_blocked"])
        self.assertEqual(len(self.broker.history()["trades"]), 1)
        self.assertEqual(len(self.broker.history()["settlements"]), 0)

    def test_provider_expired_flag_locks_even_before_saved_expiry_and_is_persistent(self):
        self.open_long()
        self.advance()
        result = self.broker.on_quote(self.quote(3990, expired=True))
        self.assertFalse(result["quote_accepted"])
        self.assertEqual(result["position"]["pending_exit"], "expired_requires_settlement")
        self.restart()
        self.now = datetime(2026, 11, 3, 10, tzinfo=TZ)
        again = self.broker.execute(self.decision("close", stop=None), self.quote(4010))
        self.assertEqual(again["status"], "blocked")
        self.assertTrue(again["account"]["entry_blocked"])
        self.assertEqual(again["position"]["mark_price"], 4000)
        self.assertEqual(len(self.broker.history()["trades"]), 1)
        self.assertEqual(len(self.broker.history()["settlements"]), 0)
        events = [event for event in self.broker.events() if event["kind"] == "expired_requires_settlement"]
        self.assertEqual(len(events), 1)

    def test_expired_metadata_locks_even_when_quote_timestamp_is_missing(self):
        self.open_long()
        self.advance()
        result = self.broker.on_quote(self.quote(3900, datetime=None, expired=True))
        self.assertFalse(result["quote_accepted"])
        self.assertEqual(result["position"]["pending_exit"], "expired_requires_settlement")
        self.assertTrue(result["account"]["entry_blocked"])
        self.assertEqual(result["position"]["mark_price"], 4000)
        self.assertEqual(len(self.broker.history()["trades"]), 1)

    def test_saved_expiry_detects_missed_exit_after_restart_with_stale_or_new_main_quote(self):
        expiry = self.now + timedelta(hours=25)
        old_quote = self.quote(expire_datetime=expiry.timestamp())
        self.broker.execute(self.decision(), old_quote)
        self.now = expiry + timedelta(days=1)
        self.restart()
        stale = self.broker.on_quote(old_quote)
        self.assertEqual(stale["position"]["pending_exit"], "expired_requires_settlement")
        self.assertIn("官方最终结算价", stale["account"]["entry_block_reason"])
        main = self.broker.execute(self.decision(stop=4990), self.quote(5000, symbol="CFFEX.IF2703"))
        self.assertEqual(main["status"], "blocked")
        self.assertEqual(main["position"]["symbol"], "CFFEX.IF2612")
        self.assertEqual(main["account"]["cash"], 999919.99)
        self.assertEqual(len(self.broker.history()["trades"]), 1)
        self.assertEqual(len(self.broker.history()["settlements"]), 0)

    def test_no_liquidity_limit_and_exchange_bounds_do_not_fabricate_fills(self):
        cases = [({"ask_volume1": 0}, {}), ({"ask_price1": None}, {}),
                 ({"upper_limit": 4000.2}, {}), ({"lower_limit": None}, {}),
                 ({}, {"limit_price": 4000})]
        for qchanges, dchanges in cases:
            with self.subTest(qchanges=qchanges, dchanges=dchanges):
                result = self.broker.execute(self.decision(**dchanges), self.quote(**qchanges))
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(len(self.broker.history()["trades"]), 0)

    def test_limit_down_stop_waits_for_executable_quote(self):
        self.open_long()
        self.advance()
        locked = self.broker.on_quote(self.quote(3990, bid_price1=None, bid_volume1=0, lower_limit=3990))
        self.assertEqual(locked["position"]["pending_exit"], "stop_loss")
        self.advance()
        unlocked = self.broker.on_quote(self.quote(3995))
        self.assertIsNone(unlocked["position"])
        self.assertEqual(unlocked["risk_action"]["trade"]["price"], 3994.4)

    def test_closed_session_overrides_fresh_quote_and_manual_close_waits(self):
        self.open_long()
        self.advance()
        result = self.broker.execute(self.decision("close", stop=None), self.quote(4002, session_open=False))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("休市", result["reason"])
        self.assertIsNotNone(result["position"])
        self.assertEqual(result["position"]["mark_price"], 4002)

    def test_weekend_and_lunch_cannot_fill_even_if_session_flag_is_true(self):
        for now in (datetime(2026, 11, 2, 12, tzinfo=TZ), datetime(2026, 11, 7, 10, tzinfo=TZ)):
            with self.subTest(now=now):
                self.now = now
                result = self.broker.execute(self.decision(), self.quote())
                self.assertEqual(result["status"], "blocked")
                self.assertIn("日盘时段", result["reason"])

    def test_weekend_and_long_holiday_holds_are_not_calendar_liquidated(self):
        self.now = datetime(2026, 11, 6, 14, tzinfo=TZ)
        self.open_long()
        self.now = datetime(2026, 11, 16, 10, tzinfo=TZ)
        result = self.broker.on_quote(self.quote(4001))
        self.assertIsNotNone(result["position"])
        self.assertEqual(result["position"]["opened_trading_day"], "2026-11-06")
        self.assertIsNone(result["risk_action"])

    def test_master_contract_change_never_reprices_or_moves_existing_position(self):
        self.open_long()
        self.advance()
        other = self.quote(5000, symbol="CFFEX.IF2703")
        result = self.broker.on_quote(other)
        self.assertEqual(result["position"]["symbol"], "CFFEX.IF2612")
        self.assertEqual(result["position"]["mark_price"], 4000)
        closed = self.broker.execute(self.decision("close", stop=None), other)
        self.assertEqual(closed["status"], "blocked")
        self.assertIn("持仓合约", closed["reason"])

    def test_same_tick_cannot_close_then_reopen_or_repeat_orders(self):
        self.open_long()
        same_tick = self.broker.execute(self.decision("close", stop=None), self.quote())
        self.assertEqual(same_tick["status"], "blocked")
        closed = self.close_position(4001)
        self.assertEqual(closed["status"], "executed")
        reopened = self.broker.execute(self.decision(stop=3991), self.quote(4001))
        self.assertEqual(reopened["status"], "blocked")
        self.assertEqual(len(self.broker.history()["trades"]), 2)

    def test_parallel_entry_attempts_create_only_one_position_and_fill(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.broker.execute(self.decision(), self.quote()))) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(result["status"] == "executed" for result in results), 1)
        self.assertEqual(len(self.broker.history()["trades"]), 1)

    def test_protection_only_tightens_and_strategy_cannot_rewrite_old_position(self):
        self.open_long()
        self.advance()
        blocked = self.broker.execute(self.decision("hold", stop=3989), self.quote())
        self.assertEqual(blocked["status"], "blocked")
        updated = self.broker.execute(self.decision("hold", stop=3995), self.quote())
        self.assertEqual(updated["status"], "protection_updated")
        self.advance()
        new_strategy = self.broker.execute(self.decision("hold", stop=3996, strategy_version="new-v2"), self.quote())
        self.assertEqual(new_strategy["status"], "blocked")
        self.assertEqual(self.broker.snapshot()["position"]["stop_loss"], 3995)
        invalid_target = self.broker.execute(self.decision("hold", stop=3997, take_profit=3990), self.quote())
        self.assertEqual(invalid_target["status"], "blocked")
        self.assertEqual(self.broker.snapshot()["position"]["stop_loss"], 3995)
        self.assertEqual(invalid_target["position"]["stop_loss"], 3995)

    def test_long_trailing_activates_at_one_r_and_never_loosens(self):
        self.open_long()
        self.advance()
        below = self.broker.on_quote(self.quote(4010))
        self.assertEqual(below["position"]["stop_loss"], 3990)
        self.advance()
        trailed = self.broker.on_quote(self.quote(4011))
        self.assertEqual(trailed["position"]["stop_loss"], 4000.6)
        self.assertEqual(trailed["position"]["initial_risk_distance"], 10.4)
        self.assertEqual(trailed["position"]["strategy_version"], "baseline-v1")
        self.advance()
        pullback = self.broker.on_quote(self.quote(4005))
        self.assertEqual(pullback["position"]["stop_loss"], 4000.6)
        self.restart()
        self.advance()
        stopped = self.broker.on_quote(self.quote(4000))
        self.assertIsNone(stopped["position"])

    def test_short_trailing_activates_at_one_r_and_never_loosens(self):
        opened = self.broker.execute(self.decision("open_short", stop=4010), self.quote())
        self.assertEqual(opened["status"], "executed")
        self.advance()
        below = self.broker.on_quote(self.quote(3990))
        self.assertEqual(below["position"]["stop_loss"], 4010)
        self.advance()
        trailed = self.broker.on_quote(self.quote(3988))
        self.assertEqual(trailed["position"]["stop_loss"], 3998.6)
        self.assertEqual(trailed["position"]["initial_risk_distance"], 10.6)
        self.advance()
        bounced = self.broker.on_quote(self.quote(3995))
        self.assertEqual(bounced["position"]["stop_loss"], 3998.6)
        self.advance()
        stopped = self.broker.on_quote(self.quote(3999))
        self.assertIsNone(stopped["position"])

    def test_old_stop_is_checked_before_a_quote_can_tighten_trailing(self):
        self.open_long()
        self.advance()
        stopped = self.broker.on_quote(self.quote(3989))
        self.assertIsNone(stopped["position"])
        self.assertEqual(stopped["risk_action"]["trade"]["price"], 3988.4)
        self.assertFalse(any(event["kind"] == "trailing_stop_tightened" for event in self.broker.events()))

    def test_history_is_persistent_and_strict_json_serializable(self):
        self.open_long()
        self.close_position()
        self.broker.record_event("test", "记录测试", {"value": float("inf"), "api_key": "should-not-appear"})
        self.restart()
        history = self.broker.history()
        self.assertEqual(len(history["trades"]), 2)
        self.assertGreaterEqual(len(history["equity_curve"]), 3)
        encoded = json.dumps({"history": history, "events": self.broker.events(), "snapshot": self.broker.snapshot()}, allow_nan=False)
        self.assertNotIn("should-not-appear", encoded)

    def test_fill_cash_deltas_and_closed_trade_net_pnl_do_not_double_count_entry_fee(self):
        self.open_long()
        self.close_position()
        history = self.broker.history()
        trades = history["trades"]
        opened = next(row for row in trades if row["action"] == "open_long")
        closed = next(row for row in trades if row["action"] == "close")
        self.assertIsNone(opened["net_pnl"])
        self.assertEqual(closed["net_pnl_scope"], "closed_round_trip")
        self.assertAlmostEqual(sum(float(row["cash_delta"]) for row in trades), 1998.58)
        self.assertEqual(float(closed["net_pnl"]), 1998.58)
        filled_orders = [row for row in history["orders"] if row["status"] == "filled"]
        self.assertEqual(sorted(float(row["fill_price"]) for row in filled_orders), [4000.4, 4009.4])


if __name__ == "__main__":
    unittest.main()
