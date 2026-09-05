# Frigate host-side detector on Linux (Fedora Asahi, Apple M1, Vulkan GPU / CPU)

This is the "Apple Silicon Detector" from the Mac, running on the Mac mini's
Linux install as a **systemd service**. Since 2026-09-05 (evening) it runs the
model **on the M1 GPU via Vulkan (ncnn, fp16)**; ONNX Runtime on the CPU
remains the fallback. Frigate (Docker) talks to it over ZMQ at
`tcp://host.docker.internal:5555`. The same code still runs unchanged on
macOS (`--backend auto` picks CoreML there).

Installed 2026-09-05 (CPU), switched to Vulkan the same day.

## What is installed

| Piece | Where |
| --- | --- |
| Code | `~/frigate/detector/detector/` (`zmq_onnx_client.py`, `backends.py`, `model_util.py`) |
| Python venv | `~/frigate/detector/venv` (system Python 3.14.6, created with `uv`) |
| Packages | `onnxruntime 1.29.0` (CPU EP), `ncnn 1.0.20260526` (pip wheel, Vulkan), `mnn 3.6.1` (converter only), `numpy 2.5.2`, `pyzmq 27.2.0`, `opencv-python-headless 5.0.0.93` — pinned in `requirements-linux.txt` |
| Models | `~/frigate/detector/models/` (`fox-person-cat-320.onnx` is the live one; `*.ncnn.param/.bin` and `*.mnn` next to it are conversion caches) |
| MNN shim | `./mnn_shim/` (C shim + `libmnn_shim.so`, links the source-built `../research/MNN-src/build/libMNN.so`) |
| Converters | `/usr/bin/onnx2ncnn`, `/usr/bin/ncnnoptimize` (Fedora `ncnn` package — used only as external processes) |
| systemd unit | `/etc/systemd/system/frigate-detector.service` (copy kept at `./frigate-detector.service`) |
| Test client | `./test_client.py` |

The unit runs as the login user (`User=`/`Group=` in the unit), `Nice=5`, `Restart=always`, logs to the journal, and starts:

```
venv/bin/python detector/zmq_onnx_client.py --backend vulkan --threads 4 \
    --endpoint tcp://0.0.0.0:5555 --models-dir ~/frigate/detector/models --model AUTO
```

`--model AUTO` means: wait for Frigate's `model_request` handshake, load the named
model from `models/` if present, otherwise accept the bytes Frigate pushes
(`model_data`) and save them there.

Python 3.14 note: onnxruntime 1.29 has aarch64 Linux wheels for 3.14, so no
extra Python was needed. `python3.13` and `python3.12` are also installed on the
box if a future package lacks 3.14 wheels (`uv venv --python 3.13 venv`).

### SELinux
SELinux is enforcing. systemd (`init_t`) may not follow symlinks labelled
`user_home_t`, so the venv's `bin/` directory is persistently labelled `bin_t`:

```
sudo semanage fcontext -a -t bin_t "~/frigate/detector/venv/bin(/.*)?"
sudo restorecon -Rv ~/frigate/detector/venv/bin
```

If you ever recreate the venv, re-run the `restorecon` line (the fcontext rule
persists). Symptom if you forget: `status=203/EXEC ... Permission denied` in
`systemctl status`.

## Backends (`--backend` / `DETECTOR_BACKEND`)

| Name | Engine | Where | fox-person-cat-320, batch 1 |
| --- | --- | --- | --- |
| `vulkan` | alias -> first of `DETECTOR_VULKAN_PREFERENCE` (default `ncnn-vulkan,mnn-vulkan`) that initialises | GPU | |
| `ncnn-vulkan` | ncnn pip wheel, Vulkan (Honeykrisp), fp16 | GPU | **~24 ms** engine, 28-31 ms incl. ZMQ (**live**) |
| `mnn-vulkan` | source-built libMNN via `mnn_shim/`, Vulkan, fp16 | GPU | ~20 ms engine (verified, see below) |
| `ncnn-cpu` | ncnn NEON, fp16 arithmetic | CPU | ~5 ms idle, but collapses under ffmpeg load |
| `cpu` | ONNX Runtime CPU EP, fp32 | CPU | 12 ms idle, 30-45 ms with Frigate's ffmpeg running |
| `coreml`, `onnxruntime` | macOS / explicit ORT providers | - | unchanged |

