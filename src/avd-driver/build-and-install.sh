#!/bin/bash
# Build the patched apple-avd module (branch `series` = patches/final/0001-0006) against the RUNNING
# kernel and install it where modprobe prefers it over the in-tree copy. NOT run automatically:
# this is the deliberate "make it persist across reboots" step from README.md.
#
#   ./build-and-install.sh            build + install into /lib/modules/$(uname -r)/updates/ + depmod
#   ./build-and-install.sh --undo     remove it, depmod, back to the in-tree module on next load/boot
#   ./build-and-install.sh --status   which apple_avd modprobe would pick, initramfs, loaded taint
#
# Requirements: kernel-16k-devel for the running kernel (make needs /lib/modules/$(uname -r)/build),
# passwordless sudo. The module is unsigned: the kernel has MODULE_SIG without MODULE_SIG_FORCE and
# lockdown is off, so it loads with taint OE (same as reload.sh today).
#
# Every kernel update needs this again (new /lib/modules/<ver>/updates/). Undo it once a kernel-16k
# >= 7.1.12 ships with 0001-0003 in-tree AND 0006 (cacheable capture buffers) is merged, or keep it
# for 0006 alone until then.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
KVER=$(uname -r)
DEST=/lib/modules/$KVER/updates/apple-avd.ko
SRC=$HERE/apple-avd

status() {
  echo "kernel:            $KVER"
  echo "modprobe picks:    $(modinfo -F filename apple_avd 2>/dev/null || echo '(none)')"
  echo "installed override: $( [ -f "$DEST" ] && md5sum "$DEST" || echo none )"
  echo "reference build:   $(md5sum "$HERE/apple-avd-fixed.ko" 2>/dev/null || echo '(no apple-avd-fixed.ko)')"
  echo "loaded taint:      '$(sudo cat /sys/module/apple_avd/taint 2>/dev/null)' (OE = out-of-tree/unsigned, empty = in-tree, missing = not loaded)"
  # udev loads apple_avd from the DT alias after the root fs is up; it is only a problem if the
  # initramfs carries its own (old) copy of the module. Empty output = not in the initramfs = nothing to do.
  echo "in initramfs:      $(lsinitrd --kver "$KVER" 2>/dev/null | grep -E 'apple-avd|apple_avd' || echo 'no (good: no dracut -f needed)')"
}

case "${1:-}" in
  --status) status; exit 0 ;;
  --undo)
    if [ -f "$DEST" ]; then sudo rm -v "$DEST"; sudo depmod -a; else echo "nothing installed at $DEST"; fi
    sudo rmdir /lib/modules/$KVER/updates 2>/dev/null || true
    echo "== now:"; modinfo -F filename apple_avd
    echo "The in-tree module is used on the next modprobe/boot; to switch now: $HERE/restore.sh"
    exit 0 ;;
  "") ;;
  *) echo "usage: $0 [--undo|--status]"; exit 2 ;;
esac

[ -d /lib/modules/$KVER/build ] || { echo "no kernel headers for $KVER (dnf install kernel-16k-devel-${KVER%%.aarch64*})"; exit 1; }
echo "== building branch series in $SRC"
( cd "$SRC" && git checkout -q series && git log --oneline -1 && make clean >/dev/null && make ) 2>&1 | tail -3
KO=$SRC/apple-avd.ko
want=$(modinfo -F vermagic "$KO"); have="$KVER SMP preempt mod_unload aarch64"
[ "$want" = "$have" ] || { echo "vermagic mismatch: '$want' vs '$have'"; exit 1; }
echo "== installing $KO -> $DEST"
sudo install -D -m 644 "$KO" "$DEST"
sudo depmod -a
echo "== result:"; status
cat <<MSG

Next steps (manual, deliberately not automated):
  * If 'in initramfs' listed the module:  sudo dracut -f --kver $KVER   (then check again)
  * Load it now without rebooting:        sudo fuser /dev/video0 /dev/media0  (must be empty)
                                          sudo rmmod apple_avd && sudo modprobe apple_avd
                                          modinfo -F filename apple_avd ; sudo cat /sys/module/apple_avd/taint   (OE)
  * Verify a decode:                      sudo gst-launch-1.0 -q filesrc location=$HERE/../../research/cam2-10s.h264 ! h264parse ! v4l2slh264dec ! fakesink ; sudo dmesg | tail
  * Undo:                                 $0 --undo
MSG
