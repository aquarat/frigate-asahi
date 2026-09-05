# Apple AVD hardware decode for Frigate on this M1 Mac mini (T8103)

Research only, 2026-09-05. Host: `<nvr-host>`, Apple Mac mini (M1, 2020, `apple,j274` / T8103),
Fedora Asahi Remix 44, kernel `7.0.13-400.asahi.fc44.aarch64+16k`, Frigate 0.18.0-32daf6f4
(local image `frigate:latest`), 5 H.264 RTSP cameras, CPU decode today.

## Status summary (read this first)

1. **A working AVD driver exists upstream and Fedora Asahi already ships it — just not in the kernel this box is running.**
   `drivers/media/platform/apple/avd/` (author: sofus, MIT) landed in `AsahiLinux/linux` on
   2026-06-30 (commit `9d77b27426e3 "media: apple: add avd driver"`), with VP9 (`edd1529cbf30`,
   07-06), HEVC (`95738fb3e5cd`, 07-15) and AV1 (`0d6dcb9b11b4`, 08-02). It is a **V4L2 stateless
   M2M decoder using the Request API** (`V4L2_PIX_FMT_H264_SLICE`, NV12/P010/NV16 capture) and it
   has a `apple,t8103-avd` node in `t8103.dtsi`. It is present from tag `asahi-7.1.5-1` onward.
   Fedora Asahi's `kernel-16k-modules-7.1.6-400.asahi.fc44` contains
   `kernel/drivers/media/platform/apple/avd/apple-avd.ko.xz`; `7.0.13` (installed) does not, and the
   7.0.13 DTB has no `avd` node. **Upgrading to kernel-16k 7.1.6 (already in the enabled
   `copr:group_asahi:kernel` repo) is the whole "kernel driver" step.**
2. **The driver does not use Apple's firmware.** It loads a tiny MIT-licensed replacement CM3
   firmware from `AsahiLinux/avd-fw` (for T8103: `apple/avd-fw-v2-t0.bin`). That file is **not
   packaged** in Fedora `linux-firmware-20260622`, `linux-firmware-vendor`, or any Asahi copr; it has
   to be built (clang + llvm-objcopy + meson, ~2 KB of C) or taken from the repo's GitHub Actions
   artifact. Without it the driver's probe fails harmlessly with `failed to load firmware`.
3. **Nothing in the avd-work (T6000) kernel driver needs porting.** That effort (eiln-derived,
   Apple kext firmware, m1n1 EL2 hacks, DT overlay, custom vb2 pool) is superseded by the upstream
   design. The last recorded state was Session 40 (2026-05-08): dart13 m1n1 deployed, Session 41
   verification/first decode never recorded; no decoded frame was ever produced. What *is*
   reusable: the test clip, the netconsole scripts, and the hardware knowledge in `docs/`.
4. **The hard part is userspace/ffmpeg, not the kernel.** Neither upstream FFmpeg (checked
   `master`, `release/8.0`, `release/7.1`) nor any Frigate ffmpeg build has a V4L2 Request API
   hwaccel; ffmpeg's built-in `h264_v4l2m2m` is *stateful*-only and cannot talk to this driver.
   Frigate's arm64 ffmpeg builds (NickM-27/FFmpeg-Builds) are `--enable-libdrm --disable-vaapi`
   (VA-API is explicitly skipped for `linuxarm64` in `scripts.d/50-vaapi/50-libva.sh`). So a
   **custom ffmpeg** is unavoidable: either Kwiboo's out-of-tree `v4l2request` hwaccel (13 commits on
   top of n8.1, the same series LibreELEC ships) or a VA-API build plus sofus's
   `libva-v4l2_request` driver. Frigate supports this cleanly via `ffmpeg.path` (absolute path,
   Frigate appends `/bin/ffmpeg`) and raw `hwaccel_args`.
5. **Realistic effort: ~3–5 working days**, of which driver validation is hours and the ffmpeg
   build/packaging is most of the rest. Test on the M1 Pro first (same driver, `apple,t6000-avd`),
   after restoring stock m1n1 there.

---

## Q1. State of AVD support (Sept 2026)

### Upstream / Asahi kernel

