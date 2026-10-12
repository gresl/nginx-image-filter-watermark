#!/usr/bin/env bash
set -Eeuo pipefail

# Compile a separate, uninstrumented nginx candidate without installing,
# starting, or reloading nginx. Preserve all pre-existing benchmark binaries.
# Usage: bash tools/build_candidate_only.sh NEW_ABSOLUTE_DIR NGINX_SOURCE_TARBALL
if [ "$#" -ne 2 ]; then
    echo "Usage: $0 NEW_ABSOLUTE_DIR NGINX_1.30.5_TARBALL" >&2
    exit 2
fi

target="$1"
tarball="$2"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$target" in
    /*) ;;
    *) echo "TARGET_DIR must be an absolute path" >&2; exit 2 ;;
esac
if [ -e "$target" ]; then
    echo "Refusing to modify existing directory: $target" >&2
    exit 2
fi
if [ ! -f "$tarball" ]; then
    echo "nginx 1.30.5 source tarball not found: $tarball" >&2
    exit 2
fi
if ! git -C "$repo" rev-parse HEAD >/dev/null; then
    echo "Run this from a GitHub working-tree checkout" >&2
    exit 2
fi

mkdir -m 0700 -p "$target"
tar -xzf "$tarball" -C "$target" nginx-1.30.5
source="$target/nginx-1.30.5"

cp "$repo/ngx_http_image_filter_module.c" \
    "$source/src/http/modules/ngx_http_image_filter_module.c"

echo "Building JPEG background-copy experiment (-j2)"
(
    cd "$source"
    ./configure \
        --prefix="$target/isolated-prefix" \
        --with-http_image_filter_module \
        > "$target/configure.log" 2>&1
    make -j2 > "$target/build.log" 2>&1
) || {
    echo "Build failed. Read $target/configure.log and $target/build.log" >&2
    exit 1
}

"$source/objs/nginx" -V 2>&1
echo "Candidate revision: $(git -C "$repo" rev-parse HEAD)"
echo "Candidate binary: $source/objs/nginx"
echo "No nginx services started, installed, or reloaded."
