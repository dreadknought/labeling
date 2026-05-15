#!/usr/bin/env python3
# path: /mnt/data/generate_code128_labels.py

from __future__ import annotations

import argparse
import json
import re
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pandas as pd
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
)
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.graphics import renderPDF
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

# -----------------------------
# Defaults tuned to your labels
# -----------------------------
DEFAULT_LABEL_WIDTH_IN = 1.88
DEFAULT_LABEL_HEIGHT_IN = 0.85
DEFAULT_PAGE_WIDTH_IN = 8.5
DEFAULT_PAGE_HEIGHT_IN = 11.0
DEFAULT_MARGIN_LEFT_IN = 0.25
DEFAULT_MARGIN_RIGHT_IN = 0.25
DEFAULT_MARGIN_TOP_IN = 0.25
DEFAULT_MARGIN_BOTTOM_IN = 0.25
DEFAULT_COA_DOMAIN = "coa.dthemp.com"
CUT_SENTINEL_CMYK = (0, 1, 0, 0)
FONT_NAME = "Helvetica"
FONT_NAME_BOLD = "Helvetica-Bold"


def inches_to_points(value_in: float) -> float:
    return value_in * 72.0


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if pd.isna(value):
        return True
    return str(value).strip() == ""


def truthy_active(value: Any) -> bool:
    """
    Treat common CSV truthy values as active.

    This is intentionally permissive because export formats tend to drift.
    """
    if pd.isna(value):
        return False

    s = str(value).strip().lower()
    if s in {"1", "1.0", "true", "yes", "y"}:
        return True
    if s in {"0", "0.0", "false", "no", "n", ""}:
        return False
    return True


