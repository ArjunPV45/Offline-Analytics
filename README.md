# Zone Visitor Counter (Hailo + Raspberry Pi)

Zone/line-crossing visitor counting on **Raspberry Pi + Hailo-8/8L**, offline
batch mode:

- **Offline batch** (`batch_analytics/`) — reprocesses **already-recorded** `.mp4`
  footage (e.g. an NFS-mounted CCTV archive, one channel/day at a time), applying
  zone/line counting logic, and can push results and take zone/line
  configuration from a frontend platform over MQTT + HTTP.

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
| `VIDEOS_ROOT` | Root of the recorded-footage archive (`<channel>/<date>/*.mp4`) |
| `BATCH_REPORTS_DIR` | Where per-day JSON reports are written (default `batch_reports/`) |
| `CHANNELS_API_URL`, `SNAPSHOT_API_URL`, `ZONE_LINE_CONFIG_API_URL`, `PROCESSED_DAY_API_URL` | HTTP endpoints the batch pipeline POSTs responses to |

See [`batch_analytics/MQTT_API.md`](batch_analytics/MQTT_API.md) for the full
request/response contract if you're integrating a frontend platform.

`batch_analytics/zone_configs/*.json` and `batch_analytics/channel_camera_ids.json`
are runtime state, gitignored on purpose — they hold this specific
deployment's camera/zone layout, not code, and are created the first time you
draw a zone or add a camera.

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
│   ├── day_completion.py         # Safety checks for video deletion (see below)
│   ├── video_catalog.py, channel_discovery.py, reference_frame.py,
│   │   zone_config_io.py, zone_overlay_render.py, zone_payload_normalize.py,
│   │   day_result_push.py        # Supporting modules
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

**Frontend platform integration** (MQTT requests in, HTTP responses out —
channel discovery, remote zone/line drawing, processed-day results):
```bash
python3 -m batch_analytics.platform_integration
```
See [`batch_analytics/MQTT_API.md`](batch_analytics/MQTT_API.md) for the full
topic/payload reference.

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
