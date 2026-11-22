from __future__ import annotations

import datetime as dt
import logging
import logging.handlers
import re
import secrets
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

import pytz

# Constants


APP_TIMEZONE = "America/Denver"


class DeliveryMethod(str, Enum):
    MAIL = "mail"
    EMAIL = "email"


class RecordStatus(str, Enum):
    INCLUDED = "included"
    SKIPPED = "skipped"
    REMOVED = "removed"
    ERROR = "error"


class DeliveryState(str, Enum):
    NONE = "none"
    EXPORTED = "exported"
    STAGED = "staged"
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"


class EmailQueueStatus(str, Enum):
    STAGED = "staged"
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    DRAFT_SAVED = "draft_saved"
    HANDOFF_PREPARED = "handoff_prepared"
    FLOW_ACCEPTED = "accepted_for_sending"
    FLOW_REVIEW = "needs_review"


class Severity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


POINTS_PER_INCH = 72.0
LETTER_WIDTH_IN = 8.5
LETTER_HEIGHT_IN = 11.0
LEADING_FACTOR = 1.25

STATUS_COLOR_GREEN = "#2e7d32"
STATUS_COLOR_AMBER = "#b26a00"
STATUS_COLOR_RED = "#c62828"


# Errors


def _reference_code() -> str:
    return secrets.token_hex(4)


class AppError(Exception):
    default_operator_message = "Something went wrong. Please try again or contact support."

    def __init__(self, message: str, operator_message: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.operator_message = operator_message or self.default_operator_message
        self.reference_code = _reference_code()

    def __str__(self) -> str:
        return f"{self.message} [ref={self.reference_code}]"


class ConfigError(AppError):
    default_operator_message = "The application configuration is invalid. Contact support."


class CsvError(AppError):
    default_operator_message = "The uploaded file could not be processed."


class TemplateError(AppError):
    default_operator_message = "A statement template failed to load."


class RecordError(AppError):
    default_operator_message = "This record could not be generated."


class LayoutError(RecordError):
    pass


class RenderError(RecordError):
    pass


class HistoryDbError(AppError):
    default_operator_message = "A database error occurred."


class EmailSendError(AppError):
    default_operator_message = "This email could not be sent."


class OutlookUnavailableError(AppError):
    default_operator_message = "Outlook is not available on this host."


# Dates


_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")


class DateParseError(ValueError):
    pass


def parse_date(text: str) -> dt.date:
    if text is None:
        raise DateParseError("Cannot parse blank date")
    s = text.strip()
    if not s:
        raise DateParseError("Cannot parse blank date")
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise DateParseError(f"Cannot parse date value: {text!r}")


def format_mm_dd(value: dt.date) -> str:
    return f"{value.month:02d}/{value.day:02d}"


def format_mm_dd_yyyy(value: dt.date) -> str:
    return f"{value.month:02d}/{value.day:02d}/{value.year}"


def format_long(value: dt.date) -> str:
    return f"{value:%B} {value.day}, {value.year}"


def now_local(timezone: str = APP_TIMEZONE) -> dt.datetime:
    return dt.datetime.now(pytz.timezone(timezone))


def now_utc() -> dt.datetime:
    return dt.datetime.now(pytz.utc)


def utc_iso(value: dt.datetime | None = None) -> str:
    value = value or now_utc()
    if value.tzinfo is None:
        value = pytz.utc.localize(value)
    return value.astimezone(pytz.utc).isoformat()


def local_iso(value: dt.datetime | None = None, timezone: str = APP_TIMEZONE) -> str:
    value = value or now_local(timezone)
    if value.tzinfo is None:
        value = pytz.utc.localize(value)
    return value.astimezone(pytz.timezone(timezone)).isoformat()


def default_statement_year(timezone: str = APP_TIMEZONE) -> int:
    return now_local(timezone).year - 1


# Currency


_CENTS = Decimal("0.01")
_CLEAN_RE = re.compile(r"[,\s$]")


class MoneyParseError(ValueError):
    pass


def parse_money(text: str) -> Decimal:
    if text is None:
        return Decimal("0.00")
    s = text.strip()
    if not s:
        return Decimal("0.00")
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    s = _CLEAN_RE.sub("", s)
    if s.startswith("-"):
        negative = True
        s = s[1:]
    if not s:
        raise MoneyParseError(f"Cannot parse money value: {text!r}")
    try:
        value = Decimal(s)
    except InvalidOperation as exc:
        raise MoneyParseError(f"Cannot parse money value: {text!r}") from exc
    if negative:
        value = -value
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def format_currency(value: Decimal) -> str:
    quantized = value.quantize(_CENTS, rounding=ROUND_HALF_UP)
    negative = quantized < 0
    magnitude = -quantized if negative else quantized
    formatted = f"{magnitude:,.2f}"
    return f"-${formatted}" if negative else f"${formatted}"


# Logging


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class _UtcFormatter(logging.Formatter):
    converter = staticmethod(time.gmtime)


def configure_logging(log_dir: Path, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if getattr(root, "_donor_statements_configured", False):
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = _UtcFormatter(_LOG_FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=5 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root._donor_statements_configured = True  # type: ignore[attr-defined]


def add_batch_log_handler(logger: logging.Logger, batch_log_path: Path) -> logging.Handler:
    batch_log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(batch_log_path, encoding="utf-8")
    handler.setFormatter(_UtcFormatter(_LOG_FORMAT))
    logger.addHandler(handler)
    return handler
