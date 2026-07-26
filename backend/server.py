"""
server.py — Integration layer: stdlib REST API wiring auth.py + db.py together,
plus static file serving for the frontend. Zero third-party dependencies
(http.server + json only).
"""

from __future__ import annotations

import json
import mimetypes
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth  # noqa: E402
import db  # noqa: E402

TASK_ID_RE = re.compile(r"^/api/tasks/(\d+)$")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel_path: str) -> bool:
        """Serve a file from FRONTEND_DIR. Returns False if not found/unsafe."""
        candidate = (FRONTEND_DIR / rel_path).resolve()
        if FRONTEND_DIR not in candidate.parents and candidate != FRONTEND_DIR:
            return False
        if not candidate.is_file():
            return False
        content_type, _ = mimetypes.guess_type(str(candidate))
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def _authed_user(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        token = header[len("Bearer ") :]
        try:
            payload = auth.decode_token(token)
        except auth.AuthError:
            return None
        return db.get_user_by_username(payload.get("sub", ""))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/register":
            body = self._read_json()
            username = str(body.get("username", "")).strip()
            password = str(body.get("password", ""))
            if not username or not password:
                return self._send_json(400, {"error": "username and password required"})
            if db.get_user_by_username(username):
                return self._send_json(409, {"error": "username already exists"})
            db.create_user(username, auth.hash_password(password))
            return self._send_json(201, {"status": "created"})

        if self.path == "/api/login":
            body = self._read_json()
            username = str(body.get("username", "")).strip()
            password = str(body.get("password", ""))
            user = db.get_user_by_username(username)
            if not user or not auth.verify_password(password, user["password_hash"]):
                return self._send_json(401, {"error": "invalid credentials"})
            token = auth.encode_token({"sub": username})
            return self._send_json(200, {"token": token})

        if self.path == "/api/tasks":
            user = self._authed_user()
            if not user:
                return self._send_json(401, {"error": "unauthorized"})
            body = self._read_json()
            title = str(body.get("title", "")).strip()
            if not title:
                return self._send_json(400, {"error": "title required"})
            task_id = db.create_task(user["id"], title)
            return self._send_json(201, {"id": task_id})

        return self._send_json(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/api/tasks":
            user = self._authed_user()
            if not user:
                return self._send_json(401, {"error": "unauthorized"})
            return self._send_json(200, {"tasks": db.list_tasks(user["id"])})
        if self.path == "/healthz":
            return self._send_json(200, {"status": "ok"})
        if self.path.startswith("/api/"):
            return self._send_json(404, {"error": "not found"})

        # Static frontend serving
        path = urlparse(self.path).path
        rel_path = path.lstrip("/") or "index.html"
        if self._send_static(rel_path):
            return
        if self._send_static("index.html"):
            return
        return self._send_json(404, {"error": "not found"})

    def do_PATCH(self):
        match = TASK_ID_RE.match(self.path)
        if match:
            user = self._authed_user()
            if not user:
                return self._send_json(401, {"error": "unauthorized"})
            task_id = int(match.group(1))
            body = self._read_json()
            ok = db.update_task(task_id, user["id"], title=body.get("title"), done=body.get("done"))
            if not ok:
                return self._send_json(404, {"error": "task not found"})
            return self._send_json(200, {"status": "updated"})
        return self._send_json(404, {"error": "not found"})

    def do_DELETE(self):
        match = TASK_ID_RE.match(self.path)
        if match:
            user = self._authed_user()
            if not user:
                return self._send_json(401, {"error": "unauthorized"})
            task_id = int(match.group(1))
            ok = db.delete_task(task_id, user["id"])
            if not ok:
                return self._send_json(404, {"error": "task not found"})
            return self._send_json(200, {"status": "deleted"})
        return self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):  # silence default access logging
        pass


def run(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    db.init_db()
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    srv = run()
    print(f"Serving on http://{srv.server_address[0]}:{srv.server_address[1]}")
    srv.serve_forever()
