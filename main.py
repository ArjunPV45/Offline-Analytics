import logging
import datetime
import sys
import os
import signal
import threading
import time
import json
import shutil
from pathlib import Path
from dotenv import load_dotenv
import paho.mqtt.client as mqtt
import ssl

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
Gst.init(None)

from logging_config import setup_logging, get_logger
from health_monitor import HealthMonitor

load_dotenv()

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
HAILO_LOG_PATH = LOG_DIR / "hailort.log"
os.environ["HAILORT_LOGGER_PATH"] = str(HAILO_LOG_PATH.absolute())

def rotate_hailo_logs():
    if HAILO_LOG_PATH.exists():
        old_log = HAILO_LOG_PATH.with_suffix(".log.old")
        try:
            if old_log.exists():
                if old_log.is_dir():
                    shutil.rmtree(old_log)
                else:
                    old_log.unlink()
            shutil.move(str(HAILO_LOG_PATH), str(old_log))
            print(f"Rotated HailoRT log to {old_log}")
        except Exception as e:
            print(f"Failed to rotate HailoRT log: {e}")

rotate_hailo_logs()

from config import DEBUG_MODE
from zone_counter import MultiSourceZoneVisitorCounter
from gstreamer_pipeline import PipelineManager
from video_stream import VideoStreamManager
from command_listener import MqttCommandListener

from camera_persistence import load_all_cameras
from pi_status_monitor import get_status_monitor
from status_poster import CameraStatusPoster


components = {}
_shutdown_event = threading.Event()

logger = setup_logging() 

def signal_handler(signum, frame):
    logger.info(f"Received signal {signum}, initiating shutdown...")
    _shutdown_event.set()

def create_mqtt_client():
    logger = logging.getLogger(__name__)
    broker_url = os.getenv("MQTT_BROKER_URL")
    broker_port = int(os.getenv("MQTT_BROKER_PORT", 8883))
    username = os.getenv("MQTT_USERNAME")
    password = os.getenv("MQTT_PASSWORD")

    if not all([broker_url, username, password]):
        logger.error("MQTT credentials not set. MQTT features disabled.")
        return None

    client = mqtt.Client()
    
    client.username_pw_set(username, password)
    try:
        client.tls_set_context(ssl.create_default_context())
    except Exception:
        client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
    
    max_retries = 10
    current_delay = 2.0
    for attempt in range(max_retries):
        try:
            logger.info(f"Connecting to MQTT Broker at {broker_url} (attempt {attempt + 1}/{max_retries})...")
            client.connect(broker_url, broker_port, 60)
            client.loop_start()
            logger.info("Connected to MQTT Broker.")
            return client
        except Exception as e:
            logger.error(f"Failed to connect to MQTT Broker: {e}")
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {current_delay}s...")
                time.sleep(current_delay)
                current_delay = min(current_delay * 1.5, 30.0)
    
    return None

def run_counts_pusher(user_data, mqtt_client, stop_event, interval=2.0):
    logger_pusher = logging.getLogger(f"{__name__}.counts_pusher")
    pi_id = os.getenv("PI_UNIQUE_ID", "pi-default")

    logger_pusher.info("Counts pusher thread started.")

    while not stop_event.is_set():
        try:            
            with user_data.lock:
                all_data = user_data.data
            
            combined_payload = {}
            current_timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
            
            for camera_id, camera_data in all_data.items():
                camera_json_data = {}
                
                for zone_name, zone_data in camera_data.get("zones", {}).items():
                    camera_json_data[zone_name] = {"in": zone_data.get("in_count", 0), "out": zone_data.get("out_count", 0)}
                
                for line_name, line_data in camera_data.get("lines", {}).items():
                    camera_json_data[line_name] = {
                        "in": line_data.get("in_count", 0), 
                        "out": line_data.get("out_count", 0),
                        "pi_id": pi_id,
                        "timestamp": current_timestamp
                    }
                
                if camera_json_data:
                    combined_payload[camera_id] = camera_json_data
            
            if combined_payload:
                try:
                    with open("live_test_counts.json", "w") as f:
                        json.dump(combined_payload, f, indent=4)
                except Exception as file_e:
                    logger_pusher.error(f"Failed to write to live_test_counts.json: {file_e}")
                    
        except Exception as e:
            logger_pusher.error(f"Error in counts pusher thread: {e}")
        time.sleep(interval)
    logger_pusher.info("Counts pusher thread has stopped.")


