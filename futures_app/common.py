from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CHINA = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")


def now_china() -> dt.datetime:
    return dt.datetime.now(CHINA)


def iso_now() -> str:
    return now_china().isoformat()


def parse_time(value: Any) -> dt.datetime | None:
    try:
        stamp = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
        return stamp.replace(tzinfo=CHINA) if stamp.tzinfo is None else stamp.astimezone(CHINA)
    except (ValueError, TypeError):
        return None


def safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_json(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def connect_db(path: Path | str) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=15, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=15000")
    return connection


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(safe_json(data), ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)
