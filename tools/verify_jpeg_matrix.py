#!/usr/bin/env python3
"""Bounded real-JPEG / watermark output-parity matrix for two isolated nginx builds.

Read-only production inputs. Copies are kept in /var/tmp and removed afterwards.
The test binds a single localhost port; it never installs or reloads nginx.
"""
import argparse
import csv
import grp
import hashlib
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from benchmark_local import check_port, copy_image, start_nginx, stop_nginx


def config(prefix, fixtures, nginx_source, port, watermark_count):
    lines = [
        "worker_processes 1;",
        "user nginx nginx;",
        f"pid {prefix}/nginx.pid;",
        f"error_log {prefix}/error.log warn;",
        "events { worker_connections 128; }",
        "http {",
        f"    include {nginx_source}/conf/mime.types;",
        "    default_type application/octet-stream;",
        "    access_log off;",
        "    server {",
        f"        listen 127.0.0.1:{port};",
    ]
    for i in range(watermark_count):
        for mode, filter_line in (
            ("r", "image_filter resize 640 -;"),
            ("s", "image_filter watermark;"),
        ):
            loc = f"/{mode}{i}/"
            lines.extend([
                f"        location {loc} {{",
                f"            rewrite ^/{mode}{i}/(.+)$ /$1 break;",
                f"            root {fixtures};",
                f"            {filter_line}",
                "            image_filter_buffer 16M;",
                "            image_filter_jpeg_quality 85;",
                f"            image_filter_watermark {fixtures}/watermark{i}.png;",
                "            image_filter_watermark_position bottom-right;",
                "            image_filter_watermark_width_from 0;",
                "            image_filter_watermark_height_from 0;",
                "        }",
            ])
    lines.extend(["    }", "}", ""])
    return "\n".join(lines)


def digest_file(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for piece in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(piece)
    return h.hexdigest()


def fetch_hash(opener, port, path):
    url = f"http://127.0.0.1:{port}/{path}"
    with opener.open(url, timeout=20) as response:
        data = response.read()
        if response.status != 200:
            raise RuntimeError(f"{path}: HTTP {response.status}")
        if response.headers.get("Content-Type", "").split(";")[0] != "image/jpeg":
            raise RuntimeError(f"{path}: unexpected Content-Type")
    if not data or len(data) > 16 * 1024 * 1024:
        raise RuntimeError(f"{path}: invalid output size {len(data)}")
    return hashlib.sha256(data).hexdigest(), len(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first-binary", type=Path, required=True)
    parser.add_argument("--second-binary", type=Path, required=True)
    parser.add_argument("--jpeg", type=Path, action="append", required=True,
                        help="Repeat for 1..6 representative JPEGs")
    parser.add_argument("--watermark", type=Path, action="append", required=True,
                        help="Repeat for 1..6 representative agency PNGs")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    if os.geteuid() != 0:
        parser.error("run as root for isolated nginx master and worker permissions")
    if not 1 <= len(args.jpeg) <= 6:
        parser.error("select 1..6 JPEGs")
    if not 1 <= len(args.watermark) <= 6:
        parser.error("select 1..6 watermarks")
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    if args.report.exists():
        parser.error(f"refusing to overwrite report {args.report}")
    for label, files in (("JPEG", args.jpeg), ("watermark", args.watermark)):
        for path in files:
            if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                parser.error(f"{label} missing or over 16 MiB: {path}")
    for binary in (args.first_binary, args.second_binary):
        if not binary.is_file() or not os.access(binary, os.X_OK):
            parser.error(f"missing/unexecutable isolated binary: {binary}")
    grp.getgrnam("nginx")
    check_port(args.port)
    opener = build_opener(ProxyHandler({}))

    routes = [
        (f"{mode}{wm}/photo{photo}.jpg", photo, wm, mode)
        for wm in range(len(args.watermark))
        for photo in range(len(args.jpeg))
        for mode in ("r", "s")
    ]
    routes.extend((f"s{wm}/small.jpg", "small", wm, "skip")
                  for wm in range(len(args.watermark)))
    expected_count = len(routes)
    hashes = {}
    rows = []
    process = None
    order = ("first", "second", "second", "first")
    binaries = {"first": args.first_binary, "second": args.second_binary}

    with tempfile.TemporaryDirectory(prefix="wmmatrix-", dir="/var/tmp") as directory:
        fixtures = Path(directory)
        os.chown(fixtures, 0, grp.getgrnam("nginx").gr_gid)
        os.chmod(fixtures, 0o750)
        for idx, image in enumerate(args.jpeg):
            copy_image(image, fixtures / f"photo{idx}.jpg")
        for idx, image in enumerate(args.watermark):
            copy_image(image, fixtures / f"watermark{idx}.png")

        small = fixtures / "small.jpg"
        subprocess.run([
            "convert", "-size", "140x90", "gradient:#174060-#e3b36f",
            "-quality", "85", str(small)
        ], check=True, timeout=30, stdout=subprocess.DEVNULL)
        os.chown(small, 0, grp.getgrnam("nginx").gr_gid)
        os.chmod(small, 0o640)
        small_hash = digest_file(small)

        try:
            for run, variant in enumerate(order, 1):
                check_port(args.port)
                prefix = fixtures / f"run-{run}-{variant}"
                prefix.mkdir(mode=0o755)
                (prefix / "logs").mkdir(mode=0o755)
                binary = binaries[variant]
                nginx_source = binary.parent.parent
                nginx_conf = prefix / "nginx.conf"
                nginx_conf.write_text(config(
                    prefix, fixtures, nginx_source, args.port,
                    len(args.watermark)
                ))
                process = start_nginx(binary, prefix, nginx_conf, args.port)

                for route, photo, wm, mode in routes:
                    actual, size = fetch_hash(opener, args.port, route)
                    # Check a repeated request for accidental nondeterminism.
                    repeated, repeat_size = fetch_hash(opener, args.port, route)
                    if actual != repeated or size != repeat_size:
                        raise RuntimeError(f"NONDETERMINISTIC {variant}: {route}")
                    if mode == "skip" and actual != small_hash:
                        raise RuntimeError(f"OVERSIZE SKIP CHANGED: {variant} {route}")
                    if route in hashes and hashes[route] != actual:
                        raise RuntimeError(f"OUTPUT DIFFERENCE: {variant} {route}")
                    hashes.setdefault(route, actual)
                    rows.append(dict(run=run, variant=variant,
                                     jpeg=str(photo), watermark=str(wm),
                                     mode=mode, route=route,
                                     response_bytes=size, sha256=actual))

                stop_nginx(process)
                process = None
                time.sleep(0.2)
                print(f"{run} {variant:6s} PASS: {expected_count} routes "
                      "(each requested twice)", flush=True)
        finally:
            stop_nginx(process)

    with args.report.open("x", newline="") as report:
        writer = csv.DictWriter(report, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Output parity: PASS ({expected_count} scenarios, "
          f"{len(args.jpeg)} JPEGs x {len(args.watermark)} watermarks, "
          "including oversized-watermark skips)")
    print(f"Report: {args.report}")
    print("All isolated test nginx processes stopped; temporary copies removed.")


if __name__ == "__main__":
    main()
