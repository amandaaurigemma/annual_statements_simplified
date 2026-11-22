from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from core.persistence import Database
from core.utils import EmailQueueStatus, utc_iso

# Email queue


def stage_records(db: Database, batch_id: str, items: list[dict]) -> None:
    now = utc_iso()
    rows = [
        (
            batch_id,
            item["row_index"],
            item["constituent_id"],
            item["to_address"],
            item.get("from_on_behalf", ""),
            item["subject"],
            item["body_html"],
            item["attachment_path"],
            item["attachment_name"],
            EmailQueueStatus.STAGED.value,
            now,
            batch_id,
        )
        for item in items
    ]
    db.executemany(
        """INSERT INTO email_queue
           (batch_id, row_index, constituent_id, to_address, from_on_behalf, subject,
            body_html, attachment_path, attachment_name, status, staged_at_utc)
           SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
           WHERE NOT EXISTS (SELECT 1 FROM power_automate_exports WHERE batch_id = ?)
           ON CONFLICT (batch_id, row_index) DO UPDATE SET
               constituent_id = excluded.constituent_id,
               to_address = excluded.to_address,
               from_on_behalf = excluded.from_on_behalf,
               subject = excluded.subject,
               body_html = excluded.body_html,
               attachment_path = excluded.attachment_path,
               attachment_name = excluded.attachment_name,
               staged_at_utc = excluded.staged_at_utc
           WHERE email_queue.status = excluded.status""",
        rows,
    )


def confirm_send_now(db: Database, batch_id: str) -> int:
    cur = db.execute(
        "UPDATE email_queue SET status = ?, queued_at_utc = ? WHERE batch_id = ? AND status = ?",
        (EmailQueueStatus.QUEUED.value, utc_iso(), batch_id, EmailQueueStatus.STAGED.value),
    )
    return cur.rowcount


def claim_next(db: Database, batch_id: str | None = None) -> dict | None:
    row = db.query_one(
        "SELECT id FROM email_queue WHERE status = ? AND (? IS NULL OR batch_id = ?) "
        "ORDER BY id LIMIT 1",
        (EmailQueueStatus.QUEUED.value, batch_id, batch_id),
    )
    if row is None:
        return None
    cur = db.execute(
        "UPDATE email_queue SET status = ? WHERE id = ? AND status = ?",
        (EmailQueueStatus.SENDING.value, row["id"], EmailQueueStatus.QUEUED.value),
    )
    if cur.rowcount == 0:
        return None
    claimed = db.query_one("SELECT * FROM email_queue WHERE id = ?", (row["id"],))
    return dict(claimed) if claimed else None


def mark_sent(db: Database, queue_id: int) -> None:
    db.execute(
        "UPDATE email_queue SET status = ?, sent_at_utc = ? WHERE id = ?",
        (EmailQueueStatus.SENT.value, utc_iso(), queue_id),
    )


def mark_retry_or_failed(db: Database, queue_id: int, error: str, max_attempts: int) -> str:
    row = db.query_one("SELECT attempts FROM email_queue WHERE id = ?", (queue_id,))
    attempts = (row["attempts"] if row else 0) + 1
    if attempts < max_attempts:
        db.execute(
            "UPDATE email_queue SET status = ?, attempts = ?, last_error = ? WHERE id = ?",
            (EmailQueueStatus.QUEUED.value, attempts, error, queue_id),
        )
        return EmailQueueStatus.QUEUED.value
    db.execute(
        "UPDATE email_queue SET status = ?, attempts = ?, last_error = ? WHERE id = ?",
        (EmailQueueStatus.FAILED.value, attempts, error, queue_id),
    )
    return EmailQueueStatus.FAILED.value


def cancel(db: Database, ids: list[int]) -> None:
    placeholders = ",".join("?" for _ in ids)
    db.execute(
        f"UPDATE email_queue SET status = ? WHERE id IN ({placeholders}) AND status IN (?, ?)",
        (
            EmailQueueStatus.CANCELLED.value,
            *ids,
            EmailQueueStatus.STAGED.value,
            EmailQueueStatus.QUEUED.value,
        ),
    )


def retry_failed(db: Database, ids: list[int]) -> None:
    placeholders = ",".join("?" for _ in ids)
    db.execute(
        f"UPDATE email_queue SET status = ?, attempts = 0 WHERE id IN ({placeholders}) AND status = ?",
        (EmailQueueStatus.QUEUED.value, *ids, EmailQueueStatus.FAILED.value),
    )


