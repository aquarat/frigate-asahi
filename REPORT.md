# Frigate on <nvr-host> — migration, GPU detection, and Apple AVD hardware decode

**Date:** 2026-09-05 (one working day, ~15:45–21:10 BST)
**Host:** <nvr-host>, Apple Mac mini (M1, 2020, T8103), Fedora Asahi Remix 44, <nvr-ip>
**Source system:** <mac-host>, MacBook Pro M1 Pro, macOS + OrbStack, <mac-ip> (still running in parallel)
**Outcome:** Frigate 0.18 runs natively on Linux with object detection on the M1 GPU (Vulkan) and
H.264 decode on the Apple Video Decoder with GPU rescaling. Container CPU dropped from ~1.1 cores to
~0.5 cores for four cameras; total system power ~5 W. Four kernel-driver bugs, two Mesa/ffmpeg
interop bugs and one Frigate integration issue were found and fixed along the way. Everything is
reproducible from this directory; the detailed per-topic documents are linked in section 9.

## 1. Starting point and goal

The house NVR ran as a Frigate container under OrbStack on a headless MacBook, with two native macOS
helpers: an ONNX detector on the Apple Neural Engine (ZMQ) and a TCP proxy that ran every ffmpeg on
the host to get VideoToolbox decode. The goal was to recreate Frigate on the Linux Mac mini, first on
CPU, then add a Vulkan/OpenCL detector backend, then hardware video decode via the Apple AVD.

## 2. What was built, in order

| Step | Result |
|---|---|
| Migration | Same image (0.18.0-32daf6f4, moved with `docker save`), consistent SQLite snapshot, model cache, clips/exports/recordings; config ported with the differences commented inline. MQTT under `frigate2/` so the home-automation MQTT consumer keeps following the Mac. |
| Host detector service | `frigate-detector.service`: the Mac's ZMQ detector code, ported to Linux with a backend seam. CPU (ONNX Runtime) first. |
| GPU inference research | Vulkan (Honeykrisp) and OpenCL (rusticl) verified headless. Small-CNN inference is dispatch-latency bound on this GPU, so it is not faster than the CPU, but it stays flat under load and costs ~5% of a core instead of ~40%. rusticl was numerically wrong and rejected. |
| Vulkan detector backend | `ncnn-vulkan` fp16 (production) and `mnn-vulkan` (alternative), auto-fallback to CPU. Soaked, accuracy-checked against CPU, switched live. |
| Kernel | kernel-16k 7.1.6 installed (default, 7.0.13 kept as fallback); it carries the upstream AVD V4L2 driver. MIT replacement firmware built and installed. |
| Custom ffmpeg | Kwiboo's out-of-tree `v4l2request` hwaccel on FFmpeg 8.1, built in a Debian bookworm container so it runs inside the Frigate image. |
| AVD driver fixes | First hardware tests decoded Baseline streams but failed on every real camera stream. Four driver bugs fixed (see §4). |
| Read-back fix | Decoded frames were in uncached memory (~250 MB/s to copy); cacheable V4L2 buffers made hardware decode a CPU win. |
| GPU rescaling | Zero-copy: AVD frame → Vulkan `hwmap` → `scale_vulkan` → download only 640x360. Needed two ffmpeg patches (one is a Mesa bug workaround). |
| Frigate integration | An `ffmpeg.path` wrapper rewrites Frigate's detect command into the Vulkan chain; compose passes the device nodes and host Mesa libs through. Switched live 20:45. |
| Post-switch incident | Frigate's record maintainer deleted in-progress segments because the process was not *named* `ffmpeg` (exec'd via the host loader). Fixed by exec'ing through an `ffmpeg`-named symlink; ~20 min of recordings lost on this instance only. |

## 3. Final architecture

```
cameras (RTSP/ONVIF)
   │
   ▼
go2rtc (in container) ── restream rtsp://127.0.0.1:8554/<cam>
   │
   ▼  per camera, one ffmpeg (wrapper ffmpeg-avd-vk → /opt/ffmpeg-vk under the host loader)
   ├─ record: -c copy → /tmp/cache (tmpfs) → recordings/          (no decode)
   └─ detect: -hwaccel v4l2request ─► apple_avd (patched, /dev/video0 + /dev/media0)
                 drm_prime dma-buf ─► hwmap ─► scale_vulkan ×2 ─► hwdownload 640x360 NV12
                                          (Honeykrisp via host /usr/lib64 bind-mounted, /dev/dri/renderD128)
                 → rawvideo pipe → Frigate motion/tracking → ZMQ tcp://host.docker.internal:5555
                                                                       │
                                                       frigate-detector.service (host)
                                                       ncnn Vulkan fp16, fox-person-cat-320 (4 classes)
```

