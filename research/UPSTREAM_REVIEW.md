# Upstream review of the 2026-09-05 patch series

Reviewed 2026-09-05 (read-only). Scope: `src/avd-driver/patches/final/0001..0008`,
`patches/optional/h264-early-submit-v2.patch`, `src/avd-driver/patches/ffmpeg/0001..0004`,
`patches/frigate/0001-*`, `patches/zmq_ipc.py` vs `.orig`, the `record/maintainer.py` process-name
issue, the Mesa Honeykrisp plane-offset bug, and licensing. Tools run: `checkpatch.pl` from
`/lib/modules/7.1.6-400.asahi.fc44.aarch64+16k/build/scripts/` (v0.32, `--strict`), FFmpeg
`tools/patcheck` from `src/ffmpeg-vulkan/FFmpeg`. Upstream state was taken from the sparse
`src/avd-analysis/linux-7.1.12` clone (tag asahi-7.1.12-1, HEAD ca9a850) and from raw files fetched
today from `AsahiLinux/linux` (`asahi-wip`, `asahi`), `FFmpeg/FFmpeg` master, `Mesa3D/mesa` main,
`blakeblackshear/frigate` dev, `torvalds/linux` master and `KhronosGroup/Vulkan-Headers`. Scratch
copies are in the session scratchpad (`checkpatch.txt`, `patcheck.txt`, `pr600.patch`, etc.).

Note on `src/avd-analysis/drv-asahi-tip`: that directory is *older* than 7.1.12 (it still has
`rvra`/`INST_DMA`); it matches the GitHub `asahi` branch, not the development tip. The branch the
tags are cut from and that PRs target is `asahi-wip`, which today is byte-identical to the 7.1.12
driver files.

## Executive summary

Nothing is sendable exactly as it stands: every new patch has `Signed-off-by: <fill in>` (checkpatch
ERROR), the author identity differs between patches ("Aquarat", "Aaron", "avd"), and the kernel
series is numbered `[PATCH n/5]` / `[PATCH]` inconsistently. Beyond that:

**Send after mechanical fixes**
- FFmpeg 0001/0002 (cacheable CAPTURE buffers, skip cache clean) to Jonas Karlman's v4l2request
  series; small, correct per the V4L2 API, in Kwiboo's style.
- Kernel 0006, cut down to the two `allow_cache_hints = true` lines (the `DMA_ATTR_NO_KERNEL_MAPPING`
  half is already upstream: `asahi-wip` `avd-drv.c:271` has `dst_vq->dma_attrs = 0`).
- Frigate `zmq_ipc.py` retry: small, follows the reset pattern already in the file; strip the local
  comment.

**Needs rework before sending**
- FFmpeg 0003 (multi-plane DRM layer import): technically sound; re-motivate it with in-tree
  exporters (kmsgrab, VAAPI composed layers) because v4l2request/rkmpp are not in FFmpeg master, add
  a `sw_format` plane-count sanity check, ask for ANV/RADV testing.
- FFmpeg 0004 (Honeykrisp quirk): file the Mesa issue first, fix the header guard (`>= 287`, not 290:
  verified against Vulkan-Headers tags), bound it by Mesa `driverVersion`, or better, argue for
  applying the offset via `memoryOffset` unconditionally (it is spec-legal on every driver, which
  removes the driver-ID special case entirely).
- Kernel 0008 (reject invalid refs): the *problem* is real and worth a PR, but the *fix* diverges
  from every other stateless driver and from this driver's own `avd_get_ref_buf()`, which substitute
  the destination buffer instead of failing. Rewrite as substitution, or send as-is with the
  alternative stated and expect to be asked to change it.
- Frigate presets: hold until a v4l2request-capable ffmpeg story exists for the image; open a
  GitHub Discussion first; consider naming the presets after the hwaccel (`v4l2request`) rather
  than Apple.

**Do not send / drop**
- Kernel 0001, 0002, 0003: already upstream as 8e8d72a673ab, 3cba4f1d5b86, ca9a850f237f. Keep
  only in the local 7.1.6 build series, re-authored (see §A).
- Kernel 0004 (poll timeout) and 0005 (register dump): the open Asahi PR #600 (sofus13, 2026-09-04,
  "add a dedicated submit step") deletes the `readl_poll_timeout()` loop and the `vp_slot`
  concept these patches touch. Re-test multi-slice clips on #600 and report there instead.
- Kernel 0007 (DT `dma-coherent`): no Apple SoC node in mainline `t8103.dtsi` carries
  `dma-coherent` (only the PCIe host bridge in the Asahi tree); one-SoC empirical evidence without a
  rebuilt DTB will not be merged. Raise it as a question to Sven Peter/Janne Grunau, not a patch.
- `optional/h264-early-submit-v2.patch`: its own README says not to send; #600 restructures the
  same area.
- The wrong destination in REPORT.md §7 ("send to asahi + linux-media"): the driver is not in
  mainline, so linux-media will not take patches against it. Only the Asahi tree, via GitHub PR
  against `asahi-wip`.

