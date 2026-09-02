"""Decides whether a processed channel/day is safe to delete source video
for. This is the one place in the offline pipeline that destroys data, and
unrecoverably so if it's wrong -- every check here is a reason to refuse,
not a reason to proceed. When in doubt, don't delete.

Real case this guards against (found while building this, on real data):
ch01/2026-08-17's report said 16 segments, but the folder on disk had 20
.mp4 files -- the NFS recorder added 4 more segments to that day *after*
the report was generated. Trusting "this day has a report so it's done"
would have deleted 4 never-analyzed files. See the disk-vs-report check
below.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_GRACE_DAYS = 4


@dataclass
class DeletionDecision:
    channel: str
    date: str
    safe: bool
    reason: str
    deletable_files: list[Path] = field(default_factory=list)
    report_path: Path | None = None
    report_age_days: float | None = None


def day_is_safe_to_delete(
    channel: str,
    date_str: str,
    videos_root: str | Path,
    reports_dir: str | Path,
    grace_days: int = DEFAULT_GRACE_DAYS,
) -> DeletionDecision:
    """Every condition below must hold, or deletion is refused:

    1. A report exists for this channel/day and is valid JSON.
    2. The report has a non-empty `segments` list, each with a `filename`.
    3. At least `grace_days` days have passed since the report was written.
    4. Every .mp4 currently in the day's folder is named in the report's
       segments -- if the recorder added files after processing (it has),
       this day is left alone entirely, not partially cleaned up.

    Only on passing all four does it return safe=True, with the exact list
    of files it verified are covered by the report (never "everything in
    the folder" -- delete only what was explicitly checked).
    """
    day_dir = Path(videos_root) / channel / date_str
    report_path = Path(reports_dir) / f"{channel}_{date_str}.json"

    if not day_dir.is_dir():
        return DeletionDecision(channel, date_str, False, "video folder does not exist (already gone?)")

    if not report_path.exists():
        return DeletionDecision(channel, date_str, False, "no report found -- not processed yet")

    try:
        report = json.loads(report_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return DeletionDecision(channel, date_str, False, f"report unreadable/corrupt: {exc}", report_path=report_path)

    segments = report.get("segments")
    if not segments:
        return DeletionDecision(channel, date_str, False, "report has no segments -- treating as incomplete/corrupt", report_path=report_path)

    report_filenames: set[str] = set()
    for seg in segments:
        name = seg.get("filename") if isinstance(seg, dict) else None
        if not name:
            return DeletionDecision(channel, date_str, False, "report has a malformed segment entry (missing filename)", report_path=report_path)
        report_filenames.add(name)

    mtime = datetime.fromtimestamp(report_path.stat().st_mtime, tz=timezone.utc)
    age_days = (datetime.now(tz=timezone.utc) - mtime).total_seconds() / 86400.0

    if age_days < grace_days:
        return DeletionDecision(
            channel, date_str, False,
            f"grace period not elapsed yet ({age_days:.1f}/{grace_days} days since report)",
            report_path=report_path, report_age_days=age_days,
        )

    disk_files = sorted(day_dir.glob("*.mp4"))
    disk_filenames = {p.name for p in disk_files}
    uncovered = disk_filenames - report_filenames
    if uncovered:
        return DeletionDecision(
            channel, date_str, False,
            f"{len(uncovered)} file(s) on disk are not covered by the report "
            f"(added after processing?) -- refusing to touch this day: {sorted(uncovered)}",
            report_path=report_path, report_age_days=age_days,
        )

    deletable = [p for p in disk_files if p.name in report_filenames]
    return DeletionDecision(
        channel, date_str, True,
        f"report is {age_days:.1f} days old, all {len(deletable)} file(s) on disk match the report",
        deletable_files=deletable, report_path=report_path, report_age_days=age_days,
    )
