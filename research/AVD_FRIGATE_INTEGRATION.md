# Frigate on Apple silicon: zero-copy AVD decode + Vulkan rescale — integration (2026-09-05)

Machine: M1 Mac mini (T8103), Fedora Asahi 44, kernel `7.1.6-400.asahi.fc44.aarch64+16k`, Mesa 26.1.8
(Honeykrisp), Frigate `0.18.0-32daf6f4` in `frigate:latest` (bookworm). Companion documents:
`AVD_READBACK.md` (driver + cacheable buffers), `GPU_RESCALE.md` (Vulkan in the image, ICD recipe),
`src/avd-driver/RESULTS.md` (driver fixes), `AVD_PHASE0.md` (builds). Nothing in the running `frigate`
container, `docker-compose.yml` or `config/config.yaml` was changed; the switch is **staged** under
`staging/` (see `staging/APPLY.md`). The system is left on the in-tree `apple_avd` module (`restore.sh` at the end; taint empty).

## TL;DR

* The zero-copy chain works inside `frigate:latest`: AVD decode (`-hwaccel v4l2request
  -hwaccel_output_format drm_prime`) → `hwmap` (dma-buf import into Honeykrisp) → `scale_vulkan` ×2 →
  `hwdownload` of the 640x360 frame → `format=nv12,format=yuv420p` → `-f rawvideo`. **0.21-0.24 s CPU per
  150 frames** of 2304x1296 (single-stage) / 0.27-0.31 s (two-stage), vs **0.60-0.64 s** software (`-threads 2`,
  what Frigate runs today) and 0.48-0.53 s hardware decode + swscale. Luma 31.6 dB PSNR vs swscale bicubic
  (two-stage), chroma 55 dB; 4 concurrent streams identical output; a 10-minute live soak at 5 fps used
  ~2 % of one core per stream, RSS flat at ~100 MB.
* It needed **two new ffmpeg patches** (both generic, `patches/ffmpeg/0003-0004`): FFmpeg's Vulkan importer
  only understood one-plane-per-layer DRM descriptors (VAAPI style) and rejected v4l2request's single NV12
  layer; and Mesa Honeykrisp ignores the plane offset in the explicit DRM-modifier layout (a Mesa bug), so the
  chroma plane was read from offset 0 — worked around by binding at `memoryOffset`.
* It also exposed **one driver bug** (`patches/final/0008`): every time ffmpeg starts on a live RTSP stream
  (non-IDR first frame) the driver programmed the VP with references to an empty DPB → DART translation
  fault + 2 s watchdog reset. The patched driver rejects such slices before touching the hardware.
* Frigate plumbing, prepared but not applied: an `ffmpeg.path` wrapper (`ffmpeg-avd-vk/`) that rewrites
  Frigate's detect command into the chain above; an upstream-style Frigate preset patch
  (`patches/frigate/`); ready-to-copy `staging/docker-compose.yml` + `staging/config.yaml` with
  `staging/APPLY.md`; `src/avd-driver/build-and-install.sh` to make the module persist.

## 1. Pipeline

```
 camera ──RTSP──▶ go2rtc (restream) ──RTSP/tcp──▶ ffmpeg (/opt/ffmpeg-vk, one process per camera, in the container)
                                                     │
                     ┌───────────────────────────────┴───────────────────────────────┐
                     │ output 1: -c:v copy -c:a aac -f segment  → /tmp/cache/*.mp4  │  (record: no decode)
                     │ output 2: detect                                               │
                     │   h264 parser ──slices+controls──▶ /dev/video0 + /dev/media0 (V4L2 Request API)      │
                     │                                          │ apple_avd (patched) → AVD hardware          │
                     │                    CAPTURE dma-buf (NV12 2304x1296, cacheable, never read by the CPU) │
                     │                                          │ AV_PIX_FMT_DRM_PRIME frame                   │
                     │   fps=5 (drops 2 of 3 hw frames, no cost) ─▶ hwmap ──dma-buf fd──▶ Vulkan image (R8 + GR88 planes, Honeykrisp)
                     │                                          │ /dev/dri/renderD128                          │
                     │   scale_vulkan w=max(iw/2,640) ─▶ scale_vulkan 640:360 (compute shaders, GPU)         │
                     │   hwdownload (640x360 NV12, 345 KB) ─▶ format=yuv420p (swscale, tiny) ─▶ -f rawvideo pipe: ─▶ Frigate detect process
                     └────────────────────────────────────────────────────────────────┘
 Frigate → host ZMQ detector (ncnn on the same GPU, Vulkan)  — unaffected (avg ~30 ms, no FALLBACK during all tests)
```

CPU touches: the compressed bitstream (parsing), the 345 KB 640x360 frames, ioctls. The 4.5 MB decoded
frames are never copied by the CPU (in the read-back path they are copied once per frame, in the software
path they are produced by the CPU).

