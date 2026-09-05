# frigate-asahi

Sanitised export (2026-09-05) of a live [Frigate](https://frigate.video) NVR deployment on an
Apple M1 Mac mini running Fedora Asahi Remix: Frigate 0.18 in Docker, object detection on the
M1 GPU through Vulkan (ncnn, fp16) in a host-side ZMQ detector service, and H.264 decode on the
Apple Video Decoder (AVD, V4L2 request API) with zero-copy Vulkan rescaling. The Mac deployment it
replaced (macOS + OrbStack + VideoToolbox proxy) is documented under `src/mac-originals/`.

**Start with [`REPORT.md`](REPORT.md)** — the end-to-end account of the build-out: what was built,
the kernel-driver, FFmpeg and Frigate bugs found and fixed, measurements, and what is still open.
`DEPLOYMENT.md` is the operational guide (layout, cut-over checklist, operations).

## This is a sanitised snapshot

Everything here was copied from the working tree and then scrubbed. Placeholders in angle
brackets replace real values and must be filled in before anything is used:

| Placeholder | Meaning |
|---|---|
| `<nvr-host>`, `<nvr-ip>` | the Mac mini running this deployment |
| `<mac-host>`, `<mac-ip>` | the MacBook that ran the previous deployment |
| `<m1pro-ip>` | a second Apple-silicon machine used for driver validation notes |
| `<mqtt-host>`, `<docs-host>`, `<public-hostname>` | MQTT broker, admin host, external DNS name |
| `<camera-N-ip>`, `cam1` … `cam5`, `<camera-model>` | the five cameras (names are placeholders everywhere, including clip names such as `cam2-10s.h264`) |
| `<onvif-user>`, `<onvif-password>`, `<rtsp-password>`, `<plus-api-key>` | credentials |
| `<user>`, `~/frigate`, `$FRIGATE_HOME` | the login account and the checkout path |

Not included: `.env` (see `.env.example`), `config/` apart from `config/config.example.yaml`
(the live `config.yaml`, sanitised), recordings/clips/exports, the SQLite database, every video
clip, frame and raw YUV dump used in the research, all built binaries (`.ko`, ffmpeg builds,
shaderc, `.so`), Python venvs, and the detection models. `yolov9-t-320` can be downloaded per
the Frigate docs; `fox-person-cat-320.onnx` is a custom-trained model and is not distributed.

Paths: docs use `~/frigate`; shell scripts use `${FRIGATE_HOME:-$HOME/frigate}`;
`detector/frigate-detector.service` is an example unit with `/home/<user>/frigate` and `User=<user>`
to edit before installing. The `ffmpeg-avd-vk/` wrapper uses container paths only and is unchanged.

## Layout

```
DEPLOYMENT.md           deployment guide (was the working tree's README.md)
REPORT.md               end-to-end report of the 2026-09-05 build-out
docker-compose.yml      Frigate container: AVD + Vulkan device/mount pass-through
config/config.example.yaml   sanitised live Frigate config (also staging/config.yaml)
ffmpeg-avd-vk/          `ffmpeg.path` wrapper: AVD decode + zero-copy Vulkan rescale chain
staging/                APPLY.md runbook (apply / verify / rollback) + verify.sh
detector/               host-side ZMQ detector service (ncnn/MNN Vulkan, ONNX Runtime CPU)
patches/                Frigate: zmq_ipc.py retry patch, draft preset-apple-avd(-vulkan) patch
src/avd-driver/         AVD kernel-driver work: patches/final 0001-0008, ffmpeg patches, test harness, results
src/avd-analysis/       driver root-cause analysis notes, patch generator, upstream fix diffs
src/ffmpeg-vulkan/      FFmpeg (v4l2request + Vulkan) build recipe for the bookworm-based image, bench scripts
src/mac-originals/      the previous macOS deployment's compose/Dockerfile/STACK_GUIDE/MACBOOK_TUNING
src/wrapper-coordinator/  pointer only (separate repository)
research/               GPU inference, AVD decode, read-back and rescale investigations + benchmark scripts/logs
```

## Companion repositories

The source trees this work patches are not vendored here; see the companion repositories on
the same GitHub account: the Apple AVD kernel driver / Linux fork (patch series in
`src/avd-driver/patches/final/`), the FFmpeg fork (`src/avd-driver/patches/ffmpeg/`), the
Frigate fork (branch with the Apple AVD/Vulkan presets, `patches/frigate/`), and the
`frigate-remote-ffmpeg` proxy used by the Mac deployment.
