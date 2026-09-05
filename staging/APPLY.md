# Switching Frigate to Apple AVD decode + Vulkan rescale — apply / verify / rollback

Prepared 2026-09-05, NOT applied. Background, numbers and the wrapper's rules:
`research/AVD_FRIGATE_INTEGRATION.md`. Everything below runs from `~/frigate`.

What changes: `docker-compose.yml` (devices `/dev/video0 /dev/media0 /dev/dri/renderD128`, mounts
`/opt/ffmpeg-vk`, `./ffmpeg-avd-vk` -> `/opt/ffmpeg-avd-vk`, `/usr/lib64` -> `/host-lib64:ro`, the host `ld.so`,
`./src/ffmpeg-vulkan/icd`, env `VK_DRIVER_FILES`) and `config/config.yaml` (`ffmpeg.path: /opt/ffmpeg-avd-vk`,
`hwaccel_args: [-hwaccel, v4l2request]`). The staged copies are complete files: `diff -u docker-compose.yml
staging/docker-compose.yml` and `diff -u config/config.yaml staging/config.yaml` show exactly the delta.

## 0. Preconditions (all must hold)

```bash
uname -r                                   # 7.1.6-400.asahi.fc44.aarch64+16k (the module below is built for it)
# The patched apple_avd module must be the one that loads, now and after a reboot:
src/avd-driver/build-and-install.sh --status
#   -> "modprobe picks: /lib/modules/.../updates/apple-avd.ko" after running  src/avd-driver/build-and-install.sh
#      (installs branch `series` = patches/final/0001-0006 + 0008; without 0008 every ffmpeg start on a live
#      stream costs one DART fault + 2 s watchdog reset; without 0006 the read-back is 250 MB/s).
#   Temporary alternative for a first try (lost at reboot): sudo fuser /dev/video0 /dev/media0 (must be empty),
#   then  src/avd-driver/reload.sh apple-avd-fixed-0008.ko
sudo cat /sys/module/apple_avd/taint       # OE = out-of-tree module loaded
ls -l /dev/video0 /dev/media0 /dev/dri/renderD128 /opt/ffmpeg-vk/bin/ffmpeg ffmpeg-avd-vk/bin/ffmpeg ffmpeg-avd-vk/bin/ffprobe src/ffmpeg-vulkan/icd/asahi_icd.container.json
md5sum /opt/ffmpeg-vk/bin/ffmpeg           # 0b0d5261… = Kwiboo b57fbbe + patches/ffmpeg/0001-0004 (+Vulkan)
# Dry run of the wrapper with the real Frigate command lines (live restream, throwaway container):
src/ffmpeg-vulkan/bench/wrapper-test.sh    # expect: detect pipe ~5 fps of 640x360, segments playable, rewrite=1 only for the detect+record command
```

## 1. Baseline before the switch (keep the output)

```bash
docker stats --no-stream frigate
curl -s 127.0.0.1:5000/api/stats | python3 -m json.tool | grep -E '"(camera_fps|process_fps|skipped_fps|detection_fps)"' | head -20
docker exec frigate ps -eo pid,pcpu,rss,args | grep '[f]fmpeg' | grep -v mpeg1video
```

## 2. Apply

```bash
ts=$(date +%Y%m%d-%H%M%S)
cp docker-compose.yml docker-compose.yml.bak.$ts
cp config/config.yaml config/config.yaml.bak.$ts
cp staging/docker-compose.yml docker-compose.yml
cp staging/config.yaml config/config.yaml
docker compose config >/dev/null && docker compose up -d      # recreates the container (devices/mounts changed)
```

## 3. Verify (first 10 minutes)

