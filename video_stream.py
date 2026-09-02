import cv2
import numpy as np
import threading
import time
import logging
import json
import base64

from config import JPEG_QUALITY
from logging_config import get_logger
from camera_persistence import load_all_cameras

logger = get_logger(__name__)

class VideoStreamManager:
    def __init__(self, frame_buffers, user_data, clean_buffers=None, mqtt_client=None, pi_id="pi-default", pipeline_manager=None):
        self.logger = logging.getLogger(__name__)
        self.frame_buffers = frame_buffers
        self.clean_buffers = clean_buffers if clean_buffers is not None else {}
        self.user_data = user_data
        self.pipeline_manager = pipeline_manager
        
        self.mqtt_client = mqtt_client
        self.pi_id = pi_id
        
        self._pusher_thread = None
        self._pusher_stop_event = threading.Event()

        self._last_valid_snapshot = {}
        self._last_snapshot_time = {}
        # In-memory mappings to help resolve frontend numeric IDs to runtime names
        # mapping keys/values are strings
        self.id_to_name = {}
        self.name_to_id = {}

    def start_snapshot_pusher(self, interval=2.0):
        if self._pusher_thread is not None and self._pusher_thread.is_alive():
            self.logger.warning("Snapshot pusher is already running.")
            return

        self._pusher_stop_event.clear()
        self._pusher_thread = threading.Thread(
            target=self._run_snapshot_pusher,
            args=(interval,),
            daemon=True
        )
        self._pusher_thread.start()
        self.logger.info(f"Started MQTT snapshot pusher with a {interval}s interval.")

    def stop_snapshot_pusher(self):
        if self._pusher_thread and self._pusher_thread.is_alive():
            self._pusher_stop_event.set()
            self._pusher_thread.join()
            self.logger.info("Stopped MQTT snapshot pusher.")
    
    def _run_snapshot_pusher(self, interval):
        if not self.mqtt_client:
            self.logger.error("MQTT client not provided. Cannot start snapshot pusher.")
            return
            
        while not self._pusher_stop_event.is_set():
            camera_id = self.user_data.active_camera
            
            if self.is_camera_available(camera_id):
                success, image_bytes = self.get_snapshot(camera_id, skip_blank=True)
                if success:
                    try:
                        b64 = base64.b64encode(image_bytes).decode('ascii')
                        payload = {
                            "camera_id": camera_id,
                            "image": b64,
                            "timestamp": int(time.time()),
                            "filename": f"snapshot_{camera_id}_{int(time.time())}.jpg"
                        }
                        topic = f"vision/{self.pi_id}/{camera_id}/snapshot"
                        self.mqtt_client.publish(topic, json.dumps(payload), qos=0)
                    except Exception as e:
                        self.logger.error(f"Failed to publish snapshot for {camera_id}: {e}")
            
            time.sleep(interval)

    def handle_snapshot_request(self, camera_id: str):
        if not self.mqtt_client:
            self.logger.error("Cannot handle snapshot request: MQTT client not available.")
            return
        success, image_bytes = self.get_snapshot(camera_id, skip_blank=False)

        if success:
            try:
                b64 = base64.b64encode(image_bytes).decode('ascii')
                payload = {
                    "camera_id": camera_id,
                    "image": b64,
                    "timestamp": int(time.time()),
                    "filename": f"snapshot_{camera_id}_{int(time.time())}.jpg"
                }
                topic = f"vision/{self.pi_id}/{camera_id}/snapshot"
                self.mqtt_client.publish(topic, json.dumps(payload), qos=0)
                self.logger.info(f"Snapshot response sent for camera {camera_id}")
            except Exception as e:
                self.logger.error(f"Failed to publish snapshot response for {camera_id}: {e}")
        else:
            self.logger.error(f"Snapshot request failed for {camera_id}: {image_bytes}")
            error_payload = {
                "camera_id": camera_id,
                "error": str(image_bytes),
                "timestamp": int(time.time())
            }
            error_topic = f"vision/{self.pi_id}/{camera_id}/snapshot/error"
            self.mqtt_client.publish(error_topic, json.dumps(error_payload), qos=0)

    def get_snapshot(self, camera_id, skip_blank=False, clean=False):
        
        mapped_camera = camera_id
        if camera_id not in self.user_data.data:
            if camera_id in self.frame_buffers:
                self.logger.debug(f"Camera '{camera_id}' missing from user_data but present in frame_buffers; proceeding")
            else:
                # Try numeric index fallbacks
                if isinstance(camera_id, str) and camera_id.isdigit():
                    try:
                        idx = int(camera_id)
                        candidates = [f"camera{idx+1}", f"camera{idx}"]
                        for c in candidates:
                            if c in self.user_data.data or c in self.frame_buffers:
                                mapped_camera = c
                                self.logger.debug(f"Mapped numeric camera id '{camera_id}' -> '{mapped_camera}'")
                                break
                    except Exception:
                        pass

                if mapped_camera == camera_id and mapped_camera not in self.frame_buffers:
                    return False, f"Camera {camera_id} not found"

        # Use the (possibly) remapped camera id from here on
        camera_id = mapped_camera

        # If still not present, try persisted camera store to resolve human-friendly names
        if camera_id not in self.user_data.data and camera_id not in self.frame_buffers:
            try:
                persisted = load_all_cameras()
                cam_entry = persisted.get(str(camera_id))
                if cam_entry:
                    name = cam_entry.get('name')
                    if name and name in self.frame_buffers:
                        self.logger.debug(f"Mapped persisted camera id '{camera_id}' -> runtime name '{name}'")
                        camera_id = name
            except Exception:
                pass
        
        # Check in-memory mappings registered at add/remove time
        try:
            mapped = self.id_to_name.get(str(camera_id))
            if mapped and mapped in self.frame_buffers:
                self.logger.debug(f"Mapped in-memory camera id '{camera_id}' -> runtime name '{mapped}'")
                camera_id = mapped
        except Exception:
            pass

        target_buffer = self.clean_buffers if clean else self.frame_buffers
        
        if not hasattr(target_buffer, 'get'):
            # It's a dict
            frame = target_buffer.get(camera_id)
        else:
            # It's a proper buffer object
            frame = target_buffer.get(camera_id)
        
        if frame is None or (isinstance(frame, np.ndarray) and frame.size == 0):
            # Fallback: Try to get raw frame directly from producer if available
            if self.pipeline_manager and hasattr(self.pipeline_manager, '_producers'):
                producer = self.pipeline_manager._producers.get(camera_id)
                if producer:
                    raw_frame, _ = producer.get_latest_frame()
                    if raw_frame is not None:
                        self.logger.debug(f"Using raw fallback frame for camera {camera_id}")
                        frame = raw_frame

            if frame is None:
                if skip_blank:
                    if camera_id in self._last_valid_snapshot:
                        cache_age = time.time() - self._last_snapshot_time.get(camera_id, 0)
                        if cache_age < 30:
                            self.logger.debug(f"Using cached snapshot for camera {camera_id} (age: {cache_age:.1f}s)")
                            return True, self._last_valid_snapshot[camera_id]
                    self.logger.warning(f"No frame available for camera {camera_id} and cached snapshot is stale.")
                    return False, "No recent snapshot available"
                else:
                    frame = self._create_blank_frame(f"No signal from {camera_id}")
        
        try:
            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            image_bytes = buffer.tobytes()

            if frame is not None and (not skip_blank or frame.size > 0):
                self._last_valid_snapshot[camera_id] = image_bytes
                self._last_snapshot_time[camera_id] = time.time()
            return True, image_bytes
        except Exception as e:
            self.logger.error(f"Could not process snapshot for {camera_id}: {e}")
            return False, f"Could not process snapshot: {e}"
    
    def get_video_feed_response(self, camera_id):
        """Return the response for a multipart MJPEG stream."""
        from flask import Response
        return Response(
            self._gen_frames(camera_id),
            mimetype='multipart/x-mixed-replace; boundary=frame'
        )

    def _gen_frames(self, camera_id):
        """Generator function for MJPEG stream."""
        while True:
            success, image_bytes = self.get_snapshot(camera_id, skip_blank=False, clean=False)
            if success:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + image_bytes + b'\r\n')
            else:
                # If fail, send a blank frame with the error
                frame = self._create_blank_frame(f"Stream Error: {image_bytes}")
                _, buffer = cv2.imencode('.jpg', frame)
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
            
            # Limit stream FPS to avoid overwhelming the network
            time.sleep(0.1)  # ~10 FPS

    def _create_blank_frame(self, message):
        blank_frame = np.zeros((640, 640, 3), np.uint8)
        cv2.putText(
            blank_frame, 
            message, 
            (50, 320), 
            cv2.FONT_HERSHEY_SIMPLEX, 
            0.8, 
            (255, 255, 255), 
            2
        )
        return blank_frame
    
    def is_camera_available(self, camera_id):
        return (camera_id in self.frame_buffers and 
                self.frame_buffers[camera_id] is not None)

    def get_available_cameras(self):
        return [camera_id for camera_id in self.frame_buffers.keys() 
                if self.frame_buffers[camera_id] is not None]
