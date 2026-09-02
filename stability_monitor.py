import time
import requests
import json
import os
import datetime
from pathlib import Path

HEALTH_URL = "http://localhost:8080/health"
LOG_FILE = "logs/stability_test.log"
Path("logs").mkdir(exist_ok=True)

def log_stability(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] {message}\n")

log_stability("Stability test started.")

while True:
    try:
        response = requests.get(HEALTH_URL, timeout=10)
        if response.status_code == 200:
            status = response.json()
            # Extract key metrics
            cpu = status['system']['cpu_percent']
            mem = status['system']['memory_percent']
            active_cams = status['pipeline']['active_cameras']
            uptime = status['uptime_seconds']
            
            summary = f"STATUS: healthy | CPU: {cpu}% | MEM: {mem}% | Active Cams: {active_cams} | Uptime: {uptime}s"
            log_stability(summary)
            print(summary)
        else:
            log_stability(f"STATUS: degraded (HTTP {response.status_code}) | Details: {response.text}")
    except Exception as e:
        log_stability(f"ERROR: Could not reach health endpoint: {e}")
    
    time.sleep(60) # Log every minute
