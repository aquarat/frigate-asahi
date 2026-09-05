#!/bin/bash
# Go back to the in-tree (Fedora-signed) apple_avd module.
set -u
if sudo fuser /dev/video0 /dev/media0 >/dev/null 2>&1; then
  echo "ERROR: /dev/video0 or /dev/media0 is in use:"; sudo fuser -v /dev/video0 /dev/media0; exit 3
fi
mark=$(sudo dmesg | sed -n 's/^\[ *\([0-9.]*\)\].*/\1/p' | tail -1)
if lsmod | grep -q '^apple_avd'; then
  echo "== rmmod apple_avd"; sudo rmmod apple_avd || { echo "rmmod failed"; sudo dmesg | tail -5; exit 5; }
fi
echo "== modprobe apple_avd (in-tree)"
sudo modprobe apple_avd || { echo "modprobe failed"; sudo dmesg | tail -10; exit 6; }
for i in $(seq 1 50); do [ -e /dev/video0 ] && break; sleep 0.1; done
[ -e /dev/video0 ] || { echo "ERROR: /dev/video0 did not reappear"; sudo dmesg | tail -10; exit 7; }
echo "== loaded $(modinfo -F filename apple_avd); taint='$(sudo cat /sys/module/apple_avd/taint)' (empty = in-tree)"
lsmod | grep '^apple_avd'
echo "== dmesg tail:"; sudo dmesg | awk -v m="$mark" '{t=$0; sub(/^\[ */,"",t); sub(/\].*/,"",t); if (t+0 > m+0) print}' | tail -20
