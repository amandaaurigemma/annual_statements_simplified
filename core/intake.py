from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

import pandas as pd

from core.config import AppConfig, CsvSchema
from core.models import Donor, Gift, HistoryHit, Warning_
from core.utils import CsvError, DateParseError, DeliveryMethod, parse_date, parse_money

# CSV ingestion


@dataclass(frozen=True)
class IngestResult:
    raw_header: list[str]
    raw_rows: dict[int, list[str]]
    donors: list[Donor]
    gift_issues: dict[int, list[Warning_]]
    encoding_used: str
    gift_group_indexes: list[int]
    incomplete_groups: list[int]


def _decode(csv_bytes: bytes, candidates: tuple[str, ...]) -> tuple[str, str]:
    last_exc: Exception | None = None
    for enc in candidates:
        try:
            return csv_bytes.decode(enc), enc
        except (UnicodeDecodeError, LookupError) as exc:
            last_exc = exc
            continue
    raise CsvError(f"Could not decode CSV using any of {candidates}: {last_exc}")


def _detect_gift_groups(
    headers: list[str], schema: CsvSchema
) -> tuple[dict[int, dict[str, str]], list[int]]:
    index_re = re.compile(schema.index_regex)
    groups: dict[int, dict[str, str]] = {}
    for header in headers:
        m = index_re.match(header)
        if not m:
            continue
        base, idx_str = m.group("base"), m.group("idx")
        idx = int(idx_str)
        if idx < 1 or idx > schema.max_groups:
            continue
        for logical, base_name in schema.gift_pattern_fields.items():
            if base == base_name:
                groups.setdefault(idx, {})[logical] = header
    incomplete = [
        idx for idx, fields in groups.items() if "amount" in fields and "date" not in fields
    ]
    return groups, incomplete


def ingest_csv(
    csv_bytes: bytes, filename: str, config: AppConfig, *, require_email: bool
) -> IngestResult:
    schema = config.csv_schema
    if not csv_bytes.strip():
        raise CsvError("Uploaded file is empty")

    text, encoding_used = _decode(csv_bytes, schema.encoding_candidates)

    reader = csv.reader(io.StringIO(text))
    try:
        raw_header = next(reader)
    except StopIteration as exc:
        raise CsvError("Uploaded file has no header row") from exc

    if len(set(raw_header)) != len(raw_header):
        seen: dict[str, int] = {}
        dupes = []
        for h in raw_header:
            seen[h] = seen.get(h, 0) + 1
        dupes = [h for h, c in seen.items() if c > 1]
        raise CsvError(f"Duplicate header names in CSV: {dupes}")

    raw_rows: dict[int, list[str]] = {}
    for row_index, row in enumerate(reader):
        raw_rows[row_index] = row

    if not raw_rows:
        raise CsvError("Uploaded file has no data rows")

    required = list(schema.required_headers)
    if require_email:
        required += list(schema.required_headers_email_delivery)
    missing = [h for h in required if h not in raw_header]
    if missing:
        raise CsvError(f"Missing required column(s): {missing}")

    groups, incomplete_groups = _detect_gift_groups(raw_header, schema)
    if not groups:
        raise CsvError("No gift columns detected in the uploaded CSV")

    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)

    donors: list[Donor] = []
    gift_issues: dict[int, list[Warning_]] = {}
    gift_group_indexes = sorted(groups)

    for row_index, row in df.iterrows():
        gifts: list[Gift] = []
        issues: list[Warning_] = []
        for idx in gift_group_indexes:
            fields = groups[idx]
            amount_header = fields.get("amount")
            if not amount_header:
                continue
            amount_raw = row.get(amount_header, "")
            if not amount_raw.strip():
                continue
            try:
                amount = parse_money(amount_raw)
            except Exception:
                issues.append(
                    Warning_("E-AMT", "error", f"Gift {idx}: unparseable amount {amount_raw!r}")
                )
                continue
            date_header = fields.get("date")
            date_raw = row.get(date_header, "") if date_header else ""
            try:
                gift_date = parse_date(date_raw)
            except DateParseError:
                issues.append(
                    Warning_("E-DAT", "error", f"Gift {idx}: unparseable date {date_raw!r}")
                )
                continue
            receipt_header = fields.get("receipt")
            receipt_raw = row.get(receipt_header, "") if receipt_header else ""
            receipt_amount = parse_money(receipt_raw) if receipt_raw.strip() else Decimal("0.00")
            reference_header = fields.get("reference")
            reference = row.get(reference_header, "") if reference_header else ""
            is_ira = config.ira.marker_substring in reference
            gifts.append(
                Gift(
                    index=idx,
                    date=gift_date,
                    amount=amount,
                    receipt_amount=receipt_amount,
                    reference=reference,
                    is_ira=is_ira,
                )
            )

        if issues:
            gift_issues[row_index] = issues

        total_amount = sum((g.amount for g in gifts), Decimal("0.00"))
        total_receipt = sum((g.receipt_amount for g in gifts), Decimal("0.00"))
        has_ira = any(g.is_ira for g in gifts)

        address_lines = tuple(
            v for h in schema.address_line_headers if (v := row.get(h, "").strip())
        )
        merge_fields = {h: row.get(h, "") for h in raw_header}
        merge_fields["Statement Year"] = ""
        merge_fields["Total Amount"] = ""
        merge_fields["Total Receipt Amount"] = ""
        merge_fields["Gift Count"] = str(len(gifts))
        merge_fields["Today"] = ""
        merge_fields["City State Zip"] = (
            f"{row.get(schema.city_header, '')}, {row.get(schema.state_header, '')} {row.get(schema.zip_header, '')}"
        )

        donor = Donor(
            row_index=row_index,
            constituent_id=row.get(schema.constituent_id_header, ""),
            addressee=row.get(schema.addressee_header, ""),
            salutation=row.get(schema.salutation_header, ""),
            address_lines=address_lines,
            city=row.get(schema.city_header, ""),
            state=row.get(schema.state_header, ""),
            zip=row.get(schema.zip_header, ""),
            email=row.get(schema.email_header, "") if schema.email_header in raw_header else "",
            gifts=tuple(gifts),
            merge_fields=merge_fields,
            total_amount=total_amount,
            total_receipt=total_receipt,
            has_ira=has_ira,
        )
        donors.append(donor)

    return IngestResult(
        raw_header=raw_header,
        raw_rows=raw_rows,
        donors=donors,
        gift_issues=gift_issues,
        encoding_used=encoding_used,
        gift_group_indexes=gift_group_indexes,
        incomplete_groups=incomplete_groups,
    )


