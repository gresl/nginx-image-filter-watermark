#!/usr/bin/env bash
set -Eeuo pipefail

# Build identical baseline and candidate nginx binaries without installing
# either nginx, altering /etc/nginx, or opening any network listeners.
#
# Usage: bash tools/build_isolated.sh [WORKDIR]
# Workdir defaults to /root/image-module-bench-YYYYmmdd-HHMMSS.
# Run from a checkout of optimization/watermark-hotpath-2026-10.

NGINX_VERSION="1.30.5"
STABLE_SHA="ef879803c1d0405e87f7a59b9598bd3e49c2bf9b"
WORK="${1:-/root/image-module-bench-$(date +%Y%m%d-%H%M%S)}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOBS="${BENCH_BUILD_JOBS:-2}"

for command in git curl tar make gcc; do
    command -v "$command" >/dev/null || {
        echo "Missing build prerequisite: $command" >&2
        exit 1
    }
done

case "$WORK" in
    /*) ;;
    *) echo "WORKDIR must be an absolute path" >&2; exit 2 ;;
esac
case "$JOBS" in
    1|2) ;;
    *) echo "BENCH_BUILD_JOBS must be 1 or 2" >&2; exit 2 ;;
esac

if [ -e "$WORK" ]; then
    echo "Refusing to reuse an existing directory: $WORK" >&2
    exit 2
fi
if ! git -C "$REPO" cat-file -e "$STABLE_SHA:ngx_http_image_filter_module.c"; then
    echo "Stable commit missing from checkout; fetch origin first." >&2
    exit 1
fi

mkdir -m 0700 -p "$WORK"
TARBALL="$WORK/nginx-$NGINX_VERSION.tar.gz"

echo "Downloading nginx $NGINX_VERSION source to isolated workdir..."
curl --fail --location --silent --show-error --retry 2 \
    "https://nginx.org/download/nginx-$NGINX_VERSION.tar.gz" \
    -o "$TARBALL"

for variant in baseline candidate; do
    target="$WORK/$variant"
    mkdir -p "$target"
    tar -xzf "$TARBALL" -C "$target"
    nginxsrc="$target/nginx-$NGINX_VERSION"
    module="$nginxsrc/src/http/modules/ngx_http_image_filter_module.c"

    if [ "$variant" = baseline ]; then
        git -C "$REPO" show "$STABLE_SHA:ngx_http_image_filter_module.c" > "$module"
    else
        cp "$REPO/ngx_http_image_filter_module.c" "$module"
    fi

    echo
    echo "=== Building $variant nginx $NGINX_VERSION (-j$JOBS) ==="
    (
        cd "$nginxsrc"
        ./configure --with-http_image_filter_module \
            --prefix="$target/isolated-prefix" \
            > "$target/configure.log" 2>&1
        make -j"$JOBS" > "$target/build.log" 2>&1
        ./objs/nginx -V 2>&1 | head -n 4
    ) || {
        echo "Build failed; inspect $target/configure.log and $target/build.log" >&2
        exit 1
    }
    echo "Built $nginxsrc/objs/nginx"
done

printf '\nBaseline commit: %s\nCandidate commit: %s\n' \
    "$STABLE_SHA" "$(git -C "$REPO" rev-parse HEAD)"
echo "Workdir: $WORK"
echo "No nginx service was started, installed or reloaded."
