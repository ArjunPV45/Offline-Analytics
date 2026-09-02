"""Runs run_batch_analytics.py / run_batch_analytics_all_days.py as background
subprocesses on behalf of platform_integration.py's process_request handlers,
so the platform can trigger batch processing remotely instead of someone
SSHing in to run these scripts by hand.

Subprocess, not in-process import, for the same reason run_batch_analytics_parallel.py
and run_batch_analytics_all_days.py already do it that way: each run owns its
own GStreamer pipeline / Hailo device session end-to-end (GStreamerApp.run()
calls sys.exit() when done) and would fight platform_integration.py's own
MQTT event loop for the process's GLib main loop.

Only one job runs at a time, system-wide -- run_batch_analytics_parallel.py's
own docstring documents 3-way concurrency as unstable on this hardware
(HailoRT scheduler starvation under sustained load), and a platform-triggered
job has no operator watching it to notice trouble. A process_request that
arrives while a job is already running is rejected (status "busy") rather
than queued, so the frontend always knows whether its request actually
started rather than silently waiting behind an invisible queue.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from hailo_apps.python.core.common.hailo_logger import get_logger

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent


class BatchJobManager:
    """Serializes run_batch_analytics(_all_days).py subprocess launches.

    `on_job_finished(job, returncode)` is called (from a background thread)
    once a submitted job's subprocess exits, where `job` is the dict `submit()`
    returned for that job.
    """

    def __init__(
        self,
        videos_root: str | None = None,
        reports_dir: str | None = None,
        on_job_finished: Callable[[dict[str, Any], int], None] | None = None,
    ):
        self._lock = threading.Lock()
        self._current: dict[str, Any] | None = None
        self._videos_root = videos_root
        self._reports_dir = reports_dir
        self._on_job_finished = on_job_finished

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._current) if self._current else {"running": False}

    def submit(self, channels: list[str], date: str | None = None) -> dict[str, Any]:
        """Starts a job processing `channels` (a single channel's one day if
        `date` is given, otherwise each channel's full backlog). Returns a
        dict describing the outcome -- `status` is "started" or "busy".
        """
        with self._lock:
            if self._current is not None:
                return {"status": "busy", "running_job": dict(self._current)}

            cmd = self._build_command(channels, date)
            logger.info("Starting batch job: %s", " ".join(cmd))
            proc = subprocess.Popen(cmd, cwd=REPO_ROOT)

            job = {"channels": channels, "date": date, "pid": proc.pid}
            self._current = job
            threading.Thread(target=self._wait_for, args=(job, proc), daemon=True).start()
            return {"status": "started", **job}

    def _build_command(self, channels: list[str], date: str | None) -> list[str]:
        if date:
            if len(channels) != 1:
                raise ValueError("a specific date can only be processed for a single channel")
            cmd = [
                sys.executable, "-u", str(REPO_ROOT / "run_batch_analytics.py"),
                "--channel", channels[0], "--date", date,
            ]
        else:
            cmd = [sys.executable, "-u", str(REPO_ROOT / "run_batch_analytics_all_days.py")]
            for channel in channels:
                cmd += ["--channel", channel]

        if self._videos_root:
            cmd += ["--videos-root", self._videos_root]
        if self._reports_dir:
            cmd += ["--output-dir", self._reports_dir]
        return cmd

    def _wait_for(self, job: dict[str, Any], proc: subprocess.Popen) -> None:
        returncode = proc.wait()
        logger.info(
            "Batch job for channel(s) %s finished (exit code %s)",
            ",".join(job["channels"]), returncode,
        )
        with self._lock:
            self._current = None
        if self._on_job_finished:
            self._on_job_finished(job, returncode)
