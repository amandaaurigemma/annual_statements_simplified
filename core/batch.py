from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from core.config import AppConfig
from core.documents import build_mail_outputs, build_record_pdf
from core.intake import IngestResult, ValidationReport, ingest_csv, validate
from core.models import Batch, Donor, Overrides, Record
from core.persistence import (
    Database,
    insert_batch,
    lookup_duplicates,
    record_generated,
    update_batch_status,
    update_history_status,
)
from core.templates import TemplateManifest, get_template, render
from core.utils import (
    DeliveryMethod,
    DeliveryState,
    RecordStatus,
    format_currency,
    format_long,
    now_local,
    utc_iso,
)

ProgressCallback = Callable[[int, int, str], None]


def _sanitize_filename(text: str) -> str:
    for ch in '\\/:*?"<>|':
        text = text.replace(ch, "-")
    return " ".join(text.split())[:120]


def _build_field_map(donor: Donor, statement_year: int, timezone: str) -> dict[str, str]:
    fields = dict(donor.merge_fields)
    fields["Statement Year"] = str(statement_year)
    fields["Total Amount"] = format_currency(donor.total_amount)
    fields["Total Receipt Amount"] = format_currency(donor.total_receipt)
    fields["Gift Count"] = str(len(donor.gifts))
    fields["Today"] = format_long(now_local(timezone).date())
    fields["City State Zip"] = f"{donor.city}, {donor.state} {donor.zip}"
    return fields


@dataclass
class BatchContext:
    batch: Batch
    ingest: IngestResult
    validation: ValidationReport
    template_manifest: TemplateManifest
    config: AppConfig
    batch_dir: Path
    records: dict[int, Record] = field(default_factory=dict)
    field_maps: dict[int, dict[str, str]] = field(default_factory=dict)
    rendered_blocks: dict[int, dict[str, str]] = field(default_factory=dict)

    @property
    def manifest_path(self) -> Path:
        return self.batch_dir / "manifest.json"

    @property
    def records_dir(self) -> Path:
        return self.batch_dir / "records"

    @property
    def output_dir(self) -> Path:
        return self.batch_dir / "output"

    @property
    def logs_dir(self) -> Path:
        return self.batch_dir / "logs"

    def donors_by_row(self) -> dict[int, Donor]:
        return {d.row_index: d for d in self.ingest.donors}


# Batch creation


def create_batch(
    csv_bytes: bytes,
    filename: str,
    template_id: str,
    delivery: DeliveryMethod,
    statement_year: int,
    config: AppConfig,
    db: Database | None = None,
) -> BatchContext:
    sha256 = hashlib.sha256(csv_bytes).hexdigest()
    timestamp = now_local(config.timezone).strftime("%Y%m%d_%H%M%S")
    batch_id = f"{timestamp}_{template_id}_{delivery.value}"
    batch_dir = config.batches_dir / batch_id
    for sub in ("source", "records", "output", "logs"):
        (batch_dir / sub).mkdir(parents=True, exist_ok=True)
    (batch_dir / "source" / "original.csv").write_bytes(csv_bytes)

    require_email = delivery == DeliveryMethod.EMAIL
    ingest = ingest_csv(csv_bytes, filename, config, require_email=require_email)

    history_lookup = (lambda cids: lookup_duplicates(db, cids)) if db is not None else None
    validation = validate(ingest, delivery, history_lookup=history_lookup)

    batch = Batch(
        batch_id=batch_id,
        csv_filename=filename,
        csv_sha256=sha256,
        template_id=template_id,
        delivery=delivery,
        statement_year=statement_year,
        created_at_utc=utc_iso(),
        row_count=len(ingest.donors),
    )
    if db is not None:
        insert_batch(db, batch)

    template_manifest = get_template(config.templates_dir, template_id)

    ctx = BatchContext(
        batch=batch,
        ingest=ingest,
        validation=validation,
        template_manifest=template_manifest,
        config=config,
        batch_dir=batch_dir,
    )

    for donor in ingest.donors:
        status = (
            RecordStatus.SKIPPED
            if donor.row_index in validation.auto_skip_rows
            else RecordStatus.INCLUDED
        )
        ctx.records[donor.row_index] = Record(donor=donor, status=status)
        ctx.field_maps[donor.row_index] = _build_field_map(donor, statement_year, config.timezone)

    write_manifest(ctx)
    return ctx


# Generation and review


