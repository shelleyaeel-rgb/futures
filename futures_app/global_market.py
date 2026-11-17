"""Dated public market snapshots used only to prepare the daily IF strategy."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import re
import urllib.parse
import urllib.request

from .common import CHINA, now_china
from .strategy import finite_number


FACTORS = (
    ("china_a50", "富时中国A50连续报价", "hf_CHA50CFD", "sina", "指数点"),
    ("sp500", "标普500期货", "ES=F", "yahoo", "指数点"),
    ("nasdaq100", "纳斯达克100期货", "NQ=F", "yahoo", "指数点"),
    ("vix", "VIX波动率指数", "^VIX", "yahoo", "波动率点"),
    ("china_hshares", "恒生国企指数", "^HSCE", "yahoo", "指数点"),
    ("usdcnh", "美元兑离岸人民币", "CNH=X", "yahoo", "人民币/美元"),
    ("us10y", "美国10年期国债收益率", "^TNX", "yahoo", "%"),
    ("dollar_index", "美元指数", "DX-Y.NYB", "yahoo", "指数点"),
)


def parse_yahoo(data: dict, symbol: str) -> dict:
    result = data["chart"]["result"][0]
    meta = result["meta"]
    if meta["symbol"] != symbol:
        raise ValueError("Symbol mismatch")
    price, stamp = meta["regularMarketPrice"], meta["regularMarketTime"]
    if not finite_number(price) or price <= 0 or not finite_number(stamp):
        raise ValueError("Invalid market observation")
    # chartPreviousClose can refer to the beginning of the requested month.
    # Use the provider's explicitly reported daily percentage instead.
    change = meta.get("regularMarketChangePercent")
    change = float(change) if finite_number(change) else None
    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    stamps = result.get("timestamp", [])
    observed = [float(close) for at, close in zip(stamps, closes)
                if finite_number(at) and at <= stamp and finite_number(close) and close > 0]
    five_session_change = (price / observed[-6] - 1) * 100 if len(observed) >= 6 else None
    return {"value": float(price), "observed_at": dt.datetime.fromtimestamp(stamp, CHINA).isoformat(),
            "change_pct": change, "five_session_change_pct": five_session_change,
            "exchange_timezone": meta.get("exchangeTimezoneName"),
            "instrument_name": meta.get("shortName", symbol)}


def parse_sina(text: str) -> dict:
    match = re.search(r'hq_str_hf_CHA50CFD="([^"]+)"', text)
    if not match:
        raise ValueError("Missing A50 quote")
    fields = match.group(1).split(",")
    value, previous = float(fields[0]), float(fields[7])
    if not finite_number(value) or value <= 0 or not finite_number(previous) or previous <= 0:
        raise ValueError("Invalid A50 quote")
    observed = dt.datetime.fromisoformat(fields[12] + "T" + fields[6]).replace(tzinfo=CHINA)
    return {"value": value, "observed_at": observed.isoformat(),
            "change_pct": (value / previous - 1) * 100, "five_session_change_pct": None,
            "exchange_timezone": "Asia/Shanghai", "instrument_name": fields[13]}


class GlobalMarketFeed:
    def __init__(self, settings: dict | None = None):
        self.settings = settings or {}

    def _fetch(self, factor: tuple, as_of: dt.datetime) -> dict:
        identifier, name, symbol, provider, unit = factor
        if provider == "sina":
            url = "https://hq.sinajs.cn/list=hf_CHA50CFD"
        else:
            encoded = urllib.parse.quote(symbol, safe="")
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?interval=1d&range=1mo"
        item = {"id": identifier, "name": name, "symbol": symbol, "provider": provider,
                "source_url": url, "unit": unit, "status": "unavailable"}
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                           "Referer": "https://finance.sina.com.cn/"})
            with urllib.request.urlopen(request, timeout=self.settings.get("request_timeout_seconds", 10)) as response:
                body = response.read(1_000_001)
            if len(body) > 1_000_000:
                raise ValueError("Response too large")
            value = parse_sina(body.decode("gb18030")) if provider == "sina" else parse_yahoo(json.loads(body), symbol)
            observed = dt.datetime.fromisoformat(value["observed_at"])
            age = (as_of - observed).total_seconds()
            if age < -300:
                raise ValueError("Future observation")
            item.update(value, age_hours=round(max(0, age) / 3600, 2))
            item["status"] = "available" if age <= self.settings.get("max_observation_age_hours", 120) * 3600 else "stale"
        except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
            item["error_code"] = type(exc).__name__
        return item

    def collect(self) -> dict:
        as_of = now_china()
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="global-market") as pool:
            factors = list(pool.map(lambda factor: self._fetch(factor, as_of), FACTORS))
        missing = [factor["name"] for factor in factors if factor["status"] != "available"]
        return {"as_of": as_of.isoformat(), "factors": factors,
                "available_count": sum(factor["status"] == "available" for factor in factors),
                "limitations": ["公共行情可能延迟，报价时间与当地交易时段一并提供。",
                                "A50使用新浪连续报价，期货连续合约可能发生换月。",
                                "新闻及宏观事件日历尚待接入。"] + (["缺失或过时指标：" + "、".join(missing)] if missing else [])}
