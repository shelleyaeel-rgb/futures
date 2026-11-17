"""Immutable daily rule versions with restart reuse and leased generation."""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import datetime as dt
import hashlib
import json
import time
import uuid

from .common import connect_db, iso_now
from .global_market import FACTORS
from .strategy import (EXECUTION_BOUNDS, PARAMETER_BOUNDS, base_strategy_v2,
                       validate_execution, validate_parameters)


DAILY_BOUNDS = {**PARAMETER_BOUNDS, **EXECUTION_BOUNDS}


def apply_daily_changes(base: dict, result: dict) -> dict:
    changes = result.get("changes")
    if not isinstance(changes, list) or len(changes) > 2:
        raise ValueError("Daily changes must contain at most two parameters")
    strategy = deepcopy(base)
    seen = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"parameter", "value"}:
            raise ValueError("Invalid daily parameter")
        name = change["parameter"]
        if not isinstance(name, str) or name not in DAILY_BOUNDS or name in seen:
            raise ValueError("Invalid daily parameter")
        seen.add(name)
        target = strategy["parameters"] if name in PARAMETER_BOUNDS else strategy["execution"]
        target[name] = change["value"]
    strategy["parameters"] = validate_parameters(strategy["parameters"])
    strategy["execution"] = validate_execution(strategy["execution"])
    return strategy


def validate_daily_result(base: dict, market: dict, result: dict) -> dict:
    if (not isinstance(result, dict) or set(result) != {"regime", "summary", "changes", "evidence", "limitations"}
            or result["regime"] not in ("normal", "risk_off", "volatile", "mixed")
            or not isinstance(result["summary"], str) or not result["summary"].strip() or len(result["summary"]) > 2000
            or not isinstance(result["evidence"], list) or len(result["evidence"]) > len(FACTORS)
            or not isinstance(result["limitations"], list) or len(result["limitations"]) > 10
            or any(not isinstance(item, str) or len(item) > 1000 for item in result["limitations"])):
        raise ValueError("Invalid daily result")
    strategy = apply_daily_changes(base, result)
    available = {item["id"] for item in market.get("factors", []) if item.get("status") == "available"}
    known = {factor[0] for factor in FACTORS}
    seen = set()
    for evidence in result["evidence"]:
        if (not isinstance(evidence, dict) or set(evidence) != {"factor", "interpretation"}
                or not isinstance(evidence["factor"], str) or evidence["factor"] not in available & known
                or evidence["factor"] in seen or not isinstance(evidence["interpretation"], str)
                or not evidence["interpretation"].strip() or len(evidence["interpretation"]) > 1000):
            raise ValueError("Invalid daily evidence")
        seen.add(evidence["factor"])
    if result["changes"] and not seen:
        raise ValueError("Daily changes require market evidence")
    return strategy


