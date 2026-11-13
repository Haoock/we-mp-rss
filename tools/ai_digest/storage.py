"""Read-only WeRSS access and a completely separate, transactional state database."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from common import DigestError


def connect_readonly(path):
    connection = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA query_only=ON")
    return connection


class Source:
    def __init__(self, path):
        self.path = path

    def tasks(self):
        with closing(connect_readonly(self.path)) as db:
            return [dict(row) for row in db.execute(
                "SELECT id, name, status, mps_id FROM message_tasks ORDER BY name"
            )]

    def groups(self, configured):
        tasks = {task["id"]: task for task in self.tasks()}
        groups, problems = [], []
        for group in configured:
            task = tasks.get(group["task_id"])
            if not task:
                problems.append((group["task_id"], "消息任务不存在"))
                continue
            if task["status"] != 1:
                continue
            try:
                members = json.loads(task["mps_id"] or "[]")
                if not isinstance(members, list):
                    raise ValueError()
                ids = {member["id"] for member in members}
                if not ids or any(not isinstance(value, str) or not value for value in ids):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                problems.append((task["id"], "公众号列表为空或格式错误；不会回退到全部公众号"))
                continue
            groups.append(dict(group, name=task["name"], mp_ids=ids))
        return groups, problems

    def articles(self, mp_ids, since):
        if not mp_ids:
            return []
        placeholders = ",".join("?" for _ in mp_ids)
        with closing(connect_readonly(self.path)) as db:
            rows = db.execute(f"""
                SELECT a.id, a.mp_id, a.title, a.url, a.publish_time,
                       (a.has_content=1 AND length(trim(a.content))>0) AS ready,
                       coalesce(f.mp_name, a.mp_id) AS mp_name
                FROM articles a LEFT JOIN feeds f ON a.mp_id=f.id
                WHERE a.mp_id IN ({placeholders}) AND a.publish_time>=?
                  AND coalesce(a.status, 1) != 1000
                ORDER BY a.publish_time, a.id
            """, [*sorted(mp_ids), since]).fetchall()
            return [dict(row) for row in rows]

    def content(self, article_id):
        with closing(connect_readonly(self.path)) as db:
            row = db.execute("""SELECT content FROM articles
                WHERE id=? AND has_content=1 AND coalesce(status, 1)!=1000""",
                (article_id,)).fetchone()
            return row[0] if row else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS summaries (
    article_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, summary TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
    last_error TEXT, usage_json TEXT, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS slots (
    group_id TEXT NOT NULL, slot TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY(group_id, slot)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, slot TEXT NOT NULL,
    text TEXT NOT NULL, article_ids TEXT NOT NULL, target_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    next_retry REAL NOT NULL DEFAULT 0, last_error TEXT, sent_at REAL
);
CREATE TABLE IF NOT EXISTS deliveries (
    group_id TEXT NOT NULL, article_id TEXT NOT NULL, message_id INTEGER NOT NULL,
    PRIMARY KEY(group_id, article_id),
    FOREIGN KEY(message_id) REFERENCES messages(id)
);
CREATE INDEX IF NOT EXISTS messages_pending ON messages(status, next_retry);
"""


