#!/bin/bash
# 10-minute soak of the zero-copy chain on a LIVE go2rtc restream, inside frigate:latest.
#   soak.sh <camera> [seconds]  -> research/avdvk/soak/<camera>.{txt,rss}
set -u
cam=$1; secs=${2:-600}
R=${FRIGATE_HOME:-$HOME/frigate}/research; OUT=$R/avdvk/soak; mkdir -p "$OUT"
ICD=${FRIGATE_HOME:-$HOME/frigate}/src/ffmpeg-vulkan/icd
LD="/host-lib64-ld.so --library-path /host-lib64"
name=avdvk-soak-$cam
docker rm -f $name >/dev/null 2>&1
docker run --rm --name $name --device /dev/dri/renderD128 --device /dev/video0 --device /dev/media0 \
  --add-host host.docker.internal:host-gateway \
  -v /usr/lib64:/host-lib64:ro -v /usr/lib/ld-linux-aarch64.so.1:/host-lib64-ld.so:ro -v /usr/bin/time:/host-time:ro \
  -v $ICD:/etc/vulkan-asahi:ro,z -v /opt/ffmpeg-vk:/opt/ffmpeg-vk:ro,z \
  -e VK_DRIVER_FILES=/etc/vulkan-asahi/asahi_icd.container.json -e VK_ICD_FILENAMES=/etc/vulkan-asahi/asahi_icd.container.json \
  --entrypoint bash frigate:latest -c "timeout -s KILL $((secs+60)) $LD /host-time -v $LD /opt/ffmpeg-vk/bin/ffmpeg -hide_banner -loglevel info -stats_period 30 \
    -init_hw_device vulkan=vk -filter_hw_device vk -hwaccel v4l2request -hwaccel_output_format drm_prime \
    -rtsp_transport tcp -timeout 10000000 -i rtsp://host.docker.internal:8554/$cam -t $secs \
    -r 5 -vf fps=5,hwmap,scale_vulkan=1152:648,scale_vulkan=640:360,hwdownload,format=nv12,format=yuv420p -threads 2 -f null - 2>&1" > "$OUT/$cam.txt" 2>&1 &
# sample container RSS every 30 s
: > "$OUT/$cam.rss"
for i in $(seq 1 $((secs/30+1))); do sleep 30; docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' $name 2>/dev/null >> "$OUT/$cam.rss"; done
wait
