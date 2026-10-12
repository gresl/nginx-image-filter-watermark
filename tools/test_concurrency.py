#!/usr/bin/env python3
"""Bounded multiworker concurrency and VmHWM test of two isolated nginx binaries.

Only local HTTP requests; real inputs are copied to an ephemeral directory.
Never invokes installed nginx, never reloads services, never writes to uploads.
"""
import argparse
import csv
import grp
import hashlib
import http.client
import math
import os
import random
import statistics
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from benchmark_local import (
    check_port, copy_image, nginx_config, start_nginx, stop_nginx,
)

CASES = (
    ("JPEG resize", "/plain/photo.jpg"),
    ("JPEG resize + WM", "/wm/photo.jpg"),
    ("JPEG WM only", "/stamp/photo.jpg"),
)
ORDER = ("first", "second", "second", "first")
MAX_RESPONSE = 16 * 1024 * 1024


def request(port, label, route):
    start = time.perf_counter_ns()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", route, headers={"Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"{label} {route}: HTTP {response.status}")
        media = response.getheader("Content-Type", "").split(";")[0]
        if media != "image/jpeg":
            raise RuntimeError(f"{label} {route}: content-type {media}")
        body = response.read(MAX_RESPONSE + 1)
        if not body or len(body) > MAX_RESPONSE:
            raise RuntimeError(f"{label} {route}: response size invalid")
    finally:
        connection.close()
    elapsed = (time.perf_counter_ns() - start) / 1_000_000
    return label, hashlib.sha256(body).hexdigest(), len(body), elapsed


def worker_memory(master_pid, expected_workers):
    path = Path(f"/proc/{master_pid}/task/{master_pid}/children")
    children = path.read_text().split()
    if len(children) != expected_workers:
        raise RuntimeError(
            f"Expected {expected_workers} nginx workers, found {len(children)}"
        )
    peaks = []
    for child in children:
        stats = Path(f"/proc/{child}/status").read_text().splitlines()
        value = next(
            (int(line.split()[1]) for line in stats
             if line.startswith("VmHWM:")), None
        )
        if value is None:
            raise RuntimeError(f"VmHWM unavailable for worker {child}")
        peaks.append(value)
    return max(peaks), sum(peaks)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--first-binary", type=Path, required=True)
    p.add_argument("--second-binary", type=Path, required=True)
    p.add_argument("--source-jpeg", type=Path, required=True)
    p.add_argument("--watermark-png", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--port", type=int, default=18766)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--requests-per-case", type=int, default=40)
    args = p.parse_args()

    if os.geteuid() != 0:
        p.error("run as root for isolated nginx master and worker privileges")
    if not 1 <= args.workers <= 4:
        p.error("worker count must be 1..4")
    if not 1 <= args.concurrency <= 8:
        p.error("concurrency must be 1..8")
    if not 20 <= args.requests_per_case <= 100:
        p.error("requests per case must be 20..100")
    if not 1024 <= args.port <= 65535:
        p.error("port must be between 1024 and 65535")
    if args.report.exists():
        p.error(f"report already exists: {args.report}")
    for binary in (args.first_binary, args.second_binary):
        if not binary.is_file() or not os.access(binary, os.X_OK):
            p.error(f"missing or nonexecutable binary: {binary}")
    for source in (args.source_jpeg, args.watermark_png):
        if not source.is_file() or source.stat().st_size > MAX_RESPONSE:
            p.error(f"missing source or file > 16 MiB: {source}")
    nginx_group = grp.getgrnam("nginx")
    check_port(args.port)

    binaries = {"first": args.first_binary, "second": args.second_binary}
    reference = {}
    rows = []
    process = None

    with tempfile.TemporaryDirectory(prefix="wmconcurrency-", dir="/var/tmp") as td:
        fixtures = Path(td)
        os.chown(fixtures, 0, nginx_group.gr_gid)
        os.chmod(fixtures, 0o750)
        copy_image(args.source_jpeg, fixtures / "photo.jpg")
        copy_image(args.watermark_png, fixtures / "watermark.png")
        try:
            for run, variant in enumerate(ORDER, 1):
                check_port(args.port)
                prefix = fixtures / f"run-{run}-{variant}"
                prefix.mkdir(mode=0o755)
                (prefix / "logs").mkdir(mode=0o755)
                binary = binaries[variant]
                source_root = binary.parent.parent
                cfg = prefix / "nginx.conf"
                nginx_text = nginx_config(prefix, source_root, args.port)
                nginx_text = nginx_text.replace(
                    "worker_processes 1;",
                    f"worker_processes {args.workers};",
                )
                cfg.write_text(nginx_text)
                process = start_nginx(binary, prefix, cfg, args.port)

                for label, route in CASES:
                    request(args.port, label, route)

                work = [
                    (label, route)
                    for label, route in CASES
                    for _ in range(args.requests_per_case)
                ]
                random.Random(1473 + run).shuffle(work)

                observations = {label: [] for label, _ in CASES}
                responses = {}
                with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                    futures = [
                        pool.submit(request, args.port, label, route)
                        for label, route in work
                    ]
                    for future in as_completed(futures):
                        label, digest, size, elapsed = future.result()
                        if label in responses and responses[label] != (digest, size):
                            raise RuntimeError(
                                f"NONDETERMINISTIC OUTPUT: {variant} {label}"
                            )
                        responses[label] = (digest, size)
                        if label in reference and reference[label] != (digest, size):
                            raise RuntimeError(
                                f"OUTPUT DIFFERENCE: {variant} {label}"
                            )
                        reference.setdefault(label, (digest, size))
                        observations[label].append(elapsed)

                peak_max, peak_sum = worker_memory(process.pid, args.workers)
                for label, _ in CASES:
                    measures = sorted(observations[label])
                    if len(measures) != args.requests_per_case:
                        raise RuntimeError(f"Wrong sample count for {label}")
                    rows.append(dict(
                        run=run, variant=variant, case=label,
                        n=len(measures),
                        median_ms=round(statistics.median(measures), 3),
                        p95_ms=round(measures[math.ceil(.95 * len(measures)) - 1], 3),
                        mean_ms=round(statistics.mean(measures), 3),
                        response_bytes=reference[label][1],
                        sha256=reference[label][0], workers=args.workers,
                        concurrency=args.concurrency,
                        worker_peak_max_kib=peak_max,
                        worker_peak_sum_kib=peak_sum,
                    ))

                print(
                    f"{run} {variant:6} PASS "
                    f"({len(work)} concurrent requests; "
                    f"{args.workers} workers; peak worker max={peak_max/1024:.2f} MiB, "
                    f"sum of per-worker peaks={peak_sum/1024:.2f} MiB)",
                    flush=True,
                )
                stop_nginx(process)
                process = None
                time.sleep(.2)
        finally:
            stop_nginx(process)

    with args.report.open("x", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(
        f"Output parity: PASS ({len(reference)} JPEG scenarios; "
        f"{len(ORDER) * len(CASES) * args.requests_per_case} "
        "HTTP requests under bounded concurrency)"
    )
    print(f"Report: {args.report}")
    print("All isolated nginx processes stopped; temporary image copies removed.")


if __name__ == "__main__":
    main()
