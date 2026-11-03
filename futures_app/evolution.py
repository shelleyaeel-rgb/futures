"""Audited parameter hypotheses, chronological validation, and forward shadow trials.

This evaluates the deterministic entry/exit rules independently of the proposing
model. It does not train model weights or claim an AI score proves profitability.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from .strategy import (CHINA, PARAMETER_BOUNDS, aggregate_five_minute, baseline_decision,
                       build_context, closed_bars, default_strategy, finite_number,
                       timestamp, validate_parameters)


def _live_quote_valid(quote: dict, now: float) -> bool:
    symbol = quote.get("symbol", quote.get("instrument_id"))
    stamp = timestamp(quote.get("datetime"))
    expiry = timestamp(quote.get("expire_datetime"))
    if (not isinstance(symbol, str) or not re.fullmatch(r"CFFEX\.IF\d{2}(?:0[1-9]|1[0-2])", symbol)
            or stamp is None or not -2 <= now - stamp <= 10
            or quote.get("expired") is not False or expiry is None or expiry <= now
            or quote.get("session_open") is not True or quote.get("calendar_verified") is not True
            or quote.get("price_tick") != 0.2 or quote.get("volume_multiple") != 300):
        return False
    for key in ("last_price", "bid_price1", "ask_price1", "lower_limit", "upper_limit"):
        if not finite_number(quote.get(key)) or quote[key] <= 0:
            return False
    if not quote["lower_limit"] <= quote["bid_price1"] <= quote["ask_price1"] <= quote["upper_limit"]:
        return False
    if not quote["lower_limit"] <= quote["last_price"] <= quote["upper_limit"]:
        return False
    return all(finite_number(quote.get(key)) and quote[key] >= 1 for key in ("bid_volume1", "ask_volume1"))


def _costs(config: dict) -> dict | None:
    source = config.get("simulation_policy", {})
    rates = source.get("fee_rates", {})
    required = [rates.get(name) for name in ("open", "close_today", "close_previous")]
    required += [source.get("broker_commission_cny_per_side"), source.get("slippage_ticks"), source.get("margin_rate")]
    if any(not finite_number(value) or value < 0 for value in required) or not 0 < source.get("margin_rate", 0) < 1:
        return None
    if not source.get("assumption_version") or not source.get("assumption_label"):
        return None
    return deepcopy(source)


def _new_book() -> dict:
    return {"position": None, "realized_net": 0.0, "gross_pnl": 0.0, "fees": 0.0,
            "trades": 0, "wins": 0, "peak_pnl": 0.0, "max_drawdown": 0.0,
            "net_pnl": 0.0, "daily_baselines": {}, "blocked_days": [], "last_signal_bar": None, "symbol": None}


def _fee(price: float, category: str, costs: dict) -> float:
    return price * 300 * costs["fee_rates"][category] + costs["broker_commission_cny_per_side"]


def _mark(book: dict, quote: dict) -> None:
    position = book["position"]
    unrealized = 0.0
    if position:
        mark = quote["bid_price1"] if position["side"] == "long" else quote["ask_price1"]
        unrealized = (mark - position["entry_price"]) * (1 if position["side"] == "long" else -1) * 300
    book["net_pnl"] = book["realized_net"] + unrealized
    book["peak_pnl"] = max(book["peak_pnl"], book["net_pnl"])
    book["max_drawdown"] = max(book["max_drawdown"], book["peak_pnl"] - book["net_pnl"])


def _close(book: dict, quote: dict, costs: dict) -> None:
    position = book["position"]
    if not position:
        return
    direction = 1 if position["side"] == "long" else -1
    touch = quote["bid_price1"] if direction == 1 else quote["ask_price1"]
    price = touch - direction * costs["slippage_ticks"] * 0.2
    if ((finite_number(quote.get("lower_limit")) and price < quote["lower_limit"])
            or (finite_number(quote.get("upper_limit")) and price > quote["upper_limit"])):
        return
    day = datetime.fromtimestamp(timestamp(quote["datetime"]), CHINA).date().isoformat()
    fee = _fee(price, "close_today" if day == position["opened_day"] else "close_previous", costs)
    gross = (price - position["entry_price"]) * direction * 300
    book["realized_net"] += gross - fee
    book["gross_pnl"] += gross
    book["fees"] += fee
    book["trades"] += 1
    book["wins"] += int(gross - fee - position["entry_fee"] > 0)
    book["position"] = None
    _mark(book, quote)


def _trail(book: dict, quote: dict) -> None:
    position = book["position"]
    last = quote.get("last_price")
    if not position or not finite_number(last) or last <= 0:
        return
    direction = 1 if position["side"] == "long" else -1
    distance = position.setdefault("initial_risk_distance", max(abs(position["entry_price"] - position["stop_loss"]), 0.2))
    previous_best = position.get("best_price", position["entry_price"])
    best = max(previous_best, last) if direction == 1 else min(previous_best, last)
    position["best_price"] = best
    if direction * (best - position["entry_price"]) >= distance:
        candidate = best - direction * distance
        candidate = (math.floor(candidate / 0.2 + 1e-8) if direction == 1 else math.ceil(candidate / 0.2 - 1e-8)) * 0.2
        if direction * (candidate - position["stop_loss"]) > 0:
            position["stop_loss"] = round(candidate, 8)


def _paper_step(book: dict, strategy: dict, quote: dict, bars1: list, bars5: list,
                costs: dict, account: dict, *, bar_range: dict | None = None) -> None:
    """One side of a paired shadow run; no external orders or model calls."""
    as_of = timestamp(quote.get("datetime"))
    if as_of is None or any(not finite_number(quote.get(key)) or quote[key] <= 0 for key in ("bid_price1", "ask_price1")):
        return
    if quote["bid_price1"] > quote["ask_price1"]:
        return
    day = datetime.fromtimestamp(as_of, CHINA).date().isoformat()
    book["daily_baselines"].setdefault(day, book["net_pnl"])
    _mark(book, quote)
    blocked_days = book.setdefault("blocked_days", [])
    if book["net_pnl"] - book["daily_baselines"][day] <= -account.get("daily_loss_stop_new_entries_cny", 20000) and day not in blocked_days:
        blocked_days.append(day)
    position = book["position"]
    if position:
        touch = quote["bid_price1"] if position["side"] == "long" else quote["ask_price1"]
        hit = (touch <= position["stop_loss"] or touch >= position["take_profit"]) if position["side"] == "long" else (touch >= position["stop_loss"] or touch <= position["take_profit"])
        if hit:
            _close(book, quote, costs)
            return
    _trail(book, quote)
    one = closed_bars(bars1, 60, as_of)
    latest = one[-1]["datetime"] if one else None
    if latest is not None and latest != book["last_signal_bar"]:
        book["last_signal_bar"] = latest
        blocked = day in blocked_days
        context = build_context(quote, one, bars5, {"position": book["position"], "account": {"entry_blocked": blocked}}, {}, strategy)
        decision = baseline_decision(context)
        if decision["action"] == "close" and book["position"]:
            _close(book, quote, costs)
            return
        if decision["action"].startswith("open_") and not book["position"] and not blocked:
            direction = 1 if decision["action"] == "open_long" else -1
            touch = quote["ask_price1"] if direction == 1 else quote["bid_price1"]
            price = touch + direction * costs["slippage_ticks"] * 0.2
            fee = _fee(price, "open", costs)
            exit_allowance = quote["ask_price1"] - quote["bid_price1"] + costs["slippage_ticks"] * 0.2
            stop_fill = decision["stop_loss"] - direction * exit_allowance
            closing_kind = max(("close_today", "close_previous"), key=lambda key: costs["fee_rates"][key])
            stop_risk = abs(price - stop_fill) * 300 + fee + _fee(stop_fill, closing_kind, costs)
            equity = account.get("initial_balance_cny", 1000000) + book["net_pnl"]
            last_price = quote.get("last_price", (quote["bid_price1"] + quote["ask_price1"]) / 2)
            equity_after = equity - fee + (last_price - price) * direction * 300
            within_limits = not ((finite_number(quote.get("lower_limit")) and price < quote["lower_limit"])
                or (finite_number(quote.get("upper_limit")) and price > quote["upper_limit"]))
            daily_after = equity_after - account.get("initial_balance_cny", 1000000) - book["daily_baselines"][day]
            direction_valid = (direction * (price - decision["stop_loss"]) > 0
                               and direction * (decision["take_profit"] - price) > 0)
            if (within_limits and direction_valid and stop_risk <= account.get("planned_risk_per_trade_cny", 5000)
                    and last_price * 300 * costs["margin_rate"] <= equity_after
                    and daily_after > -account.get("daily_loss_stop_new_entries_cny", 20000)):
                book["position"] = {"side": "long" if direction == 1 else "short", "quantity": 1,
                    "entry_price": price, "entry_fee": fee, "opened_day": day,
                    "stop_loss": decision["stop_loss"], "take_profit": decision["take_profit"],
                    "initial_risk_distance": abs(price - decision["stop_loss"]), "best_price": price}
                book["realized_net"] -= fee
                book["fees"] += fee
    position = book["position"]
    if bar_range and position:
        # Historical minute bars lack a tick path. If both levels hit, take the
        # adverse stop first. Gap exits use the opening price if worse.
        direction = 1 if position["side"] == "long" else -1
        stopped = bar_range["low"] <= position["stop_loss"] if direction == 1 else bar_range["high"] >= position["stop_loss"]
        targeted = bar_range["high"] >= position["take_profit"] if direction == 1 else bar_range["low"] <= position["take_profit"]
        if stopped or targeted:
            if stopped:
                fill_reference = min(bar_range["open"], position["stop_loss"]) if direction == 1 else max(bar_range["open"], position["stop_loss"])
            else:
                fill_reference = position["take_profit"]
            exit_quote = {**quote, "bid_price1": fill_reference - 0.2, "ask_price1": fill_reference + 0.2}
            _close(book, exit_quote, costs)
    _mark(book, quote)


def evaluate_historical(strategy: dict, bars: list[dict], validation_start: float,
                        costs: dict, account: dict) -> dict:
    """Decision uses previous bars; execution is at the next bar's opening."""
    book = _new_book()
    for index, row in enumerate(bars):
        if row["datetime"] < validation_start:
            continue
        history = bars[max(0, index - 400):index]
        quote = {"datetime": row["datetime"], "symbol": "HISTORICAL_IF", "price_tick": 0.2,
                 "last_price": row["open"], "bid_price1": row["open"] - 0.2, "ask_price1": row["open"] + 0.2}
        _paper_step(book, strategy, quote, history, aggregate_five_minute(history), costs, account, bar_range=row)
        close_quote = {**quote, "last_price": row["close"], "bid_price1": row["close"] - 0.2, "ask_price1": row["close"] + 0.2}
        _mark(book, close_quote)
        _trail(book, close_quote)
    if bars and book["position"]:
        last = bars[-1]
        _close(book, {"datetime": last["datetime"] + 60, "bid_price1": last["close"] - 0.2, "ask_price1": last["close"] + 0.2}, costs)
    return {key: book[key] for key in ("net_pnl", "gross_pnl", "fees", "trades", "wins", "max_drawdown")}


