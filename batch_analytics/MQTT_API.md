# Offline Analytics — Platform API (for frontend integration)

This is the contract for the **offline batch analytics** pipeline (processes
saved CCTV footage, not live RTSP). It's a separate device identity from any
live RTSP system (e.g. `../../GPUvarient-main`) — don't assume they share
`pi_id` or behavior.

**Requests go IN over MQTT. Responses come OUT over HTTP**, not MQTT — a
response can carry a sizeable base64-encoded image, which doesn't belong on
a message broker, and this is a deliberate split, not an inconsistency.

**Status: partial.** Everything under "Implemented" below is built and
tested. Everything under "Planned" is not — don't integrate against it yet.

## Connection

**MQTT (for requests):**
- Host/port/auth: ask the backend team for this device's broker connection details.
- Device identity: `<pi_id>` in every topic below is this device's `PI_UNIQUE_ID` — ask the backend team for the actual value.
- Topic pattern: `vision/<pi_id>/<action>` (device-level) or `vision/<pi_id>/<channel>/<action>` (per-channel — note **channel name** like `ch01`, not a numeric camera_id, see below).

**HTTP (for responses):** this device POSTs JSON to backend-provided URLs — ask the backend team which URL corresponds to which response type below.

---

## Implemented

### 1. `channels_request` → channels response

Publish to ask what channels and days of footage this device has.

**Request topic (MQTT):** `vision/<pi_id>/channels_request`
**Request payload:** `{}` (content ignored)

**Response:** HTTP POST to the channels endpoint:
```json
{
  "pi_id": "<pi_id>",
  "channels": [
    {
      "channel": "ch01",
      "camera_id": 174899,
      "days": ["2026-08-10", "2026-08-11", "2026-08-13", "..."],
      "day_count": 21
    },
    {
      "channel": "ch02",
      "camera_id": null,
      "days": ["2026-08-14", "..."],
      "day_count": 18
    }
  ],
  "generated_at": "2026-09-01T12:00:00.000Z"
}
```

Field notes:
- `channel` — the stable identifier (`ch01`, `ch02`, ...). Use this for every other request to this device, not `camera_id`.
- `camera_id` — **may be `null`** if this channel isn't mapped to a numeric ID yet on the backend. Handle `null` gracefully, or assign one with `channel_map` below.
- `days` — `YYYY-MM-DD` strings. **Only days not yet processed by this device** — once a day's analytics finishes (and its report is pushed, see §5 below), that date drops out of this list on the next `channels_request`. A channel with zero remaining unprocessed days is omitted from `channels` entirely.

### 2. `channel_map` → channels response

Assign (or clear) the numeric `camera_id` a channel reports in `channels_request`
responses — remotely, instead of someone SSHing in to hand-edit
`channel_camera_ids.json` after every deployment. Channels themselves are
never created or removed this way (they're fixed by what's actually on the
NFS mount) — this only attaches/detaches the numeric ID the backend already
uses for that channel.

**Request topic (MQTT):** `vision/<pi_id>/channel_map`
**Request payload — single channel:**
```json
{ "channel": "ch01", "camera_id": 174899 }
```
**Request payload — bulk (any number of channels in one message):**
```json
{ "mapping": { "ch01": 174899, "ch02": 174900 } }
```
`channel`/`camera_id` and `mapping` can be combined in one message; a `null`
`camera_id` clears that channel's mapping (it goes back to reporting
`camera_id: null`).

**Response:** the same **channels response** shape as `channels_request`
(§1 above — reused rather than inventing a separate ack schema), reflecting
the mapping immediately after applying it, HTTP POSTed to the channels
endpoint.

### 3. `snapshot_request` (per channel) → snapshot response

Ask for a reference frame from a specific channel — e.g. to show what a camera sees, or as the base image for drawing zones/lines.

**Request topic (MQTT):** `vision/<pi_id>/<channel>/snapshot_request` — `<channel>` is the channel *name* (`ch01`), never a numeric camera_id.
**Request payload (all fields optional):**
```json
{ "date": "2026-08-17", "segment_index": 0 }
```
Omit `date` for the most recent day available. `segment_index` picks which segment file of that day to grab a frame from (default `0`); most callers should omit both.

**Response:** HTTP POST to the snapshot endpoint:
```json
{
  "pi_id": "<pi_id>",
  "channel": "ch01",
  "camera_id": 174899,
  "date": "2026-08-17",
  "segment": "ch01_20260817T085635_085813.mp4",
  "width": 1280,
  "height": 720,
  "zone_count": 1,
  "line_count": 0,
  "image": "<base64 JPEG>",
  "generated_at": "2026-09-01T12:00:00.000Z"
}
```

**Important**: if this channel already has zones/lines configured, the image comes back **with them already drawn on it** (yellow rectangles for zones, cyan lines for lines, each labeled) — not a blank frame. `width`/`height` are the actual dimensions of the returned image — a subsequent `zone_config`/`line_config` for this channel must draw against *these* dimensions (see below).

### 4. `zone_config` / `line_config` (per channel) → ack

Send zone/line coordinates (e.g. drawn on the snapshot above) for this device to save and use when processing that channel.

**Request topics (MQTT):**
- `vision/<pi_id>/<channel>/zone_config`
- `vision/<pi_id>/<channel>/line_config`

**Request payload — zone_config:**
```json
{
  "width": 1280,
  "height": 720,
  "zones": [
    { "name": "Checkout Queue", "x1": 100, "y1": 100, "x2": 300, "y2": 300 }
  ]
}
```

**Request payload — line_config:**
```json
{
  "width": 1280,
  "height": 720,
  "lines": [
    { "name": "Main Door Entry", "x1": 50, "y1": 300, "x2": 590, "y2": 300, "swap": false }
  ]
}
```

