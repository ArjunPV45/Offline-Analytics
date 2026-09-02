"""Discovers what's actually available to process: channels and their day
folders under the videos root. Deliberately shallow — the frontend platform
needs "what channels, what days" to let someone pick one, not a listing of
every individual segment file (see run_batch_analytics.py/video_catalog.py
for that level of detail, once a specific channel+day is chosen)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# The videos root is a shared NFS mount -- other unrelated things can end up
# there (observed: a cloned robotics project sitting alongside the real
# channel folders, its .git/checkpoints/etc. subdirs would otherwise get
# reported as "days"). Only recognize directories that actually look like
# our channel/date convention.
_CHANNEL_NAME_RE = re.compile(r"^ch\d+$")
_DATE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def discover_available_channels(
    videos_root: str | Path,
    camera_id_map: dict[str, int] | None = None,
    reports_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Returns one entry per channel folder that has at least one
    not-yet-processed day of footage, sorted by channel name. `camera_id`
    is the numeric ID a frontend/backend expects (e.g. matching the old
    cameras.json convention) if a mapping is supplied for that channel, else
    None -- callers should handle that gracefully rather than treating it as
    an error, since not every channel necessarily has a numeric ID assigned
    yet.

    If `reports_dir` is given, any day that already has a report at
    `<reports_dir>/<channel>_<date>.json` is excluded — the platform only
    needs to see what's still waiting to be processed. This is a cheap
    existence check (does a report file exist), not a strict correctness
    check — see day_completion.py's day_is_safe_to_delete() for the much
    stricter check used before ever deleting anything.
    """
    videos_root = Path(videos_root)
    camera_id_map = camera_id_map or {}
    reports_dir = Path(reports_dir) if reports_dir else None

    if not videos_root.is_dir():
        return []

    channels = []
    for channel_dir in sorted(p for p in videos_root.iterdir() if p.is_dir()):
        if not _CHANNEL_NAME_RE.match(channel_dir.name):
            continue
        days = sorted(p.name for p in channel_dir.iterdir() if p.is_dir() and _DATE_NAME_RE.match(p.name))
        if reports_dir is not None:
            days = [d for d in days if not (reports_dir / f"{channel_dir.name}_{d}.json").exists()]
        if not days:
            continue
        channels.append({
            "channel": channel_dir.name,
            "camera_id": camera_id_map.get(channel_dir.name),
            "days": days,
            "day_count": len(days),
        })
    return channels
