# Frigate NVR Stack Guide

A complete reference for the custom Frigate NVR deployment running on macOS (Apple Silicon) with VideoToolbox hardware acceleration, a TCP ffmpeg proxy, and an Apple Neural Engine object detector.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Component Inventory](#component-inventory)
3. [How It All Fits Together](#how-it-all-fits-together)
4. [Directory Layout](#directory-layout)
5. [wrapper-coordinator (ffmpeg proxy)](#wrapper-coordinator-ffmpeg-proxy)
6. [Frigate Container](#frigate-container)
7. [go2rtc Streaming](#go2rtc-streaming)
8. [Apple Silicon Detector](#apple-silicon-detector)
9. [Camera Configuration](#camera-configuration)
10. [Recording Pipeline](#recording-pipeline)
11. [The Segment-Holding Problem and Fix](#the-segment-holding-problem-and-fix)
12. [Operational Procedures](#operational-procedures)
13. [Troubleshooting Reference](#troubleshooting-reference)
14. [Known Constraints and Gotchas](#known-constraints-and-gotchas)

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│  macOS host (Apple Silicon — arm64)                          │
│                                                              │
│  ┌────────────────────────────────────┐                      │
│  │  OrbStack / Docker                 │                      │
│  │                                    │                      │
│  │  Frigate container                 │                      │
│  │  (linux/arm64)                     │                      │
│  │                                    │                      │
│  │  Frigate app  ──►  wrapper         │                      │
│  │  (Python)      (drop-in ffmpeg/    │                      │
│  │                 ffprobe binary)    │                      │
│  │                      │            │                      │
│  │                      │ TCP :17346 │                      │
│  └──────────────────────┼────────────┘                      │
│                         │                                    │
│   host.docker.internal  │  (≡ 127.0.0.1 from container's   │
│                         │   perspective via Docker bridge)   │
│                         ▼                                    │
│  ┌──────────────────────────────────────┐                    │
│  │  frigate-coordinator daemon          │                    │
│  │  (launchd user agent)                │                    │
│  │                                      │                    │
│  │  - Path-maps container paths         │                    │
│  │    to host paths                     │                    │
│  │  - Execs /opt/homebrew/bin/ffmpeg    │                    │
│  │    (with VideoToolbox support)       │                    │
│  │  - Relays stdin/stdout/stderr/       │                    │
│  │    signals/exit code back over TCP   │                    │
│  └──────────────────────────────────────┘                    │
│                                                              │
│  /opt/homebrew/bin/ffmpeg  (Homebrew — VideoToolbox enabled) │
│                                                              │
│  Apple Silicon Detector (separate process, ZMQ :5555)        │
│  (yolov9-t-320-fp16.onnx via ONNX Runtime + CoreML/ANE)     │
└──────────────────────────────────────────────────────────────┘
```

**Why this design?**  
Frigate's official Docker image runs on Linux and ships a Linux ffmpeg that has no access to Apple's VideoToolbox API. The proxy approach lets Frigate invoke its normal ffmpeg workflow while silently forwarding every execution to a native macOS binary that can use VideoToolbox for hardware-accelerated H.264/H.265 decoding — no image patching required.

---

## Component Inventory

| Component | Location | Language | Role |
|-----------|----------|----------|------|
| Frigate NVR | `frigate-workspace/frigate/` (source), `frigate/` (runtime data) | Python | Core NVR — detection, recording, API, UI |
| wrapper binary | `frigate-workspace/wrapper-coordinator/dist/wrapper-linux-arm64` | Go | Drop-in `ffmpeg`/`ffprobe` inside container |
| coordinator daemon | `/usr/local/bin/frigate-coordinator` | Go | Native macOS daemon that execs real ffmpeg |
| coordinator config | `/usr/local/etc/frigate-coordinator/config.yaml` | YAML | Path mappings, ffmpeg path, listen address |
| launchd plist | `~/Library/LaunchAgents/com.frigate.coordinator.plist` | XML plist | Auto-start coordinator on login |
| Frigate runtime data | `~/Projects/frigate/` | — | Config, recordings, clips, cache |
| go2rtc | Embedded in Frigate container | Go | RTSP/WebRTC restreamer |
| Apple Silicon Detector | Runs natively on host, port 5555 | Python | ONNX Runtime + ANE inference |

---

## How It All Fits Together

1. **Frigate starts** inside the container. It reads `/config/config.yaml`.
2. **go2rtc** is started first (s6-overlay service). It reads `config.go2rtc` from `config.yaml` (via `create_config.py` → `/dev/shm/go2rtc.yaml`) and opens ONVIF/RTSP connections to cameras. It exposes restreams at `rtsp://127.0.0.1:8554/<stream_name>`.
3. **Frigate's camera processes** call the `wrapper` binary (mounted at `/usr/local/bin/frigate-wrapper/bin/ffmpeg` and `ffprobe`) with normal ffmpeg arguments. The wrapper binary connects over TCP to `host.docker.internal:17346`.
4. **The coordinator** receives the TCP connection, translates container-side file paths to host-side paths (e.g. `/tmp/cache` → `~/Projects/frigate/cache`), and execs the real `/opt/homebrew/bin/ffmpeg` with the translated arguments.
5. **ffmpeg** (native, VideoToolbox) runs on the macOS host, writes segment files into the host filesystem (which the container sees via volume mounts), and sends stdout/stderr back through the coordinator → wrapper → Frigate.
6. **The wrapper** also runs a goroutine (`holdSegmentFiles`) that holds a read-only file descriptor open on the newest in-progress segment, preventing Frigate's recording maintainer from treating it as complete (see [The Segment-Holding Problem and Fix](#the-segment-holding-problem-and-fix)).
7. **Frigate's recording maintainer** polls `/tmp/cache` (container path), skips segments that are open, and moves validated segments to `/media/frigate/recordings`.
8. **The Apple Silicon Detector** runs natively on the host, listens on ZMQ port 5555, receives raw video frames from Frigate, runs inference using ONNX Runtime against a YOLOv9 FP16 model, and returns detections over ZMQ.

---

## Directory Layout

### Runtime data — `~/Projects/frigate/`

```
frigate/
├── config/
│   ├── config.yaml              ← main Frigate config (volume-mounted at /config)
│   ├── go2rtc_homekit.yml       ← go2rtc HomeKit config (written by go2rtc)
│   └── model_cache/             ← ONNX model files
│       ├── yolov9-t-320-fp16.onnx
│       ├── facedet/
│       ├── jinaai/jina-clip-v1/
│       ├── paddleocr-onnx/
│       └── yolov9_license_plate/
├── cache/                       ← in-progress recording segments (/tmp/cache in container)
├── recordings/                  ← completed recordings (/media/frigate/recordings)
├── clips/                       ← event clips (/media/frigate/clips)
├── exports/                     ← manual exports (/media/frigate/exports)
├── docker-compose.yml
└── Dockerfile
```

### Source — `~/Projects/frigate-workspace/`

```
frigate-workspace/
├── frigate/                     ← Frigate NVR source (Python)
├── wrapper-coordinator/         ← Go source for wrapper + coordinator
│   ├── cmd/wrapper/main.go
│   ├── cmd/coordinator/main.go
│   ├── internal/pathmap/
│   ├── internal/proto/
│   ├── launchd/
│   │   ├── com.frigate.coordinator.plist
│   │   └── com.frigate.coordinator.plist.example
│   ├── dist/
│   │   ├── wrapper-linux-arm64  ← built wrapper (bind-mounted into container)
│   │   ├── wrapper-linux-amd64
│   │   └── coordinator-darwin-arm64
│   └── Makefile
└── rffmpeg/                     ← (reference only, not used in this deployment)
```

---

## wrapper-coordinator (ffmpeg proxy)

**Source:** `frigate-workspace/wrapper-coordinator/`  
**README:** `frigate-workspace/wrapper-coordinator/README.md`

### Wire protocol

Each TCP connection carries exactly one ffmpeg invocation. The protocol uses a simple 5-byte length-prefixed framing:

```
┌──────────────┬──────────────────┬───────────────┐
│  Type (1B)   │  Length (4B BE)  │  Payload      │
└──────────────┴──────────────────┴───────────────┘
```

| Direction | Frame type | Payload |
|-----------|-----------|---------|
| wrapper → coordinator | `ExecReq` | JSON: binary name, args, env, cwd |
| wrapper → coordinator | `Stdin` | raw stdin bytes |
| wrapper → coordinator | `StdinEOF` | empty |
| wrapper → coordinator | `Signal` | 1-byte OS signal number |
| coordinator → wrapper | `Stdout` | raw stdout bytes |
| coordinator → wrapper | `Stderr` | raw stderr bytes |
| coordinator → wrapper | `Exit` | 4-byte int32 exit code (big-endian) |

### Path mapping

The coordinator translates container-side absolute path prefixes to host-side paths before spawning ffmpeg. Network sources (`rtsp://`, `pipe:`, bare `-`) are never remapped. Rules are matched first-prefix-wins — list more-specific paths before less-specific ones.

### Building

```bash
cd frigate-workspace/wrapper-coordinator

# Build everything
make all

# Individual targets
make coordinator       # → dist/coordinator-darwin-arm64  (runs on macOS host)
make wrapper-arm64     # → dist/wrapper-linux-arm64       (mounted into container)
make wrapper-amd64     # → dist/wrapper-linux-amd64
```

After rebuilding, the new wrapper is picked up automatically on next container restart because it is bind-mounted read-only. The coordinator must be re-installed manually:

```bash
sudo install -m 755 dist/coordinator-darwin-arm64 /usr/local/bin/frigate-coordinator
launchctl kickstart -k gui/$(id -u)/com.frigate.coordinator
```

### Coordinator as a launchd user agent

```bash
# Install from scratch
make install-launchd

# Check status
launchctl list com.frigate.coordinator

# View logs (when running as a daemon, goes to syslog)
log show --last 1h --predicate 'process == "frigate-coordinator"'
# Or if stdout is redirected to a file:
tail -f /usr/local/var/log/frigate-coordinator.log

# Stop
launchctl unload ~/Library/LaunchAgents/com.frigate.coordinator.plist

# Remove entirely
make uninstall-launchd
```

### Coordinator config (`/usr/local/etc/frigate-coordinator/config.yaml`)

```yaml
ffmpeg_path: /opt/homebrew/bin/ffmpeg
ffprobe_path: /opt/homebrew/bin/ffprobe
listen_addr: 127.0.0.1:17346
log_level: info
path_mappings:
  - container: /tmp/cache
    host: ~/Projects/frigate/cache
  - container: /config
    host: ~/Projects/frigate/config
  - container: /media/frigate/recordings
    host: ~/Projects/frigate/recordings
  - container: /media/frigate/clips
    host: ~/Projects/frigate/clips
  - container: /media/frigate/exports
    host: ~/Projects/frigate/exports
```

> **Important:** Path mappings must exactly match the `volumes:` entries in `docker-compose.yml`. A mismatch causes ffmpeg to write to a path inside the container's ephemeral filesystem, not to a persistent host directory.

---

## Frigate Container

### Image

`frigate-local:stable-standard-arm64` — built locally from `Dockerfile` in `~/Projects/frigate/`. This is a standard Frigate image for `linux/arm64` with no custom patches; all customisation happens through config and volume mounts.

### docker-compose.yml key settings

```yaml
services:
  frigate:
    image: frigate-local:stable-standard-arm64
    shm_size: "512mb"
    volumes:
      - /etc/localtime:/etc/localtime:ro
      - ~/Projects/frigate/cache:/tmp/cache
      - ~/Projects/frigate/config:/config
      - ~/Projects/frigate/recordings:/media/frigate/recordings
      - ~/Projects/frigate/clips:/media/frigate/clips
      - ~/Projects/frigate/exports:/media/frigate/exports
      # The wrapper binary mounted as both ffmpeg AND ffprobe
      - .../dist/wrapper-linux-arm64:/usr/local/bin/frigate-wrapper/bin/ffmpeg:ro
      - .../dist/wrapper-linux-arm64:/usr/local/bin/frigate-wrapper/bin/ffprobe:ro
    ports:
      - "8971:8971"   # Frigate authenticated API
      - "5001:5000"   # Internal unauthenticated UI/API
      - "8554:8554"   # go2rtc RTSP restreams
      - "8555:8555"   # WebRTC (TCP + UDP)
    extra_hosts:
      - "host.docker.internal:host-gateway"  # Lets container reach host :17346
    environment:
      - FRIGATE_IPC_ADDR=host.docker.internal:17346
      - FRIGATE_RTSP_PASSWORD=<rtsp-password>
      - PLUS_API_KEY=...
```

### Container management

```bash
# Start / recreate (does not rebuild image)
cd ~/Projects/frigate
docker compose up --force-recreate --no-build -d

# Stop
docker compose down

# Restart
docker restart frigate

# Tail logs
docker logs -f frigate

# Shell into container
docker exec -it frigate bash
```

---

## go2rtc Streaming

### How go2rtc starts

On container start, the s6-overlay service at `/etc/s6-overlay/s6-rc.d/go2rtc/run`:

1. Deletes any stale `/dev/shm/go2rtc.yaml`.
2. Runs `python3 /usr/local/go2rtc/create_config.py`, which reads `config.yaml`'s `go2rtc:` section and writes `/dev/shm/go2rtc.yaml`.
3. Execs: `go2rtc -config=/config/go2rtc_homekit.yml -config=/dev/shm/go2rtc.yaml`

### Stream naming — critical rule

**The go2rtc stream name must exactly match the Frigate camera name.**

Frigate's web UI (`LiveCameraView.tsx`) checks:
```js
Object.keys(config.go2rtc.streams || {}).includes(cameraName)
```
If the stream name does not match, the UI shows **"Restreaming is not enabled for this camera"** and live view is unavailable.

The `create_config.py` script does **not** auto-generate stream names from camera inputs. Every stream must be declared explicitly in `config.yaml` under `go2rtc.streams`.

### Current go2rtc config (`/dev/shm/go2rtc.yaml` — generated)

```yaml
webrtc:
  candidates:
    - <mac-ip>:8555
streams:
  cam1:
    - onvif://<onvif-user>:<onvif-password>@<camera-1-ip>:2020
api:
  origin: '*'
ffmpeg:
  bin: /usr/local/bin/frigate-wrapper/bin/ffmpeg
```

### Current `config.yaml` go2rtc section

```yaml
go2rtc:
  webrtc:
    candidates:
      - <mac-ip>:8555   # host LAN IP:WebRTC port
  streams:
    cam1:              # MUST match camera name
      - onvif://<onvif-user>:<onvif-password>@<camera-1-ip>:2020
```

### Camera ffmpeg input

The Frigate camera reads from the go2rtc restream **via the container's localhost** (port 8554 is mapped through Docker):

```yaml
cameras:
  cam1:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/cam1   # go2rtc internal RTSP
          input_args: preset-rtsp-restream
```

`preset-rtsp-restream` adds a custom `User-Agent` header. It does not change the path.

Using the internal loopback address (`127.0.0.1:8554`) is preferred over the host's LAN IP because the coordinator's native ffmpeg connects to it over localhost — reliable, no network hops.

---

## Apple Silicon Detector

### Overview

A separate native process running on the macOS host. It accepts raw video frames from Frigate over ZMQ, runs ONNX Runtime inference using the CoreML execution provider (Apple Neural Engine / GPU), and returns detections.

### Configuration in `config.yaml`

```yaml
detectors:
  apple_silicon:
    type: zmq
    endpoint: tcp://host.docker.internal:5555

model:
  model_type: yolo-generic
  width: 320
  height: 320
  input_tensor: nchw
  input_dtype: float
  path: /config/model_cache/yolov9-t-320-fp16.onnx
  labelmap_path: /labelmap/coco-80.txt
```

The model file is stored in the runtime config volume at  
`~/Projects/frigate/config/model_cache/yolov9-t-320-fp16.onnx`  
(visible inside the container as `/config/model_cache/yolov9-t-320-fp16.onnx`).

---

## Camera Configuration

### Current camera: <camera-model>

| Parameter | Value |
|-----------|-------|
| Camera name | `cam1` |
| ONVIF address | `onvif://<onvif-user>:<onvif-password>@<camera-1-ip>:2020` |
| go2rtc stream | `cam1` (ONVIF source) |
| ffmpeg input (inside container) | `rtsp://127.0.0.1:8554/cam1` |
| Resolution | 2560×1440 |
| Codec | H.264 High |
| Detection FPS | 10 |

### Full camera block in `config.yaml`

```yaml
cameras:
  cam1:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/cam1
          input_args: preset-rtsp-restream
          roles:
            - record
            - detect
    detect:
      enabled: true    # Explicit true prevents detection going off after restart
      fps: 10
    record:
      enabled: true
      motion:
        days: 1        # Save segments even without detection events
      alerts:
        retain:
          days: 14
          mode: motion
      detections:
        retain:
          days: 14
          mode: motion
    snapshots:
      enabled: true
      clean_copy: true
      retain:
        default: 14
    objects:
      track:
        - person
```

### Why `detect.enabled: true` is needed

Frigate persists the runtime detection on/off state (toggled via MQTT) to its SQLite database. Without an explicit `enabled: true` in the config, the last MQTT state takes precedence. If detection was turned off via the UI or MQTT before a restart, it stays off. Setting it explicitly in the config makes `true` the boot default regardless of prior runtime state.

---

## Recording Pipeline

```
Camera (ONVIF)
    │
    ▼
go2rtc (inside container)
rtsp://127.0.0.1:8554/cam1
    │
    ▼
ffmpeg (wrapper → coordinator → native macOS ffmpeg)
  - Input: RTSP from go2rtc
  - Segment output: /tmp/cache/cam1@%Y%m%d%H%M%S%z.mp4  (10-second segments)
    (maps to ~/Projects/frigate/cache/ on host)
  - Detection output: raw YUV frames on stdout → pipe to Frigate
    │
    ▼
/tmp/cache/*.mp4  (in-progress segments)
    │
    ▼ (Frigate recording maintainer — Python, runs inside container)
validate_and_move_segment()
  - Calls ffprobe to check video properties (has_valid_video)
  - Calls move_segment() → runs ffmpeg to copy with -movflags faststart
  - Moves to /media/frigate/recordings/<camera>/<date>/<time>.mp4
    │
    ▼
/media/frigate/recordings/
(= ~/Projects/frigate/recordings/ on host)
```

### Segment file naming

Segments are named `<camera>@<timestamp+tz>.mp4`, e.g.:  
`cam1@20260519182347+0100.mp4`

The `@` character is used by both the wrapper's `holdSegmentFiles` function and Frigate's maintainer to identify segment files for a specific camera.

---

## The Segment-Holding Problem and Fix

### Problem

Frigate's recording maintainer (`frigate/record/maintainer.py`) uses `psutil` to enumerate open files belonging to processes named `ffmpeg` inside the container. Files that are open are treated as "in use" and skipped. Files that are not open are validated (via ffprobe) and moved to recordings.

**The issue:** The actual `ffmpeg` process runs on the **macOS host** (via the coordinator). `psutil` inside the container cannot see host processes. The only `ffmpeg`-named process visible to psutil inside the container is the `wrapper` binary — which holds no segment file descriptors (all I/O happens on the host side).

Result: every segment file appears "not in use" to the maintainer, even segments that ffmpeg is still actively writing. The maintainer probes them before the `moov` atom is written, gets "invalid or missing video stream", and discards them.

### Fix

The wrapper binary (`cmd/wrapper/main.go`) runs a goroutine called `holdSegmentFiles` immediately after sending the exec request to the coordinator:

```go
go holdSegmentFiles(req.Args)
```

This goroutine:
1. Parses the ffmpeg args to find the segment output pattern (a `.mp4` path containing `@`).
2. Polls `/tmp/cache` (container-side path) every 200 ms.
3. Tracks the lexicographically greatest segment filename (timestamp-sortable format).
4. Holds a read-only `os.Open` file descriptor on **only the newest** segment.
5. When a newer segment appears, closes the old fd (making that completed segment available to the maintainer) and opens the new one.

The key insight: the wrapper process **is** visible to psutil inside the container. By holding an fd, the wrapper makes the newest segment appear "in use". Once the next segment starts, the old fd is released and the maintainer can validate and move it.

### Observable behaviour

- One "Invalid or missing video stream" warning at startup (the segment from the previous ffmpeg run before the wrapper started) — this is expected and harmless.
- After the first 10-second segment completes, recordings appear in `/recordings`.
- The maintainer logs show segments being moved, not discarded.

---

## Operational Procedures

### Start everything from scratch

```bash
# 1. Ensure coordinator is running
launchctl list com.frigate.coordinator
# If not running:
launchctl load -w ~/Library/LaunchAgents/com.frigate.coordinator.plist

# 2. Start Frigate
cd ~/Projects/frigate
docker compose up --force-recreate --no-build -d

# 3. Start the Apple Silicon detector (if not already running)
# (run according to that project's own instructions)

# 4. Open UI
open http://localhost:5001
```

### Restart Frigate (config changes)

```bash
docker restart frigate
```

After restart, check for orphan ffmpeg processes from the old instance:

```bash
ps aux | grep ffmpeg | grep cam1 | grep -v grep
```

Kill any PIDs using the old stream URL or that appear duplicated.

### Rebuild the wrapper/coordinator after code changes

```bash
cd ~/Projects/frigate-workspace/wrapper-coordinator
make all

# Reinstall coordinator binary
sudo install -m 755 dist/coordinator-darwin-arm64 /usr/local/bin/frigate-coordinator
launchctl kickstart -k gui/$(id -u)/com.frigate.coordinator

# Wrapper is picked up automatically on next container restart
docker restart frigate
```

### Check coordinator is running

```bash
launchctl list com.frigate.coordinator
# Should show PID, not "-"
```

### Check go2rtc generated config

```bash
docker exec frigate cat /dev/shm/go2rtc.yaml
# Verify stream names match camera names in config.yaml
```

### Verify recordings are being saved

```bash
ls -lt ~/Projects/frigate/recordings/
# Should see dated subdirectories
find ~/Projects/frigate/recordings -name "*.mp4" | tail -5
```

### Check for orphan ffmpeg processes

```bash
ps aux | grep ffmpeg | grep -v grep
```

Orphan processes arise when the Frigate container restarts but the coordinator does not immediately kill the spawned ffmpeg instances (timing/connection-close race). Kill them manually if found.

---

## Troubleshooting Reference

### "Restreaming is not enabled for this camera" in UI

**Cause:** The go2rtc stream name does not match the Frigate camera name.

**Check:**
```bash
docker exec frigate cat /dev/shm/go2rtc.yaml
# Look for streams: section — key must match the camera name exactly
```

**Fix:** In `config.yaml`, ensure `go2rtc.streams.<name>` uses the exact camera name:
```yaml
go2rtc:
  streams:
    cam1:        # ← must match cameras.cam1
      - onvif://...
```
Then: `docker restart frigate`

---

### "Invalid or missing video stream in segment" — segments discarded

**Cause A (expected, one-time):** Startup discard of the segment from the previous ffmpeg run. Normal.

**Cause B (persistent):** The wrapper's `holdSegmentFiles` goroutine is not running, or the wrapper binary is outdated.

**Check:**
```bash
# Verify wrapper is the binary being called
docker exec frigate ls -la /usr/local/bin/frigate-wrapper/bin/
# Verify config points to it
grep "ffmpeg.path" ~/Projects/frigate/config/config.yaml
# Should be: path: /usr/local/bin/frigate-wrapper
```

**Check that recordings eventually appear** (wait 20+ seconds after start):
```bash
ls -lt ~/Projects/frigate/recordings/
```

If they don't, check maintainer logs:
```bash
docker logs frigate 2>&1 | grep -i "maintainer\|segment\|invalid"
```

---

### Detection not working after restart

**Cause:** Frigate persists the MQTT detection on/off state. Without an explicit `enabled: true` in the config, the previous "off" state wins.

**Fix:** Ensure `config.yaml` has:
```yaml
cameras:
  cam1:
    detect:
      enabled: true
```

---

### Coordinator not accepting connections

```bash
# Is it listening?
lsof -i :17346

# Check launchd status
launchctl list com.frigate.coordinator

# Reload
launchctl kickstart -k gui/$(id -u)/com.frigate.coordinator
```

---

### ffmpeg processes not cleaned up after container restart

The coordinator spawns ffmpeg as a child process. If the TCP connection closes before the coordinator can send SIGTERM (race condition on container stop), ffmpeg may become an orphan.

```bash
# Find and kill orphans
ps aux | grep ffmpeg | grep -v grep
kill <PID>
```

This is a known timing issue. It is safe to kill these manually.

---

### go2rtc config not updated after config.yaml change

The go2rtc config at `/dev/shm/go2rtc.yaml` is only regenerated at container start. After changing `config.yaml`:
```bash
docker restart frigate
# Verify:
docker exec frigate cat /dev/shm/go2rtc.yaml
```

---

## Known Constraints and Gotchas

### 1. Volume mount paths must be consistent everywhere

The same physical directory must be mapped identically in **three places**:
- `docker-compose.yml` `volumes:` section
- `/usr/local/etc/frigate-coordinator/config.yaml` `path_mappings:`
- Any references in `config.yaml` that specify absolute paths (model paths, etc.)

A mismatch causes ffmpeg to write files to a non-existent or ephemeral path.

### 2. The wrapper binary serves as both `ffmpeg` and `ffprobe`

The same binary (`wrapper-linux-arm64`) is mounted at both:
- `/usr/local/bin/frigate-wrapper/bin/ffmpeg`
- `/usr/local/bin/frigate-wrapper/bin/ffprobe`

The wrapper detects which tool to proxy via `filepath.Base(os.Args[0])` (the binary name) and sends `BinaryName` in the `ExecRequest`. The coordinator uses this to decide whether to exec `ffmpeg_path` or `ffprobe_path` from its config.

### 3. go2rtc stream names must match camera names

Documented above, but worth repeating: `go2rtc.streams.<key>` must equal the camera name under `cameras:`. There is no auto-derivation.

### 4. `psutil` inside container cannot see host processes

This is the fundamental reason the `holdSegmentFiles` goroutine exists. Never rely on psutil-based open-file detection across the container/host boundary. If Frigate is updated and the maintainer's logic for skipping in-use segments changes, this goroutine may need updating too.

### 5. Hardware acceleration flags are macOS-specific

```yaml
ffmpeg:
  hwaccel_args:
    - -hwaccel
    - videotoolbox
    - -hwaccel_output_format
    - nv12
```

These args are passed through the wrapper to the native macOS ffmpeg. They work because the coordinator execs a real macOS ffmpeg with VideoToolbox support. If the coordinator were replaced with a Linux ffmpeg, these flags would fail. Remove or replace them if migrating to a non-macOS host.

### 6. WebRTC candidates must use the host's LAN IP

```yaml
go2rtc:
  webrtc:
    candidates:
      - <mac-ip>:8555
```

This must be the LAN IP of the macOS machine, not `localhost` or `127.0.0.1`. WebRTC ICE candidates are advertised to browsers and must be reachable from the client network. Update this if the host's IP changes.

### 7. FRIGATE_IPC_ADDR environment variable

The coordinator's TCP address is passed to the container via `FRIGATE_IPC_ADDR=host.docker.internal:17346`. If the port ever changes (e.g. conflict with another service), update both the coordinator's `listen_addr` and this environment variable in `docker-compose.yml`.

### 8. Orphan ffmpeg processes accumulate on repeated restarts

Each `docker restart frigate` may leave behind one orphan ffmpeg process if the coordinator's graceful drain does not complete in time. Check and clean these up periodically.

### 9. `record.motion.days` is required for continuous recording

Without `record.motion.days` set, Frigate only retains segments that contain detection events. Setting `days: 1` causes segments to be retained even when there are no detections (motion-only days). This is independent of the `detections:` and `alerts:` retain blocks.
