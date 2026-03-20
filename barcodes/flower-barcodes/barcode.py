#!/usr/bin/env python3
# path: barcode.py

from __future__ import annotations

import argparse
import json
import math
import re
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pandas as pd
from PIL import Image, ImageSequence
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
)
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

DEFAULT_LABEL_WIDTH_IN = 1.88
DEFAULT_LABEL_HEIGHT_IN = 0.85

DEFAULT_PAGE_WIDTH_IN = 8.5
DEFAULT_PAGE_HEIGHT_IN = 11.0

# Reasonable default "safe" margins for typical office/laser/inkjet printers.
# User can override these on the CLI.
DEFAULT_MARGIN_LEFT_IN = 0.25
DEFAULT_MARGIN_RIGHT_IN = 0.25
DEFAULT_MARGIN_TOP_IN = 0.25
DEFAULT_MARGIN_BOTTOM_IN = 0.25

CUT_SENTINEL_CMYK = (0, 1, 0, 0)

FONT_NAME = "Helvetica"
FONT_NAME_BOLD = "Helvetica-Bold"

COA_DOMAIN = "coa.dthemp.com"


def patch_cutcontour(pdf_path: Path) -> None:
    """
    Post-process the ReportLab PDF so strokes use a real
    Separation spot color named /CutContour instead of plain CMYK.
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
                NameObject("/C0"): ArrayObject(
                    [FloatObject(0), FloatObject(0), FloatObject(0), FloatObject(0)]
                ),
                NameObject("/C1"): ArrayObject(
                    [FloatObject(0), FloatObject(1), FloatObject(0), FloatObject(0)]
                ),
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

        patterns = [
            "0 100 0 0 K",
            "0 1 0 0 K",
            "0 100 0 0 k",
            "0 1 0 0 k",
        ]

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


def inches_to_points(value_in: float) -> float:
    return value_in * 72.0


def truthy_active(value) -> bool:
    if pd.isna(value):
        return False

    s = str(value).strip().lower()
    if s in {"1", "1.0", "true", "yes", "y"}:
        return True
    if s in {"0", "0.0", "false", "no", "n", ""}:
        return False
    return True


def is_blank(value) -> bool:
    if value is None:
        return True
    if pd.isna(value):
        return True
    return str(value).strip() == ""


def slugify(text: str) -> str:
    s = text.lower().strip()
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
    return re.sub(r"\s*\((?:1/8|1/4)\s+oz\)\s*$", "", name, flags=re.IGNORECASE).strip()


def parse_tags(tags_raw: str) -> Dict[str, str]:
    """
    Parse top-level semicolon-separated tags into a dict of key=value pairs.

    Assumptions guaranteed by caller:
    - top-level tags are separated by semicolons
    - values do not contain semicolons
    - values do not contain additional '=' characters
    """
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
    THC extraction order:
    1. top-level thc=...
    2. embedded json=... payload
    3. legacy THC column fallback
    """
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


def format_thc_text(thc_value: str) -> str:
    if not thc_value:
        return ""
    return f"{thc_value}%"


def make_coa_line(thc_text: str) -> str:
    thc_text = (thc_text or "").strip()
    if thc_text:
        return f"{thc_text} CoA: {COA_DOMAIN}"
    return f"CoA: {COA_DOMAIN}"


def parse_barcode_filename_to_sku(barcode_path: Path) -> tuple[Optional[str], Optional[str]]:
    """
    Example:
      FLBISCE.gif -> ("FLBISCE", "E")
      FLBISCQ.gif -> ("FLBISCQ", "Q")
    """
    stem = barcode_path.stem.strip().upper()
    if not stem.startswith("FL"):
        return None, None
    if not stem.endswith(("E", "Q")):
        return None, None
    return stem, stem[-1]


def load_shared_image(path: Path) -> Image.Image:
    img = Image.open(path)

    if getattr(img, "is_animated", False):
        try:
            img = ImageSequence.Iterator(img).__next__()
        except Exception:
            img.seek(0)

    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img.convert("RGBA"))
        img = bg.convert("RGB")
    else:
        img = img.convert("RGB")

    return img


