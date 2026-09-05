#!/bin/bash
# Reproduce the stream-start fault (P-frame first, empty DPB) with research/cam2-10s-noidr.h264 inside
# frigate:latest, hw decode + Vulkan chain, and compare the frames after the first IDR with software decode.
#   noidr-test.sh <label>     (load the module to test with reload.sh first)
set -u
label=$1; R=${FRIGATE_HOME:-$HOME/frigate}/research; OUT=$R/avdvk/noidr; mkdir -p $OUT
mark=$(sudo dmesg | tail -1 | sed -n 's/^\[ *\([0-9.]*\)\].*/\1/p')
cd ${FRIGATE_HOME:-$HOME/frigate}/src/ffmpeg-vulkan
EXTRA_DOCKER_ARGS="--device /dev/video0 --device /dev/media0" RUN_ENTRY=bash ./run-in-image.sh -c '
set -u
echo "== hw (zero-copy chain, full rate, framemd5)"
timeout -s KILL 30 $FFMPEG_VK -loglevel info -init_hw_device vulkan=vk -filter_hw_device vk -hwaccel v4l2request -hwaccel_output_format drm_prime \
  -i /research/cam2-10s-noidr.h264 -vf hwmap,scale_vulkan=640:360,hwdownload,format=nv12,format=yuv420p -f framemd5 -y /research/avdvk/noidr/hw-'"$label"'.framemd5 2>/research/avdvk/noidr/hw-'"$label"'.log
echo "rc=$?"; grep -cE "Failed waiting|Decoding error|Invalid data" /research/avdvk/noidr/hw-'"$label"'.log; grep -E "frames decoded|decode errors" /research/avdvk/noidr/hw-'"$label"'.log
echo "== sw reference (same scaler chain on the CPU-decoded frames is not bit-identical; compare frame counts and hw-vs-hw md5 instead)"
timeout -s KILL 60 $FFMPEG_VK -loglevel info -i /research/cam2-10s-noidr.h264 -vf scale=640:360 -f framemd5 -y /research/avdvk/noidr/sw.framemd5 2>/research/avdvk/noidr/sw.log
grep -E "frames decoded|decode errors" /research/avdvk/noidr/sw.log
echo "hw frames out: $(grep -vc "^#" /research/avdvk/noidr/hw-'"$label"'.framemd5)   sw frames out: $(grep -vc "^#" /research/avdvk/noidr/sw.framemd5)"
'
echo "--- dmesg since start:"; sudo dmesg | awk -v m="$mark" '{t=$0; sub(/^\[ */,"",t); sub(/\].*/,"",t); if (t+0 > m+0) print}' | grep -viE 'veth|docker0|eth0'
