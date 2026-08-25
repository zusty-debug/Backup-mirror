from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any

from .models import (
    Project,
    ProjectCounters,
    ProjectSettings,
    ProjectStatus,
    ScanMode,
    TransferStatus,
    utcnow,
)


class Database:
    """Small transactional SQLite repository. All mutations are committed atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 10000")

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                yield self.connection
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                raise

    def initialize(self) -> None:
        with self._lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_user_id INTEGER PRIMARY KEY,
                    role TEXT NOT NULL DEFAULT 'OWNER',
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS telegram_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL REFERENCES users(telegram_user_id) ON DELETE CASCADE,
                    label TEXT NOT NULL,
                    phone_hint TEXT,
                    session_encrypted BLOB,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner_id, label)
                );

                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    owner_id INTEGER NOT NULL REFERENCES users(telegram_user_id) ON DELETE CASCADE,
                    profile_id INTEGER NOT NULL REFERENCES telegram_profiles(id) ON DELETE RESTRICT,
                    name TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    destination_ref TEXT NOT NULL,
                    source_chat_id INTEGER,
                    destination_chat_id INTEGER,
                    source_name TEXT,
                    destination_name TEXT,
                    scan_mode TEXT NOT NULL,
                    start_message_id INTEGER,
                    start_date TEXT,
                    end_date TEXT,
                    status TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    checkpoint_message_id INTEGER,
                    status_message_chat_id INTEGER,
                    status_message_id INTEGER,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);

                CREATE TABLE IF NOT EXISTS project_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    result TEXT,
                    summary_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS transfer_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    source_chat_id INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    destination_chat_id INTEGER,
                    destination_message_id INTEGER,
                    media_type TEXT,
                    file_name TEXT,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    checksum_sha256 TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, source_chat_id, source_message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_transfer_project_status
                    ON transfer_items(project_id, status, source_message_id);

                CREATE TABLE IF NOT EXISTS project_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    level TEXT NOT NULL,
                    event TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self.connection.commit()

    def ensure_user(self, telegram_user_id: int) -> None:
        now = utcnow()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO users(telegram_user_id, role, created_at, last_seen_at)
                VALUES (?, 'OWNER', ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET last_seen_at = excluded.last_seen_at
                """,
                (telegram_user_id, now, now),
            )

    def profile_for_owner(self, owner_id: int, label: str = "default") -> sqlite3.Row | None:
        with self._lock:
            return self.connection.execute(
                "SELECT * FROM telegram_profiles WHERE owner_id = ? AND label = ?", (owner_id, label)
            ).fetchone()

    def ensure_profile(self, owner_id: int, label: str = "default") -> int:
        existing = self.profile_for_owner(owner_id, label)
        if existing:
            return int(existing["id"])
        now = utcnow()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO telegram_profiles(owner_id, label, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (owner_id, label, now, now),
            )
            return int(cursor.lastrowid)

    def save_profile_session(self, profile_id: int, encrypted_session: bytes, phone_hint: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE telegram_profiles
                SET session_encrypted = ?, phone_hint = ?, updated_at = ?
                WHERE id = ?
                """,
                (encrypted_session, phone_hint, utcnow(), profile_id),
            )

    def profile_by_id(self, profile_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self.connection.execute("SELECT * FROM telegram_profiles WHERE id = ?", (profile_id,)).fetchone()

    def create_project(self, project: Project) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO projects(
                  id, owner_id, profile_id, name, source_ref, destination_ref,
                  source_chat_id, destination_chat_id, source_name, destination_name,
                  scan_mode, start_message_id, start_date, end_date, status, settings_json,
                  checkpoint_message_id, status_message_chat_id, status_message_id,
                  last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project.id,
                    project.owner_id,
                    project.profile_id,
                    project.name,
                    project.source_ref,
                    project.destination_ref,
                    project.source_chat_id,
                    project.destination_chat_id,
                    project.source_name,
                    project.destination_name,
                    project.scan_mode.value,
                    project.start_message_id,
                    project.start_date,
                    project.end_date,
                    project.status.value,
                    project.settings.to_json(),
                    project.checkpoint_message_id,
                    project.status_message_chat_id,
                    project.status_message_id,
                    project.last_error,
                    project.created_at,
                    project.updated_at,
                ),
            )

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            owner_id=int(row["owner_id"]),
            profile_id=int(row["profile_id"]),
            name=row["name"],
            source_ref=row["source_ref"],
            destination_ref=row["destination_ref"],
            source_chat_id=row["source_chat_id"],
            destination_chat_id=row["destination_chat_id"],
            source_name=row["source_name"],
            destination_name=row["destination_name"],
            scan_mode=ScanMode(row["scan_mode"]),
            start_message_id=row["start_message_id"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            status=ProjectStatus(row["status"]),
            settings=ProjectSettings.from_json(row["settings_json"]),
            checkpoint_message_id=row["checkpoint_message_id"],
            status_message_chat_id=row["status_message_chat_id"],
            status_message_id=row["status_message_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_error=row["last_error"],
        )

    def get_project(self, project_id: str, owner_id: int | None = None) -> Project | None:
        sql = "SELECT * FROM projects WHERE id = ?"
        params: tuple[Any, ...] = (project_id,)
        if owner_id is not None:
            sql += " AND owner_id = ?"
            params = (project_id, owner_id)
        with self._lock:
            row = self.connection.execute(sql, params).fetchone()
        return self._project_from_row(row) if row else None

    def list_projects(self, owner_id: int) -> list[Project]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM projects WHERE owner_id = ? ORDER BY created_at DESC", (owner_id,)
            ).fetchall()
        return [self._project_from_row(row) for row in rows]

    def count_projects(self, owner_id: int) -> int:
        with self._lock:
            return int(self.connection.execute("SELECT COUNT(*) FROM projects WHERE owner_id = ?", (owner_id,)).fetchone()[0])

    def projects_to_resume(self) -> list[Project]:
        statuses = (
            ProjectStatus.RUNNING.value,
            ProjectStatus.WAITING_RATE_LIMIT.value,
        )
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM projects WHERE status IN (?, ?)", statuses
            ).fetchall()
        return [self._project_from_row(row) for row in rows]

    def update_project_resolution(
        self, project_id: str, source_chat_id: int, source_name: str, destination_chat_id: int, destination_name: str
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE projects
                SET source_chat_id = ?, source_name = ?, destination_chat_id = ?, destination_name = ?,
                    status = ?, updated_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (
                    source_chat_id,
                    source_name,
                    destination_chat_id,
                    destination_name,
                    ProjectStatus.READY.value,
                    utcnow(),
                    project_id,
                ),
            )

    def update_project_status(self, project_id: str, status: ProjectStatus, error: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE projects SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status.value, error, utcnow(), project_id),
            )

    def update_project_settings(self, project_id: str, settings: ProjectSettings) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE projects SET settings_json = ?, updated_at = ? WHERE id = ?",
                (settings.to_json(), utcnow(), project_id),
            )

    def update_project_checkpoint(self, project_id: str, message_id: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE projects SET checkpoint_message_id = ?, updated_at = ? WHERE id = ?",
                (message_id, utcnow(), project_id),
            )

    def set_status_message(self, project_id: str, chat_id: int, message_id: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE projects SET status_message_chat_id = ?, status_message_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (chat_id, message_id, utcnow(), project_id),
            )

    def delete_project(self, project_id: str, owner_id: int) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM projects WHERE id = ? AND owner_id = ?", (project_id, owner_id))
            return cursor.rowcount > 0

    def request_pause(self, project_id: str) -> None:
        self.update_project_status(project_id, ProjectStatus.PAUSE_REQUESTED)

    def request_stop(self, project_id: str) -> None:
        self.update_project_status(project_id, ProjectStatus.STOP_REQUESTED)

    def open_run(self, project_id: str) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO project_runs(project_id, started_at, summary_json) VALUES (?, ?, '{}')",
                (project_id, utcnow()),
            )
            return int(cursor.lastrowid)

    def close_run(self, run_id: int, result: str, counters: ProjectCounters) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE project_runs SET ended_at = ?, result = ?, summary_json = ? WHERE id = ?",
                (utcnow(), result, json.dumps(asdict(counters), sort_keys=True), run_id),
            )

    def log_event(self, project_id: str, level: str, event: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO project_events(project_id, level, event, created_at) VALUES (?, ?, ?, ?)",
                (project_id, level, event[:2000], utcnow()),
            )

    def transfer_completed(self, project_id: str, source_chat_id: int, source_message_id: int) -> bool:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT status FROM transfer_items
                WHERE project_id = ? AND source_chat_id = ? AND source_message_id = ?
                """,
                (project_id, source_chat_id, source_message_id),
            ).fetchone()
        return bool(row and row["status"] == TransferStatus.COMPLETED.value)

    def begin_transfer(
        self,
        *,
        project_id: str,
        source_chat_id: int,
        source_message_id: int,
        media_type: str,
        file_name: str,
        file_size: int,
        status: TransferStatus,
    ) -> None:
        now = utcnow()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO transfer_items(
                  project_id, source_chat_id, source_message_id, media_type, file_name, file_size,
                  status, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(project_id, source_chat_id, source_message_id) DO UPDATE SET
                  media_type = excluded.media_type,
                  file_name = excluded.file_name,
                  file_size = excluded.file_size,
                  status = excluded.status,
                  attempts = transfer_items.attempts + 1,
                  error = NULL,
                  updated_at = excluded.updated_at
                """,
                (
                    project_id,
                    source_chat_id,
                    source_message_id,
                    media_type,
                    file_name,
                    file_size,
                    status.value,
                    now,
                    now,
                ),
            )

    def complete_transfer(
        self,
        *,
        project_id: str,
        source_chat_id: int,
        source_message_id: int,
        destination_chat_id: int,
        destination_message_id: int,
        checksum_sha256: str | None,
    ) -> None:
        now = utcnow()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE transfer_items
                SET destination_chat_id = ?, destination_message_id = ?, checksum_sha256 = ?,
                    status = ?, error = NULL, completed_at = ?, updated_at = ?
                WHERE project_id = ? AND source_chat_id = ? AND source_message_id = ?
                """,
                (
                    destination_chat_id,
                    destination_message_id,
                    checksum_sha256,
                    TransferStatus.COMPLETED.value,
                    now,
                    now,
                    project_id,
                    source_chat_id,
                    source_message_id,
                ),
            )

    def mark_transfer(self, project_id: str, source_chat_id: int, source_message_id: int, status: TransferStatus, error: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE transfer_items SET status = ?, error = ?, updated_at = ?
                WHERE project_id = ? AND source_chat_id = ? AND source_message_id = ?
                """,
                (status.value, (error or "")[:2000] or None, utcnow(), project_id, source_chat_id, source_message_id),
            )

    def counters(self, project_id: str) -> ProjectCounters:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT
                  COUNT(*) FILTER (WHERE status != ?) AS eligible,
                  COUNT(*) FILTER (WHERE status = ?) AS completed,
                  COUNT(*) FILTER (WHERE status = ?) AS skipped,
                  COUNT(*) FILTER (WHERE status = ?) AS failed,
                  COALESCE(SUM(file_size) FILTER (WHERE status = ?), 0) AS bytes_transferred
                FROM transfer_items WHERE project_id = ?
                """,
                (
                    TransferStatus.SKIPPED.value,
                    TransferStatus.COMPLETED.value,
                    TransferStatus.SKIPPED.value,
                    TransferStatus.FAILED.value,
                    TransferStatus.COMPLETED.value,
                    project_id,
                ),
            ).fetchone()
        return ProjectCounters.from_row(dict(row))

    def failed_items(self, project_id: str, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            return self.connection.execute(
                """
                SELECT source_message_id, file_name, error, attempts, updated_at
                FROM transfer_items WHERE project_id = ? AND status = ?
                ORDER BY source_message_id LIMIT ?
                """,
                (project_id, TransferStatus.FAILED.value, limit),
            ).fetchall()

    def cleanup_incomplete_items(self) -> int:
        """An abrupt process stop leaves these safe to retry on the next run."""
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE transfer_items SET status = ?, updated_at = ?
                WHERE status IN (?, ?, ?)
                """,
                (
                    TransferStatus.RETRY_WAIT.value,
                    utcnow(),
                    TransferStatus.PENDING.value,
                    TransferStatus.DOWNLOADING.value,
                    TransferStatus.UPLOADING.value,
                ),
            )
            return cursor.rowcount

    def retryable_source_message_ids(self, project_id: str, limit: int = 500) -> list[int]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT source_message_id FROM transfer_items
                WHERE project_id = ? AND status IN (?, ?)
                ORDER BY source_message_id LIMIT ?
                """,
                (project_id, TransferStatus.FAILED.value, TransferStatus.RETRY_WAIT.value, limit),
            ).fetchall()
        return [int(row["source_message_id"]) for row in rows]

    def project_status_summary(self, owner_id: int) -> dict[str, int]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT status, COUNT(*) AS total FROM projects WHERE owner_id = ? GROUP BY status",
                (owner_id,),
            ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    def worker_profile_summary(self, owner_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT id, label, phone_hint, session_encrypted, created_at, updated_at
                FROM telegram_profiles WHERE owner_id = ? AND label = 'default'
                """,
                (owner_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "label": str(row["label"]),
            "phone_hint": row["phone_hint"],
            "connected": bool(row["session_encrypted"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def observed_worker_restrictions(self, owner_id: int) -> list[str]:
        """Return recent project errors that indicate an observed Telegram restriction."""
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT last_error FROM projects
                WHERE owner_id = ? AND last_error IS NOT NULL
                  AND (last_error LIKE '%PeerFlood%' OR last_error LIKE '%UserRestricted%'
                       OR last_error LIKE '%FloodWait%')
                ORDER BY updated_at DESC LIMIT 5
                """,
                (owner_id,),
            ).fetchall()
        return [str(row["last_error"]) for row in rows]