def image_to_reader(img: Image.Image) -> ImageReader:
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return ImageReader(bio)


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


def detect_columns(header: list[str]) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    lower_map = {h.lower(): h for h in header}

    def find_col(candidates):
        for cand in candidates:
            if cand in lower_map:
                return lower_map[cand]
        return None

    sku_col = find_col(["sku"])
    tags_col = find_col(["tags", "tag", "product tags", "product_tags"])
    thc_col = find_col(["thc", "total thc", "thc content", "thc_content"])
    active_col = find_col(["active", "is active", "is_active", "enabled"])
    composite_col = find_col(["composite_sku"])

    return sku_col, tags_col, thc_col, active_col, composite_col


def load_products(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)

    required = {"sku", "composite_sku", "name", "active"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise SystemExit(f"CSV is missing required columns: {missing}")

    _, tags_col, thc_col, _, _ = detect_columns(list(df.columns))

    df["sku"] = df["sku"].astype(str).str.strip().str.upper()

    sellable = df[
        df["sku"].str.endswith(("E", "Q"), na=False)
        & df["active"].apply(truthy_active)
        & df["composite_sku"].apply(is_blank)
    ].copy()

    sellable["clean_name"] = sellable["name"].astype(str).apply(clean_product_name)
    sellable["strain_name"] = sellable["clean_name"].apply(drop_weight_suffix)

    def extract_thc(row) -> str:
        tags_raw = row.get(tags_col, "") if tags_col else ""
        legacy_thc_raw = row.get(thc_col, "") if thc_col else ""
        return extract_thc_value(tags_raw, legacy_thc_raw)

    sellable["thc"] = sellable.apply(extract_thc, axis=1)

    thc_by_sku = {
        str(row["sku"]).strip().upper(): str(row["thc"]).strip()
        for _, row in sellable.iterrows()
        if str(row.get("thc", "")).strip()
    }

    def backfill_quarter_thc(row) -> str:
        sku = str(row["sku"]).strip().upper()
        thc = str(row.get("thc", "")).strip()

        if thc:
            return thc

        if sku.endswith("Q"):
            eighth_sku = sku[:-1] + "E"
            return thc_by_sku.get(eighth_sku, "")

        return ""

    sellable["thc"] = sellable.apply(backfill_quarter_thc, axis=1)
    sellable["thc_text"] = sellable["thc"].apply(format_thc_text)

    sellable = sellable.drop_duplicates(subset=["sku"], keep="first")
    return sellable


def iter_barcode_files(barcodes_dir: Path) -> Iterable[Path]:
    for path in sorted(barcodes_dir.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file():
            continue
        if path.suffix.lower() != ".gif":
            continue

        sku, size_code = parse_barcode_filename_to_sku(path)
        if not sku or size_code not in {"E", "Q"}:
            continue

        yield path


def draw_rounded_cut_path(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    radius: float,
    stroke_width: float,
) -> None:
    c.saveState()
    c.setLineWidth(stroke_width)
    c.setStrokeColorCMYK(*CUT_SENTINEL_CMYK)
    c.roundRect(x, y, w, h, radius, stroke=1, fill=0)
    c.restoreState()


def draw_pair_bounding_rect(
    c: canvas.Canvas,
    x: float,
    y: float,
    w: float,
    h: float,
    stroke_width: float = 0.75,
) -> None:
    c.saveState()
    c.setStrokeColorRGB(0.7, 0.7, 0.7)
    c.setLineWidth(stroke_width)
    c.rect(x, y, w, h, stroke=1, fill=0)
    c.restoreState()


def draw_label(
    c: canvas.Canvas,
    x: float,
    y: float,
    label_w: float,
    label_h: float,
    product_name: str,
    thc_text: str,
    barcode_img: Image.Image,
    draw_bounding_rect: bool = True,
) -> None:
    cut_inset = 1.0
    cut_radius = 6.0
    cut_stroke = 0.75

    content_pad = 2.5

    if draw_bounding_rect:
        draw_rounded_cut_path(
            c,
            x + cut_inset,
            y + cut_inset,
            label_w - 2 * cut_inset,
            label_h - 2 * cut_inset,
            radius=cut_radius,
            stroke_width=cut_stroke,
        )

    cx = x + cut_inset + content_pad
    cy = y + cut_inset + content_pad
    cw = label_w - 2 * (cut_inset + content_pad)
    ch = label_h - 2 * (cut_inset + content_pad)

    barcode_x = cx
    barcode_y = cy
    barcode_w = cw
    barcode_h = ch

    barcode_reader = image_to_reader(barcode_img)
    b_src_w, b_src_h = barcode_img.size
    b_draw_w, b_draw_h = fit_size_within(b_src_w, b_src_h, barcode_w, barcode_h)

    b_img_x = barcode_x + (barcode_w - b_draw_w) / 2.0
    b_img_y = barcode_y + (barcode_h - b_draw_h) / 2.0

    c.drawImage(
        barcode_reader,
        b_img_x,
        b_img_y,
        width=b_draw_w,
        height=b_draw_h,
        preserveAspectRatio=True,
        mask="auto",
    )

    strip_h = max(16.0, min(24.0, b_draw_h * 0.34))
    strip_y = b_img_y
    strip_x = cx + 0.5
    strip_w = max(0.0, cw - 1.0)

    c.saveState()
    c.setFillColorRGB(1, 1, 1)
    c.rect(strip_x, strip_y, strip_w, strip_h, stroke=0, fill=1)
    c.restoreState()

    name_text = product_name.strip()
    name_max_w = strip_w - 2.0
    name_font_size = find_best_font_size_for_width(
        c,
        name_text,
        FONT_NAME_BOLD,
        max_width=name_max_w,
        max_size=6.8,
        min_size=4.25,
    )

    if c.stringWidth(name_text, FONT_NAME_BOLD, name_font_size) > name_max_w and name_font_size <= 4.25:
        shorter = drop_weight_suffix(name_text)
        if shorter:
            shorter_font_size = find_best_font_size_for_width(
                c,
                shorter,
                FONT_NAME_BOLD,
                max_width=name_max_w,
                max_size=6.8,
                min_size=4.25,
            )
            if c.stringWidth(shorter, FONT_NAME_BOLD, shorter_font_size) <= name_max_w:
                name_text = shorter
                name_font_size = shorter_font_size

    name_text = truncate_text_to_width(
        c,
        name_text,
        FONT_NAME_BOLD,
        name_font_size,
        name_max_w,
    )

    coa_text = make_coa_line(thc_text)
    coa_max_w = strip_w - 2.0
    coa_font_size = find_best_font_size_for_width(
        c,
        coa_text,
        FONT_NAME,
        max_width=coa_max_w,
        max_size=5.2,
        min_size=3.6,
    )
    coa_text = truncate_text_to_width(
        c,
        coa_text,
        FONT_NAME,
        coa_font_size,
        coa_max_w,
    )

    line_gap = 1.4
    total_text_h = name_font_size + coa_font_size + line_gap
    base_y = strip_y + (strip_h - total_text_h) / 2.0

    name_y = base_y + coa_font_size + line_gap
    coa_y = base_y

    c.saveState()
    c.setFillColorRGB(0, 0, 0)

    c.setFont(FONT_NAME_BOLD, name_font_size)
    c.drawCentredString(strip_x + strip_w / 2.0, name_y, name_text)

    c.setFont(FONT_NAME, coa_font_size)
    c.drawCentredString(strip_x + strip_w / 2.0, coa_y, coa_text)

    c.restoreState()


def build_labels(csv_path: Path, barcodes_dir: Path) -> list[dict]:
    sellable = load_products(csv_path)

    by_sku = {}
    for _, row in sellable.iterrows():
        by_sku[str(row["sku"]).strip().upper()] = {
            "sku": str(row["sku"]).strip().upper(),
            "product_name": str(row["clean_name"]).strip(),
            "strain_name": str(row["strain_name"]).strip(),
            "thc_text": str(row.get("thc_text", "") or "").strip(),
        }

    labels = []

    for barcode_path in iter_barcode_files(barcodes_dir):
        sku, size_code = parse_barcode_filename_to_sku(barcode_path)
        if not sku:
            continue

        row = by_sku.get(sku)
        if not row:
            print(f"Skipping barcode with no matching sellable CSV row: {barcode_path.name}")
            continue

        barcode_img = load_shared_image(barcode_path)

        labels.append(
            {
                "sku": sku,
                "size_code": size_code,
                "barcode_path": barcode_path,
                "barcode_img": barcode_img,
                "product_name": row["product_name"],
                "strain_name": row["strain_name"],
                "thc_text": row["thc_text"],
            }
        )

    if not labels:
        raise SystemExit("No matching E/Q barcode labels were found after CSV filtering.")

    return labels


def write_single_label_pdf(
    out_pdf: Path,
    label_w_in: float,
    label_h_in: float,
    product_name: str,
    thc_text: str,
    barcode_img: Image.Image,
    draw_bounding_rect: bool,
) -> None:
    label_w = inches_to_points(label_w_in)
    label_h = inches_to_points(label_h_in)

    c = canvas.Canvas(str(out_pdf), pagesize=(label_w, label_h))
    draw_label(
        c=c,
        x=0,
        y=0,
        label_w=label_w,
        label_h=label_h,
        product_name=product_name,
        thc_text=thc_text,
        barcode_img=barcode_img,
        draw_bounding_rect=draw_bounding_rect,
    )
    c.save()

    if draw_bounding_rect:
        patch_cutcontour(out_pdf)


def write_individual_pdfs(
    labels: list[dict],
    out_dir: Path,
    label_w_in: float,
    label_h_in: float,
    draw_bounding_rect: bool,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    for label in labels:
        sku = label["sku"]
        product_name = label["product_name"]
        strain_name = label["strain_name"]
        thc_text = label["thc_text"]
        barcode_img = label["barcode_img"]

        strain_dir = out_dir / slugify(strain_name)
        strain_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{sku}.pdf"
        out_pdf = strain_dir / filename

        write_single_label_pdf(
            out_pdf=out_pdf,
            label_w_in=label_w_in,
            label_h_in=label_h_in,
            product_name=product_name,
            thc_text=thc_text,
            barcode_img=barcode_img,
            draw_bounding_rect=draw_bounding_rect,
        )

        print(f"Wrote: {out_pdf}")


def compute_sheet_layout(
    page_width_in: float,
    page_height_in: float,
    label_width_in: float,
    label_height_in: float,
    margin_left_in: float,
    margin_right_in: float,
    margin_top_in: float,
    margin_bottom_in: float,
) -> dict:
    printable_width_in = page_width_in - margin_left_in - margin_right_in
    printable_height_in = page_height_in - margin_top_in - margin_bottom_in

    if printable_width_in <= 0 or printable_height_in <= 0:
        raise SystemExit("Printable area is zero or negative. Check your margin values.")

    cols = int(printable_width_in // label_width_in)
    rows = int(printable_height_in // label_height_in)

    if cols < 1 or rows < 1:
        raise SystemExit(
            "Label size does not fit within the printable area. "
            "Reduce margins or use smaller label dimensions."
        )

    return {
        "cols": cols,
        "rows": rows,
        "labels_per_page": cols * rows,
        "printable_width_in": printable_width_in,
        "printable_height_in": printable_height_in,
    }


def build_sheet_pairs(labels: list[dict]) -> list[dict]:
    """
    Group labels by strain for sheet mode so the eighth can be placed above
    the quarter for each strain.
    """
    grouped: dict[str, dict[str, Optional[dict]]] = {}

    for label in labels:
        strain_name = str(label["strain_name"]).strip()
        size_code = str(label["size_code"]).strip().upper()

        if strain_name not in grouped:
            grouped[strain_name] = {"E": None, "Q": None}

        if size_code in {"E", "Q"} and grouped[strain_name][size_code] is None:
            grouped[strain_name][size_code] = label

    pairs = []
    for strain_name in sorted(grouped.keys(), key=lambda s: s.lower()):
        entry = grouped[strain_name]
        pairs.append(
            {
                "strain_name": strain_name,
                "E": entry["E"],
                "Q": entry["Q"],
            }
        )

    return pairs


def write_packed_sheet_pdf(
    labels: list[dict],
    out_pdf: Path,
    label_w_in: float,
    label_h_in: float,
    draw_bounding_rect: bool,
    page_width_in: float,
    page_height_in: float,
    margin_left_in: float,
    margin_right_in: float,
    margin_top_in: float,
    margin_bottom_in: float,
) -> None:
    layout = compute_sheet_layout(
        page_width_in=page_width_in,
        page_height_in=page_height_in,
        label_width_in=label_w_in,
        label_height_in=label_h_in,
        margin_left_in=margin_left_in,
        margin_right_in=margin_right_in,
        margin_top_in=margin_top_in,
        margin_bottom_in=margin_bottom_in,
    )

    cols = layout["cols"]
    rows = layout["rows"]

    pair_rows = rows // 2
    if pair_rows < 1:
        raise SystemExit(
            "Sheet mode pairing requires room for at least 2 label rows per column. "
            "Use a shorter label height, larger page, or smaller margins."
        )

    strain_pairs = build_sheet_pairs(labels)
    strain_pairs_per_page = cols * pair_rows

    page_w = inches_to_points(page_width_in)
    page_h = inches_to_points(page_height_in)
    label_w = inches_to_points(label_w_in)
    label_h = inches_to_points(label_h_in)

    margin_left = inches_to_points(margin_left_in)
    margin_top = inches_to_points(margin_top_in)

    out_pdf.parent.mkdir(parents=True, exist_ok=True)

    c = canvas.Canvas(str(out_pdf), pagesize=(page_w, page_h))

    for pair_index, pair in enumerate(strain_pairs):
        index_on_page = pair_index % strain_pairs_per_page

        # Fill down the page first, then move to the next column.
        col = index_on_page // pair_rows
        pair_row = index_on_page % pair_rows

        x = margin_left + (col * label_w)

        y_top = page_h - margin_top - ((pair_row * 2 + 1) * label_h)
        y_bottom = page_h - margin_top - ((pair_row * 2 + 2) * label_h)

        pair_x = x
        pair_y = y_bottom
        pair_w = label_w
        pair_h = label_h * 2.0

        draw_pair_bounding_rect(
            c=c,
            x=pair_x,
            y=pair_y,
            w=pair_w,
            h=pair_h,
        )

        eighth_label = pair.get("E")
        quarter_label = pair.get("Q")

        if eighth_label is not None:
            draw_label(
                c=c,
                x=x,
                y=y_top,
                label_w=label_w,
                label_h=label_h,
                product_name=eighth_label["product_name"],
                thc_text=eighth_label["thc_text"],
                barcode_img=eighth_label["barcode_img"],
                draw_bounding_rect=draw_bounding_rect,
            )

        if quarter_label is not None:
            draw_label(
                c=c,
                x=x,
                y=y_bottom,
                label_w=label_w,
                label_h=label_h,
                product_name=quarter_label["product_name"],
                thc_text=quarter_label["thc_text"],
                barcode_img=quarter_label["barcode_img"],
                draw_bounding_rect=draw_bounding_rect,
            )

        if index_on_page == strain_pairs_per_page - 1 and pair_index != len(strain_pairs) - 1:
            c.showPage()

    c.save()

    if draw_bounding_rect:
        patch_cutcontour(out_pdf)

    total_pages = math.ceil(len(strain_pairs) / strain_pairs_per_page)
    print(
        f"Wrote packed sheet PDF: {out_pdf} "
        f"({cols} cols x {pair_rows} strain-pairs/page = {strain_pairs_per_page} strain-pairs/page, "
        f"{total_pages} page(s))"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate rectangular flower-label PDFs, either one-per-file or packed onto letter-sized sheets."
    )
    parser.add_argument("csv_file", help="Path to Lightspeed export CSV.")
    parser.add_argument(
        "--barcodes-dir",
        required=True,
        help="Directory containing barcode GIFs.",
    )
    parser.add_argument(
        "--out-dir",
        help="Root output directory for individual PDFs.",
    )
    parser.add_argument(
        "--out-pdf",
        help="Output PDF path for packed sheet mode.",
    )
    parser.add_argument(
        "--label-width",
        type=float,
        default=DEFAULT_LABEL_WIDTH_IN,
        help=f"Label width in inches (default: {DEFAULT_LABEL_WIDTH_IN}).",
    )
    parser.add_argument(
        "--label-height",
        type=float,
        default=DEFAULT_LABEL_HEIGHT_IN,
        help=f"Label height in inches (default: {DEFAULT_LABEL_HEIGHT_IN}).",
    )
    parser.add_argument(
        "--sheet-mode",
        action="store_true",
        help="Pack labels onto 8.5x11 sheets instead of writing one PDF per SKU.",
    )
    parser.add_argument(
        "--page-width",
        type=float,
        default=DEFAULT_PAGE_WIDTH_IN,
        help=f"Sheet page width in inches (default: {DEFAULT_PAGE_WIDTH_IN}).",
    )
    parser.add_argument(
        "--page-height",
        type=float,
        default=DEFAULT_PAGE_HEIGHT_IN,
        help=f"Sheet page height in inches (default: {DEFAULT_PAGE_HEIGHT_IN}).",
    )
    parser.add_argument(
        "--margin-left",
        type=float,
        default=DEFAULT_MARGIN_LEFT_IN,
        help=f"Left unprintable margin in inches (default: {DEFAULT_MARGIN_LEFT_IN}).",
    )
    parser.add_argument(
        "--margin-right",
        type=float,
        default=DEFAULT_MARGIN_RIGHT_IN,
        help=f"Right unprintable margin in inches (default: {DEFAULT_MARGIN_RIGHT_IN}).",
    )
    parser.add_argument(
        "--margin-top",
        type=float,
        default=DEFAULT_MARGIN_TOP_IN,
        help=f"Top unprintable margin in inches (default: {DEFAULT_MARGIN_TOP_IN}).",
    )
    parser.add_argument(
        "--margin-bottom",
        type=float,
        default=DEFAULT_MARGIN_BOTTOM_IN,
        help=f"Bottom unprintable margin in inches (default: {DEFAULT_MARGIN_BOTTOM_IN}).",
    )
    parser.add_argument(
        "--no-bounding-rect",
        action="store_true",
        help="Disable the rounded bounding rectangle / CutContour path.",
    )

    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    barcodes_dir = Path(args.barcodes_dir)

    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")
    if not barcodes_dir.exists():
        raise SystemExit(f"Barcodes directory not found: {barcodes_dir}")

    labels = build_labels(csv_path, barcodes_dir)

    if args.sheet_mode:
        if not args.out_pdf:
            raise SystemExit("--sheet-mode requires --out-pdf")
        write_packed_sheet_pdf(
            labels=labels,
            out_pdf=Path(args.out_pdf),
            label_w_in=args.label_width,
            label_h_in=args.label_height,
            draw_bounding_rect=not args.no_bounding_rect,
            page_width_in=args.page_width,
            page_height_in=args.page_height,
            margin_left_in=args.margin_left,
            margin_right_in=args.margin_right,
            margin_top_in=args.margin_top,
            margin_bottom_in=args.margin_bottom,
        )
    else:
        if not args.out_dir:
            raise SystemExit("Individual mode requires --out-dir")
        write_individual_pdfs(
            labels=labels,
            out_dir=Path(args.out_dir),
            label_w_in=args.label_width,
            label_h_in=args.label_height,
            draw_bounding_rect=not args.no_bounding_rect,
        )


if __name__ == "__main__":
    main()