# Apple AVD (V4L2 stateless decoder) — out-of-tree build + live test harness

Machine: Apple M1 Mac mini (T8103), Fedora Asahi 44, kernel
`7.1.6-400.asahi.fc44.aarch64+16k` (copr group_asahi:kernel). Driver source:
AsahiLinux/linux tag **`asahi-7.1.6-1`**, commit
**`e2e1930a9595bffafad92cec2b5504525efb9cd4`**. The `.text` section of the
unmodified out-of-tree build is byte-identical to the shipped
`apple-avd.ko.xz`, so this is exactly the running driver.

Background and failure analysis: `~/frigate/research/AVD_PHASE2_FINDINGS.md`.
Test clips: `~/frigate/research/{f_,g_,t_,h_}*.h264`, `cam2-10s.h264`.

## Layout

```
linux-asahi/            sparse+shallow clone at asahi-7.1.6-1 (drivers/media/platform/apple/avd,
                        include/uapi/linux/{videodev2,v4l2-controls}.h, include/media, t8103*.dtsi)
apple-avd/              standalone module tree = copy of drivers/media/platform/apple/avd + Kbuild/Makefile
                        (git repo: c4a70b9 pristine == tag; branch `series` (checked out) = patches/final
                        0001-0006; `readback` = same; `dma-coherent`, `coh-probe` = experiments)
apple-avd-fixed-0008.ko build of branch `series` = patches/final 0001-0006 + 0008 (the recommended module)
apple-avd-fixed.ko      previous recommended build = 0001-0006 (no 0008: stream starts on a P-frame fault + reset)
build-and-install.sh    build `series` and install it into /lib/modules/$(uname -r)/updates (+ --status, --undo); NOT run
apple-avd-fixed-v1.ko   previous recommended build = 0001-0005 only (uncached read-back)
apple-avd-cachehints.ko identical to apple-avd-fixed.ko (name used during the read-back work)
apple-avd-dmacoherent.ko EXPERIMENT (branch dma-coherent): 0001-0006 + dev->dma_coherent forced on
                        (= what the DT dma-coherent property of patches/final/0007 does)
apple-avd-cohprobe.ko   EXPERIMENT (branch coh-probe): as above + per-frame cache-coherency probe (dmesg COHPROBE)
apple-avd-0001-*.ko     intermediate builds used for the bisection in RESULTS.md
apple-avd-baseline.ko   unmodified build (reference)
apple-avd-debug.ko      unmodified build with -DDEBUG_INST -DDEBUG_INST_ADDR (logs every instruction word)
patches/                0001-h264-8x8-luma-tbs-experiment.patch (first experiment, superseded)
patches/final/          the recommended series (git format-patch, kernel paths), see RESULTS.md;
                        0006 = cacheable capture buffers, 0007 = RFC DT dma-coherent (not in the module)
patches/ffmpeg/         2 patches for Kwiboo FFmpeg b57fbbe (v4l2-request-n8.1): request non-coherent
                        CAPTURE buffers + NO_CACHE_CLEAN; built into /opt/ffmpeg-avd (old: /opt/ffmpeg-avd-v1)
patches/optional/       early-submit experiment, not needed
readback/               read-back work: rb_bench.c (buffer read bandwidth), e2e.sh (Frigate chain in
                        frigate:latest with -benchmark), sw-framemd5-cam2.txt (per-frame software
                        reference), run-*.txt / test_clips-cachehints.log / ffmpeg-build-p*.log (raw results),
                        ffmpeg-p1.diff, ffmpeg-p2 (binary with patches 1+2 = /opt/ffmpeg-avd/bin/ffmpeg)
RESULTS.md              per-configuration results, real-stream and concurrency numbers
conc_test.sh            4 simultaneous Frigate-like decodes (hw|sw) with ffmpeg -benchmark
reload.sh               swap the loaded apple_avd for a freshly built .ko
restore.sh              go back to the in-tree Fedora-signed module
test_clips.sh           clip matrix: OK-identical / DIFFERENT / FAIL(...) + new avd dmesg lines
capture.sh              decode one clip while streaming dmesg to a file (for the DEBUG build)
sw-md5.txt              cached software-decode (ffmpeg in frigate:latest) md5 per clip
results-*.txt           raw test_clips.sh output per configuration (see RESULTS.md)
baseline-results.txt    matrix on the unmodified driver (regression reference for test_clips.sh)
patched-results.txt     matrix with patches/0001 applied
logs/                   per-clip gst stderr + dmesg slice from the last test_clips.sh run
diag/                   captured diagnostics (see below)
```

