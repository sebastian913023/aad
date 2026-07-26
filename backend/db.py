"""
db.py — SQLite-backed persistence layer for the task tracker (stdlib only).

All functions accept an optional db_path override that is resolved at CALL
TIME against the module-level DB_PATH (not bound at def-time), so tests can
safely repoint db.DB_PATH to an isolated temp database.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "task_tracker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


def _resolve(db_path: Optional[Path]) -> Path:
    return db_path if db_path is not None else DB_PATH


def init_db(db_path: Optional[Path] = None) -> None:
    path = _resolve(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    path = _resolve(db_path)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_user(username: str, password_hash: str, db_path: Optional[Path] = None) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, time.time()),
        )
        return cur.lastrowid


def get_user_by_username(username: str, db_path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def create_task(user_id: int, title: str, db_path: Optional[Path] = None) -> int:
    now = time.time()
    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO tasks (user_id, title, done, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
            (user_id, title, now, now),
        )
        return cur.lastrowid


def list_tasks(user_id: int, db_path: Optional[Path] = None) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_task(
    task_id: int,
    user_id: int,
    *,
    title: Optional[str] = None,
    done: Optional[bool] = None,
    db_path: Optional[Path] = None,
) -> bool:
    fields: list[str] = []
    values: list[Any] = []
    if title is not None:
        fields.append("title = ?")
        values.append(title)
    if done is not None:
        fields.append("done = ?")
        values.append(1 if done else 0)
    if not fields:
        return False
    fields.append("updated_at = ?")
    values.append(time.time())
    values.extend([task_id, user_id])
    with connect(db_path) as conn:
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(fields)} WHERE id = ? AND user_id = ?", values
        )
        return cur.rowcount > 0


def delete_task(task_id: int, user_id: int, db_path: Optional[Path] = None) -> bool:
    with connect(db_path) as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
        return cur.rowcount > 0
