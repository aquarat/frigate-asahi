#!/usr/bin/env python3
"""Generate sequential unified diffs (git-style a/ b/ paths) for the AVD 7.1.6-1 driver."""
import os, shutil, subprocess, sys
FRIGATE_HOME = os.environ.get('FRIGATE_HOME', os.path.expanduser('~/frigate'))
BASE = FRIGATE_HOME + '/src/avd-analysis/drv-asahi-7.1.6-1'
OUT = FRIGATE_HOME + '/src/avd-analysis/patches'
WORK = FRIGATE_HOME + '/src/avd-analysis/patchwork'
PFX = 'drivers/media/platform/apple/avd/'

def edit(tree, fn, old, new, count=1):
    p = os.path.join(tree, fn); s = open(p).read()
    if s.count(old) != count:
        print(f"ERROR {fn}: expected {count} of:\n{old}\nfound {s.count(old)}"); sys.exit(1)
    open(p, 'w').write(s.replace(old, new))

PATCHES = []
def patch(name, subject, body):
    def deco(fn):
        PATCHES.append((name, subject, body, fn)); return fn
    return deco

@patch('0001-h264-fix-transform_8x8_mode_flag-bit', 'media: apple: avd: h264: fix transform_8x8_mode_flag encoding',
'''stream_hdr() passes the raw V4L2 PPS flag (V4L2_H264_PPS_FLAG_TRANSFORM_8X8_MODE
== 0x40) into AVD_HDR_COMMON_LUMA_TBS(), i.e. FIELD_PREP(GENMASK(8, 7), 0x40).
(0x40 << 7) & 0x180 is 0, and FIELD_PREP only checks constant values, so bit 7
of hdr_2c_sps_param is silently never set. The VP then parses High-profile
8x8-transform slices as 4x4-only and raises DECODE_STATUS_ERR on the first
frame ("H2 00 error" on T8103). eiln's HAL sets
boolify(pps.transform_8x8_mode_flag) << 7 (avid/h264/halv3.py:155).

Backport of asahi-7.1.8-1 commit 8e8d72a673ab ("fixup! media: apple: avd:
define static values and bitmasks"), h264 part.

Expected to flip: f_high_8x8.h264, h_360_high8x8_lowbr.h264, t_320x240_high.h264.
''')
def p1(t):
    edit(t, 'avd-h264.c', '''#define H264_FLAG_NOT_IDR(v)			FIELD_PREP(BIT(21), !!(v))
''', '''#define H264_FLAG_NOT_IDR(v)			FIELD_PREP(BIT(21), !!(v))

/*
 * hdr_2c bit 7: transform_8x8_mode_flag. Must be a boolean; passing the raw
 * V4L2 flag value (0x40) through a 2-bit FIELD_PREP masks it to zero.
 */
#define H264_TRANSFORM_8X8_MODE(v)		FIELD_PREP(BIT(7), !!(v))
''')
    edit(t, 'avd-h264.c', '''		| AVD_HDR_COMMON_LUMA_TBS(pps->flags & V4L2_H264_PPS_FLAG_TRANSFORM_8X8_MODE)
''', '''		| H264_TRANSFORM_8X8_MODE(pps->flags & V4L2_H264_PPS_FLAG_TRANSFORM_8X8_MODE)
''')

