"""Isolated TqSdk market reader. All simulated orders belong to our local broker."""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from .common import CHINA, ROOT, iso_now, parse_time, safe_json, write_json

CONTRACT = re.compile(r"CFFEX\.IF\d{2}(?:0[1-9]|1[0-2])\Z")
FIELDS = (
    "last_price", "bid_price1", "ask_price1", "bid_volume1", "ask_volume1",
    "volume", "open_interest", "price_tick", "volume_multiple", "upper_limit",
    "lower_limit", "pre_close", "pre_settlement", "settlement", "highest", "lowest",
    "open", "expire_datetime", "commission", "margin",
)


def quote_snapshot(quote, symbol: str) -> dict:
    stamp = parse_time(getattr(quote, "datetime", ""))
    result = {
        "symbol": symbol,
        "instrument_id": symbol,
        "datetime": stamp.isoformat() if stamp else None,
        "exchange_id": "CFFEX",
        "expired": bool(getattr(quote, "expired", True)),
        "trading_day": stamp.date().isoformat() if stamp else None,
        "trading_day_source": "IF_day_session_quote_date",
    }
    for name in FIELDS:
        value = getattr(quote, name, None)
        try:
            result[name] = float(value) if value is not None and math.isfinite(float(value)) else None
        except (ValueError, TypeError, OverflowError):
            result[name] = None
    return result


def bars_snapshot(frame, duration: int, current: dt.datetime | None = None) -> list:
    current = current or dt.datetime.now(CHINA)
    result = []
    for row in frame.to_dict("records"):
        try:
            stamp_number = float(row.get("datetime", 0))
            if not math.isfinite(stamp_number) or stamp_number <= 0 or row.get("id", -1) < 0:
                continue
            stamp = dt.datetime.fromtimestamp(stamp_number / 1e9, CHINA)
            values = {name: float(row[name]) for name in ("open", "high", "low", "close", "volume")}
            if not all(math.isfinite(value) for value in values.values()) or min(values[name] for name in ("open", "high", "low", "close")) <= 0:
                continue
            result.append({
                "datetime": stamp.isoformat(), **values,
                "completed": stamp + dt.timedelta(seconds=duration) <= current,
                "duration_seconds": duration,
            })
        except (ValueError, TypeError, OverflowError, KeyError, OSError):
            continue
    return result


