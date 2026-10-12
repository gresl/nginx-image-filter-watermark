#!/usr/bin/env bash
set -Eeuo pipefail

# Smoke-test the full lifecycle of tools/benchmark_local.py on an isolated
# GitHub Actions runner, including 4 nginx start/stop cycles and TCP TIME_WAIT.
# The test uses generated fixtures and never contacts production.
if [ "$#" -ne 1 ]; then
    echo "Usage: $0 WORKDIR_CONTAINING_BUILT_BASELINE_AND_CANDIDATE" >&2
    exit 2
fi
workdir="$1"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if ! getent group nginx >/dev/null; then
    sudo groupadd --system nginx
fi
if ! getent passwd nginx >/dev/null; then
    sudo useradd --system --gid nginx --home /nonexistent \
        --shell /usr/sbin/nologin nginx
fi

convert -size 1080x720 gradient:'#285c92-#e0b778' -quality 88 \
    "$tmp/photo.jpg"
convert -size 100x100 xc:'rgba(15,35,50,0.40)' \
    "$tmp/watermark.png"

sudo python3 tools/benchmark_local.py \
    --workdir "$workdir" \
    --source-jpeg "$tmp/photo.jpg" \
    --watermark-png "$tmp/watermark.png" \
    --report "$tmp/report.csv" \
    --port 18764 \
    --repetitions 10

python3 - "$tmp/report.csv" <<'PY'
import csv
import sys
from collections import Counter
with open(sys.argv[1], newline='') as stream:
    rows = list(csv.DictReader(stream))
assert len(rows) == 24, f'Expected 24 benchmark rows, got {len(rows)}'
assert Counter(r['variant'] for r in rows) == {
    'baseline': 12, 'candidate': 12
}
assert {r['case'] for r in rows} == {
    'JPEG resize', 'JPEG resize + WM',
    'PNG resize + WM', 'GIF resize + WM',
    'JPEG WM only', 'PNG WM only'
}
print('ABBA lifecycle, output parity and CSV structure: PASS')
PY
