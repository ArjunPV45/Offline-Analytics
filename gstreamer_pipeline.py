import threading
import time
import logging
import os
import cv2
import numpy as np
from typing import Optional

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst

from logging_config import get_logger
from frame_producer import FrameProducer
from inference_engine import HailoInferenceEngine
from tracker_manager import CameraTrackerManager

logger = get_logger(__name__)

_IDLE_SLEEP = 0.1

_MIN_FRAME_AGE = 0.05  

_WATCHDOG_THRESHOLD = 60.0

def _draw_zones_on_frame(frame: np.ndarray, user_data, camera_id: str) -> None:
    if camera_id not in user_data.data:
        return
    
    try:
        with user_data.lock:
            zones_copy = {z: dict(d) for z, d in user_data.data[camera_id].get("zones", {}).items()}
            lines_copy = {l: dict(d) for l, d in user_data.data[camera_id].get("lines", {}).items()}
    except Exception as e:
        logger.error(f"Error copying zones/lines for drawing: {e}")
        return

    # The zones and lines are drawn on every frame to ensure snapshots and live view 
    # are always annotated.
    for zone, data in zones_copy.items():
        try:
            if "points" in data and data["points"]:
                pts = np.array(data["points"], np.int32).reshape((-1, 1, 2))
                cv2.polylines(frame, [pts], True, (0, 0, 255), 2)
                # Label at first point
                origin = tuple(map(int, data["points"][0]))
                cv2.putText(frame, zone, (origin[0], origin[1] - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            elif "top_left" in data and "bottom_right" in data:
                tl = tuple(map(int, data["top_left"]))
                br = tuple(map(int, data["bottom_right"]))
                cv2.rectangle(frame, tl, br, (0, 0, 255), 2)
                cv2.putText(frame, zone, (tl[0], tl[1] - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        except Exception as e:
            logger.warning(f"Failed to draw zone '{zone}' on {camera_id}: {e}")
            continue

    for line_name, data in lines_copy.items():
        try:
            sp = tuple(map(int, data["start"]))
            ep = tuple(map(int, data["end"]))
            cv2.line(frame, sp, ep, (0, 255, 0), 2)
            cv2.putText(frame, line_name, (sp[0], sp[1] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        except Exception as e:
            logger.warning(f"Failed to draw line '{line_name}' on {camera_id}: {e}")
            continue

def _draw_tracking_on_frame(frame: np.ndarray, tracked_persons: list = None) -> None:
    if not tracked_persons:
        return
        
    for track_data in tracked_persons:
        if len(track_data) == 5:
            track_id, x1, y1, x2, y2 = track_data
            cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), 2)
            cv2.putText(frame, f"ID:{track_id}", (int(x1), int(y1) - 10), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        else:
            track_id, cx, cy = track_data
            cv2.circle(frame, (int(cx), int(cy)), 5, (0, 255, 0), -1)
            cv2.putText(frame, f"ID:{track_id}", (int(cx) + 10, int(cy)), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)



class PipelineManager:

    def __init__(self, user_data, frame_buffers: dict, clean_buffers: dict = None):
        self.user_data = user_data
        self.frame_buffers = frame_buffers
        self.clean_buffers = clean_buffers if clean_buffers is not None else {}

        self.video_sources: list = []
        self.camera_names: list = []
        self.is_running_flag = False
        self.health_monitor = None

        self._producers: dict = {}

        self._inference_engine = HailoInferenceEngine()
        self._tracker_manager = CameraTrackerManager()

        self._consumer_thread: Optional[threading.Thread] = None
        self._consumer_stop = threading.Event()

        self._last_ts: dict = {}

        self._proc_count: dict = {}
        self._first_frame_logged: set = set()
        
        self._enable_preview = os.getenv("LOCAL_PREVIEW", "false").lower() in ("true", "1", "yes")
        if self._enable_preview:
            logger.info("Local preview window enabled (LOCAL_PREVIEW=true)")

        logger.info("PipelineManager (Round-Robin) initialized.")


    def start_pipeline(self,
                       video_sources: list,
                       custom_camera_names: Optional[list] = None,
                       on_started_callback=None) -> bool:
        if self.is_running():
            logger.info("Pipeline already running — merging new configuration.")
            
            self.stop_pipeline()
            time.sleep(1)

        if custom_camera_names is None:
            desired_names = [f"camera{i+1}" for i in range(len(video_sources))]
        else:
            if len(custom_camera_names) != len(video_sources):
                raise ValueError("custom_camera_names length must equal video_sources length")
            desired_names = list(custom_camera_names)

        try:
            logger.info(f"Starting pipeline for {len(video_sources)} camera(s)…")

            if not self._inference_engine.is_running:
                self._inference_engine.start()

            self._producers.clear()
            self._last_ts.clear()
            self._proc_count.clear()
            self._first_frame_logged.clear()
            
            for cam_id, url in zip(desired_names, video_sources):
                self.add_camera(cam_id, url)

            self.camera_names = desired_names
            self.video_sources = list(video_sources)

            if not self._consumer_thread or not self._consumer_thread.is_alive():
                self._consumer_stop.clear()
                self._consumer_thread = threading.Thread(
                    target=self._unified_inference_loop,
                    name="consumer-loop",
                    daemon=True
                )
                self._consumer_thread.start()

            self.is_running_flag = True

            try:
                from pi_status_monitor import get_status_monitor
                sm = get_status_monitor(os.getenv("PI_UNIQUE_ID", "pi-default"))
                sm.set_pipeline_status(True, cameras=self.camera_names)
            except Exception:
                pass

            if on_started_callback:
                threading.Timer(2.0, on_started_callback, args=[self]).start()

            logger.info(f"Pipeline started: {self.camera_names}")
            return True

        except Exception as e:
            logger.error(f"Failed to start pipeline: {e}", exc_info=True)
            self.is_running_flag = False
            self._cleanup_resources()
            return False

    def add_camera(self, camera_id: str, rtsp_url: str) -> bool:
        """Adds a single camera to the running pipeline without stopping others."""
        try:
            if camera_id in self._producers:
                logger.info(f"Camera '{camera_id}' already in pipeline. Stopping old producer first.")
                self.remove_camera(camera_id)

            logger.info(f"Adding camera '{camera_id}' to pipeline: {rtsp_url}")
            
            if not self._inference_engine.is_running:
                self._inference_engine.start()

            producer = FrameProducer(camera_id=camera_id, rtsp_url=rtsp_url)
            producer.start()
            self._producers[camera_id] = producer
            self._last_ts[camera_id] = time.monotonic()
            
            if camera_id not in self.camera_names:
                self.camera_names.append(camera_id)
                self.video_sources.append(rtsp_url)

            # Start consumer if not already running
            if self.is_running_flag and (not self._consumer_thread or not self._consumer_thread.is_alive()):
                self._consumer_stop.clear()
                self._consumer_thread = threading.Thread(
                    target=self._unified_inference_loop,
                    name="consumer-loop",
                    daemon=True
                )
                self._consumer_thread.start()

            return True
        except Exception as e:
            logger.error(f"Failed to add camera {camera_id}: {e}")
            return False

    def remove_camera(self, camera_id: str) -> bool:
        try:
            if camera_id not in self._producers:
                logger.warning(f"Remove called for non-existent camera: {camera_id}")
                return True

            logger.info(f"Removing camera '{camera_id}' from pipeline.")
            producer = self._producers.pop(camera_id)
            producer.stop()

            if camera_id in self._last_ts:
                del self._last_ts[camera_id]
            if camera_id in self._proc_count:
                del self._proc_count[camera_id]
            if camera_id in self._first_frame_logged:
                self._first_frame_logged.remove(camera_id)
            
            
            if camera_id in self.camera_names:
                idx = self.camera_names.index(camera_id)
                self.camera_names.pop(idx)
                if idx < len(self.video_sources):
                    self.video_sources.pop(idx)

            if self._tracker_manager:
                self._tracker_manager.remove_camera(camera_id)

            return True
        except Exception as e:
            logger.error(f"Failed to remove camera {camera_id}: {e}")
            return False

    def stop_pipeline(self) -> bool:
        if not self.is_running():
            logger.info("Stop called but pipeline not running.")
            return True

        logger.info("Stopping pipeline…")
        try:

            self._consumer_stop.set()
            if self._consumer_thread and self._consumer_thread.is_alive():
                self._consumer_thread.join(timeout=5.0)

            self._cleanup_resources()

            self.frame_buffers.clear()
            if hasattr(self, 'clean_buffers'):
                self.clean_buffers.clear()
            self.is_running_flag = False

            try:
                from pi_status_monitor import get_status_monitor
                sm = get_status_monitor(os.getenv("PI_UNIQUE_ID", "pi-default"))
                sm.set_pipeline_status(False)
            except Exception:
                pass

            logger.info("Pipeline stopped.")
            return True
        except Exception as e:
            logger.error(f"Error stopping pipeline: {e}", exc_info=True)
            self.is_running_flag = False
            return False

    def is_running(self) -> bool:
        return self.is_running_flag

    def _update_frame_buffer(self, cam_id: str, frame: np.ndarray):
        """Updates the shared frame buffers for snapshots and streaming."""
        self.frame_buffers[cam_id] = frame
        # If clean_buffers exists, store a copy with zones but without tracking
        if self.clean_buffers is not None:
             self.clean_buffers[cam_id] = frame.copy()

    def _unified_inference_loop(self):
        logger.info("[Consumer] Unified inference loop started.")

        while not self._consumer_stop.is_set():
            if not self._producers:
                self._consumer_stop.wait(_IDLE_SLEEP)
                continue

            processed_any = False

            for cam_id, producer in list(self._producers.items()):
                if self._consumer_stop.is_set():
                    break

                try:
                    frame, ts = producer.get_latest_frame()

                    if frame is None or ts is None or ts <= self._last_ts.get(cam_id, 0.0):
                        continue

                    self._last_ts[cam_id] = ts
                    processed_any = True
                    
                    if cam_id not in self._first_frame_logged:
                        logger.info(f"[Consumer] Successfully received first frame from camera '{cam_id}'")
                        self._first_frame_logged.add(cam_id)

                    self._proc_count[cam_id] = self._proc_count.get(cam_id, 0) + 1

                    # 1. DRAW ZONES FIRST: Ensure zones/lines appear even if AI/Tracker fails or timeouts
                    _draw_zones_on_frame(frame, self.user_data, cam_id)
                    
                    # 2. UPDATE BUFFER EARLY: Ensure the frame (with zones) is visible in snapshots
                    self._update_frame_buffer(cam_id, frame)
                    
                    # 3. AI INFERENCE (with timeout)
                    t_infer_start = time.time()
                    raw_detections = self._inference_engine.infer(frame)
                    
                    # 4. TRACKING
                    tracked = self._tracker_manager.update(cam_id, raw_detections)
                    
                    # 5. COUNTING
                    self.user_data.update_counts(cam_id, set(tracked) if tracked else set())
                    
                    # 6. DRAW TRACKING (on the same frame object)
                    _draw_tracking_on_frame(frame, tracked)
                    
                    # Latency watchdog
                    latency = (time.time() - t_infer_start) * 1000
                    if latency > 2000:
                        logger.warning(f"[Consumer] High processing latency for camera '{cam_id}': {latency:.1f}ms")

                    if self._enable_preview and cam_id == self.user_data.active_camera:
                        cv2.imshow("Detection Preview", frame)
                        cv2.waitKey(1)

                    if self._proc_count[cam_id] % 100 == 0:
                        logger.info(f"[Consumer] Camera '{cam_id}' processed {self._proc_count[cam_id]} frames. "
                                    f"Latest: {len(raw_detections)} detections, {len(tracked)} tracks")

                    if self.health_monitor:
                        self.health_monitor.update_frame_timestamp(cam_id)
                    
                    try:
                        from pi_status_monitor import get_status_monitor
                        sm = get_status_monitor(os.getenv("PI_UNIQUE_ID", "pi-default"))
                        sm.update_frame_time()
                    except Exception:
                        pass

                except Exception as e:
                    logger.error(f"[Consumer] Error in processing camera '{cam_id}': {e}", exc_info=True)

                # Periodic producer watchdog
                time_since_last = time.monotonic() - self._last_ts.get(cam_id, 0.0)
                if producer.is_alive() and time_since_last > _WATCHDOG_THRESHOLD:
                    logger.warning(f"[Watchdog] Camera '{cam_id}' has not provided a new frame for {time_since_last:.1f}s. "
                                   "Restarting producer...")
                    self._last_ts[cam_id] = time.monotonic()
                    try:
                        def restart_producer(p, cid):
                            logger.info(f"[Watchdog] Restarting producer for {cid}...")
                            p.stop()
                            time.sleep(2)
                            p.start()
                            logger.info(f"[Watchdog] Producer for {cid} restarted.")
                        
                        threading.Thread(
                            target=restart_producer,
                            args=(producer, cam_id),
                            daemon=True
                        ).start()
                    except Exception as re:
                        logger.error(f"[Watchdog] Failed to trigger restart for {cam_id}: {re}")

            if not processed_any:
                time.sleep(_IDLE_SLEEP)

        logger.info("[Consumer] Unified inference loop stopped.")

    def _cleanup_resources(self):
        
        if self._enable_preview:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

        for cam_id, producer in self._producers.items():
            try:
                producer.stop()
            except Exception as e:
                logger.warning(f"Error stopping producer {cam_id}: {e}")
        self._producers.clear()

        if self._inference_engine:
            try:
                if self._inference_engine.is_running:
                    self._inference_engine.stop()
            except Exception as e:
                logger.warning(f"Error stopping inference engine: {e}")

        # self._tracker_manager = None # Don't clear it here, keep it for next start
        self._last_ts.clear()

    def _extract_camera_id_from_pad(self, pad):
        return "unknown_camera"