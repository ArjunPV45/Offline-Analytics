#!/usr/bin/env python3
"""Runs run_batch_analytics.py for every available day across one or more
channels, sequentially, day-first: day 1 for every channel that has it,
then day 2 for every channel that has it, and so on. No --date needed.

Day-first (not channel-first) is deliberate: with several channels each
running their full history back-to-back, channel-first would mean channel 3
doesn't even start until channels 1 and 2 have been fully processed --
day-first gets you a complete cross-channel picture for the earliest day
quickly, then the next day, etc.

Deliberately sequential (never more than one Hailo Detection App process at
a time) rather than concurrent — see run_batch_analytics_parallel.py's
README notes on 3-way concurrency instability; this avoids that risk
entirely by design, at the cost of not overlapping channels.

GStreamerApp.run() always ends the process via sys.exit() once a day's
segments are done, so multiple days can't be looped inside one process —
each day runs as its own subprocess instead, same approach as
run_batch_analytics_parallel.py uses for concurrent channels.

Usage:
    python3 run_batch_analytics_all_days.py --channel ch01
    python3 run_batch_analytics_all_days.py --channel ch01 --channel ch02 --channel ch03

Not every channel necessarily has the same set of days available (e.g. one
channel's recorder started later) — a channel missing a given day is simply
skipped for that day, not treated as an error.

Each day's normal report is written to <output-dir>/<channel>_<date>.json,
same as running run_batch_analytics.py directly. Days that already have a
report are skipped by default (safe to re-run after an interruption) —
pass --reprocess to redo them anyway.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from batch_analytics import config as cfg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--channel", action="append", required=True,
        help="Repeat for multiple channels, e.g. --channel ch01 --channel ch02. "
             "Channels (and their days) are processed one at a time, in order.",
    )
    parser.add_argument("--videos-root", default=cfg.VIDEOS_ROOT)
    parser.add_argument("--output-dir", default=cfg.BATCH_REPORTS_DIR)
    parser.add_argument(
        "--reprocess", action="store_true",
        help="Reprocess days that already have a report (default: skip them).",
    )
    parser.add_argument("--display", action="store_true", help="Passed through to each day's run.")
    parser.add_argument(
        "--analysis-fps", type=float, default=cfg.DEFAULT_ANALYSIS_FPS,
        help="Passed through to each day's run. Defaults to DEFAULT_ANALYSIS_FPS from .env.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Discover each channel's available days first, then build the union of
    # all dates across all channels (channels don't necessarily share the
    # same date range) so the outer loop can go day-first.
    channel_dates: dict[str, set[str]] = {}
    for channel in args.channel:
        channel_dir = Path(args.videos_root) / channel
        if not channel_dir.is_dir():
            print(f"WARNING: no such channel folder: {channel_dir} -- skipping", file=sys.stderr)
            continue
        dates = {p.name for p in channel_dir.iterdir() if p.is_dir()}
        if not dates:
            print(f"WARNING: no date folders under {channel_dir} -- skipping", file=sys.stderr)
            continue
        channel_dates[channel] = dates
        print(f"{channel}: {len(dates)} day(s) available ({min(dates)} .. {max(dates)})")

    all_dates = sorted({d for dates in channel_dates.values() for d in dates})
    print(f"\n=== {len(all_dates)} distinct day(s) across {len(channel_dates)} channel(s), processing day-first ===")

    overall_t0 = time.time()
    results: list[tuple[str, str, str, float | None, float | None]] = []  # channel, date, status, video_s, wall_s

    for date in all_dates:
        channels_for_day = [c for c in args.channel if date in channel_dates.get(c, ())]
        print(f"\n=== Day {date}: {len(channels_for_day)} channel(s) ({', '.join(channels_for_day)}) ===")

        for channel in channels_for_day:
            report_path = output_dir / f"{channel}_{date}.json"
            if report_path.exists() and not args.reprocess:
                print(f"[{channel}/{date}] already processed, skipping (--reprocess to redo)")
                results.append((channel, date, "skipped", None, None))
                continue

            cmd = [
                sys.executable, "-u", "run_batch_analytics.py",
                "--channel", channel, "--date", date,
                "--output-dir", str(output_dir),
                "--videos-root", args.videos_root,
            ]
            if args.display:
                cmd.append("--display")
            if args.analysis_fps is not None:
                cmd += ["--analysis-fps", str(args.analysis_fps)]

            print(f"\n--- [{channel}/{date}] starting ---")
            t0 = time.time()
            ret = subprocess.run(cmd).returncode
            elapsed = time.time() - t0

            if ret != 0:
                print(f"[{channel}/{date}] FAILED (exit code {ret}) after {elapsed:.1f}s", file=sys.stderr)
                results.append((channel, date, "failed", None, elapsed))
                continue

            if not report_path.exists():
                print(f"[{channel}/{date}] WARNING: exited cleanly but no report found", file=sys.stderr)
                results.append((channel, date, "no_report", None, elapsed))
                continue

            with open(report_path) as f:
                report = json.load(f)
            results.append((channel, date, "done", report.get("total_video_seconds"), elapsed))
            print(
                f"[{channel}/{date}] done in {elapsed:.1f}s wall "
                f"({report.get('realtime_factor') or 0:.2f}x realtime)"
            )

    overall_elapsed = time.time() - overall_t0
    done = [r for r in results if r[2] == "done"]
    total_video_s = sum(r[3] for r in done if r[3])
    total_wall_s = sum(r[4] for r in done if r[4])

    print("\n=== Summary ===")
    for channel, date, status, _video_s, _wall_s in results:
        print(f"  {channel}/{date}: {status}")
    print(f"\n{len(done)}/{len(results)} day(s) processed successfully.")
    if total_wall_s:
        print(
            f"Combined: {total_video_s / 3600:.2f} video-hours in {total_wall_s / 3600:.2f} "
            f"wall-hours -> {total_video_s / total_wall_s:.2f}x realtime"
        )
    print(f"Total script wall-clock time: {overall_elapsed / 60:.1f} minutes")

    return 0 if all(r[2] in ("done", "skipped") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
