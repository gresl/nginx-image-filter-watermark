#!/usr/bin/env python3
"""Compare isolated nginx builds on real JPEG + agency watermark fixtures.

Never installs/reloads nginx; binds exclusively to localhost. Sources are copied
to a private temporary directory and deleted after the test.
"""
import argparse
import csv
import grp
import hashlib
import math
import os
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

VERSION = "1.30.5"
CASES = (
    ("JPEG resize", "plain/photo.jpg"),
    ("JPEG resize + WM", "wm/photo.jpg"),
    ("PNG resize + WM", "wm/photo.png"),
    ("GIF resize + WM", "wm/photo.gif"),
    ("JPEG WM only", "stamp/photo.jpg"),
    ("PNG WM only", "stamp/photo.png"),
)
ORDER = ("baseline", "candidate", "candidate", "baseline")


def nginx_config(directory, sources, port):
    return f"""
worker_processes 1;
user nginx nginx;
pid {directory}/nginx.pid;
error_log {directory}/error.log warn;
events {{ worker_connections 128; }}
http {{
    include {sources}/conf/mime.types;
    default_type application/octet-stream;
    access_log off;
    server {{
        listen 127.0.0.1:{port};
        location /plain/ {{
            rewrite ^/plain/(.+)$ /$1 break;
            root {directory.parent};
            image_filter resize 640 -;
            image_filter_buffer 16M;
            image_filter_jpeg_quality 85;
        }}
        location /wm/ {{
            rewrite ^/wm/(.+)$ /$1 break;
            root {directory.parent};
            image_filter resize 640 -;
            image_filter_buffer 16M;
            image_filter_jpeg_quality 85;
            image_filter_watermark {directory.parent}/watermark.png;
            image_filter_watermark_position bottom-right;
            image_filter_watermark_width_from 0;
            image_filter_watermark_height_from 0;
        }}
        location /stamp/ {{
            rewrite ^/stamp/(.+)$ /$1 break;
            root {directory.parent};
            image_filter watermark;
            image_filter_buffer 16M;
            image_filter_watermark {directory.parent}/watermark.png;
            image_filter_watermark_position bottom-right;
            image_filter_watermark_width_from 0;
            image_filter_watermark_height_from 0;
        }}
    }}
}}
"""


def copy_image(src, dst):
    import shutil
    shutil.copyfile(src, dst)
    os.chown(dst, 0, grp.getgrnam('nginx').gr_gid)
    os.chmod(dst, 0o640)


def convert(src, dst):
    subprocess.run(
        ["convert", str(src), str(dst)],
        check=True, stdout=subprocess.DEVNULL, timeout=45,
    )
    os.chown(dst, 0, grp.getgrnam('nginx').gr_gid)
    os.chmod(dst, 0o640)


def check_port(port):
    # The previous nginx listener may have closed already while its HTTP
    # connections remain in TCP TIME_WAIT.  A plain bind() incorrectly
    # reports EADDRINUSE in that case.  nginx itself uses SO_REUSEADDR.
    # A genuinely active listener still prevents the new listen().
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(1)


def start_nginx(binary, prefix, config, port):
    cmd = [str(binary), "-p", str(prefix) + "/", "-c", str(config),
           "-g", "daemon off;"]
    subprocess.run(
        [str(binary), "-t", "-p", str(prefix) + "/", "-c", str(config)],
        check=True, timeout=15,
    )
    process = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"nginx exited with {process.returncode}; "
                               f"see {prefix}/error.log")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return process
        except OSError:
            time.sleep(0.05)
    stop_nginx(process)
    raise RuntimeError(f"nginx did not start; see {prefix}/error.log")


def stop_nginx(process):
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGQUIT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def worker_peak_kib(master_pid):
    path = Path(f"/proc/{master_pid}/task/{master_pid}/children")
    try:
        children = path.read_text().split()
        peaks = []
        for pid in children:
            status = Path(f"/proc/{pid}/status").read_text()
            for line in status.splitlines():
                if line.startswith("VmHWM:"):
                    peaks.append(int(line.split()[1]))
                    break
        return max(peaks) if peaks else ""
    except (OSError, ValueError):
        return ""


