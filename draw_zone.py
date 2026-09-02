#!/usr/bin/env python3
"""Interactive tool: draw zone rectangles and crossing lines on a reference
frame from a channel's saved footage, then save them for run_batch_analytics.py.

Uses Tkinter for the window/mouse handling, not OpenCV's own GUI — cv2's
bundled Qt backend proved unreliable on this Pi's desktop (missing plugin
categories, then a Qt-runtime version mismatch once pointed at the system
Qt5 plugins instead; see .hailo/memory/common_pitfalls.md). OpenCV is only
used here for video decoding and image resizing, never for display.

Must be run somewhere with a real display attached (this Pi's own desktop
session, or VNC/X-forwarding to it).

Usage:
    python3 draw_zone.py --channel ch01
    python3 draw_zone.py --channel ch01 --date 2026-08-17 --segment-index 3

Controls:
    z            switch to zone mode (default) — click and drag a rectangle
    l            switch to line mode — click the start point, then the end point
    u            undo the last zone/line
    r            reset everything
    s            save and quit
    q            quit without saving
"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path

import cv2

from batch_analytics.reference_frame import ReferenceFrameError, find_reference_frame
from batch_analytics.zone_config_io import default_zone_config_path, load_zone_config, save_zone_config
from batch_analytics.zone_counter_offline import LineConfig, ZoneConfig

DEFAULT_VIDEOS_ROOT = "/home/hailopi/Analytics/Videos"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720

ZONE_COLOR = "#ff3333"
LINE_COLOR = "#33ff33"
IN_PROGRESS_COLOR = "#ffff33"


def frame_to_photoimage(frame_bgr) -> tk.PhotoImage:
    """Converts a BGR numpy frame to a Tkinter PhotoImage via a raw in-memory
    PPM blob — avoids needing Pillow's ImageTk (not installed here) and,
    more importantly, avoids OpenCV's own display code entirely."""
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    height, width = rgb.shape[:2]
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    return tk.PhotoImage(data=header + rgb.tobytes())


