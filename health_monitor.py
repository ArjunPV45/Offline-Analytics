import threading
import time
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import psutil
from datetime import datetime

logger = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(self, user_data, pipeline_manager, port=8080):
        self.user_data = user_data
        self.pipeline_manager = pipeline_manager
        self.port = port
        self.server = None
        self.server_thread = None
        self.last_frame_times = {} 
        self.start_time = datetime.now()
        
    def update_frame_timestamp(self, camera_id):
        self.last_frame_times[camera_id] = time.time()
        logger.debug(f"Updated frame timestamp for camera {camera_id}")
    
    def get_health_status(self):
        current_time = time.time()
        
        stale_cameras = []
        active_cameras = []
        
        for camera_id in self.last_frame_times:
            time_since_frame = current_time - self.last_frame_times[camera_id]
            if time_since_frame > 30:
                stale_cameras.append({
                    "camera_id": camera_id,
                    "seconds_since_frame": int(time_since_frame)
                })
            else:
                active_cameras.append(camera_id)
        
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        try:
            with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                temp = float(f.read().strip()) / 1000.0
        except:
            temp = None
        
        is_healthy = (
            len(stale_cameras) == 0 and
            self.pipeline_manager.is_running() and
            memory.percent < 85 and
            cpu_percent < 90 and
            disk.percent < 90
        )
        
        status = {
            "status": "healthy" if is_healthy else "degraded",
            "timestamp": datetime.now().isoformat(),
            "uptime_seconds": int((datetime.now() - self.start_time).total_seconds()),
            "pipeline": {
                "running": self.pipeline_manager.is_running(),
                "active_cameras": len(active_cameras),
                "stale_cameras": stale_cameras
            },
            "system": {
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "memory_available_mb": memory.available // (1024 * 1024),
                "disk_percent": disk.percent,
                "disk_free_gb": disk.free // (1024 * 1024 * 1024),
                "cpu_temp_c": temp
            },
            "device_id": os.getenv("PI_UNIQUE_ID", "pi-default")
        }
        
        return status
    
    def start(self):
        monitor = self
        
        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/health':
                    status = monitor.get_health_status()
                    
                    self.send_response(200 if status["status"] == "healthy" else 503)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps(status, indent=2).encode())
                    
                elif self.path == '/metrics':
                    status = monitor.get_health_status()
                    metrics = f"""# HELP pipeline_running Pipeline status
# TYPE pipeline_running gauge
pipeline_running {1 if status['pipeline']['running'] else 0}

# HELP active_cameras Number of active cameras
# TYPE active_cameras gauge
active_cameras {status['pipeline']['active_cameras']}

# HELP cpu_usage_percent CPU usage percentage
# TYPE cpu_usage_percent gauge
cpu_usage_percent {status['system']['cpu_percent']}

# HELP memory_usage_percent Memory usage percentage
# TYPE memory_usage_percent gauge
memory_usage_percent {status['system']['memory_percent']}
"""
                    self.send_response(200)
                    self.send_header('Content-type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(metrics.encode())
                else:
                    self.send_response(404)
                    self.end_headers()
            
            def log_message(self, format, *args):
                pass
        
        HTTPServer.allow_reuse_address = True
        self.server = HTTPServer(('0.0.0.0', self.port), HealthHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        
        logger.info(f" Health monitor started on http://0.0.0.0:{self.port}/health")
    
    def stop(self):
        if self.server:
            self.server.shutdown()
            logger.info("🏥 Health monitor stopped")
