#!/bin/bash
# Swap the running apple_avd module for the freshly built out-of-tree one.
# Usage: ./reload.sh [path/to/apple-avd.ko]   (default: ./apple-avd/apple-avd.ko)
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
KO=${1:-$HERE/apple-avd/apple-avd.ko}
[ -f "$KO" ] || { echo "no such module: $KO (run make in $HERE/apple-avd first)"; exit 2; }

# Refuse to yank the module out from under a decoder client.
if sudo fuser /dev/video0 /dev/media0 >/dev/null 2>&1; then
  echo "ERROR: /dev/video0 or /dev/media0 is in use:"; sudo fuser -v /dev/video0 /dev/media0; exit 3
fi
want=$(modinfo -F vermagic "$KO"); have="$(uname -r) SMP preempt mod_unload aarch64"
[ "$want" = "$have" ] || { echo "ERROR: vermagic mismatch: '$want' vs '$have'"; exit 4; }

mark=$(sudo dmesg | sed -n 's/^\[ *\([0-9.]*\)\].*/\1/p' | tail -1)
if lsmod | grep -q '^apple_avd'; then
  echo "== rmmod apple_avd"; sudo rmmod apple_avd || { echo "rmmod failed"; sudo dmesg | tail -5; exit 5; }
fi
echo "== insmod $KO"
sudo insmod "$KO" || { echo "insmod failed"; sudo dmesg | tail -10; exit 6; }
for i in $(seq 1 50); do [ -e /dev/video0 ] && break; sleep 0.1; done
[ -e /dev/video0 ] || { echo "ERROR: /dev/video0 did not reappear"; sudo dmesg | tail -10; exit 7; }
echo "== loaded from $KO; taint=$(sudo cat /sys/module/apple_avd/taint) (OE = out-of-tree+unsigned, empty = in-tree)"
lsmod | grep '^apple_avd'
echo "== dmesg tail:"; sudo dmesg | awk -v m="$mark" '{t=$0; sub(/^\[ */,"",t); sub(/\].*/,"",t); if (t+0 > m+0) print}' | tail -20
