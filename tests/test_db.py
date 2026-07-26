"""Tests for backend/db.py — SQLite persistence and per-user scoping."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import db  # noqa: E402


class DbTestCase(unittest.TestCase):
    """Gives each test an isolated database file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "test.db"
        db.init_db(self.db_path)

    def make_user(self, username="alice"):
        return db.create_user(username, "hash", db_path=self.db_path)


class UserTests(DbTestCase):
    def test_create_and_fetch_user(self):
        user_id = self.make_user("alice")
        user = db.get_user_by_username("alice", db_path=self.db_path)
        self.assertEqual(user["id"], user_id)
        self.assertEqual(user["username"], "alice")
        self.assertEqual(user["password_hash"], "hash")

    def test_missing_user_returns_none(self):
        self.assertIsNone(db.get_user_by_username("nobody", db_path=self.db_path))

    def test_duplicate_username_is_rejected(self):
        self.make_user("alice")
        with self.assertRaises(Exception):
            self.make_user("alice")

    def test_init_db_is_idempotent(self):
        self.make_user("alice")
        db.init_db(self.db_path)  # must not wipe or error
        self.assertIsNotNone(db.get_user_by_username("alice", db_path=self.db_path))


class TaskTests(DbTestCase):
    def setUp(self):
        super().setUp()
        self.alice = self.make_user("alice")
        self.bob = self.make_user("bob")

    def test_create_and_list_task(self):
        task_id = db.create_task(self.alice, "write tests", db_path=self.db_path)
        tasks = db.list_tasks(self.alice, db_path=self.db_path)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], task_id)
        self.assertEqual(tasks[0]["title"], "write tests")
        self.assertEqual(tasks[0]["done"], 0)

    def test_list_is_scoped_to_owner(self):
        db.create_task(self.alice, "alice task", db_path=self.db_path)
        self.assertEqual(db.list_tasks(self.bob, db_path=self.db_path), [])

    def test_list_is_newest_first(self):
        db.create_task(self.alice, "first", db_path=self.db_path)
        db.create_task(self.alice, "second", db_path=self.db_path)
        titles = [t["title"] for t in db.list_tasks(self.alice, db_path=self.db_path)]
        self.assertEqual(titles, ["second", "first"])

    def test_update_title_and_done(self):
        task_id = db.create_task(self.alice, "old", db_path=self.db_path)
        self.assertTrue(
            db.update_task(task_id, self.alice, title="new", done=True, db_path=self.db_path)
        )
        task = db.list_tasks(self.alice, db_path=self.db_path)[0]
        self.assertEqual(task["title"], "new")
        self.assertEqual(task["done"], 1)

    def test_update_bumps_updated_at(self):
        task_id = db.create_task(self.alice, "t", db_path=self.db_path)
        before = db.list_tasks(self.alice, db_path=self.db_path)[0]["updated_at"]
        db.update_task(task_id, self.alice, done=True, db_path=self.db_path)
        after = db.list_tasks(self.alice, db_path=self.db_path)[0]["updated_at"]
        self.assertGreaterEqual(after, before)

    def test_update_with_no_fields_is_a_noop(self):
        task_id = db.create_task(self.alice, "t", db_path=self.db_path)
        self.assertFalse(db.update_task(task_id, self.alice, db_path=self.db_path))

    def test_cannot_update_another_users_task(self):
        task_id = db.create_task(self.alice, "alice task", db_path=self.db_path)
        self.assertFalse(db.update_task(task_id, self.bob, title="hijacked", db_path=self.db_path))
        self.assertEqual(db.list_tasks(self.alice, db_path=self.db_path)[0]["title"], "alice task")

    def test_delete_task(self):
        task_id = db.create_task(self.alice, "t", db_path=self.db_path)
        self.assertTrue(db.delete_task(task_id, self.alice, db_path=self.db_path))
        self.assertEqual(db.list_tasks(self.alice, db_path=self.db_path), [])

    def test_delete_is_idempotent_after_first_call(self):
        task_id = db.create_task(self.alice, "t", db_path=self.db_path)
        db.delete_task(task_id, self.alice, db_path=self.db_path)
        self.assertFalse(db.delete_task(task_id, self.alice, db_path=self.db_path))

    def test_cannot_delete_another_users_task(self):
        task_id = db.create_task(self.alice, "alice task", db_path=self.db_path)
        self.assertFalse(db.delete_task(task_id, self.bob, db_path=self.db_path))
        self.assertEqual(len(db.list_tasks(self.alice, db_path=self.db_path)), 1)

    def test_update_and_delete_on_unknown_id_return_false(self):
        self.assertFalse(db.update_task(9999, self.alice, done=True, db_path=self.db_path))
        self.assertFalse(db.delete_task(9999, self.alice, db_path=self.db_path))

    def test_title_with_sql_metacharacters_is_stored_literally(self):
        nasty = "'; DROP TABLE tasks; --"
        db.create_task(self.alice, nasty, db_path=self.db_path)
        self.assertEqual(db.list_tasks(self.alice, db_path=self.db_path)[0]["title"], nasty)


if __name__ == "__main__":
    unittest.main()