## 4. Bugs found and fixed

**Apple AVD kernel driver** (`drivers/media/platform/apple/avd/`, asahi-7.1.6-1) — patches in
`src/avd-driver/patches/final/`:
1. `transform_8x8_mode_flag` (0x40) written into a 2-bit register field without normalising → masked to 0. Every High-profile stream failed on frame one. (Upstream fix exists from 7.1.8; backported.)
2. P-slice weighted-prediction denominators wrong. (Upstream fix; backported.)
3. T8103 `VP_INSN_FIFO_MASK` must be 0: large / high-bitrate / CABAC frames stalled mid-slice. (Upstream 7.1.12; backported. The "early submit" hypothesis also cured it but broke multi-slice frames; kept as optional.)
4. Multi-slice frames with slices > ~16 KiB hit a 1 ms poll timeout. (New.)
5. Diagnostic: dump VP status registers on error/timeout. (New.)
6. Cacheable (non-coherent) MMAP capture buffers via `allow_cache_hints`; read-back 250 MB/s → cached speed. (New.)
7. RFC: `dma-coherent` on the DT node — an in-kernel probe found zero stale lines in 450 frames, so the block appears IO-coherent. (Not verified with a rebuilt DTB.)
8. Reject slices referencing uninitialised DPB entries: every ffmpeg start on a live stream (mid-GOP) caused a DART fault + 2 s watchdog reset. (New.)

**FFmpeg** (Kwiboo `v4l2-request-n8.1` @ b57fbbe) — `src/avd-driver/patches/ffmpeg/`:
1–2. Request `V4L2_MEMORY_FLAG_NON_COHERENT` capture buffers; skip the cache clean on QBUF.
3. Vulkan importer: accept linear multi-plane DRM layers (v4l2request exports one NV12 layer; the importer only handled VAAPI-style one-plane-per-layer).
4. Honeykrisp ignores `pPlaneLayouts[].offset` → pass plane offsets via `memoryOffset` on that driver. **This is a Mesa bug to file.**

**Frigate:** `patches/zmq_ipc.py` (ZMQ detector plugin never retried the model handshake if the
detector was down at start) and the process-name requirement in `record/maintainer.py` (worked
around in the wrapper; a preset patch in `patches/frigate/` would avoid the wrapper entirely).

Firmware was verified identical in behaviour to the reference and needed no change.

## 5. Measurements

Detection (fox-person-cat-320, per inference, host):

| Backend | Latency | Detector CPU |
|---|---|---|
| ONNX Runtime CPU, 4 threads | 12 ms idle, 30–130 ms under load | ~40% of a core |
| ncnn Vulkan fp16 (live) | 31 ms avg, p95 38 ms | ~5% of a core |
| MNN Vulkan fp16 | 24 ms avg | similar |

Decode + scale to 640x360, 150 frames of 2304x1296 High profile, inside the image (user+sys CPU):

| Path | CPU |
|---|---|
| Software decode + swscale (Frigate default, 2 threads) | 0.59–0.64 s |
| AVD decode + read-back (before patch 6) | 3.06 s |
| AVD decode + read-back (after) + swscale | 0.48–0.53 s |
| AVD decode + zero-copy Vulkan scale (live) | 0.21–0.24 s |

Live, four cameras: each ffmpeg ~5% of a core (was ~20%); container 50–57% (was 107%); 10-minute
4-stream soak 0 dropped frames, dmesg silent; total system power ~5 W; GPU-scaled frames 31.6 dB
luma vs swscale bicubic (bilinear vs bicubic, not an error). Full clip matrix (31 clips) bit-identical
to software decode.

## 6. Operational state (as of 21:10)