## Build

```bash
cd ~/frigate/src/avd-driver/apple-avd
make              # -> apple-avd.ko against /lib/modules/$(uname -r)/build (kernel-16k-devel-7.1.6-400)
make DEBUG=1      # same, plus DEBUG_INST/DEBUG_INST_ADDR: dev_info of every word pushed to the decoder
make AVD_CFLAGS='-DAVD_DEBUG'   # arbitrary extra defines (AVD_DEBUG only affects avd-vp9.c)
make clean
```
Build takes ~2 s. The driver has no `CONFIG_*` ifdefs; the in-tree symbol
`CONFIG_VIDEO_APPLE_AVD=m` is replaced by `obj-m` in `Kbuild`. Kernel has
`MODULE_SIG` but not `MODULE_SIG_FORCE` and lockdown is `[none]`, so the
unsigned module loads with taint `OE` (harmless). No `dynamic_debug` sites and
no module parameters exist in this driver; the only knobs are the compile-time
defines above. `avd_status()` (VP register dump, avd-hw.c) is only called on the
multi-slice `readl_poll_timeout` path, not on the watchdog path.

## Reload / restore

```bash
cd ~/frigate/src/avd-driver
./reload.sh                       # apple-avd/apple-avd.ko
./reload.sh apple-avd-debug.ko    # any .ko
./restore.sh                      # rmmod + modprobe apple_avd (in-tree)
```
Both refuse to run if `sudo fuser /dev/video0 /dev/media0` shows a user, check
vermagic, wait for `/dev/video0` to come back, and print the new dmesg lines.
Nothing else uses `/dev/video0` (Frigate decodes on CPU), so this is safe while
the `frigate` container runs. Taint `OE` in `/sys/module/apple_avd/taint`
means the out-of-tree module is loaded; empty means in-tree.

## Test

```bash
./test_clips.sh                              # whole matrix, compares with baseline-results.txt,
                                             # exit 1 if a previously OK-identical clip regressed
./test_clips.sh f_high_8x8.h264 cam2-10s.h264   # subset
./test_clips.sh --save-baseline              # rewrite baseline-results.txt (only for a new reference!)
```
Per clip it runs `sudo timeout -s KILL 25 gst-launch-1.0 -q filesrc ! h264parse !
v4l2slh264dec ! videoconvert ! video/x-raw,format=I420 ! filesink` and md5-compares
with the software reference (`docker run --rm -v research:/r:ro,z --entrypoint
/usr/lib/ffmpeg/7.0/bin/ffmpeg frigate:latest -i /r/<clip> -f rawvideo -pix_fmt yuv420p -`,
cached in `sw-md5.txt`). Results: `OK-identical`, `DIFFERENT` (decoded but
mismatching), `FAIL(hw-timeout)` (driver logged "Frame processing timed out";
gst exits 1 after ~2 s with "Not enough memory to decode H264 stream"),
`FAIL(killed)` (25 s timeout), `FAIL(rc=N)`. Per-clip gst stderr and dmesg
slices are in `logs/`. The driver recovers on its own after a failure (watchdog
resets it); the harness waits for `/dev/video0` to be free before the next clip.

