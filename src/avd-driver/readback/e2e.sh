#!/bin/bash
# Frigate-representative chain inside frigate:latest.
# Usage: [CHAIN="..."] e2e.sh <label> <ffmpeg-path-in-container> hw|sw [extra ffmpeg opts before -i]
# CHAIN defaults to the Frigate detect chain; output md5 + ffmpeg -benchmark utime/stime/rtime.
label=$1; FF=$2; mode=$3; shift 3
R=${FRIGATE_HOME:-$HOME/frigate}/research
CHAIN=${CHAIN:--vf fps=5,scale=640:360 -f rawvideo -pix_fmt yuv420p -}
hw=""; [ "$mode" = hw ] && hw="-hwaccel v4l2request"
out=$(docker run --rm --device /dev/video0 --device /dev/media0 \
  -v /opt/ffmpeg-avd:/opt/ffmpeg-avd:ro,z -v /opt/ffmpeg-avd-v1:/opt/ffmpeg-avd-v1:ro,z -v ${FRIGATE_HOME:-$HOME/frigate}/src/avd-driver/readback:/rb:ro,z -v $R:/r:ro,z \
  --entrypoint bash frigate:latest -c "timeout -s KILL 60 $FF -hide_banner -loglevel info -benchmark $hw $* -i /r/cam2-10s.h264 $CHAIN 2>/tmp/err | md5sum | cut -c1-12; grep -oE 'bench: utime=[^ ]* stime=[^ ]* rtime=[^ ]*' /tmp/err; grep -c 'Failed setup' /tmp/err")
printf '%-40s %s\n' "$label" "$(echo "$out" | tr '\n' ' ')"