```bash
docker exec frigate cat /dev/shm/logs/frigate/current | grep -E "ffmpeg-avd-vk|ERROR|Restarting ffmpeg" | tail -20
#   one "[ffmpeg-avd-vk] rewrite=1 ... exec: /host-lib64-ld.so --library-path /host-lib64 /opt/ffmpeg-vk/bin/ffmpeg ... -hwaccel v4l2request
#   -init_hw_device vulkan=vk -filter_hw_device vk -hwaccel_output_format drm_prime ... -vf fps=5,hwmap,scale_vulkan=...,hwdownload,format=nv12,format=yuv420p ..."
#   per camera, no restart loop. (cam1 is offline and keeps restarting regardless — pre-existing.)
docker exec frigate ps -eo pid,pcpu,rss,args | grep '[o]pt/ffmpeg-vk/bin/ffmpeg'      # 5 detect/record processes (4 live cameras + cam1 retrying)
sudo fuser -v /dev/video0 /dev/media0                                                 # the same ffmpeg pids hold the decoder
docker exec frigate sh -c 'grep -l libvulkan_asahi /proc/[0-9]*/maps'                 # one entry per ffmpeg: Honeykrisp, not llvmpipe
sudo dmesg -w | grep -iE 'avd|dart|no handler'                                        # must stay silent (Ctrl-C after a while)
curl -s 127.0.0.1:5000/api/stats | python3 -m json.tool | grep -E '"(camera_fps|process_fps|skipped_fps|detection_fps)"' | head -20   # camera_fps ~5 per live camera, skipped 0
docker stats --no-stream frigate                                                      # compare with step 1 (expect the ffmpeg share to drop to ~1/3)
journalctl -u frigate-detector --since -10min | grep -iE 'fallback|error|Inference stats' | tail   # GPU is shared with the detector: no FALLBACK, avg ~30 ms
```
Look at a camera in the UI (debug view shows the 640x360 detect frame). Expect motion/objects as before; the
scaled picture is marginally crisper than swscale's bicubic (two-stage bilinear, ~31.6 dB luma vs bicubic).

## 4. Rollback

Full rollback (back to the image's CPU ffmpeg, exactly as before):
```bash
cp docker-compose.yml.bak.<ts> docker-compose.yml && cp config/config.yaml.bak.<ts> config/config.yaml && docker compose up -d
```
Partial fallbacks, cheapest first (edit, then `docker compose up -d`):
* Keep hardware decode, scale on the CPU (the read-back path, /opt/ffmpeg-avd behaviour): add
  `- AVD_VK_REWRITE=0` under `environment:` in docker-compose.yml.
* Keep the mounts, drop the hardware: delete the `ffmpeg:` block from config/config.yaml (`docker compose restart frigate`
  is enough for a config-only change).
* If `/dev/video0` disappears (module not loaded), ffmpeg falls back to software decode by itself with exit code 0
  ("Failed setup for format drm_prime") — the wrapper still needs `/opt/ffmpeg-vk` and the loader mounts to exist.
* Module: `src/avd-driver/build-and-install.sh --undo` (then `src/avd-driver/restore.sh` to swap it out immediately).

## Things to know
* SELinux: `/usr/lib64` is mounted `:ro` without `:z` on purpose (a relabel would touch the host's system
  libraries); `container_t` reads `lib_t`/`ld_so_t` fine (tested, no AVC denials).
* `dnf update mesa*` on the host changes the Vulkan driver the container uses (that is the point: one ICD). A glibc
  update keeps loader and libs in sync because both come from the host. A kernel update needs the module rebuilt
  (`build-and-install.sh`) before the reboot, or the machine comes back on the in-tree module (works, but with the
  slow read-back and the stream-start faults; the wrapper still functions).
* go2rtc also takes `ffmpeg.path` (`/dev/shm/go2rtc.yaml: ffmpeg.bin`), so its on-demand `ffmpeg:cam2#audio=opus`
  transcodes run through the wrapper too (pass-through, /opt/ffmpeg-vk has libopus/aac).
* Frigate upgrades: the wrapper keys on `-hwaccel v4l2request` + `-vf fps=N,scale=W:H` + `-f rawvideo`; if a new
  Frigate changes the detect chain the wrapper passes it through unchanged (hardware decode, CPU scale) and logs nothing
  — check the ffmpeg log for the `[ffmpeg-avd-vk] rewrite=1` line after an upgrade. The upstream-style alternative is
  `patches/frigate/0001-*.patch` (`hwaccel_args: preset-apple-avd-vulkan`, no wrapper rewrite needed).
