from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import escape as _html_escape
from pathlib import Path
from typing import Mapping

from core.utils import (
    TemplateError,
    format_currency,
    format_mm_dd,
    format_mm_dd_yyyy,
    parse_date,
    parse_money,
)

# Merge fields and conditionals


MAX_CONDITIONAL_DEPTH = 3

_LITERAL_BRACE_TOKEN = "\x00LITERAL_OPEN_BRACE\x00"
_PLACEHOLDER_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_TOKEN_RE = re.compile(r"\{\{#if\s+.+?\}\}|\{\{else\}\}|\{\{/if\}\}", re.DOTALL)
_IF_OPEN_RE = re.compile(r"\{\{#if\s+(?P<cond>.+?)\}\}", re.DOTALL)
_ELSE_TOKEN = "{{else}}"
_COND_EQ_RE = re.compile(r'^(?P<field>.+?)\s*(?P<op>==|!=)\s*"(?P<literal>.*)"$', re.DOTALL)


@dataclass
class RenderResult:
    output: str
    unknown_fields: list[str] = field(default_factory=list)


def _apply_filter(value: str, name: str, arg: str | None) -> str:
    if name == "upper":
        return value.upper()
    if name == "lower":
        return value.lower()
    if name == "title":
        return value.title()
    if name == "trim":
        return value.strip()
    if name == "default":
        return arg if not value.strip() else value
    if name == "currency":
        try:
            return format_currency(parse_money(value))
        except Exception:
            return value
    if name == "date":
        try:
            parsed = parse_date(value)
        except Exception:
            return value
        if arg == "MM/DD/YYYY":
            return format_mm_dd_yyyy(parsed)
        return format_mm_dd(parsed)

    return value


def _resolve_placeholder(raw: str, fields: Mapping[str, str], unknown: list[str]) -> str:
    parts = raw.split("|")
    field_name = parts[0].strip()
    if field_name not in fields:
        unknown.append(field_name)
        value = ""
    else:
        value = fields[field_name]
    for filter_expr in parts[1:]:
        filter_expr = filter_expr.strip()
        if ":" in filter_expr:
            fname, arg = filter_expr.split(":", 1)
        else:
            fname, arg = filter_expr, None
        value = _apply_filter(value, fname.strip(), arg)
    return value


def _substitute_placeholders(
    text: str, fields: Mapping[str, str], unknown: list[str], html_context: bool
) -> str:
    def _sub(m: re.Match) -> str:
        raw_value = _resolve_placeholder(m.group(1), fields, unknown)
        return _html_escape(raw_value) if html_context else _escape_pdf_text(raw_value)

    return _PLACEHOLDER_RE.sub(_sub, text)


def _escape_pdf_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _eval_condition(cond: str, fields: Mapping[str, str]) -> bool:
    m = _COND_EQ_RE.match(cond.strip())
    if m:
        field_name = m.group("field").strip()
        literal = m.group("literal")
        actual = fields.get(field_name, "")
        return (actual == literal) if m.group("op") == "==" else (actual != literal)
    field_name = cond.strip()
    return bool(fields.get(field_name, "").strip())


def _find_matching(text: str) -> tuple[int | None, int, int]:
    depth = 0
    else_index: int | None = None
    for m in _TOKEN_RE.finditer(text):
        token = m.group(0)
        if token.startswith("{{#if"):
            depth += 1
        elif token == "{{else}}":
            if depth == 0 and else_index is None:
                else_index = m.start()
        elif token == "{{/if}}":
            if depth == 0:
                return else_index, m.start(), m.end()
            depth -= 1
    raise TemplateError("Unclosed {{#if}} block in template")


def _process(
    text: str, fields: Mapping[str, str], depth: int, unknown: list[str], html_context: bool
) -> str:
    m = _IF_OPEN_RE.search(text)
    if not m:
        return _substitute_placeholders(text, fields, unknown, html_context)

    if depth >= MAX_CONDITIONAL_DEPTH:
        raise TemplateError(f"Conditional nesting exceeds max depth of {MAX_CONDITIONAL_DEPTH}")

    before = text[: m.start()]
    cond = m.group("cond").strip()
    after_open = text[m.end() :]

    else_idx, close_start, close_end = _find_matching(after_open)
    if else_idx is not None:
        if_body = after_open[:else_idx]
        else_body = after_open[else_idx + len(_ELSE_TOKEN) : close_start]
    else:
        if_body = after_open[:close_start]
        else_body = ""
    remainder = after_open[close_end:]

    chosen_body = if_body if _eval_condition(cond, fields) else else_body

    resolved_before = _substitute_placeholders(before, fields, unknown, html_context)
    resolved_chosen = _process(chosen_body, fields, depth + 1, unknown, html_context)
    resolved_remainder = _process(remainder, fields, depth, unknown, html_context)
    return resolved_before + resolved_chosen + resolved_remainder