@patch('0002-h264-fix-default-weights-handling', 'media: apple: avd: h264: fix default weights handling',
'''For streams with weighted_bipred_idc == 2 (x264 default for Main/High with
B-frames) stream_weights() OR'ed the implicit-bipred default denominators
(5/5) into the explicit denominators of every P slice that carries real
weights (weighted_pred_flag == 1), corrupting weighted prediction. Only use
the defaults when no explicit weights are required for the slice, as eiln's
HAL does (avid/h264/halv3.py:205-216).

Backport of asahi-7.1.12-1 commit 3cba4f1d5b86.

Affects P frames of g_1080_main/g_1296_main/t_*_high (not the first-frame error).
''')
def p2(t):
    edit(t, 'avd-h264.c', '''	bool pred_weight = V4L2_H264_CTRL_PRED_WEIGHTS_REQUIRED(pps, sl);

	/* TODO: is there a better flag or something for the
	 * pps->weighted_bipred_idc == 2 checks? */
	push(AVD_OP_WEIGHTS_HDR
		| AVD_OP_WEIGHTS_HDR_FLAG1(pps->weighted_bipred_idc == 2)
		| AVD_OP_WEIGHTS_HDR_FLAG0(pred_weight)
		| AVD_OP_WEIGHTS_HDR_LUMA(weights->luma_log2_weight_denom)
		| AVD_OP_WEIGHTS_HDR_CHROMA(weights->chroma_log2_weight_denom)
		/* default luma and chroma denom */
		| (pps->weighted_bipred_idc == 2 ? DEFAULT_WEIGHT_DENOM : 0),
		"slc_76c_cmd_weights_denom");

	if (!pred_weight)
		return;
''', '''	bool pred_weight_req = V4L2_H264_CTRL_PRED_WEIGHTS_REQUIRED(pps, sl);
	/*
	 * Implicit bi-pred (weighted_bipred_idc == 2) only applies to B slices
	 * without explicit weights. P slices in such a stream (x264 default
	 * for Main/High: weighted_pred=1, weighted_bipred_idc=2) carry explicit
	 * weights and must not have the default denominators OR'ed in.
	 */
	bool default_weights = pps->weighted_bipred_idc == 2 && !pred_weight_req;

	push(AVD_OP_WEIGHTS_HDR
		| AVD_OP_WEIGHTS_HDR_FLAG1(default_weights)
		| AVD_OP_WEIGHTS_HDR_FLAG0(pred_weight_req)
		| AVD_OP_WEIGHTS_HDR_LUMA(!default_weights ?
					  weights->luma_log2_weight_denom : 0)
		| AVD_OP_WEIGHTS_HDR_CHROMA(!default_weights ?
					    weights->chroma_log2_weight_denom : 0)
		| (default_weights ? DEFAULT_WEIGHT_DENOM : 0),
		"slc_76c_cmd_weights_denom");

	if (!pred_weight_req)
		return;
''')

@patch('0003-dump-vp-status-on-error', 'media: apple: avd: dump VP status registers on decode error',
'''"H%d %02d error" prints the raw mailbox word; for the replacement CM3
firmware that word is just the VP index that raised DECODE_STATUS_ERR. Dump
the per-VP registers (avd_status(): mb parsed, slice bytes parsed, status)
in the error path and in the watchdog so the failure position inside the
frame can be located (parse error at MB 0 vs stall mid-frame).
''')
def p3(t):
    edit(t, 'avd-drv.c', '''	dev_err(avd->dev, "Frame processing timed out! Vp: %d (%02d)",
		ctx->vp_slot, ctx->fifo_idx);

	free_vp_slot(avd, ctx);
''', '''	dev_err(avd->dev, "Frame processing timed out! Vp: %d (%02d)",
		ctx->vp_slot, ctx->fifo_idx);
	if (ctx->vp_slot != VP_SLOT_NONE)
		avd_status(avd, ctx->vp_slot);

	free_vp_slot(avd, ctx);
''')
    edit(t, 'avd-drv.c', '''	} else {
		dev_err(avd->dev, "H%d %02d error", status, ctx->fifo_idx);
		/* let watchdog handle */
		goto done;
	}
''', '''	} else {
		/*
		 * The CM3 firmware posts the bare VP index when that VP raised
		 * DECODE_STATUS_ERR ("H2 00 error" == VP2, inst fifo 0).
		 * Dump the VP registers (mb parsed, slice bytes parsed, ...)
		 * so the failure point in the frame can be located.
		 */
		dev_err(avd->dev, "VP%d (fifo %02d) reported error (mbox status 0x%x)",
			status, ctx->fifo_idx, status);
		if (ctx->vp_slot != VP_SLOT_NONE)
			avd_status(avd, ctx->vp_slot);
		/* let watchdog handle */
		goto done;
	}
''')