class ZoneDrawerApp:
    def __init__(
        self,
        root: tk.Tk,
        frame_bgr,
        window_title: str,
        initial_zones: list[ZoneConfig] = (),
        initial_lines: list[LineConfig] = (),
    ):
        self.root = root
        self.height, self.width = frame_bgr.shape[:2]
        self.mode = "zone"
        self.zones: list[ZoneConfig] = list(initial_zones)
        self.lines: list[LineConfig] = list(initial_lines)
        self.saved = False

        self._drag_start_point = None
        self._drag_rect_id = None
        self._line_first_point = None
        self._line_first_marker_id = None

        root.title(window_title)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Buttons, not just keyboard shortcuts: some window managers don't
        # hand a newly-opened Tk window keyboard focus automatically, so 's'/
        # 'q'/etc. can silently go nowhere while mouse clicks (positional,
        # focus-independent) keep working fine. Buttons make save/quit/undo
        # reliable regardless of the WM's focus-follows-mouse behavior.
        toolbar = tk.Frame(root)
        toolbar.pack(fill="x")
        self.mode_button = tk.Button(toolbar, command=self._toggle_mode)
        self.mode_button.pack(side="left", padx=2, pady=2)
        tk.Button(toolbar, text="Undo (u)", command=self._undo).pack(side="left", padx=2, pady=2)
        tk.Button(toolbar, text="Reset (r)", command=self._reset).pack(side="left", padx=2, pady=2)
        tk.Button(toolbar, text="Save && Quit (s)", command=self._save_and_quit).pack(side="left", padx=2, pady=2)
        tk.Button(toolbar, text="Quit without saving (q)", command=self._on_close).pack(side="left", padx=2, pady=2)

        self.banner = tk.Label(root, bg="black", fg="white", anchor="w", font=("TkFixedFont", 10))
        self.banner.pack(fill="x")

        # Keep a reference — Tkinter drops the image if nothing else does.
        self._photo = frame_to_photoimage(frame_bgr)
        self.canvas = tk.Canvas(root, width=self.width, height=self.height, highlightthickness=0)
        self.canvas.pack()
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        root.bind_all("<Key>", self._on_key)

        # Best-effort attempt at keyboard focus too, so the shortcuts work
        # when the WM does cooperate — buttons are the guaranteed fallback.
        root.after(150, root.focus_force)

        self._redraw_all()  # draws any pre-loaded zones/lines and updates the banner

    def _update_banner(self) -> None:
        self.mode_button.config(text=f"Mode: {self.mode} (z/l)")
        self.banner.config(
            text=(
                f"mode={self.mode} (z=zone, l=line)  u=undo  r=reset  s=save+quit  q=quit  "
                f"| zones={len(self.zones)} lines={len(self.lines)}"
            )
        )

    def _toggle_mode(self) -> None:
        self.mode = "line" if self.mode == "zone" else "zone"
        self._cancel_in_progress()
        self._update_banner()

    def _save_and_quit(self) -> None:
        self.saved = True
        self.root.quit()

    def _on_press(self, event) -> None:
        x, y = event.x, event.y
        if self.mode == "zone":
            self._drag_start_point = (x, y)
            self._drag_rect_id = self.canvas.create_rectangle(
                x, y, x, y, outline=IN_PROGRESS_COLOR, width=2
            )
        elif self.mode == "line":
            if self._line_first_point is None:
                self._line_first_point = (x, y)
                self._line_first_marker_id = self.canvas.create_oval(
                    x - 4, y - 4, x + 4, y + 4, fill=IN_PROGRESS_COLOR
                )
            else:
                self._finish_line((x, y))

    def _on_drag(self, event) -> None:
        if self.mode != "zone" or self._drag_start_point is None:
            return
        x0, y0 = self._drag_start_point
        self.canvas.coords(self._drag_rect_id, x0, y0, event.x, event.y)

    def _on_release(self, event) -> None:
        if self.mode != "zone" or self._drag_start_point is None:
            return
        x0, y0 = self._drag_start_point
        x1, y1 = event.x, event.y
        self.canvas.delete(self._drag_rect_id)
        self._drag_rect_id = None
        self._drag_start_point = None

        top_left = (min(x0, x1), min(y0, y1))
        bottom_right = (max(x0, x1), max(y0, y1))
        if bottom_right[0] - top_left[0] < 5 or bottom_right[1] - top_left[1] < 5:
            print("Zone too small, ignored.")
            return

        name = input(f"Name for this zone (Enter for zone{len(self.zones) + 1}): ").strip()
        if not name:
            name = f"zone{len(self.zones) + 1}"
        zone = ZoneConfig(name=name, top_left=top_left, bottom_right=bottom_right)
        self.zones.append(zone)
        self._draw_zone(zone)
        print(f"Added zone '{name}': {top_left} -> {bottom_right}")
        self._update_banner()

    def _finish_line(self, end_point) -> None:
        start = self._line_first_point
        self.canvas.delete(self._line_first_marker_id)
        self._line_first_point = None
        self._line_first_marker_id = None

        name = input(f"Name for this line (Enter for line{len(self.lines) + 1}): ").strip()
        if not name:
            name = f"line{len(self.lines) + 1}"
        swap_answer = input(
            "Swap in/out direction? Crossing left-to-right (or top-to-bottom) "
            "counts as 'in' by default. [y/N]: "
        ).strip().lower()
        swap = swap_answer.startswith("y")
        line = LineConfig(name=name, start=start, end=end_point, swap=swap)
        self.lines.append(line)
        self._draw_line(line)
        print(f"Added line '{name}': {start} -> {end_point} (swap={swap})")
        self._update_banner()

    def _draw_zone(self, zone: ZoneConfig) -> None:
        x0, y0 = zone.top_left
        x1, y1 = zone.bottom_right
        self.canvas.create_rectangle(x0, y0, x1, y1, outline=ZONE_COLOR, width=2, tags="shape")
        self.canvas.create_text(
            x0 + 4, max(0, y0 - 10), anchor="w", fill=ZONE_COLOR, text=zone.name, tags="shape"
        )

    def _draw_line(self, line: LineConfig) -> None:
        x0, y0 = line.start
        x1, y1 = line.end
        self.canvas.create_line(x0, y0, x1, y1, fill=LINE_COLOR, width=2, tags="shape")
        self.canvas.create_text(
            x0 + 4, max(0, y0 - 10), anchor="w", fill=LINE_COLOR, text=line.name, tags="shape"
        )

    def _redraw_all(self) -> None:
        self.canvas.delete("shape")
        for zone in self.zones:
            self._draw_zone(zone)
        for line in self.lines:
            self._draw_line(line)
        self._update_banner()

    def _cancel_in_progress(self) -> None:
        if self._line_first_marker_id is not None:
            self.canvas.delete(self._line_first_marker_id)
            self._line_first_marker_id = None
            self._line_first_point = None
        if self._drag_start_point is not None:
            self.canvas.delete(self._drag_rect_id)
            self._drag_rect_id = None
            self._drag_start_point = None

    def _undo(self) -> None:
        if self.mode == "zone" and self.zones:
            removed = self.zones.pop()
            print(f"Removed zone '{removed.name}'")
        elif self.mode == "line" and self.lines:
            removed = self.lines.pop()
            print(f"Removed line '{removed.name}'")
        else:
            print("Nothing to undo in this mode.")
        self._redraw_all()

    def _reset(self) -> None:
        self.zones.clear()
        self.lines.clear()
        self._cancel_in_progress()
        self._redraw_all()
        print("Cleared all zones and lines.")

    def _on_key(self, event) -> None:
        key = (event.char or "").lower()
        if key == "z":
            self.mode = "zone"
            self._cancel_in_progress()
        elif key == "l":
            self.mode = "line"
            self._cancel_in_progress()
        elif key == "u":
            self._undo()
        elif key == "r":
            self._reset()
        elif key == "s":
            self.saved = True
            self.root.quit()
            return
        elif key == "q":
            self.saved = False
            self.root.quit()
            return
        self._update_banner()

    def _on_close(self) -> None:
        self.saved = False
        self.root.quit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--channel", required=True, help="Camera channel, e.g. ch01")
    parser.add_argument("--date", default=None, help="Date folder; defaults to the most recent available")
    parser.add_argument("--segment-index", type=int, default=0, help="Which segment of that day to grab a frame from")
    parser.add_argument("--videos-root", default=DEFAULT_VIDEOS_ROOT)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="Must match run_batch_analytics.py's --width (default 1280)")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help="Must match run_batch_analytics.py's --height (default 720)")
    parser.add_argument("--output", default=None, help="Where to save the zone config (default: batch_analytics/zone_configs/<channel>.json)")
    args = parser.parse_args()

    try:
        frame, segment_name, _resolved_date = find_reference_frame(
            args.channel, args.videos_root, args.date, args.segment_index
        )
    except ReferenceFrameError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    frame = cv2.resize(frame, (args.width, args.height), interpolation=cv2.INTER_AREA)

    output_path = Path(args.output) if args.output else default_zone_config_path(args.channel)
    initial_zones, initial_lines = [], []
    if output_path.exists():
        initial_zones, initial_lines, config_width, config_height = load_zone_config(output_path)
        print(f"Loaded existing config from {output_path}: {len(initial_zones)} zone(s), {len(initial_lines)} line(s)")
        if (config_width, config_height) != (args.width, args.height):
            print(
                f"WARNING: existing config was drawn at {config_width}x{config_height}, "
                f"but this session is using {args.width}x{args.height} — shapes will be "
                f"shown at the wrong position/size. Pass --width {config_width} --height "
                f"{config_height} to match.",
                file=sys.stderr,
            )

    print(__doc__)
    print(f"Reference frame: {args.width}x{args.height} (this must match run_batch_analytics.py's resolution)")

    root = tk.Tk()
    app = ZoneDrawerApp(
        root, frame, f"Draw zones — {args.channel} ({segment_name})",
        initial_zones=initial_zones, initial_lines=initial_lines,
    )
    root.mainloop()
    root.destroy()

    if not app.saved:
        print("Quit without saving.")
        return 0

    if not app.zones and not app.lines:
        print("Nothing drawn, nothing saved.")
        return 0

    save_zone_config(output_path, args.channel, args.width, args.height, app.zones, app.lines)
    print(f"Saved to {output_path}")
    print(
        f"Run with: python3 run_batch_analytics.py --channel {args.channel} --date <date> "
        f"--width {args.width} --height {args.height}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
