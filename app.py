from __future__ import annotations

import json
import shutil
import sys

import streamlit as st

from core.batch import create_batch, export_mail, generate, mutate, stage_emails
from core.config import AppConfig, load_app_config
from core.delivery import (
    cancel,
    confirm_send_now,
    get_export,
    list_queue,
    prepare_export,
    queue_counts,
    record_result,
    retry_failed,
    stage_records,
)
from core.messaging import DraftSender, EmailWorker, OutlookSender, notify
from core.models import Overrides
from core.persistence import Database, backup_now, integrity_check
from core.templates import discover_templates
from core.utils import (
    STATUS_COLOR_AMBER,
    STATUS_COLOR_GREEN,
    STATUS_COLOR_RED,
    CsvError,
    DeliveryMethod,
    EmailQueueStatus,
    RecordStatus,
    default_statement_year,
)

# Session


K_AUTHED = "authed"
K_RUN_STAGE = "run_stage"
K_BATCH_CTX = "batch_ctx"
K_SESSION_ID = "session_id"
K_ACTIVE_WORKER = "active_worker"

RUN_STAGE_KEYS = (K_RUN_STAGE, K_BATCH_CTX)


@st.cache_resource
def get_config() -> AppConfig:
    return load_app_config()


@st.cache_resource
def get_db() -> Database:
    config = get_config()
    return Database(config.history_db_path, config.backups_dir, config.history.backup_keep)


def init_session() -> None:
    if K_SESSION_ID not in st.session_state:
        import uuid

        st.session_state[K_SESSION_ID] = uuid.uuid4().hex[:8]
    if K_RUN_STAGE not in st.session_state:
        st.session_state[K_RUN_STAGE] = 1


def reset_run() -> None:
    for key in RUN_STAGE_KEYS:
        st.session_state.pop(key, None)
    st.session_state[K_RUN_STAGE] = 1


# Shared controls


STAGE_LABELS = ["Upload", "Validate", "Generate", "Review", "Deliver", "Done"]

_STATUS_COLORS = {
    "included": STATUS_COLOR_GREEN,
    "sent": STATUS_COLOR_GREEN,
    "exported": STATUS_COLOR_GREEN,
    "skipped": STATUS_COLOR_AMBER,
    "staged": STATUS_COLOR_AMBER,
    "queued": STATUS_COLOR_AMBER,
    "removed": STATUS_COLOR_RED,
    "error": STATUS_COLOR_RED,
    "failed": STATUS_COLOR_RED,
    "unknown": STATUS_COLOR_RED,
}


def status_badge(status: str) -> str:
    color = _STATUS_COLORS.get(status, "#666666")
    return f'<span style="color:{color}; font-weight:600;">{status.upper()}</span>'


def render_stepper(active_stage: int) -> None:
    cols = st.columns(len(STAGE_LABELS))
    for i, (col, label) in enumerate(zip(cols, STAGE_LABELS), start=1):
        with col:
            if i < active_stage:
                st.markdown(f"✅ **{label}**")
            elif i == active_stage:
                st.markdown(f"🔵 **{label}**")
            else:
                st.markdown(f"⚪ {label}")


# Statement run


def render_run() -> None:
    config = get_config()
    db = get_db()
    st.title("Statement Run")
    stage = st.session_state.get(K_RUN_STAGE, 1)
    render_stepper(stage)
    st.divider()

    if stage == 1:
        _stage_upload(config)
    elif stage == 2:
        _stage_validation()
    elif stage == 3:
        _stage_generation(db)
    elif stage == 4:
        _stage_review(db)
    elif stage == 5:
        _stage_deliver(db)
    elif stage == 6:
        _stage_done()


def _stage_upload(config) -> None:
    templates = discover_templates(config.templates_dir)
    template_options = {t.display_name: t.id for t in templates}

    uploaded = st.file_uploader("Donor CSV export", type=["csv"])
    delivery_label = st.radio("Delivery method", ["Mail", "Email"], horizontal=True)
    template_label = st.selectbox("Template", list(template_options.keys()))
    year = st.number_input("Statement Year", value=default_statement_year(config.timezone), step=1)

    disabled = uploaded is None
    if st.button("Validate", type="primary", disabled=disabled):
        delivery = DeliveryMethod.MAIL if delivery_label == "Mail" else DeliveryMethod.EMAIL
        try:
            with st.spinner("Reading and validating the upload..."):
                ctx = create_batch(
                    uploaded.getvalue(),
                    uploaded.name,
                    template_options[template_label],
                    delivery,
                    int(year),
                    config,
                    db=get_db(),
                )
            st.session_state[K_BATCH_CTX] = ctx
            st.session_state[K_RUN_STAGE] = 2
            st.rerun()
        except CsvError as exc:
            st.error(exc.operator_message + f" (ref: {exc.reference_code})")


