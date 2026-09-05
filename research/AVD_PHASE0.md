# AVD hardware decode — Phase 0 (userspace prep) — DONE 2026-09-05

Host: `<nvr-host>`, Mac mini M1 (T8103), Fedora Asahi 44, running kernel `7.0.13-400.asahi.fc44.aarch64+16k`
(no AVD driver). Companion to `AVD_DECODE.md` (research) — this file records what was built in Phase 0,
where it lives, how it was verified, and the ready-to-run checklists for Phases 1–3.

Constraints honoured: no kernel/m1n1/DTB/bootloader change, no reboot, no module loads, no changes to the
running `frigate` container, its compose file, `config/` or `detector/`. The only system-level side effects
are userspace dnf installs (`clang llvm meson v4l-utils libva-utils gstreamer1-plugins-bad-free libva-devel
libdrm-devel systemd-devel`, which pulled a `systemd 259.7 -> 259.8` upgrade as a dep) and the files
listed under "Artifacts".

## Status summary

| Item | Result |
|---|---|
| Firmware `apple/avd-fw-v2-t0.bin` (T8103) | Built from `AsahiLinux/avd-fw` `5e34aca83906`, installed `/lib/firmware/apple/`, 49152 B (`--pad-to=0xc000`), root:root 0644. Also `v3-t0` (T6000/M1 Pro) and `v3-t1` (T8112/T6020), 65536 B each. Names match upstream `avd-drv.c` (`.fw_name = "apple/avd-fw-v2-t0.bin"` for `apple,t8103-avd`). |
| Stale `/lib/firmware/apple/avd-fw.bin` | **Renamed** to `/lib/firmware/apple/avd-fw.bin.apple-kext-old` (61592 B, sha256 `aa3cafa1…`, the Apple-kext blob from avd-work). Not deleted. |
| Custom ffmpeg | `Kwiboo/FFmpeg` branch `v4l2-request-n8.1` @ `b57fbbe50c9b2656fad86a1a7eeabfd2b2a50935` (2026-03-16, "avutil/hwcontext_v4l2request: Add support for BROADCOM_SAND128 pix fmts"; 13 commits on n8.1). Built in a throwaway `debian:bookworm` (arm64, glibc 2.36) container, installed to `/opt/ffmpeg-avd`. Reports `ffmpeg version 8.1-avd-b57fbbe`. |
| Runs in `frigate:latest` | Yes. `ldd` inside the image resolves only `libm libdrm.so.2 libz libssl.so.3 libcrypto.so.3 libudev.so.1 libc` — all present in the image. x264/opus are linked statically (the image has no libx264/libopus .so). |
| `-hwaccels` | `drm`, `v4l2request`. h264/hevc/mpeg2/vp8/vp9/av1 `*_v4l2request` hwaccels all compiled in; `-h decoder=h264` → "Supported hardware devices: v4l2request". |
| Software-decode parity | Identical to the image's own `/usr/lib/ffmpeg/7.0/bin/ffmpeg` (n7.1.3): `fps=5,scale=640:360` yuv420p rawvideo = **17,280,000 bytes = 50 frames** both; md5 of first 5 frames `9495248e2bb237d64602398f33cd0a73` both; full decode 150/150 frames. |
| `-hwaccel v4l2request` without driver | Graceful: `[h264] Failed setup for format drm_prime: hwaccel initialisation returned error.` then `Format drm_prime not usable, retrying get_format() without it` → falls back to software, exit 0, no crash. |
| Annex-B test clip | `~/frigate/research/cam2-10s.h264` — 1,404,544 B, 150 frames, H.264 High 2304x1296, starts `00 00 00 01 67` (SPS). Source: `recordings/2026-09-05/14/cam2/59.24.mp4` (copy kept as `research/cam2-src-59.24.mp4`). |
| libva-v4l2_request (host, optional) | Built: `~/frigate/src/libva-v4l2_request/build/src/v4l2_request_drv_video.so` (sofus13 `cfe6c2ab1b53`, all 6 codecs enabled). Not installed system-wide. Without a device: `libva-v4l2request: no V4L2 Request API decoder found` (clean). |
| Kernel 7.1.6 | Being installed by the parent session at the time of writing (`kernel-16k-7.1.6-400.asahi.fc44` + modules present on disk: `/lib/modules/7.1.6-…+16k/kernel/drivers/media/platform/apple/avd/apple-avd.ko.xz` 80396 B, module name `apple_avd`, alias `of:N*T*Capple,t8103-avd`; `/boot/dtb-7.1.6-…/apple/t8103-j274.dtb` contains `avd@268000000` / `apple,t8103-avd`). **Not booted yet.** |