def render(
    text: str, fields: Mapping[str, str], *, strict: bool = False, html_context: bool = False
) -> RenderResult:
    prepared = text.replace("\\{{", _LITERAL_BRACE_TOKEN)
    unknown: list[str] = []
    output = _process(prepared, fields, 0, unknown, html_context)
    output = output.replace(_LITERAL_BRACE_TOKEN, "{{")
    if strict and unknown:
        raise TemplateError(f"Unknown template field(s): {sorted(set(unknown))}")
    return RenderResult(output=output, unknown_fields=unknown)


# Template manifests


_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass(frozen=True)
class TemplateManifest:
    id: str
    display_name: str
    description: str
    version: int
    dir_path: Path
    raw: dict

    def block_path(self, filename: str) -> Path:
        return self.dir_path / filename

    def asset_path(self, relative: str) -> Path:
        return self.dir_path / relative

    def read_block(self, filename: str) -> str:
        path = self.block_path(filename)
        return path.read_text(encoding="utf-8") if path.exists() else ""


def _validate_colors(raw: dict, manifest_id: str) -> None:
    candidates = [
        raw.get("fonts", {}).get("color"),
        raw.get("gift_table", {}).get("grid_color"),
        raw.get("gift_table", {}).get("header_bg"),
        raw.get("gift_table", {}).get("split_divider", {}).get("color"),
    ]
    for color in candidates:
        if color and not _COLOR_RE.match(color):
            raise TemplateError(f"Template {manifest_id}: invalid color {color!r}")


def _validate_fractions(raw: dict, manifest_id: str) -> None:
    cols = raw.get("gift_table", {}).get("columns", {})
    total = cols.get("date_frac", 0) + cols.get("amount_frac", 0) + cols.get("receipt_frac", 0)
    if abs(total - 1.0) > 0.001:
        raise TemplateError(
            f"Template {manifest_id}: gift_table.columns fractions must sum to ~1.0, got {total}"
        )


def _validate_assets(raw: dict, dir_path: Path, manifest_id: str) -> None:
    for key in ("header_image", "footer_image"):
        img = raw.get(key, {})
        file_rel = img.get("file")
        if file_rel and not (dir_path / file_rel).exists():
            raise TemplateError(f"Template {manifest_id}: missing asset {file_rel!r} for {key}")


_SYNTHETIC_DONOR_FIELDS = {
    "Addressee": "Test Donor",
    "Salutation": "Test",
    "Statement Year": "2026",
    "Total Amount": "$100.00",
    "Total Receipt Amount": "$100.00",
    "Gift Count": "1",
    "Today": "01/01/2026",
    "City State Zip": "Denver, CO 80202",
}


def load_manifest(dir_path: Path) -> TemplateManifest:
    manifest_path = dir_path / "template.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TemplateError(f"No template.json in {dir_path}") from exc
    except json.JSONDecodeError as exc:
        raise TemplateError(f"template.json in {dir_path} is not valid JSON: {exc}") from exc

    manifest_id = raw.get("id")
    if not manifest_id:
        raise TemplateError(f"template.json in {dir_path} is missing 'id'")

    for required_key in ("display_name", "description", "version", "page", "fonts"):
        if required_key not in raw:
            raise TemplateError(f"Template {manifest_id}: missing required key '{required_key}'")

    _validate_fractions(raw, manifest_id)
    _validate_colors(raw, manifest_id)
    _validate_assets(raw, dir_path, manifest_id)

    return TemplateManifest(
        id=manifest_id,
        display_name=raw["display_name"],
        description=raw["description"],
        version=raw["version"],
        dir_path=dir_path,
        raw=raw,
    )


def validate_manifest_strict(manifest: TemplateManifest) -> None:
    for block_file in (
        "statement_intro.txt",
        "statement_closing.txt",
        "cover_letter.txt",
        "email_subject.txt",
    ):
        text = manifest.read_block(block_file)
        if text:
            render(text, _SYNTHETIC_DONOR_FIELDS, strict=True)
    body = manifest.read_block("email_body.html")
    if body:
        fields = dict(_SYNTHETIC_DONOR_FIELDS)
        fields["Cover Letter"] = "<p>Sample</p>"
        render(body, fields, strict=True, html_context=True)


def discover_templates(templates_dir: Path) -> list[TemplateManifest]:
    manifests = []
    for child in sorted(templates_dir.iterdir()):
        if not child.is_dir():
            continue
        manifest_json = child / "template.json"
        if not manifest_json.exists():
            continue
        manifest = load_manifest(child)
        validate_manifest_strict(manifest)
        manifests.append(manifest)
    return manifests


def get_template(templates_dir: Path, template_id: str) -> TemplateManifest:
    manifest = load_manifest(templates_dir / template_id)
    validate_manifest_strict(manifest)
    return manifest
