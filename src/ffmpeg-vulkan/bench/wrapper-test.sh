#!/bin/bash
# Feed the ffmpeg.path wrapper (ffmpeg-avd-vk/bin) the exact command lines Frigate runs, inside a throwaway
# frigate:latest container with the SAME mounts/devices/env as staging/docker-compose.yml.
# Paths adjusted: rtsp://127.0.0.1:8554 -> rtsp://host.docker.internal:8554 (the live go2rtc restream of the
# running `frigate` container), /tmp/cache exists in the throwaway container. Never touches `frigate`.
set -u
R=${FRIGATE_HOME:-$HOME/frigate}/research; ICD=${FRIGATE_HOME:-$HOME/frigate}/src/ffmpeg-vulkan/icd; W=${FRIGATE_HOME:-$HOME/frigate}/ffmpeg-avd-vk
OUT=$R/avdvk/wrapper-test; mkdir -p "$OUT"
secs=${1:-20}
docker run --rm --name avdvk-wrapper-test --device /dev/dri/renderD128 --device /dev/video0 --device /dev/media0 \
  --add-host host.docker.internal:host-gateway \
  -v /usr/lib64:/host-lib64:ro -v /usr/lib/ld-linux-aarch64.so.1:/host-lib64-ld.so:ro \
  -v $ICD:/etc/vulkan-asahi:ro,z -v /opt/ffmpeg-vk:/opt/ffmpeg-vk:ro,z -v $W:/opt/ffmpeg-avd-vk:ro,z -v $R:/research:z \
  -e VK_DRIVER_FILES=/etc/vulkan-asahi/asahi_icd.container.json -e VK_ICD_FILENAMES=/etc/vulkan-asahi/asahi_icd.container.json \
  -e LIBAVFORMAT_VERSION_MAJOR=61 ${WRAPPER_ENV:-} \
  --entrypoint bash frigate:latest -c '
set -u; mkdir -p /tmp/cache; F=/opt/ffmpeg-avd-vk/bin/ffmpeg; P=/opt/ffmpeg-avd-vk/bin/ffprobe
echo "=== 1. Frigate cam2 detect+record command (captured from `docker exec frigate ps`), hwaccel_args added, '"$secs"' s"
timeout -s INT -k 5 '"$secs"' $F -hide_banner -loglevel warning -threads 2 -user_agent "FFmpeg Frigate/0.18.0-32daf6f4" \
  -hwaccel v4l2request -rtsp_transport tcp -timeout 10000000 -i rtsp://host.docker.internal:8554/cam2 \
  -f segment -segment_time 10 -segment_format mp4 -reset_timestamps 1 -strftime 1 -c:v copy -c:a aac /tmp/cache/cam2@%Y%m%d%H%M%S%z.mp4 \
  -r 5 -vf fps=5,scale=640:360 -threads 2 -f rawvideo -pix_fmt yuv420p pipe: 2>/tmp/detect.err | wc -c | awk "{printf \"detect pipe: %d bytes = %.1f frames of 640x360 yuv420p\n\", \$1, \$1/345600}"
echo "rc=${PIPESTATUS[0]}"; grep -v "^\[ffmpeg-avd-vk\]" /tmp/detect.err | head -5; grep "^\[ffmpeg-avd-vk\]" /tmp/detect.err | cut -c1-400
echo "--- record segments:"; ls -l /tmp/cache/; for f in /tmp/cache/*.mp4; do echo -n "$f: "; $P -v error -show_entries stream=codec_name,width,height -show_entries format=duration -of csv=p=0 "$f" | tr "\n" " "; echo; done
echo "=== 2. Frigate ffprobe (util/services.py ffprobe_stream)"
timeout 20 $P -v quiet -print_format json -show_format -show_streams -rtsp_transport tcp -timeout 10000000 rtsp://host.docker.internal:8554/cam2 | python3 -c "import json,sys; d=json.load(sys.stdin); print([(s[\"codec_type\"],s[\"codec_name\"],s.get(\"width\"),s.get(\"height\")) for s in d[\"streams\"]])"
echo "=== 3. jsmpeg/birdseye-style encode from rawvideo (no hwaccel args -> pass-through)"
head -c $((345600*25)) /research/avdvk/B.yuv | timeout 30 $F -threads 1 -f rawvideo -pix_fmt yuv420p -video_size 640x360 -i pipe: -threads 1 -f mpegts -s 1280x720 -codec:v mpeg1video -q 8 -bf 0 pipe: 2>/tmp/js.err | wc -c; grep -c "ffmpeg-avd-vk" /tmp/js.err
echo "=== 4. export-style -c copy from a record segment (pass-through)"
seg=$(ls /tmp/cache/*.mp4 | head -1); timeout 30 $F -hide_banner -y -i "$seg" -c copy -movflags +faststart /tmp/export.mp4 2>/tmp/exp.err; echo "rc=$? $(stat -c %s /tmp/export.mp4) bytes"; grep -c "ffmpeg-avd-vk" /tmp/exp.err
echo "=== 5. same detect command with AVD_VK_REWRITE=0 (hw decode + read-back + swscale), 8 s"
AVD_VK_REWRITE=0 AVD_VK_DEBUG=1 timeout -s INT -k 5 8 $F -hide_banner -loglevel warning -threads 2 -hwaccel v4l2request -rtsp_transport tcp -timeout 10000000 -i rtsp://host.docker.internal:8554/cam2 -r 5 -vf fps=5,scale=640:360 -threads 2 -f rawvideo -pix_fmt yuv420p pipe: 2>/tmp/d0.err | wc -c; grep "^\[ffmpeg-avd-vk\]" /tmp/d0.err | cut -c1-120
echo "=== 6. Honeykrisp really used? (libvulkan_asahi mapped by the ffmpeg process during a 6 s run)"
( timeout -s INT -k 3 6 $F -loglevel error -hwaccel v4l2request -rtsp_transport tcp -i rtsp://host.docker.internal:8554/cam2 -r 5 -vf fps=5,scale=640:360 -f rawvideo -pix_fmt yuv420p pipe: >/dev/null 2>&1 & sleep 4; grep -l libvulkan_asahi /proc/[0-9]*/maps 2>/dev/null | head -2; grep -l "lvp\|llvmpipe" /proc/[0-9]*/maps 2>/dev/null | head -1; wait )
' 2>&1 | tee "$OUT/run.txt"
