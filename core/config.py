from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from core.utils import ConfigError

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class EmailConfig:
    send_mode: str = "send"
    from_on_behalf: str = ""
    throttle_seconds: float = 2.0
    max_attempts: int = 3
    retry_backoff_seconds: float = 30.0


@dataclass(frozen=True)
class PreviewConfig:
    dpi: int = 110
    max_pages: int = 3


@dataclass(frozen=True)
class HistoryConfig:
    backup_keep: int = 20


@dataclass(frozen=True)
class RetentionConfig:
    batch_artifact_days: int = 90


@dataclass(frozen=True)
class GenerationConfig:
    continue_on_record_error: bool = True


@dataclass(frozen=True)
class IraConfig:
    marker_substring: str = "IRA "
    case_sensitive: bool = True


@dataclass(frozen=True)
class ServerConfig:
    session_stale_minutes: int = 5


@dataclass(frozen=True)
class CsvSchema:
    encoding_candidates: tuple[str, ...]
    constituent_id_header: str
    addressee_header: str
    salutation_header: str
    address_line_headers: tuple[str, ...]
    city_header: str
    state_header: str
    zip_header: str
    email_header: str
    gift_pattern_fields: dict[str, str]
    index_regex: str
    max_groups: int
    required_headers: tuple[str, ...]
    required_headers_email_delivery: tuple[str, ...]


@dataclass(frozen=True)
class AppConfig:
    app_name: str
    timezone: str
    data_dir: Path
    preview: PreviewConfig
    email: EmailConfig
    history: HistoryConfig
    retention: RetentionConfig
    generation: GenerationConfig
    ira: IraConfig
    server: ServerConfig
    csv_schema: CsvSchema
    teams_webhook_success: str = ""
    teams_webhook_error: str = ""
    ui_passcode_sha256: str = ""
    repo_root: Path = field(default=REPO_ROOT)

    @property
    def templates_dir(self) -> Path:
        return self.repo_root / "templates"

    @property
    def batches_dir(self) -> Path:
        return self.data_dir / "batches"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def history_db_path(self) -> Path:
        return self.data_dir / "history.db"


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _require(d: dict, key: str, ctx: str) -> Any:
    if key not in d:
        raise ConfigError(f"Missing required config key '{key}' in {ctx}")
    return d[key]


def load_app_config(
    repo_root: Path | None = None,
    app_config_path: Path | None = None,
    csv_schema_path: Path | None = None,
    env_path: Path | None = None,
) -> AppConfig:
    root = repo_root or REPO_ROOT
    app_config_path = app_config_path or root / "config" / "app_config.json"
    csv_schema_path = csv_schema_path or root / "config" / "csv_schema.json"
    env_path = env_path or root / ".env"

    env = dotenv_values(env_path) if env_path.exists() else {}
    env = {
        **env,
        **{
            k: v
            for k, v in os.environ.items()
            if k in ("TEAMS_WEBHOOK_SUCCESS", "TEAMS_WEBHOOK_ERROR", "UI_PASSCODE_SHA256")
        },
    }

    ui_passcode = (env.get("UI_PASSCODE_SHA256") or "").strip()
    if ui_passcode and not _HEX64.match(ui_passcode):
        raise ConfigError("UI_PASSCODE_SHA256 must be a 64-character hex sha256 digest, or blank")

    try:
        raw = json.loads(app_config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"app_config.json not found at {app_config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"app_config.json is not valid JSON: {exc}") from exc

    try:
        preview = PreviewConfig(**raw.get("preview", {}))
        email_settings = {**raw.get("email", {})}
        email_settings.pop("test_recipient", None)
        email = EmailConfig(**email_settings)
        if email.send_mode not in ("send", "draft"):
            raise ConfigError(f"email.send_mode must be 'send' or 'draft', got {email.send_mode!r}")
        history = HistoryConfig(**raw.get("history", {}))
        retention = RetentionConfig(**raw.get("retention", {}))
        generation = GenerationConfig(**raw.get("generation", {}))
        ira = IraConfig(**raw.get("ira", {}))
        server = ServerConfig(**raw.get("server", {}))
    except TypeError as exc:
        raise ConfigError(f"app_config.json has an unrecognized key: {exc}") from exc

    data_dir_raw = raw.get("data_dir", "data")
    data_dir = Path(data_dir_raw)
    if not data_dir.is_absolute():
        data_dir = root / data_dir

    try:
        schema_raw = json.loads(csv_schema_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"csv_schema.json not found at {csv_schema_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"csv_schema.json is not valid JSON: {exc}") from exc

    try:
        name_address = _require(schema_raw, "name_address", "csv_schema.json")
        gifts = _require(schema_raw, "gifts", "csv_schema.json")
        csv_schema = CsvSchema(
            encoding_candidates=tuple(
                schema_raw.get("encoding_candidates", ["utf-8-sig", "cp1252"])
            ),
            constituent_id_header=_require(schema_raw, "identity", "csv_schema.json")[
                "constituent_id"
            ],
            addressee_header=name_address["addressee"],
            salutation_header=name_address.get("salutation", "Salutation"),
            address_line_headers=tuple(name_address["address_lines"]),
            city_header=name_address["city"],
            state_header=name_address["state"],
            zip_header=name_address["zip"],
            email_header=schema_raw.get("email", {}).get("address", "Email Address"),
            gift_pattern_fields=dict(gifts["pattern_fields"]),
            index_regex=gifts.get("index_regex", r"^Gifts\s+(?P<idx>\d+)\s+(?P<base>.+)$"),
            max_groups=int(gifts.get("max_groups", 400)),
            required_headers=tuple(schema_raw.get("required_headers", [])),
            required_headers_email_delivery=tuple(
                schema_raw.get("required_headers_email_delivery", [])
            ),
        )
    except KeyError as exc:
        raise ConfigError(f"csv_schema.json missing key: {exc}") from exc

    try:
        re.compile(csv_schema.index_regex)
    except re.error as exc:
        raise ConfigError(f"csv_schema.json index_regex is invalid: {exc}") from exc

    return AppConfig(
        app_name=raw.get("app_name", "Annual Donor Statements"),
        timezone=raw.get("timezone", "America/Denver"),
        data_dir=data_dir,
        preview=preview,
        email=email,
        history=history,
        retention=retention,
        generation=generation,
        ira=ira,
        server=server,
        csv_schema=csv_schema,
        teams_webhook_success=(env.get("TEAMS_WEBHOOK_SUCCESS") or "").strip(),
        teams_webhook_error=(env.get("TEAMS_WEBHOOK_ERROR") or "").strip(),
        ui_passcode_sha256=ui_passcode,
        repo_root=root,
    )


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
