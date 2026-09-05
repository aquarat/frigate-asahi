# Frigate NVR on <nvr-host> (<nvr-ip>) — deployment guide

> Sanitised copy of the live deployment README; see the top-level `README.md` for the placeholder conventions.

Recreated 2026-09-05 from the Mac deployment (<mac-host>, <mac-ip>,
OrbStack + native macOS detector + host ffmpeg proxy). This directory is the
whole deployment: config, data, the host detector service, patches, and the
research/plan for GPU inference and hardware video decode. The Mac's original
files are kept under `src/mac-originals/` (its STACK_GUIDE.md is the best
description of the *old* architecture).

**End-to-end report of the 2026-09-05 build-out: `REPORT.md`.**

## Status

| Piece | State |
|---|---|
| Frigate container (`frigate:latest` = 0.18.0-32daf6f4, same image as the Mac) | running, `docker compose` in this dir |
| Video decode | **Apple AVD hardware decode + zero-copy Vulkan rescale, live since 2026-09-05 20:45.** Patched out-of-tree `apple_avd` installed in `/lib/modules/…/updates/` (rerun `src/avd-driver/build-and-install.sh` after every kernel-16k update, before rebooting), patched ffmpeg `/opt/ffmpeg-vk`, wrapper `ffmpeg-avd-vk/`. Container CPU 107% → 50% of a core, per-camera ffmpeg ~5%. Verify: `staging/verify.sh`; rollback: `staging/APPLY.md` §4 |
| Object detection | host ZMQ detector service, **ncnn on Vulkan (Honeykrisp), fp16** since 2026-09-05 17:00. ~30 ms round trip, p95 38 ms, ~5% of a core (CPU backend was ~12 ms idle but ~40% of a core and 30–130 ms under load). `--backend cpu` reverts; `mnn-vulkan` (faster, 24 ms) is one env var away. See `detector/README-linux.md` |
| Semantic search / face recognition | enabled, `large`, CPU inside the container (same as on the Mac) |
| MQTT | `<mqtt-host>`, but under **`frigate2/`** prefix while the Mac is still live |
| Remote access | none yet; the public hostname still points at the Mac |
| GPU inference (Vulkan/OpenCL) | **done** for detection (above); measurements and why OpenCL/rusticl was rejected in `research/GPU_INFERENCE.md` |
| Kernel | **7.1.6 booted 2026-09-05 17:00**; 7.0.13 kept as GRUB fallback. AVD driver probed, firmware loaded, `/dev/video0` + `/dev/media0` present |
| Hardware decode (Apple AVD) + GPU rescale | **switched on 2026-09-05 20:45** (see Video decode). Upstream patch series: kernel `src/avd-driver/patches/final/` 0001–0008, ffmpeg `patches/ffmpeg/` 0001–0004, Frigate `patches/frigate/`; status and what remains in `research/AVD_FRIGATE_INTEGRATION.md` |

## Layout

```
~/frigate/
├── docker-compose.yml     Frigate container (ports 8971, 127.0.0.1:5000, 8554, 8555)
├── .env                   FRIGATE_RTSP_PASSWORD, PLUS_API_KEY (mode 600, gitignored)
├── config/                /config in the container
│   ├── config.yaml        THIS host's config (diffs from the Mac are commented inline)
│   ├── config.yaml.mac-20260905   the Mac's config as copied
│   ├── frigate.db         sqlite `.backup` snapshot of the Mac DB taken 2026-09-05 16:02
│   ├── .jwt_secret        copied too, so the Mac's logins/sessions work here
│   └── model_cache/       fox-person-cat-320.onnx (+fp16), yolov9-t-320, facedet, jina-clip-v1, paddleocr
├── recordings/ clips/ exports/   /media/frigate/* — copied from the Mac (snapshot, not synced)
├── patches/zmq_ipc.py     bind-mounted over the image's ZMQ detector plugin (retry handshake)
├── patches/frigate/       draft upstream Frigate patch: preset-apple-avd(-vulkan) (not applied)
├── ffmpeg-avd-vk/         ffmpeg.path wrapper for the AVD + Vulkan pipeline (not mounted yet)
├── staging/               docker-compose.yml + config.yaml with the AVD/Vulkan switch, APPLY.md
├── detector/              host-side ZMQ detector (systemd `frigate-detector.service`)
├── research/              GPU_INFERENCE.md, AVD_*.md, GPU_RESCALE.md, AVD_FRIGATE_INTEGRATION.md + benchmark scripts
├── src/avd-driver/        patched apple_avd driver, patches/final + patches/ffmpeg, build-and-install.sh
├── src/ffmpeg-vulkan/     ffmpeg-vk build recipe, run-in-image.sh, bench/ (measure, soak, wrapper-test)
├── src/mac-originals/     Mac compose/Dockerfile/STACK_GUIDE/MACBOOK_TUNING as copied
├── src/wrapper-coordinator/  pointer only: the Mac ffmpeg TCP proxy lives in its own repository
├── src/detector-models/   training/export scripts for the fox-person-cat model (not in this export)
└── xfer/                  transfer tarballs + SHA256SUMS (delete once happy)
```

