#!/bin/bash
# Hardware-decode test matrix for the Apple AVD driver.
#   ./test_clips.sh                  run all f_*/g_*/t_*/h_*/cam2-10s clips
#   ./test_clips.sh f_high_8x8.h264  run a subset (names relative to research/)
#   ./test_clips.sh --save-baseline  also write baseline-results.txt
# Per clip prints: OK-identical | DIFFERENT | FAIL(timeout) | FAIL(rc=N)  and the
# number of new 'avd' dmesg lines. Exits 1 if any clip that is OK-identical in
# baseline-results.txt is not OK-identical now (regression), 2 on setup errors.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
RESEARCH=${RESEARCH:-${FRIGATE_HOME:-$HOME/frigate}/research}
SWMD5=$HERE/sw-md5.txt
BASELINE=$HERE/baseline-results.txt
LOGDIR=$HERE/logs; mkdir -p "$LOGDIR"
TMPYUV=${TMPYUV:-$LOGDIR/hw.yuv}   # written by root (sudo); dir is ours so we can unlink it
TIMEOUT=${TIMEOUT:-25}
save=0; clips=()
for a in "$@"; do case $a in --save-baseline) save=1;; *) clips+=("$a");; esac; done
if [ ${#clips[@]} -eq 0 ]; then
  cd "$RESEARCH" && clips=( $(ls f_*.h264 g_*.h264 t_*.h264 h_*.h264 cam2-10s.h264 2>/dev/null) )
fi
[ -e /dev/video0 ] || { echo "no /dev/video0 (module not loaded?)"; exit 2; }
if sudo fuser /dev/video0 /dev/media0 >/dev/null 2>&1; then echo "/dev/video0 busy"; sudo fuser -v /dev/video0 /dev/media0; exit 2; fi
touch "$SWMD5"

sw_md5() {  # cached software reference
  local f=$1 m
  m=$(awk -v f="$f" '$1==f{print $2}' "$SWMD5")
  if [ -z "$m" ]; then
    m=$(docker run --rm -v "$RESEARCH":/r:ro,z --entrypoint /usr/lib/ffmpeg/7.0/bin/ffmpeg frigate:latest \
        -hide_banner -loglevel error -i /r/"$f" -f rawvideo -pix_fmt yuv420p - 2>/dev/null | md5sum | cut -d' ' -f1)
    echo "$f $m" >> "$SWMD5"
  fi
  echo "$m"
}
dmesg_mark() { sudo dmesg | sed -n 's/^\[ *\([0-9.]*\)\].*/\1/p' | tail -1; }
dmesg_since() { sudo dmesg | awk -v m="$1" '{t=$0; sub(/^\[ */,"",t); sub(/\].*/,"",t); if (t+0 > m+0) print}'; }

mod=$(sudo cat /sys/module/apple_avd/taint 2>/dev/null); [ -z "$mod" ] && mod="intree" || mod="taint=$mod(out-of-tree)"
echo "# $(date -Is) kernel=$(uname -r) apple_avd=$mod"
printf "%-28s %-14s %s\n" "clip" "result" "new-avd-dmesg"
results=()
for f in "${clips[@]}"; do
  if [ ! -s "$RESEARCH/$f" ]; then printf "%-28s %-14s %s\n" "$f" "SKIP(empty)" 0; results+=("$f SKIP(empty) 0"); continue; fi
  mark=$(dmesg_mark); rm -f "$TMPYUV"
  sudo timeout -s KILL "$TIMEOUT" gst-launch-1.0 -q filesrc location="$RESEARCH/$f" ! h264parse ! v4l2slh264dec \
       ! videoconvert ! video/x-raw,format=I420 ! filesink location="$TMPYUV" >"$LOGDIR/$f.gst.log" 2>&1
  rc=$?
  if [ $rc -eq 137 ]; then res="FAIL(killed)"
  elif [ $rc -ne 0 ]; then res="FAIL(rc=$rc)"
  else
    hw=$(md5sum "$TMPYUV" | cut -d' ' -f1); sw=$(sw_md5 "$f")
    [ "$hw" = "$sw" ] && res="OK-identical" || res="DIFFERENT"
  fi
  # let the driver settle after a killed client before the next clip
  for i in $(seq 1 30); do sudo fuser /dev/video0 >/dev/null 2>&1 || break; sleep 0.2; done
  [ "$res" = OK-identical ] || sleep 1
  dmesg_since "$mark" > "$LOGDIR/$f.dmesg.log"
  n=$(grep -c 'avd' "$LOGDIR/$f.dmesg.log")
  # the known failure signature: driver reports a frame timeout, gst then bails with rc=1
  if [ "$res" != OK-identical ] && [ "$res" != DIFFERENT ] && grep -q 'Frame processing timed out' "$LOGDIR/$f.dmesg.log"; then res="FAIL(hw-timeout)"; fi
  printf "%-28s %-14s %s\n" "$f" "$res" "$n"
  results+=("$f $res $n")
done
rm -f "$TMPYUV"

rcode=0
if [ -f "$BASELINE" ] && [ $save -eq 0 ]; then
  for r in "${results[@]}"; do
    set -- $r; b=$(awk -v f="$1" '$1==f{print $2}' "$BASELINE")
    if [ "$b" = OK-identical ] && [ "$2" != OK-identical ]; then echo "REGRESSION: $1 was OK-identical, now $2"; rcode=1; fi
    if [ -n "$b" ] && [ "$b" != OK-identical ] && [ "$2" = OK-identical ]; then echo "IMPROVED: $1 was $b, now OK-identical"; fi
  done
fi
if [ $save -eq 1 ]; then
  { echo "# baseline $(date -Is) kernel=$(uname -r) apple_avd=$mod"; printf "%-28s %-14s %s\n" clip result new-avd-dmesg; for r in "${results[@]}"; do set -- $r; printf "%-28s %-14s %s\n" "$1" "$2" "$3"; done; } > "$BASELINE"
  echo "baseline written to $BASELINE"
fi
exit $rcode
