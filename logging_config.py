import logging
import logging.handlers
import os
import sys
from pathlib import Path

LOG_DIR = Path("/var/log/people-counter")
LOG_DIR_FALLBACK = Path("./logs")  

try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR
except PermissionError:
    LOG_DIR_FALLBACK.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR_FALLBACK
    


APP_LOG = log_path / "app.log"
ERROR_LOG = log_path / "error.log"
PERFORMANCE_LOG = log_path / "performance.log"

MAX_BYTES = 10 * 1024 * 1024  
BACKUP_COUNT = 5  

DEBUG_MODE = os.getenv("DEBUG_MODE", "False").lower() == "true"
ENABLE_CONSOLE_LOG = os.getenv("ENABLE_CONSOLE_LOG", "True").lower() == "true"


class PerformanceFilter(logging.Filter):
    def filter(self, record):
        return 'fps' in record.getMessage().lower() or 'memory' in record.getMessage().lower()


class ExcludePerformanceFilter(logging.Filter):
    def filter(self, record):
        return 'fps' not in record.getMessage().lower() and 'memory' not in record.getMessage().lower()


def setup_logging():
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
    
    root_logger.handlers.clear()
    if ENABLE_CONSOLE_LOG:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
        console_handler.addFilter(ExcludePerformanceFilter())
        
        console_formatter = logging.Formatter(
            fmt='%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        console_handler.setFormatter(console_formatter)
        root_logger.addHandler(console_handler)
        root_logger.info("Console logging enabled")
    else:
        root_logger.info("Console logging disabled")
    
    app_handler = logging.handlers.RotatingFileHandler(
        filename=APP_LOG,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding='utf-8'
    )
    app_handler.setLevel(logging.INFO)
    app_handler.addFilter(ExcludePerformanceFilter())
    
    app_formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(name)-20s | %(funcName)-15s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    app_handler.setFormatter(app_formatter)
    root_logger.addHandler(app_handler)
    
    error_handler = logging.handlers.RotatingFileHandler(
        filename=ERROR_LOG,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    
    error_formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(name)s.%(funcName)s:%(lineno)d | %(message)s\n%(exc_info)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    error_handler.setFormatter(error_formatter)
    root_logger.addHandler(error_handler)
    
    perf_handler = logging.handlers.RotatingFileHandler(
        filename=PERFORMANCE_LOG,
        maxBytes=5 * 1024 * 1024, 
        backupCount=3,
        encoding='utf-8'
    )
    perf_handler.setLevel(logging.INFO)
    perf_handler.addFilter(PerformanceFilter())
    
    perf_formatter = logging.Formatter(
        fmt='%(asctime)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    perf_handler.setFormatter(perf_formatter)
    root_logger.addHandler(perf_handler)
    
    
    logging.getLogger('gi.repository').setLevel(logging.WARNING)
        
    logging.getLogger('paho.mqtt').setLevel(logging.INFO)
    
    for module in ['zone_counter', 'gstreamer_pipeline', 'video_stream', 
                   'command_listener', 'config', '__main__']:
        logging.getLogger(module).setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
    
    root_logger.info("=" * 80)
    root_logger.info(f"Logging initialized - Mode: {'DEBUG' if DEBUG_MODE else 'PRODUCTION'}")
    root_logger.info(f"Log directory: {log_path}")
    root_logger.info(f"App log: {APP_LOG} (max {MAX_BYTES/1024/1024:.1f}MB, {BACKUP_COUNT} backups)")
    root_logger.info(f"Error log: {ERROR_LOG}")
    root_logger.info(f"Performance log: {PERFORMANCE_LOG}")
    root_logger.info("=" * 80)
    
    return root_logger


def get_logger(name):
    return logging.getLogger(name)

