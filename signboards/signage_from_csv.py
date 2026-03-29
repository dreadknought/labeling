#!/usr/bin/env python3
# path: signage_from_csv.py
"""
Generate a signboard from a Lightspeed-export CSV as:
  - PDF
  - PNG
  - BOTH (PDF + PNG)

THIS VERSION IS HARD-WIRED TO "EIGHTHS ONLY" for your flower sign:
  ✅ Includes base 1/8oz items (e.g. FL-BISC)
  ❌ Excludes size variants (-16, -Q, -OZ)
  ❌ Excludes composite "component/recipe" rows (where composite_sku is filled)
  ✅ THC is read from:
       1) top-level tags thc=...
       2) json=... tag payload
       3) legacy THC column fallback

PNG export is done by rendering the generated PDF using an external renderer:
  - preferred: pdftoppm (poppler-utils)
  - fallback: ImageMagick (magick)

Rows can be optionally filtered by an 'active' column:
  - active == FALSE / 0 / NO (any case)  -> row is skipped
  - active empty / missing / anything else -> treated as TRUE (included)

TEXT PLACEMENT OPTIONS:
  --text-side left   -> draw text block on left side only
  --text-side right  -> draw text block on right side only
  --text-side both   -> split the product list across left and right sides

SORT / ORDERING:
  - Products are sorted by price ascending
  - If prices tie, products are sorted alphabetically by strain name
  - With --text-side both, the sorted list fills the left column first,
    then the right column
  - Each column is rendered in reverse order so the first item in that
    column appears at the bottom and the last item appears at the top

DISPLAY-SIZING OPTIONS:
  - Default output is still 1920x1080
  - You can reduce the output height with --page-height for browser-based display
  - You can nudge the text block vertically with --vertical-offset
"""

import argparse
import csv
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, List, Dict, Optional, Tuple

from reportlab.pdfgen import canvas
from reportlab.lib.colors import black, white, HexColor
from reportlab.lib.utils import ImageReader

try:
    from PIL import Image  # type: ignore
except Exception:
    Image = None  # Pillow is optional unless you enable scaling


PAGE_WIDTH = 1920
DEFAULT_PAGE_HEIGHT = 1080


def is_inactive(active_raw: str) -> bool:
    s = (active_raw or "").strip().lower()
    return s in {"false", "0", "no", "n"}


def is_composite_component_row(row: dict) -> bool:
    return bool((row.get("composite_sku") or "").strip())


def is_size_variant_sku(sku: str) -> bool:
    s = (sku or "").strip().upper()
    return s.endswith("-16") or s.endswith("-Q") or s.endswith("-OZ")


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


def extract_tag_value(tags_raw: str, key: str) -> str:
    tag_map = parse_tags(tags_raw)

    direct = (tag_map.get(key.lower()) or "").strip()
    if direct:
        return direct

    json_blob = (tag_map.get("json") or "").strip()
    if json_blob:
        try:
            parsed = json.loads(json_blob)
        except Exception:
            parsed = None

        if parsed is not None:
            embedded = find_key_recursive(parsed, key)
            if embedded:
                return embedded.strip()

    return ""


def normalize_thc_display(thc_value: str) -> str:
    if not thc_value:
        return ""
    s = str(thc_value).strip()
    if not s:
        return ""
    return s if s.endswith("%") else f"{s}%"


def normalize_netwt(tag_value: str) -> str:
    if not tag_value:
        return ""
    s = str(tag_value).strip().lower().replace(" ", "")
    return s


def looks_like_eighth(name: str, product_category: str, netwt: str) -> bool:
    n = (name or "").lower()
    cat = (product_category or "").lower()
    nw = normalize_netwt(netwt)

    if nw == "3.5g":
        return True

    if "1/8" in n or "eighth" in n:
        return True
    if "eighth" in cat:
        return True

    return False


def detect_columns(header: List[str]) -> Tuple[str, Optional[str], str, Optional[str], Optional[str], Optional[str]]:
    lower_map = {h.lower(): h for h in header}

    def find_col(candidates):
        for cand in candidates:
            if cand in lower_map:
                return lower_map[cand]
        return None

    name_col = find_col(["product name", "product_name", "name", "product", "strain"])
    tags_col = find_col(["tags", "tag", "product tags", "product_tags"])
    price_col = find_col([
        "retail price", "retail_price",
        "price", "sell price", "sell_price",
        "unit price", "unit_price",
        "base price", "base_price"
    ])
    active_col = find_col(["active", "is active", "is_active", "enabled"])
    category_col = find_col(["product category", "product_category", "category"])
    thc_col_legacy = find_col(["thc content", "thc_content", "total thc", "total_thc", "thc"])

    return name_col or "", tags_col, price_col or "", active_col, category_col, thc_col_legacy