def _render_blocks_for(ctx: BatchContext, donor: Donor) -> dict[str, str]:
    manifest = ctx.template_manifest
    fields = ctx.field_maps[donor.row_index]
    intro = render(manifest.read_block("statement_intro.txt"), fields).output
    closing = render(manifest.read_block("statement_closing.txt"), fields).output
    cover_body = render(manifest.read_block("cover_letter.txt"), fields).output
    email_subject = (
        render(manifest.read_block("email_subject.txt"), fields).output.replace("\n", " ").strip()
    )

    cover_html = (
        "<p>"
        + cover_body.replace("\r\n", "\n").replace("\n\n", "</p><p>").replace("\n", "<br/>")
        + "</p>"
    )
    body_fields = dict(fields)
    body_fields["Cover Letter"] = cover_html
    email_body_html = render(
        manifest.read_block("email_body.html"), body_fields, html_context=True
    ).output

    return {
        "intro": intro,
        "closing": closing,
        "cover_body": cover_body,
        "email_subject": email_subject,
        "email_body_html": email_body_html,
    }


def _apply_overrides(rendered: dict[str, str], overrides: Overrides) -> dict[str, str]:
    result = dict(rendered)
    if overrides.intro_text is not None:
        result["intro"] = overrides.intro_text
    if overrides.closing_text is not None:
        result["closing"] = overrides.closing_text
    if overrides.cover_body is not None:
        result["cover_body"] = overrides.cover_body
    if overrides.email_subject is not None:
        result["email_subject"] = overrides.email_subject
    if overrides.email_body_html is not None:
        result["email_body_html"] = overrides.email_body_html
    return result


def _record_dir(ctx: BatchContext, donor: Donor) -> Path:
    return ctx.records_dir / f"{donor.row_index:05d}_{donor.constituent_id}"


def _generate_one(ctx: BatchContext, record: Record, db: Database | None) -> None:
    donor = record.donor
    fonts_dir = ctx.config.repo_root / "assets" / "fonts"
    rendered = _render_blocks_for(ctx, donor)
    rendered = _apply_overrides(rendered, record.overrides)
    ctx.rendered_blocks[donor.row_index] = rendered

    try:
        out_dir = _record_dir(ctx, donor)
        today_long = format_long(now_local(ctx.config.timezone).date())
        pdf_path = build_record_pdf(
            donor,
            rendered["intro"],
            rendered["closing"],
            rendered["cover_body"],
            ctx.template_manifest,
            fonts_dir,
            today_long,
            out_dir,
        )
        record.pdf_path = pdf_path
        if record.status not in (RecordStatus.SKIPPED, RecordStatus.REMOVED):
            record.status = RecordStatus.INCLUDED
        record.error_message = ""
        if db is not None:
            record_generated(
                db, ctx.batch.batch_id, donor, ctx.template_manifest.id, ctx.batch.delivery
            )
    except Exception as exc:
        record.status = RecordStatus.ERROR
        record.error_message = str(exc)


def generate(
    ctx: BatchContext, db: Database | None = None, on_progress: ProgressCallback | None = None
) -> None:
    total = len(ctx.ingest.donors)
    for i, donor in enumerate(ctx.ingest.donors, start=1):
        record = ctx.records[donor.row_index]
        if record.status != RecordStatus.REMOVED:
            _generate_one(ctx, record, db)
        if on_progress:
            on_progress(i, total, donor.addressee)
    write_manifest(ctx)


def mutate(
    ctx: BatchContext,
    row_index: int,
    action: str,
    overrides: Overrides | None = None,
    db: Database | None = None,
) -> None:
    record = ctx.records[row_index]
    if action == "skip":
        record.status = RecordStatus.SKIPPED
    elif action == "unskip":
        record.status = RecordStatus.INCLUDED
    elif action == "remove":
        record.status = RecordStatus.REMOVED
    elif action == "restore":
        record.status = RecordStatus.INCLUDED
    elif action == "edit":
        if overrides is not None:
            record.overrides = overrides
        record.edited = True
        _generate_one(ctx, record, db)
        _invalidate_previews(record)
    else:
        raise ValueError(f"Unknown mutate action: {action}")
    write_manifest(ctx)


def _invalidate_previews(record: Record) -> None:
    if record.pdf_path is None:
        return
    for preview in record.pdf_path.parent.glob("preview_p*.png"):
        preview.unlink(missing_ok=True)
    marker = record.pdf_path.parent / ".preview_dpi"
    marker.unlink(missing_ok=True)


# Delivery artifacts


def _write_removed_csv(ctx: BatchContext, out_path: Path) -> int:
    removed_rows = [
        row_index for row_index, r in ctx.records.items() if r.status == RecordStatus.REMOVED
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(ctx.ingest.raw_header)
        for row_index in sorted(removed_rows):
            writer.writerow(ctx.ingest.raw_rows[row_index])
    return len(removed_rows)


def _write_run_summary(ctx: BatchContext, out_path: Path) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "row_index",
                "constituent_id",
                "addressee",
                "status",
                "delivery_state",
                "edited",
                "warning_rule_ids",
                "error_message",
            ]
        )
        for row_index in sorted(ctx.records):
            r = ctx.records[row_index]
            warning_ids = ";".join(w.rule_id for w in ctx.validation.rows.get(row_index, []))
            writer.writerow(
                [
                    row_index,
                    r.constituent_id,
                    r.donor.addressee,
                    r.status.value,
                    r.delivery_state.value,
                    r.edited,
                    warning_ids,
                    r.error_message,
                ]
            )