## Artifacts

```
/lib/firmware/apple/avd-fw-v2-t0.bin              49152  sha256 a563c69a2332c5c67d333ab28eb235c34fe4a3b703e48b86ef6eb80e706d22e1  (T8103 — this box)
/lib/firmware/apple/avd-fw-v3-t0.bin              65536  sha256 81749a2db9c9269eba794a33f6438ea82ff618d5978c6b709930bd686bcf1ef5  (T6000 — M1 Pro)
/lib/firmware/apple/avd-fw-v3-t1.bin              65536  sha256 e34391d1f8bd50e580dddbcdb8394dd50ed99cb203ca7801c36f22f4f697ed1c  (T8112/T6020, spare)
/lib/firmware/apple/avd-fw.bin.apple-kext-old     61592  (old Apple kext blob, renamed, unused)
/opt/ffmpeg-avd/bin/ffmpeg                        22.6 MB  ffmpeg 8.1-avd-b57fbbe (static ffmpeg libs, dynamic system libs only)
/opt/ffmpeg-avd/bin/ffprobe                       22.4 MB
/opt/ffmpeg-avd/{include,lib,share}               static .a libs + headers (not needed at runtime, harmless)
~/frigate/src/avd-fw/                 firmware source + build/ (all six variants in build/apple/)
~/frigate/src/FFmpeg-v4l2request/     ffmpeg source (shallow clone, branch v4l2-request-n8.1)
~/frigate/src/uapi-7.0.13/linux/      copies of host kernel-headers 7.0.13 v4l2/media UAPI headers used for the build
~/frigate/src/libva-v4l2_request/     VA-API driver source + build/
~/frigate/research/ffmpeg-avd-build.log         full configure + make + install log
~/frigate/research/avd-fw-build.log             firmware build log
~/frigate/research/libva-v4l2_request-build.log
~/frigate/research/avd-driver-src/{avd-drv.c,avd-v4l2.c,avd.h}   upstream driver source (asahi branch, for reference)
~/frigate/research/cam2-10s.h264             Annex-B test clip
~/frigate/research/cam2-src-59.24.mp4        source mp4 for the clip
```

Note: `src/FFmpeg-v4l2request`, `src/uapi-7.0.13`, `/opt/ffmpeg-avd` and `research/` were bind-mounted with
`:z`, so they now carry the SELinux label `container_file_t` (needed anyway for the Frigate mount).

## Exact commands used

### 1. Firmware
```sh
sudo dnf install -y clang llvm meson
git clone https://github.com/AsahiLinux/avd-fw ~/frigate/src/avd-fw     # 5e34aca83906
cd ~/frigate/src/avd-fw
meson setup --cross-file=./llvm.ini build && ninja -C build                            # clang --target=arm-none-eabi, llvm-objcopy
sudo mv -n /lib/firmware/apple/avd-fw.bin /lib/firmware/apple/avd-fw.bin.apple-kext-old
sudo install -o root -g root -m 0644 build/apple/avd-fw-v2-t0.bin build/apple/avd-fw-v3-t0.bin build/apple/avd-fw-v3-t1.bin /lib/firmware/apple/
sudo restorecon -v /lib/firmware/apple/*
```
Driver filename check (`avd-drv.c` from `raw.githubusercontent.com/AsahiLinux/linux/asahi/...`):
`avd_t8103_variant .fw_name = "apple/avd-fw-v2-t0.bin"`, `avd_t6000_variant .fw_name = "apple/avd-fw-v3-t0.bin"`,
loaded with `request_firmware()` at probe (`avd-drv.c:655`). The module has no `MODULE_FIRMWARE()` line, so the
file is not pulled into the initramfs — fine, `apple_avd` is a module that probes after the root fs is up.

