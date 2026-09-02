import os
import requests
import logging
import time
import threading
import json
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

STATUS_API_URL = os.getenv('CAMERA_STATUS_API_URL', 'https://iot.centelon.com/api/camera-status')
INTERVAL = 300.0 # 5 minutes
TIMEOUT = 5.0

class CameraStatusPoster:
    def __init__(self, pipeline_manager, pi_id):
        self.pipeline_manager = pipeline_manager
        self.pi_id = pi_id
        self._stop_event = threading.Event()
        self._trigger_event = threading.Event()
        self._thread = None
        self._session = None
        self.logger = logging.getLogger(f"{__name__}.CameraStatusPoster")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._session = requests.Session()
        self._thread = threading.Thread(target=self._run, name="StatusPosterThread", daemon=True)
        self._thread.start()
        self.logger.info(f"Camera status poster started (interval: {INTERVAL}s, target: {STATUS_API_URL})")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        
        if self._session:
            self._session.close()
            self._session = None
            
        self.logger.info("Camera status poster stopped")

    def post_statuses_now(self):
        """Manually trigger an immediate post of the camera statuses."""
        self._trigger_event.set()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._post_statuses()
            except Exception as e:
                self.logger.error(f"Error in status poster loop: {e}")
            
            # Wait for either INTERVAL seconds or until manually triggered
            triggered = self._trigger_event.wait(INTERVAL)
            if triggered:
                self._trigger_event.clear()
                self.logger.info("Manual status post triggered.")

    def _post_statuses(self):
        if not self.pipeline_manager.is_running():
            return

        statuses = []
        with self.pipeline_manager.user_data.lock:
            camera_names = getattr(self.pipeline_manager, 'camera_names', [])
            
        producers = getattr(self.pipeline_manager, '_producers', {})
        
        for cam_id in camera_names:
            producer = producers.get(cam_id)
            is_connected = False
            if producer:
                _, last_ts = producer.get_latest_frame()
                if last_ts > 0 and (time.monotonic() - last_ts) < 30:
                    is_connected = True
            
            statuses.append({
                "camera_id": cam_id,
                "status": "Connected" if is_connected else "Disconnected",
                "timestamp": int(time.time())
            })

        if not statuses:
            return

        payload = {
            "pi_id": self.pi_id,
            "cameras": statuses,
            "timestamp": int(time.time())
        }

        max_retries = 3
        retry_delay = 5.0
        for attempt in range(max_retries):
            try:
                if not self._session:
                    self._session = requests.Session()
                    
                self.logger.debug(f"Posting camera statuses: {payload} (attempt {attempt + 1})")
                resp = self._session.post(STATUS_API_URL, json=payload, timeout=TIMEOUT)
                if 200 <= resp.status_code < 300:
                    self.logger.info(f"Successfully posted status for {len(statuses)} cameras to {STATUS_API_URL}")
                    return 
                else:
                    self.logger.error(f"Failed to post status. Status code: {resp.status_code}, Response: {resp.text}")
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
            except Exception as e:
                self.logger.error(f"Failed to post camera statuses to {STATUS_API_URL}: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