def export_mail(ctx: BatchContext, db: Database | None = None) -> dict[str, Path]:
    included = [r for r in ctx.records.values() if r.status == RecordStatus.INCLUDED and r.pdf_path]
    fonts_dir = ctx.config.repo_root / "assets" / "fonts"
    record_paths = [(r.row_index, r.pdf_path) for r in included]
    donors_by_row = ctx.donors_by_row()

    mail_path, envelopes_path = build_mail_outputs(
        record_paths, donors_by_row, ctx.template_manifest, fonts_dir, ctx.output_dir
    )

    removed_path = ctx.output_dir / "removed.csv"
    removed_count = _write_removed_csv(ctx, removed_path)

    summary_path = ctx.output_dir / "run_summary.csv"
    _write_run_summary(ctx, summary_path)

    for r in included:
        r.delivery_state = DeliveryState.EXPORTED
        if db is not None:
            update_history_status(db, ctx.batch.batch_id, r.constituent_id, "exported")

    write_manifest(ctx)

    outputs = {
        "statements_mail": mail_path,
        "envelopes": envelopes_path,
        "run_summary": summary_path,
    }
    if removed_count:
        outputs["removed"] = removed_path
    return outputs


def build_email_attachment_name(ctx: BatchContext, donor: Donor) -> str:
    fields = ctx.field_maps[donor.row_index]
    pattern = ctx.template_manifest.raw.get("email", {}).get(
        "attachment_name_pattern", "{{Statement Year}} Giving Statement - {{Addressee}}.pdf"
    )
    return _sanitize_filename(render(pattern, fields).output)


def stage_emails(ctx: BatchContext) -> list[dict]:
    staged = []
    for row_index in sorted(ctx.records):
        record = ctx.records[row_index]
        if record.status != RecordStatus.INCLUDED or not record.pdf_path:
            continue
        donor = record.donor
        if not donor.email.strip():
            continue
        rendered = ctx.rendered_blocks.get(row_index) or _render_blocks_for(ctx, donor)
        rendered = _apply_overrides(rendered, record.overrides)
        staged.append(
            {
                "row_index": row_index,
                "constituent_id": donor.constituent_id,
                "to_address": donor.email.strip(),
                "subject": rendered["email_subject"],
                "body_html": rendered["email_body_html"],
                "attachment_path": str(record.pdf_path),
                "attachment_name": build_email_attachment_name(ctx, donor),
            }
        )
        record.delivery_state = DeliveryState.STAGED
    write_manifest(ctx)
    return staged


def finalize(ctx: BatchContext, db: Database | None = None) -> None:
    if db is not None:
        update_batch_status(db, ctx.batch.batch_id, "finalized")
    write_manifest(ctx)


# Batch manifests


def _record_to_dict(record: Record) -> dict:
    return {
        "row_index": record.row_index,
        "constituent_id": record.constituent_id,
        "status": record.status.value,
        "delivery_state": record.delivery_state.value,
        "edited": record.edited,
        "warnings": [asdict(w) for w in record.warnings],
        "overrides": asdict(record.overrides),
        "pdf_path": str(record.pdf_path) if record.pdf_path else None,
        "error_message": record.error_message,
        "layout_result": asdict(record.layout_result) if record.layout_result else None,
    }


def write_manifest(ctx: BatchContext) -> None:
    data = {
        "manifest_version": 1,
        "batch": {
            "batch_id": ctx.batch.batch_id,
            "csv_filename": ctx.batch.csv_filename,
            "csv_sha256": ctx.batch.csv_sha256,
            "template_id": ctx.batch.template_id,
            "delivery": ctx.batch.delivery.value,
            "statement_year": ctx.batch.statement_year,
            "created_at_utc": ctx.batch.created_at_utc,
            "row_count": ctx.batch.row_count,
        },
        "records": {str(k): _record_to_dict(v) for k, v in ctx.records.items()},
    }
    tmp_path = ctx.manifest_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, ctx.manifest_path)


def read_manifest(batch_dir: Path) -> dict:
    manifest_path = batch_dir / "manifest.json"
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def list_resumable_batches(batches_dir: Path) -> list[dict]:
    resumable = []
    if not batches_dir.exists():
        return resumable
    for child in sorted(batches_dir.iterdir()):
        manifest_path = child / "manifest.json"
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                resumable.append(data)
            except json.JSONDecodeError:
                continue
    return resumable
