# ffmpeg-avd-vk — `ffmpeg.path` wrapper for Frigate (Apple AVD decode + Vulkan rescale)

Mounted at `/opt/ffmpeg-avd-vk` in the Frigate container (staging/docker-compose.yml) and selected with
`ffmpeg: path: /opt/ffmpeg-avd-vk` (Frigate execs `<path>/bin/ffmpeg` and `<path>/bin/ffprobe`; go2rtc's
`ffmpeg.bin` follows the same setting).

* `bin/ffmpeg`  — POSIX sh. Runs `/opt/ffmpeg-vk/bin/ffmpeg` under the host's `ld.so` with `/host-lib64` (so Mesa's
  Honeykrisp ICD loads inside the bookworm image) and, only for Frigate's detect-role command (`-hwaccel v4l2request`
  from `hwaccel_args`, `-vf fps=N,scale=W:H`, `-f rawvideo`), adds `-init_hw_device vulkan=vk -filter_hw_device vk
  -hwaccel_output_format drm_prime` and rewrites the `-vf` to the zero-copy chain
  `fps=N,hwmap,scale_vulkan=w=max(iw/2\,W):h=max(ih/2\,H),scale_vulkan=W:H,hwdownload,format=nv12,format=yuv420p`.
  Everything else is passed through unchanged. The rewritten command is logged to stderr with the prefix
  `[ffmpeg-avd-vk]`. Env knobs: `AVD_VK_REWRITE=0` (never rewrite: hardware decode + CPU scale), `AVD_VK_SCALE=single`
  (one bilinear step, ~25 dB luma instead of ~31 dB, ~20 % less CPU), `AVD_VK_DEBUG=1` (log every exec),
  `AVD_VK_FFMPEG/AVD_VK_LOADER/AVD_VK_LIBDIR` (paths, for tests).
* `bin/ffprobe` — same loader trick, no rewrite.

Test: `src/ffmpeg-vulkan/bench/wrapper-test.sh` feeds it the exact command lines captured from the running Frigate.
Rules, numbers and background: `research/AVD_FRIGATE_INTEGRATION.md`.