## 2. Building blocks

| piece | where | state |
|---|---|---|
| Driver `apple_avd` | `src/avd-driver/apple-avd` branch `series`, module `apple-avd-fixed-0008.ko` (md5 `cf9e03be…`) = `patches/final/0001-0006 + 0008` | loaded with `reload.sh` for the tests, `restore.sh` afterwards; NOT installed |
| ffmpeg | `/opt/ffmpeg-vk` = Kwiboo `v4l2-request-n8.1` @ `b57fbbe` + `patches/ffmpeg/0001-0004`, `--enable-vulkan --enable-libshaderc`, static bookworm build; source `src/ffmpeg-vulkan/FFmpeg` branch `avd-readback-vk`; previous binary `/opt/ffmpeg-vk-v1` (no patches); `/opt/ffmpeg-avd` = same base + 0001-0002, no Vulkan | `ffmpeg version 403fdd4-vk-b57fbbe`, md5 `0b0d5261…`, hwaccels `drm vulkan v4l2request` |
| Vulkan in the container | host `/usr/lib64` + `ld.so` bind-mounted read-only, ffmpeg started through the host loader, `VK_DRIVER_FILES` → `src/ffmpeg-vulkan/icd/asahi_icd.container.json` (`GPU_RESCALE.md` §3) | SELinux: `container_t` reads the `:ro` binds of `lib_t`/`ld_so_t` without relabel (0 AVC denials) |
| Wrapper | `ffmpeg-avd-vk/bin/{ffmpeg,ffprobe}` | tested with the captured Frigate command lines (§7) |

Rebuild recipe (incremental, after source changes on `avd-readback-vk`):
`src/ffmpeg-vulkan/rebuild-incremental-in-bookworm.sh` run in a throwaway `debian:bookworm` (mounts in the
script header); logs `research/ffmpeg-vk-rebuild-{readback,vkdrm,vkoffset}.log`.

## 3. Getting `hwmap` to work: what failed and the fixes

Variants from `GPU_RESCALE.md` §4, all inside `frigate:latest` via `src/ffmpeg-vulkan/run-in-image.sh`
with `EXTRA_DOCKER_ARGS="--device /dev/video0 --device /dev/media0"` and `timeout -s KILL 30`
(`research/avdvk/{A,B,C,D}.log`):

| variant | first result (unpatched Vulkan importer) | after patches 0003+0004 |
|---|---|---|
| A `-init_hw_device drm=dr:/dev/dri/renderD128 -init_hw_device vulkan=vk@dr`, `hwmap` | `Failed to map frame: -22` | works (same output as B) |
| **B** `-init_hw_device vulkan=vk -filter_hw_device vk`, `hwmap,scale_vulkan=640:360` | `-22` | **works — chosen** (no DRM device needed) |
| C `hwmap=derive_device=drm,hwmap=derive_device=vulkan` | `Failed to created derived device context: -38` (v4l2request has no derive hooks, as predicted) | not needed |
| **D** = B with `scale_vulkan=1152:648,scale_vulkan=640:360` | `-22` | **works — used for quality** (wrapper: `max(iw/2,W)` form) |

1. `-loglevel debug` showed `Unsupported DMABUF layer format 0x3231564e` (`'NV12'`).
   `libavutil/hwcontext_vulkan.c: vulkan_map_from_drm_frame_desc()` creates one `VkImage` per DRM *layer* and
   only maps single-plane fourccs (`R8`, `GR88`, `R16`…): the shape VAAPI exports with
   `VA_EXPORT_SURFACE_SEPARATE_LAYERS`. `hwcontext_v4l2request.c` (and rkmpp) export one `DRM_FORMAT_NV12`
   layer with two planes. **Patch 0003** expands a linear multi-plane layer into per-plane layers
   (NV12/NV16/NV24 → R8+GR88, P010/P012/P016 → R16+GR1616, YUV420/422/444 → R8×3) before import; tiled
   modifiers are left alone. Verified in the debug log: `Expanded multi-plane DRM layer(s) into 2 single-plane
   layers`, `Mapped DRM object to Vulkan!`, `shaderc compile status 'success'`.