| Item | Finding | Source |
|---|---|---|
| Driver location | `drivers/media/platform/apple/avd/` — `avd-drv.c`, `avd-v4l2.c`, `avd-hw.c`, `avd-h264.c`, `avd-hevc.c`, `avd-vp9.c`, `avd-av1.c`, `avd-inst.h`, `avd.h`; `CONFIG_VIDEO_APPLE_AVD` (depends `VIDEO_DEV`, `MEDIA_CONTROLLER`; selects `V4L2_MEM2MEM_DEV`, `VIDEOBUF2_DMA_CONTIG`, `V4L2_H264`, `V4L2_VP9`) | https://github.com/AsahiLinux/linux/tree/asahi/drivers/media/platform/apple/avd |
| First commit | `9d77b27426e3` 2026-06-30 "media: apple: add avd driver" (sofus); Janne Grunau fixup `8fdfd1a5dd80` 2026-07-15 | `gh api repos/AsahiLinux/linux/commits?sha=asahi&path=drivers/media/platform/apple/avd` |
| Codec history | VP9 `edd1529cbf30` (07-06); HEVC `95738fb3e5cd` (07-15), HEVC offset fix `adb54ad2ed92` (08-08); AV1 `0d6dcb9b11b4` (08-02), "better av1" `adec89957b42` (08-08) | same |
| Tags containing it | `asahi-7.1.5-1`, `asahi-7.1.6-1`, `asahi-7.1.12-1` all have `drivers/media/platform/apple/{avd,isp}`; `asahi-7.0.13-1` does not | GitHub contents API per tag |
| Supported SoCs (`avd_of_match`) | `apple,t8103-avd`, `t6000`, `t8112`, `t6020`, `t8122`, `t8132`, `t8140` | `avd-drv.c` lines ~581–613 |
| T8103 DT node | `avd@268000000`, `compatible = "apple,t8103-avd"`, `reg`: code `0x269080000/0xc000`, sram `0x26908c000/0xc000`, mbox `0x269098000/0x4000`, ctrl `0x269100000/0x10000`; IRQs AIC 540, 541; `power-domains/resets = <&ps_avd_sys>`; `iommus = <&avd_dart 0>`; `avd_dart: iommu@269010000` `apple,t8103-dart`, IRQ 547 | `arch/arm64/boot/dts/apple/t8103.dtsi` (asahi branch, ~line 1410) |
| Other DTs | `t600x-dieX.dtsi`, `t602x-dieX.dtsi`, `t8112.dtsi`, `t6030/t6031`, `t8122.dtsi`, `t8132-pmgr` all mention avd → M1 Pro (T6000) covered too | grep of `arch/arm64/boot/dts/apple/*` |
| Interface | V4L2 M2M mplane, `src_vq->supports_requests = true`, media controller registered (`/dev/mediaN`); OUTPUT `H264_SLICE`/`HEVC_SLICE`/`VP9_FRAME`/`AV1_FRAME`; CAPTURE `NV12`, `P010`, `NV16` (linear — no detiling problem) | `avd-v4l2.c`, `avd-drv.c` |
| Feature matrix | "Video Decoder: M1 (T8103) linux-asahi; M1 Pro/Max/Ultra linux-asahi"; encoder TBA | https://asahilinux.org/docs/platform/feature-support/m1/ |
| Progress reports | 7.1 (2026-06): sofus wrote "a working V4L2 driver for the AVC hardware" using "a blob of custom AVD firmware that simply installs interrupt handlers and applies each variant's set of tunables"; 10-bit AVC to 4K. 7.2 (2026-08): "AVC, HEVC and VP9 all working mostly reliably on all machines supported by Asahi Linux", AV1 on M3+; VA-API layer (megi's, forked by sofus) works with an env var but "does not yet ship by default in Fedora Asahi Remix" | https://asahilinux.org/2026/06/progress-report-7-1/ , https://asahilinux.org/2026/08/progress-report-7-2/ , https://www.phoronix.com/news/Asahi-Linux-AVD-Firmware-M3 |
| m1n1 | No AVD code in `AsahiLinux/m1n1` `src/` (only eiln's 2023 tracer commits in history); v1.6.1 (2026-08-08) notes mention nothing AVD. **No bootloader change needed.** Installed m1n1 1.5.2; 1.6.1 available. | `gh api repos/AsahiLinux/m1n1/...` |

### Firmware (`AsahiLinux/avd-fw`)

- https://github.com/AsahiLinux/avd-fw (MIT, pushed 2026-08-07). `src/avd.c` is the whole runtime:
  `_start()` → `apply_tunables()` (per-variant mask/value table under `src/hw/tunables/`,
  `tunables_v2.h` for T8103) → enable all NVIC IRQs → `reg_write(CM3_BOOT, 1)` → `wfi` loop.
  `src/irq.c` forwards decoder IRQs to the host mailbox.
- Variants built by `meson.build`: `avd-fw-v2-t0` (pad 0xc000) … `v5-t1`. Driver expects
  **`apple/avd-fw-v2-t0.bin` for T8103**, `apple/avd-fw-v3-t0.bin` for T6000, `v3-t1` for T8112/T6020.
- Build: `meson setup --cross-file=./llvm.ini build && ninja -C build` (clang targeting
  `arm-none-eabi`, `llvm-objcopy`), installs to `<libdir>/<firmwaredir>/apple/`. CI
  (`.github/workflows`) builds on `ubuntu-26.04-arm` with LLVM 21 and uploads `avd-fw.tar.xz`.
  No GitHub releases. This host has `ninja` but **no `clang`, `llvm-objcopy`, or `meson`** (dnf-installable).
- Not in Fedora: `dnf repoquery -l linux-firmware linux-firmware-vendor | grep avd` → nothing;
  no `*avd-fw*` package in any enabled repo; `asahi-platform-metapackage-core-0-29` does not pull one.

### Userspace

- **VA-API**: https://github.com/sofus13/libva-v4l2_request (megi/Ondrej Jirman's rewrite,
  v1.2 2026-07-14, + sofus HEVC fixes 2026-07-16, last push 2026-08-08). Usage:
  `LIBVA_DRIVER_NAME=v4l2_request` (+ `LIBVA_DRIVERS_PATH` if not installed),
  optional `LIBVA_V4L2_REQUEST_MEDIA_PATH=/dev/mediaN`. Fedora's packaged
  `libva-v4l2-request-1.0.0-18.20190517gita3c2476` is the **2019 bootlin** code — the one the
  avd-work docs describe as abandoned; do not use it.
- **FFmpeg**: no `v4l2_request*` files in `FFmpeg/FFmpeg` `master`/`release/8.0`/`release/7.1`;
  `configure` has no `v4l2-request` option. Out-of-tree: `Kwiboo/FFmpeg` branch
  `v4l2-request-n8.1` = 13 commits ahead of `n8.1`, 27 files (hwdevice type `v4l2request`,
  `--enable-v4l2-request`, needs `libdrm` + `libudev` + kernel headers ≥ 6.0; H.264/HEVC/MPEG2 from
  the 2024 v2 series, VP9 2025-12, AV1). LibreELEC ships it as a single 247 KB patch against
  ffmpeg 9.0 (`packages/multimedia/ffmpeg/patches/v4l2-request/0001-v4l2-request.patch`). The
  upstream ML status: still unmerged (v2 series Aug 2024, https://ffmpeg.org/pipermail/ffmpeg-devel/2024-August/332034.html).
- **GStreamer**: Fedora's `gstreamer1-plugins-bad-free` ships `libgstv4l2codecs.so`
  (`v4l2slh264dec`) — a zero-build way to validate the driver on the host. (Not installed yet.)
- **Host ffmpeg**: `ffmpeg-free-8.1.2-4.fc44` (Fedora) links `libva.so.2` → VA-API path testable
  on the host with the distro ffmpeg, no custom build.

### This machine, right now (read-only checks)

```
find /lib/modules/7.0.13-400.asahi.fc44.aarch64+16k -iname '*avd*'   → nothing
ls /sys/bus/platform/devices | grep -i avd                            → nothing
ls /dev/video*                                                        → none; /dev/dri has card1, card2, renderD128 (Asahi GPU)
dmesg | grep -i avd                                                   → nothing
ls /proc/device-tree/soc | grep avd                                   → nothing (7.0.13 DTB only has ps "avd_sys" pmgr string)
/boot/config-7.0.13: no CONFIG_VIDEO_APPLE_AVD; VIDEO_DEV=m, MEDIA_CONTROLLER=y, V4L2_MEM2MEM_DEV=m, VIDEOBUF2_DMA_CONTIG=m, APPLE_DART=m
dnf repoquery kernel-16k: 6.19.13, 6.19.14, 7.0.13 (installed), 7.1.5, 7.1.6 (available)
dnf repoquery -l kernel-16k-modules-7.1.6-400.asahi.fc44 | grep avd   → .../kernel/drivers/media/platform/apple/avd/apple-avd.ko.xz
grubby: entries for 6.19.13, 7.0.13, 6.19.14 (three kernels retained → a 7.1.6 install keeps 7.0.13 as fallback)
/lib/firmware/apple/avd-fw.bin: 61592 B, unowned, sha256 aa3cafa1…  = the Apple-kext blob from avd-work (remote/avd/linux/firmware/apple-avd-fw.bin). Wrong name and wrong firmware for the upstream driver; remove it to avoid confusion.
```

---

## Q2. avd-work (T6000) vs what T8103 needs

### Hardware-level differences (avd-work docs + macOS ioreg dump vs upstream DT/driver)

| | T6000 / M1 Pro (avd-work) | T8103 / M1 (this box) |
|---|---|---|
| AVD aperture | `0x286000000`, size `0x1404000` (ioreg `avd0`) | `0x268000000` (eiln `fw/avd/__init__.py`), same internal layout |
| Sub-blocks used by upstream | IMEM `+0x1080000`, SRAM `+0x108c000`, mailbox `+0x1098000`, decode/ADS ctrl `+0x1100000` — identical offsets to avd-work's `hardware-reference.md` | DT `reg`: `0x269080000` code, `0x26908c000` sram, `0x269098000` mbox, `0x269100000` ctrl (= same offsets from `0x268000000`) |
| DART | `dart-avd0@87010000`, `"dart,t8110"` → Linux `apple,t8110-dart`; NOC/permission regs at `+0x20c/0x220/0x224` (session 31 root cause) | `iommu@269010000`, `apple,t8103-dart` (T8020-style); eiln's `mask32(0x269010060/68/6c)` are the T8103 offsets that did *not* apply on T6000 |
| Interrupts | AIC 0x3f3/0x3f4 (1011/1012), DART 0x3fa | AIC 540/541, DART 547 (DT) |
| Driver variant (upstream) | `avd_t6000_variant`: fw `avd-fw-v3-t0.bin`, revision 4, `t8112_configure_stream` (FIFO IOVA regs `0x150/0x30c…`), 15 FIFO slots, 4 H264 VP slots, quirks `LSR|NO_PIPE_STATE` | `avd_t8103_variant`: fw `avd-fw-v2-t0.bin`, revision 3, `t8103_configure_stream` (regs `0x4068/0x4084/0x40a0/0x40bc`, submit `0x4014`), **7 FIFO slots, 1 H264 VP slot**, quirks `LSR|NO_PIPE_STATE` (38-bit DMA mask) |
| CM3 boot signal | avd-work: `CM3_BOOT_READY` stayed 0 on T6000, used `MBOX_C & 0x20000` | upstream polls `AVD_REG_FLAG0_SET == 1`, which its *own* firmware sets (`CM3_BOOT`) — no Apple-firmware quirk |

### Architectural difference — why nothing ports

avd-work tried to drive Apple's own CM3 firmware (kext-extracted blob, INIT/DECODE mailbox
commands built into SRAM, TX-FIFO doorbell) and hit a chain of platform obstacles: NS-EL1 MMIO
write gate (FABRIC bit 12), DART NOC permission registers, no IOMMU domain for a DT-overlay device
(→ `dma_alloc_coherent` failing, → gen_pool `avd_vb2_memops`), DTB patching that boot-looped m1n1,
so it needed `kboot_avd.c` in m1n1 (EL2 unlock + FDT injection + 64 MB DART bypass pool) and a
runtime DT overlay module. Upstream instead replaces the CM3 firmware entirely (2 KB stateless
MIT firmware that applies the tunables and forwards IRQs), builds the instruction stream in the
kernel (eiln's `avid` HAL ported to C, `avd-inst.h`), writes the VP instruction FIFO / submit
registers directly from EL1 via the `ctrl` region, and uses the real DART through `iommus` +
`vb2_dma_contig` — so no m1n1 change, no overlay, no custom allocator, and it ships in the distro
kernel with the DT node. The write-gate problem avd-work fought is evidently not hit by this
sequence (upstream has no EL2 code and reports T6000 working); whether that is due to the
reset/power-domain handling via `ps_avd_sys` or to the different firmware is not documented — it
is moot for T8103 since the whole stack is prebuilt.

### Last recorded state of avd-work

- `AGENTS.md` / `docs/current-status.md` / `docs/session-history.md` end at **Session 40
  (2026-05-08)**: custom `avd_vb2_memops` (gen_pool over DART region) compiled in; `kboot.c`
  ordering bug (`kboot_avd_patch_dt` before `kboot_avd_unlock`) fixed; **dart13** m1n1 deployed to
  the M1 Pro (`/boot/efi/m1n1/boot.bin`, backups `boot.bin.bak-dart11/12`). Session 41's
  "verify `apple,avd-dma-base/size` in FDT, load modules, run decode" was never logged.
- Never reached: CM3 command acknowledgement, any decoded pixel, NV12 detiling, HEVC test.
- The M1 Pro is therefore left with a **modified m1n1** and out-of-tree `apple-avd.ko` +
  `apple-avd-overlay.ko` in `/lib/modules/*/extra/`. Both must be removed before using the
  upstream driver there (see plan; the out-of-tree module has the *same name* `apple-avd.ko`).

---

## Q3. Integration path into Frigate's ffmpeg

Frigate facts (dev branch, checked 2026-09-05):
- `docker/main/install_deps.sh`: arm64 image gets NickM-27/FFmpeg-Builds tarballs
  `ffmpeg-n5.1-2…linuxarm64-gpl-5.1`, `n7.0.2-18…` (7.0) and, on dev, `n8.1.1-9-g58d4114d36…`
  (8.0 alias; `DEFAULT_FFMPEG_VERSION="8.0"`, `INCLUDED_FFMPEG_VERSIONS="8.0:7.0:5.0"`).
  The local `frigate:latest` (built June 2026) has only `/usr/lib/ffmpeg/{5.0,7.0}`; its 7.0 is
  `n7.1.3-43-g5a1f107b4c-20260319`.
- Those builds, verified by running them (downloaded copy in `research/ffmpeg-frigate/ffmpeg`
  and inside the container): hwaccels `cuda drm opencl vulkan (amf)`, buildconf
  `--enable-libdrm --disable-vaapi --enable-vulkan`, 9 `*_v4l2m2m` decoders (stateful API only —
  irrelevant for AVD), **no `v4l2request`**. The image has Debian bookworm `libva2 2.17.0`,
  `libva-drm2`, `libdrm2 2.4.123`, `mesa-va-drivers` (5 `*_drv_video.so`, none for v4l2).
- `ffmpeg.path` config: `resolve_ffmpeg_path()` returns `f"{path}/bin/ffmpeg"` for any absolute
  path, so a bind-mounted custom build works without rebuilding the image.
- `hwaccel_args` accepts raw strings; presets are only for Intel/NVIDIA/RPi/Rockchip/Jetson.
  `preset-rpi-64-h264` = `-c:v:1 h264_v4l2m2m` (stateful; useless here). `preset-vaapi` =
  `-hwaccel vaapi -hwaccel_device {3} -hwaccel_output_format vaapi` + scale
  `scale_vaapi=…,hwdownload,format=nv12` — needs a VPP entrypoint the v4l2_request VA driver does
  not provide on Apple (its converter path is Rockchip RGA), so **do not use the preset**.
- Only the `detect` role decodes; `record` is `-c copy` (`preset-record-generic`). So per camera
  one decode of the restreamed go2rtc stream at camera fps, then software `fps=5,scale=640:360`.

### Options

| Path | What it needs | Verdict |
|---|---|---|
| **A. `-hwaccel v4l2request`** (Kwiboo out-of-tree hwaccel) | Custom ffmpeg n8.1 + 13 patches, `--enable-v4l2-request --enable-libdrm --enable-libudev`, built against kernel headers ≥ 6.0 (Fedora 7.1 headers fine). Runtime: `/dev/videoN` + `/dev/mediaN` in the container, libdrm/libudev present (bookworm has both). No libva. Output NV12 sw frames if `-hwaccel_output_format` is omitted (ffmpeg downloads automatically). | **Recommended.** Fewest moving parts, same code LibreELEC has shipped since 2018 (H.264 path mature), no VA-API ABI coupling with the container's old libva 2.17. |
| **B. VA-API via `libva-v4l2_request`** | ffmpeg built `--enable-vaapi` (no source patches — Fedora's `ffmpeg-free` already qualifies on the host), sofus13's driver `.so` built against the *container's* libva 2.17 and mounted into `/usr/lib/aarch64-linux-gnu/dri/`, env `LIBVA_DRIVER_NAME=v4l2_request`; `hwaccel_args: -hwaccel vaapi -hwaccel_device /dev/dri/renderD128` (no `-hwaccel_output_format`, so frames come back as sw NV12); `/dev/dri`, `/dev/videoN`, `/dev/mediaN` passed in. | Good for **host-side validation** (vainfo, `ffmpeg-free -hwaccel vaapi`, mpv) with zero custom ffmpeg. For the container it adds libva ABI risk and the driver is still being tuned for non-Rockchip SoCs (README). Keep as fallback. |
| C. `h264_v4l2m2m` / `preset-rpi-64-h264` | nothing | **Does not work**: stateful V4L2 codec API; AVD driver is stateless/request-based. |
| D. `-hwaccel drm` with stock Frigate ffmpeg | nothing | **Does not work**: `drm` is only a hwdevice/frames type; no decoder behind it without the v4l2request patches. |
| E. Wait for upstream ffmpeg / Frigate | — | The series has been out of tree since 2019; Asahi says VA-API/Vulkan Video "at some point". Not plannable. |

### Device passthrough

```yaml
services:
  frigate:
    devices:
      - /dev/video0:/dev/video0     # AVD (verify index: `v4l2-ctl -d /dev/video0 --info` → card "avd")
      - /dev/media0:/dev/media0     # REQUIRED: the Request API allocates requests on the media node
      # - /dev/dri:/dev/dri         # only for path B (VA-API device open)
    volumes:
      - /opt/ffmpeg-avd:/opt/ffmpeg-avd:ro   # custom build; config: ffmpeg: path: /opt/ffmpeg-avd
```
Frigate runs as root inside the container, so group membership is not an issue; on the host the
nodes are `root:video 0660` — fine for `--device`. On this Mac mini there is no ISP camera, so the
AVD should be `video0`/`media0`; still pin by checking `/sys/class/video4linux/video*/name` and
add a udev rule for a stable symlink (`/dev/avd`) if the index moves after a kernel update.
SELinux: `--device` passthrough works under `container_t` without extra labels.

---

## Q4. Phased plan for this machine

### Phase 0 — prepare without touching the running NVR (½ day)
1. Build the firmware on this host (userspace only): `dnf install clang llvm meson`; clone
   `https://github.com/AsahiLinux/avd-fw` into `~/frigate/src/avd-fw`; `meson setup
   --cross-file=./llvm.ini build && ninja -C build`; copy `build/apple/avd-fw-v2-t0.bin` (and
   `v3-t0` for the M1 Pro) to `/lib/firmware/apple/`. Remove the stale `/lib/firmware/apple/avd-fw.bin`.
   (Alternative: download the `avd-fw.tar.xz` artifact from the repo's latest Actions run.)
2. Build `sofus13/libva-v4l2_request` on the host (`meson setup build && ninja -C build`) — for
   host validation only. Install `v4l-utils`, `gstreamer1-plugins-bad-free`, `libva-utils`.
3. Prepare the custom ffmpeg (path A) in a throwaway **Debian bookworm** container so the binary
   matches the Frigate image's glibc: `git clone -b v4l2-request-n8.1 https://github.com/Kwiboo/FFmpeg`
   (or apply LibreELEC's `0001-v4l2-request.patch` to n8.1), `./configure --enable-gpl
   --enable-v4l2-request --enable-libdrm --enable-libudev --enable-libx264 … --prefix=/opt/ffmpeg-avd`.
   Confirm `ffmpeg -hwaccels` lists `v4l2request`. Either a static build (BtbN/NickM-27 style,
   heavy) or a dynamic build with only bookworm system libs. Put it in `/opt/ffmpeg-avd` on the host.

### Phase 1 — validate the driver off the production box (½–1 day)
Do this on the **M1 Pro** (<m1pro-ip>) first — it is the same driver (`apple,t6000-avd`,
`avd-fw-v3-t0.bin`) and it is not the NVR:
1. Restore stock boot: `sudo update-m1n1` (replaces the dart13 `boot.bin`); delete
   `/lib/modules/*/extra/apple-avd.ko` and `apple-avd-overlay.ko` (depmod's default search order
   prefers `extra/` over `kernel/`, so the old module would shadow the in-tree one) and run `depmod`.
2. `dnf install kernel-16k-7.1.6-400.asahi.fc44` (or newer), reboot into it, check:
   `dmesg | grep -iE 'avd|apple-avd'`, `ls /dev/video* /dev/media*`,
   `v4l2-ctl -d /dev/videoN --list-formats-out` (expect `H264` slice, `HEVC`, `VP90`).
3. Decode test without any custom code: dump 10 s of a camera stream to an Annex-B file
   (`ffmpeg -rtsp_transport tcp -i rtsp://…/cam2 -t 10 -c copy -bsf:v h264_mp4toannexb cam2.h264`)
   then `gst-launch-1.0 filesrc location=cam2.h264 ! h264parse ! v4l2slh264dec ! fakesink -v`
   and, for the VA-API route, `LIBVA_DRIVER_NAME=v4l2_request LIBVA_DRIVERS_PATH=… vainfo` and
   `ffmpeg -hwaccel vaapi -hwaccel_device /dev/dri/renderD128 -i cam2.h264 -f null -` with
   Fedora's `ffmpeg-free`. Watch dmesg for watchdog/`no handler for IRQ` messages.
4. Run the phase-0 custom ffmpeg: `ffmpeg -hwaccel v4l2request -i cam2.h264 -f null -` and a
   5-stream soak (`5 × ffmpeg … -vf fps=5,scale=640:360 -f null -` for an hour) to check the
   driver under Frigate-like concurrency (T8103 has 7 FIFO slots / 1 H.264 VP slot; the m2m
   scheduler serialises jobs, so 5 contexts should be fine but this is the thing to measure).

### Phase 2 — same on this box, still CPU-decoding in Frigate (½ day)
1. `dnf install kernel-16k-7.1.6…` — 7.0.13 stays as a GRUB fallback (three kernels kept). Note
   the driver will auto-probe at boot because the 7.1 DTB has the node; with the firmware in place
   it registers `/dev/video0`/`/dev/media0`, without it the probe just fails. Frigate is unaffected
   either way. Reboot in a maintenance window (cameras keep recording in-camera; Frigate is down
   ~2 min).
2. Repeat the phase-1 checks on the host with the real camera streams.

### Phase 3 — switch Frigate to hardware decode (½ day)
1. compose: add the `devices:` entries and the `/opt/ffmpeg-avd` mount above.
2. `config.yaml`:
   ```yaml
   ffmpeg:
     path: /opt/ffmpeg-avd
     hwaccel_args: -hwaccel v4l2request      # path A; for B: -hwaccel vaapi -hwaccel_device /dev/dri/renderD128
   ```
   Leave `output_args`/scale defaults (software `fps=5,scale=640:360` on NV12 is cheap).
   Optionally `input_args: preset-rtsp-restream` stays as is (stream comes from go2rtc).
3. Verify: `docker exec frigate cat /dev/shm/logs/frigate/current` shows no ffmpeg restarts,
   `/api/stats` camera fps stable, `docker stats` CPU drop (baseline first), `cat
   /sys/kernel/debug/...` not needed — `dmesg -w` on the host for driver errors.
4. Rollback = delete the two `ffmpeg:` lines and restart; kernel rollback = pick 7.0.13 in GRUB.

### Phase 4 — hardening (¼ day, ongoing)
- udev rule for a stable AVD node; a systemd `ConditionPathExists=/dev/media0` check is not
  needed since Frigate's ffmpeg simply fails and restarts if the device is absent.
- Track: `kernel-16k` updates (module is in-tree, no rebuild), `avd-fw` changes (rebuild bin),
  Frigate upgrades (custom ffmpeg is outside the image; re-check `ffmpeg.path` semantics),
  FFmpeg upstream merge of v4l2request (then drop the patches), Fedora packaging of `avd-fw`
  and the VA driver (progress report 7.2 says they want to ship it).
- Keep the M1 Pro as the canary for kernel updates.

### Effort estimate
- Firmware build + host validation: 0.5–1 day.
- Custom ffmpeg (bookworm-compatible, patched) + container plumbing: 1–2 days.
- Frigate cut-over, soak, docs: 0.5–1 day.
- Total **≈ 3–5 working days**; no kernel development expected. If the driver misbehaves under
  5 concurrent contexts, add 1–3 days of driver debugging and upstream bug reports
  (`AsahiLinux/linux` issues; driver author sofus, github.com/sofus13).

### Hard blockers / risks (honest list)
1. **No packaged firmware**: self-build (MIT) is mandatory; trivial but unpackaged, so it must be
   re-copied after any `linux-firmware` housekeeping. Not a real blocker.
2. **No Frigate ffmpeg supports the driver** (arm64 builds: `--disable-vaapi`, no v4l2request).
   A custom ffmpeg is unavoidable for as long as Kwiboo's series stays out of tree (since 2019).
   This is the only genuinely open-ended maintenance item.
3. **Driver is ~10 weeks old** (first commit 2026-06-30) and the Asahi team itself says "there's
   still some work to do before we can ship AVD support to users"; T8103 has a single H.264 VP
   slot and 7 FIFO slots, and the quirk `NO_PIPE_STATE`. Concurrency with 5 streams is untested
   publicly — measure before cutting over.
4. **Kernel major bump on the NVR** (7.0 → 7.1) — mitigated by GRUB fallback and by testing on
   the M1 Pro first.
5. **M1 Pro carries avd-work leftovers** (patched m1n1 `boot.bin`, out-of-tree `apple-avd.ko`
   with the same module name). Must be cleaned before it can serve as a test bed.
6. **VA-API route ABI**: sofus's driver vs the container's libva 2.17 — avoid by choosing path A.
7. `-hwaccel v4l2request` currently outputs via `drm_prime`→sw download inside ffmpeg; per-frame
   copy cost is small at 640×360 detect but the decode happens at the source resolution (go2rtc
   restream = camera main stream). If CPU savings matter more, feed detect from the cameras'
   sub-streams — orthogonal to AVD.

---

## Sources (files and URLs used)

- Local: `~/Projects/avd-work/{AGENTS.md,DEVELOPER_GUIDE.md,io-device-tree,docs/*.md}`,
  `remote/avd/linux/drivers/media/platform/apple/avd/*`, `remote/avd-overlay/avd-t6000.dts`,
  `m1n1-main/src/kboot_avd.c`, `m1n1-avd` (`origin/avd-take0` = `fc37d419aae7`, 2024-01-01),
  `remote/avd` (eiln/avd `e8dfcb70dcef`, 2023-12-31); `/proc/device-tree`, `/boot/config-7.0.13…`,
  `/lib/modules/7.0.13…`, `dnf repoquery` against the enabled Fedora/copr repos;
  `~/frigate/{docker-compose.yml,DEPLOYMENT.md,config/config.yaml}`;
  `research/ffmpeg-frigate/ffmpeg` (NickM-27 `autobuild-2026-06-02-14-20` linuxarm64 n8.1.1-9).
- AsahiLinux/linux `asahi` branch: `drivers/media/platform/apple/avd/*`,
  `arch/arm64/boot/dts/apple/t8103.dtsi`; commits `9d77b27426e3`, `edd1529cbf30`, `95738fb3e5cd`,
  `0d6dcb9b11b4`, `adb54ad2ed92`, `adec89957b42`; tags `asahi-7.1.5-1`, `asahi-7.1.6-1`, `asahi-7.1.12-1`.
- https://github.com/AsahiLinux/avd-fw (`5e34aca83906` 2026-07-26 latest) — `Makefile`, `meson.build`, `src/avd.c`, `src/hw/tunables/tunables_v2.h`, `.github/workflows`.
- https://github.com/sofus13/libva-v4l2_request (`cfe6c2ab1b53` 2026-07-16) — `README`.
- https://github.com/Kwiboo/FFmpeg/tree/v4l2-request-n8.1 (13 commits: `900fa8e6625a` … `b57fbbe50c9b`); https://github.com/LibreELEC/LibreELEC.tv/tree/master/packages/multimedia/ffmpeg (patches/v4l2-request, `package.mk` `--enable-v4l2-request`).
- https://ffmpeg.org/pipermail/ffmpeg-devel/2024-August/332034.html (v2 series, unmerged).
- https://github.com/blakeblackshear/frigate/blob/dev/docker/main/install_deps.sh , `frigate/ffmpeg_presets.py`, `frigate/util/config.py` (`resolve_ffmpeg_path`), `docs/docs/configuration/hardware_acceleration_video.md`; https://github.com/NickM-27/FFmpeg-Builds (`scripts.d/50-vaapi/50-libva.sh`).
- https://asahilinux.org/docs/platform/feature-support/m1/ ; https://asahilinux.org/2026/06/progress-report-7-1/ ; https://asahilinux.org/2026/08/progress-report-7-2/ ; https://www.phoronix.com/news/Asahi-Linux-AVD-Firmware-M3 (2026-07-01).
- https://github.com/AsahiLinux/m1n1 (no AVD code; v1.6.1 2026-08-08 release notes).
