from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from pypdf import PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

from core.models import Donor, Gift, LayoutResult
from core.templates import TemplateManifest
from core.utils import LEADING_FACTOR, POINTS_PER_INCH, format_currency, format_mm_dd

# Page layout


_ALIGN_MAP = {"left": 0, "center": 1, "right": 2, "justify": 4}


def _safe_font_name(font_name: str) -> str:
    try:
        pdfmetrics.getFont(font_name)
        return font_name
    except KeyError:
        return "Helvetica"


def measure_paragraph_height(
    text: str, width_pt: float, font_name: str, font_size: float, align: str = "left"
) -> float:
    if not text.strip():
        return 0.0
    style = ParagraphStyle(
        "measure",
        fontName=_safe_font_name(font_name),
        fontSize=font_size,
        leading=font_size * LEADING_FACTOR,
        alignment=_ALIGN_MAP.get(align, 0),
    )
    xml_text = text.replace("\r\n", "\n").replace("\n", "<br/>")
    paragraph = Paragraph(xml_text, style)
    _, height = paragraph.wrap(width_pt, 1_000_000)
    return height


def measure_image_height(image_path: Path, max_height_in: float, content_width_pt: float) -> float:
    if not image_path.exists():
        return 0.0
    reader = ImageReader(str(image_path))
    img_w, img_h = reader.getSize()
    scaled_h = content_width_pt * (img_h / img_w)
    cap = max_height_in * POINTS_PER_INCH
    return min(scaled_h, cap)


def available_height(
    page_height_pt: float,
    margin_top_pt: float,
    margin_bottom_pt: float,
    header_img_h_pt: float,
    footer_img_h_pt: float,
    intro_h_pt: float,
    closing_h_pt: float,
    ira_footnote_h_pt: float,
    block_gap_pt: float,
    table_gap_pt: float,
) -> float:
    gaps = block_gap_pt * 2 + table_gap_pt
    return (
        page_height_pt
        - margin_top_pt
        - margin_bottom_pt
        - header_img_h_pt
        - footer_img_h_pt
        - intro_h_pt
        - closing_h_pt
        - ira_footnote_h_pt
        - gaps
    )


def _row_height(font_size: float, row_pad_pt: float) -> float:
    return font_size * LEADING_FACTOR + 2 * row_pad_pt


def single_table_height(
    n_rows: int, font_size: float, row_pad_pt: float, show_totals: bool
) -> float:
    row_h = _row_height(font_size, row_pad_pt)
    header_h = row_h
    total_h = row_h if show_totals else 0.0
    return header_h + n_rows * row_h + total_h


def split_table_height(
    n_rows: int, font_size: float, row_pad_pt: float, show_totals: bool
) -> tuple[float, int, int]:
    left_rows = math.ceil(n_rows / 2)
    right_rows = n_rows - left_rows
    row_h = _row_height(font_size, row_pad_pt)
    header_h = row_h
    left_h = header_h + left_rows * row_h
    right_h = (header_h + right_rows * row_h) if right_rows > 0 else 0.0
    body_h = max(left_h, right_h)
    if show_totals:
        body_h += row_h
    return body_h, left_rows, right_rows


@dataclass(frozen=True)
class FitParams:
    font_size: float
    min_font_size: float
    font_step: float
    row_pad_pt: float
    min_row_pad_pt: float
    show_totals: bool = True


def _font_rungs(params: FitParams) -> list[float]:
    rungs = []
    f = params.font_size
    while f >= params.min_font_size - 1e-9:
        rungs.append(round(f, 4))
        f -= params.font_step
    if not rungs or rungs[-1] > params.min_font_size:
        rungs.append(params.min_font_size)
    return rungs


def _pad_rungs(params: FitParams) -> list[float]:
    rungs = []
    p = params.row_pad_pt
    while p >= params.min_row_pad_pt - 1e-9:
        rungs.append(round(p, 4))
        p -= 0.5
    if not rungs or rungs[-1] > params.min_row_pad_pt:
        rungs.append(params.min_row_pad_pt)
    return rungs


