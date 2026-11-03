"""Application coordinator. Quote/risk loop never waits on model HTTP requests."""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
import threading
import time

from futures_probe.secrets import get_secret
from .ai import LunaAnalyst
from .broker import PaperBroker
from .calendar import ExchangeCalendar
from .common import ROOT, connect_db, iso_now, now_china, parse_time, safe_json
from .evolution import StrategyEvolution
from .feed import MarketFeed
from .strategy import baseline_decision, build_context


class TradingService:
    def __init__(self, root: Path = ROOT, *, feed=None, broker=None, analyst=None, evolution=None):
        self.root = Path(root)
        self.config = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.db_path = self.root / self.config.get("app", {}).get("database", "data/futures.sqlite3")
        self.db = connect_db(self.db_path)
        self.db.execute("CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS app_reviews(id INTEGER PRIMARY KEY, trading_day TEXT NOT NULL, created_at TEXT NOT NULL,payload TEXT NOT NULL)")
        self.db.commit()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._model_lock = threading.Lock()
        self.calendar = ExchangeCalendar(self.root / "calendar.json")
        self.feed = feed or MarketFeed(self.root / "data" / "market-cache.json")
        self.broker = broker or PaperBroker(self.db_path, self.config)
        self.analyst = analyst or LunaAnalyst(self.db_path, self.config)
        self.evolution = evolution or StrategyEvolution(self.db_path, self.config)
        row = self.db.execute("SELECT value FROM app_settings WHERE key='running'").fetchone()
        running = bool(json.loads(row[0])) if row else bool(self.config.get("automatic_trading_enabled", False))
        self.runtime = {"running": running, "started_at": iso_now(), "last_error": None, "model_busy": False}
        self.ai_state = {"status": "等待分析", "last_decision": None, "next_analysis_at": None}
        last_decision = self.db.execute("SELECT value FROM app_settings WHERE key='last_decision'").fetchone()
        if last_decision:
            self.ai_state["last_decision"] = json.loads(last_decision[0])
        self._last_analysis = 0.0
        self._last_signal_check = 0.0
        row = self.db.execute("SELECT value FROM app_settings WHERE key='last_signal_review'").fetchone()
        self._last_signal_review = json.loads(row[0]) if row else None
        self._last_quote_key = None
        self._last_risk_check = 0.0
        self._last_error_time = 0.0
        self._last_review_check = 0.0
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        snapshot = self.broker.snapshot()
        position = snapshot.get("position")
        self.feed.start(position.get("symbol") if position else None)
        self._thread = threading.Thread(target=self._run, name="paper-trading-risk-loop", daemon=True)
        self._thread.start()
        self.broker.record_event("system", "本地模拟服务已启动，账户与持仓从账本恢复。")

    def stop(self) -> None:
        self._stop.set()
        self.feed.stop()
        if hasattr(self, "_thread"):
            self._thread.join(timeout=5)
        for thread in self._threads:
            thread.join(timeout=2)
        if any(thread.is_alive() for thread in self._threads):
            return  # Process exit ends daemon jobs; never close their DB mid-transaction.
        for component in (self.broker, self.analyst, self.evolution):
            if hasattr(component, "close"):
                component.close()
        self.db.close()

    def _market(self) -> tuple[dict, dict, dict]:
        market = self.feed.snapshot()
        calendar = self.calendar.context()
        quote = copy.deepcopy(market.get("quote") or {})
        quote["session_open"] = calendar["session_open"]
        quote["calendar_verified"] = calendar["calendar_verified"]
        stamp = parse_time(quote.get("datetime"))
        quote_age = (now_china() - stamp).total_seconds() if stamp else None
        market["quote_age_seconds"] = round(quote_age, 3) if quote_age is not None else None
        market["quote_is_fresh"] = bool(quote_age is not None and -2 <= quote_age <= 10)
        market["session"] = calendar["session"]
        market["quote"] = quote or None
        return market, calendar, quote

    def _context(self, market: dict, calendar: dict, quote: dict) -> dict:
        account = self.broker.snapshot(quote or None)
        position = account.get("position")
        strategy = self.evolution.current_strategy()
        if position and position.get("strategy_version"):
            strategy = self.evolution.strategy_for_version(position["strategy_version"])
        context = build_context(
            quote, market.get("bars_1m", []), market.get("bars_5m", []),
            account, calendar, strategy,
        )
        context["execution_allowed"] = bool(calendar["session_open"] and market.get("quote_is_fresh") and market.get("status") == "connected")
        context["cost_assumptions"] = self.config["simulation_policy"]
        context["information_gaps"] = [calendar["known_events_note"]]
        if not context["execution_allowed"]:
            context["eligible_actions"] = ["hold"]
        return context

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            try:
                market, calendar, quote = self._market()
                if quote.get("datetime"):
                    key = tuple(quote.get(field) for field in (
                        "symbol", "datetime", "last_price", "bid_price1", "ask_price1", "bid_volume1", "ask_volume1", "settlement"
                    ))
                    changed = key != self._last_quote_key
                    holding_needs_clock_check = time.monotonic() - self._last_risk_check >= 1 and bool(self.broker.snapshot().get("position"))
                    if changed or holding_needs_clock_check:
                        self._last_quote_key = key
                        self._last_risk_check = time.monotonic()
                        result = self.broker.on_quote(quote)
                        position = result.get("position")
                        self.feed.watch_position(position.get("symbol") if position else None)
                        if changed:
                            self.evolution.observe(quote, market.get("bars_1m", []), market.get("bars_5m", []))
                interval = self.config["model"]["analysis_interval_seconds"]
                if self.runtime["running"] and not self.runtime["model_busy"] and calendar["session_open"] and market.get("quote_is_fresh") and market.get("status") == "connected":
                    if time.monotonic() - self._last_signal_check >= interval:
                        self._last_signal_check = time.monotonic()
                        self.request_analysis(signal_only=True)
                remaining = max(0, interval - (time.monotonic() - self._last_signal_check))
                with self._lock:
                    self.ai_state["next_analysis_at"] = (now_china() + dt.timedelta(seconds=remaining)).isoformat() if self.runtime["running"] and calendar["session_open"] else None
                if time.monotonic() - self._last_review_check > 30:
                    self._last_review_check = time.monotonic()
                    review_time = self.config.get("app", {}).get("review_time", "15:10")
                    if self.runtime["running"] and calendar["is_trading_day"] and now_china().strftime("%H:%M") >= review_time and market.get("bars_1m"):
                        day = now_china().date().isoformat()
                        with self._lock:
                            exists = self.db.execute("SELECT 1 FROM app_reviews WHERE trading_day=? LIMIT 1", (day,)).fetchone()
                            row = self.db.execute("SELECT value FROM app_settings WHERE key=?", ("review_attempt:" + day,)).fetchone()
                        attempt = json.loads(row[0]) if row else {}
                        retry_at = parse_time(attempt.get("at"))
                        retry_ready = retry_at is None or (now_china() - retry_at).total_seconds() >= 300
                        if not exists and attempt.get("count", 0) < 3 and retry_ready:
                            self.request_review()
            except Exception as exc:
                with self._lock:
                    self.runtime["last_error"] = f"后台处理暂时失败（{type(exc).__name__}）"
                if time.monotonic() - self._last_error_time > 60:
                    self._last_error_time = time.monotonic()
                    self.broker.record_event("error", "后台处理异常，等待重试。", {"error_code": type(exc).__name__})

    def _launch_model_job(self, target, label: str) -> dict:
        if not self._model_lock.acquire(blocking=False):
            return {"status": "busy", "message": "已有模型任务运行，完成后再试。"}
        with self._lock:
            self.runtime["model_busy"] = True
            self.ai_state["status"] = label

        def job():
            try:
                target()
            except Exception as exc:
                with self._lock:
                    self.ai_state["status"] = "模型任务失败，程序风控继续运行"
                    self.runtime["last_error"] = f"模型任务失败（{type(exc).__name__}）"
                self.broker.record_event("error", "模型任务失败，未新增仓位。", {"error_code": type(exc).__name__})
            finally:
                with self._lock:
                    self.runtime["model_busy"] = False
                self._model_lock.release()

        thread = threading.Thread(target=job, name="luna-task", daemon=True)
        self._threads = [item for item in self._threads if item.is_alive()]
        self._threads.append(thread)
        thread.start()
        return {"status": "accepted", "message": label}

    @staticmethod
    def _signal_review(context: dict) -> tuple[dict, str | None]:
        """Narrow the reviewer to the local proposal; never invent a trade."""
        candidate = baseline_decision(context)
        action = candidate["action"]
        bar = context.get("indicators", {}).get("last_closed_1m", {})
        if (not context.get("execution_allowed") or not context.get("data_ready")
                or action == "hold" or action not in context.get("eligible_actions", [])
                or not bar.get("datetime")):
            return context, None
        context = copy.deepcopy(context)
        context["eligible_actions"] = ["hold", action]
        context["local_proposal"] = candidate
        context["review_role"] = "审核本地规则信号：只能同意候选动作或hold拒绝；规则改进在收盘复盘后独立验证。"
        position = context.get("broker", {}).get("position") or {}
        key = json.dumps([context["quote"].get("symbol"), context["strategy"]["version"],
                          bar["datetime"], action, position.get("opened_at")], ensure_ascii=False)
        return context, key

    def request_analysis(self, *, signal_only: bool = False) -> dict:
        market, calendar, quote = self._market()
        if not quote.get("datetime") or not market.get("bars_1m"):
            return {"status": "blocked", "message": "等待行情和K线就绪。"}
        context = self._context(market, calendar, quote)
        signal_key = None
        if signal_only:
            context, signal_key = self._signal_review(context)
            if signal_key is None or signal_key == self._last_signal_review:
                with self._lock:
                    self.ai_state["status"] = "本地检查完成：等待新信号" if signal_key is None else "该K线信号已送审，等待新K线"
                return {"status": "skipped", "message": self.ai_state["status"]}
        started = time.monotonic()

        def task():
            self._last_analysis = started
            key = get_secret("OPENAI_API_KEY")
            if not key:
                with self._lock:
                    self.ai_state["status"] = "模型密钥未配置"
                return
            if signal_key is not None:
                with self._lock:
                    self._last_signal_review = signal_key
                    self.db.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('last_signal_review',?)", (json.dumps(signal_key),))
                    self.db.commit()
            decision = self.analyst.analyze(context, key)
            current, current_calendar, fresh_quote = self._market()
            signal_valid = True
            if signal_key is not None:
                fresh_context, fresh_key = self._signal_review(self._context(current, current_calendar, fresh_quote))
                signal_valid = fresh_key == signal_key and decision.get("action") in fresh_context.get("eligible_actions", [])
            can_execute = (
                self.runtime["running"] and current_calendar["session_open"]
                and current.get("quote_is_fresh") and current.get("status") == "connected"
                and fresh_quote.get("symbol") == quote.get("symbol")
                and time.monotonic() - started <= self.config["model"]["request_timeout_seconds"]
                and signal_valid
            )
            position = self.broker.snapshot(fresh_quote).get("position")
            if position and decision.get("action") in ("close", "hold"):
                decision["strategy_version"] = position.get("strategy_version")
            if can_execute and decision.get("status") in ("ok", "success", "passed", None):
                result = self.broker.execute(decision, fresh_quote, context.get("indicators"))
                decision["execution"] = {key: result.get(key) for key in ("status", "reason")}
            else:
                decision["execution"] = {"status": "not_executed", "reason": "仅分析：当前休市、行情过期、模拟已暂停、信号已变化，或模型请求未通过。"}
            with self._lock:
                self.ai_state["last_decision"] = decision
                self.ai_state["status"] = "分析完成" if decision.get("status") == "ok" else "本次观望"
                self.runtime["last_error"] = None
                self.db.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('last_decision',?)", (json.dumps(safe_json(decision), ensure_ascii=False),))
                self.db.commit()
            self.broker.record_event("analysis", str(decision.get("reason", "已完成一次模型分析。"))[:1000], {"action": decision.get("action"), "execution": decision.get("execution")})

        return self._launch_model_job(task, "Luna 正在审核信号" if signal_only else "Luna 正在分析")

    def request_review(self) -> dict:
        market, calendar, quote = self._market()
        if not market.get("bars_1m"):
            return {"status": "blocked", "message": "等待行情历史就绪。"}

        def task():
            key = get_secret("OPENAI_API_KEY")
            if not key:
                with self._lock:
                    self.ai_state["status"] = "模型密钥未配置"
                return
            history = self.broker.history(limit=1000)
            day = now_china().date().isoformat()
            with self._lock:
                row = self.db.execute("SELECT value FROM app_settings WHERE key=?", ("review_attempt:" + day,)).fetchone()
                attempt = json.loads(row[0]) if row else {}
                self.db.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES(?,?)", ("review_attempt:" + day, json.dumps({"at": iso_now(), "count": attempt.get("count", 0) + 1})))
                self.db.commit()
            day_trades = [trade for trade in history.get("trades", []) if trade.get("trading_day") == day]
            day_decisions = [decision for decision in self.analyst.recent_decisions(limit=60) if str(decision.get("created_at", "")).startswith(day) and decision.get("kind") == "decision"]
            # A limited context keeps review and optimization within the same daily budget.
            review = self.analyst.daily_review({
                "trading_day": day, "account": self.broker.snapshot(quote),
                "trades": day_trades[:100],
                "decisions": day_decisions[:30],
                "record_scope": "trades和decisions仅含所标交易日；account是累计账户快照。平仓net_pnl是整笔交易净收益，可能包含以前交易日的开仓费，不等同当日盈亏；当日总盈亏以account.account.daily_pnl为准。",
                "market": {"symbol": market.get("symbol"), "quote": quote, "recent_bars": market.get("bars_1m", [])[-60:]},
                "closure_risk": calendar,
                "cost_assumptions": self.config["simulation_policy"],
            }, key)
            if review.get("status") == "ok":
                with self._lock:
                    self.db.execute("INSERT INTO app_reviews(trading_day,created_at,payload) VALUES(?,?,?)", (day, iso_now(), json.dumps(safe_json(review), ensure_ascii=False)))
                    self.db.commit()
                self.broker.record_event("review", "当日复盘已保存，开始检查策略改进候选。")
                try:
                    evolution = self.evolution.maybe_review(history, market.get("bars_1m", []), key, self.analyst)
                    self.broker.record_event("strategy", "策略候选检查完成。", safe_json(evolution))
                except Exception as exc:
                    self.broker.record_event("strategy", "策略检查暂未完成，继续沿用当前版本。", {"error_code": type(exc).__name__})
                with self._lock:
                    self.ai_state["status"] = "复盘已完成"
            else:
                with self._lock:
                    self.ai_state["status"] = "复盘暂未完成，稍后可重试"

        return self._launch_model_job(task, "正在生成复盘")

    def control(self, action: str) -> dict:
        if action in ("start", "pause"):
            running = action == "start"
            with self._lock:
                self.runtime["running"] = running
                self.db.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('running',?)", (json.dumps(running),))
                self.db.commit()
            message = "自动模拟已启动。" if running else "已暂停新开仓，已有持仓的程序风控继续运行。"
            self.broker.record_event("control", message)
            return {"status": "ok", "message": message}
        if action == "close":
            market, calendar, quote = self._market()
            position = self.broker.snapshot(quote).get("position")
            if not position:
                return {"status": "blocked", "message": "当前没有持仓。"}
            result = self.broker.execute({"action": "close", "reason": "用户手动平仓", "strategy_version": position.get("strategy_version")}, quote)
            return {"status": result.get("status"), "message": result.get("reason", "已提交模拟平仓检查。")}
        if action == "analyze":
            return self.request_analysis()
        if action == "review":
            return self.request_review()
        if action == "check":
            market, calendar, _ = self._market()
            ready = market.get("status") == "connected"
            return {"status": "ok" if ready else "blocked", "message": ("行情连接正常；" + calendar["session"] + "。") if ready else "行情正在连接或重连。", "market_status": market.get("status"), "model_key_configured": bool(get_secret("OPENAI_API_KEY"))}
        return {"status": "blocked", "message": "未知操作。"}

    def state(self) -> dict:
        market, calendar, quote = self._market()
        snapshot = self.broker.snapshot(quote or None)
        history = self.broker.history(limit=100)
        with self._lock:
            runtime = copy.deepcopy(self.runtime)
            ai = copy.deepcopy(self.ai_state)
            rows = self.db.execute("SELECT id,trading_day,created_at,payload FROM app_reviews ORDER BY id DESC LIMIT 20").fetchall()
        ai.update(usage=self.analyst.usage_today(), decisions=self.analyst.recent_decisions(limit=30), model=self.config["model"]["model"])
        if ai["last_decision"] is None and ai["decisions"]:
            ai["last_decision"] = ai["decisions"][0]
        reviews = [{"id": row["id"], "trading_day": row["trading_day"], "created_at": row["created_at"], **json.loads(row["payload"])} for row in rows]
        # Historical bars are saved in full locally; only the recent section is needed by the UI.
        market["bars_1m"] = market.get("bars_1m", [])[-240:]
        market["bars_5m"] = market.get("bars_5m", [])[-180:]
        return safe_json({
            "server_time": iso_now(), "market": market, "calendar": calendar,
            "runtime": runtime, "account": snapshot.get("account", {}), "position": snapshot.get("position"),
            "orders": history.get("orders", []), "trades": history.get("trades", []),
            "equity_curve": history.get("equity_curve", history.get("equity", [])),
            "ai": ai, "strategy": self.evolution.state(), "reviews": reviews,
            "risk_limits": self.config["account"], "cost_assumptions": self.config["simulation_policy"],
            "events": self.broker.events(limit=80),
        })