### 2. ffmpeg (inside debian:bookworm, arm64)
```sh
git clone -b v4l2-request-n8.1 --depth 1 https://github.com/Kwiboo/FFmpeg ~/frigate/src/FFmpeg-v4l2request   # b57fbbe
mkdir -p ~/frigate/src/uapi-7.0.13/linux && cp /usr/include/linux/{videodev2,v4l2-common,v4l2-controls,media,media-bus-format,v4l2-mediabus,v4l2-subdev,v4l2-dv-timings}.h ~/frigate/src/uapi-7.0.13/linux/
sudo mkdir -p /opt/ffmpeg-avd && sudo chown $USER:$USER /opt/ffmpeg-avd
docker run -d --name ffbuild --platform linux/arm64 \
  -v ~/frigate/src/FFmpeg-v4l2request:/src/ffmpeg:z -v ~/frigate/src/uapi-7.0.13:/uapi:z \
  -v /opt/ffmpeg-avd:/opt/ffmpeg-avd:z -v ~/frigate/research:/research:z debian:bookworm sleep infinity
docker exec ffbuild sh -c 'apt-get update -qq && apt-get install -y -qq --no-install-recommends build-essential nasm pkg-config ca-certificates libdrm-dev libudev-dev libssl-dev libx264-dev libopus-dev zlib1g-dev'
# force static x264/opus (frigate image has no libx264/libopus .so):
docker exec ffbuild sh -c 'rm -f /usr/lib/aarch64-linux-gnu/libx264.so /usr/lib/aarch64-linux-gnu/libopus.so'
docker exec ffbuild sh -c 'cd /src/ffmpeg && ./configure --prefix=/opt/ffmpeg-avd --enable-gpl --enable-version3 \
  --enable-static --disable-shared --disable-doc --disable-debug --enable-pic --enable-openssl --enable-libx264 \
  --enable-libopus --enable-libdrm --enable-libudev --enable-v4l2-request --pkg-config-flags=--static \
  --extra-cflags="-I/uapi" --extra-version=avd-b57fbbe'
docker exec ffbuild sh -c 'cd /src/ffmpeg && make -j8 && make install'
docker rm -f ffbuild
```
Why the header overlay: bookworm's `linux-libc-dev` is 6.1 (has H264/HEVC/VP8/VP9 stateless controls but not
AV1 and not the newer pixfmts); the host's `kernel-headers-7.0.13` has everything, so only the v4l2/media UAPI
headers are overlaid via `-I/uapi` (they include only `linux/types.h`/`linux/ioctl.h` from the base set, so
mixing is safe). configure reported `v4l2_request` + all six `*_v4l2request` hwaccels enabled, GPLv3.
OpenSSL 3 is accepted with `--enable-gpl` without `--enable-nonfree`.

