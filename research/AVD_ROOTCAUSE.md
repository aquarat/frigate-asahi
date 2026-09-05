# Apple AVD H.264 decode failures on T8103 (kernel 7.1.6) — root-cause analysis

Analysis date: 2026-09-05. Companion to `AVD_PHASE2_FINDINGS.md`. Working tree with all
sources, tools, cut clips, patches and out-of-tree builds:
`~/frigate/src/avd-analysis/`. Nothing was loaded into the running kernel;
patched drivers were only compiled (against `kernel-16k-devel-7.1.6-400`).

## Executive summary

Two independent bugs, both in the in-tree driver `drivers/media/platform/apple/avd/`
(not in the replacement CM3 firmware, not in the hardware):

1. **`transform_8x8_mode_flag` is silently dropped (CONFIRMED, upstream already fixed).**
   `avd-h264.c:219` feeds the raw V4L2 flag value `0x40` into
   `AVD_HDR_COMMON_LUMA_TBS()` = `FIELD_PREP(GENMASK(8,7), v)` (`avd-inst.h:124`).
   `(0x40 << 7) & 0x180 == 0`, and `FIELD_PREP` only validates compile-time constants
   (`include/linux/bitfield.h:70-73`), so bit 7 of `hdr_2c_sps_param` is never set. eiln's
   hardware-verified HAL sets `boolify(pps.transform_8x8_mode_flag) << 7`
   (`eiln/avd avid/h264/halv3.py:155`). The VP parses 8x8-transform slices as 4x4 and raises
   `DECODE_STATUS_ERR` on the first frame. Fixed upstream by commit `8e8d72a673ab`
   (2026-08-08, first in tag `asahi-7.1.8-1`; **not** in `asahi-7.1.6-1`, whose last driver
   commit is `0d6dcb9b11b4`). Patch `0001` is that fix. Every cam1 stream (main and sub)
   needs it.

