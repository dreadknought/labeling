#!/usr/bin/env bash
set -Eeuo pipefail
set -x

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
echo "script dir: $SCRIPT_DIR"

OUTPUT_DIR="/media/ibuckman/SSK SSD/pictures/labels"
LABEL_HEIGHT=0.75
SKUS="$SCRIPT_DIR/skus/skus.csv"
BARCODE_SCRIPT="$SCRIPT_DIR/barcodes/generate_code128_labels.py"

run_job() {
  local name="$1"
  shift
  (
    set -Eeuo pipefail
    echo "START: $name"
    "$@"
    echo "DONE:  $name"
  ) &
}

pids=()
names=()

start_job() {
  local name="$1"
  shift
  run_job "$name" "$@"
  pids+=("$!")
  names+=("$name")
}

wait_for_jobs() {
  local i
  local failed=0

  for i in "${!pids[@]}"; do
    if ! wait "${pids[$i]}"; then
      echo "FAILED: ${names[$i]}" >&2
      failed=1
    fi
  done

  if (( failed != 0 )); then
    echo "One or more jobs failed." >&2
    exit 1
  fi
}

mkdir -p \
  "$OUTPUT_DIR/signboards" \
  "$OUTPUT_DIR/lids" \
  "$OUTPUT_DIR/lids/bbuds" \
  "$OUTPUT_DIR/lids/barcode" \
  "$OUTPUT_DIR/lids/qr" \
  "$OUTPUT_DIR/lids/jars" \
  "$OUTPUT_DIR/lids/jars/bbuds" \
  "$OUTPUT_DIR/barcodes" \
  "$OUTPUT_DIR/barcodes/bbuds" \
  "$OUTPUT_DIR/barcodes/edibles" \
  "$OUTPUT_DIR/barcodes-short" \
  "$OUTPUT_DIR/barcodes-short/bbuds" \
  "$OUTPUT_DIR/barcodes-page" \
  "$OUTPUT_DIR/barcodes-page/bbuds" \
  "$OUTPUT_DIR/grid_signboard"

start_job "signboard left" bash -lc "
  cd '$SCRIPT_DIR/signboards' &&
  ./signage_from_csv.py \
    --bg-image left-tv.jpg \
    '$SKUS' \
    --text both \
    --format png \
    --page-height 900 \
    --vertical-offset 40 \
    --out '$OUTPUT_DIR/signboards/left.png' &&
  cd '$OUTPUT_DIR/signboards' &&
  cp left.png left2.png &&
  cp left.png left3.png &&
  cp left.png left4.png &&
  cp left.png left5.png
"

start_job "signboard right" bash -lc "
  cd '$SCRIPT_DIR/signboards' &&
  ./signage_from_csv.py \
    --bg-image right-tv.jpg \
    '$SKUS' \
    --text both \
    --format png \
    --page-height 900 \
    --vertical-offset 40 \
    --out '$OUTPUT_DIR/signboards/right.png' &&
  cd '$OUTPUT_DIR/signboards' &&
  cp right.png right2.png &&
  cp right.png right3.png &&
  cp right.png right4.png
"

start_job "circle lids" bash -lc "
  cd '$SCRIPT_DIR/lids' &&
  ./circle.py \
    --out-dir '$OUTPUT_DIR/lids' \
    --diameters 1.25 1.5 \
    --bg-image hemp-dahntahn.png \
    --exclude-sku-regex 'BB-[A-Z]+-(HO|OZ|QP|LB)' \
    '$SKUS'
"

start_job "circle lids bbuds" bash -lc "
  cd '$SCRIPT_DIR/lids' &&
  ./circle.py \
    --out-dir '$OUTPUT_DIR/lids/bbuds' \
    --diameters 1.25 1.5 \
    --bg-image hemp-dahntahn.png \
    --sku-regex 'BB-[A-Z]+-(HO|OZ|QP|LB)' \
    '$SKUS'
"

start_job "circle lids barcode" bash -lc "
  cd '$SCRIPT_DIR/lids' &&
  ./circle.py \
    --out-dir '$OUTPUT_DIR/lids/barcode' \
    --diameters 1.25 1.5 \
    --code-style barcode \
    --category-prefix Flower \
    --sku-regex 'FL[A-Z]+[EQ]' \
    '$SKUS'
"

