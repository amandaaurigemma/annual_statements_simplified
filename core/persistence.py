from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.models import Batch, Donor, HistoryHit
from core.utils import DeliveryMethod, HistoryDbError, local_iso, utc_iso

# Database


_WRITE_LOCK = threading.Lock()


MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        CREATE TABLE batches (
          batch_id TEXT PRIMARY KEY, csv_filename TEXT NOT NULL, csv_sha256 TEXT NOT NULL,
          template_id TEXT NOT NULL, delivery TEXT NOT NULL, statement_year INTEGER NOT NULL,
          created_at_utc TEXT NOT NULL, created_at_local TEXT NOT NULL, status TEXT NOT NULL
        );
        CREATE TABLE statement_history (
          id INTEGER PRIMARY KEY,
          constituent_id TEXT NOT NULL, batch_id TEXT NOT NULL REFERENCES batches,
          csv_filename TEXT NOT NULL, template_id TEXT NOT NULL, delivery TEXT NOT NULL,
          generated_at_utc TEXT NOT NULL, generated_at_local TEXT NOT NULL,
          status TEXT NOT NULL,
          UNIQUE (constituent_id, batch_id)
        );
        CREATE INDEX ix_hist_cid ON statement_history(constituent_id);
        CREATE TABLE email_queue (
          id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, row_index INTEGER NOT NULL,
          constituent_id TEXT NOT NULL, to_address TEXT NOT NULL,
          from_on_behalf TEXT NOT NULL DEFAULT '', subject TEXT NOT NULL,
          body_html TEXT NOT NULL, attachment_path TEXT NOT NULL, attachment_name TEXT NOT NULL,
          status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
          staged_at_utc TEXT NOT NULL, queued_at_utc TEXT, sent_at_utc TEXT,
          UNIQUE (batch_id, row_index)
        );
        INSERT INTO schema_version (version) VALUES (1);
        """,
    ),
    (
        2,
        """
        BEGIN;
        CREATE TABLE power_automate_exports (
          batch_id TEXT PRIMARY KEY,
          payload BLOB NOT NULL
        );
        INSERT INTO schema_version (version) VALUES (2);
        COMMIT;
        """,
    ),
]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    )
    if cur.fetchone() is None:
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return row["v"] or 0


def backup_now(db_path: Path, backups_dir: Path, keep: int) -> Path | None:
    if not db_path.exists():
        return None
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_iso().replace(":", "").replace("-", "").replace("+00:00", "Z")
    dest = backups_dir / f"history_{stamp}.db"
    src_conn = sqlite3.connect(str(db_path))
    dst_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    backups = sorted(backups_dir.glob("history_*.db"), key=lambda p: p.name)
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
    return dest


def migrate(conn: sqlite3.Connection, db_path: Path, backups_dir: Path, keep: int = 20) -> None:
    version = current_version(conn)
    pending = [(v, sql) for v, sql in MIGRATIONS if v > version]
    if not pending:
        return
    if version > 0:
        backup_now(db_path, backups_dir, keep)
    for _, sql in pending:
        with conn:
            conn.executescript(sql)


def integrity_check(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return row[0] == "ok"


class Database:
    def __init__(self, db_path: Path, backups_dir: Path, backup_keep: int = 20):
        self.db_path = Path(db_path)
        self.backups_dir = Path(backups_dir)
        self.conn = connect(self.db_path)
        migrate(self.conn, self.db_path, self.backups_dir, backup_keep)

    @contextmanager
    def write_transaction(self):
        with _WRITE_LOCK:
            try:
                with self.conn:
                    self.conn.execute("BEGIN IMMEDIATE")
                    yield self.conn
            except sqlite3.Error as exc:
                raise HistoryDbError(f"SQLite write failed: {exc}") from exc

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with _WRITE_LOCK:
            try:
                with self.conn:
                    return self.conn.execute(sql, params)
            except sqlite3.Error as exc:
                raise HistoryDbError(f"SQLite write failed: {exc}") from exc

    def executemany(self, sql: str, seq_of_params) -> None:
        with _WRITE_LOCK:
            try:
                with self.conn:
                    self.conn.executemany(sql, seq_of_params)
            except sqlite3.Error as exc:
                raise HistoryDbError(f"SQLite write failed: {exc}") from exc

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def close(self) -> None:
        self.conn.close()


# Statement history


_CHUNK_SIZE = 500


def insert_batch(db: Database, batch: Batch, status: str = "created") -> None:
    db.execute(
        """INSERT OR REPLACE INTO batches
           (batch_id, csv_filename, csv_sha256, template_id, delivery, statement_year,
            created_at_utc, created_at_local, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            batch.batch_id,
            batch.csv_filename,
            batch.csv_sha256,
            batch.template_id,
            batch.delivery.value,
            batch.statement_year,
            batch.created_at_utc,
            local_iso(),
            status,
        ),
    )


def update_batch_status(db: Database, batch_id: str, status: str) -> None:
    db.execute("UPDATE batches SET status = ? WHERE batch_id = ?", (status, batch_id))


def record_generated(
    db: Database, batch_id: str, donor: Donor, template_id: str, delivery: DeliveryMethod
) -> None:
    db.execute(
        """INSERT OR REPLACE INTO statement_history
           (constituent_id, batch_id, csv_filename, template_id, delivery,
            generated_at_utc, generated_at_local, status)
           VALUES (?, ?, (SELECT csv_filename FROM batches WHERE batch_id = ?), ?, ?, ?, ?, 'generated')""",
        (
            donor.constituent_id,
            batch_id,
            batch_id,
            template_id,
            delivery.value,
            utc_iso(),
            local_iso(),
        ),
    )


def update_history_status(db: Database, batch_id: str, constituent_id: str, status: str) -> None:
    db.execute(
        "UPDATE statement_history SET status = ? WHERE batch_id = ? AND constituent_id = ?",
        (status, batch_id, constituent_id),
    )


def lookup_duplicates(db: Database, constituent_ids: list[str]) -> dict[str, list[HistoryHit]]:
    unique_cids = [c for c in dict.fromkeys(constituent_ids) if c]
    if not unique_cids:
        return {}

    hits: dict[str, list[HistoryHit]] = {}
    for start in range(0, len(unique_cids), _CHUNK_SIZE):
        chunk = unique_cids[start : start + _CHUNK_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        rows = db.query(
            f"""SELECT constituent_id, csv_filename, generated_at_local, template_id, delivery, status
                FROM statement_history
                WHERE constituent_id IN ({placeholders})
                ORDER BY generated_at_utc DESC""",
            tuple(chunk),
        )
        for row in rows:
            hits.setdefault(row["constituent_id"], []).append(
                HistoryHit(
                    csv_filename=row["csv_filename"],
                    generated_at_local=row["generated_at_local"],
                    template_id=row["template_id"],
                    delivery=row["delivery"],
                    status=row["status"],
                )
            )
    return hits


# Audit trail


def write_event(
    audit_path: Path,
    event: str,
    batch_id: str,
    row_indexes: list[int] | None = None,
    detail: Any = None,
) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts_utc": utc_iso(),
        "ts_local": local_iso(),
        "event": event,
        "batch_id": batch_id,
        "row_indexes": row_indexes,
        "detail": detail,
    }
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def read_events(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    events = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events
