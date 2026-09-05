#!/bin/bash
# Decode one clip (fakesink) while streaming dmesg to a file; useful with the
# DEBUG=1 build (make DEBUG=1 && ./reload.sh) which logs every instruction word.
#   ./capture.sh <clip.h264 in research/> <outfile.txt>
set -u
RESEARCH=${RESEARCH:-${FRIGATE_HOME:-$HOME/frigate}/research}
clip=$1; out=$2
if sudo fuser /dev/video0 /dev/media0 >/dev/null 2>&1; then echo "/dev/video0 busy"; exit 2; fi
mark=$(sudo dmesg | sed -n 's/^\[ *\([0-9.]*\)\].*/\1/p' | tail -1)
sudo dmesg -w > "$out.raw" 2>/dev/null &
DW=$!
sleep 0.3
sudo timeout -s KILL "${TIMEOUT:-25}" gst-launch-1.0 -q filesrc location="$RESEARCH/$clip" ! h264parse ! v4l2slh264dec ! fakesink >/dev/null 2>&1
rc=$?
sleep 1
sudo kill "$DW" 2>/dev/null; wait "$DW" 2>/dev/null
sudo chown "$(id -u)" "$out.raw"
# dmesg -w first replays the whole ring buffer: keep only lines newer than the start mark
awk -v m="$mark" '{t=$0; sub(/^\[ */,"",t); sub(/\].*/,"",t); if (t+0 > m+0) print}' "$out.raw" > "$out"; rm -f "$out.raw"
echo "$clip rc=$rc: $(wc -l < "$out") dmesg lines in $out ($(grep -c avd "$out") avd lines)"
