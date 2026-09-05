# GPU rescale for Frigate's detect pipeline — Vulkan (Honeykrisp) preparation — 2026-09-05

Host `<nvr-host>`: Mac mini M1 (T8103), Fedora Asahi 44, kernel `7.1.6-400.asahi.fc44.aarch64+16k`, Mesa 26.1.8
(Honeykrisp Vulkan 1.4, `/usr/share/vulkan/icd.d/asahi_icd.aarch64.json` -> `/usr/lib64/libvulkan_asahi.so`),
`/dev/dri/renderD128` world-rw. Frigate `0.18.0` in `frigate:latest` (debian bookworm, glibc 2.36).
Goal of this phase: prove that ffmpeg can scale decoded frames to 640x360 on the M1 GPU via Vulkan, both on
the host and inside the Frigate image, so that the AVD (v4l2request, `drm_prime`) frames can later be mapped
into Vulkan without a CPU read-back. Companion docs: `AVD_PHASE0.md`, `AVD_PHASE2_FINDINGS.md`, `GPU_INFERENCE.md`.

Constraints honoured: no `apple_avd` load/unload, `/dev/video0`/`/dev/media0` never opened, nothing under
`src/FFmpeg-v4l2request`, `/opt/ffmpeg-avd*`, the `frigate` container/compose/config/detector untouched.
Side effects on the host: `dnf install ffmpeg-free golang` (ffmpeg-free 8.1.2, golang 1.26.7), `/opt/ffmpeg-vk`,
files under `src/ffmpeg-vulkan/`, files listed below under `research/`. `frigate-detector` was watched throughout:
no FALLBACK/errors; inference avg rose from ~28 ms to ~30.5 ms (max 68 ms) while the GPU tests ran, back to
normal afterwards.

## TL;DR

