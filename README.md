# Zone Visitor Counter (Hailo + Raspberry Pi)

Zone/line-crossing visitor counting on **Raspberry Pi + Hailo-8/8L**, offline
batch mode:

- **Offline batch** (`batch_analytics/`) — reprocesses **already-recorded** `.mp4`
  footage (e.g. an NFS-mounted CCTV archive, one channel/day at a time), applying
  zone/line counting logic. Runs as one long-lived service
  (`batch_analytics/platform_integration.py`, see "Production deployment"
  below) fully driven by a frontend platform over MQTT + HTTP: channel
  discovery, camera ID mapping, zone/line configuration, and triggering the
  actual processing all happen as platform requests, with results and job
  status pushed back out automatically.

---

## Hardware

| Component | Requirement |
|---|---|
| Compute | Raspberry Pi 5 (or equivalent aarch64 SBC) |
| AI accelerator | Hailo-8 or Hailo-8L, connected via M.2/PCIe |
| Footage source | Pre-recorded `.mp4` files, named `chNN_YYYYMMDDTHHMMSS_HHMMSS.mp4`, one folder per channel per day (`<channel>/<YYYY-MM-DD>/`) — e.g. an NFS-mounted recorder archive |

## Software prerequisites

- Raspberry Pi OS (64-bit), or another aarch64 Linux
- Hailo AI Software Suite (HailoRT + driver) for your device — installed and
  `hailortcli fw-control identify` working, **before** anything below
- Python 3.10+
- GStreamer 1.0 with the Hailo GStreamer plugins (`hailonet`, `hailofilter`,
  `hailotracker`, etc.) — these come from the `hailo-apps` framework install below
- `git`

---

## Installing on a fresh Pi + Hailo device