**Auto-fallback:** if a GPU backend cannot initialise (no Vulkan device, missing
wheel/shim) *or* cannot load/convert a model, the service logs a line starting
with `BACKEND FALLBACK:` and serves that model with `cpu` instead. Nothing else
changes, so grep the journal for `BACKEND FALLBACK` after a Mesa/kernel update.

**Switching:** edit the `--backend` value in the unit (`vulkan`, `ncnn-vulkan`,
`mnn-vulkan`, `ncnn-cpu`, `cpu`), then
`sudo cp frigate-detector.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart frigate-detector`.
Frigate re-handshakes within ~10 s; no container restart.
**Revert to CPU:** set `--backend cpu` (that is the only change) and restart the unit.

Knobs (env vars, all optional): `DETECTOR_NCNN_FP16=0` / `DETECTOR_MNN_FP16=0`
(fp32 on the GPU: bit-for-bit with ORT, ~30 ms / ~21 ms), `DETECTOR_VULKAN_DEVICE=<n>`
(ncnn device index; by default the first hardware GPU is used and llvmpipe is
skipped), `DETECTOR_NCNN_THREADS` (CPU-side helper threads for the GPU path,
default 1), `DETECTOR_VULKAN_PREFERENCE=mnn-vulkan,ncnn-vulkan` (order for the
`vulkan` alias), `DETECTOR_ONNX2NCNN` / `DETECTOR_NCNNOPTIMIZE` / `DETECTOR_MNNCONVERT`
(converter binaries), `DETECTOR_MNN_SHIM` / `DETECTOR_MNN_LIB` (shim / libMNN paths).

### Conversion cache
The protocol still hands the service `.onnx` files. On first load a GPU backend
converts them and caches the result **next to the model** (regenerated whenever
the `.onnx` is newer than the cache; delete the cache files to force it):

* `ncnn-*`: `models/<name>.ncnn.param` + `models/<name>.ncnn.bin` via
  `onnx2ncnn` + `ncnnoptimize` (fp32 weights; fp16 is a runtime option).
* `mnn-vulkan`: `models/<name>.mnn` via the `mnn` wheel's converter extension
  (`_tools.mnnconvert`, run as a subprocess). Deliberately **not** the wheel's
  `mnnconvert` CLI, which reports usage (machine-id, model uuid, args) to
  Alibaba Cloud; set `DETECTOR_MNNCONVERT` to a standalone `MNNConvert` binary
  to bypass the wheel entirely.

The unit's `ReadWritePaths=` already covers `models/`, so this works under the
hardening. Conversion takes well under a second for this model; a model pushed
by Frigate over ZMQ (`model_data`) is converted the same way after it is saved.

Only the fp32 ONNX is a valid source: `fox-person-cat-320-fp16.onnx` is
malformed (fp16 Conv fed by an fp32 input without Casts) and has a different
class set; every backend does fp16 itself at load time.

### Measured 2026-09-05 (Frigate live on 4 cameras during all runs)

Accuracy, `cpu` vs GPU, 45 real frames (21 from today's recordings, 24 Frigate
event snapshots with people), post-processed detections (score > 0.4, NMS):

| | ncnn-vulkan fp16 | mnn-vulkan fp16 |
| --- | --- | --- |
| raw `output0` max / mean abs diff vs ORT | 9.5 / 0.125 (coords in px, 0..320) | 2.7 / 0.045 |
| detections cpu / gpu | 39 / 40 | 39 / 40 |
| frames with class or count mismatch | 1 (same box, score 0.399 cpu vs 0.404 gpu, straddles the 0.4 cut) | 1 (same frame, 0.402) |
| max score delta on matched boxes | 0.015 | 0.005 |
| max box-corner delta on matched boxes | 11 px on two low-score (0.44/0.62) boxes, <1.3 px on all others | 0.39 px |

