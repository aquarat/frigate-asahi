#!/bin/bash
# 4 simultaneous Frigate-like decodes of cam2-10s (hw = v4l2request ffmpeg in the frigate image, sw = image's own ffmpeg)
# usage: ./conc_test.sh hw|sw [N]
set -u
R=${FRIGATE_HOME:-$HOME/frigate}/research; L=${FRIGATE_HOME:-$HOME/frigate}/src/avd-driver/logs; mode=$1; N=${2:-4}
if [ "$mode" = hw ]; then
  FF="docker run --rm --device /dev/video0 --device /dev/media0 -v /opt/ffmpeg-avd:/opt/ffmpeg-avd:ro -v $R:/r:ro,z --entrypoint /opt/ffmpeg-avd/bin/ffmpeg frigate:latest -hide_banner -loglevel info -benchmark -hwaccel v4l2request"
else
  FF="docker run --rm -v $R:/r:ro,z --entrypoint /usr/lib/ffmpeg/7.0/bin/ffmpeg frigate:latest -hide_banner -loglevel info -benchmark"
fi
start=$(date +%s.%N)
for i in $(seq 1 $N); do
  ( timeout -s KILL 25 $FF -i /r/cam2-10s.h264 -vf fps=5,scale=640:360 -f rawvideo -pix_fmt yuv420p - 2>"$L/conc-$mode-$i.err" | md5sum > "$L/conc-$mode-$i.md5" ) &
done
wait
end=$(date +%s.%N)
printf "%s x%d: wall=%.2fs\n" "$mode" "$N" "$(echo "$end - $start" | bc)"
for i in $(seq 1 $N); do printf "  #%d md5=%s %s\n" $i "$(cut -c1-12 "$L/conc-$mode-$i.md5")" "$(grep -o 'bench: utime=[^ ]* stime=[^ ]* rtime=[^ ]*' "$L/conc-$mode-$i.err")"; done