`/tmp/cache` (in-progress segments) is a 1 GB tmpfs, not a host dir — the Mac
only used a host dir because its host-side ffmpeg had to write there.

## What changed vs the Mac config

- `ffmpeg.path: /opt/ffmpeg-avd-vk` + `hwaccel_args: [-hwaccel, v4l2request]` (AVD hardware
  decode + Vulkan rescale through the wrapper) instead of the Mac's VideoToolbox proxy.
- No `networking.listen.internal: 5050` — that only existed because macOS
  AirPlay squats on port 5000. Port 5000 is bound to 127.0.0.1 on this host.
- Detector renamed `apple_silicon` → `host_zmq`; same `type: zmq`, same
  endpoint `tcp://host.docker.internal:5555` (compose adds the host-gateway
  entry, which on Linux resolves to docker0's 172.17.0.1).
- `go2rtc.webrtc.candidates` → `<nvr-ip>:8555`.
- `mqtt.topic_prefix`/`client_id` = `frigate2` so the home-automation frigate
  integration keeps following the Mac. **Remove both lines at cut-over.**
- Camera `cam1` (<camera-1-ip>) is unreachable from here *and* showed
  0 fps on the Mac — pre-existing, not a migration problem.

## Cut-over checklist (when this box replaces the Mac)

1. Stop the Mac's container (`docker stop frigate` on <mac-ip>) so the cameras
   only serve one NVR, and so events are not recorded twice.
2. Re-snapshot `frigate.db`, `clips/`, `recordings/`, `exports/` from the
   Mac if you care about the interval since 2026-09-05 (same recipe: sqlite
   `.backup`, tar on the Mac, pull with `rsync --partial --inplace
   --timeout=30` in a retry loop — plain rsync/tar streams from the Mac stall
   after ~20 MB, see below).
3. Delete the `topic_prefix`/`client_id` lines from `config/config.yaml`.
4. Repoint the external reverse proxy / DNS for the public hostname at
   `https://<nvr-ip>:8971`.
5. Frigate+ key and the fox-person-cat model are already here.

## Operations

```bash
cd ~/frigate
docker compose up -d                 # start / apply compose changes
docker compose restart frigate       # after config.yaml edits
docker logs -f frigate               # s6/nginx/go2rtc log
docker exec frigate cat /dev/shm/logs/frigate/current   # Frigate app log
curl -s http://127.0.0.1:5000/api/stats | python3 -m json.tool | grep -E 'camera_fps|detection_fps|inference_speed'
sudo systemctl status frigate-detector; journalctl -u frigate-detector -f
```

UI: https://<nvr-ip>:8971 (self-signed cert; Mac users/passwords work).

## Image

`frigate:latest` is **not in a registry**. It was built on the Mac from
`github.com/aquarat/frigate` branch `dev` at 32daf6f4 with `make local`
(the fork's only diff: `NODE_OPTIONS=--max-old-space-size=4096` for the web
build), then moved here with `docker save | ssh | docker load`. To upgrade,
either build again from the fork (`make local` on this box is untested and
slow) or pull an official `ghcr.io/blakeblackshear/frigate:<ver>-standard-arm64`
once one is ≥ the DB's migration level (the DB is at 0.18 dev schema — check
`migratehistory` before downgrading).

## Transfer gotcha

Streaming `tar`/`rsync` from the Mac over SSH stalled after ~12–21 MB every
time (TCP showed ~900 out-of-order segments; the Mac ships openrsync). A
5.8 GB `docker save` stream did **not** stall. Workaround that worked: tar to
`/tmp/frig-xfer/` on the Mac, then pull each tarball with
`rsync --partial --inplace --timeout=30` in a retry loop and verify SHA256.
