
import threading
import time
import logging
import numpy as np
import cv2
from typing import Optional, Tuple

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

from logging_config import get_logger



logger = get_logger(__name__)


def _build_decode_pipeline(rtsp_url: str, camera_id: str) -> Tuple[str, str]:
    
    sink_name = f"frame_sink_{camera_id}"
    pipeline_str = (
        f"uridecodebin uri={rtsp_url} ! "
        f"videoconvert ! "
        f"videoscale ! "
        f"video/x-raw,format=BGR,width=640,height=640 ! "
        f"appsink name={sink_name} max-buffers=1 drop=true sync=false emit-signals=true"
    )
    return pipeline_str, sink_name


class FrameProducer:

    def __init__(self, camera_id: str, rtsp_url: str, reconnect_delay: float = 3.0):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.reconnect_delay = reconnect_delay

        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._timestamp: float = 0.0

        self._pipeline = None
        self._appsink = None
        self._bus_watch_id = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

    

    def start(self):
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"producer-{self.camera_id}",
            daemon=True
        )
        self._thread.start()
        logger.info(f"[Producer] Started for camera '{self.camera_id}'")

    def stop(self):
        self._stop_event.set()
        self._teardown_pipeline()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._running = False
        logger.info(f"[Producer] Stopped for camera '{self.camera_id}'")

    def get_latest_frame(self) -> Tuple[Optional[np.ndarray], float]:
        """Return (frame_bgr, timestamp). frame is None if no frame yet."""
        with self._lock:
            return (self._frame.copy() if self._frame is not None else None,
                    self._timestamp)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    

    def _run_loop(self):
        """Outer loop: build pipeline, run, restart on failure."""
        self._running = True
        while not self._stop_event.is_set():
            try:
                self._build_and_run_pipeline()
            except Exception as e:
                logger.error(f"[Producer:{self.camera_id}] Pipeline execution error: {e}", exc_info=True)
            
            if self._stop_event.is_set():
                break

            logger.info(f"[Producer:{self.camera_id}] Reconnecting in {self.reconnect_delay}s...")
            
            self._stop_event.wait(self.reconnect_delay)
        self._running = False

    def _build_and_run_pipeline(self):
        pipeline_str, sink_name = _build_decode_pipeline(self.rtsp_url, self.camera_id)
        logger.info(f"[Producer:{self.camera_id}] Building pipeline with uridecodebin")

        try:
            self._pipeline = Gst.parse_launch(pipeline_str)
        except Exception as e:
            logger.error(f"[Producer:{self.camera_id}] Failed to parse pipeline: {e}")
            return

        try:
            self._appsink = self._pipeline.get_by_name(sink_name)
            if not self._appsink:
                logger.error(f"[Producer:{self.camera_id}] appsink '{sink_name}' not found")
                return

            
            logger.info(f"[Producer:{self.camera_id}] Setting state to PLAYING...")
            ret = self._pipeline.set_state(Gst.State.PLAYING)
            
            if ret == Gst.StateChangeReturn.FAILURE:
                
                bus = self._pipeline.get_bus()
                msg = bus.timed_pop_filtered(2 * Gst.SECOND, Gst.MessageType.ERROR)
                if msg:
                    err, debug = msg.parse_error()
                    logger.error(f"[Producer:{self.camera_id}] GStreamer Error during set_state: {err}")
                    logger.error(f"[Producer:{self.camera_id}] Debug Info: {debug}")
                else:
                    logger.error(f"[Producer:{self.camera_id}] Pipeline failed to enter PLAYING state (no bus msg)")
                return

            
            ret, current, pending = self._pipeline.get_state(10 * Gst.SECOND)
            
            if ret == Gst.StateChangeReturn.FAILURE:
                bus = self._pipeline.get_bus()
                msg = bus.timed_pop_filtered(Gst.SECOND, Gst.MessageType.ERROR)
                if msg:
                    err, debug = msg.parse_error()
                    logger.error(f"[Producer:{self.camera_id}] GStreamer Error during get_state: {err}")
                    logger.error(f"[Producer:{self.camera_id}] Debug Info: {debug}")
                else:
                    logger.error(f"[Producer:{self.camera_id}] Pipeline failed to transition (current={current}, pending={pending})")
                return
            
            
            logger.info(f"[Producer:{self.camera_id}] Pipeline in stable state: {current} (ret={ret})")

            bus = self._pipeline.get_bus()
            while not self._stop_event.is_set() and self._pipeline:
                
                sample = self._appsink.emit('pull-sample')
                if sample:
                    self._on_new_sample_from_sample(sample)

                
                msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if msg:
                    if msg.type == Gst.MessageType.ERROR:
                        err, debug = msg.parse_error()
                        logger.error(f"[Producer:{self.camera_id}] Runtime GStreamer error: {err} | {debug}")
                        break
                    elif msg.type == Gst.MessageType.EOS:
                        logger.warning(f"[Producer:{self.camera_id}] EOS received")
                        break

                
                if not self._pipeline:
                    break
                ret, state, _ = self._pipeline.get_state(timeout=100 * Gst.MSECOND)
                if ret == Gst.StateChangeReturn.FAILURE:
                    logger.warning(f"[Producer:{self.camera_id}] Pipeline state reported FAILURE")
                    break
                
                time.sleep(0.01) 
        finally:
            self._teardown_pipeline()

    def _on_new_sample_from_sample(self, sample):
        buf = sample.get_buffer()
        caps = sample.get_caps()
        structure = caps.get_structure(0)
        width = structure.get_value('width')
        height = structure.get_value('height')

        success, map_info = buf.map(Gst.MapFlags.READ)
        if not success:
            return
        try:
            frame = np.ndarray(
                shape=(height, width, 3),
                dtype=np.uint8,
                buffer=map_info.data
            ).copy()
        finally:
            buf.unmap(map_info)

        with self._lock:
            self._frame = frame
            self._timestamp = time.monotonic()



    def _teardown_pipeline(self):
        if self._pipeline:
            try:
                pipeline = self._pipeline
                self._pipeline = None
                self._appsink = None
                
                logger.info(f"[Producer:{self.camera_id}] Tearing down pipeline (non-blocking)...")
                
                
                def do_teardown():
                    try:
                        
                        pipeline.set_state(Gst.State.NULL)
                        
                        pipeline.get_state(2 * Gst.SECOND)
                        logger.info(f"[Producer:{self.camera_id}] Pipeline teardown complete.")
                    except Exception as te:
                        logger.error(f"[Producer:{self.camera_id}] Error in async teardown: {te}")
                
                
                t = threading.Thread(target=do_teardown, name=f"teardown-{self.camera_id}", daemon=True)
                t.start()
                
            except Exception as e:
                logger.error(f"[Producer:{self.camera_id}] Error initiating teardown: {e}")
            finally:
                self._pipeline = None
                self._appsink = None