Verbose capture with the DEBUG build:
```bash
./reload.sh apple-avd-debug.ko
./capture.sh f_high_8x8.h264 diag/my-run.txt    # dmesg lines from this run only
```

## Baseline (unmodified driver, 2026-09-05)

Identical for the in-tree module and the out-of-tree build.

```
clip                         result         new-avd-dmesg
f_base_plain.h264            OK-identical   0
f_high_8x8.h264              FAIL(hw-timeout) 2
f_high_no8x8.h264            OK-identical   0
f_main_bframes.h264          OK-identical   0
f_main_cabac.h264            OK-identical   0
f_main_plain.h264            OK-identical   0
f_main_ref3.h264             OK-identical   0
f_main_weightp.h264          OK-identical   0
g_1080_base.h264             OK-identical   0
g_1080_high8x8.h264          FAIL(hw-timeout) 2
g_1080_main.h264             FAIL(hw-timeout) 1
g_1296_base.h264             FAIL(hw-timeout) 1
g_1296_main.h264             FAIL(hw-timeout) 1
h_1296_base_lowbr.h264       OK-identical   0
h_360_base_lossless.h264     SKIP(empty)    0
h_360_high8x8_lowbr.h264     FAIL(hw-timeout) 2
cam2-10s.h264             FAIL(hw-timeout) 4
t_1080p_high.h264            FAIL(hw-timeout) 2
t_1080p_main_cavlc.h264      FAIL(hw-timeout) 1
t_1280x720_base.h264         OK-identical   0
t_1296p_main_cavlc.h264      FAIL(hw-timeout) 1
t_320x240_base.h264          OK-identical   0
t_320x240_high.h264          FAIL(hw-timeout) 2
t_640x360_base.h264          OK-identical   0
t_720p_high.h264             FAIL(hw-timeout) 2
t_960x540_base.h264          OK-identical   0
```
Two distinct dmesg signatures: 8x8/High clips give `H2 00 error` (hardware
error IRQ, arrives in the same microsecond as the last instruction word) and
then `Frame processing timed out! Vp: 2 (00)` (2 lines); the 1080p+/1296p
failures give only the watchdog timeout (1 line), no `H2` error.

## Diagnostics captured (diag/)

- `f_high_8x8.dmesg-debuginst.txt`, `f_high_no8x8.dmesg-debuginst.txt`:
  DEBUG_INST dmesg of the full instruction stream (failing clip: first frame
  then error; OK clip: all 30 frames). Compare first frames with
  `diff <(sed -n '1,/inst_fifo_end/p' A | sed 's/^.*avd: //') <(... B ...)`.
- `*.gst-v4l2codecs.full.log` / `*.first-frame.log`: `GST_DEBUG=v4l2codecs*:7,
  codecparsers_h264:6`. GStreamer does not print the V4L2 control struct
  payloads at any level; to see what the driver receives, add a `dev_info`
  dump of `run->pps->flags` / `run->sps` in `avd_h264_run_preamble()`
  (avd-h264.c) and use `capture.sh`.

## Findings so far

**Problem 1 (High profile / transform_8x8_mode) — cause found, one-line fix
verified.** The DEBUG_INST streams of `f_high_8x8` and `f_high_no8x8` are
identical in every non-address word, including `hdr_2c_sps_param`
(`0x01002801`), although that word is where the driver means to pass the 8x8
flag. `avd-h264.c:219`:
```c
| AVD_HDR_COMMON_LUMA_TBS(pps->flags & V4L2_H264_PPS_FLAG_TRANSFORM_8X8_MODE)
```
`AVD_HDR_COMMON_LUMA_TBS(v)` is `FIELD_PREP(GENMASK(8, 7), v)` and the flag is
`0x40`, so `(0x40 << 7) & 0x180 == 0`: the flag is always dropped and the hardware
decodes 8x8-transform macroblocks as 4x4 -> `H2 00 error`. With
`patches/0001-h264-8x8-luma-tbs-experiment.patch` (`!!(...)`, i.e. field value 1):

