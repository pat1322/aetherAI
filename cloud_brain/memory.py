"""
AetherAI — Memory Manager  (Stage 5 — fully patched)

All fixes applied
─────────────────
FIX A  PRAGMA foreign_keys = ON added to every connection so the declared
       FOREIGN KEY constraints on `steps` and `files` are actually enforced.
       Without this SQLite silently ignores FK violations, allowing orphaned
       step/file rows when a task is deleted out-of-order.

FIX B  delete_preference(key) performs a real SQL DELETE (original Stage 5
       patch — retained).
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

RESULT_MAX_CHARS = 2000


class MemoryManager:
    """SQLite-backed memory for tasks, steps, files, and preferences."""

    def __init__(self):
        db_path = Path(settings.DB_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path)
        self._init_db()
        self._purge_on_startup()

    # ── Connection ─────────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=10.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")   # FIX A: enforce FK constraints
        return conn

    # ── Schema ─────────────────────────────────────────────────────────────────

    def _init_db(self):
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

                CREATE TABLE IF NOT EXISTS files (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename    TEXT NOT NULL UNIQUE,
                    filepath    TEXT NOT NULL,
                    size_bytes  INTEGER DEFAULT 0,
                    task_id     TEXT,
                    created_at  TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_created
                    ON tasks(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tasks_status
                    ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_steps_task
                    ON steps(task_id);
                CREATE INDEX IF NOT EXISTS idx_steps_task_num
                    ON steps(task_id, step_number);
                CREATE INDEX IF NOT EXISTS idx_files_task
                    ON files(task_id);
                CREATE INDEX IF NOT EXISTS idx_files_created
                    ON files(created_at DESC);
            """)
        logger.info(f"[Memory] Database ready at {self.db_path}")

    def _purge_on_startup(self):
        days = getattr(settings, "TASK_RETENTION_DAYS", 30)
        try:
            deleted = self.purge_old_tasks(days=days)
            if deleted:
                logger.info(f"[Memory] Auto-purged {deleted} tasks older than {days} days")
        except Exception as e:
            logger.warning(f"[Memory] Auto-purge failed: {e}")

    # ── Tasks ──────────────────────────────────────────────────────────────────

    def create_task(self, task_id: str, command: str, source: str = "web"):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO tasks (task_id, command, source, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (task_id, command, source, now, now),
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
        if result and len(result) > RESULT_MAX_CHARS:
            result = result[:RESULT_MAX_CHARS] + "…"
        with self._conn() as conn:
            if result is not None:
                conn.execute(
                    "UPDATE tasks SET status=?, result=?, updated_at=? WHERE task_id=?",
                    (status, result, now, task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                    (status, now, task_id),
                )

    def list_tasks(self, limit: int = 20) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_task(self, task_id: str) -> bool:
        with self._conn() as conn:
            conn.execute("DELETE FROM steps WHERE task_id=?", (task_id,))
            result = conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
            return result.rowcount > 0

    def delete_all_tasks(self) -> int:
        with self._conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            conn.execute("DELETE FROM steps")
            conn.execute("DELETE FROM tasks")
            return count

    def delete_tasks_by_status(self, status: str) -> int:
        with self._conn() as conn:
            ids = [
                r[0] for r in conn.execute(
                    "SELECT task_id FROM tasks WHERE status=?", (status,)
                ).fetchall()
            ]
            if not ids:
                return 0
            placeholders = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM steps WHERE task_id IN ({placeholders})", ids)
            conn.execute(f"DELETE FROM tasks WHERE task_id IN ({placeholders})", ids)
            return len(ids)

    def purge_old_tasks(self, days: int = 30) -> int:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            ids = [
                r[0] for r in conn.execute(
                    "SELECT task_id FROM tasks WHERE created_at < ?", (cutoff,)
                ).fetchall()
            ]
            if not ids:
                return 0
            placeholders = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM steps WHERE task_id IN ({placeholders})", ids)
            conn.execute(f"DELETE FROM tasks WHERE task_id IN ({placeholders})", ids)
            return len(ids)

    def get_task_stats(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
            ).fetchall()
            return {r["status"]: r["cnt"] for r in rows}

    # ── Steps ──────────────────────────────────────────────────────────────────

    def create_step(self, task_id: str, step_number: int, agent: str, description: str):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO steps "
                "(task_id, step_number, agent, description, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (task_id, step_number, agent, description, now, now),
            )

    def update_step(self, task_id: str, step_number: int, status: str, output: str = None):
        now = datetime.utcnow().isoformat()
        if output and len(output) > RESULT_MAX_CHARS:
            output = output[:RESULT_MAX_CHARS] + "…"
        with self._conn() as conn:
            conn.execute(
                "UPDATE steps SET status=?, output=?, updated_at=? "
                "WHERE task_id=? AND step_number=?",
                (status, output, now, task_id, step_number),
            )

    def _get_steps(self, task_id: str, conn: sqlite3.Connection) -> list:
        rows = conn.execute(
            "SELECT * FROM steps WHERE task_id=? ORDER BY step_number", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── File registry ──────────────────────────────────────────────────────────

    def register_file(self, filename: str, filepath: str,
                      task_id: Optional[str] = None, size_bytes: int = 0):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO files "
                "(filename, filepath, size_bytes, task_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (filename, filepath, size_bytes, task_id, now),
            )

    def list_files(self, limit: int = 50) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM files ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_file_record(self, filename: str) -> bool:
        with self._conn() as conn:
            result = conn.execute(
                "DELETE FROM files WHERE filename=?", (filename,)
            )
            return result.rowcount > 0

    def purge_orphaned_files(self, output_dir: str) -> int:
        records = self.list_files(limit=1000)
        cleaned = 0
        with self._conn() as conn:
            for rec in records:
                if not Path(rec["filepath"]).exists():
                    conn.execute(
                        "DELETE FROM files WHERE filename=?", (rec["filename"],)
                    )
                    cleaned += 1
        if cleaned:
            logger.info(f"[Memory] Purged {cleaned} orphaned file records")
        return cleaned

    # ── Preferences ────────────────────────────────────────────────────────────

    def set_preference(self, key: str, value):
        now = datetime.utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO preferences (key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), now),
            )

    def get_preference(self, key: str, default=None):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM preferences WHERE key=?", (key,)
            ).fetchone()
            if not row:
                return default
            return json.loads(row["value"])

    def delete_preference(self, key: str) -> bool:
        """FIX B: Permanently remove a preference row (real DELETE, not null set)."""
        with self._conn() as conn:
            result = conn.execute(
                "DELETE FROM preferences WHERE key=?", (key,)
            )
            return result.rowcount > 0