def _stage_validation() -> None:
    ctx = st.session_state[K_BATCH_CTX]
    report = ctx.validation

    if report.has_blocking_errors:
        st.error("This upload cannot proceed:")
        for msg in report.blocking:
            st.write(f"- {msg}")
        if st.button("Start over"):
            reset_run()
            st.rerun()
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows", len(ctx.ingest.donors))
    c2.metric("Gift columns detected", len(ctx.ingest.gift_group_indexes))
    c3.metric("Warnings", sum(len(v) for v in report.rows.values()))
    c4.metric("Duplicate hits", len(report.duplicate_hits))

    if report.rows:
        rows_table = [
            {
                "row": ri,
                "constituent_id": ctx.records[ri].constituent_id,
                "rule": w.rule_id,
                "severity": w.severity,
                "message": w.message,
            }
            for ri, warnings in sorted(report.rows.items())
            for w in warnings
        ]
        st.dataframe(rows_table, width="stretch")
        st.download_button(
            "Download validation.csv", report.to_csv_bytes(), "validation.csv", "text/csv"
        )
    else:
        st.info("No findings -- this upload is clean.")

    col1, col2 = st.columns(2)
    if col1.button("Generate statements", type="primary"):
        st.session_state[K_RUN_STAGE] = 3
        st.rerun()
    if col2.button("Start over"):
        reset_run()
        st.rerun()


def _stage_generation(db) -> None:
    ctx = st.session_state[K_BATCH_CTX]
    progress = st.progress(0.0, text="Starting...")

    def on_progress(done, total, label):
        progress.progress(done / total, text=f"{done} / {total} — {label}")

    with st.spinner("Generating statements..."):
        generate(ctx, db=db, on_progress=on_progress)

    included = sum(1 for r in ctx.records.values() if r.status == RecordStatus.INCLUDED)
    errored = sum(1 for r in ctx.records.values() if r.status == RecordStatus.ERROR)
    c1, c2 = st.columns(2)
    c1.metric("Generated", included)
    c2.metric("Errors", errored)

    st.session_state[K_RUN_STAGE] = 4
    st.rerun()