def main():
    global components
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    
    try:
        logger.info("Starting Pi Vision Processing Backend (Event-Driven MQTT Architecture)")
        logger.info(f"Device ID: {os.getenv('PI_UNIQUE_ID', 'pi-default')}")
        logger.info(f" Debug Mode:{DEBUG_MODE}"
        )
        mqtt_client = create_mqtt_client()
        if not mqtt_client:
            return 1
        
        pi_id = os.getenv("PI_UNIQUE_ID", "pi-002")
        logger.info(f"Using PI_UNIQUE_ID: {pi_id}")
        
        logger.info("Initializing core components...")
        user_data = MultiSourceZoneVisitorCounter(mqtt_client=mqtt_client, pi_id=pi_id)
        
        
        user_data.db_enabled = False
        try:
            status_monitor = get_status_monitor(pi_id, heartbeat_interval=30.0)
            status_monitor.start()
            components['status_monitor'] = status_monitor
            logger.info("PiStatusMonitor started.")
        except Exception as e:
            logger.error(f"Failed to start PiStatusMonitor: {e}")

        frame_buffers = {}
        clean_buffers = {}
        
        pipeline_manager = PipelineManager(user_data, frame_buffers, clean_buffers=clean_buffers)
        video_stream_manager = VideoStreamManager(frame_buffers, user_data, clean_buffers=clean_buffers, mqtt_client=mqtt_client, pi_id=pi_id, pipeline_manager=pipeline_manager)
        
        status_poster = CameraStatusPoster(pipeline_manager, pi_id)
        status_poster.start()
        components['status_poster'] = status_poster
        logger.info("Camera status poster started.")

        mqtt_listener = MqttCommandListener(pipeline_manager, user_data, video_stream_manager, mqtt_client=mqtt_client, pi_id=pi_id, status_poster=status_poster)
        
        health_monitor = HealthMonitor(user_data, pipeline_manager, port=8080)
        health_monitor.start()

        logger.info("Core components initialized")

        user_data.load_zone_line_config()
        logger.info("Loaded zone/line configuration from disk")

        
        try:
            restore_flag = os.getenv('RESTORE_PERSISTED_CAMERAS', 'false').lower() in ('1', 'true', 'yes')
            if restore_flag:
                persisted = load_all_cameras()
                active_cams = [c for c in persisted.values() if c.get('status') == 'active']
                if active_cams:
                    video_sources = [cam.get('rtsp_url') for cam in active_cams if cam.get('rtsp_url')]
                    camera_ids = [str(cam.get('camera_id')) for cam in active_cams if cam.get('rtsp_url')]
                    if video_sources and camera_ids and len(video_sources) == len(camera_ids):
                        started = pipeline_manager.start_pipeline(video_sources, custom_camera_names=camera_ids, on_started_callback=mqtt_listener.wait_and_publish_active_cameras)
                        if started:
                            try:
                                
                                user_data.initialize_sources(camera_ids)
                                
                                
                                for cam in active_cams:
                                    cid = str(cam.get('camera_id'))
                                    if cid in user_data.data:
                                       
                                        for z in cam.get('zones', []):
                                            zname = z.get('name')
                                            tl = z.get('top_left')
                                            br = z.get('bottom_right')
                                            if zname and tl and br:
                                                user_data.create_or_update_zone(cid, zname, tl, br)
                                        
                                        
                                        for l in cam.get('lines', []):
                                            lname = l.get('name')
                                            start = l.get('start')
                                            end = l.get('end')
                                            swap = l.get('swap', False)
                                            if lname and start and end:
                                                user_data.create_or_update_line(cid, lname, start, end, swap)
                                                
                                logger.info(f"Restored and started {len(camera_ids)} persisted cameras and their zones from cameras.json")
                            except Exception as e:
                                logger.warning(f"Failed to initialize user_data for persisted cameras: {e}")
            else:
                logger.info("RESTORE_PERSISTED_CAMERAS not set — starting with no cameras (fresh state)")
        except Exception as e:
            logger.warning(f"Could not restore persisted cameras from local file: {e}")

        components.update({
            'user_data': user_data,
            'pipeline_manager': pipeline_manager,
            'video_stream_manager': video_stream_manager,
            'frame_buffers': frame_buffers,
            'clean_buffers': clean_buffers,
            'mqtt_client': mqtt_client,
            'health_monitor': health_monitor
        })
        
        logger.info("Starting MQTT pusher threads...")

        video_stream_manager.start_snapshot_pusher(interval=0.5)
        
        counts_pusher_stop_event = threading.Event()
        counts_pusher_thread = threading.Thread(
            target=run_counts_pusher,
            args=(user_data, mqtt_client, counts_pusher_stop_event),
            daemon=True
        )
        counts_pusher_thread.start()
        
        components['counts_pusher_stop_event'] = counts_pusher_stop_event
        
        # Camera status is handled by CameraStatusPoster (class-based)
        # No need for manual thread launch here anymore
        
        # Temporarily disabled Centelon API status poster section (removed redundant block)
        # components['status_poster'] = status_poster is already set above


        
        def run_glib_loop():
            loop = GLib.MainLoop()
            while not _shutdown_event.is_set():
               
                context = loop.get_context()
                context.iteration(True) 
        
        glib_thread = threading.Thread(target=run_glib_loop, daemon=True, name="glib-mainloop")
        glib_thread.start()

        logger.info("Core components initialized and running.")
        logger.info("System ready. Waiting for commands via MQTT.")

        _shutdown_event.wait()
    
    except KeyboardInterrupt:
        logger.info("Application interrupted by user (Ctrl+C).")
    
    except Exception as e:
        logger.critical(f"An unhandled exception occurred: {e}", exc_info=True)
        return 1
        
    finally:
        logger.info("--- Starting Graceful Shutdown ---")

        if 'health_monitor' in components:
            components['health_monitor'].stop()

        for key in ['counts_pusher_stop_event']:
            if key in components:
                components[key].set()
                logger.info(f"Stopped {key.replace('_stop_event', '')}")
        
        if 'status_poster' in components:
            try:
                components['status_poster'].stop()
            except Exception as e:
                logger.error(f"Error stopping status_poster: {e}")
        
        if 'video_stream_manager' in components:
            components['video_stream_manager'].stop_snapshot_pusher()
            logger.info("Stopped snapshot pusher")

        if 'db_writer' in components:
            try:
                db_writer = components['db_writer']
                stats = db_writer.get_stats()
                logger.info(f"Database writer stats: {stats}")
                db_writer.stop()
                logger.info("Database writer stopped.")
            except Exception as e:
                logger.error(f"Error stopping database writer: {e}")

        if 'status_monitor' in components:
            try:
                components['status_monitor'].stop()
                logger.info("PiStatusMonitor stopped.")
            except Exception as e:
                logger.error(f"Error stopping PiStatusMonitor: {e}")

        if 'status_poster' in components:
            try:
                components['status_poster'].stop()
                logger.info("Camera status poster stopped.")
            except Exception as e:
                logger.error(f"Error stopping status poster: {e}")

        if 'pipeline_manager' in components:
            components['pipeline_manager'].stop_pipeline()
            logger.info("Stopped GStreamer pipeline")

        
        try:
            from analytics_poster import stop_worker
            stop_worker()
            logger.info("Stopped analytics background worker")
        except Exception:
            pass

        if 'mqtt_client' in components:
            mqtt_client = components['mqtt_client']
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
            logger.info("MQTT client disconnected.")

        

        _shutdown_event.set()
            
        logger.info("--- Shutdown Complete ---")
    
    return 0

if __name__ == "__main__":
    sys.exit(main())