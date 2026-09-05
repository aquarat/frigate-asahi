# Optional / experimental patches (NOT part of patches/final)

- `h264-early-submit-v2.patch` (module parameter `h264_early_submit`, default on):
  write the H.264 decode command once per frame right after the first slice's
  instruction words instead of from the VP-done IRQ handler (matches eiln's
  T8103 flow). The original 0004 from the root-cause analysis wrote one command
  per slice like the HEVC path; that broke multi-slice frames (PP-done IRQ never
  arrives from the second frame on, `Frame processing timed out! Vp: 255`), v2
  writes one START command per frame and passes the multi-slice clips.
  With patches/final applied it is not needed for correctness: `INSN_FIFO_MASK=0`
  (final 0003) alone fixes the large-frame stall. Measured benefit on top of
  final: cam2-10s (150 frames, 2304x1296) 0.50 s vs 0.59 s with gst fakesink
  (~15%). Kept for reference; do not send upstream without more testing
  (changes the IRQ/submit protocol).
- Analysis patches 0005 (traced INST_DMA words, `inst_dma_words`) and 0006
  (`ctrl+0x40f4=0x1555`, `ctrl+0x4110=0`, `t8103_extra_init`) from
  research/avd-patches-7.1.6-1/ were tested on/off in combination and made no
  difference either way once INSN_FIFO_MASK=0 is applied; not carried here.
