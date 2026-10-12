#!/usr/bin/env bash
set -Eeuo pipefail

# Build a separate, instrumented nginx binary, never install/reload services.
# Usage: bash tools/build_profiled.sh TARGET_DIRECTORY NGINX_1.30.5_TARBALL
if [ "$#" -ne 2 ]; then
    echo "Usage: $0 NEW_ABSOLUTE_DIRECTORY EXISTING_NGINX_1.30.5_TARBALL" >&2
    exit 2
fi

target="$1"
tarball="$2"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$target" in
    /*) ;;
    *) echo "TARGET_DIRECTORY must be absolute" >&2; exit 2 ;;
esac
if [ -e "$target" ]; then
    echo "Refusing to replace an existing directory: $target" >&2
    exit 2
fi
if [ ! -f "$tarball" ]; then
    echo "Missing nginx source tarball: $tarball" >&2
    exit 2
fi

mkdir -m 0700 -p "$target"
tar -xzf "$tarball" -C "$target" nginx-1.30.5
source="$target/nginx-1.30.5"
cp "$repo/ngx_http_image_filter_module.c" \
    "$source/src/http/modules/ngx_http_image_filter_module.c"

(
    cd "$source"
    ./configure \
        --prefix="$target/isolated-prefix" \
        --with-http_image_filter_module \
        --with-cc-opt="-DNGX_WM_PROFILE" \
        > "$target/configure.log" 2>&1

    make -j2 > "$target/build.log" 2>&1
)
"$source/objs/nginx" -V 2>&1
echo
echo "Instrumented nginx: $source/objs/nginx"
echo "No existing nginx installation or service was modified."
