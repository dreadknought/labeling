# /mnt/data/barcode_label_maker.py
"""
Generate barcode label PDFs or SVGs from a directory of GIF barcode images.

Expected input filenames:
    [0-9]_SKU.gif
    [0-9][0-9]_SKU.gif
Examples:
    1_ABC123.gif
    12_ABC123.gif

For each GIF, this script:
1. Removes the leading one- or two-digit numeric prefix from the filename to get the SKU.
2. Looks up that SKU in a CSV using the `sku` column.
3. Reads the matching product name from the `name` column.
4. Covers the existing text band at the bottom of the barcode image with white.
5. Writes the product name into that band.
6. Draws a rounded CutContour rectangle around the finished label.
7. Writes one output file per GIF as PDF or SVG.

PDF output uses a ReportLab spot separation color named `CutContour`.
SVG output uses a stroke color literally named `CutContour`, which Illustrator/
VersaWorks workflows sometimes preserve, but PDF is the safer option when you
need a real spot separation.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas


DEFAULT_FONT_NAME = "Helvetica"
CUTCONTOUR_NAME = "CutContour"
FILENAME_PATTERN = re.compile(r"^(\d{1,2})_(.+)$")
OUTPUT_SAFE_CHARS_PATTERN = re.compile(r"[^A-Za-z0-9._() -]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate barcode label PDFs or SVGs with a CutContour rounded rectangle."
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        help="Directory containing input GIF barcode files named like 1_SKU.gif or 12_SKU.gif",
    )
    parser.add_argument(
        "--csv",
        required=True,
        help="CSV file containing at least 'sku' and 'name' columns",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where generated files will be written",
    )
    parser.add_argument(
        "--height-in",
        required=True,
        type=float,
        help="Final output height in inches",
    )
    parser.add_argument(
        "--format",
        choices=["pdf", "svg"],
        default="pdf",
        help="Output format. PDF is recommended for real CutContour spot color output.",
    )
    parser.add_argument(
        "--label-band-ratio",
        type=float,
        default=0.18,
        help=(
            "Base fraction of label height to cover with white at the bottom for the text band. "
            "The script automatically makes that whiteout area 20%% taller."
        ),
    )
    parser.add_argument(
        "--barcode-side-pad-in",
        type=float,
        default=0.05,
        help="Extra left/right padding in inches between the barcode image and the CutContour rectangle",
    )
    parser.add_argument(
        "--corner-radius-in",
        type=float,
        default=0.08,
        help="Rounded contour corner radius in inches",
    )
    parser.add_argument(
        "--contour-stroke-pt",
        type=float,
        default=0.75,
        help="CutContour stroke width in points",
    )
    parser.add_argument(
        "--page-margin-in",
        type=float,
        default=0.0,
        help="Optional outer margin around the label in inches",
    )
    parser.add_argument(
        "--font-name",
        default=DEFAULT_FONT_NAME,
        help="Font name registered in ReportLab. Helvetica works out of the box.",
    )
    parser.add_argument(
        "--max-font-size-pt",
        type=float,
        default=12.0,
        help="Maximum label font size in points",
    )
    parser.add_argument(
        "--min-font-size-pt",
        type=float,
        default=5.0,
        help="Minimum label font size in points before truncation kicks in",
    )
    return parser.parse_args()


def load_lookup(csv_path: Path) -> Dict[str, str]:
    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = {h.strip().lower(): h for h in (reader.fieldnames or [])}

        if "sku" not in headers or "name" not in headers:
            raise ValueError(
                f"CSV must contain 'sku' and 'name' columns. Found: {reader.fieldnames}"
            )

        sku_key = headers["sku"]
        name_key = headers["name"]

        lookup: Dict[str, str] = {}
        for row in reader:
            raw_sku = (row.get(sku_key) or "").strip()
            raw_name = (row.get(name_key) or "").strip()
            if raw_sku:
                lookup[raw_sku] = raw_name

    return lookup


def extract_sku_from_filename(path: Path) -> Optional[str]:
    match = FILENAME_PATTERN.match(path.stem)
    if not match:
        return None
    return match.group(2)


def open_first_frame_rgb(image_path: Path) -> Image.Image:
    with Image.open(image_path) as im:
        im.seek(0)
        frame = im.convert("RGB")
    return frame


def pil_image_to_png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def fit_text(text: str, font_name: str, max_width_pt: float, max_size_pt: float, min_size_pt: float) -> Tuple[str, float]:
    candidate = text.strip()
    if not candidate:
        return "", min_size_pt

    font_size = max_size_pt
    while font_size >= min_size_pt:
        if pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width_pt:
            return candidate, font_size
        font_size -= 0.25

    truncated = candidate
    ellipsis = "..."
    while truncated:
        attempt = truncated + ellipsis
        if pdfmetrics.stringWidth(attempt, font_name, min_size_pt) <= max_width_pt:
            return attempt, min_size_pt
        truncated = truncated[:-1]

    return ellipsis, min_size_pt


def compute_scaled_size(src_width_px: int, src_height_px: int, height_in: float) -> Tuple[float, float]:
    output_height_pt = height_in * inch
    aspect_ratio = src_width_px / src_height_px
    output_width_pt = output_height_pt * aspect_ratio
    return output_width_pt, output_height_pt


def cutcontour_color_pdf() -> colors.CMYKColorSep:
    return colors.CMYKColorSep(0, 100, 0, 0, spotName=CUTCONTOUR_NAME, density=1)


def make_safe_output_stem(product_name: str, fallback_sku: str) -> str:
    cleaned = OUTPUT_SAFE_CHARS_PATTERN.sub("", product_name.strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    if not cleaned:
        cleaned = fallback_sku
    return cleaned


def write_pdf(
    image: Image.Image,
    product_name: str,
    output_path: Path,
    height_in: float,
    label_band_ratio: float,
    barcode_side_pad_in: float,
    corner_radius_in: float,
    contour_stroke_pt: float,
    page_margin_in: float,
    font_name: str,
    max_font_size_pt: float,
    min_font_size_pt: float,
) -> None:
    src_width_px, src_height_px = image.size
    label_w_pt, label_h_pt = compute_scaled_size(src_width_px, src_height_px, height_in)
    margin_pt = page_margin_in * inch
    page_w_pt = label_w_pt + (margin_pt * 2)
    page_h_pt = label_h_pt + (margin_pt * 2)
    label_x = margin_pt
    label_y = margin_pt
    band_h_pt = label_h_pt * label_band_ratio * 1.2
    radius_pt = corner_radius_in * inch
    barcode_side_pad_pt = barcode_side_pad_in * inch
    barcode_x = label_x + barcode_side_pad_pt
    barcode_w_pt = max(label_w_pt - (barcode_side_pad_pt * 2), 1)

    png_bytes = pil_image_to_png_bytes(image)
    image_reader = ImageReader(io.BytesIO(png_bytes))

    c = canvas.Canvas(str(output_path), pagesize=(page_w_pt, page_h_pt))
    c.drawImage(
        image_reader,
        barcode_x,
        label_y,
        width=barcode_w_pt,
        height=label_h_pt,
        preserveAspectRatio=False,
        mask="auto",
    )

    c.setFillColor(colors.white)
    c.setStrokeColor(colors.white)
    c.rect(label_x, label_y, label_w_pt, band_h_pt, fill=1, stroke=0)

    safe_text = product_name.strip()
    text_max_width = max(label_w_pt - 10, 10)
    text_value, font_size = fit_text(
        safe_text,
        font_name,
        text_max_width,
        max_font_size_pt,
        min_font_size_pt,
    )
    c.setFillColor(colors.black)
    c.setFont(font_name, font_size)
    c.drawCentredString(
        label_x + (label_w_pt / 2),
        label_y + ((band_h_pt - font_size) / 2) + 1.5,
        text_value,
    )

    c.setStrokeColor(cutcontour_color_pdf())
    c.setLineWidth(contour_stroke_pt)
    inset = contour_stroke_pt / 2
    c.roundRect(
        label_x + inset,
        label_y + inset,
        label_w_pt - contour_stroke_pt,
        label_h_pt - contour_stroke_pt,
        radius_pt,
        fill=0,
        stroke=1,
    )

    c.showPage()
    c.save()


def xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def write_svg(
    image: Image.Image,
    product_name: str,
    output_path: Path,
    height_in: float,
    label_band_ratio: float,
    barcode_side_pad_in: float,
    corner_radius_in: float,
    contour_stroke_pt: float,
    page_margin_in: float,
    max_font_size_pt: float,
    min_font_size_pt: float,
) -> None:
    src_width_px, src_height_px = image.size
    label_w_pt, label_h_pt = compute_scaled_size(src_width_px, src_height_px, height_in)
    margin_pt = page_margin_in * inch
    page_w_pt = label_w_pt + (margin_pt * 2)
    page_h_pt = label_h_pt + (margin_pt * 2)
    label_x = margin_pt
    label_y = margin_pt
    band_h_pt = label_h_pt * label_band_ratio * 1.2
    radius_pt = corner_radius_in * inch
    barcode_side_pad_pt = barcode_side_pad_in * inch
    barcode_x = label_x + barcode_side_pad_pt
    barcode_w_pt = max(label_w_pt - (barcode_side_pad_pt * 2), 1)

    png_bytes = pil_image_to_png_bytes(image)
    b64 = base64.b64encode(png_bytes).decode("ascii")

    approx_font_size = max(min(max_font_size_pt, band_h_pt * 0.5), min_font_size_pt)
    safe_text = xml_escape(product_name.strip())

    svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{page_w_pt:.3f}pt" height="{page_h_pt:.3f}pt" viewBox="0 0 {page_w_pt:.3f} {page_h_pt:.3f}">
  <image x="{barcode_x:.3f}" y="{label_y:.3f}" width="{barcode_w_pt:.3f}" height="{label_h_pt:.3f}" href="data:image/png;base64,{b64}" />
  <rect x="{label_x:.3f}" y="{label_y + (label_h_pt - band_h_pt):.3f}" width="{label_w_pt:.3f}" height="{band_h_pt:.3f}" fill="white" />
  <text x="{label_x + (label_w_pt / 2):.3f}" y="{label_y + label_h_pt - (band_h_pt / 2):.3f}" font-family="Helvetica, Arial, sans-serif" font-size="{approx_font_size:.3f}" text-anchor="middle" dominant-baseline="middle" fill="black">{safe_text}</text>
  <rect x="{label_x + (contour_stroke_pt / 2):.3f}" y="{label_y + (contour_stroke_pt / 2):.3f}" width="{label_w_pt - contour_stroke_pt:.3f}" height="{label_h_pt - contour_stroke_pt:.3f}" rx="{radius_pt:.3f}" ry="{radius_pt:.3f}" fill="none" stroke="{CUTCONTOUR_NAME}" stroke-width="{contour_stroke_pt:.3f}" />
</svg>
'''
    output_path.write_text(svg, encoding="utf-8")


def process_one_file(
    gif_path: Path,
    lookup: Dict[str, str],
    output_dir: Path,
    output_format: str,
    height_in: float,
    label_band_ratio: float,
    barcode_side_pad_in: float,
    corner_radius_in: float,
    contour_stroke_pt: float,
    page_margin_in: float,
    font_name: str,
    max_font_size_pt: float,
    min_font_size_pt: float,
) -> Tuple[bool, str]:
    sku = extract_sku_from_filename(gif_path)
    if not sku:
        return False, f"Skipping {gif_path.name}: filename does not match <1-2 digits>_<SKU>.gif"

    if sku not in lookup:
        return False, f"Skipping {gif_path.name}: SKU '{sku}' not found in CSV"

    product_name = lookup[sku]
    image = open_first_frame_rgb(gif_path)
    output_stem = make_safe_output_stem(product_name, sku)
    output_path = output_dir / f"{output_stem}.{output_format}"

    if output_format == "pdf":
        write_pdf(
            image=image,
            product_name=product_name,
            output_path=output_path,
            height_in=height_in,
            label_band_ratio=label_band_ratio,
            barcode_side_pad_in=barcode_side_pad_in,
            corner_radius_in=corner_radius_in,
            contour_stroke_pt=contour_stroke_pt,
            page_margin_in=page_margin_in,
            font_name=font_name,
            max_font_size_pt=max_font_size_pt,
            min_font_size_pt=min_font_size_pt,
        )
    else:
        write_svg(
            image=image,
            product_name=product_name,
            output_path=output_path,
            height_in=height_in,
            label_band_ratio=label_band_ratio,
            barcode_side_pad_in=barcode_side_pad_in,
            corner_radius_in=corner_radius_in,
            contour_stroke_pt=contour_stroke_pt,
            page_margin_in=page_margin_in,
            max_font_size_pt=max_font_size_pt,
            min_font_size_pt=min_font_size_pt,
        )

    return True, f"Wrote {output_path.name} for SKU '{sku}'"


def main() -> int:
    args = parse_args()

    input_dir = Path(args.input_dir).expanduser().resolve()
    csv_path = Path(args.csv).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    if not input_dir.is_dir():
        print(f"Input directory does not exist or is not a directory: {input_dir}", file=sys.stderr)
        return 2

    if not csv_path.is_file():
        print(f"CSV file does not exist: {csv_path}", file=sys.stderr)
        return 2

    if args.height_in <= 0:
        print("--height-in must be greater than 0", file=sys.stderr)
        return 2

    if not (0 < args.label_band_ratio < 1):
        print("--label-band-ratio must be between 0 and 1", file=sys.stderr)
        return 2

    if args.barcode_side_pad_in < 0:
        print("--barcode-side-pad-in must be 0 or greater", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    lookup = load_lookup(csv_path)

    gif_files = sorted(input_dir.glob("*.gif"))
    if not gif_files:
        print(f"No GIF files found in {input_dir}", file=sys.stderr)
        return 1

    ok_count = 0
    skip_count = 0

    for gif_path in gif_files:
        try:
            ok, message = process_one_file(
                gif_path=gif_path,
                lookup=lookup,
                output_dir=output_dir,
                output_format=args.format,
                height_in=args.height_in,
                label_band_ratio=args.label_band_ratio,
                barcode_side_pad_in=args.barcode_side_pad_in,
                corner_radius_in=args.corner_radius_in,
                contour_stroke_pt=args.contour_stroke_pt,
                page_margin_in=args.page_margin_in,
                font_name=args.font_name,
                max_font_size_pt=args.max_font_size_pt,
                min_font_size_pt=args.min_font_size_pt,
            )
            print(message)
            if ok:
                ok_count += 1
            else:
                skip_count += 1
        except Exception as exc:
            skip_count += 1
            print(f"Skipping {gif_path.name}: {exc}", file=sys.stderr)

    print(f"Done. Wrote {ok_count} file(s). Skipped {skip_count} file(s).")
    return 0 if ok_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
