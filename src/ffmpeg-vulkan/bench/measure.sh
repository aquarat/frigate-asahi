#!/bin/bash
# CPU/wall measurement of one Frigate-like detect chain inside frigate:latest, with GNU time -v
# (the host's /usr/bin/time run under the Fedora loader; the image has no time(1)).
#   measure.sh <label> sw|hwcpu|hwvk|hwvk2 [N parallel]    -> prints user/sys/wall/maxrss per run
# sw    = /opt/ffmpeg-vk (software decode, -threads 2, swscale)           md5 must be ee810b0f…
# sw70  = image's own /usr/lib/ffmpeg/7.0 (what Frigate runs today)
# hwcpu = /opt/ffmpeg-avd (v4l2request decode, read-back, swscale)         md5 ee810b0f…
# hwvk  = /opt/ffmpeg-vk  zero-copy (v4l2request drm_prime -> hwmap -> scale_vulkan -> hwdownload)
# hwvk2 = same, two-stage scale_vulkan=1152:648,scale_vulkan=640:360
set -u
label=$1; mode=$2; N=${3:-1}
R=${FRIGATE_HOME:-$HOME/frigate}/research; OUT=$R/avdvk/bench; mkdir -p "$OUT"
ICD=${FRIGATE_HOME:-$HOME/frigate}/src/ffmpeg-vulkan/icd
LD="/host-lib64-ld.so --library-path /host-lib64"
TIME="$LD /host-time -v"
case $mode in
  sw)    CMD="$LD /opt/ffmpeg-vk/bin/ffmpeg -threads 2 -i /research/cam2-10s.h264 -vf fps=5,scale=640:360 -threads 2";;
  sw70)  CMD="/usr/lib/ffmpeg/7.0/bin/ffmpeg -threads 2 -i /research/cam2-10s.h264 -vf fps=5,scale=640:360 -threads 2";;
  hwcpu) CMD="$LD /opt/ffmpeg-avd/bin/ffmpeg -hwaccel v4l2request -i /research/cam2-10s.h264 -vf fps=5,scale=640:360 -threads 2";;
  hwvk)  CMD="$LD /opt/ffmpeg-vk/bin/ffmpeg -init_hw_device vulkan=vk -filter_hw_device vk -hwaccel v4l2request -hwaccel_output_format drm_prime -i /research/cam2-10s.h264 -vf fps=5,hwmap,scale_vulkan=640:360,hwdownload,format=nv12,format=yuv420p -threads 2";;
  hwvk2) CMD="$LD /opt/ffmpeg-vk/bin/ffmpeg -init_hw_device vulkan=vk -filter_hw_device vk -hwaccel v4l2request -hwaccel_output_format drm_prime -i /research/cam2-10s.h264 -vf fps=5,hwmap,scale_vulkan=1152:648,scale_vulkan=640:360,hwdownload,format=nv12,format=yuv420p -threads 2";;
  *) echo "bad mode"; exit 2;;
esac
DEV="--device /dev/dri/renderD128"; case $mode in hw*) DEV="$DEV --device /dev/video0 --device /dev/media0";; esac
start=$(date +%s.%N)
for i in $(seq 1 $N); do
  ( docker run --rm $DEV \
      -v /usr/lib64:/host-lib64:ro -v /usr/lib/ld-linux-aarch64.so.1:/host-lib64-ld.so:ro -v /usr/bin/time:/host-time:ro \
      -v $ICD:/etc/vulkan-asahi:ro,z -v /opt/ffmpeg-vk:/opt/ffmpeg-vk:ro,z -v /opt/ffmpeg-avd:/opt/ffmpeg-avd:ro,z -v $R:/research:ro,z \
      -e VK_DRIVER_FILES=/etc/vulkan-asahi/asahi_icd.container.json -e VK_ICD_FILENAMES=/etc/vulkan-asahi/asahi_icd.container.json \
      --entrypoint bash frigate:latest -c "timeout -s KILL 60 $TIME $CMD -hide_banner -loglevel warning -f rawvideo -pix_fmt yuv420p - 2>/tmp/err | md5sum | cut -c1-12; cat /tmp/err" > "$OUT/$label-$i.txt" 2>&1 ) &
done
wait
end=$(date +%s.%N)
for i in $(seq 1 $N); do
  f="$OUT/$label-$i.txt"
  md5=$(head -1 "$f"); u=$(sed -n 's/.*User time (seconds): //p' "$f"); s=$(sed -n 's/.*System time (seconds): //p' "$f"); w=$(sed -n 's/.*Elapsed (wall clock) time (h:mm:ss or m:ss): //p' "$f"); rss=$(sed -n 's/.*Maximum resident set size (kbytes): //p' "$f"); rc=$(sed -n 's/.*Exit status: //p' "$f")
  printf "%-22s #%d md5=%s user=%ss sys=%ss wall=%s maxrss=%sMB rc=%s\n" "$label" "$i" "$md5" "$u" "$s" "$w" "$((rss/1024))" "$rc"
done
[ "$N" -gt 1 ] && printf "%-22s all %d: wall=%.2fs\n" "$label" "$N" "$(echo "$end - $start" | bc)"