def slugify(text: str) -> str:
    s = (text or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "label"


def clean_product_name(name: str) -> str:
    s = (name or "").strip()
    s = s.replace("—", "–")
    s = re.sub(r"^\s*BASE\s*[–-]\s*BASE\s*[–-]\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*-\s*Quarter(?=\s*\(1/4 oz\))", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def drop_weight_suffix(name: str) -> str:
    return re.sub(r"\s*\((?:1/16|1/8|1/4|1)\s+oz\)\s*$", "", name or "", flags=re.IGNORECASE).strip()


def parse_tags(tags_raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not tags_raw:
        return out

    for token in str(tags_raw).split(";"):
        token = token.strip()
        if not token or "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key:
            out[key] = value
    return out


def normalize_thc_value(thc_raw: str) -> str:
    if not thc_raw:
        return ""
    s = str(thc_raw).strip()
    if not s:
        return ""
    if s.endswith("%"):
        s = s[:-1].strip()
    return s




def highest_indexed_thc_value(tags_raw: str) -> str:
    tag_map = parse_tags(tags_raw)
    best: tuple[int, str] | None = None

    for key, value in tag_map.items():
        match = re.fullmatch(r"coa_ref_(\d+)_thc", key)
        if not match:
            continue

        thc = normalize_thc_value(value)
        if not thc:
            continue

        idx = int(match.group(1))
        if best is None or idx > best[0]:
            best = (idx, thc)

    return best[1] if best else ""


def find_key_recursive(obj: Any, wanted_key: str) -> Optional[str]:
    wanted_key = wanted_key.lower()

    if isinstance(obj, dict):
        for key, value in obj.items():
            if str(key).strip().lower() == wanted_key:
                if value is None:
                    return None
                return str(value).strip()
        for value in obj.values():
            found = find_key_recursive(value, wanted_key)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_key_recursive(item, wanted_key)
            if found:
                return found

    return None


def extract_thc_value(tags_raw: str, legacy_thc_raw: str = "") -> str:
    """
    Prefer the top-level thc= tag.
    Fall back to legacy JSON-in-tags if it exists.
    Fall back again to the explicit THC CSV column if present.
    """
    indexed_thc = highest_indexed_thc_value(tags_raw)
    if indexed_thc:
        return indexed_thc

    tag_map = parse_tags(tags_raw)
    thc_raw = (tag_map.get("thc") or "").strip()
    if thc_raw:
        return normalize_thc_value(thc_raw)

    json_blob = (tag_map.get("json") or "").strip()
    if json_blob:
        try:
            parsed = json.loads(json_blob)
        except Exception:
            parsed = None
        if parsed is not None:
            embedded_thc = find_key_recursive(parsed, "thc")
            if embedded_thc:
                return normalize_thc_value(embedded_thc)

    if legacy_thc_raw:
        return normalize_thc_value(legacy_thc_raw)
    return ""


def make_line2_text(thc_value: str, coa_domain: str) -> str:
    thc_value = (thc_value or "").strip()
    if thc_value:
        return f"{thc_value}% CoA: {coa_domain}"
    return f"CoA: {coa_domain}"


# -----------------------------
# PDF CutContour patching
# -----------------------------

def patch_cutcontour(pdf_path: Path) -> None:
    """
    Convert the sentinel CMYK stroke into a true /CutContour spot color.

    ReportLab makes it easy to draw the path, but not to emit the exact spot
    color separation we need. So we patch the generated PDF after the fact.
    """
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()

    for page in reader.pages:
        resources = page.get("/Resources")
        if resources is None:
            writer.add_page(page)
            continue

        cs_obj = resources.get("/ColorSpace")
        if cs_obj is None:
            cs_dict = DictionaryObject()
            resources[NameObject("/ColorSpace")] = cs_dict
        else:
            cs_dict = cs_obj.get_object()

        func = DictionaryObject(
            {
                NameObject("/FunctionType"): NumberObject(2),
                NameObject("/Domain"): ArrayObject([FloatObject(0), FloatObject(1)]),
                NameObject("/C0"): ArrayObject([FloatObject(0), FloatObject(0), FloatObject(0), FloatObject(0)]),
                NameObject("/C1"): ArrayObject([FloatObject(0), FloatObject(1), FloatObject(0), FloatObject(0)]),
                NameObject("/N"): NumberObject(1),
            }
        )
        cs_array = ArrayObject(
            [
                NameObject("/Separation"),
                NameObject("/CutContour"),
                NameObject("/DeviceCMYK"),
                func,
            ]
        )
        cs_dict[NameObject("/CS0")] = cs_array

        contents = page.get("/Contents")
        if contents is None:
            writer.add_page(page)
            continue

        contents_obj = contents.get_object() if hasattr(contents, "get_object") else contents
        if isinstance(contents_obj, ArrayObject):
            data_bytes = b""
            for obj in contents_obj:
                stream = obj.get_object() if hasattr(obj, "get_object") else obj
                data_bytes += stream.get_data()
        else:
            data_bytes = contents_obj.get_data()

        s = data_bytes.decode("latin1")
        patterns = ["0 100 0 0 K", "0 1 0 0 K", "0 100 0 0 k", "0 1 0 0 k"]
        replaced = False
        s_new = s
        for pat in patterns:
            if pat in s_new:
                s_new = s_new.replace(pat, "/CS0 CS\n1 SCN")
                replaced = True

        if not replaced:
            writer.add_page(page)
            continue

        new_stream = DecodedStreamObject()
        new_stream.set_data(s_new.encode("latin1"))
        page[NameObject("/Contents")] = new_stream
        writer.add_page(page)

    if reader.metadata:
        writer.add_metadata(reader.metadata)

    with pdf_path.open("wb") as f:
        writer.write(f)


# -----------------------------
# Barcode generation and layout
# -----------------------------

def build_code128_drawing(
    value: str,
    quiet: bool = True,
):
    """
    Build a vector Code 128 drawing.

    This avoids ReportLab's renderPM raster backend entirely, so it works
    in environments that do not have rlPyCairo or _rl_renderPM installed.
    """
    return createBarcodeDrawing(
        "Code128",
        value=value,
        barHeight=60,
        barWidth=1,
        humanReadable=False,
        quiet=quiet,
    )


def draw_code128_within(
    c: canvas.Canvas,
    value: str,
    x: float,
    y: float,
    max_w: float,
    max_h: float,
    barcode_scale: float = 1.0,
    quiet: bool = True,
) -> None:
    """
    Draw a vector Code 128 barcode using the full available width.

    Important behavior:
    - Scale from width only.
    - Preserve aspect ratio.
    - Center vertically within the requested box.
    - Clip to the requested box so any extra height is cut off at the top/bottom.
    """
    if max_w <= 0 or max_h <= 0:
        return

    drawing = build_code128_drawing(value=value, quiet=quiet)
    src_w = float(getattr(drawing, "width", 0.0) or 0.0)
    src_h = float(getattr(drawing, "height", 0.0) or 0.0)
    if src_w <= 0 or src_h <= 0:
        return

    # Width-only scaling so the barcode uses the full cut width.
    sx = max_w / src_w
    sy = sx * barcode_scale

    draw_w = src_w * sx
    draw_h = src_h * sy

    # Center vertically in the available area.
    draw_x = x
    draw_y = y + (max_h - draw_h) / 2.0

    c.saveState()

    # Clip to the barcode area so excess height gets chopped off
    # at the top and bottom instead of shrinking the barcode.
    clip_path = c.beginPath()
    clip_path.rect(x, y, max_w, max_h)
    c.clipPath(clip_path, stroke=0, fill=0)

    c.translate(draw_x, draw_y)
    c.scale(sx, sy)
    renderPDF.draw(drawing, c, 0, 0)

    c.restoreState()

def fit_size_within(src_w: float, src_h: float, max_w: float, max_h: float) -> tuple[float, float]:
    if src_w <= 0 or src_h <= 0 or max_w <= 0 or max_h <= 0:
        return 0.0, 0.0
    scale = min(max_w / src_w, max_h / src_h)
    return src_w * scale, src_h * scale


def find_best_font_size_for_width(
    c: canvas.Canvas,
    text: str,
    font_name: str,
    max_width: float,
    max_size: float,
    min_size: float,
) -> float:
    if not text:
        return min_size
    size = max_size
    while size >= min_size:
        if c.stringWidth(text, font_name, size) <= max_width:
            return size
        size -= 0.25
    return min_size


def truncate_text_to_width(
    c: canvas.Canvas,
    text: str,
    font_name: str,
    font_size: float,
    max_width: float,
) -> str:
    if c.stringWidth(text, font_name, font_size) <= max_width:
        return text
    ellipsis = "..."
    ellipsis_w = c.stringWidth(ellipsis, font_name, font_size)
    out = text
    while out:
        out = out[:-1]
        if c.stringWidth(out, font_name, font_size) + ellipsis_w <= max_width:
            return out.rstrip() + ellipsis
    return ellipsis


def draw_cutcontour_path(c: canvas.Canvas, x: float, y: float, w: float, h: float, radius: float, stroke_width: float, rounded: bool) -> None:
    c.saveState()
    c.setLineWidth(stroke_width)
    c.setStrokeColorCMYK(*CUT_SENTINEL_CMYK)
    if rounded:
        c.roundRect(x, y, w, h, radius, stroke=1, fill=0)
    else:
        c.rect(x, y, w, h, stroke=1, fill=0)
    c.restoreState()


def draw_guide_rect(c: canvas.Canvas, x: float, y: float, w: float, h: float, stroke_width: float, gray: float, rounded: bool, radius: float) -> None:
    c.saveState()
    c.setStrokeColorRGB(gray, gray, gray)
    c.setLineWidth(stroke_width)
    if rounded:
        c.roundRect(x, y, w, h, radius, stroke=1, fill=0)
    else:
        c.rect(x, y, w, h, stroke=1, fill=0)
    c.restoreState()


def draw_label(
    c: canvas.Canvas,
    x: float,
    y: float,
    label_w: float,
    label_h: float,
    barcode_value: str,
    line1_text: str,
    line2_text: str,
    outline_mode: str,
    outline_shape: str,
    outline_stroke_pt: float,
    outline_gray: float,
    text_band_ratio: float,
    text_gap_pt: float,
    pad_pt: float,
    barcode_scale: float,
) -> None:
    """
    Draw one finished label.

    Important: the text band is a real dedicated strip at the bottom.
    We do not draw the barcode under it, which avoids the old dashed artifacts.
    """
    rounded = outline_shape == "round"
    outline_radius = 6.0 if rounded else 0.0

    if outline_mode == "cutcontour":
        draw_cutcontour_path(c, x + 1.0, y + 1.0, label_w - 2.0, label_h - 2.0, radius=outline_radius, stroke_width=outline_stroke_pt, rounded=rounded)
    elif outline_mode == "guide":
        draw_guide_rect(c, x + 1.0, y + 1.0, label_w - 2.0, label_h - 2.0, stroke_width=outline_stroke_pt, gray=outline_gray, rounded=rounded, radius=outline_radius)

    content_x = x + pad_pt
    content_y = y + pad_pt
    content_w = max(0.0, label_w - 2 * pad_pt)
    content_h = max(0.0, label_h - 2 * pad_pt)

    strip_h = max(16.0, min(26.0, content_h * text_band_ratio))
    strip_x = content_x
    strip_y = content_y
    strip_w = content_w

    barcode_area_x = content_x
    barcode_area_y = content_y + strip_h + text_gap_pt
    barcode_area_w = content_w
    barcode_area_h = max(0.0, content_h - strip_h - text_gap_pt)

    if barcode_area_w > 0 and barcode_area_h > 0:
        draw_code128_within(
            c,
            value=barcode_value,
            x=barcode_area_x,
            y=barcode_area_y,
            max_w=barcode_area_w,
            max_h=barcode_area_h,
            barcode_scale=barcode_scale,
        )

    # Explicit white text strip so nothing can show through behind the text.
    c.saveState()
    c.setFillColorRGB(1, 1, 1)
    c.rect(strip_x, strip_y, strip_w, strip_h, stroke=0, fill=1)
    c.restoreState()

    text_w = max(0.0, strip_w - 2.0)
    line1_text = (line1_text or "").strip()
    line2_text = (line2_text or "").strip()

    line1_font_size = find_best_font_size_for_width(
        c,
        line1_text,
        FONT_NAME_BOLD,
        max_width=text_w,
        max_size=6.8,
        min_size=4.25,
    )
    line1_text = truncate_text_to_width(c, line1_text, FONT_NAME_BOLD, line1_font_size, text_w)

    if line2_text:
        line2_font_size = find_best_font_size_for_width(
            c,
            line2_text,
            FONT_NAME,
            max_width=text_w,
            max_size=5.2,
            min_size=3.4,
        )
        line2_text = truncate_text_to_width(c, line2_text, FONT_NAME, line2_font_size, text_w)
    else:
        line2_font_size = 0.0

    if line2_text:
        line1_y = strip_y + strip_h * 0.62
        line2_y = strip_y + strip_h * 0.20
    else:
        line1_y = strip_y + strip_h * 0.32
        line2_y = None

    c.saveState()
    c.setFillColorRGB(0, 0, 0)
    c.setFont(FONT_NAME_BOLD, line1_font_size)
    c.drawCentredString(strip_x + strip_w / 2.0, line1_y, line1_text)
    if line2_text and line2_y is not None:
        c.setFont(FONT_NAME, line2_font_size)
        c.drawCentredString(strip_x + strip_w / 2.0, line2_y, line2_text)
    c.restoreState()


# -----------------------------
# Row selection and enrichment
# -----------------------------

def read_csv(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    lowered = {col.lower().strip(): col for col in df.columns}
    required = ["sku", "name"]
    for req in required:
        if req not in lowered:
            raise SystemExit(f"CSV is missing required column: {req}")
    return df


def col(df: pd.DataFrame, logical_name: str) -> str:
    lookup = {c.lower().strip(): c for c in df.columns}
    if logical_name not in lookup:
        raise KeyError(logical_name)
    return lookup[logical_name]


def optional_col(df: pd.DataFrame, logical_name: str) -> Optional[str]:
    lookup = {c.lower().strip(): c for c in df.columns}
    return lookup.get(logical_name)


def filter_rows(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    sku_col = col(df, "sku")
    name_col = col(df, "name")
    active_col = optional_col(df, "active")
    composite_col = optional_col(df, "composite_sku")
    product_category_col = optional_col(df, "product_category")

    working = df.copy()

    if args.require_active and active_col:
        working = working[working[active_col].map(truthy_active)]

    if args.require_empty_composite and composite_col:
        working = working[working[composite_col].map(is_blank)]

    if args.category_prefix:
        prefixes = [p.strip().lower() for p in args.category_prefix if p.strip()]
        if not product_category_col:
            raise SystemExit("--category-prefix was used but the CSV has no product_category column")
        working = working[
            working[product_category_col]
            .astype(str)
            .str.strip()
            .str.lower()
            .map(lambda value: any(value.startswith(prefix) for prefix in prefixes))
        ]

    if args.sku_regex:
        sku_re = re.compile(args.sku_regex)
        working = working[working[sku_col].astype(str).map(lambda s: bool(sku_re.fullmatch(s.strip())))]

    if args.name_regex:
        name_re = re.compile(args.name_regex, re.IGNORECASE)
        working = working[working[name_col].astype(str).map(lambda s: bool(name_re.search(s.strip())))]

    if args.sort_by_name:
        working = working.sort_values(name_col, kind="stable")
    else:
        working = working.sort_values(sku_col, kind="stable")

    return working.reset_index(drop=True)


def build_records(df: pd.DataFrame) -> list[dict[str, str]]:
    sku_col = col(df, "sku")
    name_col = col(df, "name")
    tags_col = optional_col(df, "tags")
    thc_col = optional_col(df, "thc")

    records: list[dict[str, str]] = []
    for _, row in df.iterrows():
        sku = str(row[sku_col]).strip()
        name = clean_product_name(str(row[name_col]).strip())
        tags_raw = str(row[tags_col]).strip() if tags_col else ""
        legacy_thc_raw = str(row[thc_col]).strip() if thc_col else ""
        thc_value = extract_thc_value(tags_raw, legacy_thc_raw)

        records.append(
            {
                "sku": sku,
                "name": name,
                "name_without_weight": drop_weight_suffix(name),
                "thc_value": thc_value,
            }
        )
    return records


def pair_flower_records(records: list[dict[str, str]]) -> list[dict[str, Any]]:
    """
    Pair E and Q labels by base stem for sheet mode.

    Example:
      FLBISCE + FLBISCQ -> one pair block with two stacked labels.

    Anything unpaired still gets emitted as a single-item pair block.
    """
    grouped: dict[str, dict[str, dict[str, str]]] = {}

    for rec in records:
        sku = rec["sku"]
        m = re.fullmatch(r"(FL[A-Z]+)([EQ])", sku)
        if not m:
            grouped.setdefault(sku, {})["single"] = rec
            continue

        stem = m.group(1)
        suffix = m.group(2)
        grouped.setdefault(stem, {})[suffix] = rec

    paired: list[dict[str, Any]] = []
    for stem in sorted(grouped):
        bucket = grouped[stem]
        if "single" in bucket:
            paired.append({"top": bucket["single"], "bottom": None})
        else:
            paired.append({"top": bucket.get("E"), "bottom": bucket.get("Q")})
    return paired


# -----------------------------
# Output generation
# -----------------------------

def render_individual_labels(records: list[dict[str, str]], args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    label_w = inches_to_points(args.label_width)
    label_h = inches_to_points(args.label_height)

    created_paths: list[Path] = []
    needs_cutcontour_patch = False

    for rec in records:
        output_name = rec[args.output_name_from]
        if args.output_name_from == "name_without_weight":
            output_stem = slugify(output_name)
        else:
            output_stem = output_name

        pdf_path = out_dir / f"{output_stem}.pdf"
        c = canvas.Canvas(str(pdf_path), pagesize=(label_w, label_h))
        draw_label(
            c,
            x=0,
            y=0,
            label_w=label_w,
            label_h=label_h,
            barcode_value=rec["sku"],
            line1_text=rec["name"],
            line2_text=make_line2_text(rec["thc_value"], args.coa_domain),
            outline_mode=args.individual_outline_mode,
            outline_shape=args.outline_shape,
            outline_stroke_pt=args.outline_stroke_pt,
            outline_gray=args.guide_gray,
            text_band_ratio=args.text_band_ratio,
            text_gap_pt=args.text_gap_pt,
            pad_pt=args.content_pad_pt,
            barcode_scale=args.barcode_scale,
        )
        c.showPage()
        c.save()
        created_paths.append(pdf_path)

        if args.individual_outline_mode == "cutcontour":
            needs_cutcontour_patch = True

    if needs_cutcontour_patch:
        for pdf_path in created_paths:
            patch_cutcontour(pdf_path)



def render_sheet_labels(records: list[dict[str, str]], args: argparse.Namespace) -> None:
    out_pdf = Path(args.out_pdf)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)

    page_w = inches_to_points(args.page_width)
    page_h = inches_to_points(args.page_height)
    margin_left = inches_to_points(args.margin_left)
    margin_right = inches_to_points(args.margin_right)
    margin_top = inches_to_points(args.margin_top)
    margin_bottom = inches_to_points(args.margin_bottom)

    label_w = inches_to_points(args.label_width)
    label_h = inches_to_points(args.label_height)

    usable_w = page_w - margin_left - margin_right
    usable_h = page_h - margin_top - margin_bottom
    if usable_w <= 0 or usable_h <= 0:
        raise SystemExit("Page margins leave no usable drawing area")

    if args.pair_flower_sheet:
        blocks = pair_flower_records(records)
        block_w = label_w
        block_h = label_h * 2.0
    else:
        blocks = [{"top": rec, "bottom": None} for rec in records]
        block_w = label_w
        block_h = label_h

    cols = max(1, int(usable_w // block_w))
    rows = max(1, int(usable_h // block_h))
    per_page = cols * rows
    if per_page <= 0:
        raise SystemExit("Labels do not fit on the page with the current size and margins")

    c = canvas.Canvas(str(out_pdf), pagesize=(page_w, page_h))
    needs_cutcontour_patch = args.sheet_outline_mode == "cutcontour"

    for idx, block in enumerate(blocks):
        slot = idx % per_page
        page_index = idx // per_page
        if idx > 0 and slot == 0:
            c.showPage()

        col_idx = slot % cols
        row_idx = slot // cols

        x = margin_left + col_idx * block_w
        y_top = page_h - margin_top - row_idx * block_h
        y = y_top - block_h

        # Light guide around the whole stacked pair block if requested.
        if args.sheet_outline_mode == "guide":
            draw_guide_rect(
                c,
                x=x + 1.0,
                y=y + 1.0,
                w=block_w - 2.0,
                h=block_h - 2.0,
                stroke_width=args.outline_stroke_pt,
                gray=args.guide_gray,
                rounded=False,
                radius=0.0,
            )
        elif args.sheet_outline_mode == "cutcontour":
            draw_cutcontour_path(
                c,
                x=x + 1.0,
                y=y + 1.0,
                w=block_w - 2.0,
                h=block_h - 2.0,
                radius=0.0,
                stroke_width=args.outline_stroke_pt,
                rounded=False,
            )

        top = block.get("top")
        bottom = block.get("bottom")

        if top:
            draw_label(
                c,
                x=x,
                y=y + (label_h if args.pair_flower_sheet else 0.0),
                label_w=label_w,
                label_h=label_h,
                barcode_value=top["sku"],
                line1_text=top["name"],
                line2_text=make_line2_text(top["thc_value"], args.coa_domain),
                outline_mode="none",
                outline_shape=args.outline_shape,
                outline_stroke_pt=args.outline_stroke_pt,
                outline_gray=args.guide_gray,
                text_band_ratio=args.text_band_ratio,
                text_gap_pt=args.text_gap_pt,
                pad_pt=args.content_pad_pt,
                barcode_scale=args.barcode_scale,
            )

        if bottom:
            draw_label(
                c,
                x=x,
                y=y,
                label_w=label_w,
                label_h=label_h,
                barcode_value=bottom["sku"],
                line1_text=bottom["name"],
                line2_text=make_line2_text(bottom["thc_value"], args.coa_domain),
                outline_mode="none",
                outline_shape=args.outline_shape,
                outline_stroke_pt=args.outline_stroke_pt,
                outline_gray=args.guide_gray,
                text_band_ratio=args.text_band_ratio,
                text_gap_pt=args.text_gap_pt,
                pad_pt=args.content_pad_pt,
                barcode_scale=args.barcode_scale,
            )

    c.showPage()
    c.save()

    if needs_cutcontour_patch:
        patch_cutcontour(out_pdf)


# -----------------------------
# CLI
# -----------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate Code 128 barcode labels directly from a CSV. "
            "This single script can handle your flower individual labels, flower packed pages, "
            "and category-based labels like edibles by changing CLI params."
        )
    )

    parser.add_argument("csv", help="CSV file containing at least sku and name columns")

    # Output mode
    parser.add_argument("--sheet-mode", action="store_true", help="Pack labels onto a page instead of creating one PDF per label")
    parser.add_argument("--out-dir", help="Directory for one-PDF-per-label output")
    parser.add_argument("--out-pdf", help="Output PDF path for sheet mode")

    # Row filtering
    parser.add_argument("--category-prefix", action="append", default=[], help="Keep rows whose product_category starts with this prefix. Repeatable.")
    parser.add_argument("--sku-regex", help="Keep rows whose SKU fully matches this regex")
    parser.add_argument("--name-regex", help="Keep rows whose name matches this regex")
    parser.add_argument("--require-active", action="store_true", default=True, help="Require active rows when an active column exists (default: on)")
    parser.add_argument("--no-require-active", dest="require_active", action="store_false", help="Do not filter on active")
    parser.add_argument("--require-empty-composite", action="store_true", default=True, help="Require composite_sku to be blank when that column exists (default: on)")
    parser.add_argument("--no-require-empty-composite", dest="require_empty_composite", action="store_false", help="Do not filter on composite_sku")
    parser.add_argument("--sort-by-name", action="store_true", help="Sort output by product name instead of SKU")

    # Output naming for individual mode
    parser.add_argument(
        "--output-name-from",
        choices=["name_without_weight", "name", "sku"],
        default="name_without_weight",
        help="How to name individual output PDFs",
    )

    # Label and page geometry
    parser.add_argument("--label-width", type=float, default=DEFAULT_LABEL_WIDTH_IN, help="Label width in inches")
    parser.add_argument("--label-height", type=float, default=DEFAULT_LABEL_HEIGHT_IN, help="Label height in inches")
    parser.add_argument("--page-width", type=float, default=DEFAULT_PAGE_WIDTH_IN, help="Sheet page width in inches")
    parser.add_argument("--page-height", type=float, default=DEFAULT_PAGE_HEIGHT_IN, help="Sheet page height in inches")
    parser.add_argument("--margin-left", type=float, default=DEFAULT_MARGIN_LEFT_IN, help="Sheet left margin in inches")
    parser.add_argument("--margin-right", type=float, default=DEFAULT_MARGIN_RIGHT_IN, help="Sheet right margin in inches")
    parser.add_argument("--margin-top", type=float, default=DEFAULT_MARGIN_TOP_IN, help="Sheet top margin in inches")
    parser.add_argument("--margin-bottom", type=float, default=DEFAULT_MARGIN_BOTTOM_IN, help="Sheet bottom margin in inches")

    # Outline behavior
    parser.add_argument(
        "--individual-outline-mode",
        choices=["none", "guide", "cutcontour"],
        default="none",
        help="Outline mode for individual labels",
    )
    parser.add_argument(
        "--sheet-outline-mode",
        choices=["none", "guide", "cutcontour"],
        default="guide",
        help="Outline mode for each packed block in sheet mode",
    )
    parser.add_argument(
        "--outline-shape",
        choices=["rect", "round"],
        default="rect",
        help="Outline shape for individual labels; sheet block outline is always rectangular",
    )
    parser.add_argument("--outline-stroke-pt", type=float, default=0.6, help="Outline stroke width in points")
    parser.add_argument("--guide-gray", type=float, default=0.75, help="Gray level for visible guide outlines; 0 is black, 1 is white")

    # Flower sheet behavior
    parser.add_argument(
        "--pair-flower-sheet",
        action="store_true",
        help="In sheet mode, pair FL...E over FL...Q into one stacked block",
    )

    # Barcode/text layout tuning
    parser.add_argument("--text-band-ratio", type=float, default=0.26, help="Fraction of the label content height reserved for the bottom text band")
    parser.add_argument("--text-gap-pt", type=float, default=1.5, help="Gap in points between the barcode area and the text band")
    parser.add_argument("--content-pad-pt", type=float, default=3.0, help="Inner padding in points around label content")
    parser.add_argument("--barcode-scale", type=float, default=1.0, help="Scale limit for barcode height inside its available area")
    parser.add_argument("--barcode-px-width", type=int, default=900, help="Raster width used to generate the intermediate barcode image")
    parser.add_argument("--barcode-px-height", type=int, default=240, help="Raster height used to generate the intermediate barcode image")

    # Misc metadata
    parser.add_argument("--coa-domain", default=DEFAULT_COA_DOMAIN, help="CoA domain shown on the second text line")
    parser.add_argument("--print-summary", action="store_true", help="Print the selected rows before rendering")

    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.sheet_mode:
        if not args.out_pdf:
            raise SystemExit("--sheet-mode requires --out-pdf")
    else:
        if not args.out_dir:
            raise SystemExit("Individual mode requires --out-dir")

    if args.label_width <= 0 or args.label_height <= 0:
        raise SystemExit("Label dimensions must be positive")
    if args.page_width <= 0 or args.page_height <= 0:
        raise SystemExit("Page dimensions must be positive")
    if not (0.0 <= args.guide_gray <= 1.0):
        raise SystemExit("--guide-gray must be between 0 and 1")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    validate_args(args)

    csv_path = Path(args.csv)
    df = read_csv(csv_path)
    filtered = filter_rows(df, args)
    records = build_records(filtered)

    if not records:
        raise SystemExit("No rows matched the requested filters")

    if args.print_summary:
        for rec in records:
            print(f"{rec['sku']}\t{rec['name']}")
        print(f"Selected rows: {len(records)}")

    if args.sheet_mode:
        render_sheet_labels(records, args)
    else:
        render_individual_labels(records, args)


if __name__ == "__main__":
    main()
