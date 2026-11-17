import datetime as dt
import io
import json
import unittest
from unittest.mock import patch

from futures_app.common import CHINA
from futures_app.global_market import FACTORS, GlobalMarketFeed, parse_sina, parse_yahoo


class GlobalMarketTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 11, 17, 10, tzinfo=CHINA)
        self.data = {"chart": {"result": [{"meta": {
            "symbol": "ES=F", "regularMarketPrice": 6100, "regularMarketTime": self.now.timestamp(),
            "regularMarketChangePercent": -0.5, "chartPreviousClose": 5000},
            "timestamp": [], "indicators": {"quote": [{"close": []}]}}]}}

    def test_daily_change_uses_reported_value_and_missing_data_stays_unknown(self):
        value = parse_yahoo(self.data, "ES=F")
        self.assertEqual(value["change_pct"], -0.5)
        self.assertIsNone(value["five_session_change_pct"])
        del self.data["chart"]["result"][0]["meta"]["regularMarketChangePercent"]
        self.assertIsNone(parse_yahoo(self.data, "ES=F")["change_pct"])
        with self.assertRaises(ValueError):
            parse_yahoo(self.data, "NQ=F")

    def test_sina_quote_has_china_timestamp_and_daily_previous_close(self):
        data = 'var hq_str_hf_CHA50CFD="12646.6,,12651,12652,12785,12611,10:00:00,12742,12746,1013938,3,24,2026-11-17,富时中国A50期货,197038";'
        value = parse_sina(data)
        self.assertEqual(value["observed_at"], self.now.isoformat())
        self.assertAlmostEqual(value["change_pct"], (12646.6 / 12742 - 1) * 100)

    def test_missing_stale_and_future_observations_are_labelled(self):
        feed = GlobalMarketFeed()
        for hours, expected in ((0, "available"), (121, "stale"), (-1, "unavailable")):
            self.data["chart"]["result"][0]["meta"]["regularMarketTime"] = (self.now - dt.timedelta(hours=hours)).timestamp()
            with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(self.data).encode())):
                value = feed._fetch(FACTORS[1], self.now)
            self.assertEqual(value["status"], expected)
            self.assertTrue(value["source_url"].startswith("https://query1.finance.yahoo.com/"))
        with patch("urllib.request.urlopen", side_effect=OSError("synthetic failure")):
            value = feed.collect()
        self.assertEqual(value["available_count"], 0)
        self.assertEqual(len(value["factors"]), len(FACTORS))
        self.assertTrue(all(item["status"] == "unavailable" for item in value["factors"]))

    def test_five_session_change_excludes_future_and_null_bars(self):
        result = self.data["chart"]["result"][0]
        result["timestamp"] = [self.now.timestamp() - (6 - i) * 86400 for i in range(8)]
        result["indicators"]["quote"][0]["close"] = [6000, 6010, None, 6020, 6030, 6040, 6100, 99999]
        self.assertAlmostEqual(parse_yahoo(self.data, "ES=F")["five_session_change_pct"], (6100 / 6000 - 1) * 100)