class StrategyEvolution:
    def __init__(self, db_path: Path, config: dict):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = deepcopy(config)
        self.settings = {"enabled": True, "minimum_historical_bars": 600, "min_validation_trades": 10,
            "min_shadow_trades": 10, "min_shadow_days": 3, "review_interval_hours": 24,
            "minimum_net_profit_improvement_cny": 100, "maximum_drawdown_ratio": 1.0,
            **config.get("evolution", {})}
        self.lock = threading.RLock()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS strategy_versions(version TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                  parent_version TEXT, status TEXT NOT NULL, strategy_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS strategy_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS strategy_candidates(version TEXT PRIMARY KEY,parent_version TEXT NOT NULL,
                  created_at REAL NOT NULL,status TEXT NOT NULL,reason TEXT,metrics_json TEXT NOT NULL,shadow_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS strategy_events(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,
                  event TEXT NOT NULL,version TEXT,details_json TEXT NOT NULL);
            """)
            strategy = default_strategy()
            db.execute("INSERT OR IGNORE INTO strategy_versions VALUES(?,?,?,?,?)", (strategy["version"], datetime.now(CHINA).isoformat(), None, "current", json.dumps(strategy, ensure_ascii=False)))
            db.execute("INSERT OR IGNORE INTO strategy_meta VALUES('current',?)", (strategy["version"],))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _event(db, event: str, version: str | None, details: dict) -> None:
        db.execute("INSERT INTO strategy_events(created_at,event,version,details_json) VALUES(?,?,?,?)",
                   (datetime.now(CHINA).isoformat(), event, version, json.dumps(details, ensure_ascii=False, allow_nan=False)))

    def current_strategy(self) -> dict:
        with self._db() as db:
            row = db.execute("SELECT strategy_json FROM strategy_versions WHERE version=(SELECT value FROM strategy_meta WHERE key='current')").fetchone()
        return json.loads(row[0])

    def strategy_for_version(self, version: str) -> dict:
        with self._db() as db:
            row = db.execute("SELECT strategy_json FROM strategy_versions WHERE version=?", (version,)).fetchone()
        if row is None:
            raise ValueError("UNKNOWN_STRATEGY_VERSION")
        return json.loads(row[0])

    def _pending(self, status: str, message: str) -> dict:
        value = {"status": status, "message": message, "at": datetime.now(CHINA).isoformat()}
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO strategy_meta VALUES('last_review',?)", (json.dumps(value, ensure_ascii=False),))
        return value

    def state(self) -> dict:
        with self._db() as db:
            candidates = db.execute("SELECT * FROM strategy_candidates ORDER BY created_at DESC LIMIT 20").fetchall()
            events = db.execute("SELECT * FROM strategy_events ORDER BY id DESC LIMIT 30").fetchall()
            last = db.execute("SELECT value FROM strategy_meta WHERE key='last_review'").fetchone()
        return {"current": self.current_strategy(), "enabled": bool(self.settings["enabled"]),
                "last_review": json.loads(last[0]) if last else None,
                "candidates": [{"version": row["version"], "parent_version": row["parent_version"], "status": row["status"],
                                "created_at": row["created_at"], "reason": row["reason"],
                                "metrics": json.loads(row["metrics_json"]), "shadow": json.loads(row["shadow_json"])} for row in candidates],
                "events": [{"at": row["created_at"], "event": row["event"], "version": row["version"],
                            "details": json.loads(row["details_json"])} for row in events],
                "method": "时间留出验证 + 真实未来行情规则影子对照；无模型权重训练、无AI自评分。"}

    def _better(self, candidate: dict, baseline: dict, minimum_trades: int) -> bool:
        return (candidate["trades"] >= minimum_trades and baseline["trades"] >= minimum_trades
                and candidate["net_pnl"] > 0
                and candidate["net_pnl"] - baseline["net_pnl"] >= self.settings["minimum_net_profit_improvement_cny"]
                and candidate["max_drawdown"] <= baseline["max_drawdown"] * self.settings["maximum_drawdown_ratio"])

    def maybe_review(self, history: list, bars: list, api_key: str, analyst) -> dict:
        if not self.settings["enabled"]:
            return self._pending("disabled", "策略演进已关闭。")
        if not self.lock.acquire(blocking=False):
            return {"status": "busy", "message": "已有策略验证正在运行。"}
        try:
            costs = _costs(self.config)
            if costs is None:
                return self._pending("pending_costs", "费用假设不完整，无法公正比较候选。")
            with self._db() as db:
                active = db.execute("SELECT version FROM strategy_candidates WHERE status='shadow'").fetchone()
                last_attempt = db.execute("SELECT value FROM strategy_meta WHERE key='last_attempt'").fetchone()
            if active:
                return self._pending("shadow_running", "正在等待未来影子样本，当前版本保持不变。")
            now = time.time()
            if last_attempt and now - float(last_attempt[0]) < self.settings["review_interval_hours"] * 3600:
                return {"status": "not_due", "message": "尚未到下一次策略评估时间。"}
            cleaned = closed_bars(bars, 60, now)
            if len(cleaned) < max(300, self.settings["minimum_historical_bars"]):
                return self._pending("pending_history", "历史已完成K线不足，保留当前策略。")
            split = int(len(cleaned) * 0.6)
            training, validation = cleaned[:split], cleaned[split:]
            current = self.current_strategy()
            # Do not send any holdout bars, or trades that finish in the holdout, to the proposer.
            training_end = training[-1]["datetime"] + 60
            trades = history.get("trades", []) if isinstance(history, dict) else (history or [])
            training_trades = []
            for row in trades:
                if not isinstance(row, dict):
                    continue
                exit_at = timestamp(row.get("closed_at", row.get("exit_time", row.get("quote_time", row.get("time")))))
                if exit_at is not None and exit_at <= training_end and row.get("action", "close") == "close":
                    training_trades.append(row)
            with self._db() as db:
                db.execute("INSERT OR REPLACE INTO strategy_meta VALUES('last_attempt',?)", (str(now),))
            proposal = analyst.propose_parameters({"current_strategy": current,
                "allowed_parameters": {key: {"min": values[0], "max": values[1], "integer": values[2] is int} for key, values in PARAMETER_BOUNDS.items()},
                "training_bars": training[-300:], "training_trades": training_trades[-30:],
                "training_end": training_end, "cost_assumptions": costs}, api_key)
            if proposal.get("status") != "ok":
                return self._pending("proposal_failed", "模型未提出有效候选；当前策略未变。")
            proposed = proposal.get("result", {})
            changes = proposed.get("changes", [])
            if not changes:
                return self._pending("no_change", "本次没有新参数假设，保留当前版本。")
            try:
                if not isinstance(changes, list) or len(changes) > 2 or len({row["parameter"] for row in changes}) != len(changes):
                    raise ValueError()
                parameters = validate_parameters({row["parameter"]: row["value"] for row in changes}, current["parameters"])
            except (ValueError, KeyError, TypeError):
                return self._pending("candidate_rejected", "候选越过参数白名单或范围，已拒绝。")
            if parameters == current["parameters"]:
                return self._pending("no_change", "候选参数与当前相同。")
            candidate = {**current, "version": "trend-" + uuid.uuid4().hex[:12], "parameters": parameters}
            baseline_metrics = evaluate_historical(current, cleaned, validation[0]["datetime"], costs, self.config.get("account", {}))
            candidate_metrics = evaluate_historical(candidate, cleaned, validation[0]["datetime"], costs, self.config.get("account", {}))
            metrics = {"training_end": training_end, "validation_start": validation[0]["datetime"],
                "validation_end": validation[-1]["datetime"] + 60, "training_bars": len(training), "validation_bars": len(validation),
                "baseline": baseline_metrics, "candidate": candidate_metrics,
                "cost_assumption_version": costs["assumption_version"],
                "cost_assumptions": costs,
                "assumptions": "1分钟开盘执行；固定2跳价差假设加配置滑点；同柱双触发先止损；期末平仓，未声称逐笔回放。"}
            passed = self._better(candidate_metrics, baseline_metrics, self.settings["min_validation_trades"])
            status = "shadow" if passed else "historical_rejected"
            if min(candidate_metrics["trades"], baseline_metrics["trades"]) < self.settings["min_validation_trades"]:
                status = "pending_validation_samples"
            shadow = {"baseline": _new_book(), "candidate": _new_book(), "days": [], "ticks": 0,
                      "started_at": time.time(), "last_timestamp": 0, "promotion_checkpoint": None}
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO strategy_versions VALUES(?,?,?,?,?)", (candidate["version"], datetime.now(CHINA).isoformat(), current["version"], "candidate", json.dumps(candidate, ensure_ascii=False)))
                db.execute("INSERT INTO strategy_candidates VALUES(?,?,?,?,?,?,?)", (candidate["version"], current["version"], now, status, str(proposed.get("reason", ""))[:2000], json.dumps(metrics), json.dumps(shadow)))
                self._event(db, "candidate_evaluated", candidate["version"], {"status": status, "parameters": parameters, "metrics": metrics})
                db.commit()
            return self._pending(status, "历史留出验证通过，开始真实未来行情影子对照；尚未升级。" if passed else "候选尚未满足独立验证门槛，保留当前策略。")
        finally:
            self.lock.release()

    def observe(self, quote: dict, bars1: list, bars5: list) -> None:
        if not self.settings["enabled"] or not self.lock.acquire(blocking=False):
            return
        try:
            costs = _costs(self.config)
            stamp = timestamp(quote.get("datetime"))
            # A downloaded historical snapshot is never a forward observation.
            if costs is None or not _live_quote_valid(quote, time.time()):
                return
            with self._db() as db:
                rows = db.execute("SELECT * FROM strategy_candidates WHERE status IN ('shadow','promoted')").fetchall()
            for row in rows:
                if json.loads(row["metrics_json"]).get("cost_assumptions") != costs:
                    with self._db() as db:
                        db.execute("UPDATE strategy_candidates SET status='cost_changed' WHERE version=?", (row["version"],))
                        self._event(db, "shadow_stopped_cost_change", row["version"], {})
                    continue
                shadow = json.loads(row["shadow_json"])
                if stamp <= max(shadow["started_at"], shadow["last_timestamp"]):
                    continue
                candidate = self.strategy_for_version(row["version"])
                baseline = self.strategy_for_version(row["parent_version"])
                symbol = quote.get("symbol", quote.get("instrument_id"))
                if not isinstance(symbol, str) or not symbol.startswith("CFFEX.IF"):
                    continue
                if shadow.get("symbol") not in (None, symbol):
                    with self._db() as db:
                        db.execute("UPDATE strategy_candidates SET status='contract_changed' WHERE version=?", (row["version"],))
                        self._event(db, "shadow_stopped_contract_change", row["version"], {})
                    continue
                shadow["symbol"] = symbol
                shadow["last_timestamp"] = stamp
                shadow["ticks"] += 1
                day = datetime.fromtimestamp(stamp, CHINA).date().isoformat()
                if day not in shadow["days"]:
                    shadow["days"].append(day)
                for label, strategy in (("baseline", baseline), ("candidate", candidate)):
                    _paper_step(shadow[label], strategy, quote, bars1, bars5, costs, self.config.get("account", {}))
                status = row["status"]
                both_flat = shadow["baseline"]["position"] is None and shadow["candidate"]["position"] is None
                enough_days = len(shadow["days"]) >= self.settings["min_shadow_days"]
                if status == "shadow" and both_flat and enough_days and self._better(shadow["candidate"], shadow["baseline"], self.settings["min_shadow_trades"]):
                    with self._db() as db:
                        db.execute("BEGIN IMMEDIATE")
                        current = db.execute("SELECT value FROM strategy_meta WHERE key='current'").fetchone()[0]
                        if current == row["parent_version"]:
                            db.execute("UPDATE strategy_meta SET value=? WHERE key='current'", (row["version"],))
                            db.execute("UPDATE strategy_versions SET status='previous' WHERE version=?", (current,))
                            db.execute("UPDATE strategy_candidates SET status='superseded' WHERE version=? AND status='promoted'", (current,))
                            db.execute("UPDATE strategy_versions SET status='current' WHERE version=?", (row["version"],))
                            shadow["promotion_checkpoint"] = {label: {key: shadow[label][key] for key in ("net_pnl", "trades")} for label in ("baseline", "candidate")}
                            status = "promoted"
                            self._event(db, "promoted_for_new_entries", row["version"], {"shadow": shadow, "historical": json.loads(row["metrics_json"])})
                        else:
                            status = "superseded"
                        db.commit()
                elif status == "promoted" and both_flat and shadow.get("promotion_checkpoint"):
                    checkpoint = shadow["promotion_checkpoint"]
                    candidate_delta = shadow["candidate"]["net_pnl"] - checkpoint["candidate"]["net_pnl"]
                    baseline_delta = shadow["baseline"]["net_pnl"] - checkpoint["baseline"]["net_pnl"]
                    new_trades = shadow["candidate"]["trades"] - checkpoint["candidate"]["trades"]
                    if new_trades >= self.settings["min_shadow_trades"] and candidate_delta < baseline_delta - max(100, self.settings["minimum_net_profit_improvement_cny"]):
                        with self._db() as db:
                            db.execute("BEGIN IMMEDIATE")
                            current = db.execute("SELECT value FROM strategy_meta WHERE key='current'").fetchone()[0]
                            if current == row["version"]:
                                db.execute("UPDATE strategy_meta SET value=? WHERE key='current'", (row["parent_version"],))
                                db.execute("UPDATE strategy_versions SET status='rolled_back' WHERE version=?", (row["version"],))
                                db.execute("UPDATE strategy_versions SET status='current' WHERE version=?", (row["parent_version"],))
                                self._event(db, "rolled_back_for_new_entries", row["version"], {"candidate_delta": candidate_delta, "baseline_delta": baseline_delta})
                                status = "rolled_back"
                            db.commit()
                with self._db() as db:
                    db.execute("UPDATE strategy_candidates SET status=?,shadow_json=? WHERE version=?", (status, json.dumps(shadow, allow_nan=False), row["version"]))
        finally:
            self.lock.release()