def fit_gift_table(n_rows: int, avail_height_pt: float, params: FitParams) -> LayoutResult:
    if n_rows == 0:
        return LayoutResult(
            mode="single", font_size=params.font_size, row_pad_pt=params.row_pad_pt, page_count=1
        )

    for font_size in _font_rungs(params):
        for row_pad_pt in _pad_rungs(params):
            single_h = single_table_height(n_rows, font_size, row_pad_pt, params.show_totals)
            if single_h <= avail_height_pt:
                return LayoutResult(
                    mode="single",
                    font_size=font_size,
                    row_pad_pt=row_pad_pt,
                    page_count=1,
                    measured_heights={"table": single_h},
                )
            split_h, left_rows, right_rows = split_table_height(
                n_rows, font_size, row_pad_pt, params.show_totals
            )
            if split_h <= avail_height_pt:
                return LayoutResult(
                    mode="split",
                    font_size=font_size,
                    row_pad_pt=row_pad_pt,
                    page_count=1,
                    left_rows=left_rows,
                    right_rows=right_rows,
                    measured_heights={"table": split_h},
                )

    min_font = params.min_font_size
    min_pad = params.min_row_pad_pt
    row_h = _row_height(min_font, min_pad)
    header_h = row_h
    total_row_h = row_h if params.show_totals else 0.0

    fittable_body = max(avail_height_pt - header_h - total_row_h, 0.0)
    rows_per_column = max(int(fittable_body // row_h), 0)
    page1_rows = min(n_rows, rows_per_column * 2)
    left_rows = math.ceil(page1_rows / 2)
    right_rows = page1_rows - left_rows
    overflow_rows = n_rows - page1_rows
    if page1_rows == 0:

        overflow_rows = n_rows
        left_rows = right_rows = 0

    return LayoutResult(
        mode="overflow",
        font_size=min_font,
        row_pad_pt=min_pad,
        page_count=2,
        left_rows=left_rows,
        right_rows=right_rows,
        overflow_rows=overflow_rows,
        measured_heights={"page1_rows": page1_rows},
    )


# PDF rendering


_FONTS_REGISTERED = False


def register_fonts(fonts_dir: Path) -> str:
    global _FONTS_REGISTERED
    regular = fonts_dir / "Carlito-Regular.ttf"
    bold = fonts_dir / "Carlito-Bold.ttf"
    italic = fonts_dir / "Carlito-Italic.ttf"
    if regular.exists() and bold.exists() and italic.exists():
        if not _FONTS_REGISTERED:
            pdfmetrics.registerFont(TTFont("Carlito", str(regular)))
            pdfmetrics.registerFont(TTFont("Carlito-Bold", str(bold)))
            pdfmetrics.registerFont(TTFont("Carlito-Italic", str(italic)))
            pdfmetrics.registerFontFamily(
                "Carlito", normal="Carlito", bold="Carlito-Bold", italic="Carlito-Italic"
            )
            _FONTS_REGISTERED = True
        return "Carlito"
    return "Helvetica"


def _resolve_font(manifest: TemplateManifest, fonts_dir: Path) -> str:
    requested = manifest.raw.get("fonts", {}).get("family", "Helvetica")
    if requested == "Carlito":
        return register_fonts(fonts_dir)
    return "Helvetica"


def _in(v: float) -> float:
    return v * POINTS_PER_INCH


@dataclass(frozen=True)
class PageGeometry:
    width_pt: float
    height_pt: float
    margin_top_pt: float
    margin_bottom_pt: float
    margin_left_pt: float
    margin_right_pt: float

    @property
    def content_width_pt(self) -> float:
        return self.width_pt - self.margin_left_pt - self.margin_right_pt


def _page_geometry(manifest: TemplateManifest) -> PageGeometry:
    page = manifest.raw.get("page", {})
    margins = page.get("margin_in", {})
    return PageGeometry(
        width_pt=_in(8.5),
        height_pt=_in(11.0),
        margin_top_pt=_in(margins.get("top", 0.5)),
        margin_bottom_pt=_in(margins.get("bottom", 0.5)),
        margin_left_pt=_in(margins.get("left", 0.75)),
        margin_right_pt=_in(margins.get("right", 0.75)),
    )


def _draw_image_top(
    c: canvas.Canvas, image_path: Path, geo: PageGeometry, top_y: float, max_height_in: float
) -> float:
    if not image_path.exists():
        return 0.0
    reader = ImageReader(str(image_path))
    img_w, img_h = reader.getSize()
    h = measure_image_height(image_path, max_height_in, geo.content_width_pt)
    w = h * (img_w / img_h)
    if w > geo.content_width_pt:
        w = geo.content_width_pt
        h = w * (img_h / img_w)
    x = geo.margin_left_pt + (geo.content_width_pt - w) / 2
    y = top_y - h
    c.drawImage(reader, x, y, width=w, height=h, preserveAspectRatio=True, mask="auto")
    return h


def _paragraph(
    text: str, font_name: str, size: float, color: str, align: str = "left"
) -> Paragraph:
    align_map = {"left": 0, "center": 1, "right": 2, "justify": 4}
    style = ParagraphStyle(
        "body",
        fontName=font_name,
        fontSize=size,
        leading=size * LEADING_FACTOR,
        textColor=HexColor(color),
        alignment=align_map.get(align, 0),
    )
    xml_text = text.replace("\r\n", "\n").replace("\n", "<br/>")
    return Paragraph(xml_text, style)


def _build_gift_table_rows(gifts: tuple[Gift, ...], labels: dict) -> list[list[str]]:
    rows = [
        [
            "",
            labels.get("date", "Date"),
            labels.get("amount", "Gift Amount"),
            labels.get("receipt", "Receipt Amount"),
        ]
    ]
    for g in gifts:
        marker = "*" if g.is_ira else ""
        rows.append(
            [
                marker,
                format_mm_dd(g.date),
                format_currency(g.amount),
                format_currency(g.receipt_amount),
            ]
        )
    return rows


def _draw_table(
    c: canvas.Canvas,
    rows: list[list[str]],
    x: float,
    top_y: float,
    width: float,
    font_size: float,
    row_pad_pt: float,
    style: dict,
    marker_pt: float,
    col_fracs: tuple[float, float, float],
    totals_row: list[str] | None,
) -> float:
    remaining = width - marker_pt
    col_widths = [marker_pt] + [remaining * f for f in col_fracs]
    all_rows = list(rows) + ([totals_row] if totals_row else [])
    table = Table(all_rows, colWidths=col_widths)
    table.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), font_size),
                ("TOPPADDING", (0, 0), (-1, -1), row_pad_pt),
                ("BOTTOMPADDING", (0, 0), (-1, -1), row_pad_pt),
                ("GRID", (0, 0), (-1, -1), 0.5, HexColor(style.get("grid_color", "#c9c9c9"))),
                ("BACKGROUND", (0, 0), (-1, 0), HexColor(style.get("header_bg", "#f0efe9"))),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (2, 0), (3, -1), "RIGHT"),
            ]
        )
    )
    w, h = table.wrap(width, 1_000_000)
    table.drawOn(c, x, top_y - h)
    return h


