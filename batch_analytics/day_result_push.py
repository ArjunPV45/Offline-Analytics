"""Pushes a processed day's report (produced by batch_pipeline.py's
_write_day_report) to the platform — the "how does the frontend find out a
channel/day has been processed, and what were the results" piece of the
integration.

Lives in its own module rather than inline in batch_pipeline.py so the
GStreamer pipeline code stays free of HTTP/env concerns — batch_pipeline.py
just calls push_day_result() once it has written the local report.

Deliberately best-effort: if PROCESSED_DAY_API_URL isn't configured, or the
push fails (network down, backend unreachable), the batch run itself is
unaffected — the local JSON report (already written to disk before this is
called) remains the source of truth either way. A batch job should never
fail *because* the network push failed.

GStreamerApp.run() calls sys.exit() once a day's segments are done, so this
push has to happen synchronously inside the still-running process (there's
no "after run() returns" hook to push from instead — see
run_batch_analytics_all_days.py's docstring for the same constraint).
"""

from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv

from hailo_apps.python.core.common.hailo_logger import get_logger

from batch_analytics.platform_integration import load_camera_id_map

logger = get_logger(__name__)
load_dotenv()


def push_day_result(channel: str, date_str: str, report: dict[str, Any]) -> bool:
    api_url = os.getenv("PROCESSED_DAY_API_URL")
    if not api_url:
        logger.debug("PROCESSED_DAY_API_URL not set -- skipping day-result push for %s/%s", channel, date_str)
        return False

    payload = {
        "pi_id": os.getenv("PI_UNIQUE_ID"),
        "channel": channel,
        "camera_id": load_camera_id_map().get(channel),
        "date": date_str,
        **report,
    }

    try:
        resp = requests.post(api_url, json=payload, timeout=15.0)
    except requests.RequestException as e:
        logger.error("Day-result push for %s/%s failed: %s", channel, date_str, e)
        return False

    if 200 <= resp.status_code < 300:
        logger.info("Pushed processed-day result for %s/%s to %s (HTTP %s)", channel, date_str, api_url, resp.status_code)
        return True

    logger.error("Day-result push for %s/%s: %s returned HTTP %s: %s", channel, date_str, api_url, resp.status_code, resp.text[:200])
    return False
