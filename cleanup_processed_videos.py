#!/usr/bin/env python3
"""Deletes source .mp4 footage for channel/days that have already been
analyzed, once a safety grace period has passed. Every decision is made by
batch_analytics.day_completion.day_is_safe_to_delete() -- see that module
for the exact conditions; this script does no deleting logic of its own.

SAFE BY DEFAULT: dry-run unless --delete is passed. Even with --delete,
only the specific files verified as covered by that day's report are
removed -- never a blanket folder delete. Every actual deletion is
appended to deletion_audit.log before the file is removed.

    python3 cleanup_processed_videos.py                       # dry-run, all channels
    python3 cleanup_processed_videos.py --channel ch01         # dry-run, one channel
    python3 cleanup_processed_videos.py --delete                # actually delete
    python3 cleanup_processed_videos.py --delete --grace-days 7 # longer grace period

This is not scheduled/cron'd anywhere -- run it by hand, review the
dry-run output, then run again with --delete.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from batch_analytics.day_completion import DEFAULT_GRACE_DAYS, day_is_safe_to_delete

_CHANNEL_NAME_RE = re.compile(r"^ch\d+$")
_DATE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DEFAULT_VIDEOS_ROOT = os.getenv("VIDEOS_ROOT", "/home/hailopi/Analytics/Videos")
DEFAULT_REPORTS_DIR = os.getenv("BATCH_REPORTS_DIR", "batch_reports")
AUDIT_LOG_PATH = Path(__file__).parent / "batch_analytics" / "deletion_audit.log"


def discover_day_dirs(videos_root: Path, channels: list[str] | None) -> list[tuple[str, str]]:
    """Returns (channel, date) pairs for every day folder on disk, restricted
    to `channels` if given."""
    pairs = []
    if not videos_root.is_dir():
        return pairs
    for channel_dir in sorted(videos_root.iterdir()):
        if not channel_dir.is_dir() or not _CHANNEL_NAME_RE.match(channel_dir.name):
            continue
        if channels and channel_dir.name not in channels:
            continue
        for day_dir in sorted(channel_dir.iterdir()):
            if day_dir.is_dir() and _DATE_NAME_RE.match(day_dir.name):
                pairs.append((channel_dir.name, day_dir.name))
    return pairs


def append_audit_log(entries: list[dict]) -> None:
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG_PATH.open("a") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--videos-root", default=DEFAULT_VIDEOS_ROOT)
    parser.add_argument("--reports-dir", default=DEFAULT_REPORTS_DIR)
    parser.add_argument("--channel", action="append", default=None, help="Restrict to this channel (repeatable). Default: all channels.")
    parser.add_argument("--grace-days", type=float, default=DEFAULT_GRACE_DAYS)
    parser.add_argument("--delete", action="store_true", help="Actually delete files. Without this flag, nothing is ever removed.")
    args = parser.parse_args()

    videos_root = Path(args.videos_root)
    reports_dir = Path(args.reports_dir)

    day_pairs = discover_day_dirs(videos_root, args.channel)
    if not day_pairs:
        print(f"No channel/day folders found under {videos_root}" + (f" for channel(s) {args.channel}" if args.channel else ""))
        return

    eligible = []
    blocked = []
    for channel, date_str in day_pairs:
        decision = day_is_safe_to_delete(channel, date_str, videos_root, reports_dir, grace_days=args.grace_days)
        (eligible if decision.safe else blocked).append(decision)

    print(f"Scanned {len(day_pairs)} channel/day folder(s) under {videos_root}")
    print(f"Grace period: {args.grace_days} day(s) since report was written\n")

    print(f"BLOCKED (will not be touched): {len(blocked)}")
    for d in blocked:
        print(f"  {d.channel}/{d.date}: {d.reason}")

    print(f"\nELIGIBLE for deletion: {len(eligible)}")
    total_files = 0
    total_bytes = 0
    for d in eligible:
        size = sum(p.stat().st_size for p in d.deletable_files)
        total_files += len(d.deletable_files)
        total_bytes += size
        print(f"  {d.channel}/{d.date}: {len(d.deletable_files)} file(s), {size / 1e9:.2f} GB -- {d.reason}")

    print(f"\nTotal: {total_files} file(s), {total_bytes / 1e9:.2f} GB")

    if not eligible:
        return

    if not args.delete:
        print("\nDRY RUN -- nothing was deleted. Re-run with --delete to actually remove these files.")
        return

    print(f"\n--delete passed: removing {total_files} file(s) now.")
    audit_entries = []
    for d in eligible:
        deleted_names = []
        freed = 0
        for p in d.deletable_files:
            try:
                size = p.stat().st_size
                p.unlink()
                deleted_names.append(p.name)
                freed += size
            except OSError as exc:
                print(f"  ERROR deleting {p}: {exc}")

        day_dir = videos_root / d.channel / d.date
        try:
            day_dir.rmdir()
            folder_removed = True
        except OSError:
            # Not empty (e.g. an uncovered file appeared mid-run) or already gone --
            # leave it alone rather than force-removing anything.
            folder_removed = False

        audit_entries.append({
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "channel": d.channel,
            "date": d.date,
            "deleted_files": deleted_names,
            "freed_bytes": freed,
            "grace_days": args.grace_days,
            "report_age_days": d.report_age_days,
            "day_folder_removed": folder_removed,
        })
        print(f"  {d.channel}/{d.date}: deleted {len(deleted_names)} file(s), freed {freed / 1e9:.2f} GB"
              + (" (folder removed)" if folder_removed else ""))

    append_audit_log(audit_entries)
    print(f"\nDone. Audit log: {AUDIT_LOG_PATH}")


if __name__ == "__main__":
    main()
