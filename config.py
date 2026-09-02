import json
import os
import logging
from collections import defaultdict

CONFIG_FILE = "cameras_zones.json"
logger = logging.getLogger(__name__)

def load_config(filename=CONFIG_FILE):
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except Exception as e:
            logger.error(f"Error loading config from {filename}: {e}")
            return {}
    return {}

def save_zone_line_config(user_data, filename=CONFIG_FILE):
    existing_config = load_config(filename) or {}
    for camera_id, camera_data in user_data.data.items():
        existing_config[camera_id] = {"zones": {}, "lines": {}}
        for zone_name, zone_data in camera_data.get("zones", {}).items():
            existing_config[camera_id]["zones"][zone_name] = {
                "top_left": zone_data["top_left"],
                "bottom_right": zone_data["bottom_right"]
            }
        for line_name, line_data in camera_data.get("lines", {}).items():
            existing_config[camera_id]["lines"][line_name] = {
                "start": line_data["start"],
                "end": line_data["end"]
            }
    try:
        temp_file = f"{filename}.tmp"
        with open(temp_file, "w") as f:
            json.dump(existing_config, f, indent=4)
        os.replace(temp_file, filename)
        logger.info(f"Saved zone/line configurations to {filename}")
        return True
    except Exception as e:
        logger.error(f"Error saving config to {filename}: {e}")
        return False

def load_zone_line_config(user_data, filename=CONFIG_FILE):
    config = load_config(filename)
    if not config:
        logger.info("No configuration file found, starting with empty config")
        return
    
    with user_data.lock:
        for camera_id, camera_config in config.items():
            if not isinstance(camera_config, dict):
                continue
            if "zones" not in camera_config and "lines" not in camera_config:
                continue
            
            if camera_id not in user_data.data:
                user_data.data[camera_id] = {"zones": {}, "lines": {}}
            
            if not hasattr(user_data, 'inside_zones'):
                user_data.inside_zones = {}
            if not hasattr(user_data, 'person_state_buffer'):
                user_data.person_state_buffer = {}
            if not hasattr(user_data, 'person_dwell_tracker'):
                user_data.person_dwell_tracker = {}
            if not hasattr(user_data, 'line_cross_tracker'):
                user_data.line_cross_tracker = {}
            if not hasattr(user_data, 'line_cooldown_tracker'):
                user_data.line_cooldown_tracker = {}
            if not hasattr(user_data, 'person_zone_history'):
                user_data.person_zone_history = {}
            
            if camera_id not in user_data.inside_zones:
                user_data.inside_zones[camera_id] = {}
            if camera_id not in user_data.person_state_buffer:
                user_data.person_state_buffer[camera_id] = {}
            if camera_id not in user_data.person_dwell_tracker:
                user_data.person_dwell_tracker[camera_id] = {}
            if camera_id not in user_data.line_cross_tracker:
                user_data.line_cross_tracker[camera_id] = {}
            if camera_id not in user_data.line_cooldown_tracker:
                user_data.line_cooldown_tracker[camera_id] = {}
            if camera_id not in user_data.person_zone_history:
                user_data.person_zone_history[camera_id] = {}
            
            for zone_name, zone_config in camera_config.get("zones", {}).items():
                user_data.data[camera_id]["zones"][zone_name] = {
                    "top_left": zone_config["top_left"],
                    "bottom_right": zone_config["bottom_right"],
                    "in_count": 0,
                    "out_count": 0,
                    "inside_ids": [],
                    "history": []
                }
                if zone_name not in user_data.inside_zones[camera_id]:
                    user_data.inside_zones[camera_id][zone_name] = set()
                if zone_name not in user_data.person_state_buffer[camera_id]:
                    user_data.person_state_buffer[camera_id][zone_name] = {}
                if zone_name not in user_data.person_dwell_tracker[camera_id]:
                    user_data.person_dwell_tracker[camera_id][zone_name] = {}
                if zone_name not in user_data.person_zone_history[camera_id]:
                    user_data.person_zone_history[camera_id][zone_name] = {}
            
            for line_name, line_config in camera_config.get("lines", {}).items():
                user_data.data[camera_id]["lines"][line_name] = {
                    "start": line_config["start"],
                    "end": line_config["end"],
                    "in_count": 0,
                    "out_count": 0,
                    "history": []
                }
                if line_name not in user_data.line_cross_tracker[camera_id]:
                    user_data.line_cross_tracker[camera_id][line_name] = {}
                if line_name not in user_data.line_cooldown_tracker[camera_id]:
                    user_data.line_cooldown_tracker[camera_id][line_name] = {}
    
    logger.info("✓ Loaded zone/line configurations. All counts reset to 0.")

def save_active_sources(active_sources, filename=CONFIG_FILE):
    config = load_config(filename) or {}
    config["video_sources"] = active_sources
    try:
        temp_file = f"{filename}.tmp"
        with open(temp_file, "w") as f:
            json.dump(config, f, indent=4)
        os.replace(temp_file, filename)
        return True
    except Exception as e:
        logger.error(f"Error saving active sources: {e}")
        return False

def get_active_sources(filename=CONFIG_FILE):
    config = load_config(filename)
    return config.get("video_sources", [])

MODEL_PATHS = {"yolov8s": "./resources/yolov8s_h8l.hef"}
DEFAULT_FRAME_HEIGHT = 640
DEFAULT_FRAME_WIDTH = 640
DEFAULT_ZONE_CONFIG = {
    "zone1": {
        "top_left": [160, 120],
        "bottom_right": [480, 520],
        "in_count": 0,
        "out_count": 0,
        "inside_ids": [],
        "history": []
    }
}
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 5000
DEBUG_MODE = False
CORS_ALLOWED_ORIGINS = "*"
SOCKETIO_ASYNC_MODE = 'threading'
JPEG_QUALITY = 75
TEMPLATE_FILE = "index3.html"
STABILITY_THRESHOLD = 3
DEBOUNCE_TIME = 2.0
BOUNDARY_BUFFER = 20
POSITION_HISTORY_SIZE = 5
