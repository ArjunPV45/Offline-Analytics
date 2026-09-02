#!/usr/bin/env python3
"""CPU-only inference speed benchmark: how fast can this Pi's CPU alone (no
Hailo accelerator) run a YOLO detection model — for direct comparison
against the Hailo8L-accelerated numbers measured in ../batch_analytics/.

Runs in its own venv, NOT venv_hailo_apps — needs torch + ultralytics, which
are heavy CPU-inference-specific packages irrelevant to the Hailo pipeline
and don't belong mixed into that environment. See the setup commands in
cpu_benchmark/README.md.

This measures two numbers, since they answer different questions:
  - "inference-only fps": time inside the model() call only (preprocess +
    forward pass + NMS) — the fair comparison against the Hailo8L chip's own
    inference throughput.
  - "wall fps": includes video decode too — the actually-achievable
    end-to-end rate on this hardware, since on a CPU-only run, decode and
    inference compete for the same 4 cores (unlike the Hailo case, where
    inference runs on a separate chip while the CPU only does decode/scale).

Usage:
    python3 run_cpu_inference_benchmark.py --video /path/to/file.mp4
    python3 run_cpu_inference_benchmark.py --video /path/to/file.mp4 --model yolov8s.pt
    python3 run_cpu_inference_benchmark.py --dir /path/to/folder --max-frames 500
    python3 run_cpu_inference_benchmark.py --video /path/to/file.mp4 --display

--display opens a live window with detection boxes drawn, for visually
verifying detections look right — not opencv's own GUI (this venv installs
opencv-python-headless on purpose; cv2.imshow would just fail here), but
Tkinter, same proven approach as ../draw_zone.py after cv2's Qt-based GUI
turned out unreliable on this Pi's desktop.
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time

import cv2

# Only meaningful with --display; harmless (and cheap) to import unconditionally.
import tkinter as tk

os.environ.setdefault("DISPLAY", ":0")  # see ../draw_zone.py's docstring for why

DEFAULT_MODEL = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "GPUvarient-main", "yolo26m.pt")
)
PERSON_CLASS_ID = 0  # COCO class 0 = "person" (standard Ultralytics ordering)


def frame_to_photoimage(frame_bgr) -> tk.PhotoImage:
    """BGR numpy frame -> Tkinter PhotoImage via a raw in-memory PPM blob —
    no Pillow/ImageTk, no OpenCV display code (see ../draw_zone.py)."""
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    height, width = rgb.shape[:2]
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    return tk.PhotoImage(data=header + rgb.tobytes())


class LiveDisplay:
    """A single Tk window, updated frame by frame. Since CPU inference here
    runs well under video frame rate, there's no need for anything fancier
    than "redraw whenever a new annotated frame is ready."""

    def __init__(self, title: str):
        self.root = tk.Tk()
        self.root.title(title)
        self.label = tk.Label(self.root)
        self.label.pack()
        self.closed = False
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._photo = None  # keep a reference or Tkinter drops the image

    def _on_close(self) -> None:
        self.closed = True

    def show(self, frame_bgr) -> None:
        if self.closed:
            return
        self._photo = frame_to_photoimage(frame_bgr)
        self.label.configure(image=self._photo)
        self.root.update()

    def close(self) -> None:
        if not self.closed:
            self.root.destroy()
            self.closed = True