def _stage_review(db) -> None:
    ctx = st.session_state[K_BATCH_CTX]

    included = sum(1 for r in ctx.records.values() if r.status == RecordStatus.INCLUDED)
    skipped = sum(1 for r in ctx.records.values() if r.status == RecordStatus.SKIPPED)
    removed = sum(1 for r in ctx.records.values() if r.status == RecordStatus.REMOVED)
    errored = sum(1 for r in ctx.records.values() if r.status == RecordStatus.ERROR)

    left, right = st.columns([2, 3])

    with left:
        filter_choice = st.selectbox(
            "Filter", ["All", "Warnings", "Errors", "Edited", "Skipped", "Removed"]
        )
        rows = []
        for ri in sorted(ctx.records):
            r = ctx.records[ri]
            if filter_choice == "Warnings" and ri not in ctx.validation.rows:
                continue
            if filter_choice == "Errors" and r.status != RecordStatus.ERROR:
                continue
            if filter_choice == "Edited" and not r.edited:
                continue
            if filter_choice == "Skipped" and r.status != RecordStatus.SKIPPED:
                continue
            if filter_choice == "Removed" and r.status != RecordStatus.REMOVED:
                continue
            rows.append(
                {
                    "row": ri,
                    "addressee": r.donor.addressee,
                    "cid": r.constituent_id,
                    "gifts": len(r.donor.gifts),
                    "total": str(r.donor.total_amount),
                    "status": r.status.value,
                }
            )
        st.dataframe(rows, width="stretch", height=400)
        selected_row = st.number_input(
            "Row to review", min_value=0, max_value=max(ctx.records) if ctx.records else 0, step=1
        )

    with right:
        record = ctx.records.get(int(selected_row))
        if record is not None:
            st.markdown(
                f"### {record.donor.addressee}  {status_badge(record.status.value)}",
                unsafe_allow_html=True,
            )
            st.write(
                f"CID: {record.constituent_id} · Gifts: {len(record.donor.gifts)} · Total: {record.donor.total_amount}"
            )
            for w in ctx.validation.rows.get(int(selected_row), []):
                st.warning(f"{w.rule_id}: {w.message}")
            if record.error_message:
                st.error(record.error_message)

            with st.expander("Edit statement text"):
                rendered = ctx.rendered_blocks.get(int(selected_row), {})
                intro = st.text_area(
                    "Intro", value=record.overrides.intro_text or rendered.get("intro", "")
                )
                closing = st.text_area(
                    "Closing", value=record.overrides.closing_text or rendered.get("closing", "")
                )
                if st.button("Save edits"):
                    mutate(
                        ctx,
                        int(selected_row),
                        "edit",
                        overrides=Overrides(intro_text=intro, closing_text=closing),
                        db=db,
                    )
                    st.rerun()

            b1, b2, b3 = st.columns(3)
            if record.status != RecordStatus.SKIPPED and b1.button("Skip"):
                mutate(ctx, int(selected_row), "skip", db=db)
                st.rerun()
            if record.status != RecordStatus.REMOVED and b2.button("Remove"):
                mutate(ctx, int(selected_row), "remove", db=db)
                st.rerun()
            if record.status in (RecordStatus.SKIPPED, RecordStatus.REMOVED) and b3.button(
                "Restore"
            ):
                mutate(ctx, int(selected_row), "restore", db=db)
                st.rerun()

    st.divider()
    st.caption(f"{included} included · {skipped} skipped · {removed} removed · {errored} errors")
    if st.button("Continue to Deliver", type="primary"):
        st.session_state[K_RUN_STAGE] = 5
        st.rerun()


def _stage_deliver(db) -> None:
    ctx = st.session_state[K_BATCH_CTX]

    if ctx.batch.delivery == DeliveryMethod.MAIL:
        if st.button("Generate print files", type="primary"):
            with st.spinner("Building mail merge and envelopes..."):
                outputs = export_mail(ctx, db=db)
            st.session_state["_deliver_outputs"] = outputs

        outputs = st.session_state.get("_deliver_outputs")
        if outputs:
            for name, path in outputs.items():
                st.download_button(f"Download {path.name}", path.read_bytes(), path.name)
    else:
        staged_count = sum(
            1
            for r in ctx.records.values()
            if r.status == RecordStatus.INCLUDED and r.donor.email.strip()
        )
        st.write(f"{staged_count} email(s) ready to stage for review.")
        exported = get_export(db, ctx.batch.batch_id) is not None
        if exported:
            st.info("This batch was approved for Power Automate. Open Email Queue for its handoff.")
        if st.button("Stage emails for review", type="primary", disabled=exported):
            staged = stage_emails(ctx)
            stage_records(db, ctx.batch.batch_id, staged)
            st.success(
                f"Staged {len(staged)} email(s). Open the Email Queue page to review and send."
            )

    if st.button("Finish batch"):
        st.session_state[K_RUN_STAGE] = 6
        st.rerun()


def _stage_done() -> None:
    ctx = st.session_state[K_BATCH_CTX]
    st.success(f"Batch {ctx.batch.batch_id} complete.")
    if st.button("Start new batch", type="primary"):
        reset_run()
        st.rerun()


# Email queue


def _build_sender(config):
    sender = OutlookSender()
    if config.email.send_mode == "draft":
        return DraftSender(sender), EmailQueueStatus.DRAFT_SAVED, "Save Outlook Drafts"
    return sender, EmailQueueStatus.SENT, "Send Now with Outlook"