def parse_price(price_raw: str) -> Tuple[str, Optional[float]]:
    if not price_raw:
        return "", None

    s = price_raw.replace("$", "").replace(",", "").strip()
    try:
        val = float(s)
        return f"${val:.2f}", val
    except ValueError:
        return price_raw, None


def strip_parentheses(text: str) -> str:
    if not text:
        return ""
    s = str(text)
    s = re.sub(r"\s*\([^)]*\)", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def read_products_from_csv(csv_path: Path) -> List[Dict]:
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        if not header:
            raise SystemExit("CSV has no header row.")

        name_col, tags_col, price_col, active_col, category_col, thc_col_legacy = detect_columns(header)

        if not (name_col and price_col):
            raise SystemExit(
                "Could not auto-detect required columns.\n"
                f"Found columns: {header}\n\n"
                "Need:\n"
                "  - Name column (e.g. name / product_name)\n"
                "  - Price column (e.g. retail_price)\n"
                "  - THC source via tags / json tag / legacy thc column\n"
            )

        if not (tags_col or thc_col_legacy):
            raise SystemExit(
                "No THC source found.\n"
                f"Found columns: {header}\n"
                "Need either:\n"
                "  - tags column containing thc=... or json={...}, or\n"
                "  - a THC column like 'thc' / 'thc_content'\n"
            )

        print(
            "Using columns -> "
            f"name: {name_col}, "
            f"tags: {tags_col or '(none)'}, "
            f"price: {price_col}, "
            f"active: {active_col or '(none; all treated active)'}, "
            f"category: {category_col or '(none)'}, "
            f"thc_legacy: {thc_col_legacy or '(none)'}"
        )

        products: List[Dict] = []
        for row in reader:
            if active_col and is_inactive(row.get(active_col, "")):
                continue

            if is_composite_component_row(row):
                continue

            sku = (row.get("sku") or "").strip()
            if is_size_variant_sku(sku):
                continue

            name = strip_parentheses((row.get(name_col) or "").strip())
            price_raw = (row.get(price_col) or "").strip()
            product_category = (row.get(category_col) or "").strip() if category_col else ""

            if not name:
                continue

            thc_raw = ""
            netwt_raw = ""
            if tags_col:
                tags_raw = (row.get(tags_col) or "").strip()
                thc_raw = extract_tag_value(tags_raw, "thc")
                netwt_raw = extract_tag_value(tags_raw, "netwt")

            if not thc_raw and thc_col_legacy:
                thc_raw = (row.get(thc_col_legacy) or "").strip()

            if not looks_like_eighth(name=name, product_category=product_category, netwt=netwt_raw):
                continue

            if not thc_raw:
                continue

            price_display, price_value = parse_price(price_raw)

            products.append(
                {
                    "name": name,
                    "thc": thc_raw,
                    "price_display": price_display,
                    "price_value": price_value,
                }
            )

    if not products:
        raise SystemExit("No valid eighths found in CSV (after filtering).")

    return products


def draw_fitted_text(
    c: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    max_width: float,
    base_font_size: float,
    font_name: str = "Helvetica-Bold",
    min_font_size: float = 10.0,
    align: str = "left",
):
    if not text:
        return

    size = base_font_size
    c.setFont(font_name, size)
    width = c.stringWidth(text, font_name, size)

    while width > max_width and size > min_font_size:
        size -= 1
        c.setFont(font_name, size)
        width = c.stringWidth(text, font_name, size)

    if align == "center":
        x_draw = x - width / 2.0
    elif align == "right":
        x_draw = x - width
    else:
        x_draw = x

    c.drawString(x_draw, y, text)


def get_content_bounds(text_side: str, margin_side: float) -> Tuple[float, float]:
    if text_side == "left":
        return margin_side, PAGE_WIDTH * 0.42
    if text_side == "right":
        return PAGE_WIDTH * 0.58, PAGE_WIDTH - margin_side
    raise ValueError(f"Unsupported text_side: {text_side}")


def split_products_for_both_sides(products: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    midpoint = math.ceil(len(products) / 2.0)
    left_products = list(reversed(products[:midpoint]))
    right_products = list(reversed(products[midpoint:]))
    return left_products, right_products


def draw_product_block(
    c: canvas.Canvas,
    products: List[Dict],
    content_left: float,
    content_right: float,
    margin_top: float,
    margin_bottom: float,
    page_height: float,
    vertical_offset: float = 0.0,
):
    if not products:
        return

    content_width = content_right - content_left

    name_x = content_left
    thc_x = content_left + content_width * 0.55
    price_x = content_left + content_width * 0.85

    n = len(products)
    available_height = page_height - margin_top - margin_bottom
    row_height = min(available_height / n, 70)

    block_height = row_height * n
    block_bottom = ((page_height - block_height) / 2.0) + vertical_offset

    for i, p in enumerate(products):
        row_center_y = block_bottom + (i + 0.5) * row_height

        name = p["name"]
        thc_text = normalize_thc_display(p["thc"])
        price = p["price_display"]

        c.setFillColor(white)

        draw_fitted_text(
            c, name,
            x=name_x, y=row_center_y,
            max_width=thc_x - name_x - 20,
            base_font_size=32,
            font_name="Helvetica-Bold",
            min_font_size=12,
            align="left",
        )

        draw_fitted_text(
            c, thc_text,
            x=thc_x, y=row_center_y,
            max_width=price_x - thc_x - 20,
            base_font_size=28,
            font_name="Helvetica",
            min_font_size=12,
            align="left",
        )

        if price:
            draw_fitted_text(
                c, price,
                x=price_x, y=row_center_y,
                max_width=content_right - price_x,
                base_font_size=28,
                font_name="Helvetica-Bold",
                min_font_size=12,
                align="left",
            )


def draw_page(
    c: canvas.Canvas,
    products: List[Dict],
    bg_image: Optional[Path],
    text_side: str,
    page_height: float,
    vertical_offset: float = 0.0,
):
    if bg_image is not None:
        img = ImageReader(str(bg_image))
        iw, ih = img.getSize()
        scale = max(PAGE_WIDTH / iw, page_height / ih)
        draw_w = iw * scale
        draw_h = ih * scale
        x = (PAGE_WIDTH - draw_w) / 2.0
        y = (page_height - draw_h) / 2.0
        c.drawImage(img, x, y, width=draw_w, height=draw_h, preserveAspectRatio=True, mask="auto")
    else:
        c.setFillColor(black)
        c.rect(0, 0, PAGE_WIDTH, page_height, fill=1, stroke=0)

    c.setFillColor(HexColor("#000000"))
    try:
        c.setFillAlpha(0.35)
    except AttributeError:
        pass
    c.rect(0, 0, PAGE_WIDTH, page_height, fill=1, stroke=0)
    try:
        c.setFillAlpha(1.0)
    except AttributeError:
        pass

    margin_top = 80
    margin_bottom = 80
    margin_side = 80

    if text_side == "both":
        left_products, right_products = split_products_for_both_sides(products)

        left_content_left, left_content_right = get_content_bounds("left", margin_side)
        right_content_left, right_content_right = get_content_bounds("right", margin_side)

        draw_product_block(
            c=c,
            products=left_products,
            content_left=left_content_left,
            content_right=left_content_right,
            margin_top=margin_top,
            margin_bottom=margin_bottom,
            page_height=page_height,
            vertical_offset=vertical_offset,
        )

        draw_product_block(
            c=c,
            products=right_products,
            content_left=right_content_left,
            content_right=right_content_right,
            margin_top=margin_top,
            margin_bottom=margin_bottom,
            page_height=page_height,
            vertical_offset=vertical_offset,
        )
    else:
        content_left, content_right = get_content_bounds(text_side, margin_side)
        draw_product_block(
            c=c,
            products=products,
            content_left=content_left,
            content_right=content_right,
            margin_top=margin_top,
            margin_bottom=margin_bottom,
            page_height=page_height,
            vertical_offset=vertical_offset,
        )


def build_signage_pdf(
    csv_path: Path,
    out_pdf_path: Path,
    bg_image: Optional[Path],
    text_side: str,
    page_height: int,
    vertical_offset: float = 0.0,
):
    products = read_products_from_csv(csv_path)

    def sort_key(p: Dict):
        v = p["price_value"]
        if v is None:
            return (1, float("inf"), p["name"].strip().lower())
        return (0, float(v), p["name"].strip().lower())

    products.sort(key=sort_key)

    c = canvas.Canvas(str(out_pdf_path), pagesize=(PAGE_WIDTH, page_height))
    draw_page(
        c,
        products=products,
        bg_image=bg_image,
        text_side=text_side,
        page_height=page_height,
        vertical_offset=vertical_offset,
    )
    c.showPage()
    c.save()
    print(f"Wrote signboard PDF: {out_pdf_path}")


def pdf_to_png(pdf_path: Path, out_png_path: Path, dpi: int, scale_to_page: bool, page_height: int):
    out_png_path.parent.mkdir(parents=True, exist_ok=True)

    pdftoppm = shutil.which("pdftoppm")
    magick = shutil.which("magick")

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)

        if pdftoppm:
            prefix = td_path / "render"
            cmd = [pdftoppm, "-png", "-r", str(dpi), str(pdf_path), str(prefix)]
            subprocess.run(cmd, check=True)
            rendered = td_path / "render-1.png"
        elif magick:
            rendered = td_path / "render.png"
            cmd = [magick, "-density", str(dpi), str(pdf_path) + "[0]", str(rendered)]
            subprocess.run(cmd, check=True)
        else:
            raise SystemExit(
                "To output PNG you need either:\n"
                "  - pdftoppm (poppler-utils)\n"
                "  - ImageMagick (magick)\n"
            )

        if not rendered.exists():
            raise SystemExit("PNG render failed: renderer did not produce an output file.")

        if scale_to_page:
            if Image is None:
                raise SystemExit(
                    "Pillow is required for PNG scaling.\n"
                    "Install: pip install pillow  (or: sudo apt install python3-pil)\n"
                )
            im = Image.open(rendered).convert("RGBA")
            im = im.resize((PAGE_WIDTH, page_height), resample=Image.LANCZOS)
            im.save(out_png_path)
        else:
            out_png_path.write_bytes(rendered.read_bytes())

    print(f"Wrote signboard PNG: {out_png_path}")


def derive_outputs(out_arg: str) -> Tuple[Path, Path]:
    p = Path(out_arg)
    s = p.suffix.lower()
    if s == ".pdf":
        return p, p.with_suffix(".png")
    if s == ".png":
        return p.with_suffix(".pdf"), p
    if s == "":
        return p.with_suffix(".pdf"), p.with_suffix(".png")
    return Path(str(p) + ".pdf"), Path(str(p) + ".png")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Generate a signboard from a Lightspeed CSV.\n"
            "This script is configured for EIGHTHS ONLY (excludes -16/-Q/-OZ and composite component rows).\n"
        )
    )
    parser.add_argument("csv_file", help="Path to the CSV file with product data.")
    parser.add_argument("--out", default="signboard.pdf")
    parser.add_argument("--format", choices=["pdf", "png", "both"], default="pdf")
    parser.add_argument("--bg-image", help="Optional background image (PNG/JPG).")
    parser.add_argument("--text-side", choices=["left", "right", "both"], default="left")
    parser.add_argument("--text", choices=["left", "right", "both"], dest="text_side", help="Alias for --text-side")
    parser.add_argument("--png-dpi", type=int, default=144)
    parser.add_argument("--no-png-scale-to-page", action="store_true")
    parser.add_argument("--page-height", type=int, default=DEFAULT_PAGE_HEIGHT)
    parser.add_argument("--vertical-offset", type=float, default=0.0)

    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        raise SystemExit(f"CSV file not found: {csv_path}")

    bg_image = None
    if args.bg_image:
        p = Path(args.bg_image)
        if not p.exists():
            raise SystemExit(f"Background image not found: {p}")
        bg_image = p

    out_pdf_path, out_png_path = derive_outputs(args.out)
    text_side = args.text_side
    page_height = args.page_height
    vertical_offset = args.vertical_offset

    if args.format in ("pdf", "both"):
        build_signage_pdf(
            csv_path,
            out_pdf_path,
            bg_image,
            text_side,
            page_height=page_height,
            vertical_offset=vertical_offset,
        )

    if args.format in ("png", "both"):
        if args.format == "png":
            with tempfile.TemporaryDirectory() as td:
                tmp_pdf = Path(td) / "signboard.pdf"
                build_signage_pdf(
                    csv_path,
                    tmp_pdf,
                    bg_image,
                    text_side,
                    page_height=page_height,
                    vertical_offset=vertical_offset,
                )
                pdf_to_png(
                    tmp_pdf,
                    out_png_path,
                    args.png_dpi,
                    (not args.no_png_scale_to_page),
                    page_height=page_height,
                )
        else:
            pdf_to_png(
                out_pdf_path,
                out_png_path,
                args.png_dpi,
                (not args.no_png_scale_to_page),
                page_height=page_height,
            )


if __name__ == "__main__":
    main()