def market_worker(holding_symbol: str | None = None) -> int:
    """Write only safe JSON to stdout; authentication/logging remains isolated."""
    original_stdout = sys.stdout

    def emit(data: dict) -> None:
        original_stdout.write(json.dumps(safe_json(data), ensure_ascii=False, allow_nan=False) + "\n")
        original_stdout.flush()

    phase = "credential"
    api = None
    try:
        from futures_probe.secrets import get_secret
        username, password = get_secret("TQ_USERNAME"), get_secret("TQ_PASSWORD")
        if not username or not password:
            emit({"type": "error", "code": "MISSING_MARKET_CREDENTIALS", "message": "请先配置快期行情账号。"})
            return 2
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                from tqsdk import TqApi, TqAuth, TqSim
                phase = "connect"
                emit({"type": "status", "status": "connecting", "message": "正在连接行情服务"})
                api = TqApi(TqSim(init_balance=1_000_000), auth=TqAuth(username, password), disable_print=True, debug=False)
                main = api.get_quote("KQ.m@CFFEX.IF")
                deadline = time.time() + 20
                while not CONTRACT.fullmatch(str(getattr(main, "underlying_symbol", ""))):
                    if not api.wait_update(deadline=deadline):
                        emit({"type": "error", "code": "MAIN_CONTRACT_UNRESOLVED", "message": "暂未取得 IF 活跃月份合约。"})
                        return 2
                phase = "quotes"
                active_symbol = str(main.underlying_symbol)
                focus = holding_symbol if holding_symbol and CONTRACT.fullmatch(holding_symbol) else active_symbol
                quote = api.get_quote(focus)
                active_quote = api.get_quote(active_symbol)
                length = min(10000, max(300, int(config.get("market", {}).get("history_bars", 2400))))
                bars1 = api.get_kline_serial(focus, 60, data_length=length)
                bars5 = api.get_kline_serial(focus, 300, data_length=max(600, length // 5))
                phase = "stream"
                last_emit = 0.0
                while True:
                    api.wait_update(deadline=time.time() + 1)
                    mapped = str(getattr(main, "underlying_symbol", ""))
                    if CONTRACT.fullmatch(mapped) and mapped != active_symbol:
                        active_symbol = mapped
                        active_quote = api.get_quote(active_symbol)
                        if not holding_symbol:
                            focus = active_symbol
                            quote = active_quote
                            bars1 = api.get_kline_serial(focus, 60, data_length=length)
                            bars5 = api.get_kline_serial(focus, 300, data_length=max(600, length // 5))
                    if time.monotonic() - last_emit < 1:
                        continue
                    last_emit = time.monotonic()
                    snap = quote_snapshot(quote, focus)
                    emit({
                        "type": "market",
                        "status": "connected" if snap.get("datetime") else "loading",
                        "symbol": focus,
                        "main_symbol": active_symbol,
                        "quote": snap,
                        "active_quote": quote_snapshot(active_quote, active_symbol),
                        "bars_1m": bars_snapshot(bars1, 60),
                        "bars_5m": bars_snapshot(bars5, 300),
                        "last_received_at": iso_now(),
                        "error": None,
                    })
    except Exception as exc:
        # Never render exception text: request URLs can include authentication data.
        emit({"type": "error", "code": f"MARKET_{phase.upper()}_{type(exc).__name__}", "message": "行情连接暂时不可用，程序会自动重试。"})
        return 2
    finally:
        if api is not None:
            with open(os.devnull, "w", encoding="utf-8") as sink:
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    try:
                        api.close()
                    except Exception:
                        pass


class MarketFeed:
    def __init__(self, cache_path: Path | None = None):
        self.cache_path = cache_path or ROOT / "data" / "market-cache.json"
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._process = None
        self._holding_symbol = None
        self._last_packet = 0.0
        self._last_cache = 0.0
        self._state = {"status": "disconnected", "quote": None, "bars_1m": [], "bars_5m": [], "error": None}
        try:
            cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if CONTRACT.fullmatch(str(cached.get("symbol", ""))):
                self._state.update(cached)
                self._state["status"] = "cached"
                self._state["error"] = "本地缓存，等待行情服务确认"
        except (OSError, ValueError, TypeError):
            pass

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state)

    def start(self, holding_symbol: str | None = None) -> None:
        self._holding_symbol = holding_symbol
        self._thread = threading.Thread(target=self._run, name="market-supervisor", daemon=True)
        self._thread.start()

    def watch_position(self, symbol: str | None) -> None:
        if symbol != self._holding_symbol:
            self._holding_symbol = symbol
            self._restart.set()

    def reconnect(self) -> None:
        self._restart.set()

    def stop(self) -> None:
        self._stop.set()
        self._restart.set()
        self._kill()
        if hasattr(self, "_thread"):
            self._thread.join(timeout=5)

    def _kill(self) -> None:
        process = self._process
        if process and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

    def _reader(self, process) -> None:
        for line in process.stdout:
            try:
                packet = json.loads(line)
            except (ValueError, TypeError):
                continue
            with self._lock:
                if process is not self._process:
                    return
                self._last_packet = time.monotonic()
                if packet.get("type") == "market":
                    self._state.update(packet)
                    self._state.pop("type", None)
                    if time.monotonic() - self._last_cache > 30:
                        self._last_cache = time.monotonic()
                        try:
                            write_json(self.cache_path, self._state)
                        except OSError:
                            pass
                elif packet.get("type") == "error":
                    self._state.update(status="error", error=packet.get("message"), error_code=packet.get("code"))
                elif packet.get("type") == "status":
                    self._state.update(status=packet.get("status", "connecting"), error=None)

    def _run(self) -> None:
        attempts = 0
        while not self._stop.is_set():
            self._restart.clear()
            arguments = [sys.executable, "-m", "futures_app.feed", "--worker"]
            if self._holding_symbol:
                arguments.extend(["--holding-symbol", self._holding_symbol])
            with self._lock:
                self._state["status"] = "connecting" if attempts == 0 else "reconnecting"
                self._last_packet = time.monotonic()
            self._process = subprocess.Popen(
                arguments, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            process = self._process
            reader = threading.Thread(target=self._reader, args=(process,), name="market-reader", daemon=True)
            reader.start()
            started = time.monotonic()
            while not self._stop.wait(1):
                if self._restart.is_set() or process.poll() is not None:
                    break
                if time.monotonic() - self._last_packet > 90:
                    with self._lock:
                        self._state.update(status="reconnecting", error="行情连接超时，正在重新连接")
                    break
            self._kill()
            reader.join(timeout=3)
            if self._stop.is_set():
                break
            if self._restart.is_set():
                attempts = 0
                continue
            attempts = 0 if time.monotonic() - started > 120 else min(attempts + 1, 6)
            self._stop.wait(min(30, 2 ** attempts))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--holding-symbol")
    args = parser.parse_args()
    if args.worker:
        raise SystemExit(market_worker(args.holding_symbol))