# Validation


@dataclass
class ValidationReport:
    blocking: list[str] = field(default_factory=list)
    general_warnings: list[Warning_] = field(default_factory=list)
    rows: dict[int, list[Warning_]] = field(default_factory=dict)
    duplicate_hits: dict[int, list[HistoryHit]] = field(default_factory=dict)
    auto_skip_rows: set[int] = field(default_factory=set)

    def _add(self, row_index: int, warning: Warning_) -> None:
        self.rows.setdefault(row_index, []).append(warning)

    @property
    def has_blocking_errors(self) -> bool:
        return bool(self.blocking)

    def to_csv_bytes(self) -> bytes:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["row_index", "constituent_id", "rule_id", "severity", "message"])
        for row_index in sorted(self.rows):
            for w in self.rows[row_index]:
                writer.writerow([row_index, "", w.rule_id, w.severity, w.message])
        return buf.getvalue().encode("utf-8")


HistoryLookup = Callable[[list[str]], dict[str, list[HistoryHit]]]


def validate(
    ingest: IngestResult,
    delivery: DeliveryMethod,
    *,
    history_lookup: HistoryLookup | None = None,
) -> ValidationReport:
    report = ValidationReport()

    for idx in ingest.incomplete_groups:
        report.general_warnings.append(
            Warning_(
                "W-GRP",
                "warning",
                f"Gift group {idx} has an amount column but no matching date column",
            )
        )

    for row_index, issues in ingest.gift_issues.items():
        for w in issues:
            report._add(row_index, w)

    seen_cids: dict[str, list[int]] = defaultdict(list)
    for donor in ingest.donors:
        seen_cids[donor.constituent_id].append(donor.row_index)

    for donor in ingest.donors:
        row_index = donor.row_index

        if not donor.gifts:
            report._add(row_index, Warning_("W-NOGIFT", "warning", "Donor has zero gifts"))

        if len(seen_cids[donor.constituent_id]) > 1:
            report._add(
                row_index,
                Warning_(
                    "W-DUPFILE",
                    "warning",
                    f"Constituent ID {donor.constituent_id} appears "
                    f"{len(seen_cids[donor.constituent_id])} times in this upload",
                ),
            )

        if delivery == DeliveryMethod.EMAIL:
            if not donor.email.strip():
                report._add(row_index, Warning_("W-NOEMAIL", "error", "Email address is blank"))
                report.auto_skip_rows.add(row_index)

        if delivery == DeliveryMethod.MAIL:
            missing = []
            if not donor.addressee.strip():
                missing.append("Addressee")
            if not donor.address_lines:
                missing.append("Address Line 1")
            if not donor.city.strip():
                missing.append("City")
            if not donor.state.strip():
                missing.append("State")
            if not donor.zip.strip():
                missing.append("ZIP Code")
            if missing:
                report._add(
                    row_index,
                    Warning_(
                        "W-ADDR", "warning", f"Missing address field(s): {', '.join(missing)}"
                    ),
                )

    if history_lookup is not None:
        cids = list(seen_cids.keys())
        hits_by_cid = history_lookup(cids)
        for donor in ingest.donors:
            hits = hits_by_cid.get(donor.constituent_id)
            if hits:
                report.duplicate_hits[donor.row_index] = hits
                for hit in hits:
                    report._add(donor.row_index, Warning_("W-DUPHIST", "warning", hit.as_message()))

    return report
