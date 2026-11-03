"""Deterministic indicators and bounded strategy parameters, using closed bars only."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

CHINA = timezone(timedelta(hours=8))
PARAMETER_BOUNDS = {
    "trend_fast": (4, 16, int), "trend_slow": (18, 48, int),
    "entry_ma": (3, 12, int), "atr_period": (8, 24, int),
    "stop_atr": (1.0, 3.0, float), "reward_risk": (1.2, 3.0, float),
    "pullback_atr": (0.1, 0.8, float), "min_trend_atr": (0.05, 0.5, float),
}


def default_strategy() -> dict:
    return {
        "version": "trend-v1", "name": "5分钟趋势与1分钟回调",
        "parameters": {"trend_fast": 8, "trend_slow": 21, "entry_ma": 5,
                       "atr_period": 14, "stop_atr": 1.8, "reward_risk": 2.0,
                       "pullback_atr": 0.4, "min_trend_atr": 0.15},
        "description": "仅使用已完成K线；5分钟判断方向，1分钟回调确认，AI复核后方可开仓。",
    }


def finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def timestamp(value) -> float | None:
    """Accept SDK nanoseconds, milliseconds, seconds, and China-local ISO strings."""
    if finite_number(value):
        number = float(value)
        if number > 1e17:
            number /= 1e9
        elif number > 1e14:
            number /= 1e6
        elif number > 1e11:
            number /= 1e3
        return number if number > 0 else None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=CHINA)).timestamp()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=CHINA)).timestamp()
        except (ValueError, OverflowError):
            return None
    return None


def validate_parameters(parameters: dict, base: dict | None = None) -> dict:
    if not isinstance(parameters, dict) or set(parameters) - set(PARAMETER_BOUNDS):
        raise ValueError("Unsupported strategy parameter")
    values = dict(base if base is not None else default_strategy()["parameters"])
    values.update(parameters)
    if set(values) != set(PARAMETER_BOUNDS):
        raise ValueError("Incomplete strategy parameters")
    for name, (lower, upper, kind) in PARAMETER_BOUNDS.items():
        value = values[name]
        if not finite_number(value) or not lower <= value <= upper:
            raise ValueError("Strategy parameter out of range")
        if kind is int and value != int(value):
            raise ValueError("Integer strategy parameter required")
        values[name] = kind(value)
    if values["trend_fast"] >= values["trend_slow"]:
        raise ValueError("Trend windows overlap")
    return values


def closed_bars(bars: list, duration: int, as_of: float | None) -> list[dict]:
    if as_of is None:
        return []
    valid = {}
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        start = timestamp(bar.get("datetime"))
        if start is None or start + duration > as_of or bar.get("complete") is False or bar.get("completed") is False:
            continue
        if any(not finite_number(bar.get(key)) or bar[key] <= 0 for key in ("open", "high", "low", "close")):
            continue
        if bar["high"] < max(bar["open"], bar["low"], bar["close"]) or bar["low"] > min(bar["open"], bar["close"]):
            continue
        volume = bar.get("volume", 0)
        if not finite_number(volume) or volume < 0:
            continue
        valid[start] = {"datetime": start, **{key: float(bar[key]) for key in ("open", "high", "low", "close")}, "volume": float(volume)}
    return [valid[key] for key in sorted(valid)]


def aggregate_five_minute(bars: list[dict]) -> list[dict]:
    groups: dict[int, list] = {}
    for bar in bars:
        start = timestamp(bar.get("datetime"))
        if start is not None:
            groups.setdefault(int(start // 300) * 300, []).append(bar)
    result = []
    for bucket, rows in sorted(groups.items()):
        rows = sorted(rows, key=lambda item: timestamp(item["datetime"]))
        if len(rows) != 5 or [timestamp(row["datetime"]) for row in rows] != [bucket + 60 * i for i in range(5)]:
            continue
        result.append({"datetime": bucket, "open": rows[0]["open"], "close": rows[-1]["close"],
                       "high": max(row["high"] for row in rows), "low": min(row["low"] for row in rows),
                       "volume": sum(row.get("volume", 0) for row in rows)})
    return result


def _mean(values: list[float], length: int) -> float:
    return sum(values[-length:]) / length


def _atr(bars: list[dict], length: int) -> float:
    ranges = [max(row["high"] - row["low"], abs(row["high"] - previous["close"]),
                  abs(row["low"] - previous["close"])) for previous, row in zip(bars, bars[1:])]
    return _mean(ranges, length)


def build_context(quote: dict, bars1: list, bars5: list, broker_snapshot: dict,
                  closure_context: dict, strategy: dict | None = None) -> dict:
    strategy = deepcopy(strategy or default_strategy())
    parameters = validate_parameters(strategy.get("parameters", {}))
    strategy["parameters"] = parameters
    as_of = timestamp(quote.get("datetime", quote.get("timestamp")))
    one = closed_bars(bars1, 60, as_of)
    five = closed_bars(bars5, 300, as_of)
    required_one = max(parameters["entry_ma"] + 1, parameters["atr_period"] + 1, 4)
    required_five = max(parameters["trend_slow"] + 1, parameters["atr_period"] + 1)
    ready = len(one) >= required_one and len(five) >= required_five
    position = broker_snapshot.get("position")
    has_position = bool(position and position.get("quantity", 0))
    quote_fields = ("datetime", "symbol", "instrument_id", "last_price", "bid_price1", "ask_price1",
                    "bid_volume1", "ask_volume1", "volume", "open_interest", "upper_limit", "lower_limit",
                    "price_tick", "volume_multiple", "expired")
    safe_quote = {key: quote.get(key) for key in quote_fields if key in quote}
    for key, value in list(safe_quote.items()):
        if isinstance(value, float) and not math.isfinite(value):
            safe_quote[key] = None
    context = {
        "as_of": as_of, "quote": safe_quote, "strategy": strategy,
        "broker": deepcopy(broker_snapshot), "closure": deepcopy(closure_context or {}),
        "data_ready": ready, "bar_counts": {"one_minute": len(one), "five_minute": len(five)},
        "required_bar_counts": {"one_minute": required_one, "five_minute": required_five},
        "indicators": {}, "signal": "observe", "eligible_actions": ["hold"] + (["close"] if has_position else []),
        "data_note": "只使用报价时间之前已经收盘的K线。",
        "constraints": {"max_contracts": 1, "fractional_contracts_allowed": False,
                        "calendar_forced_exit": False, "program_limits_override_ai": True},
    }
    if not ready:
        context["data_note"] = "已完成K线不足；暂停新开仓，已有持仓继续程序风控与AI退出评估。"
        return context
    close1 = [row["close"] for row in one]
    close5 = [row["close"] for row in five]
    indicators = {
        "ma5_fast": _mean(close5, parameters["trend_fast"]),
        "ma5_fast_previous": _mean(close5[:-1], parameters["trend_fast"]),
        "ma5_slow": _mean(close5, parameters["trend_slow"]),
        "ma1_entry": _mean(close1, parameters["entry_ma"]),
        "atr1": _atr(one, parameters["atr_period"]),
        "atr5": _atr(five, parameters["atr_period"]),
        "last_closed_1m": one[-1], "last_closed_5m": five[-1],
    }
    context["indicators"] = indicators
    if indicators["atr1"] <= 0 or indicators["atr5"] <= 0:
        context["data_ready"] = False
        context["data_note"] = "波动指标无效，暂停新开仓。"
        return context
    trend_gap = indicators["ma5_fast"] - indicators["ma5_slow"]
    trend_minimum = parameters["min_trend_atr"] * indicators["atr5"]
    pullback = indicators["atr1"] * parameters["pullback_atr"]
    if (trend_gap > trend_minimum and indicators["ma5_fast"] > indicators["ma5_fast_previous"]
            and close1[-1] > indicators["ma1_entry"]
            and min(row["low"] for row in one[-3:]) <= indicators["ma1_entry"] + pullback):
        context["signal"] = "long"
    elif (trend_gap < -trend_minimum and indicators["ma5_fast"] < indicators["ma5_fast_previous"]
          and close1[-1] < indicators["ma1_entry"]
          and max(row["high"] for row in one[-3:]) >= indicators["ma1_entry"] - pullback):
        context["signal"] = "short"
    else:
        context["signal"] = "observe"
    account = broker_snapshot.get("account", broker_snapshot)
    if not has_position and not account.get("entry_blocked", False) and context["signal"] != "observe":
        context["eligible_actions"].append("open_" + context["signal"])
    return context


def baseline_decision(context: dict) -> dict:
    """Rule-only benchmark for independent evaluations, never an AI substitute."""
    strategy = context["strategy"]
    parameters = strategy["parameters"]
    position = context.get("broker", {}).get("position")
    decision = {"action": "hold", "stop_loss": None, "take_profit": None,
                "reason": "规则基线观察", "risk_level": "medium",
                "closure_assessment": "独立基线未接入事件判断；不因休市日期自动平仓。",
                "strategy_version": strategy["version"]}
    if position:
        if context["signal"] in {"long", "short"} and context["signal"] != position.get("side"):
            decision.update(action="close", reason="5分钟趋势反向")
        return decision
    action = "open_" + context.get("signal", "observe")
    if action not in context.get("eligible_actions", []):
        return decision
    price = context["quote"].get("ask_price1" if action == "open_long" else "bid_price1")
    if not finite_number(price) or price <= 0:
        return decision
    distance = context["indicators"]["atr1"] * parameters["stop_atr"]
    direction = 1 if action == "open_long" else -1
    tick = context["quote"].get("price_tick") or 0.2
    if not finite_number(tick) or tick <= 0:
        return decision
    decision.update(action=action, stop_loss=round(round((price - direction * distance) / tick) * tick, 8),
                    take_profit=round(round((price + direction * distance * parameters["reward_risk"]) / tick) * tick, 8),
                    reason="5分钟趋势与1分钟回调满足规则")
    return decision