@patch('0004-h264-write-decode-command-right-after-instruction-stream', 'media: apple: avd: h264: write the decode command right after the instruction stream',
'''The driver only writes the submit/decode command (ctrl+0x4014 on T8103)
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
''')
def p4(t):
    edit(t, 'avd.h', '''	u8 fifo_idx;
	u8 vp_slot;
};
''', '''	u8 fifo_idx;
	u8 vp_slot;

	/* codec already wrote the submit/decode command for this job */
	bool submitted;
};
''')
    edit(t, 'avd-drv.c', '''	} else if (status & 0x100) {
		free_vp_slot(avd, ctx);
		/* a vp is done, kick the pp and hope for the best */
		if(ctx->coded_fmt_desc->ops->submit)
			ctx->coded_fmt_desc->ops->submit(ctx);

		goto done;
	} else {
''', '''	} else if (status & 0x100) {
		free_vp_slot(avd, ctx);
		/*
		 * a vp is done, kick the pp and hope for the best -- unless the
		 * codec already submitted right after pushing the instruction
		 * stream (the order the firmware-traced sequence uses).
		 */
		if (!ctx->submitted && ctx->coded_fmt_desc->ops->submit)
			ctx->coded_fmt_desc->ops->submit(ctx);

		goto done;
	} else {
''')
    edit(t, 'avd-drv.c', '''	if (status & 0x1000) {
		/* pp is done ! we are done */
		state = VB2_BUF_STATE_DONE;

		free_inst_slot(avd, ctx);
	}''', '''	if (status & 0x1000) {
		/* pp is done ! we are done */
		state = VB2_BUF_STATE_DONE;

		free_inst_slot(avd, ctx);
		/* with an early submit the vp-done IRQ may still be pending */
		if (ctx->submitted && ctx->vp_slot != VP_SLOT_NONE)
			free_vp_slot(avd, ctx);
		ctx->submitted = false;
	}''')
    edit(t, 'avd-drv.c', '''	free_vp_slot(avd, ctx);
	free_inst_slot(avd, ctx);

	writel(0, avd->mbox + AVD_REG_MBOX_IRQ_ENABLE);
''', '''	free_vp_slot(avd, ctx);
	free_inst_slot(avd, ctx);
	ctx->submitted = false;

	writel(0, avd->mbox + AVD_REG_MBOX_IRQ_ENABLE);
''')
    edit(t, 'avd-h264.c', '''#include "avd.h"
#include "avd-inst.h"
''', '''#include <linux/moduleparam.h>

#include "avd.h"
#include "avd-inst.h"
''')
    edit(t, 'avd-h264.c', '''/* ffmpeg submits wrong timestamp, use first_mb_in_slice as a workaround */
''', '''static bool avd_h264_early_submit = true;
module_param_named(h264_early_submit, avd_h264_early_submit, bool, 0644);
MODULE_PARM_DESC(h264_early_submit,
		 "write the H.264 decode command right after the instruction stream instead of after VP done (default: Y)");

/* ffmpeg submits wrong timestamp, use first_mb_in_slice as a workaround */
''')
    edit(t, 'avd-h264.c', '''	schedule_delayed_work(&ctx->watchdog_work, msecs_to_jiffies(2000));

	slice_size = stream_slice(ctx, &run);
''', '''	schedule_delayed_work(&ctx->watchdog_work, msecs_to_jiffies(2000));

	slice_size = stream_slice(ctx, &run);

	/*
	 * Write the decode command as soon as the instruction stream for the
	 * slice is in the FIFO (eiln's m1n1 flow and the emulated Apple
	 * firmware do this before waiting for anything). Waiting for the VP
	 * done IRQ before kicking the PP serialises the two stages; on T8103
	 * (no pipe_state) that appears to stall the VP on frames whose
	 * residual output exceeds the internal buffering (high bitrate,
	 * CABAC, >1080p) and ends in DECODE_STATUS_ERR.
	 *
	 * First slice of a frame: START flag; further slices: plain command
	 * (same pattern as avd_hevc_submit()/avd_vp9_submit()).
	 */
	if (avd_h264_early_submit) {
		writel_relaxed(0x2b000000
			| (is_new_frame(run.slice_params) ?
				(avd->variant->revision == 3 ? 0x100 : 0x200) : 0)
			| (ctx->fifo_idx << 4)
			| avd->variant->fifo_slots,
			avd->ctrl + avd->variant->submit_offset);
		ctx->submitted = true;
	}
''')

