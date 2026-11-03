"""One bounded diagnostic request; not a trading decision engine."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


def run_model_probe(api_key: str, config: dict) -> dict:
    if config["endpoint"] != "https://api.openai.com/v1/responses":
        return {"status": "failed", "error_code": "UNEXPECTED_API_ENDPOINT"}
    payload = {
        "model": config["model"],
        "input": "Connection diagnostic only. Reply with exactly CONNECTION_OK.",
        "reasoning": {"effort": config["reasoning_effort"]},
        "max_output_tokens": 256,
        "store": False,
    }
    request = urllib.request.Request(
        config["endpoint"], data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    # Refuse redirects so credentials cannot be forwarded to another host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    started = time.monotonic()
    try:
        opener = urllib.request.build_opener(NoRedirect)
        with opener.open(request, timeout=config["request_timeout_seconds"]) as response:
            data = json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as exc:
        return {"status": "failed", "error_code": f"HTTP_{exc.code}", "model": config["model"]}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"status": "failed", "error_code": "MODEL_NETWORK_ERROR", "model": config["model"]}
    except ValueError:
        return {"status": "failed", "error_code": "INVALID_API_JSON", "model": config["model"]}
    output = "".join(
        item.get("text", "")
        for message in data.get("output", []) if message.get("type") == "message"
        for item in message.get("content", []) if item.get("type") == "output_text"
    ).strip()
    passed = data.get("status") == "completed" and output == "CONNECTION_OK"
    usage = data.get("usage") or {}
    return {
        "status": "passed" if passed else "failed",
        "error_code": None if passed else "UNEXPECTED_MODEL_RESPONSE",
        "requested_model": config["model"],
        "actual_model": data.get("model"),
        "response_id": data.get("id"),
        "response_status": data.get("status"),
        "diagnostic_reply_matched": passed,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "usage": {name: usage.get(name) for name in ("input_tokens", "output_tokens", "total_tokens")},
        "request_count": 1,
        "orders_submitted": 0,
    }