def _render_power_automate(db, batch_id, rows, payload) -> None:
    if payload is None:
        eligible = {EmailQueueStatus.STAGED.value, EmailQueueStatus.CANCELLED.value}
        if any(row["status"] not in eligible for row in rows):
            return
        if not any(row["status"] == EmailQueueStatus.STAGED.value for row in rows):
            return
        with st.popover("Send Now with Power Automate"):
            st.write("Approve this batch for a manual upload to the restricted SharePoint library.")
            approved = st.checkbox(
                "I reviewed the recipients and statements.", key=f"flow-{batch_id}"
            )
            if st.button("Confirm and prepare handoff", disabled=not approved, type="primary"):
                try:
                    prepare_export(db, batch_id)
                except (ValueError, OSError) as exc:
                    st.error(f"Unable to prepare the handoff: {exc}")
                else:
                    st.rerun()
        return

    st.info("The handoff is prepared. Download it, upload it to SharePoint, and run the flow.")
    st.download_button(
        "Download approved email package",
        payload,
        file_name=f"{batch_id}-approved-emails.json",
        mime="application/json",
    )
    jobs = {job["queue_id"]: job["job_id"] for job in json.loads(payload)["jobs"]}
    pending = [
        row["id"]
        for row in rows
        if row["id"] in jobs
        and row["status"]
        in (EmailQueueStatus.HANDOFF_PREPARED.value, EmailQueueStatus.FLOW_REVIEW.value)
    ]
    if not pending:
        return
    with st.expander("Record a result from the SharePoint ledger"):
        queue_id = st.selectbox("Job", pending, format_func=lambda q: f"Queue {q} · {jobs[q]}")
        label = st.radio("Result", ["Accepted for sending", "Needs review"])
        confirmed = st.checkbox("I checked this exact job ID in SharePoint.", key=jobs[queue_id])
        if st.button("Save result", disabled=not confirmed):
            status = (
                EmailQueueStatus.FLOW_ACCEPTED
                if label == "Accepted for sending"
                else EmailQueueStatus.FLOW_REVIEW
            )
            record_result(db, batch_id, queue_id, status)
            st.rerun()


def render_queue() -> None:
    config = get_config()
    db = get_db()
    st.title("Email Queue")

    batch_ids = sorted(
        {row["batch_id"] for row in db.query("SELECT DISTINCT batch_id FROM email_queue")}
    )
    if not batch_ids:
        st.info("No staged emails yet. Stage a batch from the Statement Run wizard first.")
        return

    batch_id = st.selectbox("Batch", batch_ids)
    counts = queue_counts(db, batch_id)
    st.write(counts)

    rows = list_queue(db, batch_id)
    payload = get_export(db, batch_id)
    st.dataframe(
        [
            {
                "id": r["id"],
                "cid": r["constituent_id"],
                "to": r["to_address"],
                "subject": r["subject"],
                "status": r["status"],
                "attempts": r["attempts"],
                "last_error": r["last_error"],
            }
            for r in rows
        ],
        width="stretch",
    )

    if payload is not None or config.email.send_mode != "draft":
        _render_power_automate(db, batch_id, rows, payload)
    if payload is not None:
        return

    if sys.platform == "win32":
        staged_count = counts.get(EmailQueueStatus.STAGED.value, 0)
        queued_count = counts.get(EmailQueueStatus.QUEUED.value, 0)
        active_worker = st.session_state.get(K_ACTIVE_WORKER)
        worker_active = active_worker is not None and active_worker.is_alive()
        sender, success_status, action = _build_sender(config)
        with st.popover(action, disabled=worker_active or staged_count + queued_count == 0):
            st.write(f"{staged_count} staged and {queued_count} queued email(s).")
            confirmed = st.checkbox(
                "I have reviewed these emails to my satisfaction.", key=f"outlook-{batch_id}"
            )
            if st.button("Confirm " + action, disabled=not confirmed, type="primary"):
                confirm_send_now(db, batch_id)
                worker = EmailWorker(
                    db,
                    config,
                    sender,
                    batch_id,
                    config.teams_webhook_success,
                    config.teams_webhook_error,
                    success_status=success_status,
                )
                worker.start()
                st.session_state[K_ACTIVE_WORKER] = worker
                st.rerun()
    else:
        st.caption(
            "Desktop Outlook delivery requires Windows. Use Power Automate for email delivery from this host."
        )

    failed_ids = [r["id"] for r in rows if r["status"] == EmailQueueStatus.FAILED.value]
    if failed_ids and st.button(f"Retry {len(failed_ids)} failed"):
        retry_failed(db, failed_ids)
        st.rerun()

    cancellable_ids = [
        r["id"]
        for r in rows
        if r["status"] in (EmailQueueStatus.STAGED.value, EmailQueueStatus.QUEUED.value)
    ]
    if cancellable_ids and st.button(f"Cancel {len(cancellable_ids)} staged/queued"):
        cancel(db, cancellable_ids)
        st.rerun()