This project depends on the [`hailo-apps`](https://github.com/hailo-ai/hailo-apps)
framework for the Hailo GStreamer pipeline building blocks
(`hailo_apps.python.core...`) used by the offline batch pipeline. Install that
first, in its own directory, then install this project as a **sibling**
directory that uses the same Python environment — don't nest one inside the
other.

```bash
# 1. Install the Hailo AI Software Suite for your device first (HailoRT, driver)
#    following Hailo's own instructions -- hailortcli fw-control identify
#    should succeed before continuing.

# 2. Install the hailo-apps framework (GStreamer plugins + the hailo_apps
#    Python package used by this project's offline batch pipeline)
git clone https://github.com/hailo-ai/hailo-apps.git
cd hailo-apps
./install.sh
source setup_env.sh      # activates venv_hailo_apps -- always re-run this
                          # from THIS directory before working in either project

# 3. Clone this project as a sibling directory (not inside hailo-apps/) and
#    install its own dependencies into that same activated venv
cd ..
git clone <this-repo-url> urbanrain-counter
cd urbanrain-counter
pip install -r requirements.txt

# 4. Fetch the HEF models the batch pipeline uses
export DEVICE_ARCHITECTURE=HAILO8L   # or HAILO8 -- hailo-apps' setup_env.sh
                                      # doesn't set this legacy variable itself,
                                      # unlike download_resources.sh below expects
./download_resources.sh

# 5. Configure device identity, MQTT broker, and API endpoints
cp .env.example .env
# edit .env with real values (see Configuration below)
```

---

## Configuration

Copy `.env.example` to `.env` and fill in real values:

| Variable | Purpose |
|---|---|
| `PI_UNIQUE_ID` | This device's identity on the MQTT broker |
| `MQTT_BROKER_URL` / `_PORT` / `MQTT_USERNAME` / `MQTT_PASSWORD` | Broker connection |
| `VIDEOS_ROOT` | Root of the recorded-footage archive (`<channel>/<date>/*.mp4`) — the one setting basically guaranteed to differ between devices |
| `BATCH_REPORTS_DIR` | Where per-day JSON reports are written (default `batch_reports/`) |
| `HAILO_ARCH`, `HEF_PATH` | This device's Hailo chip and which compiled model to run — leave blank for hailo-apps' own auto-detected defaults |
| `DEFAULT_ANALYSIS_FPS` | Default frame-skipping rate for batch runs — leave blank to analyze every frame |
| `CHANNELS_API_URL`, `SNAPSHOT_API_URL`, `ZONE_LINE_CONFIG_API_URL`, `PROCESSED_DAY_API_URL`, `PROCESS_STATUS_API_URL` | HTTP endpoints the batch pipeline POSTs responses to |

Every one of these is read in exactly one place, [`batch_analytics/config.py`](batch_analytics/config.py)
— every script imports its settings from there rather than hardcoding a
default, so a new device (different footage path, different Hailo chip or
model) only ever needs `.env` edited, never the code. See
[`batch_analytics/README.md`](batch_analytics/README.md#plug-and-play-device-setup)
for details.

See [`batch_analytics/MQTT_API.md`](batch_analytics/MQTT_API.md) for the full
request/response contract if you're integrating a frontend platform.

`batch_analytics/zone_configs/*.json` and `batch_analytics/channel_camera_ids.json`
are runtime state, gitignored on purpose — they hold this specific
deployment's camera/zone layout, not code, and are created the first time you
draw a zone or add a camera (or the platform sends a `zone_config`/`channel_map`
message — see below).

---

## Production deployment (systemd)

The intended production setup for a device is: clone this repo, fill in
`.env`, mount the NFS footage archive, install the systemd service, and then
walk away — everything from that point on happens because the frontend
platform asked for it, over MQTT, not because someone is SSHed into the
device.

```bash
# 1. Mount (or symlink) the NFS-hosted footage archive at VIDEOS_ROOT,
#    however this device normally does that -- e.g. an /etc/fstab entry:
#    nfs-server:/export/Analytics/Videos  /home/hailopi/Analytics/Videos  nfs  defaults,_netdev  0  0

# 2. Install and enable the service (edit deploy/urbanrain-analytics.service's
#    User/WorkingDirectory/ExecStart first if this repo or the hailo-apps venv
#    aren't at the paths it assumes -- see "Installing on a fresh Pi + Hailo
#    device" above for that layout)
sudo ./deploy/install_service.sh
sudo systemctl start urbanrain-analytics
sudo systemctl status urbanrain-analytics
sudo journalctl -u urbanrain-analytics -f     # follow logs
```

From here, the running service (`python3 -m batch_analytics.platform_integration`)
is the only thing that needs to be running on the device. Everything else is
a request the frontend platform sends over MQTT (full contract in
[`batch_analytics/MQTT_API.md`](batch_analytics/MQTT_API.md)):

| Platform sends | Device does |
|---|---|
| `channels_request` | Reports which channels/days it has footage for on the NFS mount |
| `channel_map` | Assigns (or clears) a channel's numeric `camera_id` — also **opts that channel in** for automatic processing (see `process_request` below) — no more hand-editing `channel_camera_ids.json` after every deployment |
| `snapshot_request` | Sends a reference frame (with any existing zones/lines drawn on it) |
| `zone_config` / `line_config` | Saves zone/line coordinates drawn on that frame |
| `process_request` | Runs batch analytics — one channel/day, one channel's whole backlog, several named channels, or (with no channels named) every channel already added via `channel_map` — a shared NFS archive can hold footage for cameras this device was never asked to analyze, so only explicitly-added channels are processed automatically |

Results (and `process_request`'s started/busy/finished status) get POSTed
back out over HTTP as they happen. `Restart=always` in the unit file means a
crash just restarts the controller, which re-subscribes and picks back up on
the next request — no state is lost, since everything it acts on (zone/line
config, camera ID mapping, which days are already processed) lives on disk,
not in memory.

---

## Project layout

```
.
├── batch_analytics/            # Offline batch pipeline (see its own README)
│   ├── README.md                # Full internal documentation
│   ├── MQTT_API.md               # Frontend integration contract (copy-paste ready)
│   ├── batch_pipeline.py         # GStreamer app: multi-file sequential processing
│   ├── zone_counter_offline.py   # Time-based zone/line counting for batch footage
│   ├── platform_integration.py   # MQTT-in / HTTP-out controller
│   ├── config.py                 # .env-driven settings, single source of truth
│   ├── day_completion.py         # Safety checks for video deletion (see below)
│   ├── video_catalog.py, channel_discovery.py, reference_frame.py,
│   │   zone_config_io.py, zone_overlay_render.py, zone_payload_normalize.py,
│   │   day_result_push.py, job_manager.py  # Supporting modules
│   └── zone_configs/              # Per-channel zone/line layouts (gitignored)
├── run_batch_analytics.py       # Process one channel/day
├── run_batch_analytics_parallel.py  # Process multiple channels concurrently
├── run_batch_analytics_all_days.py  # Process all channels, day-by-day
├── draw_zone.py                  # Interactive Tkinter tool to draw zones/lines
├── cleanup_processed_videos.py   # Deletes processed footage after a safety grace period
├── batch_reports/                 # Per-day JSON output (gitignored)
│
├── cpu_benchmark/                # Standalone CPU-only (no Hailo) inference speed test
│
├── deploy/                       # Production deployment (see above)
│   ├── urbanrain-analytics.service  # systemd unit for platform_integration.py
│   └── install_service.sh           # Installs/enables the unit above
│
├── resources/                    # Postprocess .so libs + .hef models (gitignored, fetched by download_resources.sh)
├── install.sh, download_resources.sh, setup_env.sh
└── .env.example
```

---

## Usage

**Offline batch mode:**
```bash
# Draw zones/lines on a channel's reference frame first
python3 draw_zone.py --channel ch01

# Process one channel/day
python3 run_batch_analytics.py --channel ch01 --date 2026-08-19

# Process several channels concurrently
python3 run_batch_analytics_parallel.py --channel ch01 --channel ch02 --date 2026-08-19

# Process every channel, one day at a time, skipping already-processed days
python3 run_batch_analytics_all_days.py --channel ch01 --channel ch02 --channel ch03
```
Full details, including `--analysis-fps` frame-skipping and zone/line config
format, are in [`batch_analytics/README.md`](batch_analytics/README.md).

**Frontend platform integration** — the single long-running command that
drives the whole pipeline from platform requests (MQTT requests in, HTTP
responses out): channel discovery, remote zone/line drawing, **triggering
processing** (`process_request`, no manual `run_batch_analytics*.py`
invocation needed), and processed-day results:
```bash
python3 -m batch_analytics.platform_integration
```
See [`batch_analytics/MQTT_API.md`](batch_analytics/MQTT_API.md) for the full
topic/payload reference. The `run_batch_analytics*.py` scripts above still
work standalone for local runs/debugging, but production use only needs this
one process running.

**Deleting processed footage** (manual, dry-run by default, 4-day safety grace
period, refuses to touch a day if any file wasn't covered by its report):
```bash
python3 cleanup_processed_videos.py            # dry-run
python3 cleanup_processed_videos.py --delete    # actually delete
```

**CPU-only inference benchmark** (no Hailo device needed):
```bash
cd cpu_benchmark
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install ultralytics
python3 run_cpu_inference_benchmark.py --video /path/to/file.mp4 --model yolov8s.pt
```
See [`cpu_benchmark/README.md`](cpu_benchmark/README.md).

---

## License

Not yet specified — add a `LICENSE` file before making this repository public
if you intend others to reuse the code.