@patch('0005-t8103-restore-instruction-stream-dma-config-words', 'media: apple: avd: t8103: push the firmware-traced DMA config words',
'''INST_DMA1/2/3 are defined as 0 with the real values only in comments. On
T8103 the Apple firmware inserts 0x00020002 / 0x04020002 / 0x00070007 from a
per-codec table in CM3 SRAM before each buffer-address group (m1n1
fw/avd/__init__.py mcpu_decode_init dump, avid/h264/halv3.py
cm3_dma_config_*). Zeros were only verified on newer revisions. Push the
traced values on revision 3; avd.inst_dma_words=0 disables this.

Experiment for the size-dependent failures (low risk: matches the
hardware-verified stream byte for byte).
''')
def p5(t):
    edit(t, 'avd-inst.h', '''/* i have no clue what this is */
#define INST_DMA1 0 /* (0x14 << 16 | 0x14) */
#define INST_DMA2 0 /* (0x4000000 | INST_DMA1) */
#define INST_DMA3 0 /* (0x07 << 16 | 0x07) */
''', '''/*
 * DMA configuration words that precede every buffer address group in the
 * instruction stream. The Apple firmware inserts them from a per-codec table
 * in CM3 SRAM; on T8103 the traced values are 0x00020002 / 0x04020002 /
 * 0x00070007 (eiln avid/h264/halv3.py, m1n1 fw/avd/__init__.py). Newer
 * revisions apparently tolerate zeros (comment values were 0x14 << 16 | 0x14).
 * Keep them for revision 3 unless avd.inst_dma_words=0.
 */
extern bool avd_inst_dma_words;
#define INST_DMA1 ((avd_inst_dma_words && avd->variant->revision == 3) ? 0x00020002 : 0)
#define INST_DMA2 ((avd_inst_dma_words && avd->variant->revision == 3) ? 0x04020002 : 0)
#define INST_DMA3 ((avd_inst_dma_words && avd->variant->revision == 3) ? 0x00070007 : 0)
''')
    edit(t, 'avd-drv.c', '''void fill_rvra(struct avd_rvra *rvra, enum avd_image_fmt image_fmt,
		u32 width, u32 height)
{''', '''bool avd_inst_dma_words = true;
module_param_named(inst_dma_words, avd_inst_dma_words, bool, 0644);
MODULE_PARM_DESC(inst_dma_words,
		 "push the firmware-traced DMA config words on T8103 instead of zeros (default: Y)");

void fill_rvra(struct avd_rvra *rvra, enum avd_image_fmt image_fmt,
		u32 width, u32 height)
{''')