def iter_video_paths(args):
    if args.video:
        yield args.video
    elif args.dir:
        paths = sorted(glob.glob(os.path.join(args.dir, "*.mp4")))
        if not paths:
            print(f"ERROR: no .mp4 files found in {args.dir}", file=sys.stderr)
            sys.exit(1)
        yield from paths
    else:
        print("ERROR: provide --video <file> or --dir <folder>", file=sys.stderr)
        sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--video", help="Single video file to benchmark")
    parser.add_argument("--dir", help="Folder of .mp4 files to benchmark (processed in sequence)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Path to a YOLO .pt model (default: {DEFAULT_MODEL})")
    parser.add_argument("--imgsz", type=int, default=640, help="Inference resolution")
    parser.add_argument("--conf", type=float, default=0.35, help="Confidence threshold")
    parser.add_argument(
        "--max-frames", type=int, default=None,
        help="Stop after this many timed frames total -- useful for a quick sanity run before a full one",
    )
    parser.add_argument(
        "--threads", type=int, default=None,
        help="torch.set_num_threads override (default: torch's own choice, usually = CPU count)",
    )
    parser.add_argument(
        "--warmup-frames", type=int, default=5,
        help="Frames run before timing starts (model warmup cost is real but shouldn't skew steady-state fps)",
    )
    parser.add_argument(
        "--progress-every", type=int, default=10,
        help="Print a running fps/detection-count line every N timed frames (0 to disable).",
    )
    parser.add_argument(
        "--display", action="store_true",
        help="Show a live window with detection boxes drawn, to visually verify detections look right.",
    )
    args = parser.parse_args()

    import torch
    from ultralytics import YOLO

    if args.threads:
        torch.set_num_threads(args.threads)
    print(f"torch threads: {torch.get_num_threads()}")

    print(f"Loading model: {args.model}")
    model = YOLO(args.model, task="detect")

    total_frames = 0
    total_detections = 0
    total_infer_time = 0.0
    warmup_count = 0
    warmed_up = False
    stop = False

    display = LiveDisplay(f"CPU inference benchmark — {os.path.basename(args.model)}") if args.display else None

    overall_t0 = time.time()

    try:
        for video_path in iter_video_paths(args):
            if stop:
                break
            print(f"\n--- {video_path} ---", flush=True)
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                print("  could not open, skipping", file=sys.stderr)
                continue

            video_frames = 0
            video_t0 = time.time()

            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if display is not None and display.closed:
                    print("\nDisplay window closed -- stopping.")
                    stop = True
                    break

                if not warmed_up:
                    print(f"  warming up ({warmup_count + 1}/{args.warmup_frames})...", flush=True)
                    model(frame, imgsz=args.imgsz, conf=args.conf, classes=[PERSON_CLASS_ID], device="cpu", verbose=False)
                    warmup_count += 1
                    if warmup_count >= args.warmup_frames:
                        warmed_up = True
                    continue

                t0 = time.perf_counter()
                results = model(frame, imgsz=args.imgsz, conf=args.conf, classes=[PERSON_CLASS_ID], device="cpu", verbose=False)
                total_infer_time += time.perf_counter() - t0

                frame_detections = 0
                if results and results[0].boxes is not None:
                    frame_detections = len(results[0].boxes)
                    total_detections += frame_detections
                total_frames += 1
                video_frames += 1

                if display is not None:
                    display.show(results[0].plot())

                if args.progress_every and total_frames % args.progress_every == 0:
                    elapsed = time.time() - overall_t0
                    print(
                        f"  [progress] {total_frames} frames timed, "
                        f"{total_frames / elapsed:.2f} fps running average, "
                        f"{total_detections} detections so far "
                        f"({frame_detections} in this frame)",
                        flush=True,
                    )

                if args.max_frames and total_frames >= args.max_frames:
                    stop = True
                    break

            cap.release()
            video_elapsed = time.time() - video_t0
            if video_frames:
                print(f"  {video_frames} frames in {video_elapsed:.1f}s wall ({video_frames / video_elapsed:.2f} fps wall)")
    finally:
        if display is not None:
            display.close()

    overall_elapsed = time.time() - overall_t0

    print("\n=== Summary ===")
    print(f"Model: {args.model}")
    print(f"Frames timed (after {warmup_count}-frame warmup): {total_frames}")
    print(f"Total person detections: {total_detections}")
    if total_frames == 0:
        print("No frames were timed -- nothing to report.")
        return 1
    print(f"Pure inference time: {total_infer_time:.1f}s -> {total_frames / total_infer_time:.2f} inference-only fps")
    print(f"Total wall time (incl. decode): {overall_elapsed:.1f}s -> {total_frames / overall_elapsed:.2f} fps wall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