def build_statement_pdf(
    donor: Donor,
    rendered_intro: str,
    rendered_closing: str,
    manifest: TemplateManifest,
    fonts_dir: Path,
    out_path: Path,
) -> Path:
    font_name = _resolve_font(manifest, fonts_dir)
    geo = _page_geometry(manifest)
    style = manifest.raw
    fonts_cfg = style.get("fonts", {})
    base_size = fonts_cfg.get("base_size", 10.5)
    color = fonts_cfg.get("color", "#1a1a1a")
    spacing = style.get("spacing", {})
    block_gap = spacing.get("block_gap_pt", 10)
    table_gap = spacing.get("table_gap_pt", 12)
    header_cfg = style.get("header_image", {})
    footer_cfg = style.get("footer_image", {})
    gift_cfg = style.get("gift_table", {})
    cols = gift_cfg.get("columns", {})
    col_fracs = (
        cols.get("date_frac", 0.22),
        cols.get("amount_frac", 0.39),
        cols.get("receipt_frac", 0.39),
    )
    marker_pt = cols.get("marker_pt", 10)

    header_path = manifest.asset_path(header_cfg["file"]) if header_cfg.get("file") else None
    footer_path = manifest.asset_path(footer_cfg["file"]) if footer_cfg.get("file") else None
    header_h = (
        measure_image_height(
            header_path, header_cfg.get("max_height_in", 1.1), geo.content_width_pt
        )
        if header_path
        else 0.0
    )
    footer_h = (
        measure_image_height(
            footer_path, footer_cfg.get("max_height_in", 0.8), geo.content_width_pt
        )
        if footer_path
        else 0.0
    )

    intro_h = measure_paragraph_height(
        rendered_intro,
        geo.content_width_pt,
        font_name,
        base_size,
        style.get("text_align", {}).get("intro", "left"),
    )
    closing_h = measure_paragraph_height(
        rendered_closing,
        geo.content_width_pt,
        font_name,
        base_size,
        style.get("text_align", {}).get("closing", "left"),
    )
    ira_footnote_text = style.get("ira_footnote", "") if donor.has_ira else ""
    ira_h = (
        measure_paragraph_height(ira_footnote_text, geo.content_width_pt, font_name, base_size - 1)
        if ira_footnote_text
        else 0.0
    )

    avail = available_height(
        geo.height_pt,
        geo.margin_top_pt,
        geo.margin_bottom_pt,
        header_h,
        footer_h,
        intro_h,
        closing_h,
        ira_h,
        block_gap,
        table_gap,
    )

    params = FitParams(
        font_size=gift_cfg.get("font_size", 9.5),
        min_font_size=gift_cfg.get("min_font_size", 8.0),
        font_step=gift_cfg.get("font_step", 0.5),
        row_pad_pt=gift_cfg.get("row_pad_pt", 3.0),
        min_row_pad_pt=gift_cfg.get("min_row_pad_pt", 1.5),
        show_totals=gift_cfg.get("show_totals", True),
    )
    layout: LayoutResult = fit_gift_table(len(donor.gifts), avail, params)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out_path), pagesize=(geo.width_pt, geo.height_pt))
    labels = gift_cfg.get("header_labels", {})
    all_rows = _build_gift_table_rows(donor.gifts, labels)
    totals_row = None
    if gift_cfg.get("show_totals", True):
        totals_row = [
            "",
            gift_cfg.get("totals_label", "Total"),
            format_currency(donor.total_amount),
            format_currency(donor.total_receipt),
        ]

    def _draw_page(rows_slice: list[list[str]], is_first: bool, is_last: bool, sub_totals_row):
        cursor_y = geo.height_pt - geo.margin_top_pt
        if is_first and header_path:
            drawn = _draw_image_top(
                c, header_path, geo, cursor_y, header_cfg.get("max_height_in", 1.1)
            )
            cursor_y -= drawn + block_gap
        elif not is_first:
            heading = _paragraph(
                f"Statement continued — {donor.addressee}", font_name, base_size, color
            )
            w, h = heading.wrap(geo.content_width_pt, 1_000_000)
            heading.drawOn(c, geo.margin_left_pt, cursor_y - h)
            cursor_y -= h + block_gap

        if is_first and rendered_intro.strip():
            p = _paragraph(
                rendered_intro,
                font_name,
                base_size,
                color,
                style.get("text_align", {}).get("intro", "left"),
            )
            w, h = p.wrap(geo.content_width_pt, 1_000_000)
            p.drawOn(c, geo.margin_left_pt, cursor_y - h)
            cursor_y -= h + block_gap

        if not rows_slice or len(rows_slice) <= 1:
            empty_text = gift_cfg.get("empty_text", "No gifts recorded.")
            p = _paragraph(empty_text, font_name, base_size, color)
            w, h = p.wrap(geo.content_width_pt, 1_000_000)
            p.drawOn(c, geo.margin_left_pt, cursor_y - h)
            cursor_y -= h + table_gap
        elif layout.mode == "split" and is_last:
            gutter = gift_cfg.get("split_divider", {}).get("gutter_pt", 14)
            half_w = (geo.content_width_pt - gutter) / 2
            left_rows = [rows_slice[0]] + rows_slice[1 : 1 + layout.left_rows]
            right_start = 1 + layout.left_rows
            right_rows = [rows_slice[0]] + rows_slice[right_start : right_start + layout.right_rows]
            h_left = _draw_table(
                c,
                left_rows,
                geo.margin_left_pt,
                cursor_y,
                half_w,
                layout.font_size,
                layout.row_pad_pt,
                gift_cfg,
                marker_pt,
                col_fracs,
                None,
            )
            h_right = _draw_table(
                c,
                right_rows,
                geo.margin_left_pt + half_w + gutter,
                cursor_y,
                half_w,
                layout.font_size,
                layout.row_pad_pt,
                gift_cfg,
                marker_pt,
                col_fracs,
                None,
            )
            body_h = max(h_left, h_right)
            cursor_y -= body_h
            if sub_totals_row:
                h_tot = _draw_table(
                    c,
                    [["", "", "", ""]],
                    geo.margin_left_pt,
                    cursor_y,
                    geo.content_width_pt,
                    layout.font_size,
                    layout.row_pad_pt,
                    gift_cfg,
                    marker_pt,
                    col_fracs,
                    sub_totals_row,
                )
                cursor_y -= h_tot
            cursor_y -= table_gap
        else:
            h = _draw_table(
                c,
                rows_slice,
                geo.margin_left_pt,
                cursor_y,
                geo.content_width_pt,
                layout.font_size,
                layout.row_pad_pt,
                gift_cfg,
                marker_pt,
                col_fracs,
                sub_totals_row if is_last else None,
            )
            cursor_y -= h + table_gap

        if is_last and ira_footnote_text:
            p = _paragraph(ira_footnote_text, font_name, base_size - 1, color)
            w, h = p.wrap(geo.content_width_pt, 1_000_000)
            p.drawOn(c, geo.margin_left_pt, cursor_y - h)
            cursor_y -= h + block_gap

        if is_last and rendered_closing.strip():
            p = _paragraph(
                rendered_closing,
                font_name,
                base_size,
                color,
                style.get("text_align", {}).get("closing", "left"),
            )
            w, h = p.wrap(geo.content_width_pt, 1_000_000)
            p.drawOn(c, geo.margin_left_pt, cursor_y - h)

        if is_last and footer_path:
            _draw_image_top(
                c,
                footer_path,
                geo,
                geo.margin_bottom_pt + footer_h,
                footer_cfg.get("max_height_in", 0.8),
            )

    if layout.mode == "overflow":
        split_point = 1 + (len(donor.gifts) - layout.overflow_rows)
        page1 = all_rows[:split_point]
        page2 = [all_rows[0]] + all_rows[split_point:]
        _draw_page(page1, is_first=True, is_last=False, sub_totals_row=None)
        c.showPage()
        _draw_page(page2, is_first=False, is_last=True, sub_totals_row=totals_row)
    else:
        _draw_page(all_rows, is_first=True, is_last=True, sub_totals_row=totals_row)

    c.showPage()
    c.save()
    return out_path


