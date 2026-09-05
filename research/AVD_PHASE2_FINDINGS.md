# AVD on kernel 7.1.6 — first hardware results (2026-09-05, after the reboot)

Host booted `7.1.6-400.asahi.fc44.aarch64+16k`. The in-tree driver probed
(`/dev/video0` = card `avd`, `/dev/media0`), the self-built firmware
`/lib/firmware/apple/avd-fw-v2-t0.bin` loaded (no probe failure), formats
advertised: S264 up to 4096x4096, S265 up to 16384x16384, VP9F; capture NV12.
the login user is not in group `video`, so host tests ran under `sudo`; inside the
Frigate image `--device /dev/video0 --device /dev/media0` works.

## Result: the decoder works, but not on our camera streams

Tested with GStreamer `v4l2slh264dec` (host) and the custom ffmpeg
`-hwaccel v4l2request` (inside `frigate:latest`), comparing raw NV12/I420
output byte-for-byte with software decode. Clips are in this directory
(`f_*.h264`, `g_*.h264`, `t_*.h264`, `h_*.h264`, 2–3 s each, x264 from the
real `cam2-10s.h264`).

| Clip (x264 unless noted) | Result |
|---|---|
| `test_baseline.h264` 320x240 CBP (avd-work's clip) | OK, identical |
| Baseline 320x240 / 640x360 / 960x540 / 1280x720 / 1920x1080 | OK, identical |
| Main 640x360 + CABAC, + B-frames, + weightp, + ref=3 (each separately) | OK, identical |
| High 640x360 with `8x8dct=0` | OK, identical |
| **High with `8x8dct=1`** — 320x240, 640x360 (even at crf 40), 720p, 1080p | **FAIL** |
| Main 1920x1080 CABAC + B-frames | **FAIL** |
| Baseline 2304x1296 crf 23 (max packet 359 KB) | **FAIL** |
| Baseline 2304x1296 crf 40 (max packet 97 KB) | OK, identical |
| `cam2-10s.h264` (the real IP-camera stream: High, 2304x1296, 8x8) | **FAIL** |

Failure signature is always the same, on the first frame:
```
avd 269080000.avd: H2 00 error
avd 269080000.avd: Frame processing timed out! Vp: 2 (00)
```
repeating every ~2 s until the client is killed; GStreamer reports
"Request N took too long" then "Not enough memory to decode H264 stream";
ffmpeg hangs (no software fallback once the hwaccel opened). The driver
recovers as soon as the process exits — no reboot needed, Frigate (CPU
decode, GPU detection) was unaffected throughout.

Two separate problems, then:
1. **`transform_8x8_mode_flag` (High profile) is broken** regardless of size
   or bitrate. Every cam1 camera here streams High profile with 8x8, so this
   alone blocks Frigate use.
2. **Something size/complexity related above ~1080p** (1296p works only at
   low bitrate; 1080p Main with CABAC+B fails while 1080p Baseline at a larger
   packet size works). Not pinned down; looks like a buffer/DPB sizing issue
   rather than a pure bitstream-size cap.

## What this means for Frigate

Hardware decode of the cameras is **not usable yet** on this driver revision
(7.1.6, driver first commit 2026-06-30). Frigate stays on CPU decode (see
README status). Options, cheapest first:
- **Report upstream** (AsahiLinux/linux issues, driver author sofus) with
  the clips here: `f_high_8x8.h264` vs `f_high_no8x8.h264` is a one-flag
  reproducer for (1); `g_1080_main.h264` vs `g_1080_base.h264` for (2).
- Re-test on each new `kernel-16k` (module is in-tree, no rebuild):
  `sudo gst-launch-1.0 filesrc location=~/frigate/research/cam2-10s.h264 ! h264parse ! v4l2slh264dec ! fakesink`
  and watch `sudo dmesg -w`.
- Work around (1) camera-side only if the cam1 firmware allows disabling
  8x8/High profile (unlikely) — then (2) still applies at 2304x1296 unless
  detect is fed from the sub-stream. Feeding `detect` from the cameras'
  sub-streams (usually 640x360 High, still 8x8) does not avoid (1).
- Don't sink time into the driver ourselves unless upstream is unresponsive;
  the hardware knowledge in `~/Projects/avd-work/docs/` would be the
  starting point.

Re-run recipe (all bounded, safe to run while Frigate is up):
```bash
cd ~/frigate/research
sudo timeout -s KILL 25 gst-launch-1.0 -q filesrc location=f_high_8x8.h264 ! h264parse ! v4l2slh264dec ! fakesink; sudo dmesg | tail -3
```

## Update, later the same day — both problems fixed in the driver

See `research/AVD_ROOTCAUSE.md` and `src/avd-driver/RESULTS.md`. Firmware was
not at fault. Patch set in `src/avd-driver/patches/final/`:
1. 8x8 transform flag was masked to 0 (`AVD_HDR_COMMON_LUMA_TBS(pps->flags & FLAG)` into a 2-bit field) — upstream 8e8d72a673ab (asahi-7.1.8).
2. P-slice weighted-prediction denominators — upstream 3cba4f1d5b86.
3. Large/high-bitrate/CABAC stalls: T8103 `VP_INSN_FIFO_MASK` 0x100000 → 0 — upstream ca9a850f237f (asahi-7.1.12).
4. Multi-slice frames with slices > ~16 KiB: scale the held-slice poll timeout with slice size — local, not upstream.
Result: the whole clip matrix plus `cam2-10s.h264` decode bit-identically; 4 concurrent decodes OK; ~270 fps single-stream at 2304x1296.
Caveat: ffmpeg's read-back from the uncached NV12 capture buffers costs ~18 ms/frame at 1296p, more CPU than software decode, so Frigate stays on CPU until that is addressed.
