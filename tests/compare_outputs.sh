#!/usr/bin/env bash
set -euo pipefail

# Compare the stable and candidate image-filter implementations using
# identical nginx builds, GD libraries, fixtures and configuration.
# No production hosts, URLs, or customer images are used here.
if [ "$#" -ne 2 ]; then
    echo "Usage: $0 BASELINE_NGINX_SOURCE_DIR CANDIDATE_NGINX_SOURCE_DIR" >&2
    exit 2
fi

baseline="$1"
candidate="$2"
work="$(mktemp -d)"
port=18763

cleanup() {
    for name in baseline candidate; do
        if [ -f "$work/$name/nginx.pid" ]; then
            "$([ "$name" = baseline ] && echo "$baseline" || echo "$candidate")/objs/nginx" \
                -p "$work/$name/" -c "$work/$name/nginx.conf" -s quit 2>/dev/null || true
        fi
    done
    rm -rf -- "$work"
}
trap cleanup EXIT

mkdir -p "$work/files"
files="$work/files"

convert -size 640x400 gradient:'#174060-#d3a15d' "$files/photo.jpg"
convert -size 640x400 gradient:'#174060-#d3a15d' "$files/opaque.png"
convert -size 640x400 xc:none \
    -fill 'rgba(255,0,0,0.50)' -draw 'rectangle 25,25 580,350' \
    -fill 'rgba(0,255,0,0.35)' -draw 'circle 300,180 400,180' \
    "$files/transparent.png"
convert "$files/opaque.png" "$files/photo.gif"
convert -size 95x75 xc:none \
    -fill 'rgba(20,20,20,0.65)' -draw 'roundrectangle 0,0 94,74 10,10' \
    -fill white -pointsize 12 -draw 'text 12,42 WM' \
    "$files/watermark.png" || {
        # Avoid relying on fonts installed on a specific runner.
        convert -size 95x75 xc:none \
            -fill 'rgba(20,20,20,0.65)' -draw 'rectangle 0,0 94,74' \
            "$files/watermark.png"
    }

cases=(
    'watermark/photo.jpg'
    'watermark/opaque.png'
    'watermark/transparent.png'
    'watermark/photo.gif'
    'resize/photo.jpg'
    'resize/opaque.png'
    'resize/transparent.png'
    'resize/photo.gif'
    'missing/photo.jpg'
    'missing/transparent.png'
)

run_build() {
    local name="$1" source="$2"
    local prefix="$work/$name"
    mkdir -p "$prefix/out"

    cat > "$prefix/nginx.conf" <<EOF
worker_processes 1;
pid $prefix/nginx.pid;
error_log $prefix/error.log warn;
events { worker_connections 128; }
http {
    include $source/conf/mime.types;
    default_type application/octet-stream;
    access_log off;
    server {
        listen 127.0.0.1:$port;
        location /watermark/ {
            rewrite ^/watermark/(.+)$ /\$1 break;
            root $files;
            image_filter watermark;
            image_filter_watermark $files/watermark.png;
            image_filter_watermark_position bottom-right;
            image_filter_watermark_width_from 0;
            image_filter_watermark_height_from 0;
            image_filter_buffer 32M;
        }
        location /resize/ {
            rewrite ^/resize/(.+)$ /\$1 break;
            root $files;
            image_filter resize 320 -;
            image_filter_watermark $files/watermark.png;
            image_filter_watermark_position bottom-right;
            image_filter_watermark_width_from 0;
            image_filter_watermark_height_from 0;
            image_filter_buffer 32M;
        }
        location /missing/ {
            rewrite ^/missing/(.+)$ /\$1 break;
            root $files;
            image_filter watermark;
            image_filter_watermark $files/nonexistent.png;
            image_filter_watermark_width_from 0;
            image_filter_watermark_height_from 0;
            image_filter_buffer 32M;
        }
    }
}
EOF

    "$source/objs/nginx" -t -p "$prefix/" -c "$prefix/nginx.conf"
    "$source/objs/nginx" -p "$prefix/" -c "$prefix/nginx.conf"

    local path code
    for path in "${cases[@]}"; do
        code="$(curl --silent --show-error --max-time 15 \
            -o "$prefix/out/$(basename "$(dirname "$path")")-$(basename "$path")" \
            -w '%{http_code}' "http://127.0.0.1:$port/$path")"
        if [ "$code" != 200 ]; then
            echo "$name $path returned HTTP $code (expected 200)" >&2
            cat "$prefix/error.log" >&2
            return 1
        fi
        convert "$prefix/out/$(basename "$(dirname "$path")")-$(basename "$path")" null:
    done

    # Diagnostic microbenchmark; not a CI pass/fail gate.
    python3 - "$port" "$name" <<'PY'
import math
import statistics
import sys
import time
import urllib.request

port, variant = sys.argv[1:3]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

for label, url in (
    ("JPEG+WM resize", "resize/photo.jpg"),
    ("PNG+WM resize", "resize/transparent.png"),
    ("PNG+WM only", "watermark/transparent.png"),
    ("GIF+WM resize", "resize/photo.gif"),
):
    endpoint = f"http://127.0.0.1:{port}/{url}"
    for _ in range(5):
        with opener.open(endpoint, timeout=15) as response:
            response.read()
    elapsed = []
    for _ in range(35):
        start = time.perf_counter_ns()
        with opener.open(endpoint, timeout=15) as response:
            response.read()
        elapsed.append((time.perf_counter_ns() - start) / 1_000_000)
    ordered = sorted(elapsed)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
    print(
        f"BENCH {variant} {label}: n={len(elapsed)} "
        f"median={statistics.median(ordered):.3f}ms "
        f"p95={p95:.3f}ms mean={statistics.mean(ordered):.3f}ms"
    )
PY

    "$source/objs/nginx" -p "$prefix/" -c "$prefix/nginx.conf" -s quit
    sleep 1
}

run_build baseline "$baseline"
run_build candidate "$candidate"

echo "Comparing ${#cases[@]} image responses, byte for byte."
for file in "$work/baseline/out/"*; do
    name="$(basename "$file")"
    if ! cmp -s "$file" "$work/candidate/out/$name"; then
        echo "OUTPUT DIFFERENCE: $name" >&2
        sha256sum "$file" "$work/candidate/out/$name" >&2
        exit 1
    fi
    echo "MATCH: $name"
done

echo "Output compatibility: PASS"
