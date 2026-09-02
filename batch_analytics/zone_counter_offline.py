"""Single-camera zone/line visitor counter for offline batch analytics.

The zone-counting algorithm is ported from GPUvarient-main's
`AdvancedZoneCounter`/`LineCounter` (core/camera_processor.py) — a live,
GPU/RTSP retail-analytics system — adapted for offline processing of saved
files:
  - Network-free: no MQTT, no cloud API posting, no offline-buffer outbox.
    GPUvarient-main's equivalent posts every event to a live production API;
    unsuitable for reprocessing historical footage (same reasoning as the
    original port from ../zone_counter.py).
  - Scoped to one camera per instance (the batch runner processes one
    camera/day per run), so no per-camera dict nesting is needed, and no
    cross-camera Re-ID/identity-linking (that's a separate, larger feature
    GPUvarient-main also has — out of scope here).
  - Time-based (seconds) entry/exit confirmation instead of GPUvarient's
    raw frame counts (ZONE_ENTRY_CONFIRM_FRAMES=8, ZONE_EXIT_CONFIRM_FRAMES=45
    at their fixed ~15fps effective rate). Our source footage is variable
    frame rate, and --analysis-fps can skip frames, so a frame-count
    threshold would mean a different real-world dwell time depending on
    processing settings — seconds don't have that problem. Defaults
    (0.5s / 3.0s) match GPUvarient's frame counts at their assumed ~15fps.

What's kept from GPUvarient-main (the actual point of the port):
  - A proper 5-state machine (UNKNOWN/ENTERING/INSIDE/EXITING/OUTSIDE) with
    hysteresis — a person must stay confirmed-inside before entry counts,
    and confirmed-outside before exit counts (re-entering during EXITING
    cancels the pending exit rather than double-firing).
  - "Baseline occupancy": on the very first frame of the whole day's
    processing only, a track_id already inside a zone is recorded as inside
    WITHOUT firing a spurious entry event — there's no video history at that
    point to say when they actually arrived (could easily be before the
    footage even starts), so it's left uncounted rather than guessed at.
    Anywhere else in the day, a track observed already-inside with no
    matching open visit (see re-linking below) is a genuine new arrival and
    goes through the normal entry-confirm path instead — keeping every
    counted entry paired with a matching exit. Treating every such case as
    baseline (as GPUvarient-main does, having no equivalent of segmented
    per-file reprocessing) would silently drop the exit for anyone whose
    visit happens to span a segment boundary the re-linking below can't
    bridge, e.g. after a real gap in recording -- in_count would keep
    climbing over the day while out_count quietly lagged behind.
  - Spatial/temporal ID re-linking: when a brand-new track_id appears inside
    a zone, and no track_id is already known there, it's matched against any
    recently-seen "open visit" (a track_id that was INSIDE/EXITING and
    counted) within zone_id_link_max_sec and zone_spatial_match_px — the new
    id is treated as a continuation of that visit rather than a fresh entry.
    This absorbs hailotracker ID switches mid-visit (occlusion, brief
    misdetection, or a segment-boundary pipeline rebuild -- see
    batch_pipeline.py's known limitation on track IDs resetting there)
    without double-counting. The same matching now also applies to line
    crossings (see _find_lost_line_track), so a crossing that happens to
    straddle a segment boundary isn't silently missed just because the
    tracker handed out a new id for it.
  - Line crossing via proper segment-intersection (CCW test) instead of a
    side-of-line + displacement-threshold heuristic, plus a minimum
    real-movement threshold (line_min_crossing_px) so a bounding box merely
    jittering across the line while someone stands near it doesn't fire a
    crossing on its own.

Zones/lines are optional. With none configured, `update()` is a no-op and
`summary()` reports empty zones/lines.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import cv2
import numpy as np

from hailo_apps.python.core.common.hailo_logger import get_logger

logger = get_logger(__name__)

PersonObservation = tuple[int, float, float, float, float]  # (track_id, x1, y1, x2, y2)

_UNKNOWN = "unknown"
_ENTERING = "entering"
_INSIDE = "inside"
_EXITING = "exiting"
_OUTSIDE = "outside"


@dataclass
class ZoneConfig:
    name: str
    top_left: tuple[int, int] | None = None
    bottom_right: tuple[int, int] | None = None
    points: list[tuple[int, int]] | None = None
    zone_padding: int = 30  # unused by the current geometry test; see _is_inside


@dataclass
class LineConfig:
    name: str
    start: tuple[int, int]
    end: tuple[int, int]
    swap: bool = False


@dataclass
class _ZoneState:
    config: ZoneConfig
    in_count: int = 0
    out_count: int = 0
    # {track_id: {"state", "state_since", "counted", "last_seen", "foot"}}
    track_status: dict[int, dict[str, Any]] = field(default_factory=dict)
    # {new_track_id: original_track_id} — active while the original visit is still "counted"
    id_aliases: dict[int, int] = field(default_factory=dict)


@dataclass
class _LineState:
    config: LineConfig
    in_count: int = 0
    out_count: int = 0
    previous_positions: dict[int, tuple[float, float]] = field(default_factory=dict)
    last_seen: dict[int, float] = field(default_factory=dict)
    last_cross_time: dict[int, float] = field(default_factory=dict)
    # {new_track_id: original_track_id} — active while the original track's
    # position history is still being retained (see the stale-track sweep)
    id_aliases: dict[int, int] = field(default_factory=dict)


class OfflineZoneLineCounter:
    def __init__(
        self,
        zones: Iterable[ZoneConfig] = (),
        lines: Iterable[LineConfig] = (),
        zone_entry_confirm_sec: float = 0.5,
        zone_exit_confirm_sec: float = 3.0,
        zone_spatial_match_px: float = 80.0,
        zone_id_link_max_sec: float = 8.0,
        zone_stale_track_sec: float = 60.0,
        line_event_cooldown_sec: float = 5.0,
        line_min_crossing_px: float = 5.0,
    ):
        self.zone_entry_confirm_sec = zone_entry_confirm_sec
        self.zone_exit_confirm_sec = zone_exit_confirm_sec
        # Also used for line crossing's own id re-linking (_find_lost_line_track)
        # -- one spatial/temporal tolerance shared by both, since both are
        # answering the same question ("is this new id really the same
        # person as that recently-lost one?").
        self.zone_spatial_match_px = zone_spatial_match_px
        self.zone_id_link_max_sec = zone_id_link_max_sec
        self.zone_stale_track_sec = zone_stale_track_sec
        self.line_event_cooldown_sec = line_event_cooldown_sec
        # A crossing whose prev->current displacement is smaller than this
        # is treated as bounding-box jitter (someone standing near the line,
        # or minor detection noise), not a real step across it.
        self.line_min_crossing_px = line_min_crossing_px

        self._zones: dict[str, _ZoneState] = {z.name: _ZoneState(config=z) for z in zones}
        self._lines: dict[str, _LineState] = {l.name: _LineState(config=l) for l in lines}
        # Set on the very first update() call, then left False for the rest
        # of the day -- see the UNKNOWN+inside branch of _update_zone.
        self._is_first_update = True

    @property
    def has_zones_or_lines(self) -> bool:
        return bool(self._zones or self._lines)

    def overlay_shapes(self) -> list[dict[str, Any]]:
        """Zone/line geometry plus their live in/out counts, for drawing a
        debug overlay (see batch_pipeline.py's app_callback)."""
        shapes = []
        for state in self._zones.values():
            shapes.append({
                "kind": "zone",
                "name": state.config.name,
                "in_count": state.in_count,
                "out_count": state.out_count,
                "top_left": state.config.top_left,
                "bottom_right": state.config.bottom_right,
            })
        for state in self._lines.values():
            shapes.append({
                "kind": "line",
                "name": state.config.name,
                "in_count": state.in_count,
                "out_count": state.out_count,
                "start": state.config.start,
                "end": state.config.end,
            })
        return shapes

    def update(self, people: set[PersonObservation], now: float | None = None) -> None:
        """`now` should be the video-feed's own clock (the real time this
        frame's content shows -- see batch_pipeline.py's _video_feed_now()),
        not wall-clock processing time. All the dwell-time/cooldown
        thresholds above are measured against `now`, so if it doesn't track
        the footage's real timeline, they end up measuring processing speed
        instead of how long someone actually appeared in the video. Defaults
        to time.time() only for callers with no video timeline of their own
        (e.g. ad hoc/interactive use)."""
        if not self._zones and not self._lines:
            return
        now = now if now is not None else time.time()
        is_first_update = self._is_first_update
        self._is_first_update = False
        active_ids = {p[0] for p in people}
        for zone_state in self._zones.values():
            self._update_zone(zone_state, people, active_ids, now, is_first_update)
        for line_state in self._lines.values():
            self._update_line(line_state, people, now)

    def _bottom_center(self, person: PersonObservation) -> tuple[float, float]:
        _, x1, y1, x2, y2 = person
        return (x1 + x2) / 2, y2

    def _is_inside(self, point: tuple[float, float], zone: ZoneConfig) -> bool:
        x, y = point
        if zone.points:
            poly = np.array(zone.points, dtype=np.int32).reshape((-1, 1, 2))
            return cv2.pointPolygonTest(poly, (float(x), float(y)), False) >= 0
        if not zone.top_left or not zone.bottom_right:
            return False
        x1, y1 = zone.top_left
        x2, y2 = zone.bottom_right
        left, right = sorted((x1, x2))
        top, bottom = sorted((y1, y2))
        return left <= x <= right and top <= y <= bottom

    # ------------------------------------------------------------------
    # Zone state machine
    # ------------------------------------------------------------------

    def _find_lost_inside_match(
        self, state: _ZoneState, new_id: int, new_foot: tuple[float, float],
        current_ids: set[int], now: float, used_old_ids: set[int],
    ) -> int | None:
        fx, fy = new_foot
        best_id, best_dist = None, float("inf")

        for old_id, status in state.track_status.items():
            if old_id == new_id or old_id in current_ids or old_id in used_old_ids:
                continue
            if not status.get("counted"):
                continue
            if status.get("state") not in (_INSIDE, _EXITING):
                continue
            old_foot = status.get("foot")
            if old_foot is None:
                continue
            gap = now - status.get("last_seen", 0.0)
            if gap < 0 or gap > self.zone_id_link_max_sec:
                continue
            ox, oy = old_foot
            dist = ((fx - ox) ** 2 + (fy - oy) ** 2) ** 0.5
            if dist <= self.zone_spatial_match_px and dist < best_dist:
                best_id, best_dist = old_id, dist

        return best_id

    def _update_zone(self, state: _ZoneState, people, active_ids, now: float, is_first_update: bool) -> None:
        cfg = state.config
        current_feet = {p[0]: self._bottom_center(p) for p in people}
        in_zone_ids = {tid for tid, foot in current_feet.items() if self._is_inside(foot, cfg)}
        existing_ids_for_zone = set(state.track_status.keys())

        zone_current_feet = dict(current_feet)
        aliased_new_ids: set[int] = set()
        used_old_ids: set[int] = set()

        # Drop aliases whose original visit already resolved (exit fired, or
        # was never actually counted) — no longer anything to redirect to.
        for new_id, old_id in list(state.id_aliases.items()):
            old_status = state.track_status.get(old_id)
            if not old_status or not old_status.get("counted"):
                del state.id_aliases[new_id]
                continue
            if new_id not in current_feet:
                continue
            zone_current_feet[old_id] = current_feet[new_id]
            if new_id in in_zone_ids:
                in_zone_ids.add(old_id)
            else:
                in_zone_ids.discard(old_id)
            aliased_new_ids.add(new_id)
            used_old_ids.add(old_id)

        # New ids inside the zone with no established status: check whether
        # they're really a tracker ID-switch of someone already mid-visit.
        for new_id in list(in_zone_ids):
            if new_id in aliased_new_ids:
                continue
            status = state.track_status.get(new_id)
            if status and (status.get("counted") or status.get("state") not in (_UNKNOWN, _OUTSIDE)):
                continue  # already a known, distinct track -- nothing to relink

            old_id = self._find_lost_inside_match(
                state, new_id, current_feet[new_id], set(current_feet), now, used_old_ids
            )
            if old_id is None:
                continue

            zone_current_feet[old_id] = current_feet[new_id]
            in_zone_ids.add(old_id)
            in_zone_ids.discard(new_id)
            aliased_new_ids.add(new_id)
            used_old_ids.add(old_id)
            state.id_aliases[new_id] = old_id
            logger.debug(
                "Zone '%s': linked new track %s -> open visit %s", cfg.name, new_id, old_id
            )

        # State machine transitions for every track with a current position
        # or existing status (letting a track that vanished while inside age
        # through EXITING rather than freezing state forever).
        for track_id in (zone_current_feet.keys() | existing_ids_for_zone) - aliased_new_ids:
            status = state.track_status.setdefault(track_id, {
                "state": _UNKNOWN, "state_since": now, "counted": False,
                "last_seen": now, "foot": None,
            })
            currently_seen = track_id in zone_current_feet
            currently_inside = track_id in in_zone_ids

            if currently_seen:
                status["last_seen"] = now
                status["foot"] = zone_current_feet[track_id]

            if currently_inside:
                if status["state"] == _UNKNOWN:
                    if is_first_update:
                        # Very first frame of the whole day: no video history
                        # exists to say when they arrived (could be before
                        # the footage even starts) -- baseline occupancy,
                        # not a fresh entry.
                        status.update(state=_INSIDE, state_since=now, counted=False)
                    else:
                        # A "new" id appearing already inside mid-day (the
                        # re-linking above found no matching open visit) is a
                        # genuine unseen arrival, not a startup artifact --
                        # run it through the normal entry-confirm path so
                        # it's counted like any other entry, keeping this
                        # visit's eventual exit symmetric with it.
                        status.update(state=_ENTERING, state_since=now)
                elif status["state"] == _OUTSIDE:
                    status.update(state=_ENTERING, state_since=now)
                elif status["state"] == _ENTERING:
                    if now - status["state_since"] >= self.zone_entry_confirm_sec:
                        status.update(state=_INSIDE, counted=True)
                        state.in_count += 1
                elif status["state"] == _EXITING:
                    status["state"] = _INSIDE  # re-entered before exit confirmed
                # _INSIDE: nothing to do, no exit timer running
            else:
                if status["state"] == _UNKNOWN:
                    status.update(state=_OUTSIDE, state_since=now)
                elif status["state"] == _ENTERING:
                    status.update(state=_OUTSIDE, state_since=now)  # left before entry confirmed
                elif status["state"] == _INSIDE:
                    if status["counted"]:
                        status.update(state=_EXITING, state_since=now)
                    else:
                        status.update(state=_OUTSIDE, state_since=now)
                elif status["state"] == _EXITING:
                    if now - status["state_since"] >= self.zone_exit_confirm_sec:
                        status["state"] = _OUTSIDE
                        if status["counted"]:
                            state.out_count += 1
                        status["counted"] = False
                        for alias_id, orig_id in list(state.id_aliases.items()):
                            if orig_id == track_id:
                                del state.id_aliases[alias_id]
                # _OUTSIDE: stays outside

        # Garbage-collect uncounted tracks that have been gone a long time.
        for track_id in list(state.track_status.keys()):
            status = state.track_status[track_id]
            if not status.get("counted") and (now - status["last_seen"]) > self.zone_stale_track_sec:
                del state.track_status[track_id]

    # ------------------------------------------------------------------
    # Line crossing (segment intersection)
    # ------------------------------------------------------------------

    @staticmethod
    def _ccw(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> bool:
        return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])

    @classmethod
    def _segments_intersect(cls, a, b, c, d) -> bool:
        return cls._ccw(a, c, d) != cls._ccw(b, c, d) and cls._ccw(a, b, c) != cls._ccw(a, b, d)

    def _find_lost_line_track(
        self, state: _LineState, new_id: int, new_point: tuple[float, float],
        current_ids: set[int], now: float, used_old_ids: set[int],
    ) -> int | None:
        """Same idea as _find_lost_inside_match, for lines: is this brand-new
        id really a tracker ID-switch of someone mid-crossing (most often a
        segment-boundary pipeline rebuild), close to where a recently-seen
        id was last spotted? If so, its position history carries over so the
        crossing test below still sees a real prev->current movement instead
        of starting blind."""
        best_id, best_dist = None, float("inf")
        for old_id, old_point in state.previous_positions.items():
            if old_id == new_id or old_id in current_ids or old_id in used_old_ids:
                continue
            gap = now - state.last_seen.get(old_id, -float("inf"))
            if gap < 0 or gap > self.zone_id_link_max_sec:
                continue
            dist = ((new_point[0] - old_point[0]) ** 2 + (new_point[1] - old_point[1]) ** 2) ** 0.5
            if dist <= self.zone_spatial_match_px and dist < best_dist:
                best_id, best_dist = old_id, dist
        return best_id

    def _update_line(self, state: _LineState, people, now: float) -> None:
        cfg = state.config
        p1, p2 = cfg.start, cfg.end
        current_ids = {p[0] for p in people}
        used_old_ids: set[int] = set()

        # Drop aliases whose original track no longer has any position
        # history to redirect to (already garbage-collected below).
        for new_id, old_id in list(state.id_aliases.items()):
            if old_id not in state.previous_positions:
                del state.id_aliases[new_id]

        for person in people:
            raw_id = person[0]
            current_point = self._bottom_center(person)

            track_id = state.id_aliases.get(raw_id)
            if track_id is None and raw_id not in state.previous_positions:
                old_id = self._find_lost_line_track(state, raw_id, current_point, current_ids, now, used_old_ids)
                if old_id is not None:
                    state.id_aliases[raw_id] = old_id
                    used_old_ids.add(old_id)
                    track_id = old_id
                    logger.debug("Line '%s': linked new track %s -> lost track %s", cfg.name, raw_id, old_id)
            if track_id is None:
                track_id = raw_id

            state.last_seen[track_id] = now
            prev_point = state.previous_positions.get(track_id)
            state.previous_positions[track_id] = current_point
            if prev_point is None:
                continue

            if now - state.last_cross_time.get(track_id, -float("inf")) < self.line_event_cooldown_sec:
                continue

            displacement = ((current_point[0] - prev_point[0]) ** 2 + (current_point[1] - prev_point[1]) ** 2) ** 0.5
            if displacement < self.line_min_crossing_px:
                continue  # bounding-box jitter, not real movement across the line

            if not self._segments_intersect(prev_point, current_point, p1, p2):
                continue

            val = (p2[0] - p1[0]) * (current_point[1] - p1[1]) - (p2[1] - p1[1]) * (current_point[0] - p1[0])
            crossed_in = val > 0
            if cfg.swap:
                crossed_in = not crossed_in

            state.last_cross_time[track_id] = now
            if crossed_in:
                state.in_count += 1
            else:
                state.out_count += 1

        stale_cutoff = now - self.zone_stale_track_sec
        for track_id in [tid for tid, ts in state.last_seen.items() if ts < stale_cutoff]:
            state.last_seen.pop(track_id, None)
            state.previous_positions.pop(track_id, None)
            state.last_cross_time.pop(track_id, None)
        for new_id, old_id in list(state.id_aliases.items()):
            if old_id not in state.previous_positions:
                del state.id_aliases[new_id]

    def summary(self) -> dict[str, Any]:
        return {
            "zones": {
                name: {"in_count": s.in_count, "out_count": s.out_count}
                for name, s in self._zones.items()
            },
            "lines": {
                name: {"in_count": s.in_count, "out_count": s.out_count}
                for name, s in self._lines.items()
            },
        }
