#!/usr/bin/env python3
# path: circle.py

import argparse
import csv
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

from reportlab.pdfgen import canvas
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.units import inch
from reportlab.lib.colors import CMYKColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        NameObject,
        DictionaryObject,
        ArrayObject,
        NumberObject,
        FloatObject,
        DecodedStreamObject,
    )
except ImportError:
    from PyPDF2 import PdfReader, PdfWriter
    from PyPDF2.generic import (
        NameObject,
        DictionaryObject,
        ArrayObject,
        NumberObject,
        FloatObject,
        DecodedStreamObject,
    )

DEFAULT_DIAMETER_INCH = 1.5


# ---------------- TAG PARSING ---------------- #

def parse_tags(tags_raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not tags_raw:
        return out

    for token in str(tags_raw).split(";"):
        t = token.strip()
        if not t or "=" not in t:
            continue
        k, v = t.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k:
            out[k] = v
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


def extract_indexed_thc_values(tags_raw: str) -> List[Tuple[int, str]]:
    tag_map = parse_tags(tags_raw)
    out: List[Tuple[int, str]] = []

    for key, value in tag_map.items():
        match = re.fullmatch(r"coa_ref_(\d+)_thc", key)
        if not match:
            continue

        thc = normalize_thc_value(value)
        if thc:
            out.append((int(match.group(1)), thc))

    return sorted(out, key=lambda item: item[0])


def highest_indexed_thc_value(tags_raw: str) -> str:
    indexed = extract_indexed_thc_values(tags_raw)
    if not indexed:
        return ""
    return indexed[-1][1]


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


def normalize_weight_grams(weight_raw: str) -> str:
    if not weight_raw:
        return ""
    s = str(weight_raw).strip().lower().replace(" ", "")
    if not s:
        return ""
    if s.endswith("g"):
        s = s[:-1]
    return s.strip()


def is_inactive(active_raw: str) -> bool:
    s = (active_raw or "").strip().lower()
    return s in {"false", "0", "no", "n"}


def is_composite_component_row(row: dict) -> bool:
    return bool((row.get("composite_sku") or "").strip())


def regex_matches(value: str, pattern: Optional[str]) -> bool:
    if not pattern:
        return False
    return bool(re.fullmatch(pattern, (value or "").strip()))


def category_matches_prefix(value: str, prefixes: List[str]) -> bool:
    if not prefixes:
        return True
    folded = (value or "").strip().lower()
    return any(folded.startswith(prefix.strip().lower()) for prefix in prefixes if prefix.strip())


# ---------------- HELPERS ---------------- #

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "label"


def format_inches(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".")


def format_size_tag(diameter_inch: float) -> str:
    return f"{format_inches(diameter_inch)}in"


def normalized_sku_key(sku: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", sku or "").upper()


def lid_sku_suffix_for_diameter(diameter_inch: float) -> Optional[str]:
    if math.isclose(diameter_inch, 1.25, abs_tol=0.001):
        return "E"
    if math.isclose(diameter_inch, 1.5, abs_tol=0.001):
        return "Q"
    return None


def parse_diameters(raw_values: Optional[List[str]]) -> List[float]:
    if not raw_values:
        return [DEFAULT_DIAMETER_INCH]

    out: List[float] = []
    for raw in raw_values:
        for part in str(raw).split(","):
            s = part.strip()
            if not s:
                continue
            try:
                val = float(s)
            except ValueError:
                raise SystemExit(f"Invalid diameter value: {s}")
            if val <= 0:
                raise SystemExit(f"Diameter must be positive: {s}")
            out.append(val)

    if not out:
        return [DEFAULT_DIAMETER_INCH]

    deduped: List[float] = []
    seen = set()
    for val in out:
        key = round(val, 6)
        if key not in seen:
            seen.add(key)
            deduped.append(val)

    return deduped


def clean_product_name_for_lid(text: str) -> str:
    """
    Turn Lightspeed product names into the strain name used on lids.

    Examples:
      BASE – BASE – Lemon Cherry Sherbert (1/8) -> Lemon Cherry Sherbert
      Lemon Cherry Sherbert (1/8 oz) -> Lemon Cherry Sherbert
      Lemon Cherry Sherbert - Quarter (1/4 oz) -> Lemon Cherry Sherbert
      Lemon Cherry Sherbert - Ounce (1 oz) -> Lemon Cherry Sherbert
    """
    if not text:
        return ""

    s = str(text).strip()

    s = re.sub(
        r"^\s*BASE\s*[^A-Za-z0-9]+\s*BASE\s*[^A-Za-z0-9]+\s*",
        "",
        s,
        flags=re.IGNORECASE,
    )

    s = re.sub(r"\s*\([^)]*\)", "", s)

    s = re.sub(
        r"\s*[-–—]\s*(eighth|quarter|ounce|half ounce|half|1/8|1/4|1 oz|1oz)\s*$",
        "",
        s,
        flags=re.IGNORECASE,
    )

    s = re.sub(r"\s+", " ", s).strip()
    return s


def strip_parentheses(text: str) -> str:
    return clean_product_name_for_lid(text)


def is_base_product_name(name: str) -> bool:
    return bool(
        re.match(
            r"^\s*BASE\s*[^A-Za-z0-9]+\s*BASE\s*[^A-Za-z0-9]+",
            name or "",
            flags=re.IGNORECASE,
        )
    )


def sku_candidate_score(
    sku: str,
    raw_name: str,
    handle: str,
    tags_raw: str,
    product_category: str,
    active_raw: str,
) -> int:
    """
    Higher score wins when multiple rows provide an E or Q SKU for the same strain.

    Important rule:
    - Sellable flower SKUs do not have dashes.
    - Base inventory SKUs generally do have dashes, like FL-INTE.

    So a no-dash SKU should beat a dashed SKU for the same strain/size.
    """
    score = 0
    tags = parse_tags(tags_raw)
    handle_l = (handle or "").strip().lower()
    category_l = (product_category or "").strip().lower()
    sku_s = (sku or "").strip()

    if is_inactive(active_raw):
        score -= 1000
    else:
        score += 20

    # This is now the strongest signal.
    # FLINTERSE beats FL-INTE.
    # FLINTERSQ beats FL-INTQ or any dashed quarter/base SKU.
    if "-" not in sku_s:
        score += 1000
    else:
        score -= 500

    if is_base_product_name(raw_name) or handle_l.startswith("base-"):
        score -= 500
    else:
        score += 100

    if tags.get("sellable_composite", "").strip().lower() in {"1", "true", "yes", "y"}:
        score += 300

    if category_l.startswith("flower / eighth") and not is_base_product_name(raw_name):
        score += 50

    if category_l.startswith("flower / quarter") and not is_base_product_name(raw_name):
        score += 50

    if sku_s:
        score += 1

    return score


def remember_best_sku(
    skus_by_strain: dict,
    strain_key: str,
    sku_suffix: str,
    sku: str,
    raw_name: str,
    handle: str,
    tags_raw: str,
    product_category: str,
    active_raw: str,
) -> None:
    if not sku:
        return

    score = sku_candidate_score(
        sku=sku,
        raw_name=raw_name,
        handle=handle,
        tags_raw=tags_raw,
        product_category=product_category,
        active_raw=active_raw,
    )

    skus_by_strain.setdefault(strain_key, {})

    existing = skus_by_strain[strain_key].get(sku_suffix)
    if existing is None or score > existing["score"]:
        skus_by_strain[strain_key][sku_suffix] = {
            "sku": sku,
            "score": score,
            "raw_name": raw_name,
        }


def patch_cutcontour(pdf_path: Path) -> None:
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


# ---------------- DRAWING ---------------- #

def fit_text_size(text: str, font_name: str, max_width: float, max_size: float, min_size: float) -> float:
    size = max_size
    while size > min_size and stringWidth(text, font_name, size) > max_width:
        size -= 0.25
    return max(size, min_size)


def draw_code128(c, value: str, x: float, y: float, width: float, height: float) -> None:
    drawing = createBarcodeDrawing(
        "Code128",
        value=value,
        barHeight=height,
        barWidth=1,
        humanReadable=False,
        quiet=False,
    )

    src_w = float(drawing.width)
    src_h = float(drawing.height)

    scale_x = width / src_w
    scale_y = height / src_h

    c.saveState()
    c.translate(x, y)
    c.scale(scale_x, scale_y)
    renderPDF.draw(drawing, c, 0, 0)
    c.restoreState()


def draw_qr(c, value: str, x: float, y: float, size: float) -> None:
    qr = QrCodeWidget(value)
    x1, y1, x2, y2 = qr.getBounds()
    src_size = max(x2 - x1, y2 - y1)

    drawing = Drawing(
        size,
        size,
        transform=[
            size / src_size,
            0,
            0,
            size / src_size,
            -x1 * size / src_size,
            -y1 * size / src_size,
        ],
    )
    drawing.add(qr)
    renderPDF.draw(drawing, c, x, y)


def draw_white_text_band(
    c,
    center_x: float,
    y_center: float,
    width: float,
    height: float,
    text: str,
    font_name: str,
    max_size: float,
    min_size: float,
) -> None:
    x = center_x - width / 2.0
    y = y_center - height / 2.0

    c.saveState()
    c.setFillColorRGB(1, 1, 1)
    c.rect(x, y, width, height, stroke=0, fill=1)

    c.setFillColorRGB(0, 0, 0)
    text_width = width - 0.04 * inch
    font_size = fit_text_size(text, font_name, text_width, max_size, min_size)
    c.setFont(font_name, font_size)
    c.drawCentredString(center_x, y + (height - font_size) / 2.0 + 1.0, text)
    c.restoreState()


def draw_machine_readable_label(
    c,
    center_x: float,
    center_y: float,
    radius: float,
    name: str,
    thc: str,
    code_style: str,
    code_value: str,
    diameter_inch: float,
) -> None:
    inner_radius = radius - max(0.012 * inch, diameter_inch * 0.008 * inch)

    band_height = max(0.115 * inch, diameter_inch * 0.09 * inch)
    name_band_width = inner_radius * 1.65
    thc_band_width = inner_radius * 1.05

    if code_style == "barcode":
        barcode_height = inner_radius * 0.88
        barcode_width = inner_radius * 1.82

        code_x = center_x - barcode_width / 2.0
        code_y = center_y - barcode_height / 2.0

        c.saveState()
        c.setFillColorRGB(1, 1, 1)
        c.rect(code_x, code_y, barcode_width, barcode_height, stroke=0, fill=1)
        c.restoreState()

        draw_code128(c, code_value, code_x, code_y, barcode_width, barcode_height)

        name_y_center = center_y + inner_radius * 0.40
        thc_y_center = center_y - inner_radius * 0.40

        draw_white_text_band(
            c,
            center_x=center_x,
            y_center=name_y_center,
            width=name_band_width,
            height=band_height,
            text=name,
            font_name="Helvetica-Bold",
            max_size=8.0,
            min_size=4.5,
        )

        draw_white_text_band(
            c,
            center_x=center_x,
            y_center=thc_y_center,
            width=thc_band_width,
            height=band_height,
            text=f"{thc}%",
            font_name="Helvetica",
            max_size=8.5,
            min_size=5.0,
        )

    elif code_style == "qr":
        qr_size = inner_radius * 1.42
        qr_x = center_x - qr_size / 2.0
        qr_y = center_y - qr_size / 2.0

        quiet_pad = 0.006 * inch

        c.saveState()
        c.setFillColorRGB(1, 1, 1)
        c.rect(
            qr_x - quiet_pad,
            qr_y - quiet_pad,
            qr_size + quiet_pad * 2,
            qr_size + quiet_pad * 2,
            stroke=0,
            fill=1,
        )
        c.restoreState()

        draw_qr(c, code_value, qr_x, qr_y, qr_size)

        name_y_center = center_y + qr_size * 0.42
        thc_y_center = center_y - qr_size * 0.42

        draw_white_text_band(
            c,
            center_x=center_x,
            y_center=name_y_center,
            width=name_band_width,
            height=band_height,
            text=name,
            font_name="Helvetica-Bold",
            max_size=8.0,
            min_size=4.5,
        )

        draw_white_text_band(
            c,
            center_x=center_x,
            y_center=thc_y_center,
            width=thc_band_width,
            height=band_height,
            text=f"{thc}%",
            font_name="Helvetica",
            max_size=8.5,
            min_size=5.0,
        )


def draw_label_on_canvas(
    c,
    origin_x: float,
    origin_y: float,
    name: str,
    thc: str,
    weight: Optional[str],
    diameter_inch: float,
    bg_image: Optional[Path] = None,
    code_style: Optional[str] = None,
    code_value: Optional[str] = None,
):
    label_w = diameter_inch * inch
    label_h = diameter_inch * inch

    if bg_image is not None:
        img = ImageReader(str(bg_image))
        img_w, img_h = img.getSize()

        x_img = origin_x + (label_w - img_w) / 2.0
        y_img = origin_y + (label_h - img_h) / 2.0

        c.drawImage(
            img,
            x_img,
            y_img,
            width=img_w,
            height=img_h,
            preserveAspectRatio=True,
            mask="auto",
        )

    cut_contour = CMYKColor(0, 100, 0, 0, spotName="CutContour")

    center_x = origin_x + label_w / 2.0
    center_y = origin_y + label_h / 2.0

    margin = max(0.04 * inch, 0.03 * diameter_inch * inch)
    radius = (diameter_inch * inch) / 2.0 - margin

    c.setStrokeColor(cut_contour)
    c.setLineWidth(0.01 * inch)
    c.circle(center_x, center_y, radius, stroke=1, fill=0)

    c.setFillColorCMYK(0, 0, 0, 1)

    if code_style and code_value:
        draw_machine_readable_label(
            c=c,
            center_x=center_x,
            center_y=center_y,
            radius=radius,
            name=name,
            thc=thc,
            code_style=code_style,
            code_value=code_value,
            diameter_inch=diameter_inch,
        )
        return

    text_pad = max(0.06 * inch, 0.05 * diameter_inch * inch)
    r_text = max(0.01 * inch, radius - text_pad)

    def max_width_at_y(y: float) -> float:
        dy = y - center_y
        if abs(dy) >= r_text:
            return 0.0
        return 2.0 * math.sqrt((r_text * r_text) - (dy * dy))

    def fit_font_size_at_y(
        text: str,
        font_name: str,
        max_size: float,
        min_size: float,
        y: float,
    ) -> float:
        max_width = max_width_at_y(y) - 2 * text_pad
        size = max_size
        if max_width <= 1:
            return min_size
        while size > min_size and stringWidth(text, font_name, size) > max_width:
            size -= 0.25
        return max(size, min_size)

    def split_name_two_lines(s: str) -> Optional[Tuple[str, str]]:
        parts = s.split()
        if len(parts) < 2:
            return None
        best_score = None
        best_pair = None
        for i in range(1, len(parts)):
            a = " ".join(parts[:i])
            b = " ".join(parts[i:])
            score = abs(len(a) - len(b))
            if best_score is None or score < best_score:
                best_score = score
                best_pair = (a, b)
        return best_pair

    def leading_for(font_size: float) -> float:
        return max(font_size * 1.25, font_size + 2.0)

    weight_present = bool((weight or "").strip())

    name_y_single = center_y + 0.36 * r_text
    name_y_top = center_y + 0.50 * r_text

    thc_y_default = (center_y - 0.02 * r_text) if weight_present else (center_y - 0.22 * r_text)
    weight_y_default = center_y - 0.32 * r_text

    name_font = "Helvetica-Bold"
    name_max = 9.5
    name_min = 5.5

    name_size_1 = fit_font_size_at_y(name, name_font, name_max, name_min, name_y_single)

    drew_two_line = False
    name_block_bottom_y = name_y_single

    if name_size_1 <= 6.75:
        two = split_name_two_lines(name)
        if two:
            a, b = two
            y1 = name_y_top
            y2 = y1 - leading_for(name_max)

            size_a = fit_font_size_at_y(a, name_font, name_max, name_min, y1)
            size_b = fit_font_size_at_y(b, name_font, name_max, name_min, y2)
            name_size_2 = min(size_a, size_b)

            y2 = y1 - leading_for(name_size_2)
            size_a2 = fit_font_size_at_y(a, name_font, name_max, name_min, y1)
            size_b2 = fit_font_size_at_y(b, name_font, name_max, name_min, y2)
            name_size_2 = min(size_a2, size_b2)
            y2 = y1 - leading_for(name_size_2)

            if name_size_2 > name_size_1:
                c.setFont(name_font, name_size_2)
                c.drawCentredString(center_x, y1, a)
                c.drawCentredString(center_x, y2, b)
                drew_two_line = True
                name_block_bottom_y = y2

    if not drew_two_line:
        c.setFont(name_font, name_size_1)
        c.drawCentredString(center_x, name_y_single, name)
        name_block_bottom_y = name_y_single

    thc_font = "Helvetica"
    thc_text = f"{thc}%"
    thc_max = 8.5
    thc_min = 5.5

    min_gap = 7.5 if drew_two_line else max(6.5, name_size_1 * 0.95)

    thc_y = thc_y_default
    if (name_block_bottom_y - thc_y) < min_gap:
        thc_y = name_block_bottom_y - min_gap

    if weight_present:
        thc_y = max(center_y - 0.55 * r_text, min(thc_y, center_y + 0.15 * r_text))
    else:
        thc_y = max(center_y - 0.70 * r_text, min(thc_y, center_y + 0.10 * r_text))

    thc_size = fit_font_size_at_y(thc_text, thc_font, thc_max, thc_min, thc_y)
    c.setFont(thc_font, thc_size)
    c.drawCentredString(center_x, thc_y, thc_text)

    if weight_present:
        weight_font = "Helvetica"
        weight_text = f"Net Wt: {weight} g"
        wt_max = 8.0
        wt_min = 5.0

        weight_y = weight_y_default
        min_gap_w = max(6.0, thc_size * 0.95)
        if (thc_y - weight_y) < min_gap_w:
            weight_y = thc_y - min_gap_w

        weight_y = max(center_y - 0.70 * r_text, min(weight_y, center_y - 0.10 * r_text))

        wt_size = fit_font_size_at_y(weight_text, weight_font, wt_max, wt_min, weight_y)
        c.setFont(weight_font, wt_size)
        c.drawCentredString(center_x, weight_y, weight_text)


def make_label_pdf(
    name: str,
    thc: str,
    weight: Optional[str],
    outfile: Path,
    diameter_inch: float,
    bg_image: Optional[Path] = None,
    code_style: Optional[str] = None,
    code_value: Optional[str] = None,
):
    page_w = diameter_inch * inch
    page_h = diameter_inch * inch
    c = canvas.Canvas(str(outfile), pagesize=(page_w, page_h))

    draw_label_on_canvas(
        c,
        origin_x=0,
        origin_y=0,
        name=name,
        thc=thc,
        weight=weight,
        diameter_inch=diameter_inch,
        bg_image=bg_image,
        code_style=code_style,
        code_value=code_value,
    )

    c.showPage()
    c.save()

    patch_cutcontour(outfile)
    print(f"Wrote {outfile}")


# ---------------- CSV COLUMN DETECTION ---------------- #

def detect_columns(header) -> Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    lower_map = {h.lower(): h for h in header}

    def find_col(candidates):
        for cand in candidates:
            if cand in lower_map:
                return lower_map[cand]
        return None

    name_col = find_col(["name", "product", "product name", "strain"])
    tags_col = find_col(["tags", "tag", "product tags", "product_tags"])
    thc_col = find_col(["thc", "total thc", "thc content", "thc_content"])
    active_col = find_col(["active", "is active", "is_active", "enabled"])
    sku_col = find_col(["sku"])
    product_category_col = find_col(["product_category", "product category", "category"])
    handle_col = find_col(["handle"])

    return name_col, tags_col, thc_col, active_col, sku_col, product_category_col, handle_col


def extract_lid_thc_records(tags_raw: str, legacy_thc_raw: str = "") -> List[Tuple[Optional[int], str]]:
    indexed = extract_indexed_thc_values(tags_raw)
    if indexed:
        return [(idx, thc) for idx, thc in indexed]

    fallback = extract_thc_value(tags_raw, legacy_thc_raw)
    if fallback:
        return [(None, fallback)]

    return []


def thc_filename_fragment(index: Optional[int], thc: str) -> str:
    safe_thc = slugify(normalize_thc_value(thc).replace(".", "-"))
    if index is None:
        return f"legacy-thc-{safe_thc}"
    return f"{index}-thc-{safe_thc}"


def sku_suffix_from_row(sku: str, product_category: str) -> Optional[str]:
    """
    Prefer category because the uploaded Lightspeed export has reliable categories:
      Flower / Eighth / ...
      Flower / Quarter / ...

    Fall back to SKU suffix only when needed.
    """
    category = (product_category or "").strip().lower()
    sku_key = normalized_sku_key(sku)

    if category.startswith("flower / eighth"):
        return "E"

    if category.startswith("flower / quarter"):
        return "Q"

    if sku_key.endswith("E"):
        return "E"

    if sku_key.endswith("Q"):
        return "Q"

    return None


def process_csv(
    csv_path: Path,
    out_dir: Path,
    bg_image: Optional[Path],
    diameters_inch: List[float],
    weight_override: Optional[str],
    sku_regex: Optional[str],
    exclude_sku_regex: Optional[str],
    category_prefixes: List[str],
    code_style: Optional[str] = None,
):
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []

        name_col, tags_col, thc_col, active_col, sku_col, product_category_col, handle_col = detect_columns(header)

        if not name_col:
            raise SystemExit(
                "Could not auto-detect column for name.\n"
                f"Found columns: {header}\n"
                "Expected something like: name / product name / strain."
            )

        if not (tags_col or thc_col):
            raise SystemExit(
                "Could not auto-detect column for tags or thc.\n"
                f"Found columns: {header}\n"
                "Expected either:\n"
                "  - tags column containing thc=... or json={...}, or\n"
                "  - a thc column like 'THC Content' (legacy)\n"
            )

        print(
            f"Using columns -> name: {name_col}, "
            f"tags: {tags_col or '(none)'}, "
            f"thc_legacy: {thc_col or '(none)'}, "
            f"active: {active_col or '(none; all treated active)'}, "
            f"sku: {sku_col or '(none)'}, "
            f"product_category: {product_category_col or '(none)'}, "
            f"handle: {handle_col or '(none)'}"
        )

        normalized_override = None
        if weight_override is not None:
            normalized_override = normalize_weight_grams(weight_override.strip()) or None

        labels: list[tuple[str, Optional[int], str, Optional[str], str]] = []
        seen_labels: set[tuple] = set()

        # For barcode/QR lids:
        # 1. Collect E and Q SKUs by cleaned strain name.
        # 2. Prefer no-dash sellable rows over dashed base rows.
        # 3. Collect THC label records by cleaned strain name.
        # 4. Generate 1.25in from E and 1.5in from Q.
        code_skus_by_strain: dict[str, dict[str, dict[str, object]]] = {}
        code_display_name_by_strain: dict[str, str] = {}
        code_labels: dict[
            tuple[str, Optional[int], str, Optional[str]],
            dict[str, str]
        ] = {}

        for row in reader:
            if is_composite_component_row(row):
                continue

            active_raw = row.get(active_col, "") if active_col else ""

            if active_col and is_inactive(active_raw):
                continue

            sku = (row.get(sku_col, "") if sku_col else "").strip()
            product_category = (row.get(product_category_col, "") if product_category_col else "").strip()
            raw_name = (row.get(name_col) or "").strip()
            handle = (row.get(handle_col, "") if handle_col else "").strip()
            tags_raw = row.get(tags_col, "") if tags_col else ""

            if exclude_sku_regex and regex_matches(sku, exclude_sku_regex):
                continue

            if not category_matches_prefix(product_category, category_prefixes):
                continue

            name = clean_product_name_for_lid(raw_name)
            if not name:
                continue

            strain_key = slugify(name)

            legacy_thc_raw = row.get(thc_col, "") if thc_col else ""
            thc_records = extract_lid_thc_records(tags_raw, legacy_thc_raw)

            weight: Optional[str] = normalized_override

            if code_style:
                sku_suffix = sku_suffix_from_row(sku, product_category)

                if sku_suffix in {"E", "Q"}:
                    remember_best_sku(
                        skus_by_strain=code_skus_by_strain,
                        strain_key=strain_key,
                        sku_suffix=sku_suffix,
                        sku=sku,
                        raw_name=raw_name,
                        handle=handle,
                        tags_raw=tags_raw,
                        product_category=product_category,
                        active_raw=active_raw,
                    )
                    code_display_name_by_strain.setdefault(strain_key, name)

                if thc_records:
                    code_display_name_by_strain.setdefault(strain_key, name)

                    for coa_index, thc in thc_records:
                        label_key = (strain_key, coa_index, thc, weight)
                        code_labels.setdefault(label_key, {})
                        code_labels[label_key]["name"] = name

                continue

            # Old/background lid behavior.
            if sku_regex and not regex_matches(sku, sku_regex):
                continue

            if not thc_records:
                continue

            for coa_index, thc in thc_records:
                label = (name, coa_index, thc, weight, sku)
                dedupe_key = (name, coa_index, thc, weight)
                if dedupe_key in seen_labels:
                    continue
                seen_labels.add(dedupe_key)
                labels.append(label)

        if code_style:
            if not code_labels:
                print("No valid barcode/QR labels found in CSV.")
                return

            for (strain_key, coa_index, thc, weight), label_info in code_labels.items():
                name = label_info.get("name") or code_display_name_by_strain.get(strain_key) or strain_key
                skus_by_suffix = code_skus_by_strain.get(strain_key, {})

                strain_dir = out_dir / strain_key
                strain_dir.mkdir(parents=True, exist_ok=True)

                for diameter_inch in diameters_inch:
                    expected_suffix = lid_sku_suffix_for_diameter(diameter_inch)
                    if expected_suffix is None:
                        continue

                    sku_record = skus_by_suffix.get(expected_suffix)
                    if not sku_record:
                        print(
                            f"Skipping {name} {format_size_tag(diameter_inch)}: "
                            f"no {expected_suffix} SKU found"
                        )
                        continue

                    sku = str(sku_record["sku"])

                    size_tag = format_size_tag(diameter_inch)
                    prefix = thc_filename_fragment(coa_index, thc)

                    if weight:
                        filename = f"{prefix}-{slugify(name)}-{weight}g-{size_tag}.pdf"
                    else:
                        filename = f"{prefix}-{slugify(name)}-{size_tag}.pdf"

                    outfile = strain_dir / filename

                    print(
                        f"Generating {name} {size_tag} with SKU {sku} "
                        f"(source row: {sku_record.get('raw_name', '')})"
                    )

                    make_label_pdf(
                        name=name,
                        thc=thc,
                        weight=weight,
                        outfile=outfile,
                        diameter_inch=diameter_inch,
                        bg_image=bg_image,
                        code_style=code_style,
                        code_value=sku,
                    )

            return

        if not labels:
            print("No valid labels found in CSV.")
            return

        for (name, coa_index, thc, weight, sku) in labels:
            strain_slug = slugify(name)
            strain_dir = out_dir / strain_slug
            strain_dir.mkdir(parents=True, exist_ok=True)

            for diameter_inch in diameters_inch:
                size_tag = format_size_tag(diameter_inch)

                prefix = thc_filename_fragment(coa_index, thc)
                if weight:
                    filename = f"{prefix}-{slugify(name)}-{weight}g-{size_tag}.pdf"
                else:
                    filename = f"{prefix}-{slugify(name)}-{size_tag}.pdf"

                outfile = strain_dir / filename

                make_label_pdf(
                    name=name,
                    thc=thc,
                    weight=weight,
                    outfile=outfile,
                    diameter_inch=diameter_inch,
                    bg_image=bg_image,
                    code_style=code_style,
                    code_value=sku,
                )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Generate circular label PDFs with CutContour from a Lightspeed CSV.\n"
            "THC is read from tags or legacy THC column fallback.\n"
            "Net weight is printed only if --weight is provided.\n"
            "Rows with Active == FALSE/0/NO are skipped.\n"
            "Composite component rows are skipped."
        )
    )
    parser.add_argument("csv_file", help="Path to the CSV file with label data.")
    parser.add_argument(
        "--out-dir",
        default="labels_out",
        help="Directory to write PDFs into.",
    )
    parser.add_argument(
        "--bg-image",
        help="Optional PNG background image to place behind circle/text.",
    )
    parser.add_argument(
        "--diameters",
        "-d",
        nargs="+",
        help='One or more circle diameters in inches. Example: --diameters 1.25 1.5',
    )
    parser.add_argument(
        "--weight",
        "-w",
        help='Optional: print net weight on labels, example "3.5" or "3.5g".',
    )
    parser.add_argument(
        "--sku-regex",
        help="Only generate lids for rows whose SKU fully matches this regex.",
    )
    parser.add_argument(
        "--exclude-sku-regex",
        help="Skip rows whose SKU fully matches this regex.",
    )
    parser.add_argument(
        "--category-prefix",
        action="append",
        default=[],
        help="Only generate lids for rows whose product_category starts with this value. Repeatable.",
    )
    parser.add_argument(
        "--code-style",
        choices=["barcode", "qr"],
        help=(
            "Replace the background with a scannable code. "
            "For this mode, 1.25-inch lids use E/eighth SKUs and 1.5-inch lids use Q/quarter SKUs."
        ),
    )

    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    out_dir = Path(args.out_dir)

    if not csv_path.exists():
        raise SystemExit(f"CSV file not found: {csv_path}")

    bg_image = None
    if args.bg_image:
        p = Path(args.bg_image)
        if not p.exists():
            raise SystemExit(f"Background image not found: {p}")
        bg_image = p

    diameters_inch = parse_diameters(args.diameters)

    process_csv(
        csv_path=csv_path,
        out_dir=out_dir,
        bg_image=bg_image,
        diameters_inch=diameters_inch,
        weight_override=args.weight,
        sku_regex=args.sku_regex,
        exclude_sku_regex=args.exclude_sku_regex,
        category_prefixes=args.category_prefix,
        code_style=args.code_style,
    )


if __name__ == "__main__":
    main()