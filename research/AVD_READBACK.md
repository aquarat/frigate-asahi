# AVD hardware decode — fixing the decoded-frame read-back cost

Date: 2026-09-05. Machine: M1 Mac mini (T8103), Fedora Asahi 44, kernel
`7.1.6-400.asahi.fc44.aarch64+16k`, driver tree `~/frigate/src/avd-driver`
(branch `series` = pristine `asahi-7.1.6-1` + `patches/final/0001-0006`), ffmpeg
`Kwiboo/FFmpeg` branch `v4l2-request-n8.1` @ `b57fbbe50c9b2656fad86a1a7eeabfd2b2a50935`
+ `patches/ffmpeg/0001-0002`, built the Phase-0 way (debian:bookworm container),
installed at `/opt/ffmpeg-avd` (previous build kept as `/opt/ffmpeg-avd-v1`).
Companion documents: `AVD_PHASE0.md` (ffmpeg/firmware build), `AVD_ROOTCAUSE.md`
and `src/avd-driver/RESULTS.md` (the driver correctness fixes 0001-0005).

## Result in one table

Frigate detect chain, `cam2-10s.h264` (real IP-camera stream, 2304x1296 High CABAC,
150 frames), run inside `frigate:latest` with `-benchmark`
(`-i cam2-10s.h264 -vf fps=5,scale=640:360 -f rawvideo -pix_fmt yuv420p -`,
output md5 `ee810b0f…` = software in every row):