2. Then the picture had correct luma (Y PSNR 25.5 dB = exactly the `hwupload` number from `GPU_RESCALE.md`)
   but garbage chroma (U/V 12.5 dB; `research/avdvk/…` first PNG showed green/magenta blotches following the
   luma structure = the chroma image reading the Y bytes). Not the multiplane-format issue
   (`disable_multiplane=1` and the DRM-derived device gave the same result). Cause, from Mesa 26.1.8
   `src/asahi/vulkan/hk_image.c`: with `VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT` Honeykrisp takes only
   `rowPitch` from `VkImageDrmFormatModifierExplicitCreateInfoEXT.pPlaneLayouts[]` (line ~880) and
   positions the plane at `VkBindImageMemoryInfo.memoryOffset` (`hk_BindImageMemory2`, rounded up to
   `HK_PLANE_ALIGN_B` = 128); FFmpeg passes the plane offset (2304×1296 = 2,985,984 for the chroma plane)
   only in the explicit layout and binds at `memoryOffset 0`. Confirmed by an experiment (env-switched build):
   binding at `memoryOffset = offset` gives U/V 54.1 dB. **Patch 0004** does that when
   `driverID == VK_DRIVER_ID_MESA_HONEYKRISP` (single-plane layers: layout offset 0, `memoryOffset` = plane
   offset, checked against the image's memory alignment, allocation size inflated by the offset). This is a
   **Mesa bug** to report (spec: the explicit layout offsets must be honoured; ANV/RADV do); the quirk keeps
   released Mesa working.

## 4. Verification of the chosen chain (cam2-10s.h264, 150 frames 2304x1296, 50 output frames)

Commands (inside the image; `$FFMPEG_VK` = `/host-lib64-ld.so --library-path /host-lib64 /opt/ffmpeg-vk/bin/ffmpeg`):
```sh
# B (single-stage)                                                                                       md5 73811bb3fe93
$FFMPEG_VK -init_hw_device vulkan=vk -filter_hw_device vk -hwaccel v4l2request -hwaccel_output_format drm_prime \
  -i /research/cam2-10s.h264 -vf fps=5,hwmap,scale_vulkan=640:360,hwdownload,format=nv12,format=yuv420p -f rawvideo -pix_fmt yuv420p -y B.yuv
# D (two-stage)                                                                                          md5 fea8b0b44cb8
  ... -vf fps=5,hwmap,scale_vulkan=1152:648,scale_vulkan=640:360,hwdownload,format=nv12,format=yuv420p ...
# software reference (ffmpeg-vk and the image's ffmpeg 7.0 give the same bytes)                         md5 ee810b0f90e2
$FFMPEG_VK -threads 2 -i /research/cam2-10s.h264 -vf fps=5,scale=640:360 -f rawvideo -pix_fmt yuv420p -y sw.yuv
$FFMPEG_VK -f rawvideo -pix_fmt yuv420p -s 640x360 -i D.yuv -f rawvideo -pix_fmt yuv420p -s 640x360 -i sw.yuv -lavfi psnr -f null -
```
All: 17,280,000 bytes, 150 frames decoded, 0 decode errors, `Device 0 selected: Apple M1 (G13G B1)`,
dmesg silent, output md5 identical across every run (single and 4× concurrent).

### Quality (PSNR, 50 frames, 640x360 yuv420p)

| comparison | Y | U | V |
|---|---|---|---|
| B (one bilinear step 3.6×) vs swscale bicubic (Frigate default) | 25.5 dB | 54.1 | 54.1 |
| **D (halve, then 1.8×) vs swscale bicubic** | **31.6 dB** | 55.3 | 55.5 |
| D vs swscale `flags=area` | 34.4 dB | 56.6 | 56.7 |
| B vs swscale `flags=area` | 27.1 dB | 54.6 | 54.4 |
| swscale `area` vs swscale bicubic (two CPU scalers, for scale) | 37.4 dB | 58.7 | 58.5 |

Why not higher: `scale_vulkan` is a 2-tap bilinear sampler; a single 3.6× reduction sees 2 of 3.6 source
pixels per output pixel (aliasing). The two-stage form makes the first step an exact 2× box-like average
and the second a 1.8× bilinear. Two different *correct* CPU scalers (area vs bicubic) only agree to 37 dB,
so 31.6 dB is "a slightly different, marginally crisper scaler", not an error; chroma at 55 dB is smooth.
Frames: `research/avd_vk_sw_frame0.png` (swscale), `research/avd_vk_zero_copy_frame0.png` (B),
`research/avd_vk_zero_copy_2stage_frame0.png` (D) — indistinguishable at a glance. A sharper GPU scaler
would need `libplacebo` (bookworm's is too old for FFmpeg 8.1) or an area/bicubic `scale_vulkan` mode
(upstream feature request).

### CPU / wall (`/usr/bin/time -v` from the host run under the host loader in the image; `src/ffmpeg-vulkan/bench/measure.sh`, raw `research/avdvk/bench-*.txt`)

Single stream, 150 frames in, 50 out, 3 runs each:

| chain | binary | user | sys | **user+sys** | wall | max RSS | md5 |
|---|---|---|---|---|---|---|---|
| software decode + swscale, `-threads 2` (Frigate today) | /opt/ffmpeg-vk | 0.58-0.63 | 0.01 | **0.59-0.64 s** | 0.30 s | 58 MB | ee810b0f |
| same, image's own ffmpeg 7.0 | /usr/lib/ffmpeg/7.0 | 0.59-0.76 | 0.02 | 0.61-0.78 s | 0.30-0.55 s | 66-91 MB | ee810b0f |
| hw decode + read-back + swscale | /opt/ffmpeg-avd | 0.40-0.41 | 0.08-0.12 | **0.48-0.53 s** | 0.63-0.65 s | 119 MB | ee810b0f |
| **hw decode + Vulkan scale, zero-copy (B)** | /opt/ffmpeg-vk | 0.15-0.18 | 0.05-0.06 | **0.21-0.24 s** | 0.66-0.69 s | 174-179 MB | 73811bb3 |
| **hw decode + Vulkan two-stage (D)** | /opt/ffmpeg-vk | 0.19-0.23 | 0.07-0.08 | **0.27-0.31 s** | 0.72-0.77 s | 176-178 MB | fea8b0b4 |

The Vulkan rows include ~0.2 s of one-off CPU (instance/device creation, shaderc compiling the scale shader,
`GPU_RESCALE.md` §1) that a Frigate process pays once per start; the steady-state per-frame cost is therefore
well under the 0.5 ms/output-frame the table implies. Wall time of the hardware rows is the VP's throughput
(~250 fps), irrelevant for live streams. RSS is higher (Vulkan driver + shader compiler mapped).

4 concurrent (`measure.sh conc-… 4`):

| chain ×4 | per process user / sys | wall (all four) | md5 |
|---|---|---|---|
| zero-copy B | 0.17-0.18 / 0.06-0.09 s | 2.81 s (VP-serialised) | all 73811bb3 |
| zero-copy D | 0.24-0.28 / 0.05-0.07 s | 2.81 s | all fea8b0b4 |
| software `-threads 2` | 0.66-0.75 / 0.01-0.03 s | 1.03 s | all ee810b0f |

dmesg silent; `frigate-detector` (ncnn on the same GPU): inference avg 29-32 ms, max rose to 85 ms during the
4× runs (from ~40 ms), no FALLBACK/errors.

## 5. Live soak from the go2rtc restream

`src/ffmpeg-vulkan/bench/soak.sh <camera> 600`: the real Frigate input (`-rtsp_transport tcp -timeout 10000000 -i
rtsp://host.docker.internal:8554/<camera>`, `--add-host host.docker.internal:host-gateway`), the two-stage
zero-copy chain, `-r 5`, `-f null -`, `-t 600`, under `/usr/bin/time -v`, container RSS sampled every 30 s
with `docker stats` (raw: `research/avdvk/soak/`).

**Single stream, cam2 (2304x1296 15 fps High), module 0001-0006** (`soak/single/cam2.txt`):

| item | result |
|---|---|
| frames out | **3000 in 10:00.9** (= 600 s × 5 fps exactly; `dup=5 drop=0`; 9000 frames decoded at 15 fps) |
| CPU | 6.13 s user + 6.55 s sys = 12.7 s per 600 s = **2.1 % of one core** (the sys half is the per-frame ioctl/request traffic at 15 fps) |
| RSS | 96-101 MB flat over the 10 minutes (`docker stats`), max 182 MB (`time -v`, includes the shader-compiler peak at start) |
| dmesg | one DART fault + one watchdog line at the start (§6), silent for the remaining 9:58 |
| detector | inference avg 31-33 ms, max 78-83 ms, no FALLBACK |
| first attempt | ended after 1:52 with `Failed reading RTSP data: Connection timed out` — the camera feed itself dropped (Frigate's own cam2 ffmpeg logged the same error and its watchdog restarted it at the same second); 458 frames in 91.6 s, 1.15 s user + 0.91 s sys, kept as `soak/cam2-attempt1-camera-dropped.txt` |

**Four streams (cam2, cam3, cam4, cam5; cam1 is offline), module 0001-0006 + 0008:**

Four `soak.sh` instances started within the same second (four simultaneous mid-GOP joins), 10 minutes
(`soak/{cam2,cam3,cam4,cam5}.{txt,rss}`):

| camera | stream | frames out | user + sys (600 s) | % of one core | RSS (docker stats) | max RSS | ffmpeg errors |
|---|---|---|---|---|---|---|---|
| cam2 | 2304x1296 15 fps High | 3000 (`drop=0`) | 5.72 + 4.61 s | 1.7 % | 97-101 MB | 181 MB | 0 |
| cam3 | **2560x1440 20 fps** High | 3000 (`drop=0`) | 6.04 + 5.61 s | 1.9 % | 109-114 MB | 210 MB | 0 |
| cam4 | 2304x1296 15 fps High | 3000 (`drop=0`) | 5.80 + 4.28 s | 1.7 % | 98-102 MB | 181 MB | 0 |
| cam5 | 2304x1296 15 fps High | 3000 (`drop=0`) | 5.15 + 5.30 s | 1.7 % | 98-101 MB | 181 MB | 0 |

Total for four cameras: **~43 s CPU per 600 s ≈ 7 % of one core** (the CPU-decoding Frigate today: each
detect ffmpeg is ~0.6 s CPU per 150 decoded frames at 15 fps → ~6 % of a core per camera, ~25 % for four).
dmesg: **nothing** from the four starts to the end (module 0008: the start-up fault is gone; with 0001-0006
each start had cost one DART fault + watchdog reset). Detector during the run: avg 32-33 ms, max 81-90 ms,
no FALLBACK. cam3's 2560x1440 exercised the wrapper's `max(iw/2,640)` rule (1280x720 intermediate).
Decoder nodes free afterwards (`fuser` empty).

## 6. The stream-start fault and driver patch 0008

Symptom, at the start of *every* live decode (both soak attempts, `soak/dmesg-mark-*`):
```
apple-dart 269010000.iommu: translation fault: status:0x80000002 stream:0 code:0x2 (NO PMD FOR IOVA) at 0x8080800
avd 269080000.avd: Frame processing timed out! Vp: 255 (00)          (2 s later: watchdog reset of the VP)
```
and in ffmpeg `[V4L2RequestContext] Failed waiting on OUTPUT buffer 0` + `Decoding error: Invalid data found`
for the first frame; decoding then continues normally. Cause: joining an RTSP stream mid-GOP, the first
frames are P-frames whose references do not exist. ffmpeg's `v4l2_request_h264.c: fill_ref_list()` leaves
the index of a reference it cannot find in the DPB at 0, and the DPB control is empty. The driver's
`stream_refs()` pushes reference headers/RVRA addresses only for VALID DPB entries (none), but the slice
command still says `AVD_OP_REF_DBP_IDX(0)`, so the VP dereferences an uninitialised reference slot → DART
fault → the frame never completes → watchdog. In production this would cost every ffmpeg (re)start a 2 s
stall and a hardware reset while the other three streams are decoding.

Reproducer: `research/cam2-10s-noidr.h264` = cam2-10s with its first IDR NAL removed (SPS, PPS, then
P-frames; the next IDR is frame 31). `src/ffmpeg-vulkan/bench/noidr-test.sh` decodes it with the zero-copy
chain at full rate to `-f framemd5` and compares the frames after that IDR with the same frames from the
intact clip:

| module | dmesg | ffmpeg errors | frames out | md5 of the 120 post-IDR frames |
|---|---|---|---|---|
| 0001-0006 (`apple-avd-fixed.ko`) | DART fault at 0x8080800 + `Frame processing timed out! Vp: 255` | 2 (`Failed waiting on OUTPUT buffer`, `Decoding error`) | 120 (= software: ffmpeg drops frames before the first keyframe) | identical to the intact clip |
| **0001-0006 + 0008 (`apple-avd-fixed-0008.ko`)** | **silent** | **0** | 120 | identical to the intact clip |

Patch `patches/final/0008` (`avd_h264_refs_valid()`, ~40 lines): before anything is pushed, check that every
entry of the P/B slice's `ref_pic_list0/1` (up to `num_ref_idx_lX_active_minus1 + 1` entries) has an index
`< V4L2_H264_NUM_DPB_ENTRIES` whose DPB entry is `V4L2_H264_DPB_ENTRY_FLAG_VALID`; otherwise return `-EINVAL`
from `avd_h264_run()` (→ `avd_job_finish(ctx, VB2_BUF_STATE_ERROR)`, the client gets the capture buffer back
with `V4L2_BUF_FLAG_ERROR`, as for corrupt input). The wrapper test (§7a) then started three live decodes
without a single dmesg line. Module `apple-avd-fixed-0008.ko` (md5 `cf9e03be…`) = branch `series`.
Not changed: the pre-existing assumption that VALID DPB entries are contiguous from index 0 (ffmpeg and
GStreamer fill them that way).
Regression check of the 0008 module: `test_clips.sh` (GStreamer `v4l2slh264dec`, coherent buffers, TIMEOUT=90)
31 OK-identical, 1 SKIP(empty), 0 FAIL, no regression against `baseline-results.txt`
(`src/avd-driver/results-final-0008.txt`).

## 7. Frigate plumbing (prepared, not applied)

### 7a. `ffmpeg.path` wrapper: `ffmpeg-avd-vk/bin/ffmpeg`, `bin/ffprobe`

Frigate runs one ffmpeg per camera with both roles in one command (captured from `docker exec frigate ps -eo args`):
```
/usr/lib/ffmpeg/7.0/bin/ffmpeg -hide_banner -loglevel warning -threads 2 -user_agent FFmpeg Frigate/0.18.0-32daf6f4 -rtsp_transport tcp -timeout 10000000 -i rtsp://127.0.0.1:8554/cam2 -f segment -segment_time 10 -segment_format mp4 -reset_timestamps 1 -strftime 1 -c:v copy -c:a aac /tmp/cache/cam2@%Y%m%d%H%M%S%z.mp4 -r 5 -vf fps=5,scale=640:360 -threads 2 -f rawvideo -pix_fmt yuv420p pipe:
```
(`camera.py:_get_ffmpeg_cmd`: `[ffmpeg_path] + global_args + hwaccel_args(if detect) + input_args + -i + record
output + detect output`; the detect output comes from `PRESETS_HW_ACCEL_SCALE["default"]` = `-r {fps} -vf
fps={fps},scale={w}:{h}` because a non-preset `hwaccel_args` always selects `default`;
`output_args.detect` adds `-threads 2 -f rawvideo -pix_fmt yuv420p`.) The jsmpeg/birdseye encoders
(`-f rawvideo … -i pipe: -f mpegts -codec:v mpeg1video`), exports (`-c copy`, or the `default` libx264 encode
preset — a non-preset `hwaccel_args` never reaches an encode command) and `ffprobe` also use `ffmpeg.path`,
as does go2rtc (`ffmpeg.bin`).

Rewrite rules (POSIX sh, ~100 lines, comments in the file):
1. Exec `/opt/ffmpeg-vk/bin/ffmpeg` via `/host-lib64-ld.so --library-path /host-lib64` when those mounts
   exist, else directly; export `VK_DRIVER_FILES` (default `/etc/vulkan-asahi/asahi_icd.container.json`).
2. Rewrite **only if all of**: argv contains the adjacent pair `-hwaccel v4l2request`; every `-vf` value is
   exactly `fps=<num>,scale=<W>:<H>`; `-f rawvideo` is present; none of `-hwaccel_output_format`,
   `-init_hw_device`, `-filter_hw_device`, `-filter_complex`, `-lavfi` is present; `AVD_VK_REWRITE` is not `0`.
   Then: after `-hwaccel v4l2request` insert `-init_hw_device vulkan=vk -filter_hw_device vk
   -hwaccel_output_format drm_prime`; replace each `-vf` value with
   `fps=N,hwmap,scale_vulkan=w=max(iw/2\,W):h=max(ih/2\,H),scale_vulkan=W:H,hwdownload,format=nv12,format=yuv420p`
   (`AVD_VK_SCALE=single`: `fps=N,hwmap,scale_vulkan=W:H,hwdownload,format=nv12,format=yuv420p`).
   The record output (`-c:v copy`) in the same command is untouched.
3. Otherwise pass through unchanged. With `-hwaccel v4l2request` and no rewrite, ffmpeg still decodes on the
   AVD and reads the frames back (patches 0001-0002 make that cheap); without `/dev/video0` it falls back to
   software with exit 0.
4. Log `[ffmpeg-avd-vk] rewrite=… exec: <full command>` to stderr when a rewrite happened (Frigate shows it in
   the camera's ffmpeg log) or always with `AVD_VK_DEBUG=1`.

Test, `src/ffmpeg-vulkan/bench/wrapper-test.sh 20` (throwaway `frigate:latest` with exactly the staged mounts,
devices and env, wrapper at `/opt/ffmpeg-avd-vk`, live go2rtc restream; output `research/avdvk/wrapper-test/run.txt`):

| command fed to the wrapper | result |
|---|---|
| the captured cam2 detect+record line above (`-hwaccel v4l2request` added, `127.0.0.1` → `host.docker.internal`), 20 s | `rewrite=1`; detect pipe **101 frames of 640x360 yuv420p** (5 fps); record segments `cam2@…mp4` 13.4 s + 6.9 s, `h264 2304x1296 + aac`, probe clean; the logged command is the one from §4 with the `max(iw/2\,640)` two-stage scaler |
| Frigate's `ffprobe -v quiet -print_format json -show_format -show_streams …` | streams `pcm_alaw` + `h264 2304x1296`, via the wrapper's ffprobe |
| jsmpeg/birdseye encoder `-f rawvideo … -i pipe: -f mpegts -codec:v mpeg1video` (25 frames of B.yuv) | 406 KB of mpegts, no rewrite/log |
| export-style `-i segment.mp4 -c copy -movflags +faststart` | rc 0, no rewrite/log |
| detect line with `AVD_VK_REWRITE=0`, 8 s | `rewrite=0`, 41 frames (hardware decode + read-back + swscale) |
| is it Honeykrisp? | the ffmpeg process maps `libvulkan_asahi.so`; nothing maps lvp/llvmpipe |

Three live-stream starts in this test, module 0008 loaded: dmesg silent.

### 7b. Upstream-style Frigate patch: `patches/frigate/0001-Add-ffmpeg-presets-for-Apple-silicon-AVD-via-v4l2req.patch`

Against Frigate `0.18.0-32daf6f4` (`frigate/ffmpeg_presets.py`, `frigate/const.py`; originals kept as
`*.orig` next to it). Adds `preset-apple-avd` (`-hwaccel v4l2request`, default swscale) and
`preset-apple-avd-vulkan` (decode `-init_hw_device vulkan=vk -filter_hw_device vk -hwaccel v4l2request
-hwaccel_output_format drm_prime`, scale `-r {0} -vf fps={0},hwmap,scale_vulkan=w=max(iw/2\,{1}):h=max(ih/2\,{2}),scale_vulkan=w={1}:h={2},hwdownload,format=nv12,format=yuv420p`).
With it, `hwaccel_args: preset-apple-avd-vulkan` produces the chain without any wrapper rewrite (the wrapper
would then only do the loader/ICD part and pass the command through: it sees `-init_hw_device` and does not
touch it). Not applied; it could be bind-mounted like `patches/zmq_ipc.py`. Still to do for submission:
docs section, note that `auto` does not pick it.

### 7c. Staged compose/config: `staging/docker-compose.yml`, `staging/config.yaml`, `staging/APPLY.md`

Complete copies of the current files plus: `devices` `/dev/video0 /dev/media0 /dev/dri/renderD128`; volumes
`/opt/ffmpeg-vk:ro,z`, `./ffmpeg-avd-vk:/opt/ffmpeg-avd-vk:ro,z`, `/usr/lib64:/host-lib64:ro` and
`/usr/lib/ld-linux-aarch64.so.1:/host-lib64-ld.so:ro` (**no** `:z` — that would relabel the host's system
libraries; `container_t` may read `lib_t` as is), `./src/ffmpeg-vulkan/icd:/etc/vulkan-asahi:ro,z`; env
`VK_DRIVER_FILES`/`VK_ICD_FILENAMES`; config `ffmpeg: path: /opt/ffmpeg-avd-vk`, `hwaccel_args: [-hwaccel,
v4l2request]`. Both validated (`docker compose config`, YAML load). `APPLY.md` has the preconditions, the
baseline to capture, the apply commands, 10-minute verification checks and three rollback levels.

### 7d. Module persistence: `src/avd-driver/build-and-install.sh`

Builds branch `series` (0001-0006 + 0008) against the running kernel, installs to
`/lib/modules/$(uname -r)/updates/apple-avd.ko`, `depmod -a`; `--status` shows what `modprobe` would pick,
the loaded module's taint and whether the initramfs contains the module (`lsinitrd --kver $(uname -r) | grep
apple-avd` — empty on this machine, so no `dracut -f` is needed; udev loads the module from the DT alias after
the root fs is up); `--undo` removes it. Not run: `modinfo -F filename apple_avd` still points at the in-tree
`.ko.xz`.

## 8. State of the upstream patch series

`src/avd-driver/patches/final/` (kernel, against AsahiLinux `asahi-7.1.6-1`, `drivers/media/platform/apple/avd/`):

| patch | one line | status |
|---|---|---|
| 0001 h264: fix transform_8x8_mode_flag encoding | backport of 8e8d72a673ab (in 7.1.8) | already upstream |
| 0002 h264: fix default weights handling for P slices | backport of 3cba4f1d5b86 (in 7.1.12) | already upstream |
| 0003 t8103: set VP_INSN_FIFO_MASK to 0 | backport of ca9a850f237f (in 7.1.12) | already upstream |
| 0004 h264: scale the held-slice parse poll timeout with the slice size | multi-slice frames > ~16 KiB/slice | ready; needs Signed-off-by, send to asahi + linux-media |
| 0005 dump the VP status registers on error/timeout | diagnostic, error path only | optional, could be dropped or sent as debug aid |
| 0006 allow cacheable (non-coherent) MMAP buffers | `allow_cache_hints`, drop `DMA_ATTR_NO_KERNEL_MAPPING`; read-back 250 MB/s → 20 GB/s | ready; needs Signed-off-by; mention the videobuf2 `dma_alloc_noncontiguous` WARN when `NO_KERNEL_MAPPING` is set |
| 0007 [RFC] t8103 DT: dma-coherent on the avd node | client-independent alternative to 0006; coherency shown empirically on one T8103 only | RFC for discussion with the Asahi people (T6000/T8112?) |
| 0008 h264: reject slices that reference invalid DPB entries | DART fault + watchdog reset at every live-stream start | new today; tested (§6); needs Signed-off-by; consider the hantro/rkvdec style substitution instead of -EINVAL if maintainers prefer |

`src/avd-driver/patches/ffmpeg/` (Kwiboo `v4l2-request-n8.1` @ `b57fbbe`; 0003/0004 apply to FFmpeg master too):

| patch | one line | status |
|---|---|---|
| 0001 avutil/hwcontext_v4l2request: request cacheable CAPTURE buffers | `V4L2_MEMORY_FLAG_NON_COHERENT` when the queue supports cache hints | ready for Kwiboo's branch; Signed-off-by |
| 0002 avcodec/v4l2_request: skip the cache clean when queueing CAPTURE buffers | `V4L2_BUF_FLAG_NO_CACHE_CLEAN` | ready; Signed-off-by |
| 0003 avutil/hwcontext_vulkan: import linear multi-plane DRM layers (NV12, P010, …) | expand one NV12 layer into R8+GR88 layers before import | ready for ffmpeg-devel (independent of v4l2request; also helps rkmpp→Vulkan); Signed-off-by; would benefit from a test on ANV/RADV with a VAAPI-less NV12 dma-buf (e.g. kmsgrab) |
| 0004 avutil/hwcontext_vulkan: pass DRM plane offsets through memoryOffset on Honeykrisp | works around Mesa ignoring `pPlaneLayouts[].offset` | ready as a quirk; file the Mesa issue first (hk_image.c, `hk_image_init`) and reference it; `VK_HEADER_VERSION >= 290` guard for `VK_DRIVER_ID_MESA_HONEYKRISP` |

`patches/frigate/`:

| patch | one line | status |
|---|---|---|
| 0001 Add ffmpeg presets for Apple silicon (AVD via v4l2request, Vulkan scaling) | `preset-apple-avd`, `preset-apple-avd-vulkan` in `ffmpeg_presets.py`/`const.py` | draft; needs docs hunk, a Frigate build with a v4l2request+Vulkan ffmpeg to be meaningful upstream (Frigate bundles its own ffmpeg), and a maintainer discussion on shipping such an ffmpeg for arm64 |

Mesa: one bug to file (Honeykrisp ignores the explicit DRM-modifier plane layout offset).

What remains before submission, in order: fill in `Signed-off-by`, rebase 0004/0006/0008 onto current
asahi (0001-0003 drop out), run the clip matrix (`test_clips.sh`) on that build, send the kernel series to
the Asahi tree first (they carry the driver), the ffmpeg patches to Kwiboo (0001/0002) and ffmpeg-devel
(0003/0004), the Mesa issue, and the Frigate PR last (it depends on an ffmpeg build being available).

## 9. Files produced today

```
/opt/ffmpeg-vk/bin/{ffmpeg,ffprobe}                          rebuilt (403fdd4-vk-b57fbbe); /opt/ffmpeg-vk-v1 = previous build
src/ffmpeg-vulkan/FFmpeg                                     branch avd-readback-vk (4 commits on b57fbbe)
src/ffmpeg-vulkan/rebuild-incremental-in-bookworm.sh         incremental rebuild recipe
src/ffmpeg-vulkan/bench/{measure.sh,soak.sh,wrapper-test.sh} measurement / soak / wrapper harnesses
src/avd-driver/patches/ffmpeg/0003-*, 0004-*                 new ffmpeg patches
src/avd-driver/patches/final/0008-*                          new driver patch; apple-avd-fixed-0008.ko; build-and-install.sh
research/avdvk/                                              raw outputs: *.log, *.yuv, bench-*.txt, soak/, wrapper-test/
research/avd_vk_{sw,zero_copy,zero_copy_2stage}_frame0.png   frame 0 of each chain
research/cam2-10s-noidr.h264                              cam2-10s without its first IDR (stream-start reproducer)
research/ffmpeg-vk-rebuild-*.log                             build logs
ffmpeg-avd-vk/                                               the ffmpeg.path wrapper + README
patches/frigate/                                             Frigate preset patch + originals
staging/                                                     docker-compose.yml, config.yaml, APPLY.md
```

## Post-switch incident (2026-09-05 20:45–21:10): recordings discarded — fixed

Symptom after the live switch: `watchdog.<camera> WARNING: Invalid recording segment detected` every
10 s on all cameras and `frigate.record.maintainer: Invalid or missing video stream in segment ...
Discarding.`; no files reached `recordings/`. Detection was fine (ffmpeg was writing the segments
normally — `/proc/<pid>/fdinfo` showed the segment fd advancing — but the file was already `(deleted)`).

Cause: `record/maintainer.py:158` only treats cache segments as *in use* when the process holding
them is literally named `ffmpeg` (`psutil process.name()`). The wrapper exec'd the real binary through
the host loader `/host-lib64-ld.so`, so the process was named `host-lib64-ld.s` and Frigate validated
and deleted every segment while it was still being written (no `moov` atom yet → "missing video
stream"). The standalone wrapper test could not catch this: it never runs Frigate's maintainer.

Fix (`ffmpeg-avd-vk/`): `libexec/ffmpeg` and `libexec/ffprobe` are symlinks to `/host-lib64-ld.so`
(dangling on the host, resolve inside the container) and both wrappers exec the loader through them,
so `comm` is `ffmpeg`/`ffprobe`. No compose change needed (the wrapper dir is a bind mount). Applied
live by killing the running decoders (Frigate respawns them). `staging/verify.sh` now checks the name.
Upstream angle: the Frigate preset patch avoids the wrapper entirely; alternatively the maintainer
could match on `cmdline` instead of `name`.
