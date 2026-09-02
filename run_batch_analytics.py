#!/usr/bin/env python3
"""CLI entry point: process one camera channel's saved footage for one day.

Usage:
    python3 run_batch_analytics.py --channel ch01 --date 2026-08-17
    python3 run_batch_analytics.py --channel ch03 --date 2026-08-17 \\
        --videos-root /home/hailopi/Analytics/Videos --output-dir batch_reports

--videos-root/--output-dir/--analysis-fps default to VIDEOS_ROOT/BATCH_REPORTS_DIR/
DEFAULT_ANALYSIS_FPS from .env (see batch_analytics/config.py) if not passed
explicitly. --arch/--hef-path likewise default to this device's configured
HAILO_ARCH/HEF_PATH -- pass either explicitly to override just this run.

Any additional flags accepted by get_pipeline_parser() (e.g. --show-fps) may
be appended and are passed straight through to the pipeline.
"""

import argparse
import sys
from pathlib import Path

from hailo_apps.python.core.common.core import get_pipeline_parser

from batch_analytics import config as cfg
from batch_analytics.batch_pipeline import (
    BatchAppCallback,
    BatchDetectionApp,
    app_callback,
)
from batch_analytics.video_catalog import discover_day
from batch_analytics.zone_config_io import default_zone_config_path, load_zone_config
from batch_analytics.zone_counter_offline import OfflineZoneLineCounter


def main() -> int:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--channel", required=True, help="Camera channel folder, e.g. ch01")
    pre_parser.add_argument("--date", required=True, help="Date folder, e.g. 2026-08-17")
    pre_parser.add_argument("--videos-root", default=cfg.VIDEOS_ROOT)
    pre_parser.add_argument("--output-dir", default=cfg.BATCH_REPORTS_DIR)
    pre_parser.add_argument(
        "--display",
        action="store_true",
        help=(
            "Show a live window with detection boxes + track IDs overlaid, for "
            "visually debugging tracking accuracy. Runs at the same speed the "
            "detection model actually processes at (not real-time playback)."
        ),
    )
    pre_parser.add_argument(
        "--analysis-fps",
        type=float,
        default=cfg.DEFAULT_ANALYSIS_FPS,
        help=(
            "Only analyze roughly this many frames per second, dropping the "
            "rest before scaling/inference/tracking/overlay to cut processing "
            "time proportionally. Default: process every frame (no skipping). "
            "E.g. --analysis-fps 5"
        ),
    )
    pre_parser.add_argument(
        "--zone-config",
        default=None,
        help=(
            "Path to a zone/line config JSON (see draw_zone.py). Default: "
            "batch_analytics/zone_configs/<channel>.json if it exists."
        ),
    )
    pre_parser.add_argument(
        "--no-zones",
        action="store_true",
        help="Ignore any zone config for this channel and run detection/tracking only.",
    )
    pre_args, remaining_argv = pre_parser.parse_known_args()

    day_dir = Path(pre_args.videos_root) / pre_args.channel / pre_args.date
    if not day_dir.is_dir():
        print(f"ERROR: no such folder: {day_dir}", file=sys.stderr)
        return 1

    segments = discover_day(day_dir, pre_args.channel)
    if not segments:
        print(f"ERROR: no recognizable .mp4 segments found in {day_dir}", file=sys.stderr)
        return 1

    first_segment, *pending_segments = segments

    zones, lines = [], []
    if not pre_args.no_zones:
        zone_config_path = (
            Path(pre_args.zone_config) if pre_args.zone_config
            else default_zone_config_path(pre_args.channel)
        )
        if zone_config_path.exists():
            zones, lines, config_width, config_height = load_zone_config(zone_config_path)
            print(
                f"Loaded {len(zones)} zone(s), {len(lines)} line(s) from {zone_config_path} "
                f"(drawn at {config_width}x{config_height})"
            )
            # Zone/line pixel coordinates only mean what they were drawn
            # against if this run uses the same resolution. Force it unless
            # the user already specified their own --width/--height, in which
            # case warn rather than silently producing wrong counts.
            user_set_resolution = any(
                flag in remaining_argv for flag in ("--width", "-W", "--height", "-H")
            )
            if user_set_resolution:
                print(
                    f"WARNING: --width/--height were also passed explicitly — make sure "
                    f"they match {config_width}x{config_height} or zone coordinates will "
                    f"be off.",
                    file=sys.stderr,
                )
            else:
                remaining_argv = remaining_argv + [
                    "--width", str(config_width), "--height", str(config_height),
                ]
        elif pre_args.zone_config:
            print(f"ERROR: zone config not found: {zone_config_path}", file=sys.stderr)
            return 1

    # Apply this device's configured --arch/--hef-path (see .env's
    # HAILO_ARCH/HEF_PATH) unless the caller already passed their own --
    # an explicit flag on the command line always wins over the device
    # default, same convention as --width/--height above.
    user_set_model = any(flag in remaining_argv for flag in ("--arch", "--hef-path"))
    if user_set_model:
        print("NOTE: --arch/--hef-path passed explicitly -- ignoring HAILO_ARCH/HEF_PATH from .env", file=sys.stderr)
    else:
        remaining_argv = remaining_argv + cfg.extra_pipeline_args()

    # Rewrite sys.argv so the framework's own parser (invoked inside
    # BatchDetectionApp/GStreamerApp) sees only flags it understands, plus the
    # first segment as --input and --disable-sync forced on (this is a batch
    # benchmark: we always want max throughput, never real-time playback pacing).
    sys.argv = [sys.argv[0], "--input", str(first_segment.path), "--disable-sync"] + remaining_argv

    pipeline_parser = get_pipeline_parser()

    counter = OfflineZoneLineCounter(zones=zones, lines=lines)
    user_data = BatchAppCallback(counter, segment_start_epoch=first_segment.start_dt.timestamp())

    app = BatchDetectionApp(
        app_callback,
        user_data,
        pipeline_parser,
        pending_segments=pending_segments,
        first_segment=first_segment,
        channel=pre_args.channel,
        date_str=pre_args.date,
        output_dir=Path(pre_args.output_dir),
        display=pre_args.display,
        analysis_fps=pre_args.analysis_fps,
    )
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
