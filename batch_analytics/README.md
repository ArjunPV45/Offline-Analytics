# Batch Analytics — Offline Processing for Saved CCTV Footage

Processes saved CCTV segment files (not live RTSP) for one camera channel and
one day at a time: runs person detection + tracking on every segment
sequentially, times how long it actually takes, and writes a JSON report.
Built for answering "can this Pi + Hailo8L process a full day of footage for
one camera in a day (or faster)?"

## Why this exists

The live app (`../main.py`, `../zone_counter.py`, `../inference_engine.py`)
processes RTSP streams in real time and pushes zone/line events to MQTT and a
production analytics API. This is a different job: reprocessing already-saved
`.mp4` files from `/home/hailopi/Analytics/Videos/<channel>/<date>/` (an NFS
mount), one camera/day at a time, with no live RTSP streams involved. It does
now have its own MQTT integration for the frontend platform — see below —
separate from the live app's, since "what's available to process" is a
different question than "what live cameras are connected."

## Usage

```bash
source ../../setup_env.sh   # from repo root: source setup_env.sh
cd UrbanRAIN_COUNTER-main
python3 run_batch_analytics.py --channel ch01 --date 2026-08-17
```

Output: `batch_reports/ch01_2026-08-17.json` (override with `--output-dir`).
Videos root defaults to `/home/hailopi/Analytics/Videos` (override with
`--videos-root`). Any extra `get_pipeline_parser()` flag (e.g. `--arch`,
`--hef-path`) can be appended and is passed straight through.

`--analysis-fps N` skips frames down to roughly N per second before
scaling/inference/tracking/overlay, cutting processing time proportionally —
useful since 15-25fps analysis is overkill when 5-7fps is enough. Default:
process every real frame (no skipping).

`run_batch_analytics_parallel.py` runs multiple channels concurrently (same
day) instead of one at a time — see its own docstring; stick to 2 concurrent
channels, 3-way was found unstable under sustained load even with the
scheduler fix (see `.hailo/memory/common_pitfalls.md`).

`run_batch_analytics_all_days.py` runs one or more channels through their
*entire* backlog — every day folder found under a channel, in order — without
needing `--date` at all:
```bash
python3 run_batch_analytics_all_days.py --channel ch01 --channel ch02
```
Deliberately sequential (one day, one channel, at a time — never concurrent)
to avoid the multi-process device-sharing risk `_parallel.py` carries. Days
that already have a report are skipped by default, so it's safe to re-run
after an interruption; pass `--reprocess` to redo everything anyway.

## Drawing zones and lines

`draw_zone.py` is an interactive tool (needs a real display — this Pi's own
desktop session, or VNC/X-forwarding to it) that grabs a reference frame from
a channel's footage and lets you draw zone rectangles and crossing lines with
the mouse:

```bash
python3 draw_zone.py --channel ch01
```

Controls: `z`/`l` switch between zone/line mode, drag to draw a zone or
click-click to draw a line, buttons or keyboard shortcuts (`u` undo, `r`
reset, `s` save, `q` quit without saving) — buttons are the reliable option
since some window managers don't hand a newly-opened window keyboard focus
automatically. Saves to `batch_analytics/zone_configs/<channel>.json`.
Re-running it for a channel that already has a saved config loads and shows
the existing zones/lines first, so you can see what's there before adding
more (a mismatched `--width`/`--height` vs. the saved config prints a
warning, since coordinates only mean what they were drawn against at that
resolution).

`run_batch_analytics.py` automatically loads that file for the matching
channel if it exists (override with `--zone-config <path>`, or skip it for a
detection-only run with `--no-zones`). It also auto-forces `--width`/`--height`
to match the resolution the zones were drawn at, since zone coordinates only
mean what they were drawn against at that specific resolution — overriding
`--width`/`--height` yourself prints a warning rather than silently producing
wrong counts.

With `--display`, configured zones/lines are drawn live in the video window
alongside detection boxes and track IDs, labeled with their running in/out
counts (e.g. `zoneA in:3 out:2`) so you can visually confirm the zone is
where you expect and watch the count update as people cross it — this reuses
`hailooverlay`'s own rendering (zones are injected as synthetic detection
objects) rather than a second, separate display path, since OpenCV's own GUI
has proven unreliable on this Pi's desktop (see `draw_zone.py`'s docstring
and `.hailo/memory/common_pitfalls.md`).

## Zone/line counting algorithm

`zone_counter_offline.py`'s counting logic is ported from `GPUvarient-main`
(a live, GPU/RTSP retail-analytics system — `core/camera_processor.py`'s
`AdvancedZoneCounter`/`LineCounter`), not written from scratch. It's a real
5-state machine (unknown/entering/inside/exiting/outside) with two properties
the earlier dwell-timer version didn't have:
- **Baseline occupancy**: whoever is already inside a zone the first time
  they're observed doesn't fire a spurious entry (and won't fire an exit
  later either, since they were never "counted").
