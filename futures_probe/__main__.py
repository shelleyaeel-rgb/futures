"""Read-only connection validation. No order placement or trading loop."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "data" / "connection-checks"


def save_report(name: str, report: dict) -> dict:
    report["checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    REPORTS.mkdir(parents=True, exist_ok=True)
    destination = REPORTS / f"{name}-latest.json"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(destination)
    return report


def blocked(code: str) -> dict:
    return {"status": "blocked", "error_code": code, "model_gate_passed": False}


def market_worker(seconds: int) -> int:
    from .secrets import get_secret
    username, password = get_secret("TQ_USERNAME"), get_secret("TQ_PASSWORD")
    if not username or not password:
        report = blocked("MISSING_MARKET_CREDENTIALS")
    else:
        # SDK logs and raw authentication errors must never reach shared output.
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                from .market import run_market_probe
                report = run_market_probe(username, password, observation_seconds=seconds)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0


def check_market(config: dict) -> dict:
    from .secrets import get_secret
    if not get_secret("TQ_USERNAME") or not get_secret("TQ_PASSWORD"):
        return save_report("market", blocked("MISSING_MARKET_CREDENTIALS"))
    try:
        result = subprocess.run(
            [sys.executable, "-m", "futures_probe", "_market_worker", "--seconds", str(config["market"]["probe_seconds"])],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            timeout=config["market"]["probe_timeout_seconds"],
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        report = json.loads(result.stdout) if result.returncode == 0 else blocked("MARKET_WORKER_FAILED")
    except subprocess.TimeoutExpired:
        report = blocked("MARKET_TOTAL_TIMEOUT")
    except (ValueError, OSError):
        report = blocked("MARKET_WORKER_FAILED")
    return save_report("market", report)


def check_model(config: dict) -> dict:
    # Respect the requested sequence: live market validation first.
    try:
        market_report = json.loads((REPORTS / "market-latest.json").read_text(encoding="utf-8"))
        checked = dt.datetime.fromisoformat(market_report["checked_at"])
        recent = 0 <= (dt.datetime.now(dt.timezone.utc) - checked).total_seconds() <= 86400
    except (OSError, ValueError, KeyError, TypeError):
        market_report, recent = {}, False
    if market_report.get("model_gate_passed") is not True or not recent:
        return save_report("model", blocked("MARKET_VALIDATION_REQUIRED"))
    from .secrets import get_secret
    api_key = get_secret("OPENAI_API_KEY")
    if not api_key:
        return save_report("model", blocked("MISSING_MODEL_API_KEY"))
    from .model import run_model_probe
    return save_report("model", run_model_probe(api_key, config["model"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="IF 行情与 AI 连接验证（不下单）")
    parser.add_argument("action", choices=["status", "market", "model", "all", "setup", "_market_worker"])
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--stage", choices=["market", "model"], default="market")
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    if args.action == "_market_worker":
        return market_worker(args.seconds)
    if args.action == "setup":
        from .setup_server import start_setup_server
        server, url = start_setup_server(stage=args.stage)
        print(url, flush=True)
        try:
            server.setup_thread.join()
        except KeyboardInterrupt:
            server.shutdown()
        finally:
            server.setup_timer.cancel()
            server.server_close()
        return 0
    if args.action == "status":
        from .secrets import get_secret
        report = {
            "market_credentials_configured": bool(get_secret("TQ_USERNAME") and get_secret("TQ_PASSWORD")),
            "model_key_configured": bool(get_secret("OPENAI_API_KEY")),
            "configured_model": config["model"]["model"],
            "automatic_trading_enabled": config["automatic_trading_enabled"],
        }
    elif args.action == "market":
        report = check_market(config)
    elif args.action == "model":
        report = check_model(config)
    else:
        report = {"market": check_market(config)}
        report["model"] = check_model(config)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    if report.get("status") in ("blocked", "failed", "partial_snapshot"):
        return 2
    if args.action == "all" and report["model"].get("status") != "passed":
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Exception type is safe; exception text may contain credentials.
        print(json.dumps({"status": "failed", "error_code": type(exc).__name__}))
        raise SystemExit(1)
