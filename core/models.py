from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Literal

from core.utils import DeliveryMethod, DeliveryState, RecordStatus


@dataclass(frozen=True)
class Gift:
    index: int
    date: dt.date
    amount: Decimal
    receipt_amount: Decimal
    reference: str
    is_ira: bool


@dataclass(frozen=True)
class Warning_:
    rule_id: str
    severity: Literal["warning", "error"]
    message: str


@dataclass(frozen=True)
class Donor:
    row_index: int
    constituent_id: str
    addressee: str
    salutation: str
    address_lines: tuple[str, ...]
    city: str
    state: str
    zip: str
    email: str
    gifts: tuple[Gift, ...]
    merge_fields: dict[str, str]
    total_amount: Decimal
    total_receipt: Decimal
    has_ira: bool


@dataclass
class Overrides:
    intro_text: str | None = None
    closing_text: str | None = None
    cover_body: str | None = None
    email_subject: str | None = None
    email_body_html: str | None = None


@dataclass
class LayoutResult:
    mode: Literal["single", "split", "overflow"]
    font_size: float
    row_pad_pt: float
    page_count: int
    left_rows: int = 0
    right_rows: int = 0
    overflow_rows: int = 0
    measured_heights: dict[str, float] = field(default_factory=dict)


@dataclass
class Record:
    donor: Donor
    status: RecordStatus = RecordStatus.INCLUDED
    delivery_state: DeliveryState = DeliveryState.NONE
    warnings: list[Warning_] = field(default_factory=list)
    overrides: Overrides = field(default_factory=Overrides)
    layout_result: LayoutResult | None = None
    pdf_path: Path | None = None
    error_message: str = ""
    edited: bool = False

    @property
    def row_index(self) -> int:
        return self.donor.row_index

    @property
    def constituent_id(self) -> str:
        return self.donor.constituent_id


@dataclass(frozen=True)
class Batch:
    batch_id: str
    csv_filename: str
    csv_sha256: str
    template_id: str
    delivery: DeliveryMethod
    statement_year: int
    created_at_utc: str
    row_count: int


@dataclass(frozen=True)
class HistoryHit:
    csv_filename: str
    generated_at_local: str
    template_id: str
    delivery: str
    status: str

    def as_message(self) -> str:
        return (
            f"Previously included in: {self.csv_filename} ({self.generated_at_local}, "
            f"{self.template_id}, {self.delivery})"
        )
