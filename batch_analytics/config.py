"""Centralized, .env-driven configuration for the offline batch pipeline.

Every script and module that needs a device-specific setting -- where the
footage lives, which Hailo device/model to run inference on, MQTT/HTTP
endpoints -- reads it from here instead of hardcoding its own default or
calling `os.getenv()` again. Setting up a new device should only ever mean
"copy .env.example to .env and fill it in," never "go edit a hardcoded path
or default in some script."

This module is safe to import standalone (it doesn't depend on anything
else in this package) and is imported by run_batch_analytics.py,
run_batch_analytics_all_days.py, run_batch_analytics_parallel.py,
job_manager.py, and platform_integration.py.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- Where this device's footage lives, and where its reports land ---
# Point VIDEOS_ROOT at wherever this device's NFS mount (or symlink) for
# recorded footage actually is -- it's the one thing that's guaranteed to
# differ between deployments.
VIDEOS_ROOT = os.getenv("VIDEOS_ROOT", "/home/hailopi/Analytics/Videos")
BATCH_REPORTS_DIR = os.getenv("BATCH_REPORTS_DIR", "batch_reports")

# --- Hailo device / model selection ---
# Both optional -- omit them entirely to use hailo-apps' own auto-detected
# defaults, same as not passing --arch/--hef-path on the command line. Set
# them once per device (e.g. to point at a model download_resources.sh
# fetched, or a custom-trained HEF) instead of remembering to pass
# --arch/--hef-path on every manual run_batch_analytics.py invocation --
# job_manager.py applies them automatically to every process_request too.
HAILO_ARCH = os.getenv("HAILO_ARCH") or None
HEF_PATH = os.getenv("HEF_PATH") or None

_raw_analysis_fps = os.getenv("DEFAULT_ANALYSIS_FPS")
DEFAULT_ANALYSIS_FPS: float | None = float(_raw_analysis_fps) if _raw_analysis_fps else None


def extra_pipeline_args() -> list[str]:
    """CLI flags to append to any get_pipeline_parser()-based invocation
    (run_batch_analytics.py directly, or job_manager.py's subprocess calls)
    so this device's configured arch/model apply automatically."""
    args: list[str] = []
    if HAILO_ARCH:
        args += ["--arch", HAILO_ARCH]
    if HEF_PATH:
        args += ["--hef-path", HEF_PATH]
    return args


# --- MQTT (requests come IN this way) ---
PI_UNIQUE_ID = os.getenv("PI_UNIQUE_ID")
MQTT_BROKER_URL = os.getenv("MQTT_BROKER_URL")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", 8883))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

# --- HTTP endpoints (responses go OUT this way, not over MQTT) ---
CHANNELS_API_URL = os.getenv("CHANNELS_API_URL")
SNAPSHOT_API_URL = os.getenv("SNAPSHOT_API_URL")
ZONE_LINE_CONFIG_API_URL = os.getenv("ZONE_LINE_CONFIG_API_URL")
PROCESSED_DAY_API_URL = os.getenv("PROCESSED_DAY_API_URL")
PROCESS_STATUS_API_URL = os.getenv("PROCESS_STATUS_API_URL")

CAMERA_ID_MAP_PATH = REPO_ROOT / "batch_analytics" / "channel_camera_ids.json"
