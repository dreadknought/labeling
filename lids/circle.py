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
from reportlab.lib.units import inch
from reportlab.lib.colors import CMYKColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth

from PyPDF2 import PdfReader, PdfWriter
from PyPDF2.generic import (
    NameObject,
    DictionaryObject,
    ArrayObject,
    NumberObject,
    FloatObject,
    DecodedStreamObject,
)

DEFAULT_DIAMETER_INCH = 1.5  # used if no --diameters are provided


# ---------------- TAG PARSING ---------------- #

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
    """
    Return THC without a trailing '%' because draw_label_on_canvas adds it.
    """
    if not thc_raw:
        return ""
    s = str(thc_raw).strip()
    if not s:
        return ""
    if s.endswith("%"):
        s = s[:-1].strip()
    return s


def find_key_recursive(obj: Any, wanted_key: str) -> Optional[str]:
    """
    Recursively search nested dict/list structures for a key, case-insensitive.
    Returns the first scalar value found as a string.
    """
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


def normalize_weight_grams(weight_raw: str) -> str:
    """
    Return weight as a numeric-ish string in grams WITHOUT a trailing unit.
    """
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
    """
    In Lightspeed exports, composite component/recipe rows have composite_sku filled.
    Those are not real sellable items and should not generate labels.
    """
    return bool((row.get("composite_sku") or "").strip())


# ---------------- HELPERS ---------------- #

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "label"


def format_inches(x: float) -> str:
    """
    Format inches for filenames.
      1.5 -> "1.5"
      2.0 -> "2"
      1.3333 -> "1.333"
    """
    return f"{x:.3f}".rstrip("0").rstrip(".")


def format_size_tag(diameter_inch: float) -> str:
    return f"{format_inches(diameter_inch)}in"


def parse_diameters(raw_values: Optional[List[str]]) -> List[float]:
    """
    Accepts:
      --diameters 1.5 2 3
    and also comma-separated chunks if needed:
      --diameters 1.5,2,3
    """
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


# ---------------- DRAWING ---------------- #

def draw_label_on_canvas(
    c,
    origin_x: float,
    origin_y: float,
    name: str,
    thc: str,
    weight: Optional[str],
    diameter_inch: float,
    bg_image: Optional[Path] = None,
):
    """
    Draw a single circular label at (origin_x, origin_y) on an existing canvas.
    The label's bounding box is diameter_inch x diameter_inch.
    """
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

    text_pad = max(0.06 * inch, 0.05 * diameter_inch * inch)
    r_text = max(0.01 * inch, radius - text_pad)

    def max_width_at_y(y: float) -> float:
        dy = y - center_y
        if abs(dy) >= r_text:
            return 0.0
        return 2.0 * math.sqrt((r_text * r_text) - (dy * dy))

    def fit_font_size(
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

    name_size_1 = fit_font_size(name, name_font, name_max, name_min, name_y_single)

    drew_two_line = False
    name_block_bottom_y = name_y_single

    if name_size_1 <= 6.75:
        two = split_name_two_lines(name)
        if two:
            a, b = two
            y1 = name_y_top
            y2 = y1 - leading_for(name_max)

            size_a = fit_font_size(a, name_font, name_max, name_min, y1)
            size_b = fit_font_size(b, name_font, name_max, name_min, y2)
            name_size_2 = min(size_a, size_b)

            y2 = y1 - leading_for(name_size_2)
            size_a2 = fit_font_size(a, name_font, name_max, name_min, y1)
            size_b2 = fit_font_size(b, name_font, name_max, name_min, y2)
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

    thc_size = fit_font_size(thc_text, thc_font, thc_max, thc_min, thc_y)
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

        wt_size = fit_font_size(weight_text, weight_font, wt_max, wt_min, weight_y)
        c.setFont(weight_font, wt_size)
        c.drawCentredString(center_x, weight_y, weight_text)


def make_label_pdf(
    name: str,
    thc: str,
    weight: Optional[str],
    outfile: Path,
    diameter_inch: float,
    bg_image: Optional[Path] = None,
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
    )

    c.showPage()
    c.save()

    patch_cutcontour(outfile)
    print(f"Wrote {outfile}")


# ---------------- CSV COLUMN DETECTION ---------------- #

def detect_columns(header) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """
    Weight is intentionally not detected/read from CSV/tags.
    Net weight is only printed when --weight is provided.
    """
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

    return name_col, tags_col, thc_col, active_col


def strip_parentheses(text: str) -> str:
    if not text:
        return ""
    s = str(text)
    s = re.sub(r"\s*\([^)]*\)", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def process_csv(
    csv_path: Path,
    out_dir: Path,
    bg_image: Optional[Path],
    diameters_inch: List[float],
    weight_override: Optional[str],
):
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []

        name_col, tags_col, thc_col, active_col = detect_columns(header)

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
            f"active: {active_col or '(none; all treated active)'}"
        )

        normalized_override = None
        if weight_override is not None:
            normalized_override = normalize_weight_grams(weight_override.strip()) or None

        labels: list[tuple[str, str, Optional[str]]] = []

        for row in reader:
            if is_composite_component_row(row):
                continue

            if active_col and is_inactive(row.get(active_col, "")):
                continue

            name = strip_parentheses((row.get(name_col) or "").strip())
            if not name:
                continue

            tags_raw = row.get(tags_col, "") if tags_col else ""
            legacy_thc_raw = row.get(thc_col, "") if thc_col else ""
            thc = extract_thc_value(tags_raw, legacy_thc_raw)

            if not thc:
                continue

            weight: Optional[str] = normalized_override
            labels.append((name, thc, weight))

        if not labels:
            print("No valid labels found in CSV.")
            return

        for (name, thc, weight) in labels:
            strain_slug = slugify(name)
            strain_dir = out_dir / strain_slug
            strain_dir.mkdir(parents=True, exist_ok=True)

            for diameter_inch in diameters_inch:
                size_tag = format_size_tag(diameter_inch)

                if weight:
                    filename = f"{slugify(name)}-{weight}g-{size_tag}.pdf"
                else:
                    filename = f"{slugify(name)}-{size_tag}.pdf"

                outfile = strain_dir / filename

                make_label_pdf(
                    name=name,
                    thc=thc,
                    weight=weight,
                    outfile=outfile,
                    diameter_inch=diameter_inch,
                    bg_image=bg_image,
                )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Generate circular label PDFs with CutContour from a Lightspeed CSV.\n"
            "THC is read from tags (thc=...) or from tags json={...}, with legacy THC column as fallback.\n"
            "Net weight is printed only if --weight is provided.\n"
            "Rows with Active == FALSE/0/NO are skipped.\n"
            "Composite component rows (composite_sku filled) are skipped.\n"
            "Outputs are grouped by strain, with one PDF per requested circle size."
        )
    )
    parser.add_argument("csv_file", help="Path to the CSV file with label data.")
    parser.add_argument(
        "--out-dir",
        default="labels_out",
        help="Directory to write PDFs into (default: labels_out).",
    )
    parser.add_argument(
        "--bg-image",
        help="Optional PNG background image to place behind circle/text.",
    )
    parser.add_argument(
        "--diameters",
        "-d",
        nargs="+",
        help=(
            "One or more circle diameters in inches. "
            'Examples: --diameters 1.5 2 3   or   --diameters 1.5,2,3'
        ),
    )
    parser.add_argument(
        "--weight",
        "-w",
        help=(
            'Optional: print net weight on labels (example: "3.5" or "3.5g"). '
            "If omitted, no net weight is printed."
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
    )


if __name__ == "__main__":
    main()