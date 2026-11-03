import unittest
from futures_app.strategy import (build_context, closed_bars, default_strategy,
                                  validate_parameters, timestamp)


def make_bars(start, count, duration=60):
    return [{"datetime": start + i * duration, "open": 4000 + i,
             "high": 4002 + i, "low": 3998 + i, "close": 4001 + i,
             "volume": 100, "completed": True} for i in range(count)]


class StrategyTests(unittest.TestCase):
    def test_in_progress_and_future_bars_do_not_change_indicators(self):
        end = 1_800_000_000
        one = make_bars(end - 60 * 60, 60)
        five = make_bars(end - 50 * 300, 50, 300)
        quote = {"datetime": end, "last_price": 4060, "bid_price1": 4059.8, "ask_price1": 4060.2}
        first = build_context(quote, one, five, {}, {}, default_strategy())
        future = {"datetime": end, "open": 9999, "close": 9999, "high": 10000, "low": 9998}
        second = build_context(quote, one + [future], five + [future], {}, {}, default_strategy())
        self.assertTrue(first["data_ready"])
        self.assertEqual(first["indicators"], second["indicators"])
        self.assertEqual(first["signal"], second["signal"])

    def test_explicit_incomplete_and_invalid_ohlc_are_excluded(self):
        rows = make_bars(1_800_000_000, 4)
        rows[1]["completed"] = False
        rows[2]["close"] = float("nan")
        rows[3]["high"] = rows[3]["low"] - 1
        self.assertEqual(len(closed_bars(rows, 60, 1_800_001_000)), 1)

    def test_insufficient_history_retains_exit_but_blocks_entries(self):
        context = build_context({"datetime": 1_800_000_000}, [], [],
                                {"position": {"quantity": 1, "side": "long"}}, {"days_closed": 3})
        self.assertFalse(context["data_ready"])
        self.assertEqual(context["eligible_actions"], ["hold", "close"])
        self.assertFalse(context["constraints"]["calendar_forced_exit"])

    def test_parameters_cannot_change_hard_risk_or_escape_bounds(self):
        for value in ({"max_contracts": 2}, {"stop_atr": 0.01}, {"entry_ma": True}, {"trend_fast": 4.5}):
            with self.assertRaises(ValueError):
                validate_parameters(value)

    def test_timestamp_accepts_sdk_nanoseconds_and_china_local_time(self):
        self.assertEqual(timestamp(1_800_000_000_000_000_000), 1_800_000_000)
        self.assertEqual(timestamp("2026-10-30 09:30:00"), timestamp("2026-10-30T09:30:00+08:00"))


if __name__ == "__main__":
    unittest.main()