```
f_high_8x8.h264              OK-identical   (was FAIL)
t_320x240_high.h264          OK-identical   (was FAIL)
h_360_high8x8_lowbr.h264     OK-identical   (was FAIL)
t_720p_high.h264             OK-identical   (was FAIL)
all previously-OK clips      OK-identical   (no regressions)
g_1080_high8x8 / t_1080p_high / cam2-10s   still FAIL(hw-timeout), now 1 line (no H2 error)
```
Full table: `patched-results.txt`. Whether 1 is the right field value (vs. 2
or 3, cf. HEVC's `log2_diff_max_min_luma_transform_block_size + ...` and VP9's
`tx_mode`) should be checked against the m1n1/macOS traces in
`~/Projects/avd-work/docs/` before proposing upstream, but bit-identical output on
four 8x8 clips is strong evidence. The patch is NOT applied in `apple-avd/`
(tree is pristine): `cd apple-avd && patch -p1 < ../patches/0001-*.patch && make && ../reload.sh`.

**Problem 2 — RESOLVED, see RESULTS.md**: it is the T8103 `VP_INSN_FIFO_MASK`
value (0x100000 -> 0, upstream ca9a850f237f in 7.1.12), not buffer sizing. The
original note is kept below for history.

**Problem 2 (>= 1080p with CABAC/B-frames, 1296p at higher bitrate)** was
unchanged by the 8x8 patch: watchdog timeout with no hardware error IRQ, i.e. the
decoder just never finishes. Candidates: bitstream/slice-buffer or DPB/aux buffer
sizing in `avd-h264.c` (`alloc_slots`, `fill_rvra`, `stream_hdr`), the
`msecs_to_jiffies(2000)` watchdog is not the limit (fast clips finish in ms).
Pair `g_1080_base` (OK, 411 KB) vs `g_1080_main` (FAIL, 377 KB) for a reproducer;
`h_1296_base_lowbr` (OK) vs `g_1296_base` (FAIL) isolates bitrate at the same size.

## Recommended module + how to make it persist across reboots

Status 2026-09-05 (details and tables in `RESULTS.md`): with the patches in
`patches/final/` every test clip, including the real IP-camera stream
`cam2-10s.h264` (2304x1296 High, CABAC, 8x8, scaling lists), decodes
bit-identically to software, also with four decoders running concurrently.
The built module for exactly that series is **`apple-avd-fixed.ko`**
(vermagic `7.1.6-400.asahi.fc44.aarch64+16k`, no module parameters, prints
nothing unless a frame fails). `apple-avd/` is a git repo: branch `series`
(checked out) is the final set, `c4a70b9` is the pristine tree; `make` on
`series` reproduces `apple-avd-fixed.ko`.

