"""MQTT-in, HTTP-out integration between this offline batch pipeline and the
frontend platform — the offline-processing counterpart to a live app's MQTT
control channel (see ../GPUvarient-main/main.py, ../GPUvarient-main/MQTT_COMMANDS.md
for the live conventions this mirrors).

Requests come in over MQTT (lightweight, broker-mediated commands); this
Pi's *responses* go out as HTTP POSTs to the platform's own API, not MQTT
publishes — deliberately, both because a response can carry a sizeable
base64-encoded image (snapshot) that doesn't belong on a message broker, and
because that's what was asked for.

Currently implements:
  - channels_request (MQTT in) -> channels response (HTTP POST): the
    frontend asks what's available to process, this Pi answers with
    channels + their day folders (not individual files — see
    channel_discovery.py). Offline equivalent of the live app's
    push_cameras/push_cameras_response — no dynamic camera_add/camera_remove
    here, channels are fixed by what's actually on disk.
  - snapshot_request (MQTT in, per channel) -> snapshot response (HTTP
    POST): sends a reference frame from that channel's saved footage,
    **with any already-configured zones/lines drawn on it** (yellow
    rectangles, cyan lines — see zone_overlay_render.py) so whoever's
    looking at it sees current state, not a blank frame. Identified by
    channel *name* in the topic (e.g. "ch01"), not a numeric camera_id --
    camera_id isn't guaranteed to exist for every channel (see
    channel_camera_ids.json), channel name always does.
  - zone_config / line_config (MQTT in, per channel) -> ack (HTTP POST):
    saves platform-drawn zone/line coordinates to
    batch_analytics/zone_configs/<channel>.json (same file run_batch_analytics.py
    already reads and draw_zone.py already writes locally). Accepts the same
    flexible coordinate shapes the live system's zone_manager.py does (see
    zone_payload_normalize.py) in case the same frontend component is
    reused. A zone_config message replaces all zones for that channel (not
    a merge) but leaves that channel's lines untouched, and vice versa for
    line_config — matching the live system's replace-not-append convention.

Per-day event pushing (batch_pipeline.py pushes each day's report once
processing finishes, via day_result_push.py) is implemented separately, not
in this module — see day_result_push.py's docstring for why it lives there.

Configuration is loaded from .env (see .env.example) plus an optional
channel_camera_ids.json mapping channel folder names to the numeric
camera_id the frontend already expects (see channel_camera_ids.example.json)
— channels without an entry there still work, just with camera_id: null.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv

from hailo_apps.python.core.common.hailo_logger import get_logger

from batch_analytics.channel_discovery import discover_available_channels
from batch_analytics.reference_frame import ReferenceFrameError, find_reference_frame
from batch_analytics.zone_config_io import default_zone_config_path, load_zone_config, save_zone_config
from batch_analytics.zone_overlay_render import draw_zones_on_frame
from batch_analytics.zone_payload_normalize import normalize_line, normalize_zone

logger = get_logger(__name__)
load_dotenv()

DEFAULT_VIDEOS_ROOT = os.getenv("VIDEOS_ROOT", "/home/hailopi/Analytics/Videos")
# Matches run_batch_analytics.py's own default --output-dir, resolved the
# same way (relative to wherever this is run from -- normally
# UrbanRAIN_COUNTER-main/). Must point at the same place run_batch_analytics.py
# actually writes reports to, or "already processed" filtering silently does
# nothing.
DEFAULT_REPORTS_DIR = os.getenv("BATCH_REPORTS_DIR", "batch_reports")
CAMERA_ID_MAP_PATH = Path(__file__).parent / "channel_camera_ids.json"

# Matches draw_zone.py's defaults -- a snapshot for a channel with no zone
# config yet gets resized to this, so it's the same resolution the platform
# will see once zones exist (drawn at whatever resolution *that* snapshot
# was), rather than an inconsistent "whatever this segment's native size is."
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720

SNAPSHOT_JPEG_QUALITY = 80


def load_camera_id_map(path: Path = CAMERA_ID_MAP_PATH) -> dict[str, int]:
    if not path.exists():
        logger.warning(
            "No camera ID map at %s -- responses will report camera_id: null "
            "for every channel until one is added (see channel_camera_ids.example.json)",
            path,
        )
        return {}
    with open(path) as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def build_channels_response(
    pi_id: str,
    videos_root: str | Path,
    camera_id_map: dict[str, int],
    reports_dir: str | Path = DEFAULT_REPORTS_DIR,
) -> dict[str, Any]:
    return {
        "pi_id": pi_id,
        "channels": discover_available_channels(videos_root, camera_id_map, reports_dir),
        "generated_at": _now_iso(),
    }


def build_snapshot_response(
    pi_id: str,
    channel: str,
    videos_root: str | Path,
    camera_id_map: dict[str, int],
    date: str | None = None,
    segment_index: int = 0,
) -> dict[str, Any]:
    """Raises ReferenceFrameError if the requested channel/date has no
    footage to grab a frame from."""
    frame, segment_name, resolved_date = find_reference_frame(channel, videos_root, date, segment_index)

    zones, lines = [], []
    frame_width, frame_height = DEFAULT_WIDTH, DEFAULT_HEIGHT
    zone_config_path = default_zone_config_path(channel)
    if zone_config_path.exists():
        zones, lines, frame_width, frame_height = load_zone_config(zone_config_path)

    frame = cv2.resize(frame, (frame_width, frame_height), interpolation=cv2.INTER_AREA)
    annotated = draw_zones_on_frame(frame, zones, lines)

    ok, jpeg = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), SNAPSHOT_JPEG_QUALITY])
    if not ok:
        raise ReferenceFrameError(f"failed to JPEG-encode snapshot for channel '{channel}'")

    return {
        "pi_id": pi_id,
        "channel": channel,
        "camera_id": camera_id_map.get(channel),
        "date": resolved_date,
        "segment": segment_name,
        "width": frame_width,
        "height": frame_height,
        "zone_count": len(zones),
        "line_count": len(lines),
        "image": base64.b64encode(jpeg.tobytes()).decode("ascii"),
        "generated_at": _now_iso(),
    }


def _resolve_config_resolution(config_path: Path, payload: dict[str, Any]) -> tuple[int, int]:
    """A channel's saved config has one width/height shared by both zones
    and lines. If a config already exists, its stored resolution wins
    (mismatched-resolution zone/line data in one file would silently
    misalign whichever shape wasn't just updated) — the payload's
    width/height is only used to establish it for a channel's first-ever
    zone/line. Logs a warning if the payload disagrees with an existing
    stored resolution, rather than silently overriding it.
    """
    if not config_path.exists():
        return int(payload.get("width", DEFAULT_WIDTH)), int(payload.get("height", DEFAULT_HEIGHT))

    _, _, existing_width, existing_height = load_zone_config(config_path)
    payload_width, payload_height = payload.get("width"), payload.get("height")
    if payload_width and payload_height and (int(payload_width), int(payload_height)) != (existing_width, existing_height):
        logger.warning(
            "%s already stored at %dx%d -- ignoring payload's %sx%s to avoid misaligning existing data",
            config_path, existing_width, existing_height, payload_width, payload_height,
        )
    return existing_width, existing_height


def apply_zone_config(channel: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Replaces all zones for `channel` with what's in `payload["zones"]`
    (an empty list clears them, matching the live system's convention).
    Leaves that channel's lines untouched. Returns an ack dict; never
    raises — malformed individual zone entries are skipped (logged), not
    fatal to the whole request.
    """
    raw_zones = payload.get("zones", [])
    config_path = default_zone_config_path(channel)
    width, height = _resolve_config_resolution(config_path, payload)

    zones = [z for z in (normalize_zone(r, i, channel) for i, r in enumerate(raw_zones)) if z is not None]
    _, existing_lines, _, _ = load_zone_config(config_path) if config_path.exists() else ([], [], width, height)

    save_zone_config(config_path, channel, width, height, zones, existing_lines)
    logger.info("Saved %d zone(s) for channel '%s' (%d line(s) unchanged)", len(zones), channel, len(existing_lines))

    return {
        "channel": channel,
        "status": "ok",
        "zone_count": len(zones),
        "rejected_count": len(raw_zones) - len(zones),
        "line_count": len(existing_lines),
    }


def apply_line_config(channel: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Same as apply_zone_config but for lines -- replaces all lines for
    `channel`, leaves zones untouched."""
    raw_lines = payload.get("lines", [])
    config_path = default_zone_config_path(channel)
    width, height = _resolve_config_resolution(config_path, payload)

    lines = [l for l in (normalize_line(r, i, channel) for i, r in enumerate(raw_lines)) if l is not None]
    existing_zones, _, _, _ = load_zone_config(config_path) if config_path.exists() else ([], [], width, height)

    save_zone_config(config_path, channel, width, height, existing_zones, lines)
    logger.info("Saved %d line(s) for channel '%s' (%d zone(s) unchanged)", len(lines), channel, len(existing_zones))

    return {
        "channel": channel,
        "status": "ok",
        "line_count": len(lines),
        "rejected_count": len(raw_lines) - len(lines),
        "zone_count": len(existing_zones),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class PlatformController:
    def __init__(
        self,
        pi_id: str | None = None,
        videos_root: str | Path = DEFAULT_VIDEOS_ROOT,
        reports_dir: str | Path = DEFAULT_REPORTS_DIR,
        broker_url: str | None = None,
        broker_port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        channels_api_url: str | None = None,
        snapshot_api_url: str | None = None,
        zone_line_config_api_url: str | None = None,
    ):
        self.pi_id = pi_id or os.getenv("PI_UNIQUE_ID")
        self.videos_root = videos_root
        self.reports_dir = reports_dir
        self.broker_url = broker_url or os.getenv("MQTT_BROKER_URL")
        self.broker_port = int(broker_port or os.getenv("MQTT_BROKER_PORT", 8883))
        self.username = username or os.getenv("MQTT_USERNAME")
        self.password = password or os.getenv("MQTT_PASSWORD")
        self.channels_api_url = channels_api_url or os.getenv("CHANNELS_API_URL")
        self.snapshot_api_url = snapshot_api_url or os.getenv("SNAPSHOT_API_URL")
        self.zone_line_config_api_url = zone_line_config_api_url or os.getenv("ZONE_LINE_CONFIG_API_URL")
        self.camera_id_map = load_camera_id_map()

        missing = [
            name for name, value in [
                ("PI_UNIQUE_ID", self.pi_id),
                ("MQTT_BROKER_URL", self.broker_url),
                ("MQTT_USERNAME", self.username),
                ("MQTT_PASSWORD", self.password),
            ] if not value
        ]
        if missing:
            raise ValueError(
                f"Missing required config: {', '.join(missing)}. "
                f"Copy .env.example to .env and fill these in before connecting."
            )
        for url_attr, env_name, action in [
            ("channels_api_url", "CHANNELS_API_URL", "channels_request"),
            ("snapshot_api_url", "SNAPSHOT_API_URL", "snapshot_request"),
            ("zone_line_config_api_url", "ZONE_LINE_CONFIG_API_URL", "zone_config/line_config"),
        ]:
            if not getattr(self, url_attr):
                logger.warning("%s not set -- %s will be received but the response can't be pushed anywhere", env_name, action)

        self.client = mqtt.Client()
        self.client.username_pw_set(self.username, self.password)
        try:
            self.client.tls_set_context(ssl.create_default_context())
        except Exception:
            self.client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # ------------------------------------------------------------------
    # MQTT plumbing (requests in)
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc, properties=None) -> None:
        if rc != 0:
            logger.error("MQTT connect failed, rc=%s", rc)
            return
        logger.info("Connected to MQTT broker as pi_id=%s", self.pi_id)
        client.subscribe(f"vision/{self.pi_id}/channels_request")
        client.subscribe(f"vision/{self.pi_id}/+/snapshot_request")
        client.subscribe(f"vision/{self.pi_id}/+/zone_config")
        client.subscribe(f"vision/{self.pi_id}/+/line_config")
        logger.info("Subscribed to channels_request and per-channel snapshot_request/zone_config/line_config")

    # action name (last topic segment) -> handler(channel, payload)
    _CHANNEL_ACTION_HANDLERS = {
        "snapshot_request": "_handle_snapshot_request",
        "zone_config": "_handle_zone_config",
        "line_config": "_handle_line_config",
    }

    def _on_message(self, client, userdata, msg) -> None:
        logger.info("MQTT message on %s", msg.topic)
        topic_parts = msg.topic.split("/")

        if msg.topic == f"vision/{self.pi_id}/channels_request":
            self._handle_channels_request()
            return

        if len(topic_parts) == 4 and topic_parts[:2] == ["vision", self.pi_id]:
            channel, action = topic_parts[2], topic_parts[3]
            handler_name = self._CHANNEL_ACTION_HANDLERS.get(action)
            if handler_name:
                request_payload = self._parse_json_payload(msg.payload)
                getattr(self, handler_name)(channel, request_payload)
                return

        logger.warning("No handler for topic: %s", msg.topic)

    @staticmethod
    def _parse_json_payload(raw: bytes) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignoring malformed JSON payload: %r", raw[:200])
            return {}

    # ------------------------------------------------------------------
    # Handlers -> HTTP push out
    # ------------------------------------------------------------------

    def _handle_channels_request(self) -> None:
        if not self.channels_api_url:
            logger.error("channels_request received but CHANNELS_API_URL is not configured -- dropping")
            return
        payload = build_channels_response(self.pi_id, self.videos_root, self.camera_id_map, self.reports_dir)
        self._push_json(self.channels_api_url, payload, "channels response")

    def _handle_snapshot_request(self, channel: str, request_payload: dict[str, Any]) -> None:
        if not self.snapshot_api_url:
            logger.error("snapshot_request for '%s' received but SNAPSHOT_API_URL is not configured -- dropping", channel)
            return

        date = request_payload.get("date")
        segment_index = int(request_payload.get("segment_index", 0))

        try:
            payload = build_snapshot_response(
                self.pi_id, channel, self.videos_root, self.camera_id_map,
                date=date, segment_index=segment_index,
            )
        except ReferenceFrameError as e:
            logger.error("snapshot_request for channel '%s' failed: %s", channel, e)
            return

        self._push_json(self.snapshot_api_url, payload, f"snapshot response ({channel})")

    def _handle_zone_config(self, channel: str, request_payload: dict[str, Any]) -> None:
        ack = apply_zone_config(channel, request_payload)
        self._push_config_ack(channel, "zone_config", ack)

    def _handle_line_config(self, channel: str, request_payload: dict[str, Any]) -> None:
        ack = apply_line_config(channel, request_payload)
        self._push_config_ack(channel, "line_config", ack)

    def _push_config_ack(self, channel: str, action: str, ack: dict[str, Any]) -> None:
        if not self.zone_line_config_api_url:
            logger.error("%s for '%s' saved locally, but ZONE_LINE_CONFIG_API_URL is not configured -- ack can't be pushed", action, channel)
            return
        payload = {"pi_id": self.pi_id, "action": action, "generated_at": _now_iso(), **ack}
        self._push_json(self.zone_line_config_api_url, payload, f"{action} ack ({channel})")

    def _push_json(self, url: str, payload: dict[str, Any], label: str) -> bool:
        try:
            resp = requests.post(url, json=payload, timeout=15.0)
        except requests.RequestException as e:
            logger.error("%s: failed to reach %s: %s", label, url, e)
            return False

        if 200 <= resp.status_code < 300:
            logger.info("%s: pushed to %s (HTTP %s)", label, url, resp.status_code)
            return True

        logger.error("%s: %s returned HTTP %s: %s", label, url, resp.status_code, resp.text[:200])
        return False

    def run_forever(self) -> None:
        logger.info("Connecting to MQTT broker %s:%s ...", self.broker_url, self.broker_port)
        self.client.connect(self.broker_url, self.broker_port, keepalive=60)
        self.client.loop_forever()


def main() -> int:
    try:
        controller = PlatformController()
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    controller.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
