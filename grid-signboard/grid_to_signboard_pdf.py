#!/usr/bin/env python3
# path: grid_to_signboard_pdf.py

import argparse
import math
from pathlib import Path
from typing import List

from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from PIL import Image

PAGE_WIDTH = 1920
PAGE_HEIGHT = 1080

MAX_COLS_PER_SIGNBOARD = 5
MAX_ROWS_PER_SIGNBOARD = 5


def find_images(directory: Path) -> List[Path]:
    exts = {".jpg", ".jpeg", ".png"}
    return sorted(
        [p for p in directory.iterdir() if p.suffix.lower() in exts and p.is_file()],
        key=lambda p: p.name.lower(),
    )


def draw_background(c: canvas.Canvas, bg_path: Path):
    img = ImageReader(str(bg_path))
    iw, ih = img.getSize()

    # Scale to COVER the page (no letterboxing)
    scale_x = PAGE_WIDTH / iw
    scale_y = PAGE_HEIGHT / ih
    scale = max(scale_x, scale_y)

    draw_w = iw * scale
    draw_h = ih * scale

    x = (PAGE_WIDTH - draw_w) / 2.0
    y = (PAGE_HEIGHT - draw_h) / 2.0

    c.drawImage(
        img,
        x,
        y,
        width=draw_w,
        height=draw_h,
        preserveAspectRatio=True,
        mask="auto",
    )


def draw_image_in_cell(
    c: canvas.Canvas,
    img_path: Path,
    cell_x: float,
    cell_y: float,
    cell_w: float,
    cell_h: float,
):
    with Image.open(img_path) as im:
        iw, ih = im.size

    scale = min(cell_w / iw, cell_h / ih)
    draw_w = iw * scale
    draw_h = ih * scale

    x = cell_x + (cell_w - draw_w) / 2.0
    y = cell_y + (cell_h - draw_h) / 2.0

    img = ImageReader(str(img_path))
    c.drawImage(
        img,
        x,
        y,
        width=draw_w,
        height=draw_h,
        preserveAspectRatio=True,
        mask="auto",
    )


def chunked(items: List[Path], chunk_size: int) -> List[List[Path]]:
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def build_pdf(
    images_dir: Path,
    bg_image: Path,
    out_path: Path,
    cols: int,
    rows: int,
    margin: float,
    padding: float,
):
    images = find_images(images_dir)
    if not images:
        raise SystemExit(f"No JPG/PNG files found in {images_dir}")

    cols = max(1, min(cols, MAX_COLS_PER_SIGNBOARD))
    rows = max(1, min(rows, MAX_ROWS_PER_SIGNBOARD))
    images_per_page = cols * rows

    image_pages = chunked(images, images_per_page)
    total_pages = len(image_pages)

    print(
        f"Found {len(images)} images. "
        f"Using up to {cols} columns x {rows} rows per signboard "
        f"({images_per_page} images max per page), "
        f"writing {total_pages} page(s)."
    )

    c = canvas.Canvas(str(out_path), pagesize=(PAGE_WIDTH, PAGE_HEIGHT))

    total_w = PAGE_WIDTH - 2 * margin
    total_h = PAGE_HEIGHT - 2 * margin

    cell_w = (total_w - (cols - 1) * padding) / cols
    cell_h = (total_h - (rows - 1) * padding) / rows

    for page_num, page_images in enumerate(image_pages, start=1):
        draw_background(c, bg_image)

        for idx, img_path in enumerate(page_images):
            col = idx % cols
            row = idx // cols

            inv_row = rows - 1 - row

            cell_x = margin + col * (cell_w + padding)
            cell_y = margin + inv_row * (cell_h + padding)

            draw_image_in_cell(c, img_path, cell_x, cell_y, cell_w, cell_h)

        print(
            f"Page {page_num}/{total_pages}: placed {len(page_images)} image(s)"
        )

        c.showPage()

    c.save()
    print(f"Wrote multi-page PDF: {out_path}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Pack JPG/PNG images in a directory into one or more 1920x1080 PDF signboard pages, "
            "with a background image scaled to the page."
        )
    )
    parser.add_argument(
        "images_dir",
        help="Directory containing .jpg/.jpeg/.png files.",
    )
    parser.add_argument(
        "bg_image",
        help="Background JPG/PNG to cover the 1920x1080 page.",
    )
    parser.add_argument(
        "--out",
        default="grid_signboard.pdf",
        help="Output PDF filename (default: grid_signboard.pdf).",
    )
    parser.add_argument(
        "--cols",
        type=int,
        default=4,
        help="Number of columns per signboard page (max: 5, default: 4).",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=3,
        help="Number of rows per signboard page (max: 5, default: 3).",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=80.0,
        help="Outer margin in points (default: 80).",
    )
    parser.add_argument(
        "--padding",
        type=float,
        default=20.0,
        help="Padding between cells in points (default: 20).",
    )

    args = parser.parse_args()

    images_dir = Path(args.images_dir)
    if not images_dir.is_dir():
        raise SystemExit(f"Not a directory: {images_dir}")

    bg_image = Path(args.bg_image)
    if not bg_image.is_file():
        raise SystemExit(f"Background image not found: {bg_image}")

    out_path = Path(args.out)

    build_pdf(
        images_dir=images_dir,
        bg_image=bg_image,
        out_path=out_path,
        cols=args.cols,
        rows=args.rows,
        margin=args.margin,
        padding=args.padding,
    )


if __name__ == "__main__":
    main()