class State:
    def __init__(self, directory, readonly=False):
        self.path = directory / "state.sqlite3"
        self.db = connect_readonly(self.path) if readonly else sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, timeout=10
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA foreign_keys=ON")
        if self.meta("schema_version") != "1":
            self.close()
            raise DigestError("状态库版本不受支持")

    @staticmethod
    def initialize(directory, source, since):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "state.sqlite3"
        if path.exists():
            with closing(State(directory, readonly=True)) as state:
                state.verify_source(source)
            return False
        with closing(sqlite3.connect(str(path), timeout=10)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            with db:
                db.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", [
                    ("schema_version", "1"), ("source_db", str(Path(source).resolve())), ("since", str(since)),
                ])
        path.chmod(0o600)
        return True

    def close(self):
        self.db.close()

    def meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def verify_source(self, path):
        if self.meta("source_db") != str(Path(path).resolve()):
            raise DigestError("状态库已绑定另一个 source_db，请使用独立的 state_dir")

    def summaries(self):
        return {row["article_id"]: dict(row) for row in self.db.execute("SELECT * FROM summaries")}

    def success(self, article_id, input_hash, summary, usage, now):
        with self.db:
            self.db.execute("""INSERT INTO summaries
                (article_id, input_hash, summary, attempts, usage_json, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(article_id) DO UPDATE SET input_hash=excluded.input_hash,
                    summary=excluded.summary, attempts=summaries.attempts+1,
                    usage_json=excluded.usage_json, next_retry=0, last_error=NULL,
                    updated_at=excluded.updated_at""",
                (article_id, input_hash, summary, json.dumps(usage), now))

    def failure(self, article_id, input_hash, attempts, error, now, delay, diagnostics=None):
        with self.db:
            self.db.execute("""INSERT INTO summaries
                (article_id, input_hash, attempts, last_error, updated_at, next_retry, usage_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_id) DO UPDATE SET input_hash=excluded.input_hash,
                    attempts=excluded.attempts, last_error=excluded.last_error,
                    updated_at=excluded.updated_at, next_retry=excluded.next_retry,
                    usage_json=excluded.usage_json""",
                (article_id, input_hash, attempts, error, now, now + delay, json.dumps(diagnostics or {})))

    def delivered_or_queued(self, group_id):
        return {row[0] for row in self.db.execute(
            "SELECT article_id FROM deliveries WHERE group_id=?", (group_id,)
        )}

    def has_slot(self, group_id, slot):
        return self.db.execute("SELECT 1 FROM slots WHERE group_id=? AND slot=?",
                               (group_id, slot)).fetchone() is not None

    def enqueue(self, group_id, slot, parts, target_hash, now):
        """Persist the immutable batch AND its dedup records before any HTTP request."""
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO slots VALUES (?, ?, ?)",
                                     (group_id, slot, now))
            if not cursor.rowcount:
                return
            for text, article_ids in parts:
                cursor = self.db.execute("""INSERT INTO messages
                    (group_id, slot, text, article_ids, target_hash) VALUES (?, ?, ?, ?, ?)""",
                    (group_id, slot, text, json.dumps(article_ids), target_hash))
                self.db.executemany("INSERT INTO deliveries VALUES (?, ?, ?)",
                                    [(group_id, item, cursor.lastrowid) for item in article_ids])

    def pending(self, now, max_attempts):
        return [dict(row) for row in self.db.execute("""SELECT * FROM messages
            WHERE status='pending' AND next_retry<=? AND attempts<? ORDER BY id""",
            (now, max_attempts))]

    def sent(self, message_id, now):
        with self.db:
            self.db.execute("""UPDATE messages SET status='sent', sent_at=?,
                attempts=attempts+1, last_error=NULL WHERE id=?""", (now, message_id))

    def send_failed(self, message_id, error, now, delay):
        with self.db:
            self.db.execute("""UPDATE messages SET attempts=attempts+1,
                last_error=?, next_retry=? WHERE id=?""", (error, now + delay, message_id))

    def cancel(self, message_id):
        with self.db:
            self.db.execute("UPDATE messages SET status='cancelled' WHERE id=?", (message_id,))
            self.db.execute("DELETE FROM deliveries WHERE message_id=?", (message_id,))

    def retry(self, kind):
        with self.db:
            if kind == "summary":
                return self.db.execute("""UPDATE summaries SET attempts=0, next_retry=0,
                    last_error=NULL, usage_json=NULL WHERE summary IS NULL""").rowcount
            return self.db.execute("""UPDATE messages SET attempts=0, next_retry=0,
                last_error=NULL WHERE status='pending'""").rowcount
