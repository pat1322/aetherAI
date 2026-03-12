"""
AetherAI — Memory Manager
Persistent storage using SQLite. Tracks tasks, steps, and user preferences.
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


class MemoryManager:
    """SQLite-backed memory for tasks, steps, and preferences."""

    def __init__(self):
        db_path = Path(settings.DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id     TEXT PRIMARY KEY,
                    command     TEXT NOT NULL,
                    source      TEXT DEFAULT 'web',
                    status      TEXT DEFAULT 'pending',
                    result      TEXT,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS steps (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id     TEXT NOT NULL,
                    step_number INTEGER NOT NULL,
                    agent       TEXT,
                    description TEXT,
                    status      TEXT DEFAULT 'pending',
                    output      TEXT,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE TABLE IF NOT EXISTS preferences (
                    key         TEXT PRIMARY KEY,
                    value       TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                );
            """)
        logger.info(f"Database ready at {self.db_path}")

    # ── Tasks ─────────────────────────────────────────────────────────────────

    def create_task(self, task_id: str, command: str, source: str = "web"):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO tasks (task_id, command, source, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (task_id, command, source, now, now)
            )

    def get_task(self, task_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                return None
            task = dict(row)
            task["steps"] = self._get_steps(task_id, conn)
            return task

    def update_task_status(self, task_id: str, status: str, result: str = None):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            if result:
                conn.execute(
                    "UPDATE tasks SET status=?, result=?, updated_at=? WHERE task_id=?",
                    (status, result, now, task_id)
                )
            else:
                conn.execute(
                    "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                    (status, now, task_id)
                )

    def list_tasks(self, limit: int = 20) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Steps ─────────────────────────────────────────────────────────────────

    def create_step(self, task_id: str, step_number: int, agent: str, description: str):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO steps (task_id, step_number, agent, description, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (task_id, step_number, agent, description, now, now)
            )

    def update_step(self, task_id: str, step_number: int, status: str, output: str = None):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE steps SET status=?, output=?, updated_at=? "
                "WHERE task_id=? AND step_number=?",
                (status, output, now, task_id, step_number)
            )

    def _get_steps(self, task_id: str, conn: sqlite3.Connection) -> list:
        rows = conn.execute(
            "SELECT * FROM steps WHERE task_id=? ORDER BY step_number", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Preferences ───────────────────────────────────────────────────────────

    def set_preference(self, key: str, value):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO preferences (key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), now)
            )

    def get_preference(self, key: str, default=None):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM preferences WHERE key=?", (key,)
            ).fetchone()
            if not row:
                return default
            return json.loads(row["value"])

    def delete_task(self, task_id: str) -> bool:
        """Delete a task and all its steps."""
        with self._conn() as conn:
            result = conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM steps WHERE task_id=?", (task_id,))
            return result.rowcount > 0

    def delete_all_tasks(self) -> int:
        """Delete all tasks and steps. Returns count deleted."""
        with self._conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            conn.execute("DELETE FROM steps")
            conn.execute("DELETE FROM tasks")
            return count
