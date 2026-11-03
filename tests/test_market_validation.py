"""Validation tests use recorded-shaped values, not mocked network success."""

import copy
import json
import unittest
from datetime import datetime, timedelta

from futures_probe.market import (
    CHINA_TZ,
    evaluate_market_observations,
    is_weekday_session_window,
)


class MarketValidationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 30, 14, 0, 2, tzinfo=CHINA_TZ)
        self.symbol = "CFFEX.IF2611"

    def sample(self, quote_time, received_at=None, **changes):
        quote = {
            "datetime": quote_time.replace(tzinfo=None).isoformat(sep=" "),
            "instrument_id": self.symbol,
            "exchange_id": "CFFEX",
            "ins_class": "FUTURE",
            "expired": False,
            "expire_datetime": datetime(2026, 11, 20, 15, tzinfo=CHINA_TZ).timestamp(),
            "last_price": 4000.0,
            "bid_price1": 3999.8,
            "ask_price1": 4000.0,
            "bid_volume1": 10,
            "ask_volume1": 10,
            "volume": 100,
            "open_interest": 1000,
            "price_tick": 0.2,
            "volume_multiple": 300,
        }
        quote.update(changes)
        return {"quote": quote, "received_at": received_at or quote_time}

    def live_samples(self):
        return [self.sample(self.now - timedelta(seconds=offset)) for offset in (2, 1, 0)]

    def evaluate(self, observations, symbol=None, now=None):
        return evaluate_market_observations(
            self.symbol if symbol is None else symbol,
            observations,
            now=self.now if now is None else now,
        )

    def test_fresh_timestamp_advances_pass_even_when_price_does_not_change(self):
        report = self.evaluate(self.live_samples())
        self.assertEqual(report["status"], "passed_live")
        self.assertTrue(report["model_gate_passed"])
        self.assertEqual(report["updates"], 2)
        self.assertEqual(report["fresh_updates"], 2)
        self.assertEqual(report["latency_age_seconds"], 0)
        self.assertFalse(report["checks"]["exchange_calendar_verified"])

    def test_single_fresh_snapshot_does_not_prove_live_feed(self):
        report = self.evaluate([self.sample(self.now)])
        self.assertEqual(report["status"], "partial_snapshot")
        self.assertFalse(report["model_gate_passed"])
        self.assertEqual(report["error_code"], "live_updates_not_observed")

    def test_price_changes_without_timestamp_advances_cannot_pass(self):
        records = [self.sample(self.now, last_price=4000 + offset) for offset in (0, 1, 2)]
        report = self.evaluate(records)
        self.assertEqual(report["updates"], 0)
        self.assertFalse(report["model_gate_passed"])

    def test_one_time_advance_is_insufficient(self):
        report = self.evaluate(self.live_samples()[1:])
        self.assertEqual(report["fresh_updates"], 1)
        self.assertEqual(report["status"], "partial_snapshot")

    def test_stale_replayed_stream_is_only_a_snapshot(self):
        records = [
            self.sample(self.now - timedelta(hours=1, seconds=offset), received_at=self.now)
            for offset in (2, 1, 0)
        ]
        report = self.evaluate(records)
        self.assertEqual(report["updates"], 2)
        self.assertEqual(report["fresh_updates"], 0)
        self.assertEqual(report["error_code"], "stale_snapshot")
        self.assertFalse(report["model_gate_passed"])

    def test_freshness_is_checked_at_observation_end_as_well_as_receipt(self):
        report = self.evaluate(self.live_samples(), now=self.now + timedelta(seconds=11))
        self.assertEqual(report["fresh_updates"], 2)
        self.assertEqual(report["error_code"], "stale_snapshot")
        self.assertFalse(report["model_gate_passed"])

    def test_ten_second_freshness_boundary_is_inclusive(self):
        report = self.evaluate(self.live_samples(), now=self.now + timedelta(seconds=10))
        self.assertTrue(report["model_gate_passed"])

    def test_small_future_clock_skew_is_tolerated_but_larger_is_rejected(self):
        for skew, expected in ((2, True), (2.001, False)):
            with self.subTest(skew=skew):
                records = [
                    self.sample(
                        self.now - timedelta(seconds=offset) + timedelta(seconds=skew),
                        received_at=self.now - timedelta(seconds=offset),
                    )
                    for offset in (2, 1, 0)
                ]
                report = self.evaluate(records)
                self.assertEqual(report["model_gate_passed"], expected)
                if not expected:
                    self.assertEqual(report["error_code"], "quote_time_in_future")

    def test_invalid_or_non_if_contracts_fail(self):
        for symbol in ("KQ.m@CFFEX.IF", "CFFEX.IH2611", "CFFEX.IF2613", "CFFEX.IF26", "DCE.m2701"):
            with self.subTest(symbol=symbol):
                report = self.evaluate(self.live_samples(), symbol=symbol)
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["error_code"], "invalid_if_contract")

    def test_metadata_must_match_requested_contract(self):
        for changes in (
            {"instrument_id": "CFFEX.IF2612"},
            {"exchange_id": "SHFE"},
            {"ins_class": "CONT"},
        ):
            with self.subTest(changes=changes):
                report = self.evaluate([self.sample(self.now, **changes)])
                self.assertEqual(report["error_code"], "contract_metadata_mismatch")
                self.assertFalse(report["model_gate_passed"])

    def test_expired_or_unknown_expiry_is_not_accepted(self):
        for changes in (
            {"expired": True},
            {"expired": None},
            {"expire_datetime": None},
            {"expire_datetime": self.now.timestamp()},
            {"expire_datetime": self.now.timestamp() - 1},
        ):
            with self.subTest(changes=changes):
                report = self.evaluate([self.sample(self.now, **changes)])
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["error_code"], "expired_or_unknown_expiry")

    def test_past_contract_month_is_rejected_despite_inconsistent_metadata(self):
        report = self.evaluate(
            [self.sample(self.now, instrument_id="CFFEX.IF2609")],
            symbol="CFFEX.IF2609",
        )
        self.assertEqual(report["error_code"], "expired_or_unknown_expiry")

    def test_empty_invalid_price_and_invalid_timestamp_fail(self):
        self.assertEqual(self.evaluate([])["status"], "failed")
        for changes in ({"last_price": float("nan")}, {"last_price": 0}, {"datetime": "not a timestamp"}):
            with self.subTest(changes=changes):
                report = self.evaluate([self.sample(self.now, **changes)])
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["error_code"], "invalid_snapshot")

    def test_bad_order_book_does_not_pass_model_gate(self):
        for changes in (
            {"bid_price1": 4001, "ask_price1": 4000},
            {"ask_price1": float("nan")},
            {"ask_volume1": 0},
        ):
            with self.subTest(changes=changes):
                records = self.live_samples()
                records[-1]["quote"].update(changes)
                report = self.evaluate(records)
                self.assertEqual(report["status"], "partial_snapshot")
                self.assertEqual(report["error_code"], "invalid_order_book")

    def test_time_regression_cannot_be_hidden_by_subsequent_fresh_ticks(self):
        records = self.live_samples()
        records.insert(2, self.sample(self.now - timedelta(seconds=2), received_at=self.now))
        report = self.evaluate(records)
        self.assertFalse(report["model_gate_passed"])
        self.assertEqual(report["error_code"], "quote_time_regressed")

    def test_missing_or_reversed_receipt_times_cannot_pass(self):
        for bad_receipt in (None, self.now - timedelta(seconds=10), self.now + timedelta(seconds=1)):
            with self.subTest(receipt=bad_receipt):
                records = self.live_samples()
                records[1]["received_at"] = bad_receipt
                report = self.evaluate(records)
                self.assertFalse(report["checks"]["receipt_times_valid"])
                self.assertFalse(report["model_gate_passed"])

    def test_nonfinite_optional_fields_and_unknown_secrets_are_not_serialized(self):
        records = self.live_samples()
        records[-1]["quote"].update({
            "open_interest": float("inf"),
            "volume": float("nan"),
            "password": "do-not-output-this",
            "username": "do-not-output-this-either",
        })
        original = copy.deepcopy(records)
        report = self.evaluate(records)
        encoded = json.dumps(report, allow_nan=False)
        self.assertIsNone(report["quote"]["volume"])
        self.assertIsNone(report["quote"]["open_interest"])
        self.assertNotIn("do-not-output", encoded)
        self.assertEqual(records[-1]["quote"]["password"], original[-1]["quote"]["password"])

    def test_utc_receipt_times_are_compared_in_china_time(self):
        records = self.live_samples()
        for record in records:
            record["received_at"] = record["received_at"].isoformat()
        records[-1]["quote"]["datetime"] = "2026-10-30T06:00:02+00:00"
        report = self.evaluate(records)
        self.assertTrue(report["model_gate_passed"])
        self.assertEqual(report["quote"]["datetime"], "2026-10-30T14:00:02+08:00")

    def test_weekday_window_is_only_a_clock_hint_and_excludes_lunch_close_weekend(self):
        for hour, minute, expected in ((9, 29, False), (9, 30, True), (11, 30, False), (12, 0, False), (13, 0, True), (15, 0, False)):
            with self.subTest(hour=hour, minute=minute):
                now = self.now.replace(hour=hour, minute=minute, second=0)
                self.assertEqual(is_weekday_session_window(now), expected)
        self.assertFalse(is_weekday_session_window(self.now + timedelta(days=1)))
        # A weekday public holiday still returns a clock hint; no calendar claim.
        holiday = datetime(2026, 10, 1, 10, tzinfo=CHINA_TZ)
        self.assertTrue(is_weekday_session_window(holiday))
        report = self.evaluate([self.sample(self.now)], now=self.now + timedelta(days=1))
        self.assertEqual(report["status"], "partial_snapshot")
        self.assertFalse(report["model_gate_passed"])
        self.assertFalse(report["checks"]["exchange_calendar_verified"])


if __name__ == "__main__":
    unittest.main()