# History


def render_history() -> None:
    db = get_db()
    st.title("History")

    batches = db.query("SELECT * FROM batches ORDER BY created_at_utc DESC")
    st.dataframe(
        [
            {
                "batch_id": b["batch_id"],
                "created": b["created_at_local"],
                "template": b["template_id"],
                "delivery": b["delivery"],
                "rows": b["statement_year"],
                "status": b["status"],
            }
            for b in batches
        ],
        width="stretch",
    )

    st.divider()
    st.subheader("Constituent lookup")
    cid = st.text_input("Constituent ID")
    if cid:
        rows = db.query(
            "SELECT * FROM statement_history WHERE constituent_id = ? ORDER BY generated_at_utc DESC",
            (cid,),
        )
        if rows:
            st.dataframe(
                [
                    {
                        "batch_id": r["batch_id"],
                        "csv": r["csv_filename"],
                        "template": r["template_id"],
                        "delivery": r["delivery"],
                        "status": r["status"],
                        "when": r["generated_at_local"],
                    }
                    for r in rows
                ],
                width="stretch",
            )
        else:
            st.info("No history for this Constituent ID.")


# Settings


def render_settings() -> None:
    config = get_config()
    st.title("Settings")

    st.subheader("Email")
    send_mode = st.selectbox(
        "Send mode", ["send", "draft"], index=0 if config.email.send_mode == "send" else 1
    )
    throttle = st.number_input(
        "Throttle seconds", value=config.email.throttle_seconds, min_value=0.0
    )
    dpi = st.number_input("Preview DPI", value=config.preview.dpi, min_value=50, max_value=300)

    if st.button("Save settings"):
        app_config_path = config.repo_root / "config" / "app_config.json"
        raw = json.loads(app_config_path.read_text(encoding="utf-8"))
        raw["email"]["send_mode"] = send_mode
        raw["email"]["throttle_seconds"] = throttle
        raw["preview"]["dpi"] = int(dpi)
        app_config_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        st.cache_resource.clear()
        st.success("Settings saved. Reloading...")
        st.rerun()

    st.divider()
    st.subheader("Templates")
    try:
        templates = discover_templates(config.templates_dir)
        for t in templates:
            st.write(f"✅ {t.display_name} (`{t.id}`) — {t.description}")
    except Exception as exc:
        st.error(f"Template validation failed: {exc}")

    if st.button("Reload templates"):
        st.rerun()


# Status


def render_status() -> None:
    config = get_config()
    db = get_db()
    st.title("Status")

    st.write(f"Python: {sys.version.split()[0]}  ·  Platform: {sys.platform}")

    if st.button("Verify database"):
        ok = integrity_check(db.conn)
        st.success("Database OK") if ok else st.error("Database integrity check FAILED")

    if st.button("Back up now"):
        dest = backup_now(config.history_db_path, config.backups_dir, config.history.backup_keep)
        st.success(f"Backed up to {dest}") if dest else st.info("No database file to back up yet.")

    if st.button("Validate templates"):
        try:
            templates = discover_templates(config.templates_dir)
            st.success(f"All {len(templates)} templates loaded strict-clean.")
        except Exception as exc:
            st.error(str(exc))

    if st.button("Send Teams status notification"):
        ok = notify(
            config.teams_webhook_success,
            "Application status notification",
            {"triggered_by": "Status page"},
        )
        st.success("Sent") if ok else st.warning("Not sent (blank webhook or request failed)")

    usage = shutil.disk_usage(config.data_dir if config.data_dir.exists() else config.repo_root)
    st.write(f"Disk free: {usage.free / (1024**3):.1f} GB")

    if sys.platform == "win32":
        if st.button("Check Outlook COM reachable"):
            try:
                OutlookSender()._app_or_attach()
                st.success("Outlook reachable")
            except Exception as exc:
                st.error(str(exc))
    else:
        st.info("Outlook COM checks are only available when running on Windows.")
