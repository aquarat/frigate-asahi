# Apple AVD H.264 on T8103 (kernel 7.1.6) — patch series results

Machine: M1 Mac mini (T8103), Fedora Asahi 44, `7.1.6-400.asahi.fc44.aarch64+16k`,
driver base AsahiLinux `asahi-7.1.6-1`. All hardware decodes ran under
`timeout -s KILL` (25 s, 90 s for the harness on 150-frame 1296p clips because
GStreamer's software `videoconvert` alone needs ~25 s there), every result was
md5-compared against software decode (ffmpeg 7.0 in `frigate:latest`), and
`sudo dmesg` was checked after every step. No oops, hung task, failed unload or
any other instability was seen at any point; the driver never had to be reset
by anything but its own watchdog on the known-failing clips. Date: 2026-09-05.

Raw harness output per configuration: `results-*.txt`; per-clip gst/dmesg logs in
`logs/`; VP register dumps in `diag/`.

## Summary

| Bug | Symptom | Fixed by | Verified with |
|---|---|---|---|
| 1. `transform_8x8_mode_flag` dropped | `H2 00 error` + watchdog on frame 1 of every High-profile 8x8 stream | `final/0001` (backport 8e8d72a673ab) | f_high_8x8, t_320x240_high, h_360_high8x8_lowbr, t_720p_high → OK-identical |
| 2. large / high-bitrate / CABAC frames stall | watchdog only, VP stops at 85-94 % of the slice, no error IRQ | **`final/0003`: T8103 `VP_INSN_FIFO_MASK` 0x100000 → 0** (backport of Janne Grunau's ca9a850f237f from 7.1.12) | g_1080_main, g_1080_high8x8, g_1296_base, g_1296_main, t_1080p_high, t_1080p_main_cavlc, t_1296p_main_cavlc, cam2-10s → OK-identical |
| 3. P-slice weighted prediction wrong pixels (x264 weightp=2 streams) | decodes, but DIFFERENT | `final/0002` (backport 3cba4f1d5b86) | w_360_main_weightp2: DIFFERENT → OK-identical |
| 4. multi-slice frames with slices > ~16 KiB | `VP2: timed out (00)! size: 0000c231 parsed: 0000b76e` + watchdog (1 ms poll) | `final/0004` (new) | m_1080_high_slices4, m_1296_high_slices8: FAIL → OK-identical |
| diagnostic | — | `final/0005`: VP register dump on error/timeout (error path only) | used to characterise bug 2 |
| 5. read-back of decoded frames costs 18 ms/frame (uncached Normal-NC mapping, ~250 MB/s) | hw decode = 4-5x the CPU of sw decode in the Frigate chain | **`final/0006`** (`allow_cache_hints`, drop `DMA_ATTR_NO_KERNEL_MAPPING`) + `patches/ffmpeg/0001-0002` (request `V4L2_MEMORY_FLAG_NON_COHERENT`, `NO_CACHE_CLEAN`); `final/0007` RFC DT `dma-coherent` as the client-independent alternative | cam2-10s chain 3.06 s → 0.41-0.52 s CPU, md5 unchanged, framemd5 x150 = sw in 8 runs, matrix unchanged; see `research/AVD_READBACK.md` |

The early-submit hypothesis from `AVD_ROOTCAUSE.md` (patch 0004 there) also makes
bug 2 go away, but it is not needed: with all experiments off and only
`INSN_FIFO_MASK=0` on, everything passes. Its rewritten form is kept in
`patches/optional/` (the original per-slice version breaks multi-slice frames).
The analysis' patches 0005 (traced DMA words) and 0006 (extra T8103 init
registers) made no difference on or off.

`cam2-10s.h264` (real IP-camera stream, 2304x1296 High, CABAC, 8x8, PPS scaling
lists, 150 frames): **bit-identical to software** in every path tried: gst
`v4l2slh264dec` (I420, md5 `cbc983fd…`), ffmpeg `-hwaccel v4l2request` inside
`frigate:latest` at full resolution (NV12 `c770995c…` and yuv420p `cbc983fd…`) and
with the Frigate-like `fps=5,scale=640:360` chain (`ee810b0f…` = software).

## Per-configuration clip matrix

Legend: OK = OK-identical (md5 == software), FAIL = FAIL(hw-timeout) (watchdog,
driver reset itself), DIFF = decoded but md5 differs, n/t = not tested in that
configuration. `es` = `h264_early_submit`.

| clip | baseline | 0001 | 0001+0002 | +0003diag +0004(es=1) | +0004 es=0 | 0001-0007 es=0 (0005/0006 off) | 0001-0003diag+0007 | **final** (0001-0005) |
|---|---|---|---|---|---|---|---|---|
| f_base_plain | OK | OK | OK | OK | n/t | n/t | OK | OK |
| f_high_8x8 | FAIL | OK | OK | OK | OK | OK | OK | OK |
| f_high_no8x8 | OK | OK | OK | OK | n/t | n/t | OK | OK |
| f_main_bframes | OK | OK | OK | OK | n/t | OK | OK | OK |
| f_main_cabac | OK | OK | OK | OK | n/t | n/t | OK | OK |
| f_main_plain | OK | OK | OK | OK | n/t | n/t | OK | OK |
| f_main_ref3 | OK | OK | OK | OK | n/t | n/t | OK | OK |
| f_main_weightp | OK | OK | OK | OK | n/t | n/t | OK | OK |
| g_1080_base | OK | OK | OK | OK | n/t | n/t | OK | OK |
| g_1080_high8x8 | FAIL | FAIL | FAIL | OK | n/t | n/t | OK | OK |
| g_1080_main | FAIL | FAIL | FAIL | OK | FAIL | OK | OK | OK |
| g_1296_base | FAIL | FAIL | FAIL | OK | FAIL | OK | OK | OK |
| g_1296_main | FAIL | FAIL | FAIL | OK | n/t | n/t | OK | OK |
| h_1296_base_lowbr | OK | OK | OK | OK | n/t | OK | OK | OK |
| h_360_high8x8_lowbr | FAIL | OK | OK | OK | n/t | n/t | OK | OK |
| cam2-10s (IP camera) | FAIL | FAIL | FAIL | OK (1) | n/t | n/t | OK | OK |
| t_1080p_high | FAIL | FAIL | FAIL | OK | FAIL | OK | OK | OK |
| t_1080p_main_cavlc | FAIL | FAIL | FAIL | OK | n/t | n/t | OK | OK |
| t_1280x720_base | OK | OK | OK | OK | n/t | n/t | OK | OK |
| t_1296p_main_cavlc | FAIL | FAIL | FAIL | OK | n/t | n/t | OK | OK |
| t_320x240_base | OK | OK | OK | OK | n/t | n/t | OK | OK |
| t_320x240_high | FAIL | OK | OK | OK | n/t | n/t | OK | OK |
| t_640x360_base | OK | OK | OK | OK | n/t | n/t | OK | OK |
| t_720p_high | FAIL | OK | OK | OK | n/t | n/t | OK | OK |
| t_960x540_base | OK | OK | OK | OK | n/t | n/t | OK | OK |
| w_360_main_weightp2 (new) | n/t | **DIFF** | OK | OK | n/t | n/t | OK | OK |
| m_360_base_slices4_lowbr (new) | OK | n/t | n/t | **FAIL (2)** | n/t | n/t | OK | OK |
| m_360_high_slices4 (new) | FAIL | n/t | n/t | FAIL (2) | n/t | n/t | OK | OK |
| m_1080_high_slices4 (new) | n/t | n/t | n/t | FAIL (3) | FAIL (3) | n/t | FAIL (3) | OK |
| m_1296_high_slices8 (new) | n/t | n/t | n/t | FAIL (3) | n/t | n/t | FAIL (3) | OK |

(1) cam2-10s showed `FAIL(killed)` in the first 0004 run only because the
harness' 25 s limit is shorter than gst's software videoconvert of 150 1296p
frames; the decode is bit-identical (md5 checked directly, and OK with TIMEOUT=90).
(2) the analysis' 0004 writes one decode command per slice (HEVC pattern); from
the second multi-slice frame on the PP-done IRQ never arrives
(`Frame processing timed out! Vp: 255`), `diag/multislice-irq-early1.txt`. The
one-command-per-frame rewrite (`patches/optional/`) passes, `diag/multislice-irq-variantA.txt`.
(3) pre-existing 1 ms poll limit in the held-slice path, fixed by final/0004.

The 0005/0006/0007 bisection (module `apple-avd-0001-0007.ko`, old 0004 inside,
clips g_1296_base / g_1080_main / t_1080p_high / f_high_8x8): every combination
of `h264_early_submit`, `inst_dma_words`, `t8103_extra_init` on/off passed, which
isolates 0007 (`INSN_FIFO_MASK=0`, no parameter) as the fix. Confirmed with a
build of only 0001+0002+0003diag+0007 (`apple-avd-0001-0003+0007.ko`): full matrix OK.

Cut clips (`src/avd-analysis/clips/`), final module: g_1080_base/g_1080_main/
g_1296_base/h_1296_base_lowbr/cam2-10s `_idr_only` and g_1080_main/g_1296_base/
cam2-10s `_2frames` all OK-identical (they all FAILed on 0001+0002 except the
two that already worked, so bug 2 is a first-frame failure, as predicted).

## What the VP register dump showed (final/0005, `diag/vp-status-at-timeout-0003.txt`)

At the watchdog, with 0001+0002 only (bug 2 present), VP2 status word = 0 (no
error), "slice bytes parsed" short of the slice size:

| clip | slice bytes | parsed | % |
|---|---|---|---|
| g_1080_main IDR (CABAC) | 290627 | 273060 | 94 |
| g_1296_base IDR (CAVLC) | 358341 | 303915 | 85 |
| cam2-10s IDR | 218230 | 202426 | 93 |
| t_1080p_high IDR | — | 269587 | — |

i.e. a mid-frame stall, not a parse error at MB 0 and not a memory-size limit.

## Real-stream and Frigate-like runs (final module, `frigate:latest`)

| run | md5 | time |
|---|---|---|
| gst `v4l2slh264dec ! fakesink`, cam2-10s 150 frames | — | 0.42-0.59 s wall (≈ 250-350 fps decode), 10 back-to-back runs all rc=0, dmesg silent |
| gst → I420 file | `cbc983fd…` = sw | 25 s (all software videoconvert) |
| `/opt/ffmpeg-avd/bin/ffmpeg -hwaccel v4l2request … -f rawvideo -pix_fmt yuv420p` | `cbc983fd…` = sw | 16.9 s (pipe-bound, sw takes 17.0 s) |
| same, `-pix_fmt nv12` | `c770995c…` = sw nv12 | |
| **Frigate-like** `-hwaccel v4l2request -vf fps=5,scale=640:360 -f rawvideo -pix_fmt yuv420p` | **`ee810b0f…` = sw** (also = sw via nv12 intermediate) | rtime 2.86 s, utime 3.09 s |
| software ffmpeg, same filters (default threads) | `ee810b0f…` | rtime 0.25 s, utime 0.70 s |
| software, `-threads 2` (Frigate's per-camera default) | | rtime 0.29 s, utime 0.52 s |

### Concurrency (4 simultaneous decodes of cam2-10s, Frigate-like filters, `conc_test.sh`)

| | wall for all 4 | per process (ffmpeg `-benchmark`) | md5 |
|---|---|---|---|
| hardware x4, run 1 | 3.72 s | rtime 2.98-3.02 s, utime 3.10-3.21 s | all 4 = `ee810b0f…` |
| hardware x4, run 2 | 3.67 s | rtime 2.77-3.06 s, utime 3.01-3.21 s | all 4 identical |
| software x4 | 1.31 s | rtime 0.53-0.63 s, utime 0.74-0.94 s | all 4 identical |

All four hardware decoders complete, produce bit-identical output and leave no
driver message; the single H.264 VP slot serialises them (per-process time is
the same as a single run plus little waiting).

### Read-back fix (2026-09-05, later the same day) — `final/0006` + ffmpeg patches

Numbers in the older sections are with the *uncached* read-back
(`apple-avd-fixed-v1.ko` + `/opt/ffmpeg-avd-v1`). With `apple-avd-fixed.ko` (0001-0006)
and `/opt/ffmpeg-avd` (Kwiboo b57fbbe + `patches/ffmpeg/0001-0002`), same chain,
same md5 `ee810b0f…`:

| run (cam2-10s, 150 frames, in `frigate:latest`) | utime | stime | rtime |
|---|---|---|---|
| hw Frigate-like chain, single | 0.34-0.42 s | 0.07-0.11 s | 0.59-0.61 s |
| hw x4 concurrent (`conc_test.sh hw 4`), per process | 0.36-0.38 s | 0.07-0.09 s | 2.0-2.2 s (VP-serialised), wall 2.71 s |
| hw `-f null -` (decode + read-back) | 0.07-0.09 s | 0.07-0.12 s | 0.58 s |
| hw decode only (`drm_prime`) | 0.013 s | 0.031 s | 0.60 s |
| sw chain `-threads 2` / 1 / default | 0.62 / 0.53 / 0.76 s | 0.02 / 0.01 / 0.04 s | 0.28 / 0.37 / 0.27 s |

Read bandwidth of one capture buffer (`readback/rb_bench`): 240-250 MB/s uncached
(any copy loop) → 18-24 GB/s cacheable. The AVD was also shown to be IO-coherent
on this T8103 (0 stale cache lines in 450 probed frames with `dev->dma_coherent`
forced on, `apple-avd-cohprobe.ko`), which is the basis of the RFC DT patch 0007;
that variant measures the same as 0006 (0.37-0.42 s user + 0.07-0.11 s sys) but
needs no client change. GStreamer matrix on the 0006 module: 25 OK-identical,
1 SKIP, 0 FAIL (`readback/test_clips-cachehints.log`). Full write-up:
`~/frigate/research/AVD_READBACK.md`.

### Where the hardware path's CPU time went before the read-back fix (historical)

| run (cam2-10s, `-f null -`) | utime | rtime |
|---|---|---|
| hw, decoded frames copied to system memory (what `-hwaccel v4l2request` does by default) | 2.78 s | 2.86 s |
| hw, frames kept as `drm_prime` (no read-back) | **0.02 s** | 0.56 s |
| sw, 8 threads | 0.60 s | 0.19 s |
| sw, 1 thread | 0.35 s | 0.35 s |
| 640x360 clip (sub-stream size), hw Frigate-like / sw | 0.24 s / 0.09 s | 0.24 s / 0.30 s |

The decoder itself is essentially free (0.02 s CPU for 150 frames of 1296p) and
fast (270 fps). The 3 s of CPU in the Frigate-like hardware run was ffmpeg
reading the decoded NV12 frames back out of the uncached dma-buf capture buffers
(~4.5 MB per frame at roughly 240 MB/s, ≈ 18 ms per frame, before the `fps=5`
filter drops 4 of 5 frames). On this (static) camera clip that is 4-5x more CPU
than the software decoder needs. This was fixed the same day with cacheable
capture buffers (`final/0006` + the ffmpeg patches, table above): the hardware
path now costs ~60 % of the software decoder's CPU on this chain, and the
remaining CPU is swscale, not the read-back.

## Stability

- ~60 module load/unload cycles during the session (reload.sh, insmod with
  parameters, restore.sh), 3 explicit rmmod/insmod/decode cycles on the final
  module: every unload succeeded immediately, `/dev/video0` always came back.
- No oops, WARNING, hung task or "no handler for IRQ" in dmesg at any time.
- Failing configurations always recovered through the driver's own 2 s watchdog
  reset; the `frigate` container was untouched and kept running throughout.
- System left on the in-tree Fedora module (`restore.sh`), taint empty.
- Read-back work: ~10 more load/unload cycles (cachehints, cohprobe x2, dmacoherent,
  fixed), one graceful `WARN_ON_ONCE` in `dma_alloc_noncontiguous()` from the first
  `allow_cache_hints` attempt (REQBUFS returned ENOMEM, nothing else), no other
  kernel message; the `frigate` container was untouched throughout.