| configuration | user | sys | user+sys | wall |
|---|---|---|---|---|
| **before**: hw `-hwaccel v4l2request`, driver 0001-0005, ffmpeg v1 | 2.97 s | 0.09 s | 3.06 s | 2.82 s |
| **after**: hw, driver 0001-0006 (`apple-avd-fixed.ko`), ffmpeg with patches 1+2 | 0.34-0.42 s | 0.07-0.11 s | **0.41-0.52 s** | 0.59-0.61 s |
| software, `/opt/ffmpeg-avd` (8.1), default threads | 0.76 s | 0.04 s | 0.80 s | 0.27 s |
| software, `-threads 2` (Frigate's per-camera default) | 0.62 s | 0.02 s | 0.65 s | 0.28 s |
| software, `-threads 1` | 0.53 s | 0.01 s | 0.54 s | 0.37 s |
| software, image's own ffmpeg 7.0, default threads / `-threads 2` | 0.81 / 0.73 s | 0.04 / 0.02 s | 0.85 / 0.75 s | 0.29 / 0.40 s |

Hardware decode went from 4-5x the CPU of software decode to **~60 % of the
software CPU** (vs `-threads 2`, what Frigate uses) with bit-identical output;
decode+read-back alone (no filters) is now 0.15 s of CPU for 150 frames vs 0.36 s
(software, 1 thread) — the rest of the hardware path's CPU is swscale.

Four concurrent streams (`conc_test.sh hw 4`, final module + ffmpeg): each process
0.36-0.38 s user + 0.07-0.09 s sys (was 3.0-3.2 s user), all md5-identical; wall
2.7 s for all four because the single H.264 VP serialises them at ~270 fps (a live
15-20 fps stream never hits that).

The wall time of the hardware path (0.59 s = 150 frames at ~255 fps) is the decoder's
throughput, not CPU; irrelevant for live streams.

## 1. Characterisation

### How the buffers are allocated and mapped

- `avd_queue_init()` (avd-drv.c): both queues use `vb2_dma_contig_memops`,
  `VB2_MMAP | VB2_DMABUF`; the CAPTURE queue had `dma_attrs = DMA_ATTR_NO_KERNEL_MAPPING`,
  `bidirectional = true` (the decoder also reads the buffers as reference frames), and
  neither queue set `allow_cache_hints`.
- The `avd@268000000` node in `t8103.dtsi` has `iommus = <&avd_dart 0>` and **no
  `dma-coherent`**; the only node with `dma-coherent` in the whole T8103 DT is the PCIe
  root complex. So `dev_is_dma_coherent()` is false and, through `dma-iommu`,
  `dma_alloc_attrs()` gives a `pgprot_dmacoherent()` = **Normal-NC (uncached)** kernel
  mapping (`iommu_dma_alloc_remap()`, which also ignores `DMA_ATTR_NO_KERNEL_MAPPING`),
  and `dma_mmap_attrs()` / the dma-buf mmap give user space the same **Normal-NC**
  mapping.
- The CAPTURE plane for 2304x1296 NV12 is 13,162,496 bytes (`sizeimage` = 4,478,976
  bytes of NV12 + the driver's per-buffer decoder metadata, `ctx->rvra.size`).
- ffmpeg (Kwiboo hwaccel, `libavutil/hwcontext_v4l2request.c`): `transfer_data_from()`
  → `v4l2request_map_frame()` mmaps each exported dma-buf **per frame** (`PROT_READ`),
  calls `DMA_BUF_IOCTL_SYNC` (a no-op in vb2-dma-contig: `begin_cpu_access` is empty)
  and copies with `av_frame_copy()` → `av_image_copy()` → per-line `memcpy`. It does
  not use `av_image_copy_uc_from()` at all; and that function only has an x86 SSE4
  path anyway (`libavutil/imgutils.c`), on aarch64 it is plain `memcpy`.

### Read-back bandwidth (`src/avd-driver/readback/rb_bench.c`)

`sudo ./rb_bench [noncoherent] [iters]` allocates one CAPTURE buffer (`REQBUFS`
MMAP, optionally with `V4L2_MEMORY_FLAG_NON_COHERENT`), maps it via the video fd
and via its exported dma-buf and times several copy loops over the whole 13.2 MB
plane (raw output in `readback/run-*.txt`):

| mapping | memcpy | NEON `ld1 x4` 64 B | 128 B | 256 B | `ldnp/stnp` | read-only u64 sum |
|---|---|---|---|---|---|---|
| in-tree driver, coherent = **Normal-NC** | 225-240 MB/s (55-59 ms) | 245 | 247 | 249 | 246-251 | 116-124 MB/s |
| driver 0006, `V4L2_MEMORY_FLAG_NON_COHERENT` = cacheable | 17.9 GB/s (0.74 ms) | 21.1 | 23.8 | 18.6 | 23.9 | 18.8 GB/s |
| plain RAM → RAM reference | 22-31 GB/s (0.4-0.6 ms) | | | 22-26 | | |
| dma-coherent experiment module, plain coherent request | 29 GB/s (0.45 ms) | | | | | |

Conclusions: (1) the uncached mapping reads at ~250 MB/s no matter how the loop is
written — wider NEON loads, more loads in flight or non-temporal loads change
nothing, each 64-byte line is a serialised DRAM transaction on this SoC; that is
~18 ms per 4.5 MB frame, exactly the 2.8 s / 150 frames measured before. (2) The
per-frame dma-buf `mmap`+`munmap` costs 0.04-0.06 ms, negligible. (3) A cacheable
mapping is ~80-100x faster. So step 3 of the plan (a NEON/streaming
`av_image_copy_uc_from` for aarch64) **cannot help on Apple silicon** and was
dropped after this measurement; the fix has to be a cacheable mapping.

## 2. Kernel-side options tried

### (a) `allow_cache_hints` alone — FAILED (fixed in the same patch)

Setting `allow_cache_hints = true` on the CAPTURE queue and requesting
`V4L2_MEMORY_FLAG_NON_COHERENT` hit
`WARNING: kernel/dma/mapping.c:805 dma_alloc_noncontiguous+0x2ac` and `REQBUFS`
failed with `ENOMEM` ("dma alloc of size 13172736 failed"): `dma_alloc_noncontiguous()`
rejects every attribute except `DMA_ATTR_ALLOC_SINGLE_PAGES`, and the queue passes
`DMA_ATTR_NO_KERNEL_MAPPING`. Harmless (graceful failure), but it means the attribute
must be dropped from the CAPTURE queue. It was ineffective on this platform anyway
(see above).

### (b) `allow_cache_hints` + `dma_attrs = 0` on the CAPTURE queue — CHOSEN (`patches/final/0006`)

`apple-avd-cachehints.ko` == `apple-avd-fixed.ko` (md5 `c9c5f85b…`). The queue now
reports `V4L2_BUF_CAP_SUPPORTS_MMAP_CACHE_HINTS` (`caps 0x55`), and a client that
passes `V4L2_MEMORY_FLAG_NON_COHERENT` gets `dma_alloc_noncontiguous()` memory
mapped cacheable, with videobuf2 doing `dma_sync_sgtable_for_device()` (clean) at
QBUF and `dma_sync_sgtable_for_cpu()` (invalidate) at DQBUF. Correctness does not
depend on any assumption about the fabric. Clients that do not pass the flag
(GStreamer, unpatched ffmpeg) get exactly the old coherent allocation.

Verification: full clip matrix with GStreamer (`test_clips.sh`, coherent buffers):
25 OK-identical, 1 SKIP(empty), 0 FAIL (`readback/test_clips-cachehints.log`);
ffmpeg with the non-coherent flag: `-f framemd5` of all 150 frames of cam2-10s
identical to software (`readback/sw-framemd5-cam2.txt`, md5 of the md5 list
`6b7805c5c3d8`) in 3 runs with ffmpeg patch 1, 3 runs with patches 1+2, and 2 more
on the final module; the Frigate chain md5 `ee810b0f…` in every run (single and 4x
concurrent). No dmesg output from the driver at any point.

### (c) Is the AVD IO-coherent? — YES, empirically (`patches/final/0007`, RFC)

If the decoder's DMA is coherent with the CPU caches, the DT property
`dma-coherent` on the avd node gives cacheable mappings to *every* client with no
cache maintenance at all. The DTB cannot be changed without a reboot, so the
equivalent was tested in the driver: `avd->dev->dma_coherent = true` at probe (this
is all `arch_setup_dma_ops()` does with the property on an IOMMU device):

- `apple-avd-cohprobe.ko` (branch `coh-probe`): additionally, before every decode the
  driver reads the first 2 MiB of the destination buffer through the (now cacheable)
  kernel mapping, and after the decode-done IRQ compares — on the same CPU, via
  `smp_call_function_single()` when the IRQ thread ran elsewhere — the contents seen
  through the caches with the contents after `dc civac` over the range. Result:
  **0 stale lines in 450 frames** (3 x cam2-10s; 298 of the comparisons were
  cross-CPU IPIs, all P-core/E-core combinations occurred), and every frame md5
  identical to software. The driver's own CPU-written instruction/firmware buffers
  were cacheable in that build as well and the decoder consumed them correctly, so
  device reads are coherent too.
- `apple-avd-dmacoherent.ko` (branch `dma-coherent`, no probe code): unpatched ffmpeg
  v1 (coherent request) gets cacheable buffers → Frigate chain 0.40-0.42 s user +
  0.07-0.09 s sys; ffmpeg with patches 1+2 → 0.37-0.40 s user + 0.09-0.11 s sys;
  `-f null -` 0.08-0.09 s user + 0.07-0.08 s sys; framemd5 identical (2 runs); 4x
  concurrent 0.375-0.386 s user each.

So the dma-coherent variant is *not* measurably faster than the non-coherent
variant on this chain: the cache maintenance of the 13 MB planes is cheap (see the
sys column), the remaining sys time is ioctls, the per-frame mmap and the page
faults of the freshly allocated 4.5 MB system-memory frames. Its advantage is that
every client benefits without changes (GStreamer's `videoconvert` too). It is
provided as an RFC patch because the coherency was only established empirically on
one T8103, not with a rebuilt DTB, and not for T6000/T8112; the safe, standard
mechanism (0006 + the ffmpeg patches) is the recommended one and works with either.

## 3. User-space (ffmpeg) side

Patches against Kwiboo `b57fbbe50c9b2656fad86a1a7eeabfd2b2a50935` in
`src/avd-driver/patches/ffmpeg/`:

1. `0001-avutil-hwcontext_v4l2request-request-cacheable-CAPTU.patch`: set
   `V4L2_MEMORY_FLAG_NON_COHERENT` in `VIDIOC_CREATE_BUFS` for CAPTURE buffers when the
   queue reports `V4L2_BUF_CAP_SUPPORTS_MMAP_CACHE_HINTS`. This is the patch that gives
   the 2.97 s → 0.36-0.40 s user change. (`readback/ffmpeg-p1.diff`)
2. `0002-avcodec-v4l2_request-skip-the-cache-clean-when-queue.patch`: QBUF CAPTURE
   buffers with `V4L2_BUF_FLAG_NO_CACHE_CLEAN` (the CPU never writes them). Saves
   ~0.04 s sys per 150 frames (sys 0.11-0.14 → 0.07-0.08 s), user unchanged.

Not done, with reason: an aarch64 NEON `av_image_copy_uc_from()` — measured useless
here (section 1), and `hwcontext_v4l2request.c` does not even call it.

Build: same recipe as `AVD_PHASE0.md` (bookworm container, same configure line, the
existing configured tree + incremental `make -j8`; logs `readback/ffmpeg-build-p{1,2}.log`).
The binary still reports `ffmpeg version 8.1-avd-b57fbbe` (no configure re-run);
tell them apart by md5: patched `/opt/ffmpeg-avd/bin/ffmpeg` `d9d43b42…`, previous
`/opt/ffmpeg-avd-v1/bin/ffmpeg` `d6d2f906…`. `ldd` inside `frigate:latest` unchanged
(nothing "not found"). The source tree `src/FFmpeg-v4l2request` is on branch
`avd-readback` (2 commits on top of `b57fbbe`).

## 4. Where the remaining CPU goes (final module + ffmpeg, 150 frames)

| step (`-hwaccel v4l2request -i cam2-10s.h264 …`) | user | sys | note |
|---|---|---|---|
| decode only (`-hwaccel_output_format drm_prime -f null -`) | 0.013 s | 0.031 s | decoder ~255 fps |
| decode + read-back (`-f null -`) | 0.07-0.09 s | 0.07-0.12 s | 150 x 4.5 MB copy at ~20 GB/s + page faults of the fresh sw frames + ioctls/mmap |
| + `fps=5` + rawvideo out (no scale) | 0.12 s | 0.10 s | |
| + `scale=640:360` (bicubic, NV12 → yuv420p), i.e. the Frigate chain | 0.34-0.42 s | 0.07-0.11 s | **swscale of 30 frames ≈ 0.25 s = 8 ms/frame is now the biggest item** |
| software decode `-f null -`, 1 thread / default | 0.36 / 0.72 s | 0.01 / 0.03 s | for comparison |

Scaler options (hardware path, same 30 output frames): `scale=640:360:flags=bilinear`
0.28 s user, `flags=fast_bilinear` **0.18 s user** (software decode with the same
flag: 0.44 s), `format=yuv420p,scale` 0.37 s, scaling to nv12 0.40 s. The default
bicubic is what Frigate uses; changing it changes the detect input pixels (different
md5), which is a Frigate-config decision (`output_args.detect`), not part of this work.

Sub-stream size (`t_640x360_base.h264`, `scale` is a no-op): hw 0.006 s user +
0.008 s sys, sw 0.010 s user + 0.002 s sys — at 640x360 hardware decode is a wash;
the win is for cameras whose detect stream is the main stream.

## 5. How to reproduce

```bash
cd ~/frigate/src/avd-driver
sudo fuser /dev/video0 /dev/media0           # must print nothing
./reload.sh apple-avd-fixed.ko               # driver with 0001-0006
sudo readback/rb_bench coherent 20           # ~250 MB/s (uncached, default request)
sudo readback/rb_bench noncoherent 20        # caps 0x55, ~20 GB/s
readback/e2e.sh "hw" /opt/ffmpeg-avd/bin/ffmpeg hw            # Frigate chain, md5 ee810b0f…, bench line
readback/e2e.sh "sw" /opt/ffmpeg-avd/bin/ffmpeg sw -threads 2
CHAIN="-f null -" readback/e2e.sh "hw null" /opt/ffmpeg-avd/bin/ffmpeg hw
./conc_test.sh hw 4
# per-frame correctness against readback/sw-framemd5-cam2.txt:
sudo timeout -s KILL 60 /opt/ffmpeg-avd/bin/ffmpeg -v error -hwaccel v4l2request \
  -i ~/frigate/research/cam2-10s.h264 -pix_fmt yuv420p -f framemd5 - | grep -v '^#' | cut -d, -f6 | md5sum   # 6b7805c5c3d8…
./test_clips.sh                              # GStreamer matrix (coherent buffers), expect no regression
./reload.sh apple-avd-dmacoherent.ko         # the DT dma-coherent equivalent (experiment)
./reload.sh apple-avd-cohprobe.ko && sudo dmesg | grep COHPROBE   # the coherency probe
./restore.sh                                 # back to the in-tree module (system is left like this)
```
The `e2e.sh` runs use `docker run --device /dev/video0 --device /dev/media0` with
`frigate:latest`; the container itself was never touched.

## 6. What remains

> **Update 2026-09-05 (later the same day)**: the GPU-scaling consumer and the Frigate cut-over preparation are done —
> `AVD_FRIGATE_INTEGRATION.md` (ffmpeg patches 0003-0004, driver patch 0008 for the stream-start fault, numbers, staging).

- Upstream: 0006 is ready as-is (one `Signed-off-by` to fill in). 0007 is an RFC and
  should be discussed with the Asahi people (is the AVD known to be IO-coherent, also
  on T6000/T8112?); if accepted it makes GStreamer clients fast as well. The two
  ffmpeg patches are ready for Kwiboo's `v4l2-request-n8.1` branch (they are generic:
  any v4l2request driver with `allow_cache_hints` benefits, e.g. Rockchip/Hantro
  drivers that already set it).
- A tiny videobuf2 improvement would be to mask `DMA_ATTR_NO_KERNEL_MAPPING` in
  `vb2_dc_alloc_non_coherent()` instead of letting `dma_alloc_noncontiguous()` warn
  and fail; not done here (would need rebuilding `videobuf2-dma-contig`).
- The next CPU item on the hardware path is swscale (8 ms/frame bicubic NV12 →
  yuv420p 640x360). Options: `flags=fast_bilinear` in Frigate's detect output args
  (2x cheaper, different pixels), or a consumer that scales on the GPU from the
  dma-buf (no read-back at all: decode-only is 0.04 s CPU per 150 frames).
- ffmpeg allocates a fresh 4.5 MB system-memory frame per transferred frame (page
  faults show up as sys time); a frame pool in `av_hwframe_transfer_data()`'s
  callers would shave a bit more, not pursued.
- Frigate cut-over (Phase 3 of `AVD_PHASE0.md`) is still not done: `ffmpeg.path:
  /opt/ffmpeg-avd`, `hwaccel_args: -hwaccel v4l2request`, pass `/dev/video0` and
  `/dev/media0`, and the module must be the patched one (`README.md` "Making it
  persist").
