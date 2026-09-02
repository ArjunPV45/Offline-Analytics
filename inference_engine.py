import threading
import time
import logging
import os
import numpy as np
import cv2
import queue
from typing import Optional

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

import hailo

from logging_config import get_logger

logger = get_logger(__name__)

_HEF_PATH_H8L = os.path.join(os.path.dirname(__file__), 'resources/yolov8s_h8l.hef')
_HEF_PATH_H8  = os.path.join(os.path.dirname(__file__), 'resources/yolov8m.hef')
_POST_SO      = os.path.join(os.path.dirname(__file__), 'resources/libyolo_hailortpp_postprocess.so')

INPUT_W = 640
INPUT_H = 640

NMS_SCORE_THRESHOLD = 0.4
NMS_IOU_THRESHOLD   = 0.5


def _detect_arch() -> str:
    try:
        import subprocess
        result = subprocess.run(
            ['hailortcli', 'fw-control', 'identify'],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.split('\n'):
            if 'Device Architecture' in line:
                if 'HAILO8L' in line:
                    return 'hailo8l'
                elif 'HAILO8' in line:
                    return 'hailo8'
    except Exception:
        pass
    return 'hailo8l'


class HailoInferenceEngine:

    def __init__(self, arch: Optional[str] = None):
        self._arch = arch or _detect_arch()
        self._hef_path = _HEF_PATH_H8L if self._arch == 'hailo8l' else _HEF_PATH_H8
        self._pipeline: Optional[Gst.Pipeline] = None
        self._appsrc = None
        self._appsink = None
        self._result_queue: queue.Queue = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._started = False

    @property
    def is_running(self) -> bool:
        return self._started


    def start(self):
        if self._started:
            return
        self._build_pipeline()
        self._started = True
        logger.info(f"[InferenceEngine] Started (arch={self._arch}, hef={self._hef_path})")

    def stop(self):
        if not self._started:
            return
        try:
            if self._pipeline:
                self._pipeline.set_state(Gst.State.NULL)
                self._pipeline = None
        except Exception as e:
            logger.warning(f"[InferenceEngine] Stop error: {e}")
        self._started = False
        logger.info("[InferenceEngine] Stopped")


    def infer(self, frame_bgr: np.ndarray) -> list:
        if not self._started or self._appsrc is None:
            return []

        # Health check: ensure pipeline is still playing
        if self._pipeline:
            state_ret, current_state, _ = self._pipeline.get_state(0)
            if state_ret == Gst.StateChangeReturn.FAILURE or current_state != Gst.State.PLAYING:
                logger.warning(f"[InferenceEngine] Pipeline state unhealthy ({current_state}). Attempting recovery...")
                self._pipeline.set_state(Gst.State.PLAYING)

        with self._lock:
            # Clear previous result if any to ensure fresh start
            try:
                while not self._result_queue.empty():
                    self._result_queue.get_nowait()
            except Exception:
                pass

            t_start = time.time()
            frame_rgb = cv2.cvtColor(
                cv2.resize(frame_bgr, (INPUT_W, INPUT_H)),
                cv2.COLOR_BGR2RGB
            )

            data = frame_rgb.tobytes()
            buf = Gst.Buffer.new_allocate(None, len(data), None)
            buf.fill(0, data)
            buf.pts = Gst.CLOCK_TIME_NONE
            buf.dts = Gst.CLOCK_TIME_NONE

            # push-buffer is usually non-blocking but we log timing
            ret = self._appsrc.emit('push-buffer', buf)
            if ret != Gst.FlowReturn.OK:
                logger.warning(f"[InferenceEngine] push-buffer failed: {ret}")
                return []

            try:
                # Wait for result with a timeout to avoid blocking consumer thread indefinitely
                detections = self._result_queue.get(timeout=1.5)
                latency = (time.time() - t_start) * 1000
                if latency > 500:
                    logger.debug(f"[InferenceEngine] High inference latency: {latency:.1f}ms")
                return detections
            except queue.Empty:
                logger.warning(f"[InferenceEngine] Inference timeout after 1.5s")
                return []


    def _build_pipeline(self):
        tappas_dir = os.environ.get('TAPPAS_POST_PROC_DIR', '')

        pipeline_str = (
            f"appsrc name=hailo_src "
            f"caps=video/x-raw,format=RGB,width={INPUT_W},height={INPUT_H},framerate=0/1 "
            f"is-live=true block=true max-buffers=1 leaky-type=downstream ! "
            f"queue max-size-buffers=1 leaky=downstream ! "
            f"videoconvert ! "
            f"video/x-raw,format=RGB,pixel-aspect-ratio=1/1 ! "
            f"queue max-size-buffers=1 leaky=downstream ! "
            f"hailonet name=hailo_net "
            f"hef-path={self._hef_path} "
            f"batch-size=1 "
            f"vdevice-group-id=1 "
            f"nms-score-threshold={NMS_SCORE_THRESHOLD} "
            f"nms-iou-threshold={NMS_IOU_THRESHOLD} "
            f"output-format-type=HAILO_FORMAT_TYPE_FLOAT32 "
            f"force-writable=true ! "
            f"queue max-size-buffers=1 leaky=downstream ! "
            f"hailofilter name=hailo_filter "
            f"so-path={_POST_SO} "
            f"function-name=filter_letterbox qos=false ! "
            f"queue max-size-buffers=1 leaky=downstream ! "
            f"appsink name=hailo_sink max-buffers=1 drop=false sync=false emit-signals=true"
        )

        self._pipeline = Gst.parse_launch(pipeline_str)

        self._appsrc = self._pipeline.get_by_name('hailo_src')
        self._appsink = self._pipeline.get_by_name('hailo_sink')

        if not self._appsrc or not self._appsink:
            raise RuntimeError("Failed to get appsrc/appsink from inference pipeline")

        self._appsink.connect('new-sample', self._on_new_sample)

        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message', self._on_bus_message)

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Inference pipeline failed to enter PLAYING state")

        
        self._pipeline.get_state(timeout=5 * Gst.SECOND)

    def _on_new_sample(self, appsink) -> Gst.FlowReturn:
        sample = appsink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.OK

        buf = sample.get_buffer()
        detections = self._parse_detections(buf)

        try:
            self._result_queue.put_nowait(detections)
        except queue.Full:
            try:
                self._result_queue.get_nowait()
            except queue.Empty:
                pass
            self._result_queue.put_nowait(detections)

        return Gst.FlowReturn.OK

    def _parse_detections(self, buf: Gst.Buffer) -> list:
        
        detections = []
        try:
            roi = hailo.get_roi_from_buffer(buf)
            if roi is None:
                return detections
            for d in roi.get_objects_typed(hailo.HAILO_DETECTION):
                label = d.get_label()
                confidence = d.get_confidence()
                bbox = d.get_bbox()
                x1 = bbox.xmin() * INPUT_W
                y1 = bbox.ymin() * INPUT_H
                x2 = bbox.xmax() * INPUT_W
                y2 = bbox.ymax() * INPUT_H
                if x2 <= x1 or y2 <= y1:
                    continue
                detections.append((x1, y1, x2, y2, confidence, label))
        except Exception as e:
            logger.warning(f"[InferenceEngine] Detection parse error: {e}")
        return detections

    def _on_bus_message(self, bus, message) -> bool:
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"[InferenceEngine] GStreamer error: {err} | {debug}")
        elif message.type == Gst.MessageType.WARNING:
            warn, _ = message.parse_warning()
            logger.warning(f"[InferenceEngine] GStreamer warning: {warn}")
        return True