def measure(opener, port, label, route, repetitions, variant, run):
    url = f"http://127.0.0.1:{port}/{route}"
    digest = ""
    received = 0
    timings = []
    for i in range(repetitions + 5):
        before = time.perf_counter_ns()
        with opener.open(url, timeout=20) as response:
            if response.status != 200:
                raise RuntimeError(f"{variant} {route}: HTTP {response.status}")
            data = response.read()
            if not response.headers.get("Content-Type", "").startswith("image/"):
                raise RuntimeError(f"{variant} {route}: unexpected Content-Type")
        elapsed = (time.perf_counter_ns() - before) / 1_000_000
        if i == 0:
            digest = hashlib.sha256(data).hexdigest()
            received = len(data)
        elif hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError(f"Unstable response content: {variant} {route}")
        if i >= 5:
            timings.append(elapsed)

    ordered = sorted(timings)
    p95 = ordered[math.ceil(len(ordered) * .95) - 1]
    row = dict(
        run=run, variant=variant, case=label, route=route,
        n=repetitions, median_ms=round(statistics.median(ordered), 3),
        p95_ms=round(p95, 3),
        mean_ms=round(statistics.mean(ordered), 3),
        response_bytes=received, sha256=digest, worker_peak_rss_kib="",
    )
    print(f"{run} {variant:9s} {label:18s}: "
          f"median={row['median_ms']:.3f} ms "
          f"p95={row['p95_ms']:.3f} ms", flush=True)
    return row


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workdir", type=Path, required=True,
                   help="Existing build directory containing baseline/candidate")
    p.add_argument("--source-jpeg", type=Path, required=True)
    p.add_argument("--watermark-png", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--port", type=int, default=18764)
    p.add_argument("--repetitions", type=int, default=35)
    args = p.parse_args()

    if os.geteuid() != 0:
        p.error('run as root: isolated nginx master and nginx group permissions')
    if not (1024 <= args.port <= 65535):
        p.error("port must be between 1024 and 65535")
    if not (10 <= args.repetitions <= 100):
        p.error("repetitions must be 10..100")
    if args.report.exists():
        p.error(f"report already exists: {args.report}")
    for name, source in (("source JPEG", args.source_jpeg),
                         ("watermark PNG", args.watermark_png)):
        if not source.is_file():
            p.error(f"{name} not found: {source}")
        if source.stat().st_size > 16 * 1024 * 1024:
            p.error(f"{name} exceeds 16 MiB safety limit: {source}")
    binaries = {
        name: (args.workdir / name / f"nginx-{VERSION}" / "objs/nginx")
        for name in ("baseline", "candidate")
    }
    for name, binary in binaries.items():
        if not binary.is_file():
            p.error(f"{name} binary missing: {binary}")
    check_port(args.port)
    opener = build_opener(ProxyHandler({}))
    results = []
    process = None

    with tempfile.TemporaryDirectory(prefix="wmbench-", dir="/var/tmp") as tmp:
        fixtures = Path(tmp)
        os.chown(fixtures, 0, grp.getgrnam('nginx').gr_gid)
        os.chmod(fixtures, 0o750)
        copy_image(args.source_jpeg, fixtures / "photo.jpg")
        copy_image(args.watermark_png, fixtures / "watermark.png")
        convert(fixtures / "photo.jpg", fixtures / "photo.png")
        convert(fixtures / "photo.jpg", fixtures / "photo.gif")

        try:
            for seq, name in enumerate(ORDER, 1):
                check_port(args.port)
                prefix = fixtures / f"run-{seq}-{name}"
                prefix.mkdir(mode=0o755)
                # nginx opens its default error log before processing
                # error_log in the supplied configuration during -t.
                (prefix / "logs").mkdir(mode=0o755)
                nginxsrc = binaries[name].parent.parent
                conf = prefix / "nginx.conf"
                conf.write_text(nginx_config(prefix, nginxsrc, args.port))
                process = start_nginx(binaries[name], prefix, conf, args.port)
                block = []
                for label, route in CASES:
                    block.append(measure(opener, args.port, label, route,
                                         args.repetitions, name, seq))
                peak = worker_peak_kib(process.pid)
                for row in block:
                    row["worker_peak_rss_kib"] = peak
                results.extend(block)
                stop_nginx(process)
                process = None
                time.sleep(.2)
        finally:
            stop_nginx(process)

    hashes = {}
    for row in results:
        key = row["case"]
        if key in hashes and hashes[key] != row["sha256"]:
            raise RuntimeError(f"OUTPUT DIFFERENCE for {key} -- no report written")
        hashes[key] = row["sha256"]

    with args.report.open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    print(f"Output parity: PASS ({len(hashes)} transformations)")
    print(f"Report: {args.report}")
    print("All test nginx processes stopped; temporary image copies deleted.")


if __name__ == "__main__":
    main()