| Question | Answer |
|---|---|
| Vulkan scaling works on the host? | **Yes.** `/opt/ffmpeg-vk` (and Fedora's `ffmpeg-free` via `libplacebo`): `hwupload,scale_vulkan=640:360,hwdownload` on Honeykrisp, deterministic, output visually identical to `scale=640:360`. |
| Works inside `frigate:latest`? | **Yes**, with the "Fedora ld.so" ICD recipe below (`src/ffmpeg-vulkan/run-in-image.sh`). Same bytes as on the host (md5 `bf073d2e…` for the 150-frame 640x360 output on host, in plain bookworm and in the Frigate image). |
| Image's own ffmpeg 7.0 (`/usr/lib/ffmpeg/7.0`) | Loads the ICD fine but its Vulkan hwcontext returns **all-zero frames** on Honeykrisp (even a bare `hwupload,hwdownload` round trip: PSNR 5 dB). FFmpeg 8.1 (ffmpeg-free 8.1.2 and `/opt/ffmpeg-vk`) is bit-exact on the same round trip. Do not use 7.0 for this. |
| CPU cost, software decode -> GPU scale | **Worse than CPU scaling.** 150 frames 2304x1296 -> 640x360: CPU `scale` adds ~0.45 s CPU (3.0 ms/frame); `hwupload+scale_vulkan+hwdownload` adds ~0.98 s (6.5 ms/frame), almost all of it in `hwupload` (4.5 ms/frame) + `hwdownload`. |
| CPU cost when the frame is already on the GPU (AVD zero-copy case, measured with a Vulkan-resident NV12 source) | **~0.9 ms CPU/frame** for `scale_vulkan=640:360,hwdownload,format=nv12,format=yuv420p` (0.17 ms scale dispatch + ~0.8 ms download/convert of the 640x360 frame). Versus 3.0-3.5 ms/frame CPU scaling and the 18 ms/frame uncached NV12 read-back measured in `AVD_PHASE2_FINDINGS.md`. GPU throughput ~640 frames/s at 1296p for one stream. |
| dma-buf import possible on Honeykrisp? | **Yes.** `VK_EXT_external_memory_dma_buf`, `VK_EXT_image_drm_format_modifier`, `VK_KHR_external_memory_fd/semaphore_fd` present; queried directly (`vkGetPhysicalDeviceImageFormatProperties2`): `R8_UNORM` and `R8G8_UNORM` (FFmpeg's per-plane NV12 images) with modifier `DRM_FORMAT_MOD_LINEAR` (0x0) are *importable* as dma-buf with sampled+storage usage, max 16384x16384. v4l2request exports NV12 with `DRM_FORMAT_MOD_LINEAR`. |
| Quality | `scale_vulkan` only has `bilinear`/`nearest`. At 3.6x reduction bilinear (2 taps) aliases: Y PSNR vs swscale's default bicubic is 25.5 dB (U/V 54 dB). It is not a bug (upscale 640->1280: 62 dB; exact 2x: 57 dB vs `flags=area`; CPU `flags=fast_bilinear` scores the same 23 dB). Two-stage `scale_vulkan=1152:648,scale_vulkan=640:360` gives 31.5 dB for +0.2 ms/frame. Decide after looking at motion-detection noise in practice. |
| Recommended route | **Container route** (`/opt/ffmpeg-vk` + ld.so/ICD recipe). The coordinator route also works end-to-end (proved) and is the fallback if the recipe ever bites. Details in "Recommendation". |

## 1. Host feasibility (Fedora ffmpeg-free 8.1.2 + /opt/ffmpeg-vk)

`sudo dnf install ffmpeg-free` gives ffmpeg 8.1.2 with `--enable-vulkan --enable-libshaderc --enable-libplacebo`
**but no h264 decoder and no `scale_vulkan`** (Fedora's build only has `libopenh264` for H.264; `scale_vulkan`
is absent from its filter list — it needs `spirv_compiler` at build time). So the host quick test used
`-c:v libopenh264` + the `libplacebo` filter; the real numbers below use `/opt/ffmpeg-vk` (a bookworm binary that
runs unchanged on the host, like `/opt/ffmpeg-avd`).

Device selection is correct on the host without any env: `Device 0 selected: Apple M1 (G13G B1) (integrated)`
(llvmpipe is device 1). The `MESA: error: Opening /dev/dri/card1 failed: Permission denied` lines are harmless.

### Commands (host, `cd ~/frigate/research`)
```sh
F=/opt/ffmpeg-vk/bin/ffmpeg
# reference
$F -v error -i cam2-10s.h264 -vf scale=640:360,format=yuv420p -f rawvideo -pix_fmt yuv420p -y cpu.yuv
# Vulkan
$F -v error -init_hw_device vulkan=vk -i cam2-10s.h264 \
   -vf format=yuv420p,hwupload,scale_vulkan=640:360,hwdownload,format=yuv420p -f rawvideo -pix_fmt yuv420p -y vk.yuv
# NV12 in/out (what the AVD path will look like after the map; scale_vulkan cannot convert yuv420p->nv12, it keeps the input format)
$F -v error -init_hw_device vulkan=vk -i cam2-10s.h264 \
   -vf format=nv12,hwupload,scale_vulkan=640:360,hwdownload,format=nv12,format=yuv420p -f rawvideo -pix_fmt yuv420p -y vk_nv12.yuv
# compare
$F -f rawvideo -pix_fmt yuv420p -s 640x360 -i vk.yuv -f rawvideo -pix_fmt yuv420p -s 640x360 -i cpu.yuv -lavfi psnr -f null -
# ffmpeg-free + libplacebo variant (host only; needs no custom build)
ffmpeg -init_hw_device vulkan=vk -c:v libopenh264 -i cam2-10s.h264 \
   -vf format=yuv420p,hwupload,libplacebo=w=640:h=360:format=yuv420p,hwdownload,format=yuv420p -f rawvideo -pix_fmt yuv420p -y placebo.yuv
```

### Measurements, host, `/opt/ffmpeg-vk`, 150 frames 2304x1296 H.264 High (`/usr/bin/time -v`, user+sys)
| Pipeline | real | user | sys | CPU delta vs decode-only |
|---|---|---|---|---|
| decode only (`format=yuv420p -f null`) | 0.26 s | 0.66 | 0.04 | — |
| decode + `scale=640:360` (swscale bicubic) | 0.36 s | 1.10 | 0.05 | +0.45 s = **3.0 ms/frame** |
| decode + `hwupload,scale_vulkan=640:360,hwdownload` | 0.96 s | 1.61 | 0.07 | +0.98 s = **6.5 ms/frame** |
| decode + `format=nv12,hwupload,scale_vulkan,hwdownload,format=nv12,format=yuv420p` | 1.53 s | 1.99 | 0.10 | +1.39 s (extra full-size yuv420p->nv12 swscale) |
| decode + `hwupload` only (no download) | 0.67 s | 1.35 | 0.03 | +0.68 s = **4.5 ms/frame** (the upload copy) |
| decode + `hwupload,hwdownload` full size (no shader) | 1.18 s | 2.00 | 0.02 | +1.32 s = 8.8 ms/frame (upload + full download) |
| Frigate-like `fps=5,…scale_vulkan…` (30 frames out) | 0.53 s | 1.06 | 0.06 | +0.42 s (includes ~0.2 s one-off init, see below) |
| Frigate-like `fps=5,scale=640:360` | 0.26 s | 0.80 | 0.04 | +0.14 s |
| 4 concurrent `fps=5` Vulkan scalers, total | 0.71 s | 4.00 | 0.18 | vs 4 CPU: 0.49 s / 3.00 / 0.12 |
| one-off init (instance + device + shaderc compile of the scale shader), 1 frame | 0.19 s | 0.19 | 0.02 | CPU-only 1 frame: 0.10 s |

Same pipeline inside `frigate:latest` (via `run-in-image.sh`, bash `time`): decode-only 0.63/0.04, CPU scale
1.29/0.06, Vulkan 1.60/0.06 — identical picture. PSNR of the Vulkan output vs CPU output was the same number in
all three environments (host / plain bookworm / Frigate image): `y:25.52 u:54.15 v:54.14`, md5 identical across runs.

**Where the CPU goes in the Vulkan path:** the `hwupload` copy of the full 2304x1296 frame into a Vulkan image
(4.5 ms/frame) and the `hwdownload` (full-size ≈4.3 ms/frame; 640x360 ≈0.8 ms/frame). The compute dispatch itself
is ~0.17 ms/frame of CPU. Conclusion: **with software decode the GPU scaler is a net CPU loss**; it only pays off
when the decoded frame is already in GPU-visible memory and is never touched by the CPU at full size, i.e. the AVD
`drm_prime` -> Vulkan dma-buf import.

### Measurements with a GPU-resident source (proxy for the AVD zero-copy path)
`color_vulkan` (a Vulkan source filter, frames never exist in CPU memory) at 2304x1296 NV12, 1500 frames:
```sh
$F -v error -y -init_hw_device vulkan=vk -filter_hw_device vk \
   -filter_complex 'color_vulkan=size=2304x1296:rate=25:format=nv12,scale_vulkan=640:360,hwdownload,format=nv12,format=yuv420p[o]' \
   -map '[o]' -frames:v 1500 -f rawvideo -pix_fmt yuv420p /dev/null
```
| Pipeline (1500 frames) | real | user | sys | CPU per frame |
|---|---|---|---|---|
| source only (`-f null`) | 0.10 s | 0.09 | 0.01 | 0.07 ms |
| + `scale_vulkan=640:360` (no download) | 0.66 s | 0.17 | 0.08 | 0.17 ms |
| + `hwdownload,format=nv12` (640x360) | 1.83 s | 1.06 | 0.10 | 0.77 ms |
| + `hwdownload,format=nv12,format=yuv420p` (Frigate wants yuv420p) | 2.35 s | 1.15 | 0.21 | **0.9 ms** |
| two-stage `scale_vulkan=1152:648,scale_vulkan=640:360` + download + convert | 3.45 s | 1.39 | 0.21 | 1.07 ms |
| `fps=5` before the scaler (1500 in -> 300 out) | 0.49 s | 0.27 | 0.06 | 1.1 ms per *output* frame; dropped hw frames cost ~nothing |
| no scale, `hwdownload` of the full 2304x1296 NV12 frame | 4.56 s | 6.09 | 0.08 | 4.1 ms (cached memory; the AVD vb2 buffers are uncached and cost 18 ms/frame — that read is exactly what the GPU path removes) |
| CPU reference: `testsrc2` 2304x1296 -> `scale=640:360` | 1.68 s | 6.68 | 0.12 | 3.5 ms (5.2 s minus 1.5 s source) |

### Quality
| Comparison (150 frames, 640x360 yuv420p) | PSNR |
|---|---|
| `scale_vulkan` (bilinear) vs swscale default (bicubic) | y 25.5 / u 54.1 / v 54.1 dB |
| `scale_vulkan` vs swscale `flags=area` | y 27.1 dB |
| swscale `flags=fast_bilinear` vs swscale default (CPU-only control) | y 23.0 dB |
| two-stage `scale_vulkan=1152:648,scale_vulkan=640:360` vs swscale default | y 31.5 dB |
| `scale_vulkan` upscale 640x360 -> 1280x720 vs swscale `flags=bilinear` | y 62.1 dB (no offset bug) |
| `scale_vulkan` 2304x1296 -> 1152x648 vs swscale `flags=area` | y 57.2 dB |
| `libplacebo=w=640:h=360` (ffmpeg-free) vs swscale default | y 27.4 dB regardless of `range/colorspace/dithering=none` — visually fine, not investigated further |

The target of ">40 dB" is not reached because `scale_vulkan` is a plain 2-tap bilinear sampler and 2304->640 is a
3.6x reduction (each output pixel sees 2 of 3.6 source pixels). Chroma is smooth, hence 54 dB. Frames:
`research/gpu_rescale_cpu_frame0.png` vs `research/gpu_rescale_scale_vulkan_frame0.png` (and `…_placebo_frame0.png`)
— indistinguishable at a glance, the Vulkan one is slightly crisper/aliased. For Frigate's motion detector
(frame differencing on the 640x360 image) aliasing adds a little high-frequency noise; the two-stage chain is
the cheap fix if it matters. `libplacebo` has proper filters but bookworm's 4.208 is too old for FFmpeg 8.1
(needs >= 5.229) — not built.

## 2. Build: `/opt/ffmpeg-vk` (Kwiboo v4l2-request-n8.1 @ b57fbbe + Vulkan)

Source: `~/frigate/src/ffmpeg-vulkan/FFmpeg` (shallow clone of `Kwiboo/FFmpeg` branch
`v4l2-request-n8.1`, HEAD `b57fbbe50c9b2656fad86a1a7eeabfd2b2a50935` — same as Phase 0).
Build container: `debian:bookworm` arm64 named `ffbuild-vk` (removed afterwards). Scripts (they document the
exact steps and are re-runnable): `src/ffmpeg-vulkan/build-shaderc-in-bookworm.sh`, `src/ffmpeg-vulkan/build-in-bookworm.sh`.
Logs: `research/ffmpeg-vk-build.log`, `research/shaderc-static-build.log`. Package versions of the build
container: `src/ffmpeg-vulkan/bookworm-build-packages.txt`.

```sh
mkdir -p ~/frigate/src/ffmpeg-vulkan && cd ~/frigate/src/ffmpeg-vulkan
git clone -b v4l2-request-n8.1 --depth 1 https://github.com/Kwiboo/FFmpeg FFmpeg          # b57fbbe
cp -r /usr/include/vulkan /usr/include/vk_video vk-headers/                                 # host vulkan-headers 1.4.341
sudo mkdir -p /opt/ffmpeg-vk && sudo chown $USER:$USER /opt/ffmpeg-vk
docker run -d --name ffbuild-vk --platform linux/arm64 -v $PWD:/src:z -v ~/frigate/src/uapi-7.0.13:/uapi:z \
  -v /opt/ffmpeg-vk:/opt/ffmpeg-vk:z -v ~/frigate/research:/research:z debian:bookworm sleep infinity
docker exec ffbuild-vk sh -c 'apt-get update -qq && apt-get install -y -qq --no-install-recommends build-essential nasm pkg-config \
  ca-certificates git libdrm-dev libudev-dev libssl-dev libx264-dev libopus-dev zlib1g-dev libvulkan-dev libshaderc-dev glslang-dev spirv-tools cmake python3'
docker exec ffbuild-vk sh /src/build-shaderc-in-bookworm.sh    # ~6 min: shaderc v2026.3 (glslang 16.4.0, SPIRV-Tools v2026.3) -> /opt/shaderc-static (static, PIC)
docker exec ffbuild-vk sh /src/build-in-bookworm.sh            # configure + make -j8 + install -> /opt/ffmpeg-vk
docker rm -f ffbuild-vk
```
Configure line (= Phase 0 line + Vulkan):
```
./configure --prefix=/opt/ffmpeg-vk --enable-gpl --enable-version3 --enable-static --disable-shared --disable-doc --disable-debug \
  --enable-pic --enable-openssl --enable-libx264 --enable-libopus --enable-libdrm --enable-libudev --enable-v4l2-request \
  --enable-vulkan --enable-libshaderc --pkg-config-flags=--static \
  --extra-cflags="-I/uapi -I/src/vk-headers" --extra-libs="-lstdc++ -lm -lpthread" --extra-version=vk-b57fbbe
# with PKG_CONFIG_PATH=/opt/shaderc-static/lib/pkgconfig and PATH=/opt/shaderc-static/bin:$PATH
```
Result: `ffmpeg version b57fbbe-vk-b57fbbe`, 31 MB static binary; `-hwaccels`: `drm vulkan v4l2request`;
filters include `hwmap hwupload hwdownload scale_vulkan` (+ the other `*_vulkan` filters); config.mak:
`CONFIG_VULKAN=yes CONFIG_VULKAN_1_4=yes CONFIG_LIBSHADERC=yes CONFIG_SCALE_VULKAN_FILTER=yes CONFIG_V4L2_REQUEST=yes CONFIG_LIBDRM=yes CONFIG_LIBUDEV=yes`.
`ldd` inside `frigate:latest` (native, bookworm glibc): `libdrm.so.2 libm libz libssl.so.3 libcrypto.so.3 libudev.so.1 libstdc++.so.6 libgcc_s.so.1 libc` — all present in the image, nothing "not found".
FFmpeg `dlopen()`s `libvulkan.so.1` at run time (no link-time dependency); the image has `libvulkan.so.1.3.239`
(bookworm `libvulkan1`) — but see the ICD section: that loader is not the one we end up using.

### Things that broke during the build, and the fixes (all captured in the scripts)
1. **Vulkan headers**: bookworm `libvulkan-dev` is 1.3.239; FFmpeg 8.1 needs >= 1.3.277 (and >= 1.4.317 for `vulkan_1_4`). Fix: overlay the host's `vulkan-headers-1.4.341` via `-I/src/vk-headers` (header-only, same trick as the `/uapi` overlay).
2. **Static shaderc, take 1**: Debian's `libshaderc_combined.a` (219 KB) is *not* combined — glslang/SPIRV-Tools are unbundled -> undefined `spvBinaryDestroy`, `spvtools::…`. Fix attempt: patch `shaderc.pc` with `-Wl,--start-group -lshaderc -lglslang -lMachineIndependent -lGenericCodeGen -lOSDependent -lOGLCompiler -lSPIRV -lglslang-default-resource-limits -lSPIRV-Tools-opt -lSPIRV-Tools -Wl,--end-group -lstdc++`. That links, and produced a binary whose `scale_vulkan` then failed **at run time** with `scale:10: error: '#extension' : extension not supported: GL_EXT_expect_assume` (`shaderc compile status 'error'`): FFmpeg 8.1's runtime GLSL needs a glslang newer than bookworm's 12.0.
3. **Build-time shader compiler**: the FFV1/ProRes Vulkan hwaccels precompile `.comp.glsl` with `glslang`; bookworm's 12.0 fails the same way (`GL_EXT_expect_assume`). Interim fix: the host's Fedora `glslang` 16.2 copied into the container with its lib closure (`ld-linux-aarch64.so.1 libc libm libstdc++ libgcc_s libSPIRV-Tools{,-opt}`) and wrapped as `/usr/local/bin/glslang` -> `exec /opt/host-glslang/ld-linux-aarch64.so.1 --library-path /opt/host-glslang /opt/host-glslang/glslang "$@"` (the same "run a Fedora binary under its own loader" trick used for the ICD below).
4. **Static shaderc, take 2 (final)**: build shaderc from source in the container (`build-shaderc-in-bookworm.sh`: tag `v2026.3`, `git-sync-deps` pulls glslang `168d452a` (16.4.0) + SPIRV-Tools `b707790a`; cmake `-DBUILD_SHARED_LIBS=OFF -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DSHADERC_SKIP_TESTS=ON …`), install to `/opt/shaderc-static` with a hand-written `shaderc.pc` (`-lshaderc_combined -lstdc++ -lm -lpthread`). Its `bin/glslang` also replaces the item-3 hack. Copy kept at `src/ffmpeg-vulkan/shaderc-static/` (64 MB, static libs + headers + glslang/glslc binaries).
5. **Stale link**: after switching shaderc, `make` did not relink (`ffmpeg`'s link rule doesn't depend on `config.mak`), so `make install` re-copied the old binary. Fix: `rm -f libavfilter/vulkan_shaderc.o libavfilter/libavfilter.a libswscale/libswscale.a ffmpeg ffmpeg_g ffprobe ffprobe_g && make && make install`.
6. Minor: `git rev-parse` in the bind-mounted tree needs `git config --global --add safe.directory /src/FFmpeg`; `libplacebo` skipped (bookworm 4.208 < required 5.229); `testsrc_vulkan` did not get compiled in (needs an extra dep), `color_vulkan` was used as the GPU-resident source instead.

## 3. Vulkan ICD inside the container — the glibc problem and the recipe

### What is where
* Host ICD json `/usr/share/vulkan/icd.d/asahi_icd.aarch64.json` -> `/usr/lib64/libvulkan_asahi.so` (14 MB, api 1.4.354). `/etc/vulkan/icd.d/` is empty.
* Its `ldd` closure (no LLVM needed, unlike llvmpipe): `libdrm.so.2 libz libzstd libxcb* libX11-xcb libxshmfence libwayland-client libdisplay-info.so.3 libudev libSPIRV-Tools.so libexpat libstdc++ libm libgcc_s libc libXau libffi` — all Fedora 44 libs.
* Symbol versions required by `libvulkan_asahi.so`: up to **`GLIBC_2.38`** (bookworm has 2.36) and Fedora's libstdc++ 16 ABI.
* Fedora's dynamic loader is `/usr/lib/ld-linux-aarch64.so.1` (NOT under `/usr/lib64`); libs are in `/usr/lib64`.
* `frigate:latest` already contains `libvulkan.so.1.3.239` and Mesa ICDs `broadcom freedreno lvp radeon` (bookworm 22.3) under `/usr/share/vulkan/icd.d/` — **`lvp` (llvmpipe) is auto-selected if the Asahi ICD is not provided**, and ffmpeg then happily "GPU"-scales on the CPU (`Device 0 selected: llvmpipe (LLVM 15.0.6…)`). Always pin `VK_DRIVER_FILES` and check the verbose "Device 0 selected" line.

### What fails (documented so nobody retries it)
| Attempt (plain `debian:bookworm` + `vulkan-tools`, `--device /dev/dri/renderD128`, `-v /usr/lib64:/host-lib64:ro`, ICD json pointing at `/host-lib64/libvulkan_asahi.so`) | Result |
|---|---|
| bookworm `libvulkan1` loader loads the Fedora ICD | `loader_icd_scan: Failed loading library … libdrm.so.2: cannot open shared object file` -> `vkCreateInstance: Found no drivers!` `ERROR_INCOMPATIBLE_DRIVER` (plain bookworm has no libdrm; the Frigate image has one but the next error follows anyway) |
| same + `LD_LIBRARY_PATH=/host-lib64` (Fedora libs under bookworm's ld.so) | `vulkaninfo: symbol lookup error: /host-lib64/libc.so.6: undefined symbol: __tunable_is_initialized, version GLIBC_PRIVATE` — libc and ld.so must be the same glibc build |
| `/opt/ffmpeg-vk` run natively in the image with `-init_hw_device vulkan` | works but silently selects **llvmpipe** |

### What works: run the ffmpeg process under Fedora's own loader
Bind-mount the host's `/usr/lib64` (read-only, no relabel needed — SELinux is Enforcing and reads were allowed)
and the host's `ld.so`, and start ffmpeg through that loader with `--library-path`. Only that process sees the
Fedora userspace (glibc 2.43, libstdc++ 16, Mesa 26.1.8, Fedora's `libvulkan.so.1.4.341` loader, libdrm/libudev/
openssl3/zlib), the rest of the container is untouched. The bookworm-built ffmpeg needs only glibc <= 2.36
symbols so it runs fine on 2.43 (same reason `/opt/ffmpeg-avd` runs on the host).
```sh
docker run --rm -i --device /dev/dri/renderD128 \
  -v /usr/lib64:/host-lib64:ro \
  -v /usr/lib/ld-linux-aarch64.so.1:/host-lib64-ld.so:ro \
  -v ~/frigate/src/ffmpeg-vulkan/icd:/etc/vulkan-asahi:ro,z \
  -v /opt/ffmpeg-vk:/opt/ffmpeg-vk:ro,z -v ~/frigate/research:/research:z \
  -e VK_DRIVER_FILES=/etc/vulkan-asahi/asahi_icd.container.json -e VK_ICD_FILENAMES=/etc/vulkan-asahi/asahi_icd.container.json \
  --entrypoint /host-lib64-ld.so frigate:latest --library-path /host-lib64 /opt/ffmpeg-vk/bin/ffmpeg \
  -v error -init_hw_device vulkan=vk -i /research/cam2-10s.h264 \
  -vf fps=5,format=yuv420p,hwupload,scale_vulkan=640:360,hwdownload,format=yuv420p -f rawvideo -pix_fmt yuv420p - | wc -c   # 17280000
```
`src/ffmpeg-vulkan/icd/asahi_icd.container.json` is the host json with `library_path` = `/host-lib64/libvulkan_asahi.so`.
`src/ffmpeg-vulkan/run-in-image.sh` wraps exactly this (`RUN_IMAGE=debian:bookworm` for the plain image,
`RUN_ENTRY=bash` for a shell where `$FFMPEG_VK` is the loader+ffmpeg prefix, `EXTRA_DOCKER_ARGS="--device /dev/video0 --device /dev/media0"` for the AVD step).

Proof points (all with the recipe):
* `vulkaninfo --summary`: bookworm's `vulkan-tools` binary under the Fedora loader in plain bookworm, and the Fedora `vulkaninfo` inside `frigate:latest` -> `deviceName = Apple M1 (G13G B1)`, `driverName = Honeykrisp`, `driverInfo = Mesa 26.1.8`, `apiVersion 1.4.354`.
* `/opt/ffmpeg-vk` `hwupload,hwdownload` round trip: PSNR inf (bit-exact) on host, plain bookworm, `frigate:latest`.
* `/opt/ffmpeg-vk` `scale_vulkan` chain: identical output (md5) in all three; `frigate:latest` numbers in section 1.
* Even the image's stock `/usr/lib/ffmpeg/7.0/bin/ffmpeg` (static except glibc) starts under the recipe and compiles the `scale_vulkan` shader with its own shaderc — but produces zero frames (7.0 hwcontext vs Mesa 26 issue, see TL;DR).

Caveats of the recipe:
* It is coupled to the host's Mesa: `dnf update mesa*` changes what the container runs (that is also the point — one ICD to maintain). After a glibc update the loader and `/usr/lib64` still match because both come from the host at the same time.
* `libvulkan.so.1` used is Fedora's 1.4.341 (from `/host-lib64`), not the image's 1.3.239. Fedora's loader also reads `/usr/share/vulkan/implicit_layer.d` *inside the container* (bookworm's `VK_LAYER_MESA_device_select` is not present there; harmless).
* Cannot be expressed as an `ffmpeg.path` for Frigate directly (Frigate execs `<path>/bin/ffmpeg`): make a tiny wrapper, e.g. `/opt/ffmpeg-vk-wrap/bin/ffmpeg` = `#!/bin/sh\nexec /host-lib64-ld.so --library-path /host-lib64 /opt/ffmpeg-vk/bin/ffmpeg "$@"` and point `ffmpeg.path: /opt/ffmpeg-vk-wrap` (Frigate only needs `ffmpeg` and `ffprobe` under `bin/`). Set `VK_DRIVER_FILES` in the compose `environment:`. Not done here (live container untouched).
* Process must be able to open `/dev/dri/renderD128` (world-rw here; in compose: `devices: - /dev/dri/renderD128`).

### Alternative evaluated: host ffmpeg via the wrapper/coordinator (`src/wrapper-coordinator`)
Built for linux-arm64 with Fedora's golang 1.26.7 (`GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -trimpath -ldflags="-s -w"`):
`src/wrapper-coordinator/dist/{coordinator,wrapper}-linux-arm64` (static, copies in `src/ffmpeg-vulkan/dist/`).
Test: coordinator on the host bound to the docker bridge (`listen_addr: 172.17.0.1:17347`, `ffmpeg_path: /usr/bin/ffmpeg`,
`path_mappings: /research -> ~/frigate/research`), wrapper copied to `/usr/local/bin/ffmpeg` in a
plain bookworm container with `FRIGATE_IPC_ADDR=172.17.0.1:17347`:
`ffmpeg -init_hw_device vulkan=vk -c:v libopenh264 -i /research/cam2-10s.h264 -vf …libplacebo=w=640:h=360…,hwdownload -f rawvideo -pix_fmt yuv420p - | wc -c`
-> **51840000 bytes (150 frames) over the proxied stdout**, exit codes relayed (missing input -> 254), paths remapped.
Wrapper overhead in the container: ~0.06 s CPU for 52 MB of piped rawvideo. (Coordinator stopped afterwards; nothing installed.)
It works and is Frigate-transparent, but: every ffmpeg then runs outside the container (host CPU accounting,
host needs the AVD nodes and the RTSP/go2rtc reachability, path mappings for `/media/frigate/*` and `/tmp/cache`,
one more daemon to keep alive), and the AVD ffmpeg would have to be the host one anyway (`/opt/ffmpeg-vk` runs on the host too, so that part is easy).

### Recommendation
Use the **container route**: `/opt/ffmpeg-vk` + `run-in-image.sh`-style mounts (`/usr/lib64` -> `/host-lib64`,
Fedora `ld.so`, ICD json, `VK_DRIVER_FILES`, `renderD128`) and a 2-line `bin/ffmpeg` wrapper for `ffmpeg.path`.
Evidence: identical output and CPU numbers to the host, no Frigate changes beyond compose mounts/env and
`ffmpeg.path`, one binary that has both `v4l2request` and `vulkan`, and the failure mode is loud (ffmpeg exits
non-zero if the ICD can't load — just make sure `VK_DRIVER_FILES` is set so it can't fall back to llvmpipe).
Keep the coordinator as plan B; it is proven and needs no glibc tricks because ffmpeg then *is* a host process.

## 4. What the AVD-integration agent should do next

> **Done 2026-09-05 (later the same day)** — see `AVD_FRIGATE_INTEGRATION.md`: variant B/D work after two ffmpeg
> patches (Vulkan import of single-layer NV12 dma-bufs; Honeykrisp ignores the explicit plane offset), 0.21-0.31 s CPU
> per 150 frames, staged Frigate wiring under `staging/`. The notes below are kept as written before that work.

The hardware frames come out of the v4l2request hwaccel as `AV_PIX_FMT_DRM_PRIME` frames (NV12, one dma-buf,
`DRM_FORMAT_MOD_LINEAR`, `hwcontext_v4l2request.c`), on a frames context of type `AV_HWDEVICE_TYPE_V4L2REQUEST`.
Relevant FFmpeg facts (this tree):
* `hwcontext_vulkan.c`: `vulkan_map_to()` accepts `AV_PIX_FMT_DRM_PRIME` sources (`vulkan_map_from_drm`: per-layer images, `VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT` with the frame's modifier, dma-buf fd `dup()`ed and imported, usage sampled|transfer_src (+storage|transfer_dst for write maps)). `av_hwframe_map()` tries the source's `map_from` first (v4l2request's only maps to *software* formats -> ENOSYS) and then the destination's `map_to` -> the DRM import. So a plain `hwmap` filter with a Vulkan `-filter_hw_device` should do it.
* `vulkan_device_derive()` only derives from `VAAPI` and `DRM` parents — **not** from `V4L2REQUEST`, so `hwmap=derive_device=vulkan` will fail with "Failed to create derived device". Create the Vulkan device explicitly.
* The `fps` filter works on hw frames; put it *before* `hwmap` so only 5 fps get mapped/scaled.
* `scale_vulkan` keeps the input pixel format (NV12 in -> NV12 out; it cannot emit yuv420p from nv12), so download as nv12 and convert the small frame with swscale (`format=nv12,format=yuv420p`, ~0.1 ms).
* Honeykrisp side (checked with a small C query on the host): `R8_UNORM`/`R8G8_UNORM` importable as dma-buf with modifier 0x0 (LINEAR) and the Apple modifiers `0xc00000000000001/2`, sampled+storage usage, up to 16384x16384. `VK_KHR_external_semaphore_fd` is there too (ffmpeg uses it only for sync export; the v4l2 side has no fence, ffmpeg waits on the CPU side anyway).

Chains to try, in order (inside the image via `run-in-image.sh` with `EXTRA_DOCKER_ARGS="--device /dev/video0 --device /dev/media0"`, or on the host with `/opt/ffmpeg-vk/bin/ffmpeg` directly):
```sh
# A. Vulkan device derived from a DRM device on the render node (the pattern used for rkmpp/vaapi -> vulkan):
ffmpeg -loglevel verbose -init_hw_device drm=dr:/dev/dri/renderD128 -init_hw_device vulkan=vk@dr -filter_hw_device vk \
  -hwaccel v4l2request -hwaccel_output_format drm_prime -i cam2-10s.h264 \
  -vf fps=5,hwmap,scale_vulkan=640:360,hwdownload,format=nv12,format=yuv420p -f rawvideo -pix_fmt yuv420p - | wc -c    # expect 17280000

# B. Vulkan device created directly (single GPU; with VK_DRIVER_FILES set only Honeykrisp is enumerated):
ffmpeg -loglevel verbose -init_hw_device vulkan=vk -filter_hw_device vk \
  -hwaccel v4l2request -hwaccel_output_format drm_prime -i cam2-10s.h264 \
  -vf fps=5,hwmap,scale_vulkan=640:360,hwdownload,format=nv12,format=yuv420p -f rawvideo -pix_fmt yuv420p - | wc -c

# C. If hwmap refuses (e.g. "Failed to map hardware frames"/"Invalid mapping"), try mapping explicitly through DRM first:
#    ... -vf fps=5,hwmap=derive_device=drm,hwmap=derive_device=vulkan,scale_vulkan=640:360,hwdownload,format=nv12,format=yuv420p ...
#    (v4l2request has no drm derive hook either; this is the last resort before touching hwcontext_v4l2request.c to add
#     .device_derive/.frames_derive_to for DRM, which is a ~40-line patch modelled on hwcontext_drm.c)
# D. Quality variant: fps=5,hwmap,scale_vulkan=1152:648,scale_vulkan=640:360,hwdownload,format=nv12,format=yuv420p
```
Verify: `-loglevel verbose` must show `Device 0 selected: Apple M1`, `shaderc compile status 'success'`, no
`Failed setup for format drm_prime`, and `-vf hwmap` must not fall back to a CPU path (compare `user` time with
the current 18 ms/frame read-back path: expect ~1-2 ms/frame at 5 fps output). Compare pixels with the
software chain (`PSNR y ≈ 25 dB / u,v ≈ 54 dB` is the expected bilinear number; ≈ 5 dB means garbage, ≈ inf
means it silently downloaded and scaled on the CPU). Also watch `dmesg` for AGX faults on import of the
vb2-dma-contig buffers (system memory, should be fine; the GPU has its own UAT page tables).

Frigate config once A/B works (not applied): compose `devices: /dev/dri/renderD128, /dev/video0, /dev/media0`,
volumes `/opt/ffmpeg-vk:ro`, `/usr/lib64:/host-lib64:ro`, `/usr/lib/ld-linux-aarch64.so.1:/host-lib64-ld.so:ro`,
`src/ffmpeg-vulkan/icd:/etc/vulkan-asahi:ro`, env `VK_DRIVER_FILES=/etc/vulkan-asahi/asahi_icd.container.json`;
`ffmpeg.path: /opt/ffmpeg-vk-wrap` (wrapper script, see above); `hwaccel_args: -init_hw_device vulkan=vk -filter_hw_device vk -hwaccel v4l2request -hwaccel_output_format drm_prime`.
Frigate builds the detect `-vf` from `PRESETS_HW_ACCEL_SCALE` keyed by the *preset name* (`ffmpeg_presets.py:232`,
`parse_preset_hardware_acceleration_scale`: any `hwaccel_args` containing a space falls into `"default"` =
`-r {fps} -vf fps={fps},scale={w}:{h}`, which would run swscale on `drm_prime` frames and fail). Frigate 0.18's own
`preset-vulkan` expands to `-hwaccel vulkan -init_hw_device vulkan=gpu:0 -filter_hw_device gpu -hwaccel_output_format vulkan`
with scale `-r {fps} -vf fps={fps},hwupload,scale_vulkan=w={w}:h={h},hwdownload` — that is Vulkan *video decode*
(`VK_KHR_video_decode_h264`, which Honeykrisp does not expose), so it would fall back to software decode and land in the
CPU-loss path measured above. `preset-rkmpp` shows the shape we need (`-hwaccel rkmpp -hwaccel_output_format drm_prime` +
`-vf scale_rkrga=…,hwmap=mode=read,format=yuv420p`). Two ways to get the AVD chain into Frigate, neither done here:
1. **Frigate patch (2 dict entries)**: add `"preset-avd-vulkan"` to `PRESETS_HW_ACCEL_DECODE` (`-init_hw_device vulkan=vk -filter_hw_device vk -hwaccel v4l2request -hwaccel_output_format drm_prime`) and to `PRESETS_HW_ACCEL_SCALE` (`-r {0} -vf fps={0},hwmap,scale_vulkan=w={1}:h={2},hwdownload,format=nv12,format=yuv420p`), then `hwaccel_args: preset-avd-vulkan`. Cleanest; survives as a bind-mounted `ffmpeg_presets.py` or a small image overlay.
2. **Argument-rewriting wrapper** at `/opt/ffmpeg-vk-wrap/bin/ffmpeg` (the script that already has to exec the Fedora loader): when argv contains `-hwaccel v4l2request`, replace `fps=N,scale=W:H` in the `-vf` value with `fps=N,hwmap,scale_vulkan=W:H,hwdownload,format=nv12,format=yuv420p`. No Frigate change, but fragile across Frigate upgrades.

## Artifacts
```
/opt/ffmpeg-vk/bin/ffmpeg, ffprobe                         31 MB each, ffmpeg version b57fbbe-vk-b57fbbe (static ffmpeg + shaderc/glslang/x264/opus; dynamic: libdrm libudev ssl3 z libstdc++)
~/frigate/src/ffmpeg-vulkan/FFmpeg/            source + objects (b57fbbe), 337 MB
~/frigate/src/ffmpeg-vulkan/vk-headers/        vulkan-headers 1.4.341 copied from the host
~/frigate/src/ffmpeg-vulkan/shaderc-static/    static shaderc v2026.3 + glslang 16.4.0 + SPIRV-Tools (64 MB), shaderc-build/shaderc = source (build dir pruned)
~/frigate/src/ffmpeg-vulkan/build-in-bookworm.sh, build-shaderc-in-bookworm.sh, bookworm-build-packages.txt
~/frigate/src/ffmpeg-vulkan/icd/asahi_icd.container.json
~/frigate/src/ffmpeg-vulkan/run-in-image.sh    ffmpeg-vk inside frigate:latest with all mounts/env
~/frigate/src/ffmpeg-vulkan/dist/               coordinator-linux-arm64, wrapper-linux-arm64 (also in src/wrapper-coordinator/dist/)
~/frigate/research/ffmpeg-vk-build.log, shaderc-static-build.log
~/frigate/research/gpu_rescale_{cpu,scale_vulkan,placebo}_frame0.png
```
Containers `ffbuild-vk` and `vktest` removed; coordinator stopped; no changes to `frigate`.