class DailyRules:
    def __init__(self, db_path, settings: dict):
        self.db_path = db_path
        self.settings = settings
        self.base = base_strategy_v2()
        self.fingerprint = hashlib.sha256(json.dumps(self.base, sort_keys=True).encode()).hexdigest()
        with closing(connect_db(db_path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS daily_rules(
                day TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL,
                next_attempt_at REAL NOT NULL, lease_token TEXT, lease_until REAL NOT NULL,
                payload TEXT NOT NULL)""")
            db.execute("INSERT OR IGNORE INTO strategy_versions VALUES(?,?,?,?,?)",
                       (self.base["version"], iso_now(), "trend-v1", "base", json.dumps(self.base, ensure_ascii=False)))

    def state(self, day: str) -> dict:
        with closing(connect_db(self.db_path)) as db:
            row = db.execute("SELECT * FROM daily_rules WHERE day=?", (day,)).fetchone()
        result = {"trading_day": day, "base": deepcopy(self.base), "status": "waiting", "active": False,
                  "message": "等待生成今日规则。", "attempts": 0, "strategy": deepcopy(self.base)}
        if row:
            payload = json.loads(row["payload"])
            if payload.get("base_fingerprint") == self.fingerprint:
                result.update(payload, status=row["status"], attempts=row["attempts"], next_attempt_at=row["next_attempt_at"])
                if row["status"] == "fallback" and row["attempts"] >= self.settings.get("max_attempts_per_day", 3):
                    result["message"] = "当前采用v2基础参数，今日自动尝试次数已用完，可手动重试。"
        return result

    def claim(self, day: str, *, force: bool = False) -> str | None:
        now = time.time()
        token = uuid.uuid4().hex
        with closing(connect_db(self.db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM daily_rules WHERE day=?", (day,)).fetchone()
            payload = json.loads(row["payload"]) if row else {}
            same_base = payload.get("base_fingerprint") == self.fingerprint
            if row and (row["lease_until"] > now or (same_base and row["status"] == "ready")
                        or (same_base and not force and (row["next_attempt_at"] > now
                            or (row["status"] != "generating" and row["attempts"] >= self.settings.get("max_attempts_per_day", 3))))):
                db.rollback()
                return None
            attempts = row["attempts"] + 1 if row and same_base else 1
            if not same_base:
                payload = {"active": False, "base_fingerprint": self.fingerprint}
            payload["message"] = "正在获取全球市场信息并生成今日规则。"
            db.execute("INSERT OR REPLACE INTO daily_rules VALUES(?,?,?,?,?,?,?)",
                       (day, "generating", attempts, 0, token, now + 120, json.dumps(payload, ensure_ascii=False)))
            db.commit()
        return token

    def finish(self, day: str, token: str, market: dict, response: dict) -> dict:
        error = response.get("error_code")
        result = response.get("result", {})
        strategy = deepcopy(self.base)
        status = "fallback"
        if response.get("status") == "ok":
            try:
                strategy = validate_daily_result(self.base, market, result)
                status = "ready"
            except (ValueError, KeyError, TypeError):
                error = "DAILY_RULES_INVALID"
                strategy = deepcopy(self.base)
        digest = hashlib.sha256(json.dumps(strategy, sort_keys=True).encode()).hexdigest()[:8]
        strategy.update(version=f"trend-v2-{day.replace('-', '')}-{digest}", base_version=self.base["version"],
                        trading_day=day, name="今日规则 · " + day)
        generated = iso_now()
        until = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat() + "T00:00:00+08:00"
        payload = {"active": True, "base_fingerprint": self.fingerprint, "strategy": strategy,
                   "generated_at": generated, "valid_until": until, "market": market,
                   "regime": result.get("regime", "unknown") if status == "ready" else "unknown",
                   "summary": result.get("summary", "") if status == "ready" else "生成暂未完成，当前采用v2基础参数。",
                   "changes": result.get("changes", []) if status == "ready" else [],
                   "evidence": result.get("evidence", []) if status == "ready" else [],
                   "limitations": result.get("limitations", []) if status == "ready" else [],
                   "error_code": error, "request_id": response.get("request_id"),
                   "message": "今日规则已生成，盘中由程序按该版本执行。" if status == "ready" else "当前采用v2基础参数，等待重试生成今日规则。"}
        with closing(connect_db(self.db_path)) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT lease_token FROM daily_rules WHERE day=?", (day,)).fetchone()
            if not row or row[0] != token:
                db.rollback()
                return self.state(day)
            db.execute("INSERT OR IGNORE INTO strategy_versions VALUES(?,?,?,?,?)",
                       (strategy["version"], generated, self.base["version"], "daily", json.dumps(strategy, ensure_ascii=False)))
            db.execute("UPDATE daily_rules SET status=?,next_attempt_at=?,lease_token=NULL,lease_until=0,payload=? WHERE day=?",
                       (status, time.time() + self.settings.get("retry_interval_seconds", 300), json.dumps(payload, ensure_ascii=False), day))
            db.commit()
        return self.state(day)
