import http.client
import json
import threading
import unittest

from futures_app.web import make_server


class StubService:
    def __init__(self):
        self.actions = []

    def state(self):
        return {"mode": "local_simulation", "position": None}

    def control(self, action):
        self.actions.append(action)
        return {"status": "ok", "message": action}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.service = StubService()
        self.server = make_server(self.service, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method="GET", headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request(method, path, body=body, headers={"Host": self.host, **(headers or {})})
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    def test_static_and_state_are_readable_and_no_store(self):
        for path in ("/", "/app.js", "/app.css", "/api/state"):
            status, _, headers = self.request(path)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_write_requires_origin_and_unpredictable_csrf(self):
        body = json.dumps({"action": "start"})
        status, _, _ = self.request("/api/control", "POST", {"Content-Type": "application/json"}, body)
        self.assertEqual(status, 403)
        token = json.loads(self.request("/api/bootstrap")[1])["csrf_token"]
        headers = {"Content-Type": "application/json", "Origin": f"http://{self.host}", "X-CSRF-Token": token}
        self.assertEqual(self.request("/api/control", "POST", headers, body)[0], 200)
        self.assertEqual(self.service.actions, ["start"])
        headers["Origin"] = "https://unrelated.invalid"
        self.assertEqual(self.request("/api/control", "POST", headers, body)[0], 403)
        self.assertEqual(self.service.actions, ["start"])

    def test_credentials_and_path_traversal_are_not_served(self):
        for path in ("/config.json", "/.local/secrets.dpapi", "/../config.json", "/futures_probe/secrets.py"):
            self.assertEqual(self.request(path)[0], 404)

    def test_unknown_control_cannot_invoke_service_methods(self):
        token = json.loads(self.request("/api/bootstrap")[1])["csrf_token"]
        headers = {"Content-Type": "application/json", "Origin": f"http://{self.host}", "X-CSRF-Token": token}
        self.assertEqual(self.request("/api/control", "POST", headers, '{"action":"reset_account"}')[0], 400)
        self.assertEqual(self.service.actions, [])


if __name__ == "__main__":
    unittest.main()