2. **Large / high-bitrate / CABAC frames fail (mechanism identified with medium confidence;
   not fixed upstream, including `asahi-7.1.12-1`).** Nothing in the driver's memory sizing
   scales with bitrate (all per-frame buffers are ≥ eiln's, see C), and the instruction
   stream for an IDR frame is word-for-word equal to eiln's except for (a) three
   "DMA config" words pushed as 0 and (b) an ordering difference in the *submission
   protocol*: the driver writes the decode/submit command (`ctrl+0x4014`) only from the IRQ
   handler after the VP reports done (`avd-drv.c:201-206`, "kick the pp and hope for the
   best"), whereas eiln's T8103 flow and the emulated Apple firmware write it immediately
   after the last instruction word (`m1n1 fw/avd/decoder.py:184`, `avd_emu.py w_40104014`).
   Serialising the two pipeline stages means the VP must buffer its entire residual output
   on-chip until the PP is started; frames whose output exceeds that buffering stall and
   the VP raises `DECODE_STATUS_ERR`. This is the only candidate consistent with all
   observations (2304x1296 CAVLC: 358 KB IDR fails / 97 KB works; 1080p CAVLC 296 KB works;
   1080p CABAC 290 KB fails because CABAC packs more coefficients per byte). Patch `0004`
   restores the traced order (module parameter to revert); patches `0005`/`0006` are cheap
   secondary experiments (traced DMA config words, T8103 init registers eiln writes and the
   kernel never does); patch `0003` adds the VP register dump needed to locate the failure
   point if `0004` is not sufficient.

Also found on the way: the P-slice weighted-prediction encoding in 7.1.6 is wrong for x264's
default `weighted_pred=1, weighted_bipred_idc=2` streams (upstream fix `3cba4f1d5b86`,
patch `0002`) and the real IP-camera stream carries PPS scaling lists (verified the driver's
list ordering matches eiln's AVD scan tables exactly, so no change needed there).

Upstream status: `asahi-7.1.8-1`…`7.1.12-1` contain the 8x8 fix; `7.1.12-1` additionally has
the weights fix, tighter buffer sizing, the compressed-reference ("comp") layout rewrite and a
T8103 `INSN_FIFO_MASK` fixup by Janne Grunau. None of the tags change the submit ordering.
Fedora's `kernel-16k` copr currently offers up to 7.1.6 only.

## Sources analysed (exact revisions)

| Source | Revision | Local path |
|---|---|---|
| AsahiLinux/linux, driver at the running kernel's tag `asahi-7.1.6-1` | tag commit `e2e1930a9595`; last driver commit `0d6dcb9b11b4` (2026-08-02) | `src/avd-analysis/drv-asahi-7.1.6-1/` (all line numbers below refer to this) |
| AsahiLinux/linux `asahi-7.1.12-1` driver (newest) | last driver commit `ca9a850f237f` (2026-08-30) | `src/avd-analysis/drv-asahi-7.1.12-1/`, sparse clone `src/avd-analysis/linux-7.1.12/` |
| all 18 post-7.1.6 driver commits, diffs + messages | | `src/avd-analysis/fixdiffs/`, `commits-7.1.12.txt` |
| AsahiLinux/avd-fw (replacement CM3 firmware, `v2-t0` = T8103) | `5e34aca` | `src/avd-fw/` |
| eiln/avd (Python decoder + emulator, T8103 = eiln's "V3") | HEAD 2026-09-05 | `src/avd-analysis/eiln-avd/` |
| eiln m1n1 `avd-take0` branch (hardware submission flow) | `fc37d41` | `~/Projects/avd-work/m1n1-avd` |
| GStreamer `gstv4l2codech264dec.c`, `gsth264parser.c` (main) | | `src/avd-analysis/` |
| Installed module | `apple-avd.ko.xz` built 2026-08-05, contains `avd-av1.c` (so ≥ `0d6dcb9b11b4`, matches tag) | `/lib/modules/7.1.6-400.asahi.fc44.aarch64+16k/kernel/drivers/media/platform/apple/avd/` |

The `asahi` branch on GitHub lags the tags (tip `adec89957b42`, 2026-08-08); the fixes live in
`asahi-wip` and the `asahi-7.1.x-1` tags.

## A. V4L2 → hardware translation, `TRANSFORM_8X8_MODE`, scaling matrices

**Where the translation happens.** `avd-h264.c` builds a 32-bit instruction stream by
writing words directly into the VP's FIFO register (`push()` → `avd-inst.h:195` →
`writel(inst, ctrl + 0x4004 + vp_slot*4)`):

- `stream_hdr()` `avd-h264.c:179-281` — per-frame header (`hdr_34`…`hdr_54`), tile/RVRA
  addresses, then `stream_refs()` (`:97-135`) for non-IDR and `stream_scaling()` (`:137-177`)
  if `V4L2_H264_PPS_FLAG_SCALING_MATRIX_PRESENT` (`:277-280`).
- `stream_slice()` `:364-481` — per-slice words (coded data pointer, QP, deblocking, ref
  lists, `stream_weights()` `:285-362`, MB dims, `slc_6e4` ref type, FIFO end).
- Controls are fetched in `avd_h264_run_preamble()` `:612-656`; `avd_h264_run()` `:658-726`.

**The 8x8 bug.** `avd-h264.c:214-221`:

```c
push(AVD_HDR_COMMON_CHROMA_FORMAT(sps->chroma_format_idc)
    | AVD_HDR_COMMON_BIT_DEPTH_L(sps->bit_depth_luma_minus8)
    | AVD_HDR_COMMON_BIT_DEPTH_C(sps->bit_depth_chroma_minus8)
    | AVD_HDR_COMMON_MIN_LUMA_CBS(1)
    | AVD_HDR_COMMON_LUMA_CBS(1)
    | AVD_HDR_COMMON_LUMA_TBS(pps->flags & V4L2_H264_PPS_FLAG_TRANSFORM_8X8_MODE)   /* :219 */
    | AVD_HDR_COMMON_FLAG0(sps->flags & V4L2_H264_SPS_FLAG_DIRECT_8X8_INFERENCE),
    "hdr_2c_sps_param");
```

`V4L2_H264_PPS_FLAG_TRANSFORM_8X8_MODE == 0x0040` (`include/uapi/linux/v4l2-controls.h:1451`).
`AVD_HDR_COMMON_LUMA_TBS(v)` is `FIELD_PREP(GENMASK(8, 7), v)` (`avd-inst.h:124`) =
`((0x40) << 7) & 0x180 = 0`. The neighbouring macros all use `!!(v)`; this one (shared with
HEVC where it carries a 2-bit size) does not. Result for every 8x8 stream:
`hdr_2c = 0x01002801` instead of eiln's `0x01002881` (`halv3.py:151-157`: `0x2800 |
boolify(transform_8x8) << 7 | direct_8x8_inference`). The rest of the word is identical to
eiln's for 8-bit 4:2:0. (Side note: `BIT_DEPTH_L`/`_C` shifts are swapped relative to eiln
— `avd-inst.h:128-129` vs `halv3.py:152-153` — harmless for 8-bit; the author's TODO at
`avd-h264.c:213` already suspects this.)

Upstream fix `8e8d72a673ab` ("fixup! media: apple: avd: define static values and bitmasks",
`fixdiffs/8e8d72a673ab….diff`): adds `H264_TRANSFORM_8X8_MODE(v) FIELD_PREP(BIT(7), !!(v))`
and uses it at that line. Patch `0001` below is the same change against 7.1.6-1.

**Scaling matrices.** `stream_scaling()` pushes `hdr_4c_pic_scaling_list_dims =
0x100027f` (identical to eiln's `halv3.py:88`), then the six 4x4 lists in raster order,
then either the two 8x8 luma lists remapped through `map[]` (`avd-h264.c:154-169`) or the
built-in default 8x8 lists when 8x8 is off (`:171-176`). Checks performed:

- GStreamer converts the parser's zig-zag lists to raster (`gstv4l2codech264dec.c:610-629`,
  `gst_h264_quant_matrix_8x8_get_raster_from_zigzag`) and its parser applies the fall-back
  rules (`gsth264parser.c:593-629`), so the V4L2 control holds fully-resolved raster lists.
- eiln's C parser writes coefficients directly into AVD order using its own scan table
  `h264_avd_zigzag_scan8x8` (`eiln-avd/codecs/h264.c:47-64`) and pushes them sequentially.
  I computed both orderings: the kernel's `map[]` applied to V4L2 raster order gives the
  *same* 64-entry sequence as eiln's AVD scan, and the 4x4 lists match too
  (`tools` check in this session: "8x8 … True", "4x4 … True"). The built-in default tables
  (`avd-h264.c:76-85`) are byte-identical to eiln's `halv3.py:114-125` and are also in the
  4-subblock layout, corroborating the layout.
- 8x8 list order: V4L2 `[0]=Intra Y, [1]=Inter Y`; eiln pushes its `scaling_matrix8[0]`
  then `[3]` = Intra Y, Inter Y. Same.
- No scaling matrix (x264 default, all `f_*`/`g_*` clips): both push a single 0 word
  (`avd-h264.c:280`, `halv3.py:52`).

So `stream_scaling()` is correct for the real IP-camera stream, which — unlike the x264 test
clips — **does** carry PPS scaling lists (`cam2-10s.h264`: `pic_scaling_matrix_present=1`,
lists present `[1 0 0 1 0 0 1 1]`, i.e. 4x4 Intra-Y/Inter-Y and both 8x8 luma lists, see
`src/avd-analysis/clip-analysis.txt`). Remaining minor divergence: for a PPS with
`transform_8x8=1` whose 8x8 lists are absent (fall-back), eiln pushes nothing for 8x8
(`halv3.py:102-104`) while the kernel pushes the resolved lists; untested by any clip here.

Other word-level differences vs eiln found while comparing (none first-frame relevant, listed
for completeness): `AVD_OP_SL_REF_FLAG2` (bit 15, "temporal direct") is set on every I and P
slice because GStreamer's `direct_spatial_mv_pred_flag` is 0 outside B slices
(`avd-h264.c:451`; eiln only sets it for B, `halv3.py:295-298`) — evidently tolerated;
`AVD_OP_DBLK_OFF1` is `GENMASK(16,12)` and overlaps `AVD_OP_DBLK_FLAG_EN` `BIT(16)`
(`avd-inst.h:89-90`), so a negative `slice_beta_offset_div2` leaks into the enable bit;
the emulation-prevention scan in `stream_slice()` (`:381-389`) mis-points the slice start if
an EPB sits exactly at the first slice-data byte (rare).

## B. What "H2 00 error" means

`avd-drv.c:209`: `dev_err(avd->dev, "H%d %02d error", status, ctx->fifo_idx)` — `status` is
the raw word read from the CM3 mailbox (`AVD_REG_MBOX1_RETRIEVE`, `:186`), `00` is the
instruction-FIFO index. The replacement firmware is an IRQ trampoline only (`avd-fw/src/irq.c`,
404 lines total, no decode logic): each VP has UNK/ERR/DONE IRQ lines; for T8103 (`AVD_VER 2`)
IRQ 29 → `err(2)` (`irq.c:124`), which clears `DECODE_STATUS_ERR` in the packed status
register `ctrl+0x4060` (`irq.c:62-73`, `reg_v2.h:15-19`, 5 bits per VP) and posts the bare VP
index `n` to the mailbox (`irq.c:81-85`). `vpdone()` posts `0x100|n` (`:78`), the PP done
handler posts `0x1000` (`:90`). So:

- `H2` = VP **2** raised `DECODE_STATUS_ERR`. On T8103 the driver assigns slots HEVC 0-1,
  H.264 **2**, VP9 3 (`avd-drv.c:435-440`, `alloc_slots()` `:60-86`), matching eiln's
  H.264 FIFO register `0x110400c` = `0x4004 + 2*4`.
- The handler then does nothing (`:210-211`) and the 2 s watchdog (`:699`, `:150-174`)
  prints "Frame processing timed out! Vp: 2 (00)", frees the slots, resets the block via
  `reset_control_reset()` + firmware reboot and fails the buffer. That is why recovery is
  clean and why every frame after the first repeats the pattern every ~2 s.

The firmware path is the same for any error cause (parse error, DMA fault, stall). Patch
`0003` makes the driver dump the VP registers (`avd_status()`, `avd-hw.c:120-148`: MB
parsed, slice bytes parsed, status) at that point, which separates "died at MB 0"
(bug 1) from "stalled mid-frame" (bug 2).

## C. Buffer sizing vs eiln, and the size-dependent failure

### C.1 What the driver allocates (7.1.6-1) vs eiln

| Buffer | Driver | eiln (`avid/h264/decoder.py`, `avid/decoder.py`) | Verdict |
|---|---|---|---|
| Bitstream (OUTPUT) | userspace `sizeimage`, default `width*height` (`avd-v4l2.c:602`), H.264 `SLICE_BASED` decode mode (`:252-255`) | `slice_data_size ≈ w*h/2`, grown on demand | 2304x1296 → 2.85 MB; 359 KB packets fit easily |
| Instruction FIFO | 12 MiB (`fifo_size()`, `avd-inst.h:146`) | 7 × 1 MiB | fine; a frame is ~100 words |
| `pps_tile[0..4]` line/entropy buffers | `avd_h264_alloc_bufs()` `avd-h264.c:507-521`: 0x20000, 32·mb, 128·mb (or 0xc000 if bpl>2048), 32·mb, 32·mb; all `max(…,0x8000)` | 0x8000, 32·mb, 128·mb (0xc000 if >2048), 32·mb, 32·mb; all `max(…,0x8000)` | equal or larger |
| RVRA (compressed reference) | `fill_rvra()` `avd-drv.c:24-57`, appended to each CAPTURE buffer (`avd-v4l2.c:81`) | `calc_rvra()` | driver adds extra height-based padding; larger. 7.1.12 (`139a0062983b`) rewrites this as a tiled "comp" layout that is *smaller* than 7.1.6's, confirming 7.1.6 is not under-sized |
| SPS tile (per-frame MV/colocated) | `sps_size()` `avd-h264.c:88-92`, appended after RVRA (`:754`, `:655`) | same formula | equal (7.1.12 shrinks it) |
| DPB refs | addresses recomputed from the reference CAPTURE buffers (`:117-125`) | pool of `max_dpb_frames+2` | fine |
| Coded size / frame dims | `v4l2_apply_frmsize_constraints` with `step_height = 16` (`avd-v4l2.c:413`) uses `clamp_roundup`, so 1296 stays 1296 | | no rounding issue |

Conclusion: **no buffer in the driver depends on bitrate**, none is smaller than eiln's, and
address alignment (`>>7`/`>>8` for the LSR quirk) holds for every tested geometry
(2304·1312·1.25 = 0x39A800 etc.). Per-frame slice count is 1 in every clip
(`clip-analysis.txt`), so the multi-slice path (`HOLD_CAPTURE_BUF`, 1 ms parse poll at
`avd-h264.c:710-712`) is not involved.

### C.2 What actually differs from the hardware-verified flow

Comparing the T8103 IDR instruction stream word by word (`stream_hdr`/`stream_slice` vs
`halv3.py set_header`/`set_slice`), the only differences are:

1. `INST_DMA1/2/3` pushed as **0** (`avd-inst.h:11-13`; used at `avd-h264.c:104-111`,
   `:239-240`, `:247-248`, `:258`). eiln pushes `0x04020002 / 0x00020002 / 0x00070007`
   (`halv3.py:18-25`, `:169-181`). eiln's m1n1 shows the same words in the Apple firmware's
   per-codec SRAM table (`m1n1 fw/avd/__init__.py:121-127`), so they are real firmware output.
   sofus later removed the macros with "I feel fairly confident they dont do anything"
   (`04c5bab79f33`), tested on newer silicon (comment value there is `0x14<<16|0x14`).
2. **Submit ordering.** eiln: push words → `w32(0x1104014, 0x2b000100 | fifo<<4 | 7)` →
   poll (`m1n1 fw/avd/decoder.py:171-202`); emulator records the firmware's `0x4014` write
   in-line with the instruction words (`avd_emu.py:445-462`). Driver: `avd_h264_submit()`
   (`avd-h264.c:767-774`, identical value) is called **only** from the VP-done branch of the
   IRQ handler (`avd-drv.c:201-206`). The VP demonstrably starts parsing without the submit
   (the driver polls its byte counter before any submit, `:710`), so the submit starts the
   downstream stage; the VP and PP are serialised instead of pipelined.
3. Init registers eiln writes once and the kernel never writes: `ctrl+0x40f4 = 0x1555`,
   `ctrl+0x4110 = 0` (`m1n1 fw/avd/__init__.py`, end of `avd_dma_tunables_stage0`;
   avd-work's own `kboot_avd.c:465` also writes 0x1555). Purpose unknown.

Why (2) fits the data: with the PP idle, the VP's residual/coefficient output has to sit in
on-chip buffering. Output volume grows with coefficients, not bytes: CABAC codes more
coefficients per byte than CAVLC, so a 290 KB CABAC IDR overflows where a 296 KB CAVLC IDR
does not; 2304x1296 at 358 KB overflows, at 97 KB it does not; 640x360 CABAC (52 KB) is
fine. The firmware's tunables even carve fixed on-chip "BUF" windows per client
(`tunables_v2.h:0xc044…0xcc44`), consistent with bounded internal buffering. (1) could
also matter (zeros might mean minimal DMA burst/outstanding-request settings) but cannot by
itself explain a *stall*; it is cheap to include. I could not find any evidence that the
hardware writes stage-1 output to memory, so a memory-size fix is not expected to help.

Not the cause (checked and dismissed): OUTPUT `sizeimage`; `dst_len` arithmetic for RVRA/SPS
placement (16 KiB pages, page-aligned lengths); DART IOVA width (32-bit aperture, `>>8`
fits); `readl_poll_timeout` (single slice); watchdog (2 s ≫ decode time); GStreamer's
"Not enough memory" message (it is the *allocator* failing after buffers are stuck, not a
size check); the `frmsize` rounding.

**Experiments to pin it down (all safe, host only):**
`src/avd-analysis/clips/*_idr_only.h264` and `*_2frames.h264` (SPS+PPS+first AU, first two
AUs) for `g_1296_base`, `g_1080_main`, `g_1080_base`, `h_1296_base_lowbr`, `cam2-10s`.
If `g_1296_base_idr_only` fails on the unpatched driver the problem is in the first frame
(expected); with patch `0003` the dmesg dump shows "mb parsed"/"slice bytes parsed" at the
error — a mid-frame count supports the stall theory, `~0` would point back at the stream
words.

## D. `AVD_QUIRK_NO_PIPE_STATE` and the single H.264 VP slot

`avd_t8103_variant` (`avd-drv.c:435-453`): `quirks = AVD_QUIRK_LSR | AVD_QUIRK_NO_PIPE_STATE`,
`vp_slots[H264] = 1`, `fifo_slots = 7`, `revision = 3`. `NO_PIPE_STATE` removes the 0x200-byte
`pipe_state` buffer and the `PIPE_STATE` bits in `hdr_34`/`hdr_58` (`avd-h264.c:200`, `:236`,
`:252-253`, `:496-502`) — exactly matching eiln's T8103 stream (`hdr_58 = 0x30000a`,
`hdr_34` bit 19 clear), so the quirk itself is correct. Its relevance is indirect: newer
revisions have a pipe-state save area that plausibly lets the VP park its state while the PP
is not running; T8103 has none, which is why the serialised submit order (C.2 item 2) can hurt
here while working for the driver author on newer hardware (all his "size buffers correctly"
work was done on rev-4 parts; T6000 shares the quirk and, per `avd-work/docs`, has not
decoded anything under this driver yet).

The single VP slot means every H.264 context uses VP 2 and the error is always reported as
"H2"; it also means a stuck VP blocks all H.264 clients until the watchdog resets. The
`is_new_frame()` hack (`avd-h264.c:72`, `first_mb_in_slice == 0`) and the slot handling are not
implicated in the first-frame failures.

`INSN_FIFO_MASK`: T8103 writes `0x100000` (`avd-hw.c:81`, same as eiln's
`m1n1 fw/avd/decoder.py:34`); Janne changed it to 0 in `ca9a850f237f` (7.1.12) without
description. No mechanism links it to the symptoms, but it is the one T8103-specific
hardware-setup change made upstream since 7.1.6, so patch `0007` carries it.

## E. Firmware

`avd-fw` for `v2-t0`: `_start()` applies 111 tunables (`tunables_v2.h`), enables all NVIC
lines, writes `CM3_BOOT`, then `wfi`; IRQ handlers only clear a status bit and post a mailbox
word (`irq.c`). Findings:

- The tunable list is **identical** to eiln's `avd_dma_tunables_stage0` (111/111 offsets and
  values match; checked programmatically). Nothing is missing on the firmware side.
- The firmware never sees the instruction stream, buffer sizes or PPS flags; it cannot limit
  sizes or ignore 8x8. No firmware change is needed for either bug.
- The only extra writes in eiln's bring-up that neither the firmware nor the kernel performs
  are host-side (`ctrl+0x40f4=0x1555`, `ctrl+0x4110=0`, plus PIODMA/wrap registers that are
  irrelevant without Apple's firmware). Patch `0006` adds the two `ctrl` writes from the
  kernel; they could equally be added to `apply_tunables()` as
  `{0x40f4, 0xffffffff, 0x1555}, {0x4110, 0xffffffff, 0}` if a firmware-side placement is
  preferred (the firmware's `DECODE_CTRL_BASE` for v2 is the kernel's `ctrl` region).
- The unhandled-IRQ path (`0x10000|irq`, "no handler for IRQ") never fired, so the VP did not
  raise the 4th/5th status lines (e.g. FULL); the kernel masks those anyway
  (`AVD_VP_CM3_MASK`, `avd-hw.c:40`, `:86-87`).

## Clip facts used above

From `src/avd-analysis/tools/nalinfo.py` (full table in `clip-analysis.txt`):

| Clip | Geometry / profile | cabac | 8x8 | scaling | wp/bipred | GOP | first IDR bytes | Phase-2 result |
|---|---|---|---|---|---|---|---|---|
| f_high_no8x8 | 640x368 Baseline | 0 | 0 | – | 0/0 | IPPP | 53020 | OK |
| f_high_8x8 | 640x368 High | 0 | **1** | – | 0/0 | IPPP | 53000 | FAIL |
| h_360_high8x8_lowbr | 640x368 High | 1 | **1** | – | 1/2 | IPPPB | 10655 | FAIL |
| t_320x240_high | 320x240 High | 1 | **1** | – | 1/2 | IPPPB | 19454 | FAIL |
| g_1080_base | 1920x1088 Baseline | 0 | 0 | – | 0/0 | IPPP | 295576 | OK |
| g_1080_main | 1920x1088 Main | 1 | 0 | – | 1/2 | IPBB | 290627 | FAIL |
| g_1296_base | 2304x1296 Baseline | 0 | 0 | – | 0/0 | IPPP | 358341 | FAIL |
| h_1296_base_lowbr | 2304x1296 Baseline | 0 | 0 | – | 0/0 | IPPP | 96633 | OK |
| cam2-10s (IP camera) | 2304x1296 High | 1 | **1** | PPS `[10010011]` | 0/0 | IPPP, 1 ref | 218230 | FAIL |

Note: the `f_main_*` "feature" clips are 640x368 and mostly P-frames; they do not exercise
the P-slice weight bug (`f_main_weightp` has `weighted_bipred_idc=0`, so its encoding is
correct) and never hit the size regime. "CABAC, B-frames, weighted pred, multi-ref decode
byte-identically" therefore only holds at small sizes.

## Proposed patches (against `asahi-7.1.6-1`, `drivers/media/platform/apple/avd/`)

All seven apply in sequence with `patch -p1` and the result compiles cleanly against the
installed `kernel-16k-devel-7.1.6-400` (out-of-tree, `src/avd-analysis/verify/…/avd/apple-avd.ko`;
only pre-existing warning is in `avd-av1.c`). Files: `src/avd-analysis/patches/000N-*.patch`
(`series` lists the order); also embedded in the appendix. Each experiment patch has a module
parameter so it can be disabled at load time without rebuilding.

| # | Patch | Confidence | Origin | Clips expected to flip FAIL → OK |
|---|---|---|---|---|
| 0001 | h264: fix `transform_8x8_mode_flag` bit | **High** — the arithmetic is unambiguous, eiln sets the bit, upstream fixed the same line | backport `8e8d72a673ab` (7.1.8) | `f_high_8x8`, `h_360_high8x8_lowbr`, `t_320x240_high` (small 8x8 streams). 720p/1080p/1296p 8x8 clips and `cam2-10s` additionally need bug 2 fixed |
| 0002 | h264: fix default weights for P slices | High for correctness (matches eiln), not related to the error | backport `3cba4f1d5b86` (7.1.12) | P frames of `g_1080_main`, `g_1296_main`, `t_*_high` become bit-exact once they decode at all |
| 0003 | dump VP status on error/timeout | n/a (diagnostic) | new | none; prints MB/byte counters at the error |
| 0004 | h264: write decode command right after the instruction stream | **Medium** — matches the traced firmware/eiln order and explains every data point, but untested here; IRQ-handler interaction rewritten conservatively | new (`avd.h264_early_submit=0` reverts) | `g_1296_base`, `g_1080_main` (with 0002), `t_1296p_main_cavlc`, `t_1080p_high` and `cam2-10s` (with 0001) |
| 0005 | t8103: push the traced DMA config words | Low-medium — zero risk (byte-identical to eiln's stream), unclear effect | new (`avd.inst_dma_words=0` reverts) | same set as 0004 if 0004 alone is insufficient |
| 0006 | t8103: write `ctrl+0x40f4=0x1555`, `ctrl+0x4110=0` after CM3 boot | Low — copied from eiln's init, purpose unknown | new (`avd.t8103_extra_init=0` reverts) | same |
| 0007 | t8103: `INSN_FIFO_MASK = 0` | Unknown — parity with 7.1.12 | backport `ca9a850f237f` | none predicted |

Suggested test order (each run bounded, host, `sudo`, watch `dmesg -w`):

```bash
cd ~/frigate/src/avd-analysis/verify/drivers/media/platform/apple/avd   # built .ko with 0001-0007
# (loading/replacing the in-tree module is a deliberate user step, not done here:
#  sudo rmmod apple_avd && sudo insmod ./apple-avd.ko [h264_early_submit=0] [inst_dma_words=0] [t8103_extra_init=0])
T=~/frigate/research; C=~/frigate/src/avd-analysis/clips
for f in $T/f_high_8x8.h264 $T/h_360_high8x8_lowbr.h264 \
         $C/g_1296_base_idr_only.h264 $T/g_1296_base.h264 $T/g_1080_main.h264 $T/cam2-10s.h264; do
  echo "== $f"; sudo timeout -s KILL 25 gst-launch-1.0 -q filesrc location=$f ! h264parse ! v4l2slh264dec ! fakesink; sudo dmesg | tail -4
done
```

Bisecting bug 2: if everything passes, turn the experiments off one at a time
(`h264_early_submit=0`, then `inst_dma_words=0`, `t8103_extra_init=0`) to learn which one
mattered; if `g_1296_base` still fails with all on, the `0003` dump (`VP2: … mb parsed …
slice bytes parsed …`) tells whether the VP stalls mid-frame (then look at the submit
protocol further, e.g. also writing the command *before* the slice words) or dies at the
start (then diff the header words against eiln with `DEBUG_INST`, `avd-inst.h:187-193`).

For Frigate specifically: with `0001` alone, the cameras' 640x360 High-8x8 sub-streams
should decode (they are in the size regime that already works), so `detect` from the
sub-stream becomes viable before bug 2 is resolved; the 2304x1296 main streams
(CABAC, ~220 KB IDRs) need bug 2 as well.

## Reporting upstream

Bug 1 is already fixed; the useful upstream report is bug 2 with the `g_1296_base` vs
`h_1296_base_lowbr` pair (same resolution/profile, bitrate only) plus the `0003` register
dump, addressed to sofus (driver author) and Janne Grunau (T8103 fixup author), noting that
`asahi-7.1.12-1` keeps the late submit (`avd-drv.c` IRQ handler unchanged at
`drv-asahi-7.1.12-1/avd-drv.c:217-223`).

## Uncertainty, honestly

- Bug 1: certain (static analysis; independently fixed upstream).
- Bug 2 mechanism: inferred, not observed. It is the only difference from the verified flow
  that scales with coefficient volume, and the direct evidence would come from the `0003`
  dump. Patch `0004` changes the IRQ/submit protocol; it is guarded by a module parameter and
  I made the PP-done path also release the VP slot, but a stuck job under the new ordering
  is possible and would still be caught by the watchdog reset (as today).
- "Fails on the first frame" in Phase 2 was inferred from timing; the `*_idr_only` clips
  settle it.
- Whether GStreamer versus ffmpeg populate `header_bit_size` identically was not checked
  (both failed the same way, and the driver's offset arithmetic matches eiln's).
- The 7.1.12 buffer-size rewrite (`139a0062983b`, "AVD works with interchange/compressed
  buffers internally") changes the RVRA layout to a tiled "comp" format that DCP can consume;
  I did not analyse whether 7.1.12's other H.264 changes introduce new T8103 regressions.


## Appendix: patch series (unified diffs, apply with `patch -p1` in the kernel tree)

### 0001-h264-fix-transform_8x8_mode_flag-bit.patch

```diff
From: AVD root-cause analysis <blue@aquarat.co.uk>
Subject: [PATCH 1/7] media: apple: avd: h264: fix transform_8x8_mode_flag encoding

stream_hdr() passes the raw V4L2 PPS flag (V4L2_H264_PPS_FLAG_TRANSFORM_8X8_MODE
== 0x40) into AVD_HDR_COMMON_LUMA_TBS(), i.e. FIELD_PREP(GENMASK(8, 7), 0x40).
(0x40 << 7) & 0x180 is 0, and FIELD_PREP only checks constant values, so bit 7
of hdr_2c_sps_param is silently never set. The VP then parses High-profile
8x8-transform slices as 4x4-only and raises DECODE_STATUS_ERR on the first
frame ("H2 00 error" on T8103). eiln's HAL sets
boolify(pps.transform_8x8_mode_flag) << 7 (avid/h264/halv3.py:155).

Backport of asahi-7.1.8-1 commit 8e8d72a673ab ("fixup! media: apple: avd:
define static values and bitmasks"), h264 part.

Expected to flip: f_high_8x8.h264, h_360_high8x8_lowbr.h264, t_320x240_high.h264.

Base: AsahiLinux/linux tag asahi-7.1.6-1 (drivers/media/platform/apple/avd, last driver commit 0d6dcb9b11b4)
---
diff --git a/drivers/media/platform/apple/avd/avd-h264.c b/drivers/media/platform/apple/avd/avd-h264.c
--- a/drivers/media/platform/apple/avd/avd-h264.c
+++ b/drivers/media/platform/apple/avd/avd-h264.c
@@ -29,6 +29,12 @@
 #define H264_FLAG_ENTROPY_CODING_MODE(v)	FIELD_PREP(BIT(20), !!(v))
 #define H264_FLAG_NOT_IDR(v)			FIELD_PREP(BIT(21), !!(v))
 
+/*
+ * hdr_2c bit 7: transform_8x8_mode_flag. Must be a boolean; passing the raw
+ * V4L2 flag value (0x40) through a 2-bit FIELD_PREP masks it to zero.
+ */
+#define H264_TRANSFORM_8X8_MODE(v)		FIELD_PREP(BIT(7), !!(v))
+
 
 struct avd_h264_run {
 	struct avd_run base;
@@ -216,7 +222,7 @@
 		| AVD_HDR_COMMON_BIT_DEPTH_C(sps->bit_depth_chroma_minus8)
 		| AVD_HDR_COMMON_MIN_LUMA_CBS(1)
 		| AVD_HDR_COMMON_LUMA_CBS(1)
-		| AVD_HDR_COMMON_LUMA_TBS(pps->flags & V4L2_H264_PPS_FLAG_TRANSFORM_8X8_MODE)
+		| H264_TRANSFORM_8X8_MODE(pps->flags & V4L2_H264_PPS_FLAG_TRANSFORM_8X8_MODE)
 		| AVD_HDR_COMMON_FLAG0(sps->flags & V4L2_H264_SPS_FLAG_DIRECT_8X8_INFERENCE),
 		"hdr_2c_sps_param");
 
```

### 0002-h264-fix-default-weights-handling.patch

```diff
From: AVD root-cause analysis <blue@aquarat.co.uk>
Subject: [PATCH 2/7] media: apple: avd: h264: fix default weights handling

For streams with weighted_bipred_idc == 2 (x264 default for Main/High with
B-frames) stream_weights() OR'ed the implicit-bipred default denominators
(5/5) into the explicit denominators of every P slice that carries real
weights (weighted_pred_flag == 1), corrupting weighted prediction. Only use
the defaults when no explicit weights are required for the slice, as eiln's
HAL does (avid/h264/halv3.py:205-216).

Backport of asahi-7.1.12-1 commit 3cba4f1d5b86.

Affects P frames of g_1080_main/g_1296_main/t_*_high (not the first-frame error).

Base: AsahiLinux/linux tag asahi-7.1.6-1 (drivers/media/platform/apple/avd, last driver commit 0d6dcb9b11b4)
---
diff --git a/drivers/media/platform/apple/avd/avd-h264.c b/drivers/media/platform/apple/avd/avd-h264.c
--- a/drivers/media/platform/apple/avd/avd-h264.c
+++ b/drivers/media/platform/apple/avd/avd-h264.c
@@ -297,20 +297,26 @@
 	const struct v4l2_ctrl_h264_slice_params *sl = run->slice_params;
 	struct avd_dev *avd = ctx->dev;
 
-	bool pred_weight = V4L2_H264_CTRL_PRED_WEIGHTS_REQUIRED(pps, sl);
+	bool pred_weight_req = V4L2_H264_CTRL_PRED_WEIGHTS_REQUIRED(pps, sl);
+	/*
+	 * Implicit bi-pred (weighted_bipred_idc == 2) only applies to B slices
+	 * without explicit weights. P slices in such a stream (x264 default
+	 * for Main/High: weighted_pred=1, weighted_bipred_idc=2) carry explicit
+	 * weights and must not have the default denominators OR'ed in.
+	 */
+	bool default_weights = pps->weighted_bipred_idc == 2 && !pred_weight_req;
 
-	/* TODO: is there a better flag or something for the
-	 * pps->weighted_bipred_idc == 2 checks? */
 	push(AVD_OP_WEIGHTS_HDR
-		| AVD_OP_WEIGHTS_HDR_FLAG1(pps->weighted_bipred_idc == 2)
-		| AVD_OP_WEIGHTS_HDR_FLAG0(pred_weight)
-		| AVD_OP_WEIGHTS_HDR_LUMA(weights->luma_log2_weight_denom)
-		| AVD_OP_WEIGHTS_HDR_CHROMA(weights->chroma_log2_weight_denom)
-		/* default luma and chroma denom */
-		| (pps->weighted_bipred_idc == 2 ? DEFAULT_WEIGHT_DENOM : 0),
+		| AVD_OP_WEIGHTS_HDR_FLAG1(default_weights)
+		| AVD_OP_WEIGHTS_HDR_FLAG0(pred_weight_req)
+		| AVD_OP_WEIGHTS_HDR_LUMA(!default_weights ?
+					  weights->luma_log2_weight_denom : 0)
+		| AVD_OP_WEIGHTS_HDR_CHROMA(!default_weights ?
+					    weights->chroma_log2_weight_denom : 0)
+		| (default_weights ? DEFAULT_WEIGHT_DENOM : 0),
 		"slc_76c_cmd_weights_denom");
 
-	if (!pred_weight)
+	if (!pred_weight_req)
 		return;
 
 	luma_denom = 1 << weights->luma_log2_weight_denom;
```

### 0003-dump-vp-status-on-error.patch

```diff
From: AVD root-cause analysis <blue@aquarat.co.uk>
Subject: [PATCH 3/7] media: apple: avd: dump VP status registers on decode error

"H%d %02d error" prints the raw mailbox word; for the replacement CM3
firmware that word is just the VP index that raised DECODE_STATUS_ERR. Dump
the per-VP registers (avd_status(): mb parsed, slice bytes parsed, status)
in the error path and in the watchdog so the failure position inside the
frame can be located (parse error at MB 0 vs stall mid-frame).

Base: AsahiLinux/linux tag asahi-7.1.6-1 (drivers/media/platform/apple/avd, last driver commit 0d6dcb9b11b4)
---
diff --git a/drivers/media/platform/apple/avd/avd-drv.c b/drivers/media/platform/apple/avd/avd-drv.c
--- a/drivers/media/platform/apple/avd/avd-drv.c
+++ b/drivers/media/platform/apple/avd/avd-drv.c
@@ -161,6 +161,8 @@
 
 	dev_err(avd->dev, "Frame processing timed out! Vp: %d (%02d)",
 		ctx->vp_slot, ctx->fifo_idx);
+	if (ctx->vp_slot != VP_SLOT_NONE)
+		avd_status(avd, ctx->vp_slot);
 
 	free_vp_slot(avd, ctx);
 	free_inst_slot(avd, ctx);
@@ -206,7 +208,16 @@
 
 		goto done;
 	} else {
-		dev_err(avd->dev, "H%d %02d error", status, ctx->fifo_idx);
+		/*
+		 * The CM3 firmware posts the bare VP index when that VP raised
+		 * DECODE_STATUS_ERR ("H2 00 error" == VP2, inst fifo 0).
+		 * Dump the VP registers (mb parsed, slice bytes parsed, ...)
+		 * so the failure point in the frame can be located.
+		 */
+		dev_err(avd->dev, "VP%d (fifo %02d) reported error (mbox status 0x%x)",
+			status, ctx->fifo_idx, status);
+		if (ctx->vp_slot != VP_SLOT_NONE)
+			avd_status(avd, ctx->vp_slot);
 		/* let watchdog handle */
 		goto done;
 	}
```

### 0004-h264-write-decode-command-right-after-instruction-stream.patch

```diff
From: AVD root-cause analysis <blue@aquarat.co.uk>
Subject: [PATCH 4/7] media: apple: avd: h264: write the decode command right after the instruction stream

The driver only writes the submit/decode command (ctrl+0x4014 on T8103)
from the IRQ handler once the VP has reported done ("a vp is done, kick the
pp and hope for the best"). eiln's hardware-verified T8103 flow
(m1n1 fw/avd/decoder.py AVDH264Dec.get_disp_frame) and the emulated Apple
firmware (avd_emu.py w_40104014 "decode command") write it immediately after
the last instruction word, so VP and PP run pipelined.

Serialising the two stages works for small frames but is the best
explanation for the bitrate/resolution/CABAC dependent DECODE_STATUS_ERR on
T8103 (2304x1296 CAVLC 358 KB IDR fails, 97 KB works; 1080p CAVLC 296 KB
works, 1080p CABAC 290 KB fails): the VP has to stream its residual output
into a PP that has not been started and stalls once the internal buffering
is exhausted. T8103 has no pipe_state buffer (AVD_QUIRK_NO_PIPE_STATE).

Write the command after each slice's instruction words (START flag on the
first slice of a frame, plain command for further slices, like
avd_hevc_submit()/avd_vp9_submit()) and skip the late submit in the IRQ
handler. Module parameter avd.h264_early_submit=0 restores the old order.

Expected to flip: g_1296_base.h264, g_1080_main.h264 (with 0002),
t_1296p_main_cavlc.h264; with 0001 also cam2-10s.h264.

Base: AsahiLinux/linux tag asahi-7.1.6-1 (drivers/media/platform/apple/avd, last driver commit 0d6dcb9b11b4)
---
diff --git a/drivers/media/platform/apple/avd/avd-drv.c b/drivers/media/platform/apple/avd/avd-drv.c
--- a/drivers/media/platform/apple/avd/avd-drv.c
+++ b/drivers/media/platform/apple/avd/avd-drv.c
@@ -166,6 +166,7 @@
 
 	free_vp_slot(avd, ctx);
 	free_inst_slot(avd, ctx);
+	ctx->submitted = false;
 
 	writel(0, avd->mbox + AVD_REG_MBOX_IRQ_ENABLE);
 	ret = avd_reset(avd);
@@ -200,10 +201,18 @@
 		state = VB2_BUF_STATE_DONE;
 
 		free_inst_slot(avd, ctx);
+		/* with an early submit the vp-done IRQ may still be pending */
+		if (ctx->submitted && ctx->vp_slot != VP_SLOT_NONE)
+			free_vp_slot(avd, ctx);
+		ctx->submitted = false;
 	} else if (status & 0x100) {
 		free_vp_slot(avd, ctx);
-		/* a vp is done, kick the pp and hope for the best */
-		if(ctx->coded_fmt_desc->ops->submit)
+		/*
+		 * a vp is done, kick the pp and hope for the best -- unless the
+		 * codec already submitted right after pushing the instruction
+		 * stream (the order the firmware-traced sequence uses).
+		 */
+		if (!ctx->submitted && ctx->coded_fmt_desc->ops->submit)
 			ctx->coded_fmt_desc->ops->submit(ctx);
 
 		goto done;
diff --git a/drivers/media/platform/apple/avd/avd-h264.c b/drivers/media/platform/apple/avd/avd-h264.c
--- a/drivers/media/platform/apple/avd/avd-h264.c
+++ b/drivers/media/platform/apple/avd/avd-h264.c
@@ -20,6 +20,8 @@
 #include <media/v4l2-h264.h>
 #include <media/videobuf2-dma-contig.h>
 
+#include <linux/moduleparam.h>
+
 #include "avd.h"
 #include "avd-inst.h"
 
@@ -74,6 +76,11 @@
 	} bufs;
 };
 
