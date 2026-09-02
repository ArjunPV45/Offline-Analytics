import os
import requests
import logging
import json
import threading
import queue
import time
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

ANALYTICS_URL = os.getenv('PEOPLE_COUNT_API_URL') or os.getenv('ANALYTICS_API_URL', 'https://iot.centelon.com/api/people-count')
TIMEOUT = float(os.getenv('ANALYTICS_API_TIMEOUT', '5.0'))
MAX_QUEUE_SIZE = 1000


INITIAL_RETRY_DELAY = 2.0
MAX_RETRY_DELAY = 30.0
MAX_ATTEMPTS = 5


LOG_DIR = "analytics_logs"
LOG_FILE    = os.path.join(LOG_DIR, "events.jsonl")        
LINE_LOG_FILE = os.path.join(LOG_DIR, "line_events.jsonl")
DEAD_LETTER = os.path.join(LOG_DIR, "dead_letter.jsonl")   

REPLAY_INTERVAL = 120.0

LOG_ROTATION_DAYS = int(os.getenv('ANALYTICS_LOG_ROTATION_DAYS', '3'))
LOG_ROTATION_INTERVAL = LOG_ROTATION_DAYS * 24 * 3600


class AnalyticsWorker:
    def __init__(self):
        self._queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._stop_event = threading.Event()
        self._session = requests.Session()
        self._lock = threading.Lock()  

        os.makedirs(LOG_DIR, exist_ok=True)

        
        self._thread = threading.Thread(target=self._run, name="AnalyticsWorker", daemon=True)
        self._thread.start()

        self._replay_thread = threading.Thread(target=self._replay_loop, name="AnalyticsReplay", daemon=True)
        self._replay_thread.start()

        self._rotation_thread = threading.Thread(target=self._rotation_loop, name="AnalyticsRotation", daemon=True)
        self._rotation_thread.start()

        logger.info(f"Analytics background worker started. Local log: {LOG_FILE}, Line log: {LINE_LOG_FILE}, DLQ: {DEAD_LETTER}, log rotation every {LOG_ROTATION_DAYS} day(s)")

   

    def _run(self):
        while not self._stop_event.is_set():
            try:
                event = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            self._deliver_with_retries(event, source="live")
            self._queue.task_done()

    def _deliver_with_retries(self, event: dict, source: str = "live") -> bool:
        # Strip internal-only fields before sending to the server
        server_payload = {k: v for k, v in event.items() if k != 'source'}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self._stop_event.is_set():
                return False
            try:
                resp = self._session.post(
                    ANALYTICS_URL, json=server_payload,
                    headers={'Content-Type': 'application/json'},
                    timeout=TIMEOUT
                )
                if 200 <= resp.status_code < 300:
                    logger.info(
                        f"Successfully posted analytics event: {event.get('action')} "
                        f"for {event.get('camera_id')}"
                        + (f" [replay from DLQ]" if source == "replay" else "")
                    )
                    self._increment_status_count(event)
                    return True
                else:
                    logger.warning(
                        f"Analytics endpoint returned {resp.status_code} "
                        f"(attempt {attempt}/{MAX_ATTEMPTS})"
                    )
            except Exception as e:
                delay = min(INITIAL_RETRY_DELAY * (2 ** (attempt - 1)), MAX_RETRY_DELAY)
                if attempt < MAX_ATTEMPTS:
                    logger.error(
                        f"Failed to post analytics event: {e}. "
                        f"Retrying in {delay:.1f}s (attempt {attempt})..."
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        f"Failed to post analytics event after {MAX_ATTEMPTS} attempts: {e}. "
                        f"Moving to dead-letter queue."
                    )

        
        self._write_dead_letter(event)
        return False


    def _replay_loop(self):
        
        self._stop_event.wait(REPLAY_INTERVAL)

        while not self._stop_event.is_set():
            try:
                self._attempt_replay()
            except Exception as e:
                logger.error(f"[AnalyticsReplay] Error during replay: {e}")
            self._stop_event.wait(REPLAY_INTERVAL)

    def _attempt_replay(self):
        with self._lock:
            if not os.path.exists(DEAD_LETTER):
                return
            processing_file = DEAD_LETTER + ".processing"
            try:
                os.rename(DEAD_LETTER, processing_file)
            except Exception as e:
                logger.error(f"[AnalyticsReplay] Cannot rename DLQ file: {e}")
                return

        try:
            with open(processing_file, "r") as f:
                lines = [l.strip() for l in f if l.strip()]
        except Exception as e:
            logger.error(f"[AnalyticsReplay] Cannot read DLQ processing file: {e}")
            return

        if not lines:
            try:
                os.remove(processing_file)
            except OSError:
                pass
            return

        logger.info(f"[AnalyticsReplay] Attempting to replay {len(lines)} dead-letter event(s).")
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(f"[AnalyticsReplay] Skipping malformed DLQ entry: {line[:80]}")
                continue

            # _deliver_with_retries already invokes _write_dead_letter on failure, 
            # safely appending back to the main DEAD_LETTER without a lock loop.
            self._deliver_with_retries(event, source="replay")

        try:
            os.remove(processing_file)
            logger.info("[AnalyticsReplay] Replay pass completed.")
        except Exception as e:
            logger.error(f"[AnalyticsReplay] Failed to clean up processing file: {e}")

   

    def _rotation_loop(self):
        self._stop_event.wait(LOG_ROTATION_INTERVAL)
        while not self._stop_event.is_set():
            try:
                for target_file in [LOG_FILE, LINE_LOG_FILE]:
                    if os.path.exists(target_file):
                        os.remove(target_file)
                        logger.info(
                            f"[AnalyticsRotation] Deleted {target_file} "
                            f"(scheduled {LOG_ROTATION_DAYS}-day rotation)."
                        )
            except Exception as e:
                logger.error(f"[AnalyticsRotation] Failed to rotate log file: {e}")
            self._stop_event.wait(LOG_ROTATION_INTERVAL)

    

    def _write_dead_letter(self, event: dict):
        with self._lock:
            try:
                with open(DEAD_LETTER, "a") as f:
                    f.write(json.dumps(event) + "\n")
                logger.warning(
                    f"Event written to dead-letter queue: "
                    f"{event.get('action')} for {event.get('camera_id')}"
                )
            except Exception as e:
                logger.error(f"Failed to write dead-letter event: {e}")

    def _log_locally(self, event: dict):
        target_file = LINE_LOG_FILE if event.get('source') == 'line' else LOG_FILE
        try:
            with open(target_file, "a") as f:
                f.write(json.dumps(event) + "\n")
        except Exception as e:
            logger.error(f"Failed to log analytics event locally to {target_file}: {e}")

    def _increment_status_count(self, event: dict):
        try:
            from pi_status_monitor import get_status_monitor
            pi_id = event.get('pi_id', os.getenv('PI_UNIQUE_ID', 'pi-default'))
            get_status_monitor(pi_id).increment_event_count()
        except Exception:
            pass

   

    def enqueue(self, event: dict) -> bool:
        self._log_locally(event)
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            logger.warning("Analytics queue is full. Dropping oldest event to make room.")
            try:
                self._queue.get_nowait()  
                self._queue.put_nowait(event)
                return True
            except Exception:
                return False

    def stop(self):
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        self._replay_thread.join(timeout=2.0)
        self._rotation_thread.join(timeout=2.0)
        self._session.close()



_worker = AnalyticsWorker()


def post_event(event: dict) -> bool:
    return _worker.enqueue(event)


def stop_worker():
    _worker.stop()