- **Spatial/temporal ID re-linking**: a brand-new track_id appearing inside a
  zone is checked against any recently-departed "open visit" (same zone,
  still counted, within `zone_id_link_max_sec` and `zone_spatial_match_px`)
  and treated as a continuation rather than a second entry — this absorbs
  hailotracker ID switches from occlusion/brief misdetection.

Adapted for offline use: entry/exit confirmation is time-based (seconds), not
GPUvarient's raw frame counts — our source footage is VFR and `--analysis-fps`
can skip frames, so a frame-count threshold would mean a different real-world
dwell time depending on processing settings. Line crossing uses proper
segment-intersection (CCW test) instead of a side-of-line heuristic.
Not ported: cross-camera Re-ID, demographics, heatmaps, and anything
live-stream-specific (placeholder/frozen-frame detection, MQTT, cloud API
posting) — out of scope for offline single-channel file processing, at least
for now.

## Report schema

```json
{
  "channel": "ch01",
  "date": "2026-08-17",
  "segment_count": 16,
  "total_video_seconds": 35568.0,
  "total_wall_seconds": 6584.2,
  "realtime_factor": 5.40,
  "total_person_detections": 1532,
  "total_unique_track_ids_per_segment": 214,
  "segments": [ { "filename": "...", "nominal_duration_s": ..., "wall_processing_time_s": ..., "realtime_factor": ..., "frames_processed": ..., "person_detections": ..., "unique_track_ids": ... } ],
  "zones": {},
  "lines": {}
}
```

`realtime_factor` > 1 means faster than real time — e.g. `5.4` means an hour
of footage takes about 11 minutes to process. To estimate a full day: budget
`24 / realtime_factor` hours of wall-clock time on this hardware.

## Architecture

- `video_catalog.py` — parses `chXX_YYYYMMDDTHHMMSS_HHMMSS.mp4` filenames,
  sorts segments chronologically, handles midnight rollover.
- `batch_pipeline.py` — a `GStreamerDetectionApp` subclass (official
  hailo_apps framework: `INFERENCE_PIPELINE_WRAPPER`, `TRACKER_PIPELINE`,
  `USER_CALLBACK_PIPELINE`). Overrides `on_eos()` to advance through the
  day's file queue instead of looping the same file (the framework's default
  file-source behavior), and writes the JSON report once the queue empties.
- `zone_counter_offline.py` — zone/line dwell-time and line-crossing counter,
  ported from `../zone_counter.py` but with all MQTT/`analytics_poster.py`
  network calls removed (that module POSTs every event to a live production
  API — unsuitable for reprocessing historical footage) and scoped to one
  camera per run.

## Known limitations

- **Zones/lines are opt-in per channel** — draw them with `draw_zone.py`
  (see above). Without a saved config for a channel, `zones`/`lines` in the
  report stay empty (detection/tracking + timing only).
- **Track IDs reset at every segment file boundary.** Each file boundary
  fully destroys and recreates the GStreamer pipeline (needed to advance
  `filesrc` to the next file), which also resets `hailotracker`'s state. A
  person present exactly at a segment cut gets a new track ID in the next
  file. This doesn't affect the timing benchmark or aggregate per-day
  detection/track counts, but it does mean zone dwell-time and line in/out
  counts can be off by a small amount right at segment boundaries. If exact
  cross-boundary accuracy becomes a requirement, this would need a
  persistent-tracker redesign (e.g. feeding all of a day's frames through
  one long-lived pipeline instead of per-file pipelines).
- **Per-file pipeline rebuild overhead is real and included in the numbers**,
  not hidden — each segment boundary reloads the HEF/model, which costs time.
  This is a genuine cost of processing many small segment files rather than
  one continuous stream; it's measured, not optimized away.
- **These source files are variable-frame-rate (VFR)**, not steady 25fps
  despite container metadata suggesting a nominal rate. The pipeline
  deliberately does **not** use the framework's `SOURCE_PIPELINE()` for this
  reason — see the "SOURCE_PIPELINE's videorate Pathologically Duplicates
  Frames on VFR Files" entry in `.hailo/memory/common_pitfalls.md` for what
  went wrong and why. `frames_processed` per segment reflects actual decoded
  frames, which is why it doesn't match a naive `duration * 25fps` estimate.

## Platform integration (MQTT in, HTTP out)

`platform_integration.py` handles requests from the frontend platform.
Requests come in over MQTT; **responses go out as HTTP POSTs, not MQTT
publishes** — deliberate, since a response can carry a sizeable
base64-encoded image, which doesn't belong on a message broker.

Full contract (topics, payload schemas, field-by-field notes) is in
[`MQTT_API.md`](MQTT_API.md) — that's the doc to hand a frontend developer.
Summary of what's implemented:

