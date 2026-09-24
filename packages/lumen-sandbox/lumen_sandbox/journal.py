"""Synchronous, fsynced SQLite authority for call and workspace fences."""

import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path


class Conflict(Exception):
    pass


class Journal:
    def __init__(self, directory: Path, run_id: str, resource_id: str, generation: int):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "journal.sqlite", check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS run (run_id TEXT PRIMARY KEY, resource_id TEXT NOT NULL,
                generation INTEGER NOT NULL, fence INTEGER NOT NULL, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY, call_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
                fence INTEGER NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL,
                result TEXT, started REAL, ended REAL
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, filename TEXT NOT NULL,
                length INTEGER NOT NULL, FOREIGN KEY(execution_id) REFERENCES executions(id)
            );
        """)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO run VALUES (?, ?, ?, -1, 0)", (run_id, resource_id, generation))
            if self.db.execute("SELECT run_id, resource_id, generation FROM run").fetchall() != [(run_id, resource_id, generation)]:
                raise ValueError("journal belongs to another resource/run/generation")
            # A process may have begun before power loss; its side effects cannot be retried.
            self.db.execute("UPDATE executions SET state='indeterminate' WHERE state IN ('reserved', 'running')")

    def reserve(self, call_id: str, fingerprint: str, fence: int, revision: int, *, allow_create: bool = True) -> tuple[dict, bool]:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                highest, current = self.db.execute("SELECT fence, revision FROM run").fetchone()
                if fence < highest:
                    raise Conflict("stale fence")
                if fence > highest:
                    self.db.execute("UPDATE run SET fence=?", (fence,))
                    self.db.execute("COMMIT")
                    self.db.execute("BEGIN IMMEDIATE")
                row = self.db.execute("SELECT id, fingerprint, fence FROM executions WHERE call_id=?", (call_id,)).fetchone()
                if row:
                    if row[1] != fingerprint or row[2] != fence:
                        raise Conflict("call_id fingerprint or fence mismatch")
                    result = self.get(row[0])
                    self.db.execute("COMMIT")
                    return result, False
                if not allow_create:
                    raise Conflict("run_deadline_expired")
                if revision != current:
                    raise Conflict("workspace revision mismatch")
                if self.db.execute("SELECT 1 FROM executions WHERE state='indeterminate'").fetchone():
                    raise Conflict("workspace outcome indeterminate")
                if self.db.execute("SELECT 1 FROM executions WHERE state IN ('reserved','running')").fetchone():
                    raise Conflict("execution already active")
                eid = str(uuid.uuid4())
                self.db.execute("INSERT INTO executions(id,call_id,fingerprint,fence,revision,state) VALUES (?,?,?,?,?,'reserved')", (eid, call_id, fingerprint, fence, revision))
                result = self.get(eid)
                self.db.execute("COMMIT")
                return result, True
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def highest_fence(self) -> int:
        with self.lock:
            return self.db.execute("SELECT fence FROM run").fetchone()[0]

    def get(self, eid: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT id,call_id,fence,revision,state,result,started,ended FROM executions WHERE id=?", (eid,)).fetchone()
            if row is None:
                return None
            result = json.loads(row[5]) if row[5] else {}
            return {"id": row[0], "call_id": row[1], "fence": row[2], "workspace_revision": row[3], "state": row[4], **result}

    def start(self, eid: str, timestamp: float) -> None:
        with self.lock:
            self.db.execute("UPDATE executions SET state='running',started=? WHERE id=? AND state='reserved'", (timestamp, eid))

    def finish(self, eid: str, state: str, result: dict, timestamp: float, artifacts: list[tuple[str, str, int]]) -> None:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT revision,state FROM executions WHERE id=?", (eid,)).fetchone()
                if row and row[1] in ("reserved", "running"):
                    self.db.execute("UPDATE executions SET state=?,result=?,ended=? WHERE id=?", (state, json.dumps(result), timestamp, eid))
                    self.db.execute("UPDATE run SET revision=?", (row[0] + 1,))
                    self.db.executemany("INSERT INTO artifacts VALUES (?,?,?,?)", ((aid, eid, name, size) for aid, name, size in artifacts))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def artifact(self, aid: str) -> tuple[str, str, int] | None:
        with self.lock:
            return self.db.execute("SELECT execution_id,filename,length FROM artifacts WHERE id=?", (aid,)).fetchone()
