"""Durable discovery and delivery state. Call mutations inside a transaction."""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def worker_lock(path: Path):
    """OS-held lock; released automatically on process exit, including crashes."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_name(path.name + ".lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if not handle.tell():
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError("Another worker is using this database") from None
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("Another worker is using this database") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA secure_delete=ON")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported database schema; restore compatible state")
            if version == 0:
                # Do not silently adopt or overwrite an unrelated database.
                if self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise ValueError("Unrecognized database schema")
                self.db.executescript("""
                    BEGIN;
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE posts (
                        id TEXT PRIMARY KEY,
                        created REAL NOT NULL,
                        status TEXT NOT NULL CHECK(status IN
                          ('suppressed','skipped','pending','sent','expired','failed')),
                        reason TEXT NOT NULL,
                        payload TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt REAL NOT NULL DEFAULT 0,
                        last_error TEXT,
                        message_id TEXT,
                        sent_at REAL,
                        check_at REAL NOT NULL DEFAULT 0,
                        cleanup INTEGER NOT NULL DEFAULT 0,
                        cleanup_attempts INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE INDEX pending_due ON posts(status, next_attempt);
                    CREATE INDEX cleanup_due ON posts(message_id, check_at);
                    PRAGMA user_version=1;
                    COMMIT;
                """)
        except Exception:
            self.db.close()
            raise

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE meta.value<>excluded.value",
            (key, json.dumps(value)),
        )

    def bind(self, subreddit, destination):
        identity = [subreddit.casefold(), destination]
        with self.db:
            previous = self.get("identity")
            if previous is not None and previous != identity:
                raise ValueError(
                    "Database belongs to a different subreddit or webhook; use its original configuration or a new STATE_DB_PATH"
                )
            self.set("identity", identity)

    def observe(self, post, status, reason):
        self.db.execute(
            "INSERT OR IGNORE INTO posts(id,created,status,reason,payload) VALUES (?,?,?,?,?)",
            (
                post.id,
                post.created,
                status,
                reason,
                json.dumps(post.to_dict()) if status == "pending" else None,
            ),
        )

    def pending(self, now, limit=20, include_retries=True):
        return self.db.execute(
            "SELECT * FROM posts WHERE status='pending' AND next_attempt<=? AND (attempts=0 OR ?) ORDER BY created,id LIMIT ?",
            (now, include_retries, limit),
        ).fetchall()

    def expire(self, now, max_age):
        self.db.execute(
            "UPDATE posts SET status='expired',reason='too_old',payload=NULL WHERE status='pending' AND created<=?",
            (now - max_age,),
        )

    def sent(self, post_id, message_id, now):
        self.db.execute(
            "UPDATE posts SET status='sent',payload=NULL,message_id=?,sent_at=?,check_at=?,last_error=NULL WHERE id=?",
            (message_id, now, now + 900, post_id),
        )

    def terminal(self, post_id, status, reason):
        self.db.execute(
            "UPDATE posts SET status=?,reason=?,payload=NULL,last_error=NULL WHERE id=?",
            (status, reason, post_id),
        )

    def retry(self, post_id, when, error):
        self.db.execute(
            "UPDATE posts SET attempts=attempts+1,next_attempt=?,last_error=? WHERE id=?",
            (when, str(error), post_id),
        )

    def cleanup_rows(self, now, limit=20):
        return self.db.execute(
            "SELECT * FROM posts WHERE message_id IS NOT NULL AND check_at<=? ORDER BY check_at LIMIT ?",
            (now, limit),
        ).fetchall()

    def summary(self):
        return dict(self.db.execute("SELECT status,COUNT(*) FROM posts GROUP BY status").fetchall())

    def close(self):
        self.db.close()
