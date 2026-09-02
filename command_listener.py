import os
import json
import paho.mqtt.client as mqtt
import logging
import time
import threading
from logging_config import get_logger
from camera_persistence import upsert_camera, update_camera_fields, remove_camera
from rtsp_utils import validate_rtsp_url

logger = get_logger(__name__)

class MqttCommandListener:
    def __init__(self, pipeline_manager, user_data, video_stream_manager, mqtt_client, pi_id, status_poster=None):
        self.logger = logging.getLogger(__name__)
        self.pipeline_manager = pipeline_manager
        self.user_data = user_data
        self.video_stream_manager = video_stream_manager
        self.status_poster = status_poster

        self.client = mqtt_client
        self.pi_id = pi_id
        
        self.camera_list_topic = f"vision/{self.pi_id}/cameras/active_list"

        self.client.on_message = self.on_message
        self.client.on_connect = self.on_connect
        
    '''def subscribe_to_commands(self):
        if self.client and self.client.is_connected():
            self.client.subscribe(self.command_topic)
            self.logger.info(f"Command Listener: Subscribed to topic '{self.command_topic}'")
        else:
            self.logger.error("Command Listener: Cannot subscribe, MQTT client is not connected.")'''

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.logger.info("Command Listener: MQTT Client Connected. Subscribing to commands...")
            
            topics = [
                f"vision/{self.pi_id}/+/camera_add",
                f"vision/{self.pi_id}/+/camera_remove",
                f"vision/{self.pi_id}/+/zone_config",
                f"vision/{self.pi_id}/+/zone_remove",
                f"vision/{self.pi_id}/+/line_config",
                f"vision/{self.pi_id}/+/line_remove",
                f"vision/{self.pi_id}/+/snapshot_request",
                f"vision/{self.pi_id}/pi_status",
                f"vision/{self.pi_id}/push_cameras",
            ]
            for t in topics:
                client.subscribe(t)
                self.logger.info(f"Command Listener: Subscribed to topic '{t}'")
        else:
            self.logger.error(f"Command Listener: Failed to connect, return code {rc}")

    def on_message(self, client, userdata, msg):
        try:
            parts = msg.topic.split('/')
            
            # Handle Pi-level commands (vision/pi_id/action)
            if len(parts) == 3:
                _, device_id, action = parts
                if device_id != self.pi_id:
                    return
                self._handle_pi_level_command(action, msg.payload)
                return

            # Handle Camera-level commands (vision/pi_id/cam_topic_id/action)
            if len(parts) < 4:
                self.logger.warning(f"Ignoring malformed topic: {msg.topic}")
                return

            _, device_id, cam_topic_id, action = parts[:4]
            if device_id != self.pi_id:
                
                return

            payload = json.loads(msg.payload.decode()) if msg.payload else {}
            self.logger.info(f"Received '{action}' for camera '{cam_topic_id}': {payload}")

            response_topic_base = f"vision/{self.pi_id}/{cam_topic_id}"

            if action == 'camera_add':
                status = 'failed'
                message = ''
                try:
                    cam_id = str(payload.get('camera_id', cam_topic_id))
                    rtsp_url = payload.get('rtsp_url')
                
                    human_name = payload.get('name', f"camera{cam_id}")

                    if not rtsp_url:
                        raise ValueError('rtsp_url missing')

                    self.logger.info(f"Validating RTSP URL for camera {cam_id}: {rtsp_url}")
                    if not validate_rtsp_url(rtsp_url):
                        raise RuntimeError(f'RTSP URL validation failed for {rtsp_url}. Camera might be offline or URL is incorrect.')

                    existing_urls = getattr(self.pipeline_manager, 'video_sources', []) or []
                    existing_names = getattr(self.pipeline_manager, 'camera_names', []) or []

                    if rtsp_url in existing_urls:
                        self.logger.info(f"Camera with URL {rtsp_url} is already active. Skipping add.")
                        status = 'success'
                        message = 'Camera is already active.'
                    else:
                        added = self.pipeline_manager.add_camera(cam_id, rtsp_url)
                        if not added:
                            raise RuntimeError('Failed to add camera to pipeline')
                        
                        
                        merged_names = getattr(self.pipeline_manager, 'camera_names', [])

                    try:
                        upsert_camera(cam_id, {
                            'camera_id': cam_id,
                            'name': human_name,
                            'rtsp_url': rtsp_url,
                            'status': 'active',
                            'zones': [],
                            'lines': [],
                            'last_updated': time.time()
                        })
                    except Exception as e:
                        self.logger.error(f"Failed to persist camera to local store: {e}")

                    status = 'success'
                    message = 'Camera stream started successfully.'
                
                    try:
                        self.user_data.initialize_sources(merged_names)
                    except Exception as e:
                        self.logger.warning(f"Could not initialize user_data for cameras {merged_names}: {e}")
                
                    try:
                        
                        sid = str(cam_id)
                        vsm = self.video_stream_manager
                        vsm.id_to_name[sid] = sid
                        vsm.name_to_id[sid] = sid
                    
                        pcams = getattr(self.pipeline_manager, 'camera_names', []) or []
                        if human_name in pcams:
                            vsm.id_to_name[sid] = human_name
                            vsm.name_to_id[human_name] = sid
                    except Exception:
                        pass
                    
                    try:
                        self.publish_active_cameras()
                    except Exception:
                        pass
                except Exception as e:
                    self.logger.error(f"camera_add failed: {e}")
                    message = str(e)
                resp = {
                    'camera_id': cam_id,
                    'status': status,
                    'message': message,
                    'timestamp': time.time()
                }
                self.client.publish(f"vision/{self.pi_id}/{cam_topic_id}/camera_add_response", json.dumps(resp), qos=1)

            elif action == 'camera_remove':
                status = 'failed'
                message = ''
                try:
                    cam_id = str(payload.get('camera_id', cam_topic_id))

                    removed = self.pipeline_manager.remove_camera(cam_id)
                    if not removed:
                        raise RuntimeError(f'Failed to remove camera {cam_id} from pipeline')

                    
                    try:
                        remove_camera(cam_id)
                    except Exception as e:
                        self.logger.error(f"Failed to remove camera from local store: {e}")

                    
                    removed = self.user_data.remove_camera(cam_id)
                except Exception as e:
                    self.logger.warning(f"Failed to remove runtime user_data for {cam_id}: {e}")
                    
                    try:
                        sid = str(cam_id)
                        vsm = self.video_stream_manager
                        
                        mapped = vsm.id_to_name.pop(sid, None)
                        if mapped:
                            vsm.name_to_id.pop(mapped, None)
                        vsm.name_to_id.pop(sid, None)
                    except Exception:
                        pass

                    
                    try:
                        self.publish_active_cameras()
                    except Exception:
                        pass

                    status = 'success'
                    message = 'Camera removed and pipeline updated.'
                except Exception as e:
                    self.logger.error(f"camera_remove failed: {e}")
                    message = str(e)

                resp = {
                    'camera_id': cam_id,
                    'status': status,
                    'message': message,
                    'timestamp': time.time()
                }
                self.client.publish(f"vision/{self.pi_id}/{cam_topic_id}/camera_remove_response", json.dumps(resp), qos=1)

            elif action == 'zone_config':
                status = 'failed'
                message = ''
                try:
                    cam_id = str(payload.get('camera_id', cam_topic_id))
                    zones = payload.get('zones', [])
                    updated_zones = []
                    for z in zones:
                        name = z.get('name')
                        points = z.get('points')
                        
                        top_left = z.get('top_left')
                        bottom_right = z.get('bottom_right')
                        
                        x1 = y1 = x2 = y2 = None
                        if top_left is not None and bottom_right is not None:
                            x1, y1 = top_left
                            x2, y2 = bottom_right
                        elif all(k in z for k in ['x1', 'y1', 'x2', 'y2']):
                            x1 = z.get('x1')
                            y1 = z.get('y1')
                            x2 = z.get('x2')
                            y2 = z.get('y2')

                        if not name or (not points and not all(v is not None for v in [x1, y1, x2, y2])):
                            self.logger.warning(f"Skipping invalid zone entry: {z}")
                            continue

                        if points:
                            self.logger.info(f"Adding/Updating polygon zone '{name}' for camera '{cam_id}' with {len(points)} points")
                            success = self.user_data.create_or_update_zone(camera_id=cam_id, zone=name, points=points)
                            if success:
                                updated_zones.append({'name': name, 'points': points})
                        else:
                            self.logger.info(f"Adding/Updating rectangular zone '{name}' for camera '{cam_id}': TL=[{x1}, {y1}], BR=[{x2}, {y2}]")
                            success = self.user_data.create_or_update_zone(camera_id=cam_id, zone=name, top_left=[x1, y1], bottom_right=[x2, y2])
                            if success:
                                updated_zones.append({'name': name, 'top_left': [x1, y1], 'bottom_right': [x2, y2]})

                    try:
                        update_camera_fields(cam_id, {'zones': updated_zones, 'last_updated': time.time()})
                    except Exception as e:
                        self.logger.error(f"Failed to persist camera zones to local store: {e}")

                    status = 'success'
                    message = f'Configured {len(updated_zones)} zones.'
                except Exception as e:
                    self.logger.error(f"zone_config failed: {e}")
                    message = str(e)

                resp = {'camera_id': cam_id, 'status': status, 'message': message, 'timestamp': time.time()}
                self.client.publish(f"{response_topic_base}/zone_config_response", json.dumps(resp), qos=1)

            elif action == 'zone_remove':
                status = 'failed'
                message = ''
                try:
                    cam_id = str(payload.get('camera_id', cam_topic_id))
                    zone_name = payload.get('zone_name') or payload.get('name')

                    if not zone_name:
                        raise ValueError('zone_name is required')

                    deleted = self.user_data.delete_zone(camera_id=cam_id, zone=zone_name)
                    if not deleted:
                        raise RuntimeError(f"Zone '{zone_name}' not found for camera '{cam_id}'")

                    
                    try:
                        from camera_persistence import get_camera
                        cam_record = get_camera(cam_id)
                        if cam_record:
                            existing_zones = cam_record.get('zones', [])
                            updated_zones = [z for z in existing_zones if z.get('name') != zone_name]
                            update_camera_fields(cam_id, {'zones': updated_zones, 'last_updated': time.time()})
                    except Exception as e:
                        self.logger.warning(f"Could not update cameras.json after zone_remove: {e}")

                    status = 'success'
                    message = f"Zone '{zone_name}' removed from camera '{cam_id}'."
                    self.logger.info(message)
                except Exception as e:
                    self.logger.error(f"zone_remove failed: {e}")
                    message = str(e)

                resp = {'camera_id': cam_id, 'zone_name': zone_name, 'status': status, 'message': message, 'timestamp': time.time()}
                self.client.publish(f"{response_topic_base}/zone_remove_response", json.dumps(resp), qos=1)

            elif action == 'line_config':
                status = 'failed'
                message = ''
                try:
                    cam_id = str(payload.get('camera_id', cam_topic_id))
                    lines = payload.get('lines', [])
                    updated_lines = []
                    for l in lines:
                        name = l.get('name')
                        
                        start = l.get('start')
                        end = l.get('end')
                        
                        if start is not None and end is not None:
                            x1, y1 = start
                            x2, y2 = end
                        else:
                            x1 = l.get('x1')
                            y1 = l.get('y1')
                            x2 = l.get('x2')
                            y2 = l.get('y2')
                            
                        swap = l.get('swap', False)

                        if not all(v is not None for v in [name, x1, y1, x2, y2]):
                            self.logger.warning(f"Skipping invalid line entry: {l}")
                            continue
                        
                        self.logger.info(f"Adding/Updating line '{name}' for camera '{cam_id}': Start=[{x1}, {y1}], End=[{x2}, {y2}], Swap={swap}")
                        success = self.user_data.create_or_update_line(camera_id=cam_id, line_name=name, start=[x1, y1], end=[x2, y2], swap=swap)
                        if success:
                            updated_lines.append({'name': name, 'start': [x1, y1], 'end': [x2, y2], 'swap': swap})

                    try:
                        update_camera_fields(cam_id, {'lines': updated_lines, 'last_updated': time.time()})
                    except Exception as e:
                        self.logger.error(f"Failed to persist camera lines to local store: {e}")

                    status = 'success'
                    message = f'Configured {len(updated_lines)} lines.'
                except Exception as e:
                    self.logger.error(f"line_config failed: {e}")
                    message = str(e)

                resp = {'camera_id': cam_id, 'status': status, 'message': message, 'timestamp': time.time()}
                self.client.publish(f"{response_topic_base}/line_config_response", json.dumps(resp), qos=1)

            elif action == 'line_remove':
                status = 'failed'
                message = ''
                try:
                    cam_id = str(payload.get('camera_id', cam_topic_id))
                    line_name = payload.get('line_name') or payload.get('name')

                    if not line_name:
                        raise ValueError('line_name is required')

                    deleted = self.user_data.delete_line(camera_id=cam_id, line_name=line_name)
                    if not deleted:
                        raise RuntimeError(f"Line '{line_name}' not found for camera '{cam_id}'")

                    # Update cameras.json persistence
                    try:
                        from camera_persistence import get_camera
                        cam_record = get_camera(cam_id)
                        if cam_record:
                            existing_lines = cam_record.get('lines', [])
                            updated_lines = [l for l in existing_lines if l.get('name') != line_name]
                            update_camera_fields(cam_id, {'lines': updated_lines, 'last_updated': time.time()})
                    except Exception as e:
                        self.logger.warning(f"Could not update cameras.json after line_remove: {e}")

                    status = 'success'
                    message = f"Line '{line_name}' removed from camera '{cam_id}'."
                    self.logger.info(message)
                except Exception as e:
                    self.logger.error(f"line_remove failed: {e}")
                    message = str(e)

                resp = {'camera_id': cam_id, 'line_name': line_name, 'status': status, 'message': message, 'timestamp': time.time()}
                self.client.publish(f"{response_topic_base}/line_remove_response", json.dumps(resp), qos=1)

            elif action == 'snapshot_request':
                cam_id = str(payload.get('camera_id', cam_topic_id))
                try:
                    self.video_stream_manager.handle_snapshot_request(cam_id)
                except Exception as e:
                    self.logger.error(f"snapshot_request failed: {e}")

            else:
                self.logger.warning(f"Unknown per-camera action received: {action}")

        except json.JSONDecodeError:
            self.logger.error("Could not decode JSON from message payload.")
        except Exception as e:
            self.logger.error(f"Error handling MQTT message: {e}", exc_info=True)

    def _handle_pi_level_command(self, action: str, payload_bytes: bytes):
        import datetime
        import requests
        
        try:
            payload = json.loads(payload_bytes.decode()) if payload_bytes else {}
        except json.JSONDecodeError:
            payload = {}

        current_timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

        if action == "pi_status":
            self.logger.info(f"Received manual pi_status request via MQTT.")
            # Dummy API URL - can be overridden via environment variables later
            status_api_url = os.getenv("PEOPLE_COUNT_PI_STATUS_API_URL", "https://dummy-api.com/pi-status")
            
            data = {
                "pi_id": self.pi_id,
                "status": "online",
                "timestamp": current_timestamp
            }
            
            try:
                resp = requests.post(status_api_url, json=data, timeout=5.0)
                self.logger.info(f"Pushed pi status to server. Response: {resp.status_code}")
            except Exception as e:
                self.logger.error(f"Failed to push pi status to server: {e}")
                
        elif action == "push_cameras":
            self.logger.info(f"Received manual push_cameras request via MQTT.")
            if self.status_poster:
                self.status_poster.post_statuses_now()
                resp = {'protocol': 'mqtt_trigger_acknowledged', 'status': 'pushing_camera_list', 'timestamp': time.time()}
                self.client.publish(f"vision/{self.pi_id}/push_cameras_response", json.dumps(resp), qos=1)
            else:
                self.logger.warning("status_poster not initialized. Cannot push cameras manually.")
                
        else:
            self.logger.warning(f"Unknown pi-level action received: {action}")

    def wait_and_publish_active_cameras(self, pipeline_manager_instance):
        self.logger.info("Pipeline has started, waiting for camera sources to initialize...")
        
        if hasattr(pipeline_manager_instance, 'camera_names'):
            expected_cameras = pipeline_manager_instance.camera_names
        else:
            expected_cameras = [f"camera{i+1}" for i in range(len(pipeline_manager_instance.video_sources))]
        expected_count = len(expected_cameras)
        
        timeout_seconds = 10 
        start_time = time.time()


        while time.time() - start_time < timeout_seconds:
            with self.user_data.lock:
                current_cameras = [cam_id for cam_id in self.user_data.data.keys() if cam_id in expected_cameras]
                current_count = len(current_cameras)
                
            if current_count >= expected_count:
                self.logger.info(f"All {current_count} camera sources are initialized. Publishing camera list.")
                self.publish_active_cameras()
                return
            time.sleep(0.5)
        
        self.logger.warning(f"Timeout reached while waiting for {expected_count} sources. Publishing current list of {current_count} cameras.")
        self.publish_active_cameras()
    
    def publish_active_cameras(self):
        if self.pipeline_manager.is_running() and hasattr(self.pipeline_manager, 'video_sources'):
            if hasattr(self.pipeline_manager, 'camera_names'):
                camera_names = self.pipeline_manager.camera_names
            else:
                camera_names = [f"camera{i+1}" for i in range(len(self.pipeline_manager.video_sources))]
            camera_info = {
                "active_cameras": camera_names,
                "active_camera_for_ui": camera_names[0] if camera_names else None,
                "total": len(camera_names),
                "timestamp": time.time(),
                "status": "active"
            }
            self.logger.info(f"Active cameras (not published): {len(camera_names)} active cameras: {camera_names}")
        else:
            camera_info = {
                "active_cameras": [],
                "active_camera_for_ui": None,
                "total": 0,
                "timestamp": time.time(),
                "status": "pipeline_stopped"
            }
        
        self.logger.debug(f"Prepared active camera info: {camera_info}")
        return camera_info

    def stop(self):
        self.logger.info("Command Listener: Stopping..")
        if self.client:
            self.client.on_message = None
    

