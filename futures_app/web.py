"""Loopback-only application API, with same-origin write protection."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit

from .common import ROOT, safe_json


def make_server(service, host: str = "127.0.0.1", port: int = 8765):
    if host != "127.0.0.1":
        raise ValueError("This local application must bind to 127.0.0.1")
    token = secrets.token_urlsafe(32)
    assets = ROOT / "futures_app" / "static"

    class Handler(BaseHTTPRequestHandler):
        server_version = "FuturesLocal"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(10)
            self._body_consumed = False

        def log_message(self, *_args):
            pass

        def send(self, status: int, data, content_type="application/json; charset=utf-8"):
            if self.command == "POST" and not self._body_consumed:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if 0 < length <= 1024 and not self.headers.get("Transfer-Encoding"):
                        self.rfile.read(length)
                except (ValueError, OSError, TimeoutError):
                    pass
                self._body_consumed = True
            if isinstance(data, (dict, list)):
                data = json.dumps(safe_json(data), ensure_ascii=False, allow_nan=False).encode("utf-8")
            elif isinstance(data, str):
                data = data.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def allowed(self, write=False):
            expected_host = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin")
            if self.client_address[0] != "127.0.0.1" or self.headers.get("Host") != expected_host:
                self.send(403, {"error": "local_origin_required"})
                return False
            if origin is not None and origin != f"http://{expected_host}":
                self.send(403, {"error": "local_origin_required"})
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.send(403, {"error": "same_origin_required"})
                return False
            if write and (origin != f"http://{expected_host}" or not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), token)):
                self.send(403, {"error": "csrf_check_failed"})
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            path = urlsplit(self.path).path
            if path == "/api/bootstrap":
                self.send(200, {"csrf_token": token})
            elif path == "/api/health":
                self.send(200, {"status": "ok", "mode": "local_simulation", "version": "0.1.0"})
            elif path == "/api/state":
                try:
                    state = service.state()
                except Exception as exc:
                    self.send(503, {"error": "state_unavailable", "code": type(exc).__name__, "message": "状态暂时不可用，页面会自动重试。"})
                else:
                    self.send(200, state)
            elif path in ("/", "/index.html", "/app.css", "/app.js"):
                file = assets / ("index.html" if path == "/" else path.lstrip("/"))
                try:
                    content_type = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}[file.suffix]
                    self.send(200, file.read_bytes(), content_type)
                except OSError:
                    self.send(404, {"error": "asset_not_found"})
            elif path == "/favicon.ico":
                self.send(204, b"", "image/x-icon")
            else:
                self.send(404, {"error": "not_found"})

        def do_POST(self):
            if not self.allowed(write=True):
                return
            if urlsplit(self.path).path != "/api/control":
                self.send(404, {"error": "not_found"})
                return
            if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                self.send(415, {"error": "json_required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    self.send(413, {"error": "invalid_body_length"})
                    return
                raw = self.rfile.read(length)
                self._body_consumed = True
                data = json.loads(raw)
                if not isinstance(data, dict) or set(data) != {"action"} or data["action"] not in {"start", "pause", "close", "analyze", "review", "check", "shutdown"}:
                    self.send(400, {"error": "invalid_action"})
                    return
                if data["action"] == "shutdown":
                    self.send(200, {"status": "ok", "message": "正在保存状态并退出，持仓下次启动时恢复。"})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                self.send(200, service.control(data["action"]))
            except (ValueError, TypeError, UnicodeError):
                self.send(400, {"error": "invalid_json"})
            except Exception as exc:
                self.send(503, {"error": "control_failed", "code": type(exc).__name__, "message": "操作未完成，请稍后再试。"})

    class LocalServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            # Navigating away or restarting a local browser can abort a poll.
            # Never print request contents or raw exception text to shared logs.
            import sys
            error = sys.exception()
            if not isinstance(error, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
                print(f"Local HTTP request failed ({type(error).__name__}).", file=sys.stderr)

    server = LocalServer((host, port), Handler)
    server.daemon_threads = True
    return server
