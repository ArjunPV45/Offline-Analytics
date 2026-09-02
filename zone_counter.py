import json
import cv2
import datetime
from datetime import timezone
import numpy as np
import time
import threading
import logging
import os
from typing import Dict, Set, List, Tuple, Any, Optional, Union
from dataclasses import dataclass
from collections import defaultdict
from enum import Enum
from logging_config import get_logger
from analytics_poster import post_event


logging.basicConfig(level=logging.INFO)
logger = get_logger(__name__)


class PersonState(Enum):
    INSIDE = "inside"
    OUTSIDE = "outside"
    ENTERING = "entering"
    EXITING = "exiting"


class ActionType(Enum):
    ENTRY = "Entered"
    EXIT = "Exited"
    IN = "In"
    OUT = "Out"


@dataclass
class CounterConfig:
    frame_height: int = 1080
    frame_width: int = 1920
    zone_padding: int = 30
    min_dwell_time: float = 1.0
    exit_grace_time: float = 1.0
    crossing_cooldown_seconds: float = 2.0
    min_movement_threshold: float = 5.0
    state_confirmation_frames: int = 3
    max_history_entries: int = 1000
    cleanup_interval_minutes: int = 5


class MultiSourceZoneVisitorCounter:
    def __init__(self, mqtt_client=None, pi_id: str = "pi-default", config: Optional[CounterConfig] = None):
        logger.info("Initializing MultiSourceZoneVisitorCounter")
        self.config = config or CounterConfig()
        self.mqtt_client = mqtt_client
        self.pi_id = pi_id

        self.db_enabled = False

        self.lock = threading.RLock()
        self.data: Dict[str, Dict[str, Any]] = {}

        self.inside_zones: Dict[str, Dict[str, Set[int]]] = {}
        self.person_state_buffer: Dict[str, Dict[str, Dict[int, Dict[str, Any]]]] = {}
        self.person_dwell_tracker: Dict[str, Dict[str, Dict[int, Dict[str, Any]]]] = {}
        self.line_cross_tracker: Dict[str, Dict[str, Dict[int, Dict[str, Any]]]] = {}
        self.line_cooldown_tracker: Dict[str, Dict[str, Dict[int, datetime.datetime]]] = {}

        self.person_zone_history: Dict[str, Dict[str, Dict[int, Dict[str, Any]]]] = {}
        self.zone_empty_since: Dict[str, Dict[str, Optional[datetime.datetime]]] = {}

        self.active_camera: str = "camera1"

        self.last_cleanup = datetime.datetime.now()

    def _validate_coordinates(self, top_left: Optional[List[int]] = None,
                              bottom_right: Optional[List[int]] = None,
                              points: Optional[List[List[int]]] = None) -> bool:
        try:
            if points is not None:
                if len(points) < 3:
                    return False
                for p in points:
                    if len(p) != 2:
                        return False
                    if any(coord < 0 for coord in p):
                        return False
                    if p[0] > self.config.frame_width or p[1] > self.config.frame_height:
                        return False
                return True

            if top_left is None or bottom_right is None:
                return False
            if len(top_left) != 2 or len(bottom_right) != 2:
                return False
            if top_left[0] >= bottom_right[0] or top_left[1] >= bottom_right[1]:
                return False
            if any(coord < 0 for coord in top_left + bottom_right):
                return False
            if (bottom_right[0] > self.config.frame_width or
                    bottom_right[1] > self.config.frame_height):
                return False
            return True
        except (TypeError, IndexError):
            return False

    def _validate_line_coordinates(self, start: List[int], end: List[int]) -> bool:
        try:
            if len(start) != 2 or len(end) != 2:
                return False
            if any(coord < 0 for coord in start + end):
                return False
            if (max(start[0], end[0]) > self.config.frame_width or
                    max(start[1], end[1]) > self.config.frame_height):
                return False
            distance = np.sqrt((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2)
            return distance >= 10
        except (TypeError, IndexError):
            return False

    def _validate_person_data(self, person_data: Tuple) -> bool:
        if len(person_data) == 5:
            try:
                person_id, x1, y1, x2, y2 = person_data
                return (isinstance(person_id, (int, str)) and
                        all(isinstance(coord, (int, float)) for coord in [x1, y1, x2, y2]) and
                        x1 < x2 and y1 < y2)
            except (ValueError, TypeError):
                return False
        elif len(person_data) == 3:
            try:
                person_id, x, y = person_data
                return (isinstance(person_id, (int, str)) and
                        isinstance(x, (int, float)) and isinstance(y, (int, float)))
            except (ValueError, TypeError):
                return False
        return False

    def _init_camera(self, camera_id: str) -> None:
        try:
            if camera_id not in self.inside_zones:
                self.inside_zones[camera_id] = defaultdict(set)
            if camera_id not in self.person_state_buffer:
                self.person_state_buffer[camera_id] = defaultdict(dict)
            if camera_id not in self.person_dwell_tracker:
                self.person_dwell_tracker[camera_id] = defaultdict(dict)
            if camera_id not in self.line_cross_tracker:
                self.line_cross_tracker[camera_id] = defaultdict(dict)
            if camera_id not in self.line_cooldown_tracker:
                self.line_cooldown_tracker[camera_id] = defaultdict(dict)
            if camera_id not in self.person_zone_history:
                self.person_zone_history[camera_id] = defaultdict(dict)
            if camera_id not in self.zone_empty_since:
                self.zone_empty_since[camera_id] = defaultdict(lambda: None)

            for zone in self.data[camera_id].get("zones", {}):
                if zone not in self.inside_zones[camera_id]:
                    self.inside_zones[camera_id][zone] = set()
                if zone not in self.person_state_buffer[camera_id]:
                    self.person_state_buffer[camera_id][zone] = {}
                if zone not in self.person_dwell_tracker[camera_id]:
                    self.person_dwell_tracker[camera_id][zone] = {}
                if zone not in self.person_zone_history[camera_id]:
                    self.person_zone_history[camera_id][zone] = {}

            for line in self.data[camera_id].get("lines", {}):
                if line not in self.line_cross_tracker[camera_id]:
                    self.line_cross_tracker[camera_id][line] = {}
                if line not in self.line_cooldown_tracker[camera_id]:
                    self.line_cooldown_tracker[camera_id][line] = {}

            logger.info(f"Initialized camera tracking structures: {camera_id}")
        except Exception as e:
            logger.error(f"Failed to initialize camera {camera_id}: {e}")
            raise

    def initialize_sources(self, camera_ids: List[str]):
        with self.lock:
            for camera_id in camera_ids:
                if camera_id not in self.data:
                    
                    self.data[camera_id] = {"zones": {}, "lines": {}}
                    logger.info(f"Initialized NEW camera: {camera_id}")
                else:
                    
                    logger.info(f"Preserved zones/lines for existing camera: {camera_id}")
                

                self._init_camera(camera_id)
            
            logger.info(f"Initialized cameras: {camera_ids}")

    def save_zone_line_config(self, filepath="zone_line_config.json"):
        try:
            config = {}
            line_totals = {"total_in": 0, "total_out": 0, "cameras": {}}
            with self.lock:
                for cam_id, cam_data in self.data.items():
                    config[cam_id] = {
                        "zones": {},
                        "lines": {}
                    }
                    line_totals["cameras"][cam_id] = {}
                    for zone_name, zone_data in cam_data.get("zones", {}).items():
                        config[cam_id]["zones"][zone_name] = {
                            "top_left": zone_data.get("top_left"),
                            "bottom_right": zone_data.get("bottom_right"),
                            "points": zone_data.get("points"),
                            "in_count": zone_data.get("in_count", 0),
                            "out_count": zone_data.get("out_count", 0)
                        }
                    for line_name, line_data in cam_data.get("lines", {}).items():
                        in_c = line_data.get("in_count", 0)
                        out_c = line_data.get("out_count", 0)
                        config[cam_id]["lines"][line_name] = {
                            "start": line_data.get("start"),
                            "end": line_data.get("end"),
                            "in_count": in_c,
                            "out_count": out_c
                        }
                        line_totals["cameras"][cam_id][line_name] = {
                            "in_count": in_c,
                            "out_count": out_c
                        }
                        line_totals["total_in"] += in_c
                        line_totals["total_out"] += out_c

            def _write_config(cfg, path, l_totals):
                try:
                    with open(path, "w") as f:
                        json.dump(cfg, f, indent=2)
                    logger.info(f"Saved zones/lines config for {len(cfg)} cameras to {path}")
                    
                    l_path = os.path.join(os.path.dirname(path) if os.path.dirname(path) else ".", "line_totals.json")
                    with open(l_path, "w") as f:
                        json.dump(l_totals, f, indent=2)
                    logger.info(f"Saved line totals to {l_path}")
                except Exception as e:
                    logger.error(f"Error saving zones/lines config: {e}")

            t = threading.Thread(target=_write_config, args=(config, filepath, line_totals), daemon=True)
            t.start()
        except Exception as e:
            logger.error(f"Error preparing zone/line config save: {e}")

    def load_zone_line_config(self, filepath="zone_line_config.json"):
        if not os.path.exists(filepath):
            logger.info(f"No config file found at {filepath}, starting fresh")
            return
        
        try:
            with open(filepath, "r") as f:
                config = json.load(f)
            
            with self.lock:
                for cam_id, cam_conf in config.items():
                    if cam_id not in self.data:
                        self.data[cam_id] = {"zones": {}, "lines": {}}
                    
                    recovery_events = []
                    for zone_name, zone_coords in cam_conf.get("zones", {}).items():
                        try:
                            # Ensure we are comparing integers
                            in_count = int(zone_coords.get("in_count", 0))
                            out_count = int(zone_coords.get("out_count", 0))
                        except (ValueError, TypeError):
                            in_count = 0
                            out_count = 0
                        
                        logger.debug(f"Loading zone '{zone_name}' for camera '{cam_id}': in={in_count}, out={out_count}")
                        
                        # Equalization on start: Prevent ghost occupancy from previous sessions
                        if in_count > out_count:
                            diff = in_count - out_count
                            logger.info(f"Startup Equalization: Camera {cam_id}, Zone {zone_name} has {diff} ghost counts. Equalizing and preparing recovery events.")
                            
                            for i in range(diff):
                                recovery_pid = 888000 + (int(time.time()) % 1000) + i
                                recovery_events.append({
                                    "camera_id": str(cam_id),
                                    "zone_name": zone_name,
                                    "action": "Exited",
                                    "person_id": recovery_pid,
                                    "pi_id": self.pi_id,
                                    "timestamp": datetime.datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
                                    "note": "startup_equalization_balance"
                                })
                            out_count = in_count

                        self.data[cam_id]["zones"][zone_name] = {
                            "top_left": zone_coords.get("top_left"),
                            "bottom_right": zone_coords.get("bottom_right"),
                            "points": zone_coords.get("points"),
                            "in_count": in_count,
                            "out_count": out_count,
                            "inside_ids": [],
                            "history": []
                        }
                    
                    # Send recovery events outside the lock if possible, 
                    # but for now we'll just queue them for the analytics worker
                    for ev in recovery_events:
                        try:
                            # We can use post_event from analytics_poster
                            from analytics_poster import post_event
                            post_event(ev)
                            
                            # Also publish via MQTT if client is available
                            if self.mqtt_client:
                                topic = f"vision/{self.pi_id}/{cam_id}/history/zone/exit"
                                self.mqtt_client.publish(topic, json.dumps(ev), qos=1)
                        except Exception as e:
                            logger.error(f"Failed to send startup recovery event: {e}")
                    
                    for line_name, line_coords in cam_conf.get("lines", {}).items():
                        self.data[cam_id]["lines"][line_name] = {
                            "start": line_coords.get("start"),
                            "end": line_coords.get("end"),
                            "in_count": line_coords.get("in_count", 0),
                            "out_count": line_coords.get("out_count", 0),
                            "history": []
                        }
            
            logger.info(f"Loaded zones/lines config for {len(config)} cameras from {filepath}")
        except Exception as e:
            logger.error(f"Error loading zones/lines config: {e}")

    def _clear_trackers(self) -> None:
        self.inside_zones.clear()
        self.person_state_buffer.clear()
        self.person_dwell_tracker.clear()
        self.line_cross_tracker.clear()
        self.line_cooldown_tracker.clear()

    def get_active_cameras_info(self) -> Dict[str, Any]:
        with self.lock:
            camera_list = []
            for camera_id in self.data.keys():
                camera_info = {
                    "camera_id": camera_id,
                    "status": "active",
                    "zones": list(self.data[camera_id].get("zones", {}).keys()),
                    "lines": list(self.data[camera_id].get("lines", {}).keys()),
                    "zone_count": len(self.data[camera_id].get("zones", {})),
                    "line_count": len(self.data[camera_id].get("lines", {}))
                }
                camera_list.append(camera_info)

            return {
                "cameras": camera_list,
                "total": len(camera_list),
                "timestamp": time.time(),
                "status": "active"
            }

    def _is_in_zone(self, point: Tuple[float, float], zone_data: Dict[str, Any]) -> bool:
        try:
            x, y = point
            
            # Case 1: Arbitrary polygon points
            points = zone_data.get("points")
            if points:
                # Use np.int32 and reshape to (N, 1, 2) which is the standard contour format for OpenCV
                poly_np = np.array(points, dtype=np.int32).reshape((-1, 1, 2))
                # cv2.pointPolygonTest returns >= 0 for inside/on edge
                return cv2.pointPolygonTest(poly_np, (float(x), float(y)), False) >= 0

            # Case 2: Rectangular coordinates (top_left, bottom_right)
            top_left = zone_data.get("top_left")
            bottom_right = zone_data.get("bottom_right")
            
            if not top_left or not bottom_right:
                return False

            x1, y1 = top_left
            x2, y2 = bottom_right

            px1 = x1 + self.config.zone_padding
            py1 = y1 + self.config.zone_padding
            px2 = x2 - self.config.zone_padding
            py2 = y2 - self.config.zone_padding

            if px1 >= px2 or py1 >= py2:
                return x1 <= x <= x2 and y1 <= y <= y2

            return px1 <= x <= px2 and py1 <= y <= py2

        except (ValueError, TypeError, IndexError, Exception) as e:
            logger.warning(f"Invalid zone check parameters: {e}")
            return False

    def _get_person_position(self, person_data: Tuple, method: str = "center") -> Tuple[float, float]:
        try:
            if len(person_data) == 5:
                _, x1, y1, x2, y2 = person_data
                if method == "bottom_center":
                    return (x1 + x2) / 2, y2
                elif method == "center":
                    return (x1 + x2) / 2, (y1 + y2) / 2
            elif len(person_data) == 3:
                _, x, y = person_data
                return float(x), float(y)
        except (ValueError, TypeError, IndexError) as e:
            logger.warning(f"Failed to extract person position: {e}")

        return 0.0, 0.0

    def _update_state_buffer(self, camera_id: str, zone: str, person_id: int, is_inside: bool) -> bool:
        try:
            buffer = self.person_state_buffer[camera_id][zone]
            current_time = datetime.datetime.now()

            if person_id not in buffer:
                buffer[person_id] = {
                    'state': is_inside,
                    'count': 1,
                    'last_update': current_time
                }
                return False

            data = buffer[person_id]
            if data['state'] != is_inside:
                data.update({
                    'state': is_inside,
                    'count': 1,
                    'last_update': current_time
                })
                return False
            else:
                data['count'] += 1
                data['last_update'] = current_time
                return data['count'] >= self.config.state_confirmation_frames

        except Exception as e:
            logger.error(f"Error updating state buffer: {e}")
            return False

    def _update_dwell_tracker(self, camera_id: str, zone: str, person_id: int,
                              is_inside: bool, current_time: datetime.datetime) -> Dict[str, Any]:
        try:
            tracker = self.person_dwell_tracker[camera_id][zone]

            if person_id not in tracker:
                if is_inside:
                    tracker[person_id] = {
                        'entry_time': current_time,
                        'last_seen': current_time,
                        'counted': False,
                        'exit_time': None,
                        'state': PersonState.INSIDE.value
                    }
                    return {'action': 'entered', 'dwell_time': 0.0, 'should_count': False}
                return {'action': 'none', 'dwell_time': 0.0, 'should_count': False}

            entry = tracker[person_id]

            if is_inside:
                if entry['state'] == PersonState.EXITING.value:
                    entry.update({
                        'state': PersonState.INSIDE.value,
                        'exit_time': None,
                        'last_seen': current_time
                    })
                    return {'action': 're_entered', 'dwell_time': 0.0, 'should_count': False}

                entry['last_seen'] = current_time
                dwell_time = (current_time - entry['entry_time']).total_seconds()

                if not entry['counted'] and dwell_time >= self.config.min_dwell_time:
                    entry['counted'] = True
                    return {
                        'action': 'qualified_entry',
                        'dwell_time': dwell_time,
                        'should_count': True
                    }

                return {'action': 'dwelling', 'dwell_time': dwell_time, 'should_count': False}

            else:
                if entry['state'] == PersonState.INSIDE.value:
                    entry.update({
                        'state': PersonState.EXITING.value,
                        'exit_time': current_time
                    })
                    dwell_time = (current_time - entry['entry_time']).total_seconds()
                    return {
                        'action': 'exiting',
                        'dwell_time': dwell_time,
                        'should_count': entry['counted']
                    }

                elif entry['state'] == PersonState.EXITING.value:
                    exit_duration = (current_time - entry['exit_time']).total_seconds()
                    if exit_duration >= self.config.exit_grace_time:
                        dwell_time = (entry['exit_time'] - entry['entry_time']).total_seconds()
                        should_count = entry['counted']
                        del tracker[person_id]
                        return {
                            'action': 'confirmed_exit',
                            'dwell_time': dwell_time,
                            'should_count': should_count
                        }
                return {'action': 'outside', 'dwell_time': 0.0, 'should_count': False}

        except Exception as e:
            logger.error(f"Error updating dwell tracker: {e}")
            return {'action': 'error', 'dwell_time': 0.0, 'should_count': False}

    def _get_side_of_line(self, point: np.ndarray, line_start: np.ndarray, line_end: np.ndarray) -> int:
        try:
            line_vec = line_end - line_start
            point_vec = point - line_start
            cross_product_z = line_vec[0] * point_vec[1] - line_vec[1] * point_vec[0]
            return int(np.sign(cross_product_z))
        except Exception as e:
            logger.warning(f"Error calculating line side: {e}")
            return 0

    def _trim_history(self, history: List[Dict], max_entries: int = None) -> List[Dict]:
        max_entries = max_entries or self.config.max_history_entries
        if len(history) > max_entries:
            return history[-max_entries:]
        return history

    def _publish_mqtt_event(self, topic: str, payload: Dict[str, Any]) -> None:
        try:
            if self.mqtt_client:
                self.mqtt_client.publish(topic, json.dumps(payload), qos=1)
        except Exception as e:
            logger.error(f"Failed to publish MQTT message: {e}")

    def update_counts(self, camera_id: str, detected_people: Set[Tuple]) -> None:
        if not camera_id or not isinstance(detected_people, (set, list)):
            logger.warning(f"Invalid parameters: camera_id={camera_id}, people type={type(detected_people)}")
            return

        valid_people = set()
        for person_data in detected_people:
            if self._validate_person_data(person_data):
                valid_people.add(person_data)
            else:
                logger.warning(f"Invalid person data: {person_data}")

        events_to_post = []
        with self.lock:
            try:
                if camera_id not in self.data:
                    logger.warning(f"Initializing unknown camera: {camera_id}")
                    self.data[camera_id] = {"zones": {}, "lines": {}}
                    self._init_camera(camera_id)

                active_ids = {p[0] for p in valid_people if len(p) >= 1}
                current_time = datetime.datetime.now()

                self._process_zones(camera_id, valid_people, active_ids, current_time, events_to_post)

                self._process_lines(camera_id, valid_people, current_time, active_ids, events_to_post)

                if (current_time - self.last_cleanup).total_seconds() > (self.config.cleanup_interval_minutes * 60):
                    self._perform_cleanup()
                    self.last_cleanup = current_time

            except Exception as e:
                logger.error(f"Failed to update counts for {camera_id}: {e}")
                raise

        if events_to_post:
            self.save_zone_line_config()
            
        for ev in events_to_post:
            post_event(ev)

    def _process_zones(self, camera_id: str, detected_people: Set[Tuple],
                       active_ids: Set[int], current_time: datetime.datetime, events: list) -> None:
        for zone, zone_data in self.data[camera_id].get("zones", {}).items():
            try:
                current_inside = set()
                entries_to_count = []
                exits_to_count = []

                for person_data in detected_people:
                    person_id = person_data[0]
                    # Use bottom_center (feet) for more accurate floor-based zone detection
                    position = self._get_person_position(person_data, method="bottom_center")
                    is_inside = self._is_in_zone(position, zone_data)

                    if self._update_state_buffer(camera_id, zone, person_id, is_inside):
                        if is_inside:
                            current_inside.add(person_id)

                        dwell_result = self._update_dwell_tracker(camera_id, zone, person_id, is_inside, current_time)

                        if dwell_result['should_count']:
                            if dwell_result['action'] == 'qualified_entry':
                                entries_to_count.append(person_id)
                            elif dwell_result['action'] == 'confirmed_exit':
                                exits_to_count.append(person_id)

                for person_id in list(self.person_dwell_tracker[camera_id].get(zone, {}).keys()):
                    if person_id not in active_ids:
                        dwell_result = self._update_dwell_tracker(camera_id, zone, person_id, False, current_time)
                        if dwell_result['should_count'] and dwell_result['action'] == 'confirmed_exit':
                            exits_to_count.append(person_id)

                timestamp_str = current_time.strftime("%Y-%m-%d %H:%M:%S")

                if entries_to_count:
                    zone_data["in_count"] += len(entries_to_count)
                    for pid in entries_to_count:
                        history_entry = {"id": pid, "action": ActionType.ENTRY.value, "time": timestamp_str}
                        zone_data["history"].append(history_entry)

                        topic = f"vision/{self.pi_id}/{camera_id}/history/zone/entry"
                        payload = {"zone": zone, **history_entry}
                       
                        person_position = None
                        for person_data in detected_people:
                            if person_data[0] == pid:
                                person_position = self._get_person_position(person_data, method="center")
                                break

                        try:
                            payload = {
                                "camera_id": str(camera_id),
                                "zone_name": zone,
                                "action": "Entered",
                                "person_id": pid,
                                "pi_id": self.pi_id,
                                "timestamp": datetime.datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
                            }
                            events.append(payload)
                            logger.info(f"Generated 'Entered' event for camera {camera_id}, zone {zone}, person {pid}")
                        except Exception as e:
                            logger.error(f"Failed to prepare zone entry event: {e}")

                if exits_to_count:
                    zone_data["out_count"] += len(exits_to_count)
                    for pid in exits_to_count:
                        history_entry = {"id": pid, "action": ActionType.EXIT.value, "time": timestamp_str}
                        zone_data["history"].append(history_entry)

                        topic = f"vision/{self.pi_id}/{camera_id}/history/zone/exit"
                        payload = {"zone": zone, **history_entry}
                        

                        try:
                            payload = {
                                "camera_id": str(camera_id),
                                "zone_name": zone,
                                "action": "Exited",
                                "person_id": pid,
                                "pi_id": self.pi_id,
                                "timestamp": datetime.datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
                            }
                            events.append(payload)
                            logger.info(f"Generated 'Exited' event for camera {camera_id}, zone {zone}, person {pid}")
                        except Exception as e:
                            logger.error(f"Failed to prepare zone exit event: {e}")

                self.inside_zones[camera_id][zone] = current_inside
                zone_data["inside_ids"] = list(current_inside)

                
                if not current_inside:
                    if self.zone_empty_since[camera_id][zone] is None:
                        self.zone_empty_since[camera_id][zone] = current_time
                    elif (current_time - self.zone_empty_since[camera_id][zone]).total_seconds() >= 5.0:
                        
                        
                        stale_tracker_ids = list(self.person_dwell_tracker[camera_id].get(zone, {}).keys())
                        
                        if stale_tracker_ids:
                            logger.info(f"Auto-correcting stale occupancy for camera {camera_id}, zone {zone}. Flushing IDs: {stale_tracker_ids}")
                            for pid in stale_tracker_ids:
                                tracker_entry = self.person_dwell_tracker[camera_id][zone][pid]
                                
                                if tracker_entry.get('counted'):
                                    zone_data["out_count"] += 1
                                    history_entry = {"id": pid, "action": ActionType.EXIT.value, "time": timestamp_str}
                                    zone_data["history"].append(history_entry)

                                    try:
                                        payload = {
                                            "camera_id": str(camera_id),
                                            "zone_name": zone,
                                            "action": "Exited",
                                            "person_id": pid,
                                            "pi_id": self.pi_id,
                                            "timestamp": datetime.datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
                                            "note": "auto_corrected_stale"
                                        }
                                        events.append(payload)
                                        logger.info(f"Generated auto-corrected 'Exited' event for camera {camera_id}, zone {zone}, person {pid}")
                                    except Exception as e:
                                        logger.error(f"Failed to prepare auto-corrected exit event: {e}")
                                
                                # Remove from tracker
                                del self.person_dwell_tracker[camera_id][zone][pid]
                            
                            # Also clear inside cache
                            self.inside_zones[camera_id][zone].clear()
                            zone_data["inside_ids"] = []
                            
                            # Persist the correction
                            self.save_zone_line_config()
                        else:
                            # Reboot recovery: No active trackers, but persisted counts show
                            # residual occupancy from the previous session.
                            # We reset local counts WITHOUT sending analytics exit events -
                            # those entries were already balanced in the previous session.
                            # Sending phantom exits here causes exits > entries on the backend.
                            occupancy = zone_data["in_count"] - zone_data["out_count"]
                            if occupancy > 0:
                                logger.info(
                                    f"Reboot Recovery: Clearing {occupancy} residual "
                                    f"occupants for camera {camera_id}, zone {zone}. "
                                    f"Sending balancing exit events to backend."
                                )
                                
                                for i in range(occupancy):
                                    try:
                                        # Use a large integer starting from a high offset to avoid collisions
                                        # and satisfy backend integer requirements.
                                        recovery_pid = 999000 + (int(time.time()) % 1000) + i
                                        payload = {
                                            "camera_id": str(camera_id),
                                            "zone_name": zone,
                                            "action": "Exited",
                                            "person_id": recovery_pid,
                                            "pi_id": self.pi_id,
                                            "timestamp": datetime.datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
                                            "note": "reboot_recovery_balance"
                                        }
                                        events.append(payload)
                                    except Exception as e:
                                        logger.error(f"Failed to prepare recovery exit event: {e}")

                                zone_data["out_count"] = zone_data["in_count"]
                                zone_data["inside_ids"] = []
                                self.inside_zones[camera_id][zone].clear()
                                self.save_zone_line_config()
                else:
                    # Zone is not empty, reset timer
                    self.zone_empty_since[camera_id][zone] = None
               

                zone_data["history"] = self._trim_history(zone_data["history"])

            except Exception as e:
                logger.error(f"Error processing zone {zone}: {e}")

    def _process_lines(self, camera_id: str, detected_people: Set[Tuple],
                       current_time: datetime.datetime, active_ids: Set[int], events: list) -> None:
        for line_name, line_data in self.data[camera_id].get("lines", {}).items():
            try:
                tracker = self.line_cross_tracker[camera_id][line_name]
                cooldown_tracker = self.line_cooldown_tracker[camera_id][line_name]
                p_orig_start = np.array(line_data["start"], dtype=float)
                p_orig_end = np.array(line_data["end"], dtype=float)
                
                line_vec = p_orig_end - p_orig_start
                length = np.linalg.norm(line_vec)
                if length > 0:
                    unit_vec = line_vec / length
                    p_line_start = p_orig_start - 50.0 * unit_vec
                    p_line_end = p_orig_end + 50.0 * unit_vec
                else:
                    p_line_start = p_orig_start
                    p_line_end = p_orig_end

                for person_id in list(tracker.keys()):
                    if person_id not in active_ids:
                        del tracker[person_id]

                stale_cooldowns = [pid for pid, end_time in cooldown_tracker.items()
                                   if current_time > end_time]
                for pid in stale_cooldowns:
                    del cooldown_tracker[pid]

                for person_data in detected_people:
                    person_id = person_data[0]
                    p_current = np.array(self._get_person_position(person_data, method="bottom_center"))



                    if person_id in cooldown_tracker:
                        continue

                    current_side = self._get_side_of_line(p_current, p_line_start, p_line_end)
                    if current_side == 0:
                        continue

                    if person_id not in tracker:
                        tracker[person_id] = {
                            'position': p_current.copy(),
                            'side': current_side
                        }
                        continue

                    person_track = tracker[person_id]
                    p_prev = person_track['position']
                    prev_side = person_track['side']

                    displacement = np.linalg.norm(p_current - p_prev)
                    if displacement < self.config.min_movement_threshold:
                        continue

                    person_track.update({
                        'position': p_current.copy(),
                        'side': current_side
                    })

                    if current_side != prev_side and current_side != 0 and prev_side != 0:
                        side_c = self._get_side_of_line(p_line_start, p_prev, p_current)
                        side_d = self._get_side_of_line(p_line_end, p_prev, p_current)
                        
                        if side_c != side_d and side_c != 0 and side_d != 0:
                            timestamp_str = current_time.strftime("%Y-%m-%d %H:%M:%S")
                            action = ActionType.IN.value if current_side > 0 else ActionType.OUT.value
                            
                            if line_data.get("swap", False):
                                action = ActionType.OUT.value if action == ActionType.IN.value else ActionType.IN.value

                            if action == ActionType.IN.value:
                                line_data["in_count"] += 1
                            else:
                                line_data["out_count"] += 1

                            history_entry = {"id": person_id, "action": action, "time": timestamp_str}
                            line_data["history"].append(history_entry)
                            line_data["history"] = self._trim_history(line_data["history"])

                            topic = f"vision/{self.pi_id}/{camera_id}/history/line/cross"
                            payload = {"line": line_name, **history_entry}
                            
                            try:
                                event_action = 'Entered' if action == ActionType.IN.value else 'Exited'
                                payload = {
                                    "camera_id": str(camera_id),
                                    "zone_name": line_name,
                                    "action": event_action,
                                    "person_id": person_id,
                                    "pi_id": self.pi_id,
                                    "source": "line",
                                    "timestamp": datetime.datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
                                }
                                events.append(payload)
                                logger.info(f"Generated '{event_action}' line event for camera {camera_id}, line {line_name}, person {person_id}")
                            except Exception:
                                logger.error("Failed to prepare line crossing event")

                            cooldown_end_time = current_time + datetime.timedelta(
                                seconds=self.config.crossing_cooldown_seconds)
                            cooldown_tracker[person_id] = cooldown_end_time

                            self.save_zone_line_config()
                            continue

            except Exception as e:
                logger.error(f"Error processing line {line_name}: {e}")

    def _perform_cleanup(self) -> None:
        try:
            current_time = datetime.datetime.now()
            cleanup_threshold = datetime.timedelta(minutes=30)

            for camera_id in self.data.keys():
                for zone, buffer in self.person_state_buffer.get(camera_id, {}).items():
                    stale_ids = [
                        pid for pid, data in buffer.items()
                        if (current_time - data['last_update']) > cleanup_threshold
                    ]
                    for pid in stale_ids:
                        del buffer[pid]

                for zone, tracker in self.person_dwell_tracker.get(camera_id, {}).items():
                    stale_ids = []
                    for pid, data in tracker.items():
                        last_active = data.get('exit_time', data.get('last_seen'))
                        if last_active and (current_time - last_active) > cleanup_threshold:
                            stale_ids.append(pid)
                    
                    for pid in stale_ids:
                        # CRITICAL: If they were counted as 'Entered', we MUST count them as 'Exited'
                        # before deleting, otherwise we get a permanent count imbalance.
                        data = tracker[pid]
                        if data.get('counted'):
                            zone_data = self.data[camera_id]["zones"].get(zone)
                            if zone_data:
                                zone_data["out_count"] += 1
                                logger.info(f"Cleanup: Counted exit for stale person {pid} in camera {camera_id}, zone {zone} to maintain balance.")
                                
                                # Send a background event for this cleanup exit too
                                timestamp_str = current_time.strftime("%Y-%m-%d %H:%M:%S")
                                history_entry = {"id": pid, "action": ActionType.EXIT.value, "time": timestamp_str}
                                zone_data["history"].append(history_entry)
                                
                                try:
                                    payload = {
                                        "camera_id": str(camera_id),
                                        "zone_name": zone,
                                        "action": "Exited",
                                        "person_id": pid,
                                        "pi_id": self.pi_id,
                                        "timestamp": datetime.datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
                                        "note": "cleanup_unbalanced"
                                    }
                                    # Note: We can't easily append to 'events' here as we're in a separate method,
                                    # but we can call post_event directly if imported, or just log it.
                                    # For consistency, let's use post_event if available.
                                    post_event(payload)
                                except Exception:
                                    pass

                        del tracker[pid]

            logger.info("Performed periodic cleanup")

        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

    def create_or_update_zone(self, camera_id: str, zone: str,
                              top_left: Optional[List[int]] = None,
                              bottom_right: Optional[List[int]] = None,
                              points: Optional[List[List[int]]] = None) -> bool:
        if not self._validate_coordinates(top_left, bottom_right, points):
            logger.error(f"Invalid coordinates for zone {zone}: TL={top_left}, BR={bottom_right}, P={points}")
            return False

        with self.lock:
            try:
                if camera_id not in self.data:
                    self.data[camera_id] = {"zones": {}, "lines": {}}
                    self._init_camera(camera_id)
                else:
                    if camera_id not in self.inside_zones:
                        self.inside_zones[camera_id] = defaultdict(set)
                    if camera_id not in self.person_state_buffer:
                        self.person_state_buffer[camera_id] = defaultdict(dict)
                    if camera_id not in self.person_dwell_tracker:
                        self.person_dwell_tracker[camera_id] = defaultdict(dict)
                    if camera_id not in self.person_zone_history:
                        self.person_zone_history[camera_id] = defaultdict(dict)

                    if zone not in self.inside_zones[camera_id]:
                        self.inside_zones[camera_id][zone] = set()
                    if zone not in self.person_state_buffer[camera_id]:
                        self.person_state_buffer[camera_id][zone] = {}
                    if zone not in self.person_dwell_tracker[camera_id]:
                        self.person_dwell_tracker[camera_id][zone] = {}
                    if zone not in self.person_zone_history[camera_id]:
                        self.person_zone_history[camera_id][zone] = {}

                if camera_id in self.data and zone in self.data[camera_id].get("zones", {}):
                    existing = self.data[camera_id]["zones"][zone]
                    in_count = existing.get("in_count", 0)
                    out_count = existing.get("out_count", 0)
                else:
                    in_count = 0
                    out_count = 0

                self.data[camera_id]["zones"][zone] = {
                    "top_left": top_left,
                    "bottom_right": bottom_right,
                    "points": points,
                    "in_count": in_count,
                    "out_count": out_count,
                    "inside_ids": [],
                    "history": []
                }
                
                self.save_zone_line_config()
                logger.info(f"Created/updated zone '{zone}' for camera '{camera_id}' (Counts preserved: in={in_count}, out={out_count})")
                return True
            except Exception as e:
                logger.error(f"Failed to create/update zone {zone}: {e}")
                return False

    def remove_camera(self, camera_id: str) -> bool:
        """Fully purge all tracking and data for a specific camera_id to prevent zombie state."""
        with self.lock:
            try:
                # Remove from primary data dictionary
                self.data.pop(camera_id, None)
                
                # Deep clean all tracking dicts
                self.inside_zones.pop(camera_id, None)
                self.person_state_buffer.pop(camera_id, None)
                self.person_dwell_tracker.pop(camera_id, None)
                self.line_cross_tracker.pop(camera_id, None)
                self.line_cooldown_tracker.pop(camera_id, None)
                self.person_zone_history.pop(camera_id, None)
                self.zone_empty_since.pop(camera_id, None)
                
                # Also reset active camera if it was the one being removed
                if self.active_camera == camera_id:
                    self.active_camera = next(iter(self.data.keys())) if self.data else "camera1"

                self.save_zone_line_config()
                logger.info(f"MultiSourceZoneVisitorCounter: Fully removed camera '{camera_id}' and cleared all its tracking state.")
                return True
            except Exception as e:
                logger.error(f"Failed to fully remove camera {camera_id}: {e}")
                return False

    def delete_zone(self, camera_id: str, zone: str) -> bool:
        with self.lock:
            try:
                if camera_id not in self.data or zone not in self.data[camera_id].get("zones", {}):
                    logger.warning(f"Zone {zone} not found in camera {camera_id}")
                    return False

                del self.data[camera_id]["zones"][zone]

                if camera_id in self.inside_zones and zone in self.inside_zones[camera_id]:
                    del self.inside_zones[camera_id][zone]
                if camera_id in self.person_state_buffer and zone in self.person_state_buffer[camera_id]:
                    del self.person_state_buffer[camera_id][zone]
                if camera_id in self.person_dwell_tracker and zone in self.person_dwell_tracker[camera_id]:
                    del self.person_dwell_tracker[camera_id][zone]
                if camera_id in self.person_zone_history and zone in self.person_zone_history[camera_id]:
                    del self.person_zone_history[camera_id][zone]

                self.save_zone_line_config()

                logger.info(f"Deleted zone '{zone}' from camera '{camera_id}'")
                return True
            except Exception as e:
                logger.error(f"Failed to delete zone {zone}: {e}")
                return False

    def reset_zone_counts(self, camera_id: str, zone: str) -> bool:
        with self.lock:
            try:
                if camera_id not in self.data or zone not in self.data[camera_id].get("zones", {}):
                    logger.warning(f"Zone {zone} not found in camera {camera_id}")
                    return False

                zone_data = self.data[camera_id]["zones"][zone]
                zone_data.update({
                    "in_count": 0,
                    "out_count": 0,
                    "history": [],
                    "inside_ids": []
                })

                if camera_id in self.inside_zones and zone in self.inside_zones[camera_id]:
                    self.inside_zones[camera_id][zone].clear()
                if camera_id in self.person_state_buffer and zone in self.person_state_buffer[camera_id]:
                    self.person_state_buffer[camera_id][zone].clear()
                if camera_id in self.person_dwell_tracker and zone in self.person_dwell_tracker[camera_id]:
                    self.person_dwell_tracker[camera_id][zone].clear()
                if camera_id in self.person_zone_history and zone in self.person_zone_history[camera_id]:
                    self.person_zone_history[camera_id][zone].clear()

                logger.info(f"Reset counts for zone '{zone}' in camera '{camera_id}'")
                return True
            except Exception as e:
                logger.error(f"Failed to reset zone {zone}: {e}")
                return False

    def get_zone_stats(self, camera_id: str, zone: str) -> Optional[Dict[str, Any]]:
        try:
            if (camera_id not in self.data or
                    zone not in self.data[camera_id].get("zones", {})):
                return None

            zone_data = self.data[camera_id]["zones"][zone]
            current_inside = self.inside_zones.get(camera_id, {}).get(zone, set())

            dwell_stats = {
                "active_people": 0,
                "avg_dwell_time": 0.0,
                "max_dwell_time": 0.0,
                "qualified_entries": 0
            }

            if (camera_id in self.person_dwell_tracker and
                    zone in self.person_dwell_tracker[camera_id]):
                now = datetime.datetime.now()
                dwell_times = []

                for pid, data in self.person_dwell_tracker[camera_id][zone].items():
                    if data['state'] == PersonState.INSIDE.value:
                        dwell_time = (now - data['entry_time']).total_seconds()
                        dwell_times.append(dwell_time)
                        dwell_stats["active_people"] += 1
                        if data['counted']:
                            dwell_stats["qualified_entries"] += 1

                if dwell_times:
                    dwell_stats["avg_dwell_time"] = sum(dwell_times) / len(dwell_times)
                    dwell_stats["max_dwell_time"] = max(dwell_times)

            return {
                "in_count": zone_data["in_count"],
                "out_count": zone_data["out_count"],
                "net_count": zone_data["in_count"] - zone_data["out_count"],
                "current_occupancy": len(current_inside),
                "inside_ids": list(current_inside),
                "dwell_stats": dwell_stats,
                "coordinates": {
                    "top_left": zone_data.get("top_left"),
                    "bottom_right": zone_data.get("bottom_right"),
                    "points": zone_data.get("points")
                },
                "recent_history": zone_data["history"][-10:]
            }
        except Exception as e:
            logger.error(f"Failed to get stats for zone {zone}: {e}")
            return None

    def create_or_update_line(self, camera_id: str, line_name: str,
                              start: List[int], end: List[int], swap: bool = False) -> bool:
        if not self._validate_line_coordinates(start, end):
            logger.error(f"Invalid coordinates for line {line_name}: {start} -> {end}")
            return False

        with self.lock:
            try:
                if camera_id not in self.data:
                    self.data[camera_id] = {"zones": {}, "lines": {}}

                if "lines" not in self.data[camera_id]:
                    self.data[camera_id]["lines"] = {}

                self.data[camera_id]["lines"][line_name] = {
                    "start": start,
                    "end": end,
                    "swap": swap,
                    "in_count": 0,
                    "out_count": 0,
                    "history": []
                }
                self._init_camera(camera_id)

                self.save_zone_line_config()

                logger.info(f"Created/updated line '{line_name}' for camera '{camera_id}'")
                return True
            except Exception as e:
                logger.error(f"Failed to create/update line {line_name}: {e}")
                return False

    def delete_line(self, camera_id: str, line_name: str) -> bool:
        with self.lock:
            try:
                if (camera_id not in self.data or
                        line_name not in self.data[camera_id].get("lines", {})):
                    logger.warning(f"Line {line_name} not found in camera {camera_id}")
                    return False

                del self.data[camera_id]["lines"][line_name]

                if (camera_id in self.line_cross_tracker and
                        line_name in self.line_cross_tracker[camera_id]):
                    del self.line_cross_tracker[camera_id][line_name]
                if (camera_id in self.line_cooldown_tracker and
                        line_name in self.line_cooldown_tracker[camera_id]):
                    del self.line_cooldown_tracker[camera_id][line_name]

                self.save_zone_line_config()

                logger.info(f"Deleted line '{line_name}' from camera '{camera_id}'")
                return True
            except Exception as e:
                logger.error(f"Failed to delete line {line_name}: {e}")
                return False

    def reset_line_counts(self, camera_id: str, line_name: str) -> bool:
        with self.lock:
            try:
                if (camera_id not in self.data or
                        line_name not in self.data[camera_id].get("lines", {})):
                    logger.warning(f"Line {line_name} not found in camera {camera_id}")
                    return False

                line_data = self.data[camera_id]["lines"][line_name]
                line_data.update({
                    "in_count": 0,
                    "out_count": 0,
                    "history": []
                })

                if (camera_id in self.line_cross_tracker and
                        line_name in self.line_cross_tracker[camera_id]):
                    self.line_cross_tracker[camera_id][line_name].clear()
                if (camera_id in self.line_cooldown_tracker and
                        line_name in self.line_cooldown_tracker[camera_id]):
                    self.line_cooldown_tracker[camera_id][line_name].clear()

                logger.info(f"Reset counts for line '{line_name}' in camera '{camera_id}'")
                return True
            except Exception as e:
                logger.error(f"Failed to reset line {line_name}: {e}")
                return False

    def get_line_stats(self, camera_id: str, line_name: str) -> Optional[Dict[str, Any]]:
        try:
            if (camera_id not in self.data or
                    line_name not in self.data[camera_id].get("lines", {})):
                return None

            line_data = self.data[camera_id]["lines"][line_name]

            active_tracks = 0
            if (camera_id in self.line_cross_tracker and
                    line_name in self.line_cross_tracker[camera_id]):
                active_tracks = len(self.line_cross_tracker[camera_id][line_name])

            return {
                "in_count": line_data["in_count"],
                "out_count": line_data["out_count"],
                "net_count": line_data["in_count"] - line_data["out_count"],
                "active_tracks": active_tracks,
                "coordinates": {
                    "start": line_data["start"],
                    "end": line_data["end"]
                },
                "recent_history": line_data["history"][-10:]
            }
        except Exception as e:
            logger.error(f"Failed to get stats for line {line_name}: {e}")
            return None

    def get_all_lines(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        with self.lock:
            result = {}
            for camera_id in self.data.keys():
                if "lines" in self.data[camera_id]:
                    result[camera_id] = self.data[camera_id]["lines"]
            return result

    def set_active_camera(self, camera_id: str) -> bool:
        with self.lock:
            if camera_id in self.data:
                self.active_camera = camera_id
                logger.info(f"Active camera set to: {camera_id}")
                return True
            else:
                logger.warning(f"Camera {camera_id} not found")
                return False

    def get_camera_summary(self, camera_id: str) -> Optional[Dict[str, Any]]:
        try:
            if camera_id not in self.data:
                return None

            zones_summary = {}
            for zone_name in self.data[camera_id].get("zones", {}):
                stats = self.get_zone_stats(camera_id, zone_name)
                if stats:
                    zones_summary[zone_name] = {
                        "occupancy": stats["current_occupancy"],
                        "total_entries": stats["in_count"],
                        "total_exits": stats["out_count"]
                    }

            lines_summary = {}
            for line_name in self.data[camera_id].get("lines", {}):
                stats = self.get_line_stats(camera_id, line_name)
                if stats:
                    lines_summary[line_name] = {
                        "crossings_in": stats["in_count"],
                        "crossings_out": stats["out_count"],
                        "net_flow": stats["net_count"]
                    }

            return {
                "camera_id": camera_id,
                "zones": zones_summary,
                "lines": lines_summary,
                "total_zones": len(self.data[camera_id].get("zones", {})),
                "total_lines": len(self.data[camera_id].get("lines", {}))
            }
        except Exception as e:
            logger.error(f"Failed to get camera summary for {camera_id}: {e}")
            return None

    def get_all_cameras_summary(self) -> Dict[str, Any]:
        with self.lock:
            summaries = {}
            for camera_id in self.data.keys():
                summary = self.get_camera_summary(camera_id)
                if summary:
                    summaries[camera_id] = summary

            return {
                "cameras": summaries,
                "active_camera": self.active_camera,
                "total_cameras": len(summaries)
            }

    def cleanup_stale_tracks(self, camera_id: str, active_ids: Set[int]) -> None:
        try:
            current_time = datetime.datetime.now()
            stale_threshold = datetime.timedelta(seconds=30)

            for zone, buffer in self.person_state_buffer.get(camera_id, {}).items():
                stale_ids = [
                    pid for pid, data in buffer.items()
                    if pid not in active_ids or
                    (current_time - data['last_update']) > stale_threshold
                ]
                for pid in stale_ids:
                    del buffer[pid]

            for zone, tracker in self.person_dwell_tracker.get(camera_id, {}).items():
                stale_ids = []
                for pid, data in tracker.items():
                    if pid not in active_ids:
                        if data['state'] == PersonState.INSIDE.value:
                            data.update({
                                'state': PersonState.EXITING.value,
                                'exit_time': current_time
                            })
                        elif data['state'] == PersonState.EXITING.value:
                            exit_duration = (current_time - data['exit_time']).total_seconds()
                            if exit_duration >= self.config.exit_grace_time:
                                stale_ids.append(pid)
                    else:
                        last_active = data.get('exit_time', data.get('last_seen'))
                        if last_active and (current_time - last_active) > datetime.timedelta(minutes=5):
                            stale_ids.append(pid)

                for pid in stale_ids:
                    del tracker[pid]

        except Exception as e:
            logger.error(f"Error during stale track cleanup: {e}")

    def export_data(self, camera_id: Optional[str] = None,
                      start_time: Optional[datetime.datetime] = None,
                      end_time: Optional[datetime.datetime] = None) -> Dict[str, Any]:
        try:
            with self.lock:
                export_data = {}

                cameras_to_export = [camera_id] if camera_id else list(self.data.keys())

                for cam_id in cameras_to_export:
                    if cam_id not in self.data:
                        continue

                    cam_data = self.data[cam_id].copy()

                    if start_time or end_time:
                        for zone_data in cam_data.get("zones", {}).values():
                            filtered_history = []
                            for entry in zone_data.get("history", []):
                                try:
                                    entry_time = datetime.datetime.strptime(
                                        entry["time"], "%Y-%m-%d %H:%M:%S")
                                    if start_time and entry_time < start_time:
                                        continue
                                    if end_time and entry_time > end_time:
                                        continue
                                    filtered_history.append(entry)
                                except (ValueError, KeyError):
                                    continue
                            zone_data["history"] = filtered_history

                        for line_data in cam_data.get("lines", {}).values():
                            filtered_history = []
                            for entry in line_data.get("history", []):
                                try:
                                    entry_time = datetime.datetime.strptime(
                                        entry["time"], "%Y-%m-%d %H:%M:%S")
                                    if start_time and entry_time < start_time:
                                        continue
                                    if end_time and entry_time > end_time:
                                        continue
                                    filtered_history.append(entry)
                                except (ValueError, KeyError):
                                    continue
                            line_data["history"] = filtered_history

                    export_data[cam_id] = cam_data

                return {
                    "export_timestamp": datetime.datetime.now().isoformat(),
                    "cameras": export_data,
                    "config": {
                        "frame_height": self.config.frame_height,
                        "frame_width": self.config.frame_width,
                        "zone_padding": self.config.zone_padding,
                        "min_dwell_time": self.config.min_dwell_time
                    }
                }
        except Exception as e:
            logger.error(f"Failed to export data: {e}")
            return {}

    def get_system_status(self) -> Dict[str, Any]:
        try:
            with self.lock:
                total_zones = sum(len(cam_data.get("zones", {}))
                                  for cam_data in self.data.values())
                total_lines = sum(len(cam_data.get("lines", {}))
                                  for cam_data in self.data.values())

                active_zone_tracks = 0
                active_line_tracks = 0

                for cam_id in self.data.keys():
                    for zone_tracker in self.person_dwell_tracker.get(cam_id, {}).values():
                        active_zone_tracks += len(zone_tracker)
                    for line_tracker in self.line_cross_tracker.get(cam_id, {}).values():
                        active_line_tracks += len(line_tracker)

                return {
                    "status": "operational",
                    "cameras": {
                        "total": len(self.data),
                        "active": self.active_camera,
                        "list": list(self.data.keys())
                    },
                    "zones": {
                        "total": total_zones,
                        "active_tracks": active_zone_tracks
                    },
                    "lines": {
                        "total": total_lines,
                        "active_tracks": active_line_tracks
                    },
                    "last_cleanup": self.last_cleanup.isoformat(),
                    "uptime": datetime.datetime.now().isoformat()
                }
        except Exception as e:
            logger.error(f"Failed to get system status: {e}")
            return {"status": "error", "message": str(e)}
