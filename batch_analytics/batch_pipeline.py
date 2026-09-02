"""Offline batch detection+tracking pipeline for saved CCTV segment files.

Built on the official hailo_apps GStreamer framework (SOURCE_PIPELINE,
INFERENCE_PIPELINE_WRAPPER, TRACKER_PIPELINE) rather than a hand-rolled
appsrc/hailonet pipeline.

Multi-file sequencing: GStreamerApp.on_eos() normally *loops the same file*
forever when the source is a file (see gstreamer_app.py's on_eos/_rebuild_pipeline).
This subclass overrides on_eos() to advance to the next segment in the day's
queue instead, rebuilding the pipeline each time (which reloads the model —
this per-file overhead is intentionally measured, not hidden, since it's a
real cost of this design). When the queue is empty, it writes the day's
JSON report and shuts down.

Known limitation: because each file boundary destroys and recreates the
pipeline, hailotracker's state (and therefore track IDs) resets at every
file boundary. Zone/line dwell-time and in/out counts can therefore be off
by a small amount for anyone present exactly at a segment cut. Aggregate
per-day detection/track counts and the timing benchmark are unaffected.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
import hailo
from gi.repository import GLib, Gst

from hailo_apps.python.core.common.buffer_utils import get_caps_from_pad, get_numpy_from_buffer
from hailo_apps.python.core.common.defines import GST_VIDEO_SINK
from hailo_apps.python.core.common.hailo_logger import get_logger
from hailo_apps.python.core.gstreamer.gstreamer_app import app_callback_class
from hailo_apps.python.core.gstreamer.gstreamer_helper_pipelines import (
    DISPLAY_PIPELINE,
    INFERENCE_PIPELINE,
    INFERENCE_PIPELINE_WRAPPER,
    QUEUE,
    TRACKER_PIPELINE,
    USER_CALLBACK_PIPELINE,
)
from hailo_apps.python.pipeline_apps.detection.detection_pipeline import GStreamerDetectionApp

from batch_analytics.day_result_push import push_day_result
from batch_analytics.zone_counter_offline import OfflineZoneLineCounter

logger = get_logger(__name__)

PERSON_LABEL = "person"

# Change this to control frame-skipping for analysis. None = process every
# real frame (no skipping). A number (e.g. 5 or 7) = only analyze frames at
# roughly that many per second, dropping the rest before they reach scaling,
# inference, tracking, or overlay drawing — cutting processing time roughly
# proportionally, since most of the per-frame cost happens downstream of this
# point. Can also be overridden per run with --analysis-fps.
DEFAULT_ANALYSIS_FPS: float | None = None


class BatchAppCallback(app_callback_class):
    """Per-frame state, reset at the start of every segment file."""

    def __init__(self, counter: OfflineZoneLineCounter):
        super().__init__()
        self.counter = counter
        self.file_frames = 0
        self.file_detections = 0
        self.file_track_ids: set[int] = set()

    def reset_file_stats(self) -> None:
        self.file_frames = 0
        self.file_detections = 0
        self.file_track_ids = set()


def app_callback(element, buffer, user_data: BatchAppCallback):
    roi = hailo.get_roi_from_buffer(buffer)
    if roi is None:
        return

    pad = element.get_static_pad("src")
    _, width, height = get_caps_from_pad(pad)
    width = width or 1
    height = height or 1

    people = set()
    detection_count = 0

    for detection in roi.get_objects_typed(hailo.HAILO_DETECTION):
        if detection.get_label() != PERSON_LABEL:
            continue
        detection_count += 1

        track_objs = detection.get_objects_typed(hailo.HAILO_UNIQUE_ID)
        if len(track_objs) != 1:
            continue
        track_id = track_objs[0].get_id()
        user_data.file_track_ids.add(track_id)

        bbox = detection.get_bbox()
        x1 = bbox.xmin() * width
        y1 = bbox.ymin() * height
        x2 = bbox.xmax() * width
        y2 = bbox.ymax() * height
        if x2 > x1 and y2 > y1:
            people.add((track_id, x1, y1, x2, y2))

    user_data.file_frames += 1
    user_data.file_detections += detection_count
    user_data.counter.update(people)

    _add_zone_overlays(roi, user_data.counter, width, height)


def _add_zone_overlays(roi, counter: OfflineZoneLineCounter, width: int, height: int) -> None:
    """Injects zones/lines as synthetic HailoDetection objects with the live
    in/out counts in their label, so hailooverlay (already reliably drawing
    real detection boxes + track IDs in --display) renders them too — no
    OpenCV/Qt window code involved, reusing the pipeline's own proven overlay
    rendering instead of a second, separate display mechanism.

    Lines are approximated as a thin axis-aligned box spanning their two
    endpoints (exact for horizontal/vertical lines, a bounding-box
    approximation for diagonal ones) since hailooverlay only draws boxes.
    """
    if not counter.has_zones_or_lines:
        return

    for shape in counter.overlay_shapes():
        label = f"{shape['name']} in:{shape['in_count']} out:{shape['out_count']}"

        if shape["kind"] == "zone":
            x0, y0 = shape["top_left"]
            x1, y1 = shape["bottom_right"]
        else:
            x0, y0 = shape["start"]
            x1, y1 = shape["end"]
            x0, x1 = sorted((x0, x1))
            y0, y1 = sorted((y0, y1))
            if x1 - x0 < 6:
                x0, x1 = x0 - 3, x1 + 3
            if y1 - y0 < 6:
                y0, y1 = y0 - 3, y1 + 3

        x_min = max(0.0, min(1.0, x0 / width))
        y_min = max(0.0, min(1.0, y0 / height))
        box_width = max(0.0, min(1.0, x1 / width) - x_min)
        box_height = max(0.0, min(1.0, y1 / height) - y_min)
        if box_width <= 0 or box_height <= 0:
            continue

        bbox = hailo.HailoBBox(x_min, y_min, box_width, box_height)
        roi.add_object(hailo.HailoDetection(bbox, label, 1.0))


class BatchDetectionApp(GStreamerDetectionApp):
    """Sequentially runs detection+tracking over every segment file for one
    camera/day, then writes a JSON timing + analytics report."""

    def __init__(
        self,
        app_callback,
        user_data: BatchAppCallback,
        parser,
        pending_segments,
        first_segment,
        channel: str,
        date_str: str,
        output_dir: Path,
        display: bool = False,
        analysis_fps: float | None = DEFAULT_ANALYSIS_FPS,
    ):
        # Must be set before super().__init__(), which triggers create_pipeline()
        # -> get_pipeline_string() (our override) as its very last step.
        self._display = display
        self.analysis_fps = analysis_fps
        self._last_kept_pts: int | None = None

        super().__init__(app_callback, user_data, parser)
        self._attach_frame_decimator_probe()

        self._pending_segments = list(pending_segments)
        self._current_segment = first_segment
        self._channel = channel
        self._date_str = date_str
        self._output_dir = Path(output_dir)
        self._file_records: list[dict] = []
        self._file_start_wall = time.time()

        total = 1 + len(self._pending_segments)
        logger.info(
            "Batch run initialized: channel=%s date=%s segments=%d analysis_fps=%s",
            channel, date_str, total, self.analysis_fps,
        )

    def _attach_frame_decimator_probe(self) -> None:
        """Attaches (or reattaches, after a per-file pipeline rebuild) the
        frame-skipping probe. No-op if analysis_fps is None."""
        if self.analysis_fps is None:
            return

        self._last_kept_pts = None
        element = self.pipeline.get_by_name("frame_decimator")
        if element is None:
            logger.warning("frame_decimator element not found; frame skipping inactive")
            return

        pad = element.get_static_pad("src")
        pad.add_probe(Gst.PadProbeType.BUFFER, self._decimate_probe)

    def _decimate_probe(self, pad, info) -> Gst.PadProbeReturn:
        """Keeps a buffer only if enough video-time has passed since the last
        kept one to hit analysis_fps — a time-based decimator, not a raw frame
        counter, so it works correctly on this variable-frame-rate footage
        (a fixed-N-th-frame counter would skip unevenly on VFR content).

        Deliberately not using GStreamer's videorate element for this: it
        pathologically duplicates frames on this footage (see the module
        docstring on _file_source_pipeline / .hailo/memory/common_pitfalls.md).
        This probe only ever drops buffers, never fabricates new ones, so it
        doesn't share that failure mode.
        """
        buf = info.get_buffer()
        if buf is None or buf.pts == Gst.CLOCK_TIME_NONE:
            return Gst.PadProbeReturn.OK  # can't judge timing; let it through

        interval_ns = int(Gst.SECOND / self.analysis_fps)
        if self._last_kept_pts is None or (buf.pts - self._last_kept_pts) >= interval_ns:
            self._last_kept_pts = buf.pts
            return Gst.PadProbeReturn.OK
        return Gst.PadProbeReturn.DROP

    def _on_pipeline_rebuilt(self) -> None:
        """Called by the framework after each per-file pipeline rebuild —
        reattach the decimator probe to the new pipeline's frame_decimator
        element (the old one, and its probe, were destroyed with it)."""
        self._attach_frame_decimator_probe()

    def _file_source_pipeline(self, name: str = "source") -> str:
        """Builds a file-source fragment equivalent to the framework's SOURCE_PIPELINE,
        minus the videorate+capsfilter stage.

        These NVR-recorded segments are variable-frame-rate (ffprobe reports
        avg_frame_rate=0/0). Feeding VFR content through SOURCE_PIPELINE's
        `videorate ! capsfilter` stage (meant for live-camera rate pacing)
        causes videorate to pathologically duplicate frames — confirmed by
        isolated testing: it inflated an 1,214-real-frame, 81s clip to
        ~2.4 million buffers taking 7.5 hours to process. Dropping that stage
        (unnecessary for offline batch processing — we want every real frame
        processed once, not rate-converted) fixes it: the same clip then
        correctly processes its 1,214 real frames in ~12 seconds.
        """
        return (
            f'filesrc location="{self.video_source}" name={name} ! '
            f"{QUEUE(name=f'{name}_queue_decode')} ! "
            f"decodebin name={name}_decodebin ! "
            f"identity name=frame_decimator ! "
            f"{QUEUE(name=f'{name}_scale_q')} ! "
            f"videoscale name={name}_videoscale n-threads=2 ! "
            f"{QUEUE(name=f'{name}_convert_q')} ! "
            f"videoconvert n-threads=3 name={name}_convert qos=false ! "
            f"video/x-raw, pixel-aspect-ratio=1/1, format={self.video_format}, "
            f"width={self.video_width}, height={self.video_height} "
        )

    def get_pipeline_string(self) -> str:
        source_pipeline = self._file_source_pipeline()
        detection_pipeline = INFERENCE_PIPELINE(
            hef_path=self.hef_path,
            post_process_so=self.post_process_so,
            post_function_name=self.post_function_name,
            batch_size=self.batch_size,
            config_json=self.labels_json,
            additional_params=self.thresholds_str,
            # Without this, only one run_batch_analytics.py process can hold
            # the Hailo8L device at a time — a second concurrent process fails
            # immediately with HAILO_OUT_OF_PHYSICAL_DEVICES. The system config
            # (hailo_apps/config/config.yaml: multi_processing: "enabled") only
            # makes this available; each hailonet client still has to request
            # it explicitly for HailoRT's multi-process scheduler to share the
            # physical device across processes.
            multi_process_service="true",
            # scheduler-timeout-ms defaults to 0 (no scheduler-enforced fairness
            # deadline at all). Under sustained real load with 3 concurrent
            # clients, one client starved the others long enough to hit a
            # separate ~10s internal HailoRT timeout — not just a slowdown, but
            # a fatal, non-recoverable pipeline error that could leave the
            # process hung (its own error-shutdown path also timed out).
            # Setting an explicit, bounded value here asks HailoRT's scheduler
            # to guarantee this client gets run time within that window,
            # rather than let round-robin fairness go unenforced.
            scheduler_timeout_ms=5000,
        )
        detection_pipeline_wrapper = INFERENCE_PIPELINE_WRAPPER(detection_pipeline)
        # class_id=-1: track all classes: we filter to "person" by label in
        # app_callback rather than relying on the tracker's class_id, since
        # COCO class indices vary by model/postprocess config.
        tracker_pipeline = TRACKER_PIPELINE(class_id=-1)
        user_callback_pipeline = USER_CALLBACK_PIPELINE()
        # video_sink stays "fakesink" for headless batch runs. With --display,
        # show a real window instead — sync stays "false" either way so the
        # display runs at the same speed the detection model actually
        # processes at (not throttled to real-time playback).
        display_pipeline = DISPLAY_PIPELINE(
            video_sink=(GST_VIDEO_SINK if self._display else "fakesink"),
            sync="false",
            show_fps=self.show_fps,
        )
        # DISPLAY_PIPELINE's own internal queues are all leaky=no (block when
        # full, never drop) — fine for fakesink, which drains instantly, but
        # a real rendering sink can't always keep up with hardware-max
        # inference throughput. Without a leaky queue here, a slow renderer
        # backpressures all the way up through hailonet/tracker (observed:
        # QoS messages piling up on source_videoscale, upstream of inference,
        # and overall throughput collapsing well below the headless benchmark
        # rate). This queue decouples them: display is best-effort and may
        # skip frames, but source/inference/tracker/analytics always run at
        # full hardware speed regardless of rendering speed.
        display_decouple_q = QUEUE(name="display_decouple_q", max_size_buffers=2, leaky="downstream")

        return (
            f"{source_pipeline} ! "
            f"{detection_pipeline_wrapper} ! "
            f"{tracker_pipeline} ! "
            f"{user_callback_pipeline} ! "
            f"{display_decouple_q} ! "
            f"{display_pipeline}"
        )

    def on_eos(self):
        self._finalize_current_segment()

        if self._pending_segments:
            next_segment = self._pending_segments.pop(0)
            self._current_segment = next_segment
            self.video_source = str(next_segment.path)
            self.user_data.reset_file_stats()
            self._file_start_wall = time.time()
            logger.info("Advancing to next segment: %s", next_segment.path.name)
            GLib.idle_add(self._rebuild_pipeline)
        else:
            self._write_day_report()
            logger.info("Day complete: channel=%s date=%s — shutting down", self._channel, self._date_str)
            self.shutdown()

    def _finalize_current_segment(self) -> None:
        wall_elapsed = time.time() - self._file_start_wall
        segment = self._current_segment
        record = {
            "filename": segment.path.name,
            "start_time": segment.start_dt.isoformat(),
            "end_time": segment.end_dt.isoformat(),
            "nominal_duration_s": segment.nominal_duration_s,
            "wall_processing_time_s": wall_elapsed,
            "realtime_factor": (
                segment.nominal_duration_s / wall_elapsed if wall_elapsed > 0 else None
            ),
            "frames_processed": self.user_data.file_frames,
            "person_detections": self.user_data.file_detections,
            "unique_track_ids": len(self.user_data.file_track_ids),
        }
        self._file_records.append(record)
        logger.info(
            "Finished %s: %.1fs video processed in %.1fs wall time (%.2fx realtime), "
            "%d frames, %d person detections, %d unique tracks",
            segment.path.name,
            segment.nominal_duration_s,
            wall_elapsed,
            record["realtime_factor"] or 0.0,
            record["frames_processed"],
            record["person_detections"],
            record["unique_track_ids"],
        )

    def _write_day_report(self) -> None:
        total_video_s = sum(r["nominal_duration_s"] for r in self._file_records)
        total_wall_s = sum(r["wall_processing_time_s"] for r in self._file_records)

        report = {
            "channel": self._channel,
            "date": self._date_str,
            "segment_count": len(self._file_records),
            "total_video_seconds": total_video_s,
            "total_wall_seconds": total_wall_s,
            "realtime_factor": (total_video_s / total_wall_s) if total_wall_s > 0 else None,
            "total_person_detections": sum(r["person_detections"] for r in self._file_records),
            "total_unique_track_ids_per_segment": sum(r["unique_track_ids"] for r in self._file_records),
            "segments": self._file_records,
            **self.user_data.counter.summary(),
        }

        self._output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self._output_dir / f"{self._channel}_{self._date_str}.json"
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2)

        factor = report["realtime_factor"]
        logger.info(
            "Report written to %s — %.1f video-hours processed in %.1f wall-hours (%.2fx realtime)",
            out_path,
            total_video_s / 3600.0,
            total_wall_s / 3600.0,
            factor or 0.0,
        )

        # Best-effort; the local report above is already the source of
        # truth regardless of whether this succeeds. See day_result_push.py.
        push_day_result(self._channel, self._date_str, report)
