#!/usr/bin/env python3
"""Runs run_batch_analytics.py for multiple channels concurrently (same date),
to measure whether parallel processing improves aggregate day-completion
throughput on this 4-core Pi.

Each channel runs as its own independent OS process — its own GStreamer
pipeline, its own hailonet client sharing the Hailo8L device via HailoRT's
scheduler. This does NOT parallelize a single channel's segments (segments
within one channel/day still run sequentially, same as run_batch_analytics.py
alone) — it runs multiple different channels' days side by side.

Usage:
    python3 run_batch_analytics_parallel.py --date 2026-08-17 \\
        --channel ch01 --channel ch02 --channel ch03

Writes each channel's normal report to <output-dir>/<channel>_<date>.json
(same as running it alone), plus a combined summary at
<output-dir>/parallel_summary_<date>.json with the aggregate speedup.
"""

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_OUTPUT_DIR = "batch_reports"


def _stream_output(channel: str, pipe, log_file) -> None:
    """Prints each child's output live, prefixed with its channel, so multiple
    concurrent processes' progress stays legible in one terminal — and also
    writes it to that channel's own log file."""
    for raw_line in iter(pipe.readline, b""):
        text = raw_line.decode(errors="replace").rstrip("\n")
        print(f"[{channel}] {text}", flush=True)
        log_file.write(text + "\n")
        log_file.flush()
    pipe.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--channel",
        action="append",
        required=True,
        help="Repeat for each channel to run concurrently, e.g. --channel ch01 --channel ch02",
    )
    parser.add_argument("--date", required=True)
    parser.add_argument("--videos-root", default=None)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--display",
        action="store_true",
        help=(
            "Pass --display through to every child. Not recommended when "
            "measuring throughput — rendering multiple windows adds CPU load "
            "on top of the contention this is meant to measure."
        ),
    )
    parser.add_argument(
        "--analysis-fps",
        type=float,
        default=None,
        help=(
            "Pass --analysis-fps through to every child, so each concurrent "
            "channel skips frames down to roughly this rate. See "
            "run_batch_analytics.py --help for details."
        ),
    )
    args = parser.parse_args()

    if len(args.channel) > 2:
        print(
            f"WARNING: {len(args.channel)} channels requested concurrently. "
            "3-way concurrency has been observed to be unstable under sustained "
            "real load on this hardware — one client can starve the others past "
            "HailoRT's internal timeout, causing a non-recoverable crash (and "
            "sometimes a hung process) rather than just a slowdown. 2 concurrent "
            "channels is the tested-safe default; proceeding anyway.",
            file=sys.stderr,
        )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir = output_dir / "parallel_logs"
    log_dir.mkdir(exist_ok=True)

    procs = []
    log_files = []
    threads = []
    t0 = time.time()

    for channel in args.channel:
        cmd = [
            sys.executable, "-u", "run_batch_analytics.py",
            "--channel", channel,
            "--date", args.date,
            "--output-dir", str(output_dir),
        ]
        if args.videos_root:
            cmd += ["--videos-root", args.videos_root]
        if args.display:
            cmd.append("--display")
        if args.analysis_fps is not None:
            cmd += ["--analysis-fps", str(args.analysis_fps)]

        log_path = log_dir / f"{channel}_{args.date}.log"
        log_file = open(log_path, "w")
        print(f"Launching {channel}... (log: {log_path})")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        thread = threading.Thread(
            target=_stream_output, args=(channel, proc.stdout, log_file), daemon=True
        )
        thread.start()
        procs.append((channel, proc))
        log_files.append(log_file)
        threads.append(thread)

    results = {}
    for channel, proc in procs:
        ret = proc.wait()
        results[channel] = ret
        print(f"[{channel}] *** finished with exit code {ret} ***", flush=True)

    for t in threads:
        t.join(timeout=5)
    for f in log_files:
        f.close()

    wall_elapsed = time.time() - t0

    total_video_s = 0.0
    per_channel = {}
    for channel in args.channel:
        report_path = output_dir / f"{channel}_{args.date}.json"
        if not report_path.exists():
            print(
                f"WARNING: no report for {channel} (exit code {results.get(channel)}) "
                f"-- check {log_dir / f'{channel}_{args.date}.log'}",
                file=sys.stderr,
            )
            continue
        with open(report_path) as f:
            report = json.load(f)
        total_video_s += report["total_video_seconds"]
        per_channel[channel] = {
            "total_video_seconds": report["total_video_seconds"],
            "own_wall_seconds": report["total_wall_seconds"],
            "own_realtime_factor": report["realtime_factor"],
        }

    aggregate_realtime_factor = (total_video_s / wall_elapsed) if wall_elapsed > 0 else None

    summary = {
        "date": args.date,
        "channels": args.channel,
        "parallel_wall_seconds": wall_elapsed,
        "total_video_seconds_all_channels": total_video_s,
        "aggregate_realtime_factor": aggregate_realtime_factor,
        "per_channel": per_channel,
    }
    summary_path = output_dir / f"parallel_summary_{args.date}.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nAll channels finished in {wall_elapsed / 60:.1f} minutes wall-clock.")
    if aggregate_realtime_factor is not None:
        print(
            f"Combined: {total_video_s / 3600:.2f} video-hours across "
            f"{len(args.channel)} channel(s) in {wall_elapsed / 3600:.2f} wall-hours "
            f"-> aggregate {aggregate_realtime_factor:.2f}x realtime"
        )
    print(f"Summary written to {summary_path}")

    return 0 if all(r == 0 for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
