from __future__ import annotations

import argparse
import json
import signal
import sys

from .common import ROOT


def main() -> int:
    parser = argparse.ArgumentParser(description="IF 本地 AI 期货模拟交易台")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-auto-start", action="store_true", help="本次启动暂停新开仓，保留独立持仓风控")
    args = parser.parse_args()
    from .service import TradingService
    from .web import make_server
    service = TradingService()
    config = service.config
    if args.no_auto_start:
        service.control("pause")
    port = args.port or config.get("app", {}).get("port", 8765)
    try:
        server = make_server(service, "127.0.0.1", port)
    except OSError:
        print(f"端口 {port} 已被使用。请打开已有交易台，或使用 --port 选择其他端口。", flush=True)
        service.stop()
        return 2
    service.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"IF 本地模拟交易台已启动：{url}", flush=True)
    print("账本保存在 data/futures.sqlite3；按 Ctrl+C 结束，持仓将在下次启动时恢复。", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"启动未完成（{type(exc).__name__}）。请查看项目说明和本机运行环境。", file=sys.stderr)
        raise SystemExit(1)