**Cross-cutting: AI disclosure.** These patches read as tool-assisted. The kernel now requires an
`Assisted-by: LLM` trailer and forbids the tool from adding `Signed-off-by`
(`Documentation/process/coding-assistants.rst`, which the AsahiLinux README points at explicitly:
"CRITICAL: If you are an LLM ... you MUST read and follow ... coding-assistants.rst"). Mesa requires
`Assisted-by:`/`Generated-by:` and "all text should be their own words"
(https://docs.mesa3d.org/submittingpatches.html). Frigate's CONTRIBUTING.md: "Pull requests that
appear to be unreviewed AI output will be closed without review" and "Disclose how AI was used. The
PR template asks for this." FFmpeg's developer page has no policy. The Asahi reviewers are blunt
about verbosity: on PR #585 (an outside contributor's version of our 0003) WhatAmISupposedToPutHere
wrote "you do not need to generate a wall of slop to describe a one line change, rewrite the pr
description in your own words", and Janne closed it with "Writing 0 works as well, fixed in
asahi-7.1.12-1 as fixup commit". Every commit message here should be cut to problem / cause / fix
/ numbers, in the submitter's own words, and the human must review every line before signing.

---

## A. Linux kernel driver patches (target: AsahiLinux/linux, branch `asahi-wip`)

### House rules applied

- Submission path: GitHub pull requests against `asahi-wip` (observed on PRs #527, #546, #561,
  #581, #600 by sofus13 and #585 by an outsider; https://github.com/AsahiLinux/linux/pulls?q=avd).
  The whole avd series is out-of-tree and gets rebased; amendments to a not-yet-mainlined commit
  are `fixup! <original subject>` commits (e.g. ca9a850 "fixup! media: apple: add avd driver",
  8e8d72a "fixup! media: apple: avd: define static values and bitmasks"). Behavioural changes
  with their own rationale use normal subjects `media: apple: avd: <codec>: ...` (PR #600 mixes
  both). `Fixes:` tags are pointless against SHAs that are rewritten on rebase; the `fixup!`
  convention replaces them.
- Kernel process still applies on top: `Documentation/process/submitting-patches.rst` (DCO: "using
  a known identity (sorry, no anonymous contributions.)", line 439 of today's mainline copy),
  `coding-style.rst`, `checkpatch.pl`, and `coding-assistants.rst` for AI-assisted work.
- The driver's DT binding lives in the Asahi tree
  (`Documentation/devicetree/bindings/media/apple,avd.yaml`, maintainer "Sofus
  <sofus.c@icloud.com>"), so DT changes are also Asahi-first.
- Driver maintainer/author: sofus (sofus13, sofus.c@icloud.com); committer: Janne Grunau (jannau).

### What is already upstream (verified against `commits-7.1.12.txt` and the GitHub commit pages)

| ours | upstream | author | upstream S-o-b |
|---|---|---|---|
| 0001 transform_8x8_mode_flag | 8e8d72a673ab "fixup! media: apple: avd: define static values and bitmasks" (asahi-7.1.8-1) | sofus | `Signed-off-by: sofus <sofus.c@icloud.com>` |
| 0002 default weights for P slices | 3cba4f1d5b86 "media: apple: avd: h264: fix default weights handling" (asahi-7.1.12-1) | sofus | `Signed-off-by: sofus <sofus.c@icloud.com>` |
| 0003 VP_INSN_FIFO_MASK = 0 | ca9a850f237f "fixup! media: apple: add avd driver" (asahi-7.1.12-1) | Janne Grunau | (Janne's) |

Our 0001-0003 re-author these under `From: Aquarat` with our own `Signed-off-by` and only a prose
"Backport of ..." line. That is not acceptable attribution for a backport: the original author line
and their `Signed-off-by` must be preserved and ours appended. If the local 7.1.6 series is ever
published (e.g. in `build-and-install.sh` or a copr), regenerate them with
`git cherry-pick -x <sha>` from the 7.1.12 tree (gives "(cherry picked from commit ...)" and keeps
the author), then add your own `Signed-off-by` below theirs. checkpatch also flags 0002's citation
format: ERROR `GIT_COMMIT_ID` — use `commit 3cba4f1d5b86 ("media: apple: avd: h264: fix default
weights handling")`. None of the three should go into any upstream PR.

### Rebase target and conflicts

`asahi-wip` == asahi-7.1.12-1 for `avd-h264.c` and `avd-drv.c` today (diff -q clean). Relative to
our base (7.1.6-1) the files were clang-formatted and the RVRA/"comp" buffer handling rewritten
(`fill_rvra` -> `fill_comp`, `push_rvra` -> `push_comp`, `avd_get_ref_buf()` shared), so 0004/0005/
0006/0008 need a rebase; hunks apply with fuzz but the surrounding code changed.

PR #600 ("avd fixes/cleanup", sofus13, open since 2026-09-04, no review yet, 5 commits) is the real
obstacle. Its patch 5/5 "media: apple: avd: add a dedicated submit step" (Signed-off-by sofus)
"Removes the broken vp/fifo slot logic, but adds manual slice buffer handling to h264": in
`avd_h264_run()` it copies each slice into a driver-owned buffer (`h264_ctx->slices[]`,
`MAX_SLICES`), removes `alloc_slots()`, `ctx->vp_slot`, the watchdog scheduling from `run`, and the
entire `readl_poll_timeout(... slice_parsed ...)` block; held slices just `avd_job_finish(ctx,
VB2_BUF_STATE_DONE)` and the whole frame is submitted by `avd_submit_job()` on the last slice.

### Patch table

| patch | verdict | required changes | rejection risk |
|---|---|---|---|
| 0001 h264: fix transform_8x8_mode_flag encoding | **drop** (upstream 8e8d72a673ab) | Local series only: `git cherry-pick -x 8e8d72a673ab` so author = sofus, keep his S-o-b, add yours. Our version also adds a 4-line comment upstream doesn't have. | Certain if sent: duplicate. |
| 0002 h264: fix default weights handling for P slices | **drop** (upstream 3cba4f1d5b86) | As above; fix citation to `commit <sha> ("title")` (checkpatch ERROR GIT_COMMIT_ID). | Certain: duplicate. |
| 0003 t8103: set VP_INSN_FIFO_MASK to 0 | **drop** (upstream ca9a850f237f) | As above. PR #585 was the same one-liner from an outsider and was closed in favour of Janne's fixup. | Certain: duplicate. |
| 0004 h264: scale the held-slice parse poll timeout with the slice size | **RFC only / hold** | The polled loop no longer exists in PR #600 (`pr600.patch` lines 2092-2115 delete it). Do not open a PR for this; instead run the 1080p 4-slice and 1296p 8-slice clips against #600 and comment on #600 with the result (pass, or the new failure signature). If #600 stalls and this is sent: define the constants (`AVD_VP_PARSE_BYTES_PER_US 16`, margin 4) instead of `slice_size / 4` with a prose comment; shorten the commit message to 6-8 lines; keep the measurement line. checkpatch: clean. | High. Even without #600, a reviewer will ask why the driver polls a "bytes parsed" counter for up to `slice_size/4` us (90 ms for a 358 KiB slice) inside `device_run` instead of using the VP-done IRQ; the idiomatic fix is structural, which is what #600 does. |
| 0005 dump the VP status registers on error and timeout | **drop** | `avd_status()` is a raw register dump; upstream has been *removing* its calls (8e8d72a removed one in vp9; #600 removes the commented-out ones in hevc and the `ctx->vp_slot` this patch tests). If kept locally, guard with `dev_dbg` or a debugfs knob, not `dev_err`. checkpatch: clean. | High: conflicts with #600 and with the direction of the driver; a debug aid the maintainer did not ask for. |
| 0006 allow cacheable (non-coherent) MMAP buffers | **ready after rebase + rewrite** | (1) Drop the `dma_attrs` hunk: `asahi-wip avd-drv.c:271` already has `dst_vq->dma_attrs = 0;` (and `src_vq->dma_attrs = 0 /* DMA_ATTR_NO_KERNEL_MAPPING */;` at :258). The patch becomes two lines: `src_vq->allow_cache_hints = true;` and `dst_vq->allow_cache_hints = true;`. (2) Cut the message to: what the flag enables, why (Normal-NC read-back 250 MB/s vs 18-24 GB/s on M1), that vb2 does the maintenance in prepare/finish, clients not passing the flag are unaffected, one measurement line. Drop the paragraph about `dma_alloc_noncontiguous()` rejecting attributes (no longer relevant once the attrs hunk is gone). (3) Say why the OUTPUT queue gets it too (symmetry; the hardware reads coded data after vb2's clean; the driver's own CPU reads of the OUTPUT plane in #600 go through the cacheable kernel mapping, which is fine). (4) Real `Signed-off-by` (checkpatch ERROR BAD_SIGN_OFF, NO_AUTHOR_SIGN_OFF). Subject as-is is fine. | Low. `allow_cache_hints` is the documented opt-in (`include/media/videobuf2-core.h:508`: "when set user-space can pass cache management hints in order to skip cache flush/invalidation on ->prepare() or/and ->finish()"; `V4L2_MEMORY_FLAG_NON_COHERENT` in `vidioc-reqbufs.rst`: "This requires extra care from the driver -- it must guarantee memory consistency by issuing a cache flush/sync when consistency is needed", which vb2-dma-contig does for MMAP buffers via `vb2_dc_alloc_non_coherent()` + sync in prepare/finish). The driver never CPU-touches the CAPTURE buffer, so no driver-side sync is needed. Correctness holds regardless of whether the fabric is coherent. |
| 0007 [RFC] arm64: dts: apple: t8103: mark the AVD as dma-coherent | **RFC only; do not send as a patch** | Facts: mainline `t8103.dtsi` (fetched today) has *no* `dma-coherent` anywhere; the Asahi tree has it only on the PCIe host bridge (`t8103.dtsi:1731`, `t8112.dtsi:2008`); the avd nodes on t8103 and t8112 have `iommus = <&avd_dart 0>` and nothing else; `apple,avd.yaml` does not list `dma-coherent` (run `make dt_binding_check DT_SCHEMA_FILES=media/apple,avd.yaml` and `CHECK_DTBS=y` before claiming it validates). The property belongs on the device node, not the DART (correct placement in the patch). The evidence is one T8103, driver-side `dev->dma_coherent` override, 450 frames, not a rebuilt DTB. If the block is not coherent on some SoC/firmware the failure is silent corruption for every client, so the maintainers will want Apple's own DT/ADT evidence (does the ADT for `avd` carry a coherency flag? that is the argument to make) or a probe on T6000/T8112 too. If it is ever sent: subject via `git format-patch --rfc` = `[RFC PATCH]`, real S-o-b, and it must go with a binding update. Also note 0006 already recovers almost all of the benefit (0.08 s user + 0.12 s sys vs 0.08 + 0.03). | Very high as a patch; fine as a question in the PR for 0006 or on #asahi-dev. |
| 0008 h264: reject slices that reference invalid DPB entries | **needs changes** | The bug is real and the DART fault + 2 s watchdog reset on every mid-GOP start is a good PR. But: (a) every comparable driver substitutes rather than rejects: hantro `hantro_h264_get_ref_buf()` (`hantro_h264.c:356-368`: "if (!dma_addr) { ... dst_buf ..."), rkvdec (`rkvdec-h264.c:320-325`: "If a DPB entry is unused or invalid, address of current destination buffer is returned"), cedrus skips missing refs (`cedrus_h264.c:112-114, 221-223`), and this driver's own `avd_get_ref_buf()` (`avd-drv.c:126-132`, same comment as rkvdec) already does it for the *timestamp* lookup. The stateless API text says the decoder "will still try to produce (likely corrupted) frames" and flags them with `V4L2_BUF_FLAG_ERROR` (`dev-stateless-decoder.rst`). Returning -EINVAL from `->run()` does end in `avd_job_finish(ctx, VB2_BUF_STATE_ERROR)` (`avd-drv.c:242-244`), which is API-conformant, but reviewers will ask for the hantro form. (b) Substitution design for AVD: the slice command indexes references by DPB index (`AVD_OP_REF_DBP_IDX(sl->ref_pic_list0[i].index)`, `avd-h264.c:455/465`) while `stream_refs()` pushes headers only for VALID entries; so either push a header for every DPB slot (dst for non-VALID ones) or remap invalid list indices to the first VALID entry, and when there is none push one dst header and point all entries at it. This needs hardware testing; the -EINVAL version can be offered as the fallback. (c) Drop `num[l] > V4L2_H264_REF_LIST_LEN`: `v4l2-ctrls-core.c` already rejects `num_ref_idx_l{0,1}_active_minus1 > 31` at SET_CTRL time; keep the `idx >= V4L2_H264_NUM_DPB_ENTRIES` check. (d) Shorten the 14-line function comment (it repeats the commit message) and the message itself; use `dev_dbg_ratelimited` as now. (e) It touches `avd_h264_run()`, which #600 rewrites: rebase onto #600 once merged, or base the PR on #600's branch. (f) Real S-o-b. checkpatch: only the S-o-b errors. | Medium at Asahi (sofus may accept a defensive check that stops a DART fault), high if it ever reaches linux-media in this form. |
| optional/h264-early-submit-v2.patch | **do not send** | README already says so ("do not send upstream without more testing (changes the IRQ/submit protocol)"). #600 introduces a dedicated submit step of its own; the module parameter, `ctx->submitted` and the IRQ ordering change would conflict with it. checkpatch: clean. | Certain. |

Mechanical items for the whole kernel set: regenerate from a clean branch on `asahi-wip` with
`git format-patch --cover-letter -v1 asahi-wip` (current files say `[PATCH 1/5]`...`[PATCH 5/5]`
then bare `[PATCH]`); one consistent `user.name`/`user.email` (the ffmpeg tree is configured as
"Aaron", the kernel tree commits say "Aquarat" and "avd"); "Aquarat" is acceptable under the
"known identity" wording if it is the identity you use everywhere, but pick one; fill every
`Signed-off-by: <fill in>` yourself (the tool must not); add `Assisted-by: LLM` if applicable;
run the clip matrix on the rebased build and quote it in the PR text, briefly.

---

## B. FFmpeg patches

### House rules applied

- https://ffmpeg.org/developer.html: "Patches should be posted to Forgejo
  (https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls) or the ffmpeg-devel mailing list"; "Did you
  sign-off your patch? (`git commit -s`)"; commit format "area changed: short 1 line description /
  details describing what and why and giving references"; K&R, 4-space indent, no tabs, 80 columns
  where it helps; run `tools/patcheck`.
- `MAINTAINERS` in the tree: `hwcontext_vulkan* [2] Lynne`; `v4l2_* Jorge Ramirez-Ortiz` (that is
  the old v4l2_m2m code, not v4l2request).
- The v4l2request hwaccel is **not in FFmpeg master** (raw fetch of
  `libavutil/hwcontext_v4l2request.c` on master: HTTP 404). Jonas Karlman's series lives at
  https://code.ffmpeg.org/Kwiboo/FFmpeg (branches `v4l2request-v3` default, `v4l2-request-n8.1`
  "161 commits ahead", last update 2026-03-16; the GitHub mirror redirects there), was last posted
  as "[PATCH v2 0/8] Add V4L2 Request API hwaccels for MPEG2, H.264 and HEVC" (ffmpeg-devel,
  Aug 2024, https://ffmpeg.org/pipermail/ffmpeg-devel/2024-August/332034.html), and is shipped by
  LibreELEC as `packages/multimedia/ffmpeg/patches/v4l2-request/ffmpeg-001-v4l2-request.patch`.
  The Forgejo fork shows no PR/issue tabs, and the GitHub mirror carries FFmpeg's stock "Github
  pull requests ... will be ignored" README line. Practical route for 0001/0002: email Jonas
  (jonas@kwiboo.se, cc ffmpeg-devel) with `git send-email` against `v4l2request-v3`, or attach them
  to the next revision thread; they are too small for a separate LibreELEC PR.
- `tools/patcheck` output (appendix): only "Missing changelog entry" for all four (fine for
  minor changes), plus false-positive "possibly unused/constant variable" notes on 0003/0004.

### Patch table

| patch | verdict | required changes | rejection risk |
|---|---|---|---|
| 0001 avutil/hwcontext_v4l2request: request cacheable CAPTURE buffers | **ready** (for Kwiboo's branch) | Real S-o-b; cut the message to ~10 lines (the mechanism + one measurement). The `#ifdef V4L2_MEMORY_FLAG_NON_COHERENT` guard is right (it is a `#define` in `videodev2.h`); `struct v4l2_create_buffers.flags` exists since 5.11. One design question to pre-empt: with `-hwaccel_output_format drm_prime` the CPU never reads the buffer, yet vb2 now invalidates ~13 MB per DQBUF; consider gating on an `AVOption` of the hwdevice (e.g. `cache_hints=1` default on) so zero-copy users can turn it off. | Low. Jonas maintains an out-of-tree branch and takes practical improvements; the change is a no-op on drivers without the capability. |
| 0002 avcodec/v4l2_request: skip the cache clean when queueing CAPTURE buffers | **ready** | Real S-o-b; shorten. Correct use of `V4L2_BUF_FLAG_NO_CACHE_CLEAN` (CAPTURE buffers are never CPU-written; the DQBUF invalidate is untouched). | Low. |
| 0003 avutil/hwcontext_vulkan: import linear multi-plane DRM layers | **needs minor changes** (FFmpeg proper, Forgejo PR, reviewer Lynne) | (1) Master still has the same failure (`hwvk-master.c:3476` "Unsupported DMABUF layer format", no expansion) so the patch is still needed. (2) Motivation must use in-tree producers: v4l2request and rkmpp are not in FFmpeg; kmsgrab (one NV12 layer with two planes from a KMS framebuffer) and VAAPI `VA_EXPORT_SURFACE_COMPOSED_LAYERS` are. Rewrite the first paragraph accordingly; keep the Apple test as "tested on". (3) Correctness check done: after expansion `get_plane_wh(hwfc->sw_format, ..., i)` (`hwcontext_vulkan.c:2686`) treats layer index as plane index, which is exactly right for R8+GR88 (NV12/NV16/NV24), R16+GR1616 (P010/P012/P016, both in `vulkan_drm_format_map` at 3252/3255) and R8x3 (YUV420/422/444, chroma sizes from `log2_chroma_*`). VAAPI separate-layer descriptors (`nb_planes == 1`) return 0 untouched, so no regression for existing importers; tiled composed layers still error as before. (4) Add a sanity check that the expanded layer count equals `av_pix_fmt_count_planes(hwfc->sw_format)` and that the layer's fourcc matches `sw_format` (an NV12 descriptor on a P010 frames context would currently be split into wrong formats silently). (5) State in the message that it is untested on ANV/RADV and ask for a kmsgrab test. (6) Real S-o-b, one author name. | Medium. Lynne may prefer the expansion in the exporter or in `hwcontext_drm`, or may ask for true multi-plane `VK_FORMAT_G8_B8R8_2PLANE_420_UNORM` import instead; be ready to argue that separate images are what the rest of the Vulkan hwcontext already assumes for non-multiplane paths. |
| 0004 avutil/hwcontext_vulkan: pass DRM plane offsets through memoryOffset on Honeykrisp | **needs changes** | (1) Mesa issue first, then cite its URL in the comment and message (FFmpeg maintainers do not take vendor quirks without a tracked upstream bug). (2) Header guard is wrong: `VK_DRIVER_ID_MESA_HONEYKRISP` first appears in Vulkan-Headers **v1.3.287** (checked tags 283-291; FFmpeg's minimum is 1.3.277, `configure:7771`), so `#if VK_HEADER_VERSION >= 287`; as written the quirk silently disappears on 287-289 headers. (3) Bound it: compare `p->dprops.driverVersion` with the first fixed Mesa version once the fix lands (Mesa encodes `VK_MAKE_VERSION(major, minor, patch)`), so the quirk retires itself. (4) Stronger alternative to propose: apply the plane offset via `memoryOffset` for *all* single-plane layers on *all* drivers. The Vulkan spec defines `pPlaneLayouts[].offset` relative to the memory binding, so `memoryOffset = offset, layout.offset = 0` is legal everywhere; the patch already handles the alignment check and `req2.memoryRequirements.size += off`. That removes the driver-ID branch entirely and is what a reviewer is likely to suggest. (5) `close(idesc.fd); goto fail;` matches the existing error pattern two blocks above (no double close). (6) Note in the message that multi-plane layers (tiled) are still unhandled on Honeykrisp. (7) Real S-o-b. | Medium-high as a driver-ID quirk without an issue link; low if reworked as (4). |

Author line: the ffmpeg tree is configured as "Aaron <blue@aquarat.co.uk>" while the kernel patches
say "Aquarat"; use one identity for all four.

---

## C. Frigate (target: blakeblackshear/frigate, branch `dev`)

### House rules applied

- CONTRIBUTING.md (dev): PRs rebase on `dev`; "Code must be formatted, linted and type-tested"
  (`ruff format frigate`, `ruff check frigate`); "One concern per PR. Don't combine unrelated
  changes."; "Pull requests that appear to be unreviewed AI output will be closed without review";
  "Disclose how AI was used. The PR template asks for this."; "Review and test everything you
  submit, and be prepared to explain every line when asked."
- `docs/docs/development/contributing.md`: "Do not make large sweeping changes. Open a discussion on
  GitHub for any large or architectural ideas."
- `pyproject.toml` `[tool.ruff.lint]` ignores E501, so the long preset strings/comments are fine;
  `extend-select = ["I", ...]` means import order matters (isort). `ruff` is not installed on this
  host; run it in the frigate devcontainer before opening a PR.
- Hardware docs: `docs/docs/configuration/hardware_acceleration_video.md` has an officially supported
  section (Intel/AMD/NVIDIA/RPi) and a "# Community Supported" section (Jetson, Rockchip,
  Synaptics). `docs/docs/configuration/advanced/system.md` "Custom ffmpeg build": custom
  `ffmpeg`/`ffprobe` go in `/config/custom-ffmpeg/bin` with `ffmpeg.path`, and for NVIDIA the docs
  say "this is unsupported and may not work well if at all". Frigate bundles its own ffmpeg; every
  existing preset works with the bundled build or with an image variant Frigate builds
  (Rockchip/Synaptics).

### Patch table

| item | verdict | required changes | rejection risk |
|---|---|---|---|
| 0001 Add ffmpeg presets for Apple silicon (AVD via v4l2request, Vulkan scaling) | **needs changes; hold** | (1) Open a GitHub Discussion first: the hwaccel needs an ffmpeg that is neither in Frigate's image nor in upstream FFmpeg; maintainers will ask how a user gets one. Offer a documented recipe (the `/config/custom-ffmpeg` mechanism) under "Community Supported". (2) Docs hunk is mandatory for a new preset: add an "Apple silicon (Asahi Linux)" subsection under `# Community Supported` in `hardware_acceleration_video.md` mirroring the Rockchip layout (prerequisites: kernel with the avd driver, firmware, `/dev/video0`, `/dev/media0`, `/dev/dri/renderD128`, custom ffmpeg; configuration example). Remove the "TODO before submitting upstream" from the commit message. (3) Naming: `-hwaccel v4l2request` is not Apple-specific (the same ffmpeg branch drives Rockchip, Allwinner, RPi5). `preset-v4l2request` / `preset-v4l2request-vulkan` with Apple as the documented example has a much better chance than an Apple-only preset. (4) Python: comment blocks inside dict literals are fine for ruff; ensure `ruff format` leaves the file unchanged (the `.orig` is from June; `dev` has since changed `Optional[...]` to `X | None` and other lines, so rebase). (5) `format=nv12,format=yuv420p` at the end of the vulkan chain: justify or drop the redundant first `format`. (6) AI disclosure in the PR template. | High as filed (niche platform + external ffmpeg + no docs). Medium if positioned as a generic v4l2request preset with community-supported docs. |
| `patches/zmq_ipc.py` (retry the model handshake when the detector was down at start) | **ready with cosmetic changes** | Upstream `frigate/detectors/plugins/zmq_ipc.py` on `dev` is the same file as the `.orig` apart from typing (`list[bytes]`, `Literal` import). Upstream already re-creates the socket and re-initialises the model on `zmq.Again`/`zmq.ZMQError` in `detect_raw()` (dev lines 319-334) but gives up forever if the *first* `_initialize_model()` fails (`_model_ready` stays False, warning per frame). The patch follows the same `_create_socket(); _initialize_model()` pattern, so it is in style. Changes: drop the "Local patch (<nvr-host>, 2026-09-05)" comment (keep one sentence of intent); make the interval a module constant (`ZMQ_MODEL_RETRY_INTERVAL_S = 10.0`) or a config field on `ZmqDetectorConfig`; wrap the retry in `try/except Exception: pass` like the existing reset paths; `import time` is already in isort order. Separate PR from the presets. | Low. |
| `record/maintainer.py:160` `if process.name() != "ffmpeg": continue` | **propose** | `psutil.Process.name()` is the 15-char `comm`, so any launcher/loader wrapper (our `host-lib64-ld.s`) is invisible and its in-progress segments get validated and deleted. Upstream-acceptable form keeps the cheap name filter and falls back to argv[0]: `name = process.name(); if name != "ffmpeg": cmdline = process.cmdline(); if not cmdline or os.path.basename(cmdline[0]) != "ffmpeg": continue` (inside the existing `try/except psutil.Error`). That also covers `exec -a ffmpeg` wrappers and custom `ffmpeg.path` builds whose binary is named differently only if the wrapper sets argv[0], so mention `ffmpeg.path` in the docs note. Frame it as a bug ("custom ffmpeg wrappers cause 'Invalid recording segment detected' every 10 s") with the reproduction from AVD_FRIGATE_INTEGRATION.md. | Low; small, self-contained, and it is a real footgun for the documented custom-ffmpeg feature. |

---

## D. Mesa (Honeykrisp ignores `pPlaneLayouts[].offset`)

Confirmed in `src/asahi/vulkan/hk_image.c` (Mesa3D/mesa `main`, fetched 2026-09-05):

- `hk_image_init()`: for `VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT` with
  `VkImageDrmFormatModifierExplicitCreateInfoEXT` only `pPlaneLayouts[plane].rowPitch` is consumed
  (`linear_stride_B = mod_explicit_info->pPlaneLayouts[plane].rowPitch;`, ~line 880); `.offset` is
  never read.
- `hk_BindImageMemory2()` -> `hk_image_plane_bind()` (~1317-1374): `offset_B =
  pBindInfos[i].memoryOffset`, then `*offset_B = align64(*offset_B, HK_PLANE_ALIGN_B)` (128) and,
  for non-disjoint multi-plane images, planes are laid out consecutively by `layout.size_B`. So
  both cases are wrong: a single-plane image whose explicit layout offset is non-zero is read from
  `memoryOffset`, and an NV12 image with explicit per-plane offsets ignores them. Note the silent
  `align64()` on `memoryOffset`, which an FFmpeg-side quirk must respect (0004's alignment check
  does).
- `hk_GetImageSubresourceLayout2` (~1248-1275) reports offsets computed from the internal layout,
  so it will also disagree with what the importer asked for.

Rules (https://docs.mesa3d.org/submittingpatches.html, https://docs.mesa3d.org/bugs.html): issues
and MRs on GitLab only ("Please do NOT submit your patches in email"); subject prefix by driver, so
`hk: honour explicit DRM format modifier plane offsets`; `Fixes: <sha> ("...")` pointing at the
commit that added modifier support in hk (find with `git log -S pPlaneLayouts src/asahi/vulkan/`);
a bug fix under 100 lines that landed in main is stable-eligible, use `Backport-to: 26.1`; MR must
pass CI and be bisectable; `Assisted-by:`/`Generated-by:` if AI helped and text in your own words;
bug reports need Mesa version, hardware, driver output (`vulkaninfo` here), and "the easier a bug is
to reproduce, the sooner it will be fixed" (a standalone reproducer, not an ffmpeg pipeline).

Draft issue (title: `hk: VkImageDrmFormatModifierExplicitCreateInfoEXT pPlaneLayouts[].offset is
ignored on import`):

> **System:** Apple M1 (T8103), Mesa 26.1.8 Honeykrisp (`VK_DRIVER_ID_MESA_HONEYKRISP`), Fedora Asahi
> Remix 44, kernel 7.1.6-400.asahi.fc44.aarch64+16k.
>
> **Description:** When an image is created with `VK_IMAGE_TILING_DRM_FORMAT_MODIFIER_EXT`,
> `drmFormatModifier = DRM_FORMAT_MOD_LINEAR` and a `VkImageDrmFormatModifierExplicitCreateInfoEXT`
> whose `pPlaneLayouts[0].offset` is non-zero, then bound to imported dma-buf memory with
> `memoryOffset = 0`, the image is sampled from offset 0 of the memory instead of the requested
> offset. `hk_image_init()` only reads `pPlaneLayouts[plane].rowPitch`
> (`src/asahi/vulkan/hk_image.c`, `linear_stride_B = ...->pPlaneLayouts[plane].rowPitch`) and
> `hk_image_plane_bind()` positions every plane at `memoryOffset` (rounded up to 128) plus the
> cumulative `size_B` of the preceding planes. The same applies to multi-plane images created with
> explicit per-plane offsets.
>
> **Reproducer:** one dma-buf of 2 x (width*height) bytes; create two `VK_FORMAT_R8_UNORM` linear
> images with explicit layouts `{offset 0, rowPitch width}` and `{offset width*height, rowPitch
> width}`, import the fd twice, bind both at `memoryOffset 0`, copy each image to a host buffer:
> both copies return the first half. Expected: the second returns the second half. (Real-world
> trigger: FFmpeg `hwcontext_vulkan` importing an NV12 V4L2 capture buffer as R8 + GR88 layers,
> which yields correct luma and garbage chroma; radv/anv render the same input correctly.)
>
> **Workaround:** bind at `memoryOffset = offset` with `pPlaneLayouts[].offset = 0`.
>
> **Suggested fix:** store `pPlaneLayouts[plane].offset` per plane at create time and add it in
> `hk_image_plane_bind()` (and do not accumulate `size_B` for planes that carry an explicit offset);
> report it back from `hk_GetImageSubresourceLayout2`. dEQP: `dEQP-VK.drm_format_modifiers.*`.

If we submit the MR ourselves: test with `meson test` and the dEQP group above on the M1, one
commit, `Fixes:` + `Backport-to: 26.1`, and reference the FFmpeg patch (0004) in the MR text so the
version bound in FFmpeg can be set to the fixed release.

---

## E. Licensing and attribution

- **Kernel.** No new files, so no new SPDX headers are needed. Touched files: `avd-h264.c`,
  `avd-drv.c` (`SPDX-License-Identifier: GPL-2.0`, copyright "The Asahi Linux Contributors",
  "2023 Eileen Yoon", Rockchip, Google), `avd-hw.c` (`MIT`), `t8103.dtsi` (`GPL-2.0+ OR MIT`).
  Pre-existing nits, not ours: the .c files use the deprecated `GPL-2.0` identifier and `/* */`
  instead of `//` (`Documentation/process/license-rules.rst`); do not "fix" them in a functional
  patch. Our added code is original; 0001/0002 cite eiln's HAL as the *reference* for a bit
  position and a condition, which is not copying, and eiln/avd is MIT (`src/avd-analysis/eiln-avd/
  LICENSE`, Copyright (c) 2023 Eileen Yoon) with the driver already crediting her, so even copied
  lines would be licence-compatible. Nothing was taken from the Apple kext (the analysis used
  firmware traces and Janne's value). Firmware: `src/avd-fw` is `MIT`, "Copyright The Asahi Linux
  Contributors" (not Apple's blob), so installing it to `/lib/firmware/apple/` and redistributing
  it is fine. DCO: the human submitter signs; a pseudonym is acceptable under "known identity"
  (`submitting-patches.rst:439`), an empty or `<fill in>` sign-off is not.
- **FFmpeg.** `hwcontext_vulkan.c`, `hwcontext_v4l2request.c`, `v4l2_request.c` are LGPL-2.1+;
  patches add no files and no third-party code. Built binaries: `/opt/ffmpeg-vk/bin/ffmpeg` and
  `/opt/ffmpeg-avd/bin/ffmpeg` were configured with `--enable-gpl --enable-version3 --enable-libx264
  --enable-v4l2-request --enable-libdrm --enable-libudev` (+ `--enable-vulkan --enable-libshaderc`
  for the vk build), i.e. GPLv3 binaries with no `--enable-nonfree`; redistributable under GPLv3
  with source offer if ever shared. Not planned; noted.
- **libva-v4l2_request** (`src/libva-v4l2_request`, GPL-3.0) is a reference only and is not part
  of the deployed chain (ffmpeg drives the Request API directly), so no LGPL/GPL mixing issue.
- **Frigate** is MIT; the plugin/preset changes carry no header requirement. `ffmpeg-avd-vk/`
  wrapper scripts are local.
- **Mesa** is MIT; an MR needs no header changes.

---

## Appendix: raw tool output (trimmed)

### checkpatch.pl (`--no-tree --strict --show-types`, kernel 7.1.6 build tree, v0.32)

```
0001: WARNING:UNKNOWN_COMMIT_ID: Unknown commit id '8e8d72a673ab', maybe rebased or not pulled?
      total: 0 errors, 1 warnings, 0 checks, 20 lines checked
0002: ERROR:GIT_COMMIT_ID: Please use git commit description style 'commit <12+ chars of sha1> ("<title line>")'
      WARNING:UNKNOWN_COMMIT_ID: Unknown commit id '3cba4f1d5b86'
      total: 1 errors, 1 warnings, 0 checks, 36 lines checked
0003: WARNING:UNKNOWN_COMMIT_ID: Unknown commit id 'ca9a850f237f'
      total: 0 errors, 1 warnings, 0 checks, 8 lines checked
0004: total: 0 errors, 0 warnings, 0 checks, 19 lines checked  (no obvious style problems)
0005: total: 0 errors, 0 warnings, 0 checks, 16 lines checked  (no obvious style problems)
0006: ERROR:BAD_SIGN_OFF: Unrecognized email address: '<fill in>'
      ERROR:NO_AUTHOR_SIGN_OFF: Missing Signed-off-by: line by nominal patch author 'Aquarat <blue@aquarat.co.uk>'
      total: 2 errors, 0 warnings, 0 checks, 22 lines checked
0007: ERROR:BAD_SIGN_OFF / ERROR:NO_AUTHOR_SIGN_OFF (as 0006); total: 2 errors, 7 lines checked
0008: ERROR:BAD_SIGN_OFF / ERROR:NO_AUTHOR_SIGN_OFF (as 0006); total: 2 errors, 66 lines checked
optional/h264-early-submit-v2.patch: total: 0 errors, 0 warnings, 0 checks, 79 lines checked
```

(The UNKNOWN_COMMIT_ID warnings are expected outside a tree containing the Asahi commits.)

### FFmpeg tools/patcheck

```
0001-avutil-hwcontext_v4l2request-request-cacheable-CAPTU.patch
  Missing changelog entry (ignore if minor change)
0002-avcodec-v4l2_request-skip-the-cache-clean-when-queue.patch
  Missing changelog entry (ignore if minor change)
0003-avutil-hwcontext_vulkan-import-linear-multi-plane-DR.patch
  possibly unused variables / possibly never written: drm_fourcc, nb_planes, in, expanded_desc
  possibly never read: nl                       (all false positives: struct fields / out-params)
  Missing changelog entry (ignore if minor change)
0004-avutil-hwcontext_vulkan-pass-DRM-plane-offsets-throu.patch
  possibly constant: offset_via_bind            (false positive: set under #if)
  Missing changelog entry (ignore if minor change)
```

### Upstream facts checked today

- `asahi-wip` `avd-h264.c`/`avd-drv.c` identical to asahi-7.1.12-1; `readl_poll_timeout(..., 5, 1000)`
  still at `avd-h264.c:753`; `dst_vq->dma_attrs = 0` at `avd-drv.c:271`; no `allow_cache_hints`.
- PR #600 (open) deletes that poll loop and `vp_slot`; PR #585 (closed) history quoted in the summary.
- Mainline `t8103.dtsi`: zero occurrences of `dma-coherent`; Asahi `t8103.dtsi:1731` and
  `t8112.dtsi:2008` are the PCIe host bridges.
- FFmpeg master: no `hwcontext_v4l2request.c` (404); `hwcontext_vulkan.c` still errors on
  multi-plane layers and has no Honeykrisp handling.
- Vulkan-Headers: `VK_DRIVER_ID_MESA_HONEYKRISP` absent in v1.3.286, present from v1.3.287.
- Frigate dev: `zmq_ipc.py` matches `.orig` modulo typing; `maintainer.py:160` unchanged;
  ruff ignores E501.