+static bool avd_h264_early_submit = true;
+module_param_named(h264_early_submit, avd_h264_early_submit, bool, 0644);
+MODULE_PARM_DESC(h264_early_submit,
+		 "write the H.264 decode command right after the instruction stream instead of after VP done (default: Y)");
+
 /* ffmpeg submits wrong timestamp, use first_mb_in_slice as a workaround */
 #define is_new_frame(sl) (sl->first_mb_in_slice == 0) /* (ctx->fh.m2m_ctx->new_frame) */
 
@@ -712,6 +719,28 @@
 
 	slice_size = stream_slice(ctx, &run);
 
+	/*
+	 * Write the decode command as soon as the instruction stream for the
+	 * slice is in the FIFO (eiln's m1n1 flow and the emulated Apple
+	 * firmware do this before waiting for anything). Waiting for the VP
+	 * done IRQ before kicking the PP serialises the two stages; on T8103
+	 * (no pipe_state) that appears to stall the VP on frames whose
+	 * residual output exceeds the internal buffering (high bitrate,
+	 * CABAC, >1080p) and ends in DECODE_STATUS_ERR.
+	 *
+	 * First slice of a frame: START flag; further slices: plain command
+	 * (same pattern as avd_hevc_submit()/avd_vp9_submit()).
+	 */
+	if (avd_h264_early_submit) {
+		writel_relaxed(0x2b000000
+			| (is_new_frame(run.slice_params) ?
+				(avd->variant->revision == 3 ? 0x100 : 0x200) : 0)
+			| (ctx->fifo_idx << 4)
+			| avd->variant->fifo_slots,
+			avd->ctrl + avd->variant->submit_offset);
+		ctx->submitted = true;
+	}
+
 	if (run.base.bufs.src->flags & V4L2_BUF_FLAG_M2M_HOLD_CAPTURE_BUF) {
 		if (avd->variant->revision == 3)
 			reg = (0x18 | ctx->vp_slot << 12);
diff --git a/drivers/media/platform/apple/avd/avd.h b/drivers/media/platform/apple/avd/avd.h
--- a/drivers/media/platform/apple/avd/avd.h
+++ b/drivers/media/platform/apple/avd/avd.h
@@ -216,6 +216,9 @@
 
 	u8 fifo_idx;
 	u8 vp_slot;
+
+	/* codec already wrote the submit/decode command for this job */
+	bool submitted;
 };
 
 struct avd_buf {
```

### 0005-t8103-restore-instruction-stream-dma-config-words.patch

```diff
From: AVD root-cause analysis <blue@aquarat.co.uk>
Subject: [PATCH 5/7] media: apple: avd: t8103: push the firmware-traced DMA config words

INST_DMA1/2/3 are defined as 0 with the real values only in comments. On
T8103 the Apple firmware inserts 0x00020002 / 0x04020002 / 0x00070007 from a
per-codec table in CM3 SRAM before each buffer-address group (m1n1
fw/avd/__init__.py mcpu_decode_init dump, avid/h264/halv3.py
cm3_dma_config_*). Zeros were only verified on newer revisions. Push the
traced values on revision 3; avd.inst_dma_words=0 disables this.

Experiment for the size-dependent failures (low risk: matches the
hardware-verified stream byte for byte).

Base: AsahiLinux/linux tag asahi-7.1.6-1 (drivers/media/platform/apple/avd, last driver commit 0d6dcb9b11b4)
---
diff --git a/drivers/media/platform/apple/avd/avd-drv.c b/drivers/media/platform/apple/avd/avd-drv.c
--- a/drivers/media/platform/apple/avd/avd-drv.c
+++ b/drivers/media/platform/apple/avd/avd-drv.c
@@ -21,6 +21,11 @@
 #include "avd.h"
 #include "avd-regs.h"
 
+bool avd_inst_dma_words = true;
+module_param_named(inst_dma_words, avd_inst_dma_words, bool, 0644);
+MODULE_PARM_DESC(inst_dma_words,
+		 "push the firmware-traced DMA config words on T8103 instead of zeros (default: Y)");
+
 void fill_rvra(struct avd_rvra *rvra, enum avd_image_fmt image_fmt,
 		u32 width, u32 height)
 {
diff --git a/drivers/media/platform/apple/avd/avd-inst.h b/drivers/media/platform/apple/avd/avd-inst.h
--- a/drivers/media/platform/apple/avd/avd-inst.h
+++ b/drivers/media/platform/apple/avd/avd-inst.h
@@ -7,10 +7,18 @@
 
 #include "avd.h"
 
-/* i have no clue what this is */
-#define INST_DMA1 0 /* (0x14 << 16 | 0x14) */
-#define INST_DMA2 0 /* (0x4000000 | INST_DMA1) */
-#define INST_DMA3 0 /* (0x07 << 16 | 0x07) */
+/*
+ * DMA configuration words that precede every buffer address group in the
+ * instruction stream. The Apple firmware inserts them from a per-codec table
+ * in CM3 SRAM; on T8103 the traced values are 0x00020002 / 0x04020002 /
+ * 0x00070007 (eiln avid/h264/halv3.py, m1n1 fw/avd/__init__.py). Newer
+ * revisions apparently tolerate zeros (comment values were 0x14 << 16 | 0x14).
+ * Keep them for revision 3 unless avd.inst_dma_words=0.
+ */
+extern bool avd_inst_dma_words;
+#define INST_DMA1 ((avd_inst_dma_words && avd->variant->revision == 3) ? 0x00020002 : 0)
+#define INST_DMA2 ((avd_inst_dma_words && avd->variant->revision == 3) ? 0x04020002 : 0)
+#define INST_DMA3 ((avd_inst_dma_words && avd->variant->revision == 3) ? 0x00070007 : 0)
 
 #define AVD_OP_EXEC			FIELD_PREP(GENMASK(31, 24), 0x2b)
 #define AVD_OP_EXEC_FIFO_MASK(v)	FIELD_PREP(GENMASK(3, 0), v)
```

### 0006-t8103-extra-init-registers.patch

```diff
From: AVD root-cause analysis <blue@aquarat.co.uk>
Subject: [PATCH 6/7] media: apple: avd: t8103: write ctrl+0x40f4/0x4110 after CM3 boot

eiln's T8103 bring-up writes ctrl+0x40f4 = 0x1555 and ctrl+0x4110 = 0
right after the DMA tunables (which the replacement firmware applies at
boot). The in-tree driver never touches either register. 0x1555 looks like
a 2-bit field for each of the 7 instruction FIFOs. Purpose unknown;
experiment only. avd.t8103_extra_init=0 disables it.

Base: AsahiLinux/linux tag asahi-7.1.6-1 (drivers/media/platform/apple/avd, last driver commit 0d6dcb9b11b4)
---
diff --git a/drivers/media/platform/apple/avd/avd-drv.c b/drivers/media/platform/apple/avd/avd-drv.c
--- a/drivers/media/platform/apple/avd/avd-drv.c
+++ b/drivers/media/platform/apple/avd/avd-drv.c
@@ -26,6 +26,11 @@
 MODULE_PARM_DESC(inst_dma_words,
 		 "push the firmware-traced DMA config words on T8103 instead of zeros (default: Y)");
 
+bool avd_t8103_extra_init = true;
+module_param_named(t8103_extra_init, avd_t8103_extra_init, bool, 0644);
+MODULE_PARM_DESC(t8103_extra_init,
+		 "write ctrl+0x40f4=0x1555 and ctrl+0x4110=0 after CM3 boot on T8103 as eiln's m1n1 init does (default: Y)");
+
 void fill_rvra(struct avd_rvra *rvra, enum avd_image_fmt image_fmt,
 		u32 width, u32 height)
 {
diff --git a/drivers/media/platform/apple/avd/avd-hw.c b/drivers/media/platform/apple/avd/avd-hw.c
--- a/drivers/media/platform/apple/avd/avd-hw.c
+++ b/drivers/media/platform/apple/avd/avd-hw.c
@@ -61,6 +61,16 @@
 	if (ret)
 		return ret;
 
+	/*
+	 * eiln's T8103 bring-up (m1n1 fw/avd/__init__.py, avd_dma_tunables_stage0)
+	 * writes these two after the DMA tunables the firmware applies at boot.
+	 * 0x1555 looks like a 2-bit field per instruction FIFO (7 FIFOs).
+	 */
+	if (avd->variant->revision == 3 && avd_t8103_extra_init) {
+		writel_relaxed(0x1555, avd->ctrl + 0x40f4);
+		writel_relaxed(0, avd->ctrl + 0x4110);
+	}
+
 	return 0;
 }
 
diff --git a/drivers/media/platform/apple/avd/avd.h b/drivers/media/platform/apple/avd/avd.h
--- a/drivers/media/platform/apple/avd/avd.h
+++ b/drivers/media/platform/apple/avd/avd.h
@@ -279,6 +279,8 @@
 	return container_of(file_to_v4l2_fh(filp), struct avd_ctx, fh);
 }
 
+extern bool avd_t8103_extra_init;
+
 /* hw stuff */
 int avd_boot(struct avd_dev *avd);
 void avd_shutdown(struct avd_dev *avd);
```

### 0007-t8103-insn-fifo-mask-0.patch

```diff
From: AVD root-cause analysis <blue@aquarat.co.uk>
Subject: [PATCH 7/7] media: apple: avd: t8103: set INSN_FIFO_MASK to 0

Backport of asahi-7.1.12-1 commit ca9a850f237f (Janne Grunau, 2026-08-30,
"fixup! media: apple: add avd driver"): t8103_configure_stream() writes 0
instead of 0x100000 to AVD_V3_VP_INSN_FIFO_MASK, matching what the t8112/
t8122 paths do. The commit has no description; eiln's m1n1 code uses
0x100000 with 1 MiB FIFOs. Included for parity with the newest tree; no
known link to the failures analysed here.

Base: AsahiLinux/linux tag asahi-7.1.6-1 (drivers/media/platform/apple/avd, last driver commit 0d6dcb9b11b4)
---
diff --git a/drivers/media/platform/apple/avd/avd-hw.c b/drivers/media/platform/apple/avd/avd-hw.c
--- a/drivers/media/platform/apple/avd/avd-hw.c
+++ b/drivers/media/platform/apple/avd/avd-hw.c
@@ -88,7 +88,8 @@
 			  u32 vp_slot)
 {
 	w32(AVD_V3_VP_INSN_FIFO_IOVA + (fifo_idx * 4), addr >> 8);
-	w32(AVD_V3_VP_INSN_FIFO_MASK + (fifo_idx * 4), 0x100000);
+	/* asahi-7.1.12-1 ca9a850f237f ("fixup! media: apple: add avd driver") */
+	w32(AVD_V3_VP_INSN_FIFO_MASK + (fifo_idx * 4), 0);
 	w32(AVD_V3_VP_INSN_FIFO_CACH + (fifo_idx * 4), 0);
 	w32(AVD_V3_VP_INSN_FIFO_XFER + (fifo_idx * 4), 0);
 
```

