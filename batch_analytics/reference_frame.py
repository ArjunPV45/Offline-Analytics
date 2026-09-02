"""Grabs a representative frame from a channel's saved footage — shared by
draw_zone.py (interactive local tool) and platform_integration.py (MQTT
snapshot_request handler), so both use the same tested logic.

Raises ReferenceFrameError on any failure rather than exiting the process —
draw_zone.py (a CLI tool) converts that to a clean sys.exit(1);
platform_integration.py (a long-running service) catches it and just logs +
skips that one request instead of taking the whole service down.
"""

from __future__ import annotations

from pathlib import Path

import cv2

from hailo_apps.python.core.common.hailo_logger import get_logger

from batch_analytics.video_catalog import discover_day

logger = get_logger(__name__)


class ReferenceFrameError(Exception):
    pass


def find_reference_frame(
    channel: str, videos_root: str, date: str | None = None, segment_index: int = 0
):
    """Returns (frame_bgr, segment_filename, resolved_date)."""
    root = Path(videos_root) / channel
    if not root.is_dir():
        raise ReferenceFrameError(f"no such channel folder: {root}")

    if date is None:
        dates = sorted(p.name for p in root.iterdir() if p.is_dir())
        if not dates:
            raise ReferenceFrameError(f"no date folders under {root}")
        date = dates[-1]
        logger.info("No date given for channel %s, using most recent: %s", channel, date)

    day_dir = root / date
    segments = discover_day(day_dir, channel)
    if not segments:
        raise ReferenceFrameError(f"no segments found in {day_dir}")

    if segment_index >= len(segments):
        logger.warning(
            "segment_index %d out of range (%d segments) for %s/%s; using segment 0",
            segment_index, len(segments), channel, date,
        )
        segment_index = 0

    segment = segments[segment_index]
    logger.info("Using segment: %s", segment.path.name)

    cap = cv2.VideoCapture(str(segment.path))
    if not cap.isOpened():
        raise ReferenceFrameError(f"could not open {segment.path}")

    try:
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        # Jump partway into the clip rather than frame 0 -- more likely to show
        # people/motion than the very first frame, and avoids all-black startup
        # frames some encoders emit.
        target_frame = max(0, total_frames // 3)
        cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)

        ok, frame = cap.read()
        if not ok:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
    finally:
        cap.release()

    if not ok or frame is None:
        raise ReferenceFrameError(f"could not read a frame from {segment.path}")

    return frame, segment.path.name, date