def build_cover_letter_pdf(
    donor: Donor,
    rendered_cover_body: str,
    manifest: TemplateManifest,
    fonts_dir: Path,
    today_long: str,
    out_path: Path,
) -> Path:
    font_name = _resolve_font(manifest, fonts_dir)
    geo = _page_geometry(manifest)
    style = manifest.raw
    fonts_cfg = style.get("fonts", {})
    base_size = fonts_cfg.get("base_size", 10.5)
    color = fonts_cfg.get("color", "#1a1a1a")
    cover_cfg = style.get("cover_letter", {})
    header_cfg = style.get("header_image", {})
    cover_header = manifest.asset_path("assets/cover_header.png")
    header_path = (
        cover_header
        if cover_header.exists()
        else (manifest.asset_path(header_cfg["file"]) if header_cfg.get("file") else None)
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out_path), pagesize=(geo.width_pt, geo.height_pt))
    cursor_y = geo.height_pt - geo.margin_top_pt
    if header_path and header_path.exists():
        drawn = _draw_image_top(c, header_path, geo, cursor_y, header_cfg.get("max_height_in", 1.1))
        cursor_y -= drawn + 12

    if cover_cfg.get("date_line", True):
        p = _paragraph(today_long, font_name, base_size, color)
        w, h = p.wrap(geo.content_width_pt, 1_000_000)
        p.drawOn(c, geo.margin_left_pt, cursor_y - h)
        cursor_y -= h + 12

    addr_lines = [donor.addressee, *donor.address_lines, f"{donor.city}, {donor.state} {donor.zip}"]
    addr_text = "<br/>".join(addr_lines)
    p = Paragraph(
        addr_text,
        ParagraphStyle(
            "addr",
            fontName=font_name,
            fontSize=base_size,
            leading=base_size * LEADING_FACTOR,
            textColor=HexColor(color),
        ),
    )
    w, h = p.wrap(geo.content_width_pt, 1_000_000)
    p.drawOn(c, geo.margin_left_pt, cursor_y - h)
    cursor_y -= h + 12

    if rendered_cover_body.strip():
        p = _paragraph(rendered_cover_body, font_name, base_size, color)
        w, h = p.wrap(geo.content_width_pt, 1_000_000)
        p.drawOn(c, geo.margin_left_pt, cursor_y - h)
        cursor_y -= h + 12

    sig_lines = cover_cfg.get("signature_block", [])
    if sig_lines:
        p = _paragraph("<br/>".join(sig_lines), font_name, base_size, color)
        w, h = p.wrap(geo.content_width_pt, 1_000_000)
        p.drawOn(c, geo.margin_left_pt, cursor_y - h)

    c.showPage()
    c.save()
    return out_path