@patch('0006-t8103-extra-init-registers', 'media: apple: avd: t8103: write ctrl+0x40f4/0x4110 after CM3 boot',
'''eiln's T8103 bring-up writes ctrl+0x40f4 = 0x1555 and ctrl+0x4110 = 0
right after the DMA tunables (which the replacement firmware applies at
boot). The in-tree driver never touches either register. 0x1555 looks like
a 2-bit field for each of the 7 instruction FIFOs. Purpose unknown;
experiment only. avd.t8103_extra_init=0 disables it.
''')
def p6(t):
    edit(t, 'avd.h', '''/* hw stuff */
int avd_boot(struct avd_dev *avd);
''', '''extern bool avd_t8103_extra_init;

/* hw stuff */
int avd_boot(struct avd_dev *avd);
''')
    edit(t, 'avd-drv.c', '''void fill_rvra(struct avd_rvra *rvra, enum avd_image_fmt image_fmt,
		u32 width, u32 height)
{''', '''bool avd_t8103_extra_init = true;
module_param_named(t8103_extra_init, avd_t8103_extra_init, bool, 0644);
MODULE_PARM_DESC(t8103_extra_init,
		 "write ctrl+0x40f4=0x1555 and ctrl+0x4110=0 after CM3 boot on T8103 as eiln's m1n1 init does (default: Y)");

void fill_rvra(struct avd_rvra *rvra, enum avd_image_fmt image_fmt,
		u32 width, u32 height)
{''')
    edit(t, 'avd-hw.c', '''	/* wait for cm3 to boot */
	ret = readl_poll_timeout(avd->mbox + AVD_REG_FLAG0_SET,
			val, val == 1, 10, 10000);
	if (ret)
		return ret;

	return 0;
}
''', '''	/* wait for cm3 to boot */
	ret = readl_poll_timeout(avd->mbox + AVD_REG_FLAG0_SET,
			val, val == 1, 10, 10000);
	if (ret)
		return ret;

	/*
	 * eiln's T8103 bring-up (m1n1 fw/avd/__init__.py, avd_dma_tunables_stage0)
	 * writes these two after the DMA tunables the firmware applies at boot.
	 * 0x1555 looks like a 2-bit field per instruction FIFO (7 FIFOs).
	 */
	if (avd->variant->revision == 3 && avd_t8103_extra_init) {
		writel_relaxed(0x1555, avd->ctrl + 0x40f4);
		writel_relaxed(0, avd->ctrl + 0x4110);
	}

	return 0;
}
''')

@patch('0007-t8103-insn-fifo-mask-0', 'media: apple: avd: t8103: set INSN_FIFO_MASK to 0',
'''Backport of asahi-7.1.12-1 commit ca9a850f237f (Janne Grunau, 2026-08-30,
"fixup! media: apple: add avd driver"): t8103_configure_stream() writes 0
instead of 0x100000 to AVD_V3_VP_INSN_FIFO_MASK, matching what the t8112/
t8122 paths do. The commit has no description; eiln's m1n1 code uses
0x100000 with 1 MiB FIFOs. Included for parity with the newest tree; no
known link to the failures analysed here.
''')
def p7(t):
    edit(t, 'avd-hw.c', '''	w32(AVD_V3_VP_INSN_FIFO_IOVA + (fifo_idx * 4), addr >> 8);
	w32(AVD_V3_VP_INSN_FIFO_MASK + (fifo_idx * 4), 0x100000);
''', '''	w32(AVD_V3_VP_INSN_FIFO_IOVA + (fifo_idx * 4), addr >> 8);
	/* asahi-7.1.12-1 ca9a850f237f ("fixup! media: apple: add avd driver") */
	w32(AVD_V3_VP_INSN_FIFO_MASK + (fifo_idx * 4), 0);
''')

def main():
    shutil.rmtree(WORK, ignore_errors=True); os.makedirs(WORK)
    prev = os.path.join(WORK, 'step0'); shutil.copytree(BASE, prev)
    for f in os.listdir(OUT):
        if f.endswith('.patch'): os.remove(os.path.join(OUT, f))
    allp = []
    for n, (name, subject, body, fn) in enumerate(PATCHES, 1):
        cur = os.path.join(WORK, f'step{n}'); shutil.copytree(prev, cur); fn(cur)
        hdr = f"From: AVD root-cause analysis <blue@aquarat.co.uk>\nSubject: [PATCH {n}/{len(PATCHES)}] {subject}\n\n{body}\nBase: AsahiLinux/linux tag asahi-7.1.6-1 (drivers/media/platform/apple/avd, last driver commit 0d6dcb9b11b4)\n---\n"
        out = hdr
        for f in sorted(os.listdir(cur)):
            a, b = os.path.join(prev, f), os.path.join(cur, f)
            r = subprocess.run(['diff', '-u', '--label', 'a/' + PFX + f, '--label', 'b/' + PFX + f, a, b], capture_output=True, text=True)
            if r.returncode == 1:
                out += f"diff --git a/{PFX}{f} b/{PFX}{f}\n" + r.stdout
        p = os.path.join(OUT, name + '.patch'); open(p, 'w').write(out); allp.append(p); prev = cur
        print("wrote", p)
    open(os.path.join(OUT, 'series'), 'w').write('\n'.join(os.path.basename(p) for p in allp) + '\n')
main()