start_job "circle lids qr" bash -lc "
  cd '$SCRIPT_DIR/lids' &&
  ./circle.py \
    --out-dir '$OUTPUT_DIR/lids/qr' \
    --diameters 1.25 1.5 \
    --code-style qr \
    --category-prefix Flower \
    --sku-regex 'FL[A-Z]+[EQ]' \
    '$SKUS'
"

start_job "circle jars" bash -lc "
  cd '$SCRIPT_DIR/lids' &&
  ./circle.py \
    --out-dir '$OUTPUT_DIR/lids/jars' \
    --diameters 2.15 \
    --bg-image jar.png \
    --exclude-sku-regex 'BB-[A-Z]+-(HO|OZ|QP|LB)' \
    '$SKUS'
"

start_job "circle jars bbuds" bash -lc "
  cd '$SCRIPT_DIR/lids' &&
  ./circle.py \
    --out-dir '$OUTPUT_DIR/lids/jars/bbuds' \
    --diameters 2.15 \
    --bg-image jar.png \
    --sku-regex 'BB-[A-Z]+-(HO|OZ|QP|LB)' \
    '$SKUS'
"

start_job "mason jar signboard pdf+png" bash -lc "
  cd '$SCRIPT_DIR/grid-signboard' &&
  ./grid_to_signboard_pdf.py ./images/ splash-background.jpg --cols 5 --rows 5 &&
  pdftoppm -png -r 200 grid_signboard.pdf \
    '$OUTPUT_DIR/grid_signboard'
"

start_job "barcode normal" bash -lc "
  python3 '$BARCODE_SCRIPT' '$SKUS' \
    --category-prefix Flower \
    --sku-regex 'FL[A-Z]+[EQ]' \
    --out-dir '$OUTPUT_DIR/barcodes/'
"

start_job "barcode bbuds" bash -lc "
  python3 '$BARCODE_SCRIPT' '$SKUS' \
    --sku-regex 'BB-[A-Z]+-(HO|OZ|QP|LB)' \
    --out-dir '$OUTPUT_DIR/barcodes/bbuds/'
"

start_job "barcode short" bash -lc "
  python3 '$BARCODE_SCRIPT' '$SKUS' \
    --category-prefix Flower \
    --sku-regex 'FL[A-Z]+[EQ]' \
    --out-dir '$OUTPUT_DIR/barcodes-short/' \
    --label-height $LABEL_HEIGHT
"

start_job "barcode short bbuds" bash -lc "
  python3 '$BARCODE_SCRIPT' '$SKUS' \
    --sku-regex 'BB-[A-Z]+-(HO|OZ|QP|LB)' \
    --out-dir '$OUTPUT_DIR/barcodes-short/bbuds/' \
    --label-height $LABEL_HEIGHT
"

start_job "barcode packed page" bash -lc "
  python3 '$BARCODE_SCRIPT' '$SKUS' \
    --category-prefix Flower \
    --sku-regex 'FL[A-Z]+[EQ]|BB-[A-Z]+-HO' \
    --sheet-mode \
    --pair-flower-sheet \
    --sort-by-name \
    --out-pdf '$OUTPUT_DIR/barcodes-page/barcodes_page.pdf' \
    --label-height $LABEL_HEIGHT \
    --sheet-outline-mode guide \
    --margin-left 0.25 \
    --margin-right 0.25 \
    --margin-top 0.25 \
    --margin-bottom 0.25
"

start_job "barcode packed page bbuds" bash -lc "
  python3 '$BARCODE_SCRIPT' '$SKUS' \
    --sku-regex 'BB-[A-Z]+-(HO|OZ|QP|LB)' \
    --sheet-mode \
    --out-pdf '$OUTPUT_DIR/barcodes-page/bbuds/barcodes_page.pdf' \
    --label-height $LABEL_HEIGHT \
    --sheet-outline-mode guide \
    --margin-left 0.25 \
    --margin-right 0.25 \
    --margin-top 0.25 \
    --margin-bottom 0.25
"

start_job "barcode edibles" bash -lc "
  python3 '$BARCODE_SCRIPT' '$SKUS' \
    --category-prefix Edibles \
    --sku-regex '[0-9]+' \
    --out-dir '$OUTPUT_DIR/barcodes/edibles' \
    --label-height 0.5 \
    --individual-outline-mode cutcontour \
    --outline-shape round
"

wait_for_jobs
echo 'All jobs completed successfully.'