What the binary can do (checked inside the image): decoders `h264 hevc aac opus`; encoders `libx264 aac libopus rawvideo`;
muxers `mp4 segment mpegts hls null rawvideo`; protocols `tcp rtsp rtp udp https`; filters `fps scale format hwdownload drawbox aresample`.
Hwaccels: `drm v4l2request`. (Not included, by design: vaapi, vulkan, opencl, libvpx/x265/dav1d etc. — Frigate's detect/record
paths don't need them; the stock 7.0/5.0 builds remain in the image for anything else.)

### 3. Verification (all inside `frigate:latest`)
```sh
docker run --rm -v /opt/ffmpeg-avd:/opt/ffmpeg-avd:ro,z --entrypoint ldd frigate:latest /opt/ffmpeg-avd/bin/ffmpeg
#   libm, libdrm.so.2, libz, libssl.so.3, libcrypto.so.3, libudev.so.1, libc — nothing "not found"
docker run --rm -v /opt/ffmpeg-avd:/opt/ffmpeg-avd:ro,z --entrypoint /opt/ffmpeg-avd/bin/ffmpeg frigate:latest -hwaccels
#   drm / v4l2request
R=~/frigate/research
docker run --rm -v /opt/ffmpeg-avd:/opt/ffmpeg-avd:ro,z -v $R:/research:ro,z --entrypoint bash frigate:latest -c '
  /opt/ffmpeg-avd/bin/ffmpeg -v error -i /research/cam2-src-59.24.mp4 -vf fps=5,scale=640:360 -f rawvideo -pix_fmt yuv420p - | wc -c   # 17280000
  /usr/lib/ffmpeg/7.0/bin/ffmpeg -v error -i /research/cam2-src-59.24.mp4 -vf fps=5,scale=640:360 -f rawvideo -pix_fmt yuv420p - | wc -c   # 17280000
  /opt/ffmpeg-avd/bin/ffmpeg -hide_banner -loglevel warning -hwaccel v4l2request -i /research/cam2-10s.h264 -f null -; echo $?
  #   [h264 @ …] Failed setup for format drm_prime: hwaccel initialisation returned error.   -> exit 0 (software fallback)
'
# also OK: -c copy -f segment (5 s mp4 segments), -pix_fmt nv12 rawvideo, aac decode (-vn -f null), and a live RTSP pull
# from go2rtc: --add-host=host.docker.internal:host-gateway … -rtsp_transport tcp -i rtsp://host.docker.internal:8554/cam2 -t 3
# -vf fps=5,scale=640:360 -f rawvideo -pix_fmt yuv420p -  -> 5184000 bytes (15 frames), exit 0.
# Annex-B clip:
docker run --rm -v /opt/ffmpeg-avd:/opt/ffmpeg-avd:ro,z -v $R:/research:z --entrypoint /opt/ffmpeg-avd/bin/ffmpeg frigate:latest \
  -v error -y -i /research/cam2-src-59.24.mp4 -t 10 -an -c:v copy -bsf:v h264_mp4toannexb -f h264 /research/cam2-10s.h264
```
Caveat on (d): with no `/dev/media*` present the hwaccel init fails *inside* ffmpeg's `get_format()` and ffmpeg
silently falls back to CPU decode with exit code 0. This is convenient (Frigate keeps working if the device
vanishes) but it also means "is it really using the hardware?" must be checked explicitly in Phase 2/3 —
see the checks with `-loglevel verbose` and `docker stats` below.

### 4. libva-v4l2_request (host, optional)
```sh
git clone https://github.com/sofus13/libva-v4l2_request ~/frigate/src/libva-v4l2_request   # cfe6c2ab1b53
cd ~/frigate/src/libva-v4l2_request && meson setup build && ninja -C build
# use without installing:
export LIBVA_DRIVERS_PATH=~/frigate/src/libva-v4l2_request/build/src LIBVA_DRIVER_NAME=v4l2_request
vainfo --display drm --device /dev/dri/renderD128
```
Host tools installed: `v4l-utils-1.32.0` (`v4l2-ctl`, `media-ctl`), `libva-utils-2.23.0` (`vainfo`),
`gstreamer1-plugins-bad-free-1.28.6` (`v4l2codecs` plugin → `v4l2slh264dec` appears once a device exists).
Fedora's `ffmpeg-free` is *not* installed on the host (no `ffmpeg`/`ffprobe` in PATH) — for host-side ffmpeg tests
either `sudo dnf install ffmpeg-free` (VA-API route) or just use `/opt/ffmpeg-avd/bin/ffmpeg` on the host too
(it is a bookworm/glibc-2.36 binary and runs fine on Fedora's newer glibc; it links only to libdrm/libudev/openssl3/zlib,
all present on the host — check with `ldd`).

---

## Phase 1 — validate the driver on the M1 Pro (<m1pro-ip>) — INSTRUCTIONS ONLY, not executed

Do not run anything against that machine from here; hand these to whoever operates it.
Same driver (`apple,t6000-avd`), firmware `apple/avd-fw-v3-t0.bin` (copy from this box: `/lib/firmware/apple/avd-fw-v3-t0.bin`).
```sh
# 0. remove avd-work leftovers (they shadow the in-tree module and patch m1n1)
sudo update-m1n1                                      # restores stock boot.bin (backups boot.bin.bak-dart11/12 stay)
sudo rm -f /lib/modules/*/extra/apple-avd.ko /lib/modules/*/extra/apple-avd-overlay.ko && sudo depmod -a
# 1. firmware + kernel
sudo install -o root -g root -m 0644 avd-fw-v3-t0.bin /lib/firmware/apple/avd-fw-v3-t0.bin
sudo dnf install kernel-16k-7.1.6-400.asahi.fc44        # or newer; keeps old kernels as fallback
sudo reboot
# 2. after boot
uname -r; dmesg | grep -iE 'avd|apple-avd|apple_avd'; ls -l /dev/video* /dev/media*
v4l2-ctl -d /dev/video0 --info                           # driver "apple_avd"/card "avd", caps incl. Video Memory-to-Memory Multiplanar
v4l2-ctl -d /dev/video0 --list-formats-out               # expect H264 (H.264 Parsed Slice Data), HEVC, VP90, AV1F
v4l2-ctl -d /dev/video0 --list-formats                   # expect NV12, P010, NV16
media-ctl -d /dev/media0 -p
# 3. decode tests (as root or a member of group video: nodes are root:video 0660)
gst-launch-1.0 -v filesrc location=cam2-10s.h264 ! h264parse ! v4l2slh264dec ! fakesink            # 150 buffers
/opt/ffmpeg-avd/bin/ffmpeg -loglevel verbose -hwaccel v4l2request -i cam2-10s.h264 -f null -      # look for "v4l2request" device open lines, NOT "Failed setup for format drm_prime"
LIBVA_DRIVER_NAME=v4l2_request LIBVA_DRIVERS_PATH=… vainfo                                             # optional VA-API route
# 4. concurrency soak (Frigate-like): 5 parallel decodes for an hour, watch dmesg -w for "no handler for IRQ"/watchdog
for i in 1 2 3 4 5; do /opt/ffmpeg-avd/bin/ffmpeg -loglevel error -stream_loop -1 -hwaccel v4l2request -i cam2-10s.h264 -vf fps=5,scale=640:360 -f null - & done; sleep 3600; kill %1 %2 %3 %4 %5
```

## Phase 2 — this box: boot kernel-16k 7.1.6, validate on the host (maintenance window, Frigate down ~2 min)

Pre-conditions (as of writing): `kernel-16k-7.1.6-400.asahi.fc44` + `-modules` installed on disk by the parent session
(`/lib/modules/7.1.6-…+16k/…/apple-avd.ko.xz`, `/boot/vmlinuz-7.1.6-…`, `/boot/dtb-7.1.6-…/apple/t8103-j274.dtb` with the
`avd@268000000` node). Firmware in place. **Before rebooting, confirm the install finished cleanly:**
```sh
rpm -q kernel-16k-core-7.1.6-400.asahi.fc44 kernel-16k-modules-7.1.6-400.asahi.fc44
ls -l /boot/initramfs-7.1.6-400.asahi.fc44.aarch64+16k.img        # must exist (was not yet generated at 16:45)
sudo grubby --info=ALL | grep -E '^(kernel|title)'                # 7.1.6 entry present
sudo grubby --default-kernel                                      # decide: leave default as-is and pick 7.1.6 once, or set it default
# one-shot boot into 7.1.6 while keeping 7.0.13 as default (safest first boot):
sudo grub2-reboot "$(sudo grubby --info=/boot/vmlinuz-7.1.6-400.asahi.fc44.aarch64+16k | sed -n 's/^title=//p' | tr -d '"')"
sudo reboot
```
After boot:
```sh
uname -r                                              # 7.1.6-400.asahi.fc44.aarch64+16k
dmesg | grep -iE 'avd|apple_avd'                      # probe OK; if "failed to load firmware" -> check /lib/firmware/apple/avd-fw-v2-t0.bin
ls -l /dev/video* /dev/media*                         # expect /dev/video0 + /dev/media0 (no ISP on the Mac mini)
cat /sys/class/video4linux/video*/name                # "avd"
v4l2-ctl -d /dev/video0 --info; v4l2-ctl -d /dev/video0 --list-formats-out; v4l2-ctl -d /dev/video0 --list-formats
docker ps | grep frigate                              # Frigate came back and is still CPU-decoding (unchanged config)
# host-side decode tests (sudo, or add your user to group video: sudo usermod -aG video $USER; re-login)
cd ~/frigate/research
sudo gst-launch-1.0 -v filesrc location=cam2-10s.h264 ! h264parse ! v4l2slh264dec ! fakesink 2>&1 | tail -5
sudo /opt/ffmpeg-avd/bin/ffmpeg -loglevel verbose -hwaccel v4l2request -i cam2-10s.h264 -f null - 2>&1 | grep -iE 'v4l2request|drm_prime|frame='
# hw vs sw pixel parity (expect same frame count; md5 will differ slightly: hw NV12 vs sw yuv420p rounding)
sudo /opt/ffmpeg-avd/bin/ffmpeg -v error -hwaccel v4l2request -i cam2-10s.h264 -vf fps=5,scale=640:360 -f rawvideo -pix_fmt yuv420p - | wc -c   # 17280000
# same test *inside the Frigate image* with the devices passed through (this is exactly what Phase 3 relies on):
docker run --rm --device /dev/video0 --device /dev/media0 -v /opt/ffmpeg-avd:/opt/ffmpeg-avd:ro,z -v $PWD:/research:ro,z \
  --entrypoint /opt/ffmpeg-avd/bin/ffmpeg frigate:latest -loglevel verbose -hwaccel v4l2request -i /research/cam2-10s.h264 -f null -
# live stream, 5 concurrent, 10 min, watch `dmesg -w` in another terminal:
for c in cam2 cam3 cam4 cam5 cam1; do
  docker run -d --rm --name avdtest-$c --device /dev/video0 --device /dev/media0 --add-host=host.docker.internal:host-gateway \
    -v /opt/ffmpeg-avd:/opt/ffmpeg-avd:ro,z --entrypoint /opt/ffmpeg-avd/bin/ffmpeg frigate:latest -loglevel error \
    -rtsp_transport tcp -hwaccel v4l2request -i rtsp://host.docker.internal:8554/$c -t 600 -vf fps=5,scale=640:360 -f null -
done; docker stats --no-stream
```
Rollback: reboot and pick `7.0.13` in GRUB (or `sudo grubby --set-default /boot/vmlinuz-7.0.13-400.asahi.fc44.aarch64+16k`).

## Phase 3 — switch Frigate to hardware decode

1. `docker-compose.yml` (add under `services.frigate`; keep everything else):
   ```yaml
       devices:
         - /dev/video0:/dev/video0     # AVD — verify with: cat /sys/class/video4linux/video0/name  -> avd
         - /dev/media0:/dev/media0     # REQUIRED: Request API allocates requests on the media node
       volumes:
         - /opt/ffmpeg-avd:/opt/ffmpeg-avd:ro,z
   ```
2. `config/config.yaml` (top level; cameras inherit):
   ```yaml
   ffmpeg:
     path: /opt/ffmpeg-avd                 # Frigate appends /bin/ffmpeg
     hwaccel_args: -hwaccel v4l2request    # no -hwaccel_output_format: frames come back as sw NV12, fps/scale stay software
   ```
   Do not use `preset-vaapi` / `preset-rpi-64-h264` (see AVD_DECODE.md Q3). Leave `output_args`/`input_args` as they are.
3. `docker compose up -d` (recreates the container) and verify:
   ```sh
   docker exec frigate cat /dev/shm/logs/frigate/current | grep -iE 'ffmpeg|hwaccel|error' | tail -20    # no restart loops
   docker exec frigate ps -o args= -C ffmpeg | grep -c v4l2request                                        # one per detect process
   docker exec frigate ls -l /dev/video0 /dev/media0
   curl -s 127.0.0.1:5000/api/stats | python3 -m json.tool | grep -E '"(camera_fps|detection_fps|process_fps|skipped_fps)"'
   docker stats --no-stream frigate                       # compare CPU% with a pre-cutover baseline (take it before step 3)
   sudo dmesg -w | grep -iE 'avd|no handler|watchdog'      # leave running for the first hour
   ```
   Confirm hardware is actually used (ffmpeg falls back silently otherwise): `docker exec frigate ls /proc/*/fd -l 2>/dev/null | grep -c /dev/video0`
   should be ≥ number of cameras, and host `sudo cat /sys/kernel/debug/…` is not needed — a cheap check is that
   `top` inside the container shows the ffmpeg detect processes well below their previous CPU.
4. Rollback = remove the two `ffmpeg:` lines (and optionally the compose lines) and `docker compose up -d`.

## Phase 4 notes / maintenance
- udev rule for a stable node, e.g. `/etc/udev/rules.d/90-avd.rules`: `SUBSYSTEM=="video4linux", ATTR{name}=="avd", SYMLINK+="avd"` (then map `/dev/avd:/dev/video0`? — no: ffmpeg's v4l2request probes via udev for any request-capable device, so simply passing both nodes is enough; the symlink is only for humans).
- After a `linux-firmware` update re-check `/lib/firmware/apple/avd-fw-v2-t0.bin` still exists (it is unpackaged).
- Kernel updates: driver is in-tree in `kernel-16k-modules`, nothing to rebuild. Firmware ABI changes would show up as
  probe failures in dmesg → rebuild from `src/avd-fw` (`git pull && ninja -C build && sudo install …`).
- ffmpeg rebuild recipe is fully captured above; re-run it against a newer `v4l2-request-n*` branch when Frigate's
  image moves on (the binary is outside the image, so Frigate upgrades don't touch it — re-check `ffmpeg.path` semantics).

## Things that did not work / caveats
- Nothing failed. Minor points:
  - `dnf install systemd-devel` (for libudev headers on the host, used by the libva driver build) upgraded systemd
    259.7 → 259.8 as a dependency; `systemctl is-system-running` reports `degraded` only because of the pre-existing
    `fwupd-refresh.service` failure. No reboot needed for that.
  - The host has no `ffmpeg`/`ffprobe` in PATH (AVD_DECODE.md assumed `ffmpeg-free` was installed) — host tests use
    `/opt/ffmpeg-avd/bin/ffmpeg` or gst-launch.
  - the login user is not in group `video`; host-side V4L2 tests need `sudo` or `usermod -aG video $USER`.
  - `/opt/ffmpeg-avd/bin/*` and `lib/*` are root-owned (written by the container's root); the directory is owned by
    the login user. Fine for a read-only mount.
  - The stale Apple-kext firmware was renamed, not deleted, as requested: `/lib/firmware/apple/avd-fw.bin.apple-kext-old`.