- Frigate: `~/frigate/docker-compose.yml`, config `config/config.yaml`, verify with `staging/verify.sh`, rollback in `staging/APPLY.md`.
- Kernel module: `/lib/modules/7.1.6-400.asahi.fc44.aarch64+16k/updates/apple-avd.ko` (out-of-tree, taint OE). **Rerun `src/avd-driver/build-and-install.sh` after every kernel-16k update, before rebooting**, until the copr ships ≥ 7.1.12 *and* patches 4/6/8 are upstream.
- Detector: `systemctl status frigate-detector`, backend switch is one flag in the unit.
- Camera `cam1` (<camera-1-ip>) is off the network entirely (no ARP, no ONVIF port anywhere on the /24) — not a password problem.
- KSM disabled (was ~55% of a core for ~230 MB saved).
- The Mac at <mac-ip> is untouched and still the production NVR; cut-over checklist in `DEPLOYMENT.md`.

## 7. What is not done

- **Upstream readiness review: `research/UPSTREAM_REVIEW.md` (2026-09-05).** Headline: drop the three backports (already upstream), rework 0008 into the reference-substitution form other stateless decoders use, hold 0004/0005 pending sofus's open PR #600, shrink 0006 to the two `allow_cache_hints` lines, fix ffmpeg 0004's driver-ID guard (or apply via `memoryOffset` unconditionally) and file the Mesa issue first, hold the Frigate preset for a Discussion; every patch needs real Signed-off-by lines, concise self-written commit messages and AI-assistance disclosure per each project's policy.
- Upstreaming: add Signed-off-by, rebase kernel patches 4/5/6/8 onto current `asahi`, file the Mesa Honeykrisp plane-offset bug and reference it from ffmpeg patch 4, send the ffmpeg Vulkan patches to FFmpeg proper and the v4l2request ones to Kwiboo, send the Frigate preset patch (with docs hunk) upstream, and either the wrapper's process-name lesson or a `cmdline`-based match to Frigate's maintainer.
- DT `dma-coherent` (patch 7) unverified with a rebuilt DTB.
- No GPU utilisation counters on this kernel (the `asahi` driver has no fdinfo yet, so `nvtop` shows nothing); SMC power and the DRM client list are the proxies.
- Detector inference rose from ~30 to ~50 ms once the scalers shared the GPU; fine, but worth watching if more cameras are added.
- Cut-over from the Mac (MQTT prefix, reverse-proxy rule, fresh data snapshot).

## 8. Repositories touched

| Repo | Where | Branch / state |
|---|---|---|
| AsahiLinux/linux (avd driver) | `src/avd-driver/apple-avd/` (standalone git), `src/avd-driver/linux-asahi/` (sparse) | `series` = asahi-7.1.6-1 + 8 patches |
| Kwiboo/FFmpeg | `src/FFmpeg-v4l2request/` (`avd-readback`), `src/ffmpeg-vulkan/FFmpeg/` (`avd-readback-vk`) | 4 patches; binaries `/opt/ffmpeg-avd`, `/opt/ffmpeg-vk` |
| aquarat/frigate (image) | bind-mounted patches in `patches/` | zmq_ipc retry; preset patch drafted |
| AsahiLinux/avd-fw | `src/avd-fw/` | unmodified; firmware installed to `/lib/firmware/apple/` |
| sofus13/libva-v4l2_request, eiln/avd, alibaba/MNN | `src/`, `src/avd-analysis/`, `research/MNN-src/` | reference / alternative backend |

## 9. Detailed documents

| Topic | File |
|---|---|
| Deployment guide, cut-over checklist, transfer gotcha | `DEPLOYMENT.md` |
| Detector service (Linux port, backends, numbers) | `detector/README-linux.md` |
| GPU inference research (runtimes compared) | `research/GPU_INFERENCE.md` |
| AVD state of the art and plan | `research/AVD_DECODE.md` |
| Firmware + custom ffmpeg build (Phase 0) | `research/AVD_PHASE0.md` |
| First hardware results, failure matrix | `research/AVD_PHASE2_FINDINGS.md` |
| Driver root-cause analysis | `research/AVD_ROOTCAUSE.md` |
| Driver build/test harness, results tables | `src/avd-driver/README.md`, `RESULTS.md` |
| Read-back investigation | `research/AVD_READBACK.md` |
| Vulkan rescaling, container ICD recipe | `research/GPU_RESCALE.md` |
| End-to-end integration, wrapper, patch-series status, incident | `research/AVD_FRIGATE_INTEGRATION.md` |
| Apply / verify / rollback runbook | `staging/APPLY.md`, `staging/verify.sh` |
