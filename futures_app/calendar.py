"""Explicitly sourced equity-index session calendar, never a weekday-only promise."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .common import CHINA, ROOT, now_china


class ExchangeCalendar:
    def __init__(self, path: Path | None = None):
        path = path or ROOT / "calendar.json"
        self.data = json.loads(path.read_text(encoding="utf-8"))
        self.holidays = set(self.data.get("closed_dates", []))
        self.years = set(self.data.get("covered_years", []))

    def is_trading_day(self, day: dt.date) -> bool:
        return day.year in self.years and day.weekday() < 5 and day.isoformat() not in self.holidays

    def next_open(self, current: dt.datetime) -> dt.datetime | None:
        current = current.astimezone(CHINA)
        for offset in range(370):
            day = current.date() + dt.timedelta(days=offset)
            if not self.is_trading_day(day):
                continue
            for start in (dt.time(9, 30), dt.time(13)):
                opening = dt.datetime.combine(day, start, CHINA)
                if opening > current:
                    return opening
        return None

    def context(self, current: dt.datetime | None = None) -> dict:
        current = (current or now_china()).astimezone(CHINA)
        day = current.date()
        known = day.year in self.years
        trading_day = self.is_trading_day(day)
        time = current.time().replace(tzinfo=None)
        opened = trading_day and (dt.time(9, 30) <= time < dt.time(11, 30) or dt.time(13) <= time < dt.time(15))
        if not known:
            session = "日历待更新"
        elif not trading_day:
            session = "周末休市" if day.weekday() >= 5 else "节假日休市"
        elif time < dt.time(9, 30):
            session = "开盘前"
        elif dt.time(11, 30) <= time < dt.time(13):
            session = "午间休市"
        elif time >= dt.time(15):
            session = "已收盘"
        else:
            session = "交易中"
        close_today = dt.datetime.combine(day, dt.time(15), CHINA)
        after_close = close_today if trading_day and current < close_today else current
        reopening = self.next_open(after_close)
        closure_start = close_today if trading_day and current < close_today else current
        closure_hours = max(0, (reopening - closure_start).total_seconds() / 3600) if reopening else None
        next_session = self.next_open(current)
        return {
            "session": session,
            "session_open": opened,
            "trading_day": day.isoformat(),
            "is_trading_day": trading_day,
            "calendar_verified": known,
            "calendar_source": self.data.get("source"),
            "calendar_updated_at": self.data.get("updated_at"),
            "covered_years": sorted(self.years),
            "next_open": next_session.isoformat() if next_session else None,
            "next_open_after_close": reopening.isoformat() if reopening else None,
            "closure_hours": round(closure_hours, 2) if closure_hours is not None else None,
            "is_weekend_or_holiday": bool(closure_hours is not None and closure_hours > 24),
            "preclose_review_due": bool(opened and dt.time(14, 45) <= time < dt.time(15)),
            "known_events_status": "unknown",
            "known_events_note": "首版未接入新闻或宏观事件服务，事件风险信息未知，不能视为没有事件风险。",
            "overnight_allowed": True,
            "weekend_holiday_hold_allowed": True,
        }
