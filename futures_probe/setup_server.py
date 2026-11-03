"""Short-lived loopback-only browser form for configuring local credentials."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs

from .secrets import CredentialError, credential_status, save_secrets


_PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>期货模拟 · 本机连接配置</title>
<style>body{font:16px/1.7 system-ui,sans-serif;background:#f4f6f9;color:#19253a;
max-width:640px;margin:8vh auto;padding:24px}main{background:white;border-radius:16px;
padding:30px;box-shadow:0 8px 30px #18304a12}h1{font-size:24px}label{display:block;margin:18px 0}
input{display:block;width:100%;box-sizing:border-box;padding:12px;border:1px solid #b6c1d1;
border-radius:8px;font:inherit}button{background:#155fca;color:white;border:0;padding:12px 24px;
border-radius:8px;font:inherit;cursor:pointer}.note{font-size:14px;color:#52627a}</style>
<main><h1>__TITLE__</h1><p>__INTRO__</p>
<form method="post" autocomplete="off">__FIELDS__<button type="submit">保存到本机</button></form>
<p class="note">凭据由 Windows 加密，仅保存在当前电脑。此页面不会发送凭据到第三方，
后续连接测试会向相应服务进行认证。无需把密码或密钥发送到聊天中。</p>
<p class="note">此页面在 20 分钟后关闭；保存成功仅代表本机配置完成，连接结果另行验证。</p></main></html>"""


def start_setup_server(
    stage: str = "market", *, secret_path: Path | str | None = None, ttl_seconds: int = 1200
) -> tuple[ThreadingHTTPServer, str]:
    """Start a daemon HTTP server and return (server, secret local form URL).

    Caller may stop it with ``server.shutdown(); server.server_close()``.
    ``secret_path`` is optional and primarily useful for isolated tests.
    """
    if stage not in {"market", "model"}:
        raise ValueError("stage must be market or model")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    token = secrets.token_urlsafe(32)
    base_path = "/" + token
    expires_at = time.monotonic() + ttl_seconds
    allowed_fields = {"TQ_USERNAME", "TQ_PASSWORD"} if stage == "market" else {"OPENAI_API_KEY"}
    if stage == "market":
        page = _PAGE.replace("__TITLE__", "连接快期行情").replace(
            "__INTRO__", "请填写已注册快期账号的手机号、邮箱或用户名，以及登录密码。短信验证码不能代替 SDK 密码。"
        ).replace("__FIELDS__", '<label>快期账号<input name="TQ_USERNAME" required maxlength="512" '
                  'autocomplete="off"></label><label>快期密码<input type="password" name="TQ_PASSWORD" '
                  'required maxlength="8192" autocomplete="off"></label>')
    else:
        page = _PAGE.replace("__TITLE__", "连接模型 API").replace(
            "__INTRO__", "填写为本项目创建的 OpenAI API 密钥。"
        ).replace("__FIELDS__", '<label>API 密钥<input type="password" name="OPENAI_API_KEY" '
                  'required maxlength="8192" autocomplete="off"></label>')

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalSetup"
        sys_version = ""

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(10)
            self._body_consumed = False

        def log_message(self, *_args: object) -> None:
            pass

        def _reply(self, status: int, body: str, *, json_body: bool = False) -> None:
            # Closing a Windows socket with an unread POST body can reset the
            # connection before the client receives a rejection response.
            if self.command == "POST" and not self._body_consumed:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if 0 < length <= 16384 and not self.headers.get("Transfer-Encoding"):
                        self.rfile.read(length)
                except (ValueError, OSError, TimeoutError):
                    pass
                self._body_consumed = True
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ("application/json" if json_body else "text/html") + "; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)

        def _allowed(self, *, write: bool) -> bool:
            expected_host = f"127.0.0.1:{self.server.server_port}"
            expected_origin = f"http://{expected_host}"
            if self.client_address[0] != "127.0.0.1" or self.headers.get("Host") != expected_host:
                self._reply(403, "请求来源不受支持。")
                return False
            origin = self.headers.get("Origin")
            if (write and origin != expected_origin) or (origin is not None and origin != expected_origin):
                self._reply(403, "请求来源不受支持。")
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self._reply(403, "请求来源不受支持。")
                return False
            if time.monotonic() >= expires_at:
                self._reply(410, "配置页面已过期，请重新打开。")
                return False
            return True

        def do_GET(self) -> None:
            if not self._allowed(write=False):
                return
            if self.path == base_path:
                self._reply(200, page)
            elif self.path == base_path + "/status":
                try:
                    status = credential_status(secret_path)
                    self._reply(200, json.dumps({name: status[name] for name in sorted(allowed_fields)}), json_body=True)
                except CredentialError:
                    self._reply(500, '{"error":"credential_store_unavailable"}', json_body=True)
            else:
                self._reply(404, "页面不存在。")

        def do_POST(self) -> None:
            if not self._allowed(write=True):
                return
            if self.path != base_path:
                self._reply(404, "页面不存在。")
                return
            if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                self._reply(415, "不支持的表单格式。")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    self._reply(413, "表单内容长度无效。")
                    return
                raw = self.rfile.read(length)
                self._body_consumed = True
                if len(raw) != length:
                    self._reply(400, "表单未完整提交。")
                    return
                form = parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=4)
                if set(form) != allowed_fields or any(len(items) != 1 or not items[0] for items in form.values()):
                    self._reply(400, "请完整填写所需的凭据字段。")
                    return
                values = {name: items[0] for name, items in form.items()}
                if "TQ_USERNAME" in values:
                    values["TQ_USERNAME"] = values["TQ_USERNAME"].strip()
                if "OPENAI_API_KEY" in values:
                    values["OPENAI_API_KEY"] = values["OPENAI_API_KEY"].strip()
                save_secrets(values, secret_path)
            except CredentialError:
                self._reply(500, "保存失败，请检查当前 Windows 账户和本机目录权限后重试。")
                return
            except (ValueError, UnicodeError, OSError):
                self._reply(400, "表单无效或提交未完成，请重试。")
                return
            self._reply(200, '<!doctype html><meta charset="utf-8"><title>已保存</title>'
                        '<main style="font:18px/1.8 system-ui;max-width:640px;margin:10vh auto;padding:24px">'
                        '<h1>已加密保存到本机</h1><p>现在可以返回聊天，继续验证连接。'
                        '保存成功不代表服务认证或行情验证已通过。</p></main>')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="local-credential-setup", daemon=True)
    thread.start()
    def expire() -> None:
        server.shutdown()
        server.server_close()

    timer = threading.Timer(ttl_seconds, expire)
    timer.daemon = True
    timer.start()
    # Exposed for callers that run this helper in a dedicated background process.
    server.setup_thread = thread
    server.setup_timer = timer
    return server, f"http://127.0.0.1:{server.server_port}{base_path}"


def main() -> None:
    parser = argparse.ArgumentParser(description="本机凭据配置页面")
    parser.add_argument("--stage", choices=["market", "model"], default="market")
    args = parser.parse_args()
    server, url = start_setup_server(args.stage)
    print(url, flush=True)
    try:
        server.setup_thread.join()
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        server.setup_timer.cancel()
        server.server_close()


if __name__ == "__main__":
    main()
