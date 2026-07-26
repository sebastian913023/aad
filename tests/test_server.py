"""
Integration tests for backend/server.py.

Boots a real ThreadingHTTPServer on an ephemeral port against a temporary
database and drives it over HTTP, so routing, auth enforcement, and status
codes are all exercised end to end.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import db  # noqa: E402
import server  # noqa: E402


class ApiTestCase(unittest.TestCase):
    """Boots one server per test class, backed by a fresh database."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._original_db_path = db.DB_PATH
        db.DB_PATH = Path(cls._tmp.name) / "test.db"

        cls.server = server.run(host="127.0.0.1", port=0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        db.DB_PATH = cls._original_db_path
        cls._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, method: str, path: str, body=None, token=None):
        """Returns (status, parsed_json_or_raw_text)."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url(path), data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read().decode()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            status = exc.code
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, raw

    def register_and_login(self, username, password="pw"):
        self.request("POST", "/api/register", {"username": username, "password": password})
        status, body = self.request(
            "POST", "/api/login", {"username": username, "password": password}
        )
        self.assertEqual(status, 200)
        return body["token"]


class HealthAndStaticTests(ApiTestCase):
    def test_healthz(self):
        status, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_root_serves_frontend(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("<html", body.lower())

    def test_static_path_with_query_string(self):
        status, body = self.request("GET", "/index.html?cachebust=1")
        self.assertEqual(status, 200)
        self.assertIn("<html", body.lower())

    def test_unknown_api_path_returns_json_404(self):
        status, body = self.request("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not found")

    def test_unknown_page_falls_back_to_index(self):
        status, body = self.request("GET", "/some/spa/route")
        self.assertEqual(status, 200)
        self.assertIn("<html", body.lower())

    def test_path_traversal_does_not_leak_source(self):
        """A traversal attempt must never return backend source."""
        raw_request = (
            "GET /../backend/auth.py HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            "Connection: close\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
            sock.sendall(raw_request.encode())
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        response = b"".join(chunks).decode(errors="replace")
        self.assertNotIn("SECRET_KEY", response)
        self.assertNotIn("pbkdf2_hmac", response)

    def test_options_preflight(self):
        status, _ = self.request("OPTIONS", "/api/tasks")
        self.assertEqual(status, 204)


class RegistrationAndLoginTests(ApiTestCase):
    def test_register_returns_201(self):
        status, body = self.request(
            "POST", "/api/register", {"username": "newuser", "password": "pw"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "created")

    def test_duplicate_registration_returns_409(self):
        self.request("POST", "/api/register", {"username": "dupe", "password": "pw"})
        status, body = self.request("POST", "/api/register", {"username": "dupe", "password": "pw"})
        self.assertEqual(status, 409)
        self.assertIn("exists", body["error"])

    def test_register_requires_both_fields(self):
        for payload in [{"username": "x"}, {"password": "pw"}, {}]:
            with self.subTest(payload=payload):
                status, _ = self.request("POST", "/api/register", payload)
                self.assertEqual(status, 400)

    def test_login_with_wrong_password_returns_401(self):
        self.request("POST", "/api/register", {"username": "wrongpw", "password": "right"})
        status, _ = self.request("POST", "/api/login", {"username": "wrongpw", "password": "nope"})
        self.assertEqual(status, 401)

    def test_login_for_unknown_user_returns_401(self):
        status, _ = self.request("POST", "/api/login", {"username": "ghost", "password": "pw"})
        self.assertEqual(status, 401)

    def test_login_returns_usable_token(self):
        token = self.register_and_login("tokenuser")
        status, _ = self.request("GET", "/api/tasks", token=token)
        self.assertEqual(status, 200)


class AuthEnforcementTests(ApiTestCase):
    def test_task_endpoints_require_a_token(self):
        cases = [
            ("GET", "/api/tasks", None),
            ("POST", "/api/tasks", {"title": "x"}),
            ("PATCH", "/api/tasks/1", {"done": True}),
            ("DELETE", "/api/tasks/1", None),
        ]
        for method, path, body in cases:
            with self.subTest(method=method, path=path):
                status, _ = self.request(method, path, body)
                self.assertEqual(status, 401)

    def test_garbage_and_forged_tokens_are_rejected(self):
        for bad in ["garbage", "a.b.c", ""]:
            with self.subTest(token=bad):
                status, _ = self.request("GET", "/api/tasks", token=bad)
                self.assertEqual(status, 401)

    def test_token_signed_with_wrong_secret_is_rejected(self):
        import auth

        forged = auth.encode_token({"sub": "alice"}, secret="not-the-server-secret")
        status, _ = self.request("GET", "/api/tasks", token=forged)
        self.assertEqual(status, 401)

    def test_missing_bearer_prefix_is_rejected(self):
        token = self.register_and_login("prefixuser")
        req = urllib.request.Request(self.url("/api/tasks"), method="GET")
        req.add_header("Authorization", token)  # no "Bearer "
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 401)


class TaskCrudTests(ApiTestCase):
    def setUp(self):
        self.token = self.register_and_login(f"crud{id(self)}")

    def test_create_task_returns_id(self):
        status, body = self.request("POST", "/api/tasks", {"title": "buy milk"}, self.token)
        self.assertEqual(status, 201)
        self.assertIsInstance(body["id"], int)

    def test_create_requires_nonempty_title(self):
        for payload in [{"title": ""}, {"title": "   "}, {}]:
            with self.subTest(payload=payload):
                status, _ = self.request("POST", "/api/tasks", payload, self.token)
                self.assertEqual(status, 400)

    def test_list_returns_created_tasks(self):
        self.request("POST", "/api/tasks", {"title": "task one"}, self.token)
        status, body = self.request("GET", "/api/tasks", token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("task one", [t["title"] for t in body["tasks"]])

    def test_patch_updates_done_and_title(self):
        _, created = self.request("POST", "/api/tasks", {"title": "old"}, self.token)
        task_id = created["id"]

        status, _ = self.request(
            "PATCH", f"/api/tasks/{task_id}", {"title": "new", "done": True}, self.token
        )
        self.assertEqual(status, 200)

        _, body = self.request("GET", "/api/tasks", token=self.token)
        task = next(t for t in body["tasks"] if t["id"] == task_id)
        self.assertEqual(task["title"], "new")
        self.assertEqual(task["done"], 1)

    def test_patch_unknown_task_returns_404(self):
        status, _ = self.request("PATCH", "/api/tasks/999999", {"done": True}, self.token)
        self.assertEqual(status, 404)

    def test_delete_removes_task(self):
        _, created = self.request("POST", "/api/tasks", {"title": "temp"}, self.token)
        task_id = created["id"]
        status, _ = self.request("DELETE", f"/api/tasks/{task_id}", token=self.token)
        self.assertEqual(status, 200)

        _, body = self.request("GET", "/api/tasks", token=self.token)
        self.assertNotIn(task_id, [t["id"] for t in body["tasks"]])

    def test_delete_unknown_task_returns_404(self):
        status, _ = self.request("DELETE", "/api/tasks/999999", token=self.token)
        self.assertEqual(status, 404)


class CrossUserIsolationTests(ApiTestCase):
    def test_users_cannot_see_or_touch_each_others_tasks(self):
        alice = self.register_and_login("iso_alice")
        bob = self.register_and_login("iso_bob")

        _, created = self.request("POST", "/api/tasks", {"title": "alice secret"}, alice)
        task_id = created["id"]

        # Bob cannot see it.
        _, bob_tasks = self.request("GET", "/api/tasks", token=bob)
        self.assertNotIn("alice secret", [t["title"] for t in bob_tasks["tasks"]])

        # Bob cannot modify or delete it.
        status, _ = self.request("PATCH", f"/api/tasks/{task_id}", {"title": "hijacked"}, bob)
        self.assertEqual(status, 404)
        status, _ = self.request("DELETE", f"/api/tasks/{task_id}", token=bob)
        self.assertEqual(status, 404)

        # Alice's task is untouched.
        _, alice_tasks = self.request("GET", "/api/tasks", token=alice)
        self.assertEqual(
            next(t for t in alice_tasks["tasks"] if t["id"] == task_id)["title"],
            "alice secret",
        )


if __name__ == "__main__":
    unittest.main()
