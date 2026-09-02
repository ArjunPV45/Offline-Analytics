import logging
import threading
import time
import os
import json
from datetime import datetime, timezone
from typing import Optional
from urllib import request, error

logger = logging.getLogger(__name__)

class PiStatusMonitor:
	def __init__(self, pi_id: str, heartbeat_interval: float = 30.0):
		self.pi_id = pi_id
		self.heartbeat_interval = heartbeat_interval

		self.pipeline_running = False
		self.cameras_active = []
		self.events_processed = 0
		self.last_frame_time = None
		self.error_count = 0
		self.warning_count = 0

		self.stop_event = threading.Event()
		self.monitor_thread = None
		self.running = False

		self.status_file = os.path.join(os.path.dirname(__file__), 'pi_status.json')

		
		self.remote_url = os.getenv('PI_STATUS_API_URL', '').strip() or None

		logger.info(f"PiStatusMonitor initialized for {pi_id} (interval: {heartbeat_interval}s)")

	def start(self):
		if self.running:
			logger.warning("PiStatusMonitor already running")
			return

		self.running = True
		self.stop_event.clear()

		self.monitor_thread = threading.Thread(
			target=self._monitor_loop,
			name="PiStatusMonitor",
			daemon=True
		)
		self.monitor_thread.start()
		logger.info("✓ PiStatusMonitor started")

	def stop(self):
		if not self.running:
			return

		logger.info("Stopping PiStatusMonitor...")
		self.stop_event.set()

		if self.monitor_thread:
			self.monitor_thread.join(timeout=5.0)

		self._write_status(is_shutting_down=True)

		self.running = False
		logger.info("✓ PiStatusMonitor stopped")

	def set_pipeline_status(self, running: bool, cameras: list = None):
		self.pipeline_running = running
		if cameras:
			self.cameras_active = cameras
		elif not running:
			self.cameras_active = []

		logger.debug(f"Pipeline status updated: running={running}, cameras={len(self.cameras_active)}")

	def increment_event_count(self, count: int = 1):
		self.events_processed += count

	def update_frame_time(self):
		self.last_frame_time = datetime.now(timezone.utc)

	def increment_error_count(self):
		self.error_count += 1

	def increment_warning_count(self):
		self.warning_count += 1

	def _monitor_loop(self):
		logger.info("PiStatusMonitor loop started")

		while not self.stop_event.is_set():
			try:
				self._write_status()
				self.stop_event.wait(self.heartbeat_interval)
			except Exception as e:
				logger.error(f"Error in PiStatusMonitor loop: {e}", exc_info=True)
				time.sleep(5.0)

		logger.info("PiStatusMonitor loop stopped")

	def _write_status(self, is_shutting_down: bool = False):
		"""Write current status to a local JSON file and optionally POST to remote API."""
		try:
			health_score = self._calculate_health_score()

			status_doc = {
				"pi_id": self.pi_id,
				"timestamp": datetime.now(timezone.utc).isoformat(),
				"status": "offline" if is_shutting_down else ("online" if self.pipeline_running else "idle"),
				"pipeline_running": False if is_shutting_down else self.pipeline_running,
				"cameras_active": [] if is_shutting_down else self.cameras_active,
				"camera_count": 0 if is_shutting_down else len(self.cameras_active),
				"events_processed_total": self.events_processed,
				"last_frame_time": (self.last_frame_time.isoformat() if self.last_frame_time else None),
				"error_count": self.error_count,
				"warning_count": self.warning_count,
				"health_score": 0 if is_shutting_down else health_score,
				"uptime_seconds": self._get_uptime()
			}

			try:
				with open(self.status_file, 'w') as f:
					json.dump(status_doc, f, indent=2)
				logger.debug(f"Wrote pi status to {self.status_file}")
			except Exception as e:
				logger.warning(f"Failed to write local pi status file: {e}")

			
			if self.remote_url:
				try:
					data = json.dumps(status_doc).encode('utf-8')
					req = request.Request(self.remote_url, data=data, headers={'Content-Type': 'application/json'}, method='POST')
					with request.urlopen(req, timeout=5) as resp:
						logger.debug(f"Posted status to remote endpoint, response: {resp.status}")
				except error.HTTPError as he:
					logger.warning(f"Remote status post failed: {he.code} {he.reason}")
				except Exception as e:
					logger.warning(f"Failed to post status to remote endpoint: {e}")

			logger.debug(f"Status prepared: {status_doc['status']}, cameras: {status_doc['camera_count']}, "
						 f"events: {status_doc['events_processed_total']}, health: {health_score}")

		except Exception as e:
			logger.error(f"Failed to prepare Pi status: {e}")

	def _calculate_health_score(self) -> int:
		if not self.pipeline_running:
			return 50

		score = 100
		if self.error_count > 0:
			score -= min(self.error_count * 5, 30)
		if self.warning_count > 0:
			score -= min(self.warning_count * 2, 20)
		if self.last_frame_time:
			seconds_since_frame = (datetime.now(timezone.utc) - self.last_frame_time).total_seconds()
			if seconds_since_frame > 60:
				score -= 20
		return max(0, score)

	def _get_uptime(self) -> Optional[float]:
		try:
			with open('/proc/uptime', 'r') as f:
				uptime_seconds = float(f.readline().split()[0])
				return uptime_seconds
		except Exception:
			return None

_status_monitor_instance = None

def get_status_monitor(pi_id: str, heartbeat_interval: float = 30.0) -> PiStatusMonitor:
	global _status_monitor_instance
	if _status_monitor_instance is None:
		_status_monitor_instance = PiStatusMonitor(pi_id, heartbeat_interval)
	return _status_monitor_instance


