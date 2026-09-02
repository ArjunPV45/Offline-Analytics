"""Draws zone rectangles / crossing lines onto a static frame (pure numpy/cv2
array manipulation — no window, no GUI). Used for the snapshot response sent
to the platform, so whoever's looking at it can see what's already
configured for that channel rather than a blank frame.

Visual style matches ../../GPUvarient-main/core/zone_manager.py's
render_zones (yellow zones, cyan lines) for consistency with the live
system's snapshots, in case the same person looks at both.
"""

from __future__ import annotations

import cv2

from batch_analytics.zone_counter_offline import LineConfig, ZoneConfig

ZONE_COLOR_BGR = (0, 255, 255)  # yellow
LINE_COLOR_BGR = (255, 255, 0)  # cyan


def draw_zones_on_frame(frame_bgr, zones: list[ZoneConfig], lines: list[LineConfig]):
    """Returns a new annotated frame; does not modify frame_bgr in place.

    Only rectangle zones (top_left/bottom_right) are drawn — draw_zone.py
    doesn't currently create polygon (points) zones, so there's nothing to
    render for that case yet.
    """
    annotated = frame_bgr.copy()

    for zone in zones:
        if not (zone.top_left and zone.bottom_right):
            continue
        x1, y1 = map(int, zone.top_left)
        x2, y2 = map(int, zone.bottom_right)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), ZONE_COLOR_BGR, 3)

        cv2.rectangle(
            annotated, (x1, y1 - 24), (x1 + 10 * len(zone.name) + 10, y1), ZONE_COLOR_BGR, -1,
        )
        cv2.putText(
            annotated, zone.name, (x1 + 5, y1 - 6),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2,
        )

    for line in lines:
        x1, y1 = map(int, line.start)
        x2, y2 = map(int, line.end)
        cv2.line(annotated, (x1, y1), (x2, y2), LINE_COLOR_BGR, 3)
        cv2.circle(annotated, (x1, y1), 5, (0, 0, 255), -1)
        cv2.putText(
            annotated, line.name, (x1, y1 - 10),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, LINE_COLOR_BGR, 2,
        )

    return annotated
