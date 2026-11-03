"""Isolated HTTP/DPAPI checks; all submitted values are synthetic."""

import http.client
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from futures_probe.secrets import load_secrets
from futures_probe.setup_server import start_setup_server


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class SecretSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="futures-setup-test-")
        self.addCleanup(self.temporary.cleanup)
        self.store = Path(self.temporary.name) / "synthetic.dpapi"
        self.environment = patch.dict(os.environ, {
            "TQ_USERNAME": "", "TQ_PASSWORD": "", "OPENAI_API_KEY": ""
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.server, self.url = start_setup_server("market", secret_path=self.store, ttl_seconds=60)
        self.addCleanup(self._stop, self.server)
        self.endpoint = urlsplit(self.url)
        self.synthetic = {"TQ_USERNAME": "test-user-only", "TQ_PASSWORD": "synthetic-password-only"}

    @staticmethod
    def _stop(server):
        server.setup_timer.cancel()
        server.shutdown()
        server.server_close()
        server.setup_thread.join(timeout=2)

    def request(self, method="GET", *, suffix="", fields=None, headers=None, endpoint=None):
        target = endpoint or self.endpoint
        request_headers = {"Host": target.netloc}
        body = None
        if method == "POST":
            body = urlencode(fields if fields is not None else self.synthetic)
            request_headers.update({
                "Origin": f"http://{target.netloc}",
                "Content-Type": "application/x-www-form-urlencoded",
            })
        if headers:
            request_headers.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", target.port, timeout=3)
        try:
            connection.request(method, target.path + suffix, body, request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_get_only_exposes_stage_fields_and_no_cache(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn(b'name="TQ_USERNAME"', body)
        self.assertIn(b'name="TQ_PASSWORD"', body)
        self.assertNotIn(b'name="OPENAI_API_KEY"', body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        # no-referrer can cause Chrome form navigations to send Origin: null.
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertFalse(self.store.exists())

    def test_legal_post_encrypts_and_status_only_returns_presence(self):
        status, _, body = self.request("POST")
        self.assertEqual(status, 200)
        self.assertEqual(load_secrets(self.store), self.synthetic)
        for value in self.synthetic.values():
            self.assertNotIn(value.encode(), self.store.read_bytes())
            self.assertNotIn(value.encode(), body)
        status, _, body = self.request(suffix="/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"TQ_USERNAME": True, "TQ_PASSWORD": True})

    def test_forged_host_origin_and_cross_site_are_rejected(self):
        variants = [
            ("GET", {"Host": "attacker.invalid"}),
            ("POST", {"Origin": "https://attacker.invalid"}),
            ("POST", {"Origin": "null"}),
            ("POST", {"Host": "localhost:" + str(self.endpoint.port)}),
            ("POST", {"Sec-Fetch-Site": "cross-site"}),
        ]
        for method, headers in variants:
            with self.subTest(method=method, header_names=sorted(headers)):
                status, _, _ = self.request(method, headers=headers)
                self.assertEqual(status, 403)
                self.assertFalse(self.store.exists())

    def test_extra_missing_and_duplicate_fields_are_rejected(self):
        variants = [
            dict(self.synthetic, OPENAI_API_KEY="synthetic-extra-key"),
            {"TQ_USERNAME": "test-user-only"},
            [("TQ_USERNAME", "test-user-only"), ("TQ_PASSWORD", "a"), ("TQ_PASSWORD", "b")],
        ]
        for fields in variants:
            with self.subTest(field_shape=type(fields).__name__):
                status, _, _ = self.request("POST", fields=fields)
                self.assertEqual(status, 400)
                self.assertFalse(self.store.exists())

    def test_unknown_token_path_cannot_read_or_write(self):
        for method in ("GET", "POST"):
            status, _, _ = self.request(method, suffix="-wrong-token")
            self.assertEqual(status, 404)
            self.assertFalse(self.store.exists())

    def test_model_stage_only_accepts_its_key(self):
        server, url = start_setup_server("model", secret_path=self.store, ttl_seconds=60)
        self.addCleanup(self._stop, server)
        endpoint = urlsplit(url)
        status, _, body = self.request(endpoint=endpoint)
        self.assertEqual(status, 200)
        self.assertIn(b'name="OPENAI_API_KEY"', body)
        self.assertNotIn(b'name="TQ_USERNAME"', body)
        status, _, _ = self.request("POST", endpoint=endpoint)
        self.assertEqual(status, 400)
        status, _, body = self.request("POST", fields={"OPENAI_API_KEY": "synthetic-key-only"}, endpoint=endpoint)
        self.assertEqual(status, 200)
        self.assertEqual(load_secrets(self.store), {"OPENAI_API_KEY": "synthetic-key-only"})
        self.assertNotIn(b"synthetic-key-only", body)


if __name__ == "__main__":
    unittest.main()
