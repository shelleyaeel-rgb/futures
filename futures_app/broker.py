"""Persistent, one-contract IF paper broker. No exchange order API is used.

All monetary arithmetic uses Decimal. Cash includes variation settlement and
paid costs; margin is collateral, not an expense. A settlement changes the
position's accounting basis, never its original entry price or strategy.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Mapping


TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
ZERO = Decimal("0")
CENT = Decimal("0.01")
IF_RE = re.compile(r"CFFEX\.IF\d{2}(?:0[1-9]|1[0-2])\Z")


def _d(value: Any, default: Decimal | None = None) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return default
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    return result if result.is_finite() else default


def _dt(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(TZ)


def _clean(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {
            str(k): _clean(v) for k, v in value.items()
            if str(k).lower() not in {"password", "api_key", "secret", "authorization", "access_token"}
        }
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return None


def _dump(value: Any) -> str:
    return json.dumps(_clean(value), ensure_ascii=False, allow_nan=False, sort_keys=True)


def _public(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


class PaperBroker:
    """Thread-safe persistent paper account. Tests may replace ``_clock``."""

    def __init__(self, db_path: Path, config: dict):
        self.config = copy.deepcopy(config)
        account = config.get("account", {})
        self.initial_balance = _d(account.get("initial_balance_cny", 1_000_000))
        self.trade_risk_limit = _d(account.get("planned_risk_per_trade_cny", 5000))
        self.daily_loss_limit = _d(account.get("daily_loss_stop_new_entries_cny", 20000))
        if self.initial_balance is None or self.initial_balance <= ZERO:
            raise ValueError("模拟初始资金必须大于零")
        if self.trade_risk_limit is None or not ZERO < self.trade_risk_limit <= Decimal("5000"):
            raise ValueError("单笔计划风险必须在零至5000元之间")
        if self.daily_loss_limit is None or not ZERO < self.daily_loss_limit <= Decimal("20000"):
            raise ValueError("日亏停开仓限额必须在零至20000元之间")
        if account.get("max_position_contracts", 1) != 1:
            raise ValueError("首版最多持有1手")
        expiry = config.get("risk_policy", {}).get("contract_expiry", {})
        self.expiry_entry_minutes = _d(expiry.get("block_new_entries_minutes_before_expiry", 1440))
        self.expiry_close_minutes = _d(expiry.get("close_position_minutes_before_expiry", 60))
        if (self.expiry_entry_minutes is None or self.expiry_close_minutes is None
                or not ZERO < self.expiry_close_minutes <= self.expiry_entry_minutes):
            raise ValueError("到期保护必须满足停止开仓提前分钟数不小于平仓提前分钟数，且均大于零")
        self.costs = self._parse_costs(config.get("simulation_policy", {}))
        self._lock = threading.RLock()
        self._clock = lambda: datetime.now(TZ)
        filename = str(db_path)
        if filename != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(filename, timeout=10, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS paper_state(key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_quotes(symbol TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, quote_time TEXT NOT NULL,
                payload TEXT NOT NULL, UNIQUE(symbol, quote_time));
            CREATE TABLE IF NOT EXISTS paper_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT UNIQUE, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_equity(
                id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_settlements(
                id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_days(day TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        with self._transaction():
            if self._state("account") is None:
                self._set_state("account", {
                    "initial_balance": self.initial_balance, "cash": self.initial_balance,
                    "total_fees": ZERO, "exchange_fees": ZERO, "broker_commissions": ZERO,
                    "closed_trade_gross_pnl": ZERO, "closed_trade_net_pnl": ZERO,
                    "settlement_pnl": ZERO, "trading_day": None,
                    "daily_start_equity": self.initial_balance, "entry_blocked": False,
                    "entry_block_reason": None, "last_curve_at": None,
                    "created_at": self._now().isoformat(),
                })
                self._event("account_created", "本地模拟账户已建立，保证金占用不作为费用扣除。",
                            {"initial_balance": self.initial_balance})

    def _now(self) -> datetime:
        value = _dt(self._clock())
        if value is None:
            raise ValueError("账户时钟必须包含时区")
        return value

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    @staticmethod
    def _parse_costs(policy: Mapping[str, Any]) -> dict | None:
        rates = policy.get("fee_rates", {})
        if not isinstance(rates, Mapping):
            return None
        parsed = {key: _d(rates.get(key)) for key in ("open", "close_today", "close_previous")}
        commission = _d(policy.get("broker_commission_cny_per_side"))
        margin = _d(policy.get("margin_rate"))
        slip = _d(policy.get("slippage_ticks"))
        version = policy.get("assumption_version")
        if (any(value is None or value < ZERO for value in parsed.values())
                or commission is None or commission < ZERO
                or margin is None or not ZERO < margin <= 1
                or slip is None or slip < ZERO or slip != slip.to_integral_value()
                or not isinstance(version, str) or not version.strip()):
            return None
        return {
            "fee_rates": {key: str(value) for key, value in parsed.items()},
            "broker_commission_cny_per_side": str(commission),
            "margin_rate": str(margin), "slippage_ticks": str(slip),
            "assumption_version": version[:100],
            "label": "保守模拟假设，非已核实交易所或期货公司实际收费",
        }

    def _state(self, key: str):
        row = self._db.execute("SELECT payload FROM paper_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _set_state(self, key: str, value: Any):
        self._db.execute("INSERT INTO paper_state(key,payload) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (key, _dump(value)))

    def _save(self, account, position):
        self._set_state("account", account)
        self._set_state("position", position)

    def _event(self, kind, message, data=None, dedupe=None):
        payload = {"time": self._now().isoformat(), "kind": str(kind)[:100],
                   "message": str(message)[:2000], "data": _clean(data)}
        self._db.execute("INSERT OR IGNORE INTO paper_events(dedupe_key,payload) VALUES(?,?)",
                         (dedupe, _dump(payload)))

    def record_event(self, kind, message, data=None):
        with self._transaction():
            self._event(kind, message, data)

    def _quote(self, raw: Mapping[str, Any]) -> tuple[dict, str | None]:
        if not isinstance(raw, Mapping):
            return {}, "未提供有效行情。"
        symbol = raw.get("symbol", raw.get("instrument_id"))
        stamp = _dt(raw.get("datetime"))
        if not isinstance(symbol, str) or not IF_RE.fullmatch(symbol):
            return {}, "必须使用IF实际月份合约，不能使用主连或其他品种成交。"
        if stamp is None:
            return {"symbol": symbol, "expire_datetime": _d(raw.get("expire_datetime")),
                    "expired": raw.get("expired") is True}, "行情时间缺失或不含时区，暂停成交。"
        q: dict[str, Any] = {"symbol": symbol, "datetime": stamp.isoformat()}
        for key in ("last_price", "bid_price1", "ask_price1", "bid_volume1", "ask_volume1",
                    "price_tick", "volume_multiple", "upper_limit", "lower_limit", "expire_datetime",
                    "settlement", "pre_settlement"):
            q[key] = _d(raw.get(key))
        q["expired"] = raw.get("expired") if type(raw.get("expired")) is bool else None
        q["session_open"] = raw.get("session_open") if type(raw.get("session_open")) is bool else None
        q["calendar_verified"] = raw.get("calendar_verified") is True
        supplied_day = str(raw.get("trading_day", "")).replace("-", "")
        date_day = stamp.date().isoformat()
        if re.fullmatch(r"\d{8}", supplied_day):
            try:
                day = datetime.strptime(supplied_day, "%Y%m%d").date().isoformat()
            except ValueError:
                day = date_day
        else:
            day = date_day
        # IF has no night session. A contradictory trading date is unsafe.
        if day != date_day:
            return q, "IF交易日与行情北京时间日期不一致，暂停处理。"
        q["trading_day"] = day
        q["trading_day_source"] = raw.get("trading_day_source", "provider" if supplied_day else "local_quote_date")
        age = (self._now() - stamp).total_seconds()
        if age > 10:
            return q, "行情超过10秒未更新；仅保留估值，不按旧行情成交。"
        if age < -2:
            return q, "行情时间超前本机时钟超过2秒，暂停成交。"
        if q["last_price"] is None or q["last_price"] <= ZERO:
            return q, "最新价无效，暂停处理。"
        if q["price_tick"] != Decimal("0.2") or q["volume_multiple"] != Decimal("300"):
            return q, "IF合约乘数或最小变动价位无效，暂停成交。"
        return q, None

    def _session_error(self, q):
        if q.get("session_open") is False:
            return "当前已休市，等待开市后的新鲜可成交盘口。"
        now = self._now()
        current_time = now.time().replace(tzinfo=None)
        in_hours = now.weekday() < 5 and (
            time(9, 30) <= current_time < time(11, 30) or time(13) <= current_time < time(15))
        if not in_hours:
            return "当前不在IF工作日日盘时段，等待开市；此校验不替代交易所日历。"
        return None

    def _not_expired(self, q):
        return (q.get("expired") is False and q.get("expire_datetime") is not None
                and _d(q["expire_datetime"]) > Decimal(str(self._now().timestamp())))

    def _expiry_guard(self, account, position, q):
        """Lifecycle metadata may stop trading even when the price is stale."""
        if not position:
            return None
        saved_expiry = _d(position.get("expire_datetime"))
        matching = q.get("symbol") == position["symbol"]
        reported_expiry = _d(q.get("expire_datetime")) if matching else None
        now = Decimal(str(self._now().timestamp()))
        known_expiries = [value for value in (saved_expiry, reported_expiry) if value is not None and value > ZERO]
        expired = (position.get("pending_exit") == "expired_requires_settlement"
                   or (matching and q.get("expired") is True)
                   or any(value <= now for value in known_expiries))
        if expired:
            reason = (f"持仓合约{position['symbol']}已到期，停止按盘口成交；"
                      "需人工核实官方最终结算价后处理，禁止自动换月或估算交割结算。")
            position["pending_exit"] = "expired_requires_settlement"
            position["expiry_warning"] = reason
            account["entry_blocked"] = True
            account["entry_block_reason"] = reason
            self._event("expired_requires_settlement", reason,
                        {"symbol": position["symbol"], "expire_datetime": saved_expiry or reported_expiry},
                        f"expired:{position['symbol']}:{position['opened_at']}")
            self._save(account, position)
            return reason
        changed = False
        if reported_expiry is not None and reported_expiry > ZERO and reported_expiry != saved_expiry:
            position["expire_datetime"] = reported_expiry
            saved_expiry = reported_expiry
            changed = True
        if (saved_expiry is not None and saved_expiry - now <= self.expiry_close_minutes * 60
                and not position.get("pending_exit")):
            position["pending_exit"] = "contract_expiry"
            changed = True
            self._event("contract_expiry_exit_pending", "已进入合约到期前平仓窗口，等待持仓合约的有效可成交盘口。",
                        {"symbol": position["symbol"], "expire_datetime": saved_expiry,
                         "close_minutes_before_expiry": self.expiry_close_minutes},
                        f"expiry_pending:{position['symbol']}:{position['opened_at']}")
        if changed:
            self._save(account, position)
        return None

    @staticmethod
    def _position_costs(position):
        return position["cost_assumptions"]

    def _account_view(self, account, position):
        cash = _d(account["cash"])
        initial = _d(account["initial_balance"])
        unrealized, margin = ZERO, ZERO
        if position:
            direction = 1 if position["side"] == "long" else -1
            mark, basis = _d(position["mark_price"]), _d(position["settlement_basis"])
            multiplier = _d(position["volume_multiple"])
            unrealized = (mark - basis) * multiplier * direction
            margin = mark * multiplier * _d(position["cost_assumptions"]["margin_rate"])
        equity = cash + unrealized
        return {
            "initial_balance": initial, "cash": cash, "equity": equity,
            "available": equity - margin, "margin": margin, "unrealized_pnl": unrealized,
            "realized_pnl": cash - initial, "total_fees": _d(account["total_fees"]),
            "exchange_fees": _d(account["exchange_fees"]),
            "broker_commissions": _d(account["broker_commissions"]),
            "closed_trade_gross_pnl": _d(account["closed_trade_gross_pnl"]),
            "closed_trade_net_pnl": _d(account["closed_trade_net_pnl"]),
            "settlement_pnl": _d(account["settlement_pnl"]),
            "daily_pnl": equity - _d(account["daily_start_equity"]),
            "daily_start_equity": _d(account["daily_start_equity"]),
            "daily_loss_limit": self.daily_loss_limit,
            "planned_risk_per_trade": self.trade_risk_limit,
            "entry_blocked": bool(account["entry_blocked"]),
            "entry_block_reason": account.get("entry_block_reason"),
            "trading_day": account.get("trading_day"),
        }

    def _snapshot(self, account=None, position=None, load=True):
        if load:
            account, position = self._state("account"), self._state("position")
        public_position = None
        if position:
            public_position = copy.deepcopy(position)
            for key in ("entry_price", "settlement_basis", "mark_price", "stop_loss", "take_profit",
                        "volume_multiple", "price_tick", "open_fees", "planned_risk", "initial_stop_loss",
                        "initial_risk_distance", "best_price", "prior_stop_for_tick", "expire_datetime"):
                public_position[key] = _d(public_position.get(key))
            direction = 1 if position["side"] == "long" else -1
            public_position["gross_pnl_since_entry"] = (
                _d(position["mark_price"]) - _d(position["entry_price"])) * _d(position["volume_multiple"]) * direction
        return _public({
            "account": self._account_view(account, position), "position": public_position,
            "simulation": True, "cost_assumptions": self.costs,
            "cost_assumptions_ready": self.costs is not None,
            "valuation_note": "账户已实现盈亏含结算及费用；持仓浮盈亏从最近结算基价计算。",
        })

    def snapshot(self, quote=None):
        with self._lock:
            result = self._snapshot()
            if quote is not None:
                _, error = self._quote(quote)
                result["quote_validation_reason"] = error
            return result

    def _curve(self, account, position, force=False):
        now = self._now()
        last = _dt(account.get("last_curve_at"))
        if not force and last is not None and (now - last).total_seconds() < 30:
            return
        view = self._account_view(account, position)
        row = {"time": now.isoformat(), "trading_day": account.get("trading_day"),
               **{key: view[key] for key in ("equity", "cash", "margin", "unrealized_pnl", "daily_pnl")}}
        self._db.execute("INSERT INTO paper_equity(payload) VALUES(?)", (_dump(row),))
        account["last_curve_at"] = now.isoformat()

    def _daily_risk(self, account, position):
        pnl = self._account_view(account, position)["daily_pnl"]
        if pnl <= -self.daily_loss_limit and not account["entry_blocked"]:
            account["entry_blocked"] = True
            account["entry_block_reason"] = "当日含浮亏和费用的亏损已达到限额，当前交易日停止新开仓。"
            self._event("daily_loss_limit", account["entry_block_reason"],
                        {"daily_pnl": pnl, "limit": self.daily_loss_limit, "trading_day": account["trading_day"]})

    def _roll_day(self, account, position, q):
        incoming = q["trading_day"]
        previous = account.get("trading_day")
        if previous == incoming:
            return
        if previous is None:
            account["trading_day"] = incoming
            account["daily_start_equity"] = self._account_view(account, position)["equity"]
            return
        if position:
            price = _d(position["mark_price"])
            source = "last_observed_price_assumption"
            label = "上一已观察交易日最后行情估值的模拟结算，非官方结算价"
            if q["symbol"] == position["symbol"] and q.get("pre_settlement") is not None and q["pre_settlement"] > ZERO:
                price = q["pre_settlement"]
                source = "provider_pre_settlement"
                label = "使用行情服务返回的前结算价，跨越未运行日期时仅记录累计结算变化"
            direction = 1 if position["side"] == "long" else -1
            variation = (price - _d(position["settlement_basis"])) * _d(position["volume_multiple"]) * direction
            account["cash"] = _d(account["cash"]) + variation
            account["settlement_pnl"] = _d(account["settlement_pnl"]) + variation
            position["settlement_basis"] = price
            position["mark_price"] = price
            settlement = {"time": self._now().isoformat(), "from_trading_day": previous,
                          "to_trading_day": incoming, "symbol": position["symbol"],
                          "price": price, "variation_pnl": variation, "source": source, "label": label}
            self._db.execute("INSERT INTO paper_settlements(payload) VALUES(?)", (_dump(settlement),))
            self._event("daily_settlement", label, settlement)
        closing = self._account_view(account, position)
        self._db.execute("INSERT OR REPLACE INTO paper_days(day,payload) VALUES(?,?)",
                         (previous, _dump({"trading_day": previous, "closing_equity": closing["equity"],
                                           "daily_pnl": closing["daily_pnl"], "entry_blocked": account["entry_blocked"]})))
        account["trading_day"] = incoming
        account["daily_start_equity"] = closing["equity"]
        account["entry_blocked"] = False
        account["entry_block_reason"] = None
        self._event("trading_day_started", "已建立新交易日基准；开盘跳空将计入当日盈亏。",
                    {"trading_day": incoming, "daily_start_equity": closing["equity"],
                     "trading_day_source": q["trading_day_source"]})

    def _tick_used(self, q):
        return self._db.execute("SELECT 1 FROM paper_trades WHERE symbol=? AND quote_time=?",
                                (q.get("symbol", ""), q.get("datetime", ""))).fetchone() is not None

    def _fill_price(self, q, buy, costs, limit_price=None):
        session_error = self._session_error(q)
        if session_error:
            return None, session_error
        side_price = q.get("ask_price1" if buy else "bid_price1")
        side_size = q.get("ask_volume1" if buy else "bid_volume1")
        if side_price is None or side_price <= ZERO or side_size is None or side_size < 1:
            return None, "可成交对手盘口缺失或不足1手，等待后续有效盘口，不虚构成交。"
        bid, ask = q.get("bid_price1"), q.get("ask_price1")
        if bid is not None and ask is not None and bid > ZERO and ask > ZERO and bid > ask:
            return None, "买卖盘口交叉异常，暂停成交。"
        upper, lower = q.get("upper_limit"), q.get("lower_limit")
        if upper is None or lower is None or not ZERO < lower <= upper:
            return None, "涨跌停价格边界缺失或无效，暂停成交。"
        tick = q["price_tick"]
        slip = _d(costs["slippage_ticks"]) * tick
        price = side_price + slip if buy else side_price - slip
        rounding = ROUND_CEILING if buy else ROUND_FLOOR
        price = (price / tick).to_integral_value(rounding=rounding) * tick
        if not lower <= price <= upper:
            return None, "计入保守滑点后超出涨跌停边界，等待可成交报价。"
        if limit_price is not None:
            limit = _d(limit_price)
            if limit is None or limit <= ZERO:
                return None, "委托限价无效。"
            if (buy and price > limit) or (not buy and price < limit):
                return None, "可成交价格不满足委托限价，当前未成交。"
        return price, None

    @staticmethod
    def _fees(price, multiplier, fee_kind, costs):
        rate = _d(costs["fee_rates"][fee_kind])
        exchange = (price * multiplier * rate).quantize(CENT, rounding=ROUND_CEILING)
        commission = _d(costs["broker_commission_cny_per_side"]).quantize(CENT, rounding=ROUND_CEILING)
        return {"fee_kind": fee_kind, "exchange_fee_assumption": exchange,
                "broker_commission_assumption": commission, "total": exchange + commission,
                "assumption_version": costs["assumption_version"], "label": costs["label"]}

    @staticmethod
    def _charge(account, fees):
        account["cash"] = _d(account["cash"]) - fees["total"]
        account["total_fees"] = _d(account["total_fees"]) + fees["total"]
        account["exchange_fees"] = _d(account["exchange_fees"]) + fees["exchange_fee_assumption"]
        account["broker_commissions"] = _d(account["broker_commissions"]) + fees["broker_commission_assumption"]

    def _order(self, decision, q, status, reason, origin, code=None, fill_price=None):
        safe_decision = {key: decision.get(key) for key in (
            "action", "stop_loss", "take_profit", "reason", "strategy_version", "limit_price") if key in decision}
        key_data = {"symbol": q.get("symbol"), "quote_time": q.get("datetime"), "status": status,
                    "action": decision.get("action"), "origin": origin, "code": code or reason,
                    "stop_loss": decision.get("stop_loss"), "take_profit": decision.get("take_profit")}
        dedupe = hashlib.sha256(_dump(key_data).encode()).hexdigest()
        payload = {"time": self._now().isoformat(), "symbol": q.get("symbol"),
                   "quote_time": q.get("datetime"), "trading_day": q.get("trading_day"),
                   "action": decision.get("action"), "status": status, "reason": reason,
                   "origin": origin, "decision": safe_decision, "quantity": 1,
                   "limit_price": decision.get("limit_price"), "fill_price": fill_price}
        self._db.execute("INSERT OR IGNORE INTO paper_orders(dedupe_key,payload) VALUES(?,?)", (dedupe, _dump(payload)))
        order_id = self._db.execute("SELECT id FROM paper_orders WHERE dedupe_key=?", (dedupe,)).fetchone()[0]
        if status == "blocked":
            self._event("order_blocked", reason, {"order_id": order_id, "symbol": q.get("symbol"), "origin": origin}, dedupe)
        return order_id

    def _result(self, status, reason, account, position, **extra):
        return {"status": status, "reason": reason, "executed": status == "executed",
                **extra, **self._snapshot(account, position, load=False)}

    def _blocked(self, decision, q, reason, account, position, origin="ai", code=None):
        order_id = self._order(decision, q, "blocked", reason, origin, code)
        return self._result("blocked", reason, account, position, order_id=order_id)

    def _fill_close(self, account, position, q, decision, origin):
        expiry_error = self._expiry_guard(account, position, q)
        if expiry_error:
            return position, self._blocked(decision, q, expiry_error, account, position, origin)
        if q["symbol"] != position["symbol"]:
            return position, self._blocked(decision, q, "报价不是当前持仓合约；主力换月不会隐式移仓。", account, position, origin)
        if self._tick_used(q):
            return position, self._blocked(decision, q, "这一合约的同一行情时点已成交，等待下一条行情，避免重复交易。", account, position, origin)
        costs = self._position_costs(position)
        buy = position["side"] == "short"
        price, error = self._fill_price(q, buy, costs, decision.get("limit_price"))
        if error:
            return position, self._blocked(decision, q, error, account, position, origin)
        multiplier = _d(position["volume_multiple"])
        direction = -1 if buy else 1
        fee_kind = "close_today" if q["trading_day"] == position["opened_trading_day"] else "close_previous"
        fees = self._fees(price, multiplier, fee_kind, costs)
        variation = (price - _d(position["settlement_basis"])) * multiplier * direction
        gross = (price - _d(position["entry_price"])) * multiplier * direction
        net = gross - _d(position["open_fees"]) - fees["total"]
        account["cash"] = _d(account["cash"]) + variation
        self._charge(account, fees)
        account["closed_trade_gross_pnl"] = _d(account["closed_trade_gross_pnl"]) + gross
        account["closed_trade_net_pnl"] = _d(account["closed_trade_net_pnl"]) + net
        reason = str(decision.get("reason") or "平仓")[:2000]
        order_id = self._order(decision, q, "filled", reason, origin, fill_price=price)
        trade = {"time": self._now().isoformat(), "quote_time": q["datetime"], "symbol": q["symbol"],
                 "trading_day": q["trading_day"], "action": "close", "side": position["side"],
                 "quantity": 1, "price": price, "entry_price": _d(position["entry_price"]),
                 "gross_pnl": gross, "net_pnl": net, "cash_variation_pnl": variation,
                 "net_pnl_scope": "closed_round_trip", "cash_delta": variation - fees["total"],
                 "open_fees": _d(position["open_fees"]), "fees": fees,
                 "strategy_version": position["strategy_version"], "entry_reason": position["entry_reason"],
                 "reason": reason, "origin": origin, "order_id": order_id,
                 "opened_at": position["opened_at"], "assumption_version": costs["assumption_version"],
                 "cost_assumptions": costs}
        cursor = self._db.execute("INSERT INTO paper_trades(symbol,quote_time,payload) VALUES(?,?,?)",
                                  (q["symbol"], q["datetime"], _dump(trade)))
        trade["id"] = cursor.lastrowid
        position = None
        self._daily_risk(account, position)
        self._curve(account, position, force=True)
        self._save(account, position)
        self._event("position_closed", reason, {"trade_id": trade["id"], "net_pnl": net, "origin": origin})
        return None, self._result("executed", reason, account, None, order_id=order_id, trade=_public(trade))

    def _observe(self, raw):
        account, position = self._state("account"), self._state("position")
        q, error = self._quote(raw)
        expiry_error = self._expiry_guard(account, position, q)
        if expiry_error:
            # Never run daily settlement or mark a stranded expired holding from
            # a stale book, a later main contract, or an unofficial final price.
            return account, position, q, None, expiry_error
        if error:
            self._event("quote_rejected", error, {"symbol": q.get("symbol"), "quote_time": q.get("datetime")},
                        "quote:" + hashlib.sha256(_dump({"q": q, "error": error}).encode()).hexdigest())
            return account, position, q, None, error
        if account.get("trading_day") and q["trading_day"] < account["trading_day"]:
            return account, position, q, None, "行情交易日早于账本交易日，拒绝回退账本。"
        previous = self._db.execute("SELECT payload FROM paper_quotes WHERE symbol=?", (q["symbol"],)).fetchone()
        if previous and _dt(q["datetime"]) < _dt(json.loads(previous[0])["datetime"]):
            return account, position, q, None, "行情时间发生回退，暂停使用该报价。"
        self._roll_day(account, position, q)
        self._db.execute("INSERT INTO paper_quotes(symbol,payload) VALUES(?,?) "
                         "ON CONFLICT(symbol) DO UPDATE SET payload=excluded.payload", (q["symbol"], _dump(q)))
        if position and q["symbol"] == position["symbol"]:
            position["mark_price"] = q["last_price"]
            position["mark_datetime"] = q["datetime"]
        self._daily_risk(account, position)
        risk_action = None
        if position and q["symbol"] == position["symbol"]:
            trigger = position.get("pending_exit")
            last, stop, target = q["last_price"], _d(position["stop_loss"]), _d(position.get("take_profit"))
            if position.get("stop_effective_after") == q["datetime"]:
                stop = _d(position.get("prior_stop_for_tick"), stop)
            side = position["side"]
            if trigger not in {"stop_loss", "margin_shortfall"}:
                if (side == "long" and last <= stop) or (side == "short" and last >= stop):
                    trigger = "stop_loss"
                elif self._account_view(account, position)["available"] < ZERO:
                    trigger = "margin_shortfall"
            if not trigger and target is not None and ((side == "long" and last >= target) or (side == "short" and last <= target)):
                trigger = "take_profit"
            if trigger:
                position["pending_exit"] = trigger
                messages = {"stop_loss": "程序止损已触发，按首个可成交盘口平仓；实际亏损可能超过计划值。",
                            "take_profit": "程序止盈已触发，按首个可成交盘口平仓。",
                            "margin_shortfall": "保证金余量不足，程序尝试按首个可成交盘口平仓。",
                            "contract_expiry": "合约临近到期，程序按持仓实际合约的首个可成交盘口平仓。"}
                reason = messages.get(trigger, "程序保护性平仓。")
                decision = {"action": "close", "reason": reason, "strategy_version": position["strategy_version"]}
                position, risk_action = self._fill_close(account, position, q, decision, "program_risk")
            if position and not position.get("pending_exit"):
                self._trail(position, q)
        self._curve(account, position)
        self._save(account, position)
        return account, position, q, risk_action, None

    def _trail(self, position, q):
        """Check old protection first; a newly tightened stop applies to later quotes."""
        entry, stop, tick = (_d(position[key]) for key in ("entry_price", "stop_loss", "price_tick"))
        if position.get("initial_stop_loss") is None:
            position["initial_stop_loss"] = stop
        if position.get("initial_risk_distance") is None:
            position["initial_risk_distance"] = max(abs(entry - _d(position["initial_stop_loss"])), tick)
            position["protection_migration"] = "derived_from_saved_entry_and_stop"
        distance = _d(position["initial_risk_distance"])
        previous_best = _d(position.get("best_price"), entry)
        long = position["side"] == "long"
        best = max(previous_best, q["last_price"]) if long else min(previous_best, q["last_price"])
        position["best_price"] = best
        favorable = best - entry if long else entry - best
        if favorable < distance:
            return
        candidate = best - distance if long else best + distance
        candidate = (candidate / tick).to_integral_value(rounding=ROUND_FLOOR if long else ROUND_CEILING) * tick
        improves = candidate > stop if long else candidate < stop
        if improves:
            if position.get("stop_effective_after") != q["datetime"]:
                position["prior_stop_for_tick"] = stop
            position["stop_effective_after"] = q["datetime"]
            position["stop_loss"] = candidate
            self._event("trailing_stop_tightened", "盈利运行达到1R后，程序已收紧止损；持仓策略版本保持不变。",
                        {"symbol": position["symbol"], "previous_stop": stop, "stop_loss": candidate,
                         "best_price": best, "initial_risk_distance": distance,
                         "strategy_version": position["strategy_version"]})

    def on_quote(self, quote: dict) -> dict:
        with self._transaction():
            account, position, q, risk_action, error = self._observe(quote)
            result = self._snapshot(account, position, load=False)
            result.update({"quote_accepted": error is None, "reason": error,
                           "quote_symbol": q.get("symbol"), "risk_action": risk_action})
            return result

    def _protection(self, decision, q, account, position):
        if not position:
            return self._result("held", "当前空仓，继续等待。", account, position)
        if q["symbol"] != position["symbol"]:
            return self._blocked(decision, q, "需要当前持仓合约行情才能调整保护价。", account, position)
        supplied_version = decision.get("strategy_version")
        if supplied_version and supplied_version != position["strategy_version"]:
            return self._blocked(decision, q, "已有持仓继续使用开仓策略版本，不能由新版本追溯改写。", account, position)
        if position.get("pending_exit"):
            return self._result("held", "保护性平仓已触发，继续等待可成交盘口。", account, position)
        changed = False
        proposed = copy.deepcopy(position)
        new_stop = _d(decision.get("stop_loss"))
        old_stop = _d(position["stop_loss"])
        tick = _d(position["price_tick"])
        if decision.get("stop_loss") is not None:
            if new_stop is None or new_stop <= ZERO or new_stop % tick != 0:
                return self._blocked(decision, q, "新止损价无效或不符合最小变动价位。", account, position)
            loosens = (position["side"] == "long" and new_stop < old_stop) or (position["side"] == "short" and new_stop > old_stop)
            if loosens:
                return self._blocked(decision, q, "已有止损只允许收紧，不能扩大风险。", account, position)
            wrong_side = (position["side"] == "long" and new_stop >= q["last_price"]) or (position["side"] == "short" and new_stop <= q["last_price"])
            if wrong_side:
                return self._blocked(decision, q, "新止损价必须位于当前价格的风险一侧；立即退出应明确平仓。", account, position)
            changed = new_stop != old_stop
            proposed["stop_loss"] = new_stop
        # Null means leave an existing target unchanged, not silently remove it.
        if decision.get("take_profit") is not None:
            target = _d(decision["take_profit"])
            if target is None or target <= ZERO or target % tick != 0 or (
                position["side"] == "long" and target <= q["last_price"]
            ) or (position["side"] == "short" and target >= q["last_price"]):
                return self._blocked(decision, q, "新止盈价必须有效并位于当前价格的盈利方向。", account, position)
            changed = changed or target != _d(position.get("take_profit"))
            proposed["take_profit"] = target
        position = proposed
        self._save(account, position)
        if changed:
            self._event("protection_updated", "已更新当前持仓保护价，开仓策略版本保持不变。",
                        {"symbol": position["symbol"], "stop_loss": position["stop_loss"],
                         "take_profit": position.get("take_profit"), "strategy_version": position["strategy_version"]})
        return self._result("protection_updated" if changed else "held", "保护价已更新。" if changed else "保持当前持仓。", account, position)

    def execute(self, decision: dict, quote: dict, indicators: dict | None = None) -> dict:
        del indicators  # Indicators never override ledger or program risk limits.
        with self._transaction():
            account, position, q, risk_action, error = self._observe(quote)
            if not isinstance(decision, Mapping):
                decision = {"action": "invalid"}
            if error:
                return self._blocked(decision, q, error, account, position)
            if risk_action and risk_action.get("executed"):
                return risk_action
            action = decision.get("action", "hold")
            if action not in {"hold", "open_long", "open_short", "close"}:
                return self._blocked(decision, q, "不支持的交易动作。", account, position)
            if action == "hold":
                return self._protection(decision, q, account, position)
            if action == "close":
                if not position:
                    return self._result("held", "当前空仓，无需重复平仓。", account, None)
                if decision.get("strategy_version") and decision["strategy_version"] != position["strategy_version"]:
                    return self._blocked(decision, q, "平仓决策必须使用当前持仓的策略版本。", account, position)
                _, result = self._fill_close(account, position, q, decision, "ai")
                return result
            if position:
                return self._blocked(decision, q, "已有1手持仓；不加仓、不同时持有多空，也不隐式换月。", account, position)
            if account["entry_blocked"]:
                return self._blocked(decision, q, account["entry_block_reason"], account, position)
            if self.costs is None:
                return self._blocked(decision, q, "保守成本假设尚未完整配置，暂停新开仓，不以零费用代替。", account, position)
            if self._tick_used(q):
                return self._blocked(decision, q, "这一合约的同一行情时点已成交，等待下一条行情，避免重复交易。", account, position)
            if not self._not_expired(q):
                return self._blocked(decision, q, "合约已过期或到期信息不完整，拒绝新开仓。", account, position)
            if q["expire_datetime"] - Decimal(str(self._now().timestamp())) <= self.expiry_entry_minutes * 60:
                return self._blocked(decision, q,
                                     f"距离合约到期不超过{self.expiry_entry_minutes}分钟，停止新开仓。",
                                     account, position)
            version = decision.get("strategy_version")
            if not isinstance(version, str) or not version.strip():
                return self._blocked(decision, q, "开仓必须提供可追溯的策略版本。", account, position)
            buy = action == "open_long"
            price, fill_error = self._fill_price(q, buy, self.costs, decision.get("limit_price"))
            if fill_error:
                return self._blocked(decision, q, fill_error, account, position)
            stop = _d(decision.get("stop_loss"))
            target = _d(decision.get("take_profit"))
            tick, multiplier = q["price_tick"], q["volume_multiple"]
            if stop is None or stop <= ZERO or stop % tick != 0:
                return self._blocked(decision, q, "开仓必须提供有效且符合最小变动价位的止损价。", account, position)
            if (buy and stop >= min(price, q["last_price"])) or (not buy and stop <= max(price, q["last_price"])):
                return self._blocked(decision, q, "止损价必须位于开仓价及最新价的风险一侧。", account, position)
            if decision.get("take_profit") is not None and (
                target is None or target <= ZERO or target % tick != 0
                or (buy and target <= price) or (not buy and target >= price)
            ):
                return self._blocked(decision, q, "止盈价无效或不在开仓价的盈利方向。", account, position)
            bid, ask = q.get("bid_price1"), q.get("ask_price1")
            if bid is None or ask is None or not ZERO < bid <= ask:
                return self._blocked(decision, q, "计划风险估计需要完整有效的买卖盘口。", account, position)
            spread = ask - bid
            exit_allowance = spread + _d(self.costs["slippage_ticks"]) * tick
            stop_fill = stop - exit_allowance if buy else stop + exit_allowance
            if stop_fill <= ZERO:
                return self._blocked(decision, q, "保守止损成交估值无效。", account, position)
            fees = self._fees(price, multiplier, "open", self.costs)
            closing_kind = max(("close_today", "close_previous"), key=lambda key: _d(self.costs["fee_rates"][key]))
            expected_close_fees = self._fees(stop_fill, multiplier, closing_kind, self.costs)
            risk = (abs(price - stop_fill) * multiplier + fees["total"] + expected_close_fees["total"]).quantize(CENT, rounding=ROUND_CEILING)
            if risk > self.trade_risk_limit:
                return self._blocked(decision, q, f"单笔计划风险含进出费用及止损滑点为{risk}元，超过{self.trade_risk_limit}元，拒绝开仓。", account, position)
            direction = 1 if buy else -1
            equity_after = _d(account["cash"]) - fees["total"] + (q["last_price"] - price) * multiplier * direction
            margin = q["last_price"] * multiplier * _d(self.costs["margin_rate"])
            if equity_after < margin:
                return self._blocked(decision, q, "模拟资金不足以覆盖保证金、成交价差和开仓费用。", account, position)
            if equity_after - _d(account["daily_start_equity"]) <= -self.daily_loss_limit:
                return self._blocked(decision, q, "开仓后的价差和费用将触及当日亏损限额，拒绝新开仓。", account, position)
            self._charge(account, fees)
            reason = str(decision.get("reason") or "AI开仓")[:2000]
            position = {
                "symbol": q["symbol"], "side": "long" if buy else "short", "quantity": 1,
                "entry_price": price, "settlement_basis": price, "mark_price": q["last_price"],
                "mark_datetime": q["datetime"], "stop_loss": stop, "take_profit": target,
                "initial_stop_loss": stop, "initial_risk_distance": abs(price - stop), "best_price": price,
                "strategy_version": version[:100], "entry_reason": reason,
                "opened_at": q["datetime"], "opened_trading_day": q["trading_day"],
                "expire_datetime": q["expire_datetime"],
                "volume_multiple": multiplier, "price_tick": tick,
                "open_fees": fees["total"], "planned_risk": risk,
                "cost_assumptions": copy.deepcopy(self.costs), "pending_exit": None,
            }
            order_id = self._order(decision, q, "filled", reason, "ai", fill_price=price)
            trade = {"time": self._now().isoformat(), "quote_time": q["datetime"], "symbol": q["symbol"],
                     "trading_day": q["trading_day"], "action": action, "side": position["side"],
                     "quantity": 1, "price": price, "gross_pnl": ZERO, "net_pnl": None,
                     "net_pnl_scope": "open_position_not_closed", "cash_delta": -fees["total"],
                     "fees": fees, "strategy_version": position["strategy_version"],
                     "reason": reason, "origin": "ai", "order_id": order_id,
                     "assumption_version": self.costs["assumption_version"],
                     "cost_assumptions": copy.deepcopy(self.costs), "planned_risk": risk}
            cursor = self._db.execute("INSERT INTO paper_trades(symbol,quote_time,payload) VALUES(?,?,?)",
                                      (q["symbol"], q["datetime"], _dump(trade)))
            trade["id"] = cursor.lastrowid
            self._daily_risk(account, position)
            self._curve(account, position, force=True)
            self._save(account, position)
            self._event("position_opened", reason, {"trade_id": trade["id"], "planned_risk": risk,
                                                    "strategy_version": position["strategy_version"]})
            return self._result("executed", reason, account, position, order_id=order_id, trade=_public(trade))

    def _rows(self, table, limit):
        rows = self._db.execute(f"SELECT id,payload FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": row["id"], **json.loads(row["payload"])} for row in rows]

    def history(self, limit=100) -> dict:
        limit = max(1, min(int(limit), 2000))
        with self._lock:
            return {"orders": self._rows("paper_orders", limit), "trades": self._rows("paper_trades", limit),
                    "equity_curve": list(reversed(self._rows("paper_equity", limit))),
                    "settlements": self._rows("paper_settlements", limit),
                    "days": [json.loads(row[0]) for row in self._db.execute(
                        "SELECT payload FROM paper_days ORDER BY day DESC LIMIT ?", (limit,)).fetchall()]}

    def events(self, limit=50) -> list:
        with self._lock:
            return self._rows("paper_events", max(1, min(int(limit), 2000)))