Soak, `test_client.py --rate 25 --duration 330` (Frigate's worst case: 4 cams x 5 fps)
against a second instance on port 5556, real cam2 frame, round trip incl. ZMQ + NMS:

| backend | requests | timeouts/errors | avg | p50 | p95 | p99 | max | RSS | dmesg |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ncnn-vulkan fp16 | 8251 (25.0/s, 330 s) | 0 / 0 | 30.8 ms | 30.5 | 38.0 | 44.0 | 86.6 | 176.6 MB flat (no growth) | no AGX/GPU lines |
| mnn-vulkan fp16 | 5001 (25.0/s, 200 s) | 0 / 0 | 23.9 ms | 23.2 | 29.5 | 35.7 | 77.6 | 163.2 MB flat | no AGX/GPU lines |

The engine itself (`Inference stats` lines in the journal) averaged 26-29 ms for
ncnn-vulkan at 25 req/s; the process used ~12 % of one core doing it (mostly
driver time), vs. ~40 % of a core for the ORT CPU backend at Frigate's actual
(motion-gated, ~2-8 req/s) rate. Frigate's 200 ms `request_timeout_ms` leaves
a 5x margin. SIGTERM shutdown (what systemd sends) releases the Vulkan objects
and exits in <0.5 s with no kernel messages.

Host CPU before/after the production switch (`top -bn1`, Frigate live on 4 cameras,
detection rate motion-gated so not perfectly controlled):

| | cpu backend (16:49) | ncnn-vulkan (16:55) |
| --- | --- | --- |
| detector process | 39 % of a core (service CPU time 778 s over 33 min) | ~5 % of a core (4.5 s over the first 80 s incl. start-up; not in top's top-10) |
| host `%Cpu` idle / load avg | 59.8 % idle, load 5.85 | 82.2 % idle, load 2.41 |
| last `Inference stats` line | avg 30.1 ms, max 131 ms | avg 26-29 ms, max 50-78 ms |
| Frigate `host_zmq.inference_speed` | 36 ms | 40-42 ms (EMA incl. container round trip, settling) |

## Day-to-day

```
sudo systemctl status frigate-detector
sudo systemctl restart frigate-detector
journalctl -u frigate-detector -f            # follow logs
journalctl -u frigate-detector --since -1h   # recent logs
```

The log prints a rolling summary every 1000 inferences
(`Inference stats (cpu, fox-person-cat-320.onnx): n=1000 avg=12.3ms max=31.0ms`).
Run with `-v` for per-request timings.

Frigate's own view: `curl -s http://127.0.0.1:5000/api/stats | jq .detectors`
(`inference_speed` there includes the ZMQ round trip).

Frigate re-runs the model handshake whenever its socket times out or the
detector restarts, so restarting this service does **not** require restarting
Frigate (there is a local Frigate patch in `../patches/zmq_ipc.py` that makes
this retry robust).

## Switching models

Frigate decides which model is used: it sends the basename of its `model.path`
(`/config/model_cache/<name>.onnx`) in the handshake. To change model:

1. Put the `.onnx` in `~/frigate/config/model_cache/` (Frigate side)
   and, optionally, in `~/frigate/detector/models/` (detector side —
   if it is missing here Frigate will push it over ZMQ and it gets saved here).
2. Update `model.path`, `width/height`, `labelmap_path`, `model_type` in Frigate's
   `config.yml` and restart Frigate. The detector picks the new model up at the
   next handshake; no detector restart needed.

Supported `model_type`s: `yolo-generic` (Frigate sends it as `yologeneric`),
`rfdetr`, `dfine` — post-processing lives in `detector/model_util.py`.

To pin a model without the handshake (testing only): `--model /path/to/model.onnx`.

### fp16 models on CPU
`fox-person-cat-320-fp16.onnx` **does not load** on the CPU execution provider:
its weights are fp16 but its input is fp32 and the graph has no `Cast` nodes, so
ORT rejects it at load (`Type parameter (T) of Optype (Conv) bound to different
types`). CoreML on the Mac tolerated that; a spec-valid export (with Casts, like
`yolov9-t-320-fp16.onnx`) would load, but on this CPU fp16 is ~2x *slower* than
fp32 anyway (24 ms vs 12 ms). Stay on the fp32 model for the CPU backend.

## CPU backend performance (M1, 4 threads, fp32, 320x320)

Measured 2026-09-05 (morning, before the GPU work) with `test_client.py` while Frigate was also live:

| Path | avg | p50 | p95 |
| --- | --- | --- | --- |
| ONNX Runtime alone (no ZMQ) | 11.8 ms | | |
| host -> tcp://127.0.0.1:5555 round trip | 13.2 ms | 12.8 | 14.6 |
| inside the frigate container -> host.docker.internal:5555 | 14.2 ms | 13.0 | 27.2 |
| Frigate's reported `inference_speed` | ~18 ms | | |

Thread count matters a lot on the M1 (4 Firestorm + 4 Icestorm cores):
2 threads 21 ms, 3 -> 15 ms, **4 -> 12 ms**, 5 -> 27 ms, 8 -> 40 ms. Do not raise
`--threads` above 4. Frigate's ZMQ `request_timeout_ms` defaults to 200 ms, so
there is plenty of headroom; if a slower model is used, raise that in Frigate's
detector config.

## Configuration knobs

CLI flags (or `DETECTOR_*` env vars; flags win):

| Flag | Env | Default | Notes |
| --- | --- | --- | --- |
| `--backend` | `DETECTOR_BACKEND` | `auto` | `vulkan`, `ncnn-vulkan`, `mnn-vulkan`, `ncnn-cpu`, `cpu`, `coreml`, `onnxruntime` (with `--providers`); `auto` = coreml on Apple-Silicon macOS, else cpu (auto does **not** pick the GPU; the unit says `vulkan` explicitly) |
| `--threads` | `DETECTOR_THREADS` | runtime default | intra-op threads; 4 here |
| `--endpoint` | `DETECTOR_ENDPOINT` | `tcp://*:5555` | `tcp://0.0.0.0:5555` here; `ipc://...` also works |
| `--models-dir` | `DETECTOR_MODELS_DIR` | `<repo>/models` | where models are looked up / saved |
| `--model` | `DETECTOR_MODEL` | `AUTO` | path to pin a model instead of the handshake |
| `--providers` | | | explicit ORT provider list (macOS-style usage) |

Edit the unit (`sudo systemctl edit --full frigate-detector` or edit the copy here
and `sudo cp` it) then `sudo systemctl daemon-reload && sudo systemctl restart frigate-detector`.

## The backend seam

`detector/backends.py` isolates the inference engine from the ZMQ protocol code.
The client only does:

```python
backend = create_backend("cpu", models_dir=..., threads=...)
backend.load(model_path)                      # raises on failure
outputs = backend.infer({"images": tensor})   # list[np.ndarray], same order as ORT outputs
```

Post-processing (YOLO decode + NMS, RF-DETR, D-FINE) stays in
`zmq_onnx_client.py` / `model_util.py` and expects the model's *raw* outputs
(for this model: one tensor `[1, 7, 2100]` float32).

To add another engine (the ncnn and MNN classes in `backends.py` are the
templates):

1. Subclass `InferenceBackend`, implement `load()`, `infer()`, `loaded`,
   `input_names`, `close()` (and `describe()` for the logs). `load()` gets the
   `.onnx` path; convert/cache next to the model like `NcnnBackend.cache_paths`.
2. Add a factory to the `BACKENDS` dict (signature
   `factory(models_dir, threads, providers)`); wrap it in `FallbackBackend` to
   get the cpu fallback for free, and add it to `VULKAN_PREFERENCE` if it is a
   GPU backend.
3. Point the unit's `--backend` at it. Nothing in the ZMQ handling, model
   handshake, or post-processing changes.

Keep the output contract: `infer()` must return the same tensors, dtypes and
ordering as ONNX Runtime would for that model, so the existing post-processing
keeps working. If the new engine returns already-decoded boxes, add a
post-process branch keyed on `model_type` rather than changing the protocol.

## Testing without Frigate

`test_client.py` is a copy of the client side of Frigate's
`frigate/detectors/plugins/zmq_ipc.py` (same handshake, headers, timeouts, and
response decoding). It needs only `pyzmq` and `numpy`. `--rate N --duration S`
paces requests for soak tests (prints a running summary every `--report-every`
seconds); exit status is non-zero on any timeout or error reply.

```
# from the host (handshake for fox-person-cat-320.onnx + 200 random-tensor requests)
venv/bin/python test_client.py -n 200 \
    --labels ~/frigate/config/model_cache/fox-person-cat-labelmap.txt

# from inside Docker networking, using the frigate image's own python/pyzmq
docker run --rm --add-host host.docker.internal:host-gateway \
    -v ~/frigate/detector/test_client.py:/test_client.py:ro \
    --entrypoint python3 frigate:latest /test_client.py \
    --endpoint tcp://host.docker.internal:5555 -n 200

# real image instead of noise (prints class/score/box rows)
venv/bin/python test_client.py --input image --image /path/to/frame.jpg \
    --labels ~/frigate/config/model_cache/fox-person-cat-labelmap.txt

# exercise the model-push path against a throwaway instance on another port
venv/bin/python detector/zmq_onnx_client.py --backend cpu --endpoint tcp://127.0.0.1:5556 --models-dir /tmp/m &
venv/bin/python test_client.py --endpoint tcp://127.0.0.1:5556 --model-name pushed.onnx --model-path models/fox-person-cat-320.onnx

# GPU soak on a side instance (what was run before switching production; GPU is shared with the live service)
mkdir -p /tmp/mvk && cp models/fox-person-cat-320.onnx /tmp/mvk/
venv/bin/python detector/zmq_onnx_client.py --backend vulkan --endpoint tcp://127.0.0.1:5556 --models-dir /tmp/mvk &
venv/bin/python test_client.py --endpoint tcp://127.0.0.1:5556 --input image --image /path/to/frame.jpg --rate 25 --duration 300
sudo dmesg | grep -iE 'agx|asahi|gpu|fault'      # must show nothing new
```

Exit status is non-zero if any request hit the 200 ms timeout. Note the
detector serialises requests on one REP socket, so running the test while
Frigate is live inflates both sides' p95 a little.

Caveat: a handshake for a model that *loads* successfully replaces the model the
live service is serving (that's the protocol). Test alternative models on a
second instance/port as above, not on the live 5555 service.

## Known limitations / caveats

- **GPU latency is dispatch-bound, not compute-bound:** Honeykrisp costs ~100 us
  per dispatch and this graph has ~650 layers, so the GPU is *slower* per frame
  (20-24 ms) than ncnn/MNN on the CPU (5 ms) when the CPU is idle. Its value is
  that it stays flat while ffmpeg loads the CPU (CPU inference went 12 -> 30-45 ms
  under Frigate's load) and it frees ~a third of a core. Details and every
  alternative that was tried: `../research/GPU_INFERENCE.md`.
- fp16 on the GPU moves box corners by <1.3 px on almost all boxes, occasionally
  up to ~11 px on low-confidence (<0.65) ncnn boxes, and scores by <=0.015;
  boxes right at the 0.4 threshold can flip. `DETECTOR_NCNN_FP16=0` gives fp32
  (max diff 0.002 vs ORT) for ~25 % more latency. `mnn-vulkan` fp16 is tighter
  (0.4 px / 0.005).
- `mnn-vulkan` depends on the *research* tree (`../research/MNN-src/build/libMNN.so`,
  built from git bef71b9 with a GCC-16 patch) through the shim's rpath; move that
  tree and set `DETECTOR_MNN_LIB` / rebuild with `make mnn-shim MNN_SRC=...`.
  Rebuilding libMNN needs the steps in the research report (`-D__STRICT_ANSI__` removed).
- The `mnn` pip wheel is installed only for its converter; its own runtime is
  CPU-only on Linux and is never loaded into the service.
- Fedora's `ncnn` package must only be used for `onnx2ncnn`/`ncnnoptimize`
  (external processes): its `libncnn` segfaults in `VkWeightAllocator::clear()`
  at teardown, which during the research phase also produced one
  `asahi ... GPU timeout` / `Job canceled on DRM scheduler teardown` in dmesg
  (16:14, before any of the work above). The pip wheel does not have that bug;
  no GPU fault occurred during the soaks. Keep watching
  `dmesg | grep -iE 'agx|asahi|gpu|fault'` after Mesa/kernel updates.
- Batch size 1 only for the GPU backends (all Frigate sends). Multiple
  inputs/outputs are handled generically but were only tested with this
  single-input/single-output YOLOv9 export. `dfine`'s `orig_target_sizes` int64
  input would not work on ncnn (float-only); use `cpu` for such models.
- `dnf update` of Mesa (`mesa-vulkan-drivers`) changes the GPU stack under the
  service; the auto-fallback covers a hard failure, not a silent slowdown, so
  check the `Inference stats` lines afterwards.
- Frigate's `zmq_ipc.py` has no auth; the service listens on all interfaces
  (`0.0.0.0:5555`). This is a LAN-only box; bind to `172.17.0.1` (docker0) if
  that ever changes — but note the frigate container reaches the host via its
  compose network gateway (172.19.0.1), not docker0, so binding to a single
  bridge address needs care.
- The `cache/` directory under `models/` is only used by the CoreML backend; the
  ncnn/MNN caches live next to the `.onnx` files.
- `requirements.txt` is the macOS pin set; on Linux use `requirements-linux.txt`.