Notes:
- `width`/`height` should match whatever a `snapshot_request` for this channel most recently returned — coordinates are meaningless without knowing what resolution they were drawn against. **If this channel already has a saved config, its stored resolution wins** over whatever this message says (protects already-saved zones/lines from getting silently misaligned by a mismatched follow-up message) — send the resolution the *first* snapshot for a channel came back at, and stay consistent with it from then on.
- Also accepts `startX/startY/endX/endY`, or `start`/`end` as `[x, y]` or `{"x":..,"y":..}`, in place of `x1/y1/x2/y2` — same flexible shapes the live system's zone-drawing already supports, in case the same UI component is reused here.
- A `zone_config` message **replaces all zones** for that channel (send `"zones": []` to clear them) but leaves lines untouched. Same for `line_config` and lines — they don't affect each other.
- Any individual zone/line entry with an unrecognized shape is skipped (not fatal to the rest of the message) — check `rejected_count` in the response.

**Response:** HTTP POST to the zone/line-config endpoint:
```json
{
  "pi_id": "<pi_id>",
  "action": "zone_config",
  "channel": "ch01",
  "status": "ok",
  "zone_count": 1,
  "rejected_count": 0,
  "line_count": 0,
  "generated_at": "2026-09-01T12:00:00.000Z"
}
```
(`line_config` responses look the same with `action: "line_config"` and the `zone_count`/`line_count` roles swapped as the "count I just set" / "count that was already there, untouched.")

### 5. Processed-day results (no request — pushed automatically)

Not MQTT-triggered: once this device finishes processing a channel/day
(`run_batch_analytics.py`), it automatically HTTP POSTs that day's full
report to the processed-day-results endpoint. This is how the frontend
finds out a channel/day is done and what the results were.

**Response payload:** the full report from `batch_reports/<channel>_<date>.json`, plus `pi_id`/`channel`/`camera_id`/`date`:
```json
{
  "pi_id": "<pi_id>",
  "channel": "ch01",
  "camera_id": 174899,
  "date": "2026-08-17",
  "segment_count": 16,
  "total_video_seconds": 35568.0,
  "total_wall_seconds": 6584.2,
  "realtime_factor": 5.40,
  "total_person_detections": 1532,
  "total_unique_track_ids_per_segment": 214,
  "segments": [ { "filename": "...", "nominal_duration_s": "...", "wall_processing_time_s": "...", "realtime_factor": "...", "frames_processed": "...", "person_detections": "...", "unique_track_ids": "..." } ],
  "zones": { "Checkout Queue": { "in_count": 12, "out_count": 11 } },
  "lines": {}
}
```
`zones`/`lines` are whole-day in/out totals, not individual timestamped visit events (that's the "planned" item below).

### 6. `process_request` (device-level or per-channel) → status ack

Triggers batch processing remotely — no one needs to SSH in and run
`run_batch_analytics.py` by hand. Only **one job runs at a time** on this
device (concurrent Hailo pipeline runs are unstable on this hardware — see
`batch_analytics/README.md`); a `process_request` that arrives while a job
is already running comes back with `"status": "busy"` instead of starting a
second one — retry later, or watch for the running job's `finished`/`failed`
ack.

**Device-level — process everything (or a specific set of channels):**

**Request topic (MQTT):** `vision/<pi_id>/process_request`
**Request payload (optional):**
```json
{ "channels": ["ch01", "ch02"] }
```
Omit `channels` (or send `{}`) to process every channel that currently has
pending (unprocessed) days — same set `channels_request` would return.
Each named channel's **full pending backlog** is processed, day by day,
sequentially (`run_batch_analytics_all_days.py` under the hood) — not just
the most recent day.

**Per-channel — process one channel's backlog, or a single specific day:**

**Request topic (MQTT):** `vision/<pi_id>/<channel>/process_request`
**Request payload (optional):**
```json
{ "date": "2026-08-17" }
```
Omit `date` to process that one channel's full pending backlog. With `date`,
only that specific day is processed (even if already-processed — a repeat
request just reruns it).

**Response:** HTTP POST to the process-status endpoint, once when the job
starts (or is rejected as busy) and again when it finishes:
```json
{
  "pi_id": "<pi_id>",
  "action": "process_request",
  "status": "started",
  "channels": ["ch01", "ch02"],
  "date": null,
  "pid": 48213,
  "generated_at": "2026-09-01T12:00:00.000Z"
}
```
```json
{
  "pi_id": "<pi_id>",
  "action": "process_request",
  "status": "finished",
  "channels": ["ch01", "ch02"],
  "date": null,
  "exit_code": 0,
  "generated_at": "2026-09-01T13:42:11.000Z"
}
```
`status` is one of `started`, `busy`, `nothing_to_process` (device-level
request with no pending days for any channel), `finished`, or `failed`
(non-zero exit code). This ack is a job-lifecycle signal only — each day's
actual results still arrive separately via the processed-day-results push
(§5) as that day completes, same as if the job had been started by hand.

---

## Planned / not yet available

- **Per-event pushing** (individual entry/exit/line-crossing events with timestamps, as they happen, not just whole-day totals) — not built. `zones`/`lines` in the day-results push above only have aggregate counts for now.
- **Video retention / cleanup** — built (`cleanup_processed_videos.py`), but run by hand, not automatically or on a schedule. Once a day is processed, its source footage is eligible for deletion after a 4-day grace period (configurable), and only if every file currently in that day's folder is one the report actually covered — if the recorder added files after processing, that day is left alone entirely rather than partially cleaned up. No API impact — it's local disk hygiene, not something the platform requests or is told about individually (the day already disappearing from `channels_request` after processing is the only visible signal).
