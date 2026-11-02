"""SQLite-backed job store.

Uses the standard-library ``sqlite3`` driver on a worker thread so the event
loop never blocks. One connection guarded by a lock is plenty for this
workload and keeps the dependency list short.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .schemas import AnalysisResult, Job, JobStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    text          TEXT NOT NULL,
    result        TEXT,
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    metadata      TEXT NOT NULL DEFAULT '{}',
    callback_url  TEXT,
    category      TEXT,
    urgency       TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    completed_at  TEXT,
    duration_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)

    # -- plumbing -----------------------------------------------------------
    def _exec(self, sql: str, params: Tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    async def _run(self, sql: str, params: Tuple = ()) -> List[sqlite3.Row]:
        return await asyncio.to_thread(self._exec, sql, params)

    async def close(self) -> None:
        await asyncio.to_thread(self._conn.close)

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            status=JobStatus(row["status"]),
            text=row["text"],
            result=AnalysisResult.model_validate_json(row["result"]) if row["result"] else None,
            error=row["error"],
            attempts=row["attempts"],
            metadata=json.loads(row["metadata"] or "{}"),
            callback_url=row["callback_url"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            duration_ms=row["duration_ms"],
        )

    # -- writes -------------------------------------------------------------
    async def create(self, text: str, metadata: Optional[Dict[str, Any]] = None,
                     callback_url: Optional[str] = None) -> Job:
        job_id, now = uuid.uuid4().hex, _now()
        await self._run(
            "INSERT INTO jobs (id, status, text, metadata, callback_url, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, JobStatus.queued.value, text, json.dumps(metadata or {}), callback_url, now, now),
        )
        return await self.get(job_id)  # type: ignore[return-value]

    async def mark_processing(self, job_id: str) -> None:
        await self._run(
            "UPDATE jobs SET status=?, attempts=attempts+1, error=NULL, updated_at=? WHERE id=?",
            (JobStatus.processing.value, _now(), job_id),
        )

    async def mark_completed(self, job_id: str, result: AnalysisResult, duration_ms: int) -> None:
        now = _now()
        await self._run(
            "UPDATE jobs SET status=?, result=?, error=NULL, category=?, urgency=?, "
            "updated_at=?, completed_at=?, duration_ms=? WHERE id=?",
            (JobStatus.completed.value, result.model_dump_json(), result.category.value,
             result.urgency.value, now, now, duration_ms, job_id),
        )

    async def mark_failed(self, job_id: str, error: str, final: bool) -> None:
        now = _now()
        status = JobStatus.failed if final else JobStatus.queued
        await self._run(
            "UPDATE jobs SET status=?, error=?, updated_at=?, completed_at=? WHERE id=?",
            (status.value, error, now, now if final else None, job_id),
        )

    async def reset_for_retry(self, job_id: str) -> None:
        await self._run(
            "UPDATE jobs SET status=?, attempts=0, error=NULL, result=NULL, category=NULL, urgency=NULL, "
            "completed_at=NULL, duration_ms=NULL, updated_at=? WHERE id=?",
            (JobStatus.queued.value, _now(), job_id),
        )

    async def delete(self, job_id: str) -> bool:
        before = await self.get(job_id)
        if not before:
            return False
        await self._run("DELETE FROM jobs WHERE id=?", (job_id,))
        return True

    async def requeue_interrupted(self) -> List[str]:
        """Jobs left 'processing' by a crash/restart go back to the queue."""
        await self._run(
            "UPDATE jobs SET status=?, updated_at=? WHERE status=?",
            (JobStatus.queued.value, _now(), JobStatus.processing.value),
        )
        rows = await self._run(
            "SELECT id FROM jobs WHERE status=? ORDER BY created_at", (JobStatus.queued.value,)
        )
        return [r["id"] for r in rows]

    # -- reads --------------------------------------------------------------
    async def get(self, job_id: str) -> Optional[Job]:
        rows = await self._run("SELECT * FROM jobs WHERE id=?", (job_id,))
        return self._row_to_job(rows[0]) if rows else None

    async def list(self, status: Optional[str] = None, category: Optional[str] = None,
                   urgency: Optional[str] = None, limit: int = 50, offset: int = 0,
                   q: Optional[str] = None) -> Tuple[List[Job], int]:
        where, params = [], []
        for col, val in (("status", status), ("category", category), ("urgency", urgency)):
            if val:
                where.append(f"{col}=?")
                params.append(val)
        if q:
            # Case-insensitive substring match on the ticket text and the generated summary.
            like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(LOWER(text) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(json_extract(result, '$.summary'), '')) LIKE ? ESCAPE '\\')")
            params += [like, like]
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = (await self._run(f"SELECT COUNT(*) AS n FROM jobs {clause}", tuple(params)))[0]["n"]
        rows = await self._run(
            f"SELECT * FROM jobs {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )
        return [self._row_to_job(r) for r in rows], total

    async def stats(self) -> Dict[str, Any]:
        def grouped(col: str, extra: str = "") -> Dict[str, int]:
            rows = self._exec(f"SELECT {col} AS k, COUNT(*) AS n FROM jobs {extra} GROUP BY {col}")
            return {r["k"]: r["n"] for r in rows if r["k"] is not None}

        def compute() -> Dict[str, Any]:
            total = self._exec("SELECT COUNT(*) AS n FROM jobs")[0]["n"]
            avg = self._exec("SELECT AVG(duration_ms) AS a FROM jobs WHERE status='completed'")[0]["a"]
            return {
                "total": total,
                "by_status": grouped("status"),
                "by_category": grouped("category"),
                "by_urgency": grouped("urgency"),
                "avg_duration_ms": round(avg, 1) if avg is not None else None,
            }

        return await asyncio.to_thread(compute)
