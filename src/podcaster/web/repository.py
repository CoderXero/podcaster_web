"""Small explicit SQLite repository; artifacts and manifests remain canonical."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        if not self.path.is_absolute():
            self.path = (Path.cwd() / self.path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
              job_id TEXT PRIMARY KEY, batch_id TEXT, source_root_id TEXT NOT NULL,
              relative_path TEXT NOT NULL, source_file_id TEXT NOT NULL,
              source_sha256 TEXT NOT NULL, local_episode_id TEXT NOT NULL,
              slug TEXT NOT NULL, manifest_path TEXT NOT NULL, requested_by TEXT NOT NULL,
              config_snapshot_hash TEXT NOT NULL, status TEXT NOT NULL,
              pipeline_stage TEXT NOT NULL, run_until TEXT NOT NULL,
              source_disposition TEXT NOT NULL, force INTEGER NOT NULL DEFAULT 0,
              title TEXT, current_operation TEXT, progress_completed INTEGER NOT NULL DEFAULT 0,
              progress_total INTEGER NOT NULL DEFAULT 1, safe_to_retry INTEGER NOT NULL DEFAULT 0,
              requires_reconciliation INTEGER NOT NULL DEFAULT 0, error_code TEXT,
              error_message TEXT, revision INTEGER NOT NULL DEFAULT 0,
              manifest_json TEXT, script_json TEXT, remote_json TEXT,
              cancel_requested INTEGER NOT NULL DEFAULT 0, lease_until TEXT,
              worker_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status, updated_at);
            CREATE INDEX IF NOT EXISTS jobs_batch_idx ON jobs(batch_id);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
              event_type TEXT NOT NULL, stage TEXT, message TEXT NOT NULL,
              payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
              FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS events_job_idx ON events(job_id, id);
            CREATE TABLE IF NOT EXISTS scans (
              scan_id TEXT PRIMARY KEY, source_root_id TEXT NOT NULL,
              result_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)").fetchall()}
            if "voice_ids_json" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN voice_ids_json TEXT")

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        for key in ("manifest_json", "script_json", "remote_json", "voice_ids_json"):
            if data.get(key):
                data[key[:-5] if key.endswith("_json") else key] = json.loads(data[key])
            data.pop(key, None)
        for key in ("force", "safe_to_retry", "requires_reconciliation", "cancel_requested"):
            if key in data:
                data[key] = bool(data[key])
        return data

    def create_job(self, data: dict[str, Any]) -> dict[str, Any]:
        columns = list(data)
        values = [json.dumps(data[c], separators=(",", ":")) if c in {"manifest_json", "script_json", "remote_json", "voice_ids_json"} and data[c] is not None else data[c] for c in columns]
        with self._lock, self._connect() as db:
            try:
                db.execute(f"INSERT INTO jobs ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
            except sqlite3.IntegrityError as exc:
                raise ValueError("duplicate job for source and configuration") from exc
        result = self.get_job(data["job_id"])
        assert result is not None
        return result

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            return self._decode(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def list_jobs(self, *, status: str | None = None, batch_id: str | None = None,
                  limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        clauses, args = [], []
        if status:
            clauses.append("status=?"); args.append(status)
        if batch_id:
            clauses.append("batch_id=?"); args.append(batch_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM jobs{where}", args).fetchone()[0]
            rows = db.execute(f"SELECT * FROM jobs{where} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            return [self._decode(row) for row in rows], total

    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        if not changes:
            result = self.get_job(job_id)
            if not result:
                raise KeyError(job_id)
            return result
        changes["updated_at"] = now_iso()
        columns, values = [], []
        for key, value in changes.items():
            columns.append(f"{key}=?")
            values.append(json.dumps(value, separators=(",", ":")) if key in {"manifest_json", "script_json", "remote_json", "voice_ids_json"} and value is not None else value)
        values.append(job_id)
        with self._lock, self._connect() as db:
            cursor = db.execute(f"UPDATE jobs SET {','.join(columns)} WHERE job_id=?", values)
            if cursor.rowcount != 1:
                raise KeyError(job_id)
        return self.get_job(job_id)

    def delete_queued_job(self, job_id: str) -> bool:
        """Delete only while the worker has not claimed the queued job."""
        with self._lock, self._connect() as db:
            cursor = db.execute("DELETE FROM jobs WHERE job_id=? AND status='queued'", (job_id,))
            return cursor.rowcount == 1

    def cancel_queued_job(self, job_id: str) -> bool:
        """Cancel atomically before a worker can claim the queued job."""
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET status='cancelled', pipeline_stage='CANCELLED', current_operation='Cancelled', "
                "safe_to_retry=1, cancel_requested=1, updated_at=? WHERE job_id=? AND status='queued'",
                (now_iso(), job_id),
            )
            return cursor.rowcount == 1

    def append_event(self, job_id: str, event_type: str, message: str, *, stage: str | None = None,
                     payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            cursor = db.execute("INSERT INTO events(job_id,event_type,stage,message,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                                (job_id, event_type, stage, message[:1000], json.dumps(payload or {}, separators=(",", ":")), now_iso()))
            row = db.execute("SELECT * FROM events WHERE id=?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def events(self, job_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT ?", (job_id, after, limit)).fetchall()
            result = []
            for row in rows:
                item = dict(row); item["payload"] = json.loads(item.pop("payload_json")); result.append(item)
            return result

    def save_scan(self, scan_id: str, source_root_id: str, result: list[dict[str, Any]]) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO scans(scan_id,source_root_id,result_json,created_at) VALUES(?,?,?,?)",
                       (scan_id, source_root_id, json.dumps(result, separators=(",", ":")), now_iso()))

    def get_scan(self, scan_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM scans WHERE scan_id=?", (scan_id,)).fetchone()
            if not row: return None
            return {"scan_id": row["scan_id"], "source_root_id": row["source_root_id"], "files": json.loads(row["result_json"]), "created_at": row["created_at"]}

    def claim_next(self, worker_id: str, lease_seconds: int = 300) -> dict[str, Any] | None:
        # SQLite's write transaction makes claim-and-update atomic for one worker.
        from datetime import timedelta
        lease = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current_time = now_iso()
            row = db.execute("SELECT * FROM jobs WHERE status='queued' OR (status='running' AND (lease_until IS NULL OR lease_until < ?)) ORDER BY created_at LIMIT 1", (current_time,)).fetchone()
            if not row: return None
            job_id = row["job_id"]
            if row["status"] == "running":
                db.execute("INSERT INTO events(job_id,event_type,stage,message,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                           (job_id, "worker_lost", row["pipeline_stage"], "Previous worker lease expired; resuming from last safe state", "{}", current_time))
            db.execute("UPDATE jobs SET status='running', worker_id=?, lease_until=?, current_operation='Starting', updated_at=? WHERE job_id=?",
                       (worker_id, lease, current_time, job_id))
            return self._decode(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def release_lease(self, job_id: str) -> None:
        self.update_job(job_id, lease_until=None, worker_id=None)