```
patches/final/0001  h264: fix transform_8x8_mode_flag encoding     (backport 8e8d72a673ab, 7.1.8)   bug 1
patches/final/0002  h264: fix default weights handling for P slices (backport 3cba4f1d5b86, 7.1.12)  P-slice weightp pixels
patches/final/0003  t8103: set VP_INSN_FIFO_MASK to 0               (backport ca9a850f237f, 7.1.12)  bug 2 (large/CABAC frames)
patches/final/0004  h264: scale the held-slice parse poll timeout   (new)                            multi-slice frames > ~16 KiB/slice
patches/final/0005  dump the VP status registers on error/timeout   (new, diagnostic, error path only)
patches/final/0006  allow cacheable (non-coherent) MMAP buffers      (new)                            read-back 250 MB/s -> 20 GB/s
patches/final/0007  [RFC] t8103 DT: dma-coherent on the avd node     (new, DT only, NOT in the .ko)    same effect for every client
patches/final/0008  h264: reject slices that reference invalid DPB   (new)                            DART fault + watchdog reset at every
                    entries                                                                            stream start on a non-IDR frame (live RTSP)
```
`apple-avd-fixed-0008.ko` (md5 `cf9e03be…`) = 0001-0006 + 0008 and is what branch `series` builds
now; it is the module to install (`build-and-install.sh`). Details of 0008 and the Frigate
integration: `~/frigate/research/AVD_FRIGATE_INTEGRATION.md`.
`apple-avd-fixed.ko` (md5 `c9c5f85b…`) is 0001-0006; the previous build without
0006 is kept as `apple-avd-fixed-v1.ko`. 0006 changes nothing for clients that do
not ask for `V4L2_MEMORY_FLAG_NON_COHERENT`; with the patched ffmpeg in
`/opt/ffmpeg-avd` (patches/ffmpeg/0001-0002) the Frigate-like chain costs
0.34-0.42 s user + 0.07-0.11 s sys per 150 frames instead of 3.0 s (software:
0.62 s user with `-threads 2`). Everything about that work — measurements, the
options tried, the coherency probe, reproduction — is in
`~/frigate/research/AVD_READBACK.md`.
`patches/optional/` holds the early-submit experiment (not needed, see its README).

Temporary use (until the next reboot; safe while Frigate runs, nothing else
uses `/dev/video0`):
```bash
cd ~/frigate/src/avd-driver && ./reload.sh apple-avd-fixed.ko
./restore.sh        # back to the in-tree module
```

Making it persist (NOT done on this machine, the system is left on the in-tree
module; `build-and-install.sh` scripts steps 1-2 and `--status` shows the state; do this deliberately, and undo it when a `kernel-16k` >= 7.1.12 lands
in the copr, because that tree already contains 0001-0003; 0006 is still needed
for the fast read-back until it is upstream):

1. Install the module where `modprobe` prefers it over the in-tree copy
   (the `extra/` and `updates/` directories are searched first by depmod):
   ```bash
   sudo install -D -m 644 apple-avd-fixed.ko /lib/modules/$(uname -r)/updates/apple-avd.ko
   sudo depmod -a
   modinfo -F filename apple_avd        # must print .../updates/apple-avd.ko
   ```
   The kernel has `MODULE_SIG` without `MODULE_SIG_FORCE` and lockdown is off,
   so the unsigned module loads (taint `OE`), exactly as `reload.sh` does now.
2. The module is loaded at boot by udev from the device tree; if the
   initramfs contains a copy, regenerate it so the updated file is picked up:
   `sudo dracut -f --kver $(uname -r)` (check first with
   `lsinitrd | grep apple-avd`; if it is not in the initramfs nothing to do).
3. Verify after the next reboot: `modinfo -F filename apple_avd`, then
   `sudo gst-launch-1.0 -q filesrc location=~/frigate/research/cam2-10s.h264 ! h264parse ! v4l2slh264dec ! fakesink`
   with `sudo dmesg | tail` clean.
4. Every kernel update needs a rebuild (`cd apple-avd && git checkout series &&
   make`) against the new `kernel-16k-devel` and a fresh install into the new
   `/lib/modules/<ver>/updates/`; a DKMS wrapper would automate this but is
   deliberately not set up. Remove `/lib/modules/*/updates/apple-avd.ko` and run
   `depmod -a` to go back to the in-tree driver.

Alternative: `patches/final/*.patch` apply cleanly (`git apply`) to the
AsahiLinux `asahi-7.1.6-1` tree under `drivers/media/platform/apple/avd/`, so
the copr kernel package can be rebuilt with them instead.

## Safety rules

Never reboot or touch the bootloader/default kernel; do not touch the `frigate`
container, `~/frigate/config` or `.../detector`. Always check
`sudo fuser /dev/video0 /dev/media0` is empty before rmmod (the scripts do),
always decode under `timeout -s KILL 25`, and read `sudo dmesg` after each step.
If a module load oopses or hangs, stop. Finish with `./restore.sh`.
