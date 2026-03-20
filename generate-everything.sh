#!/usr/bin/env bash
set -Eeuo pipefail
set -x

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
echo "script dir: $SCRIPT_DIR"

OUTPUT_DIR=/tmp/script_output
LABEL_HEIGHT=0.75
SKUS="$SCRIPT_DIR/skus/skus.csv"

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
  "$OUTPUT_DIR/lids/jars" \
  "$OUTPUT_DIR/barcodes" \
  "$OUTPUT_DIR/barcodes/edibles" \
  "$OUTPUT_DIR/barcodes-short" \
  "$OUTPUT_DIR/barcodes-page" \
  "$OUTPUT_DIR/grid_signboard"

start_job "signboard left" bash -lc "
  cd '$SCRIPT_DIR/signboards' &&
  ./signage_from_csv.py --bg-image left-tv.jpg '$SKUS' --text both --format png \
    --out '$OUTPUT_DIR/signboards/left.png'
"

start_job "signboard right" bash -lc "
  cd '$SCRIPT_DIR/signboards' &&
  ./signage_from_csv.py --bg-image right-tv.jpg '$SKUS' --text both --format png \
    --out '$OUTPUT_DIR/signboards/right.png'
"

start_job "circle lids" bash -lc "
  cd '$SCRIPT_DIR/lids' &&
  ./circle.py \
    --out-dir '$OUTPUT_DIR/lids' \
    --diameters 1.25 1.5 \
    --bg-image hemp-dahntahn.png \
    '$SKUS'
"

start_job "circle jars" bash -lc "
  cd '$SCRIPT_DIR/lids' &&
  ./circle.py \
    --out-dir '$OUTPUT_DIR/lids/jars' \
    --diameters 2.15 \
    --bg-image jar.png \
    '$SKUS'
"

start_job "mason jar signboard pdf+png" bash -lc "
  cd '$SCRIPT_DIR/grid-signboard' &&
  ./grid_to_signboard_pdf.py ./images/ splash-background.jpg --cols 5 --rows 5 &&
  pdftoppm -png -r 200 grid_signboard.pdf \
    '$OUTPUT_DIR/grid_signboard'
"

start_job "barcode normal" bash -lc "
  cd '$SCRIPT_DIR/barcodes/flower-barcodes' &&
  ./barcode.py '$SKUS' \
    --barcodes-dir ./barcode-images/ \
    --out-dir '$OUTPUT_DIR/barcodes/'
"

start_job "barcode short" bash -lc "
  cd '$SCRIPT_DIR/barcodes/flower-barcodes' &&
  ./barcode.py '$SKUS' \
    --barcodes-dir ./barcode-images/ \
    --out-dir '$OUTPUT_DIR/barcodes-short/' \
    --label-height $LABEL_HEIGHT \
    --no-bounding-rect
"

start_job "barcode packed page" bash -lc "
  cd '$SCRIPT_DIR/barcodes/flower-barcodes' &&
  ./barcode.py '$SKUS' \
    --barcodes-dir ./barcode-images \
    --sheet-mode \
    --out-pdf '$OUTPUT_DIR/barcodes-page/barcodes_page.pdf' \
    --label-height $LABEL_HEIGHT \
    --no-bounding-rect \
    --margin-left 0.25 \
    --margin-right 0.25 \
    --margin-top 0.25 \
    --margin-bottom 0.25
"

start_job "barcode edibles" bash -lc "
  cd '$SCRIPT_DIR/barcodes/edibles-barcodes' &&
  python3 ./barcode_label_maker.py \
    --input-dir ./barcode-images \
    --csv '$SKUS' \
    --output-dir '$OUTPUT_DIR/barcodes/edibles' \
    --height-in 0.5 \
    --format pdf
"

wait_for_jobs
echo 'All jobs completed successfully.'