def build_record_pdf(
    donor: Donor,
    rendered_intro: str,
    rendered_closing: str,
    rendered_cover_body: str,
    manifest: TemplateManifest,
    fonts_dir: Path,
    today_long: str,
    out_dir: Path,
) -> Path:
    statement_path = build_statement_pdf(
        donor, rendered_intro, rendered_closing, manifest, fonts_dir, out_dir / "statement.pdf"
    )
    if not manifest.raw.get("cover_letter", {}).get("enabled", True):
        return statement_path

    cover_path = build_cover_letter_pdf(
        donor, rendered_cover_body, manifest, fonts_dir, today_long, out_dir / "cover.pdf"
    )
    merged_path = out_dir / "record.pdf"
    writer = PdfWriter()
    for path in (cover_path, statement_path):
        writer.append(str(path))
    with open(merged_path, "wb") as f:
        writer.write(f)
    return merged_path


def build_envelope_page(
    c: canvas.Canvas, donor: Donor, manifest: TemplateManifest, font_name: str
) -> None:
    envelope_cfg = manifest.raw.get("envelope", {})
    size = envelope_cfg.get("size_in", {"w": 9.5, "h": 4.125})
    w_pt, h_pt = _in(size["w"]), _in(size["h"])
    c.setPageSize((w_pt, h_pt))
    font_size = envelope_cfg.get("font_size", 10.5)

    if envelope_cfg.get("print_return_address", True):
        pos = envelope_cfg.get("return_pos_in", {"x": 0.45, "y": 0.4})
        lines = envelope_cfg.get("return_address_lines", [])
        text = "<br/>".join(lines)
        p = Paragraph(
            text,
            ParagraphStyle(
                "ret", fontName=font_name, fontSize=font_size, leading=font_size * LEADING_FACTOR
            ),
        )
        pw, ph = p.wrap(_in(3.5), 1_000_000)
        p.drawOn(c, _in(pos["x"]), h_pt - _in(pos["y"]) - ph)

    rpos = envelope_cfg.get("recipient_pos_in", {"x": 3.9, "y": 1.9})
    lines = [donor.addressee, *donor.address_lines, f"{donor.city}, {donor.state} {donor.zip}"]
    text = "<br/>".join(lines)
    p = Paragraph(
        text,
        ParagraphStyle(
            "rec", fontName=font_name, fontSize=font_size, leading=font_size * LEADING_FACTOR
        ),
    )
    pw, ph = p.wrap(_in(4.5), 1_000_000)
    p.drawOn(c, _in(rpos["x"]), h_pt - _in(rpos["y"]) - ph)
    c.showPage()


