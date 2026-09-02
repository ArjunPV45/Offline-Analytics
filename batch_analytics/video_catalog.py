"""Discovers and orders saved CCTV segment files for a single camera/day.

Expected layout: <videos_root>/<channel>/<date>/<channel>_<YYYYMMDD>T<HHMMSS>_<HHMMSS>.mp4
e.g. Analytics/Videos/ch03/2026-08-17/ch03_20260817T194033_194120.mp4
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from hailo_apps.python.core.common.hailo_logger import get_logger

logger = get_logger(__name__)

_SEGMENT_RE = re.compile(
    r"^(?P<channel>.+)_(?P<date>\d{8})T(?P<start>\d{6})_(?P<end>\d{6})\.mp4$"
)


@dataclass
class VideoSegment:
    path: Path
    channel: str
    start_dt: datetime
    end_dt: datetime

    @property
    def nominal_duration_s(self) -> float:
        return (self.end_dt - self.start_dt).total_seconds()


def parse_segment_filename(path: Path) -> VideoSegment | None:
    match = _SEGMENT_RE.match(path.name)
    if not match:
        return None

    date_str = match.group("date")
    start_str = match.group("start")
    end_str = match.group("end")

    try:
        start_dt = datetime.strptime(date_str + start_str, "%Y%m%d%H%M%S")
        end_dt = datetime.strptime(date_str + end_str, "%Y%m%d%H%M%S")
    except ValueError:
        logger.warning("Could not parse timestamps from filename: %s", path.name)
        return None

    if end_dt < start_dt:
        # Segment crossed midnight.
        end_dt += timedelta(days=1)

    return VideoSegment(path=path, channel=match.group("channel"), start_dt=start_dt, end_dt=end_dt)


def discover_day(day_dir: Path, channel: str) -> list[VideoSegment]:
    """Returns every parseable .mp4 segment in day_dir, sorted chronologically.

    Files that don't match the expected naming pattern are skipped with a warning
    rather than aborting the whole day's run.
    """
    day_dir = Path(day_dir)
    segments: list[VideoSegment] = []

    for path in sorted(day_dir.glob("*.mp4")):
        segment = parse_segment_filename(path)
        if segment is None:
            logger.warning("Skipping file with unrecognized name: %s", path)
            continue
        if segment.channel != channel:
            logger.warning(
                "Skipping %s — filename channel '%s' does not match requested channel '%s'",
                path, segment.channel, channel,
            )
            continue
        segments.append(segment)

    segments.sort(key=lambda s: s.start_dt)
    return segments