- **`channels_request`** → channels response: "what's available to
  process" — channels + their day folders, not individual files. Offline
  equivalent of the live app's `push_cameras`/`push_cameras_response`, since
  there's no dynamic camera_add/camera_remove for saved files. Channels are
  only recognized if they match the `chNN` naming convention and contain
  `YYYY-MM-DD` day folders — the videos root is a shared NFS mount, and
  unrelated things can end up there (found during testing: an unrelated
  cloned project sitting alongside the real channel folders). **Only
  shows days that haven't been processed yet** — `discover_available_channels()`
  (`channel_discovery.py`) excludes any day that already has a report at
  `<BATCH_REPORTS_DIR>/<channel>_<date>.json` (default `batch_reports/`,
  override via `.env`'s `BATCH_REPORTS_DIR`). A channel with nothing left
  to process drops out of the response entirely. This is a cheap
  existence check on the report file, not a strict correctness check —
  it does not verify every video file in the folder was actually covered.
- **`snapshot_request`** (per channel, addressed by **channel name** like
  `ch01`, not a numeric camera_id — camera_id isn't guaranteed to exist for
  every channel) → snapshot response: a reference frame from that channel's
  footage, with any already-configured zones/lines **drawn on it** (reuses
  `zone_overlay_render.py` — same visual style as the live system's
  `zone_manager.render_zones`), so the platform sees current state rather
  than a blank frame.
- **`zone_config`/`line_config`** (per channel) → save ack: saves
  platform-drawn zone/line coordinates to
  `batch_analytics/zone_configs/<channel>.json` — the same file
  `run_batch_analytics.py` already reads and `draw_zone.py` already writes
  locally, so a config drawn remotely via the platform is immediately usable
  by the batch pipeline. Accepts the same flexible coordinate shapes the
  live system's `zone_manager.py` does (`zone_payload_normalize.py`).
  Replaces all zones (or lines) for that channel per message — sending an
  empty list clears them — but a `zone_config` message never touches that
  channel's lines and vice versa. If a channel already has a saved config,
  its stored resolution is protected: a follow-up message with a different
  `width`/`height` is ignored (logged as a warning) rather than silently
  misaligning the zones/lines that were already saved.
- **Processed-day results** (not MQTT-triggered): once
  `run_batch_analytics.py` finishes a channel/day, it automatically pushes
  that day's full report (`day_result_push.py`, called from
  `batch_pipeline.py`'s `_write_day_report()`) — best-effort, a failed or
  unconfigured push never fails the batch run itself, the local JSON report
  is unaffected either way.

Setup:
```bash
cp .env.example .env    # from UrbanRAIN_COUNTER-main/, then fill in real values
cp batch_analytics/channel_camera_ids.example.json batch_analytics/channel_camera_ids.json
# edit channel_camera_ids.json with the real numeric camera_id per channel
python3 -m batch_analytics.platform_integration
```
`camera_id` is `null` for any channel missing from `channel_camera_ids.json`
— expected, not an error, until every channel is mapped. Any of
`CHANNELS_API_URL`/`SNAPSHOT_API_URL`/`ZONE_LINE_CONFIG_API_URL`/
`PROCESSED_DAY_API_URL` unset behaves the same way: the request/event is
still received and logged, the response just can't be pushed anywhere until
that URL is configured.

Not yet implemented (deliberately deferred, needs further scoping before
building): per-event pushing (individual timestamped entry/exit/line-crossing
events, as opposed to the whole-day totals the processed-day-results push
already includes).

## Deleting processed footage (`cleanup_processed_videos.py`)

Once a channel/day is processed, its source `.mp4`s can eventually be
deleted to free disk space. This is a **manual, deliberately conservative**
tool — nothing in this pipeline deletes video on its own or on a schedule.

Safety logic lives in `batch_analytics/day_completion.py`
(`day_is_safe_to_delete()`) and every one of these must hold before a
single file is touched:

1. A report exists for that channel/day (`batch_reports/<channel>_<date>.json`) and is valid JSON with a non-empty `segments` list.
2. At least the grace period (default **4 days**, `--grace-days` to change) has passed since the report was written.
3. **Every `.mp4` currently in that day's folder is one the report actually covered.** If the NFS recorder added segments to that day *after* processing — this happens, see below — the whole day is left alone, not partially cleaned up.

Only files that pass check 3 are ever deleted, never a blanket folder
delete; the day folder itself is only removed via `rmdir()` (which fails
harmlessly if anything unexpected is still in it) after its files are gone.

Dry-run is the default — it always runs first and nothing is deleted
unless you pass `--delete`:
```bash
python3 cleanup_processed_videos.py                    # dry-run, all channels
python3 cleanup_processed_videos.py --channel ch01      # dry-run, one channel
python3 cleanup_processed_videos.py --delete            # actually delete what's eligible
python3 cleanup_processed_videos.py --delete --grace-days 7
```
Every actual deletion is appended to `batch_analytics/deletion_audit.log`
(one JSON line per channel/day: timestamp, files deleted, bytes freed)
before those files are removed, so there's a record independent of
whatever's left on disk.

**Verified against real data while building this**: `ch01/2026-08-17`'s
report covered 16 segments, but the folder had 20 `.mp4`s on disk — 4 more
had been recorded after that day was processed. Check 3 above caught it
and blocked the whole day automatically; a naive "report exists, so
delete the folder" approach would have destroyed 4 never-analyzed
segments.
