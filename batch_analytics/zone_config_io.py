"""Load/save zone+line configs shared between draw_zone.py (the interactive
authoring tool) and run_batch_analytics.py (the consumer).

A config is scoped to one channel and one reference resolution — zone/line
pixel coordinates only mean what they were drawn against if every consumer
uses the same width/height. run_batch_analytics.py's default resolution
(1280x720, from GStreamerApp's defaults) must match what draw_zone.py used,
or override --width/--height identically on both.
"""

from __future__ import annotations

import json
from pathlib import Path

from hailo_apps.python.core.common.hailo_logger import get_logger

from batch_analytics.zone_counter_offline import LineConfig, ZoneConfig

logger = get_logger(__name__)

DEFAULT_ZONE_CONFIG_DIR = Path(__file__).parent / "zone_configs"


def default_zone_config_path(channel: str, config_dir: Path | None = None) -> Path:
    config_dir = config_dir or DEFAULT_ZONE_CONFIG_DIR
    return config_dir / f"{channel}.json"


def save_zone_config(
    path: Path,
    channel: str,
    width: int,
    height: int,
    zones: list[ZoneConfig],
    lines: list[LineConfig],
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "channel": channel,
        "frame_width": width,
        "frame_height": height,
        "zones": [
            {
                "name": z.name,
                "top_left": list(z.top_left) if z.top_left else None,
                "bottom_right": list(z.bottom_right) if z.bottom_right else None,
                "points": [list(p) for p in z.points] if z.points else None,
            }
            for z in zones
        ],
        "lines": [
            {"name": l.name, "start": list(l.start), "end": list(l.end), "swap": l.swap}
            for l in lines
        ],
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Saved zone config to %s (%d zone(s), %d line(s))", path, len(zones), len(lines))


def load_zone_config(path: Path) -> tuple[list[ZoneConfig], list[LineConfig], int, int]:
    """Returns (zones, lines, frame_width, frame_height)."""
    with open(path) as f:
        data = json.load(f)

    zones = [
        ZoneConfig(
            name=z["name"],
            top_left=tuple(z["top_left"]) if z.get("top_left") else None,
            bottom_right=tuple(z["bottom_right"]) if z.get("bottom_right") else None,
            points=[tuple(p) for p in z["points"]] if z.get("points") else None,
        )
        for z in data.get("zones", [])
    ]
    lines = [
        LineConfig(
            name=l["name"],
            start=tuple(l["start"]),
            end=tuple(l["end"]),
            swap=l.get("swap", False),
        )
        for l in data.get("lines", [])
    ]
    return zones, lines, data.get("frame_width", 1280), data.get("frame_height", 720)