def build_mail_outputs(
    record_pdf_paths: list[tuple[int, Path]],
    donors_by_row: dict[int, Donor],
    manifest: TemplateManifest,
    fonts_dir: Path,
    out_dir: Path,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    font_name = _resolve_font(manifest, fonts_dir)

    ordered = sorted(record_pdf_paths, key=lambda t: t[0])

    mail_path = out_dir / "statements_mail.pdf"
    writer = PdfWriter()
    for _, path in ordered:
        writer.append(str(path))
    with open(mail_path, "wb") as f:
        writer.write(f)

    envelopes_path = out_dir / "envelopes.pdf"
    c = canvas.Canvas(str(envelopes_path))
    for row_index, _ in ordered:
        build_envelope_page(c, donors_by_row[row_index], manifest, font_name)
    c.save()

    return mail_path, envelopes_path


# Previews


def _write_rgb_png(path: Path, rgb_array) -> None:
    height, width, _ = rgb_array.shape
    raw = bytearray()
    for row in range(height):
        raw.append(0)
        raw.extend(rgb_array[row].tobytes())
    compressed = zlib.compress(bytes(raw), level=6)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b""))


def render_previews(pdf_path: Path, dpi: int = 110, max_pages: int = 3) -> list[Path]:
    pdf_path = Path(pdf_path)
    marker_path = pdf_path.parent / ".preview_dpi"
    cached_dpi = None
    if marker_path.exists():
        try:
            cached_dpi = int(marker_path.read_text().strip())
        except ValueError:
            cached_dpi = None

    doc = pdfium.PdfDocument(str(pdf_path))
    page_count = min(len(doc), max_pages)
    outputs: list[Path] = []
    scale = dpi / 72.0

    for i in range(page_count):
        out_path = pdf_path.parent / f"preview_p{i + 1}.png"
        needs_render = (
            cached_dpi != dpi
            or not out_path.exists()
            or out_path.stat().st_mtime < pdf_path.stat().st_mtime
        )
        if needs_render:
            page = doc[i]
            bitmap = page.render(scale=scale)
            arr = bitmap.to_numpy()[:, :, ::-1]
            _write_rgb_png(out_path, arr)
        outputs.append(out_path)

    marker_path.write_text(str(dpi))
    return outputs