def requeue_unknown(db: Database, ids: list[int]) -> None:
    placeholders = ",".join("?" for _ in ids)
    db.execute(
        f"UPDATE email_queue SET status = ? WHERE id IN ({placeholders}) AND status = ?",
        (EmailQueueStatus.QUEUED.value, *ids, EmailQueueStatus.UNKNOWN.value),
    )


def recover_stuck_sending(db: Database) -> int:
    cur = db.execute(
        "UPDATE email_queue SET status = ? WHERE status = ?",
        (EmailQueueStatus.UNKNOWN.value, EmailQueueStatus.SENDING.value),
    )
    return cur.rowcount


def queue_counts(db: Database, batch_id: str) -> dict[str, int]:
    rows = db.query(
        "SELECT status, COUNT(*) AS n FROM email_queue WHERE batch_id = ? GROUP BY status",
        (batch_id,),
    )
    return {row["status"]: row["n"] for row in rows}


def list_queue(db: Database, batch_id: str) -> list[dict]:
    rows = db.query("SELECT * FROM email_queue WHERE batch_id = ? ORDER BY id", (batch_id,))
    return [dict(r) for r in rows]


# Power Automate handoff


MAX_EXPORT_BYTES = 10 * 1024 * 1024


def get_export(db: Database, batch_id: str) -> bytes | None:
    row = db.query_one("SELECT payload FROM power_automate_exports WHERE batch_id = ?", (batch_id,))
    return row["payload"] if row else None


def prepare_export(db: Database, batch_id: str) -> bytes:
    with db.write_transaction() as conn:
        existing = conn.execute(
            "SELECT payload FROM power_automate_exports WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if existing:
            return existing["payload"]

        rows = conn.execute(
            "SELECT * FROM email_queue WHERE batch_id = ? ORDER BY id", (batch_id,)
        ).fetchall()
        staged = [row for row in rows if row["status"] == EmailQueueStatus.STAGED.value]
        if not staged or any(
            row["status"] not in (EmailQueueStatus.STAGED.value, EmailQueueStatus.CANCELLED.value)
            for row in rows
        ):
            raise ValueError("Use a batch containing only staged or cancelled emails.")

        jobs = []
        size = 0
        for row in staged:

            with Path(row["attachment_path"]).open("rb") as attachment:
                pdf = attachment.read(MAX_EXPORT_BYTES - size + 1)
            if not pdf.startswith(b"%PDF-"):
                raise ValueError(f"Queue item {row['id']} has an invalid PDF attachment.")
            job = {
                "job_id": str(
                    uuid5(NAMESPACE_URL, f"annual-statements/{batch_id}/{row['row_index']}")
                ),
                "queue_id": row["id"],
                "to": row["to_address"],
                "subject": row["subject"],
                "body_html": row["body_html"],
                "attachment_name": row["attachment_name"],
                "attachment_base64": base64.b64encode(pdf).decode("ascii"),
            }
            size += len(json.dumps(job).encode("utf-8"))
            if size > MAX_EXPORT_BYTES:
                raise ValueError("The Power Automate package exceeds 10 MiB.")
            jobs.append(job)

        payload = json.dumps({"version": 1, "batch_id": batch_id, "jobs": jobs}).encode("utf-8")
        if len(payload) > MAX_EXPORT_BYTES:
            raise ValueError("The Power Automate package exceeds 10 MiB.")
        conn.execute(
            "INSERT INTO power_automate_exports (batch_id, payload) VALUES (?, ?)",
            (batch_id, payload),
        )
        conn.execute(
            "UPDATE email_queue SET status = ? WHERE batch_id = ? AND status = ?",
            (EmailQueueStatus.HANDOFF_PREPARED.value, batch_id, EmailQueueStatus.STAGED.value),
        )
        return payload


def record_result(db: Database, batch_id: str, queue_id: int, status: EmailQueueStatus) -> bool:
    if status not in (EmailQueueStatus.FLOW_ACCEPTED, EmailQueueStatus.FLOW_REVIEW):
        raise ValueError("Select an accepted or needs-review result.")
    result = db.execute(
        """UPDATE email_queue SET status = ?
           WHERE batch_id = ? AND id = ? AND status IN (?, ?)""",
        (
            status.value,
            batch_id,
            queue_id,
            EmailQueueStatus.HANDOFF_PREPARED.value,
            EmailQueueStatus.FLOW_REVIEW.value,
        ),
    )
    return result.rowcount == 1
