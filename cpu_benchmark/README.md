# CPU-Only Inference Benchmark

How fast can this Pi's 4-core CPU alone (no Hailo8L) run a YOLO detection
model — for direct comparison against the Hailo8L-accelerated numbers in
`../batch_analytics/`.

Runs in its own venv, deliberately separate from `venv_hailo_apps` — torch/
torchvision/ultralytics are heavy, CPU-inference-specific packages that don't
belong mixed into the Hailo pipeline's environment.

## Setup (one-time)

```bash
cd UrbanRAIN_COUNTER-main/cpu_benchmark
python3 -m venv venv_cpu_bench
source venv_cpu_bench/bin/activate
pip install --upgrade pip
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install ultralytics opencv-python-headless
```

`torch` and `torchvision` **must** come from the same `download.pytorch.org/whl/cpu`
index — installing `ultralytics` first can quietly pull in a mismatched
`torchvision` from PyPI and crash with `RuntimeError: operator
torchvision::nms does not exist`. If that happens:
```bash
pip install --force-reinstall torchvision --index-url https://download.pytorch.org/whl/cpu
```

## Usage

```bash
source venv_cpu_bench/bin/activate

# Same model architecture as the Hailo8L pipeline (yolov8s) -- the fair comparison.
# Auto-downloads on first use (needs internet), then caches locally.
python3 run_cpu_inference_benchmark.py \
  --video /home/hailopi/Analytics/Videos/ch01/2026-08-10/ch01_20260810T185559_185643.mp4 \
  --model yolov8s.pt --threads 4

# The heavier yolo26m.pt already sitting in ../../GPUvarient-main/ (this is the default
# --model if you omit it) -- not apples-to-apples with yolov8s, but real, already-present.
python3 run_cpu_inference_benchmark.py --video <path> --threads 4

# A whole day's segments in sequence, capped to a quick sanity check:
python3 run_cpu_inference_benchmark.py --dir /home/hailopi/Analytics/Videos/ch01/2026-08-17 --max-frames 200
```

`--threads 4` matters: torch/ultralytics auto-tune their own thread count
based on internal heuristics and it isn't consistent run to run (observed
anywhere from 1 to 4 threads with no flag) — pass `--threads 4` explicitly to
get a fair, reproducible "all 4 cores" measurement, not whatever the library
happened to pick.

## What it measures

Two numbers, since they answer different questions:
- **inference-only fps** — time inside the `model()` call only (preprocess +
  forward pass + NMS). The fair comparison against the Hailo8L chip's own
  inference throughput.
- **wall fps** — includes video decode too. The actually-achievable
  end-to-end rate on this hardware: on a CPU-only run, decode and inference
  compete for the same 4 cores, unlike the Hailo case where inference runs on
  a separate chip while the CPU only handles decode/scale.

## Results so far (this session, `--threads 4`, ch01/2026-08-10 clip)

| Model | inference-only fps | wall fps |
|---|---|---|
| `yolov8s.pt` (small — same arch as the Hailo pipeline) | 2.02 | 1.67 |
| `yolo26m.pt` (medium — heavier, not apples-to-apples) | 0.76 | 0.67 |

For contrast: the Hailo8L-accelerated pipeline (`../batch_analytics/`)
processes the same class of footage at 3-6x *realtime* on this same Pi —
i.e. tens of effective fps, not under 2. That gap is the whole point of
having the Hailo8L chip: a 4-core Cortex-A76 alone can't get anywhere close
to real-time YOLO detection, even for the smaller model.
