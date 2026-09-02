"""Normalizes zone/line coordinates from the frontend's zone_config/
line_config MQTT payloads into our ZoneConfig/LineConfig dataclasses.

Supports the same handful of key conventions the live system's equivalent
already has to handle (GPUvarient-main/core/zone_manager.py's
_normalize_zone/_normalize_line) — ported here rather than reinvented, in
case the same frontend zone-drawing component ends up talking to both
systems and sends the same payload shapes to each.
"""

from __future__ import annotations

from typing import Any

from hailo_apps.python.core.common.hailo_logger import get_logger

from batch_analytics.zone_counter_offline import LineConfig, ZoneConfig

logger = get_logger(__name__)


def _extract_coords(raw: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """Accepts x1/y1/x2/y2, startX/startY/endX/endY, start/end (as [x,y] or
    {"x":..,"y":..}), or a 4+ element points list. Returns None if none of
    those shapes match."""
    if "x1" in raw:
        return float(raw["x1"]), float(raw["y1"]), float(raw["x2"]), float(raw["y2"])
    if "startX" in raw:
        return float(raw["startX"]), float(raw["startY"]), float(raw["endX"]), float(raw["endY"])
    if "start" in raw and "end" in raw:
        s, e = raw["start"], raw["end"]
        if isinstance(s, (list, tuple)):
            return float(s[0]), float(s[1]), float(e[0]), float(e[1])
        return float(s["x"]), float(s["y"]), float(e["x"]), float(e["y"])
    if "points" in raw and len(raw["points"]) >= 4:
        pts = raw["points"]
        return float(pts[0]), float(pts[1]), float(pts[2]), float(pts[3])
    return None


def normalize_zone(raw: dict[str, Any], index: int, channel: str) -> ZoneConfig | None:
    try:
        coords = _extract_coords(raw)
    except (KeyError, TypeError, ValueError, IndexError) as e:
        logger.error("channel '%s' zone %d normalization error: %s | raw=%s", channel, index, e, raw)
        return None
    if coords is None:
        logger.warning(
            "channel '%s' zone %d has unrecognized format: %s -- skipping",
            channel, index, list(raw.keys()),
        )
        return None

    x1, y1, x2, y2 = coords
    name = raw.get("name") or f"zone{index + 1}"
    return ZoneConfig(
        name=name,
        top_left=(min(x1, x2), min(y1, y2)),
        bottom_right=(max(x1, x2), max(y1, y2)),
    )


def normalize_line(raw: dict[str, Any], index: int, channel: str) -> LineConfig | None:
    try:
        coords = _extract_coords(raw)
    except (KeyError, TypeError, ValueError, IndexError) as e:
        logger.error("channel '%s' line %d normalization error: %s | raw=%s", channel, index, e, raw)
        return None
    if coords is None:
        logger.warning(
            "channel '%s' line %d has unrecognized format: %s -- skipping",
            channel, index, list(raw.keys()),
        )
        return None

    x1, y1, x2, y2 = coords
    name = raw.get("name") or f"line{index + 1}"
    return LineConfig(name=name, start=(x1, y1), end=(x2, y2), swap=bool(raw.get("swap", False)))
