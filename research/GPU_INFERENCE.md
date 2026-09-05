# GPU inference for YOLOv9-t (320x320) on Apple M1 / Fedora Asahi 44

Host: Mac mini M1 (T8103, 4P+4E cores, 16 GB), Fedora Asahi Remix 44, kernel 7.0.13-400.asahi, Mesa 26.1.8, headless.
Model: `fox-person-cat-320.onnx` (Ultralytics YOLOv9t export, opset 12, input `images` 1x3x320x320 f32, output `output0` 1x8x2100, DFL decode in-graph, no NMS; 663 nodes / 186 Conv).
Date: 2026-09-05. All scripts/binaries referenced below live in this directory.

## 1. What was installed (dnf, no kernel/boot/systemd changes)

```
sudo dnf install -y mesa-vulkan-drivers vulkan-tools vulkan-loader vulkan-loader-devel vulkan-headers \
  mesa-libOpenCL mesa-libOpenCL-devel clinfo ocl-icd ocl-icd-devel opencl-headers clpeak \
  ncnn ncnn-devel python3.12 python3.12-devel python3.13 python3.13-devel uv glslc glslang \
  cmake ninja-build protobuf-devel protobuf-compiler python3-opencv gdb
```
Side effect: mesa-dri-drivers/libGL/libEGL/libgbm were upgraded 26.1.4 -> 26.1.8 (driver packages must match mesa).

Installed versions: mesa-vulkan-drivers 26.1.8 (Honeykrisp, ICD `/usr/share/vulkan/icd.d/asahi_icd.aarch64.json`), mesa-libOpenCL 26.1.8 (rusticl, ICD `/etc/OpenCL/vendors/rusticl.icd`), vulkan-tools 1.4.341, clinfo 3.0.25, clpeak 2.1.1, ncnn 20250916 (Fedora, NCNN_VULKAN=ON, SIMPLEVK), python3.12/3.13, uv 0.12.3.

## 2. Verification (headless, as the login user, no env vars needed)

* `/dev/dri/renderD128` is `crw-rw-rw- root render` (world rw), so no group membership is needed. `card1/card2` are `video`-only; Mesa prints a harmless `Opening /dev/dri/cardN failed: Permission denied` and uses the render node.
* `RUSTICL_ENABLE` is NOT required on Fedora 44: rusticl exposes the asahi device by default. Setting `RUSTICL_ENABLE=asahi` gives identical output.

`vulkaninfo --summary` (full output in `vulkaninfo_summary.txt`, `vulkaninfo_full.txt`):
```
GPU0: apiVersion 1.4.354  driverVersion 26.1.8  deviceName = Apple M1 (G13G B1)
      driverID = DRIVER_ID_MESA_HONEYKRISP  driverInfo = Mesa 26.1.8  conformanceVersion = 1.4.0.0
GPU1: llvmpipe (LLVM 22.1.8, 128 bits)   (CPU fallback device, ignore)
```
Relevant Honeykrisp features: shaderFloat16 / shaderInt8 / shaderInt16 = true, storageBuffer16BitAccess / 8BitAccess = true, VK_KHR_shader_float16_int8, VK_KHR_16bit_storage, VK_KHR_8bit_storage, VK_KHR_shader_integer_dot_product, subgroupSize 32, maxComputeWorkGroupInvocations 1024, maxComputeSharedMemorySize 32 KB, heap 15.12 GiB (unified). No cooperative matrix, no fp64, no bf16.

`clinfo -l` (full in `clinfo_full.txt`):
```
Platform #0: rusticl
 `-- Device #0: Apple M1 (G13G B1)     OpenCL 3.0, OpenCL C 1.2, 8 CUs @ 1278 MHz, cl_khr_fp16, image support, unified memory
```
rusticl prints `Patched Mesa libclc not detected ... may contain known bugs` on every start (Fedora ships upstream libclc 22.1.8).

`clpeak` (files `clpeak.txt`, `clpeak_full.txt`, `clpeak_latency.txt`):
```
Vulkan  Apple M1: fp32 2258-2506 GFLOPS, fp16 2256-2411 GFLOPS (same rate as fp32), int32 650 GOPS, int8-dp 235 GOPS
        global mem bw 51-58 GB/s, local mem bw 0.8-1.2 TB/s
        kernel launch latency: dispatch 98.7 us, roundtrip 170.8 us
OpenCL  Apple M1: kernel launch latency: dispatch 27.8 us, roundtrip 261.7 us
```
So the GPU compute path is real (2.5 TFLOPS ~= M1 8-core theoretical 2.6), but per-dispatch cost is ~100 us, which is what limits small-CNN inference (see below).

## 3. Runtime evaluation

Python: system python 3.14 works for everything needed (`uv venv --python 3.14 venv314`; onnxruntime 1.29.0, ncnn 1.0.20260526, mnn 3.6.1, apache-tvm 0.26.0 all have cp314 manylinux aarch64 wheels). IREE (3.11.0) was installed into a python 3.12 venv (`venv_iree`) but also resolves for 3.14.

### 3.1 Baseline: onnxruntime CPU EP (`bench_ort_cpu.py`)
```
source venv314/bin/activate
python bench_ort_cpu.py ~/frigate/config/model_cache/fox-person-cat-320.onnx 4
  threads=1: median 37.2 ms   threads=4: median 12.0 ms (83 fps)   threads=8: median 48 ms (worse: E-cores + Frigate load)
python bench_ort_cpu.py .../yolov9-t-320.onnx 4              -> median 12.6 ms
python bench_ort_cpu.py .../fox-person-cat-320-fp16.onnx 4   -> FAILS TO LOAD (see risks)
```
Note: Frigate + an rsync were running during all measurements (load avg 2-6), so p95s are noisy; medians/mins are representative. ORT has no GPU EP for this platform: `onnxruntime-webgpu` publishes wheels only for x86_64 Linux, macOS arm64 and Windows (no linux aarch64); building it needs Dawn and was outside the time box.

### 3.2 ncnn (Vulkan) - WORKS
Conversion (Fedora tool, no unsupported ops):
```
onnx2ncnn $M/fox-person-cat-320.onnx ncnn_models/fox-person-cat-320.param ncnn_models/fox-person-cat-320.bin
ncnnoptimize ncnn_models/fox-person-cat-320.param ncnn_models/fox-person-cat-320.bin ncnn_models/fox-person-cat-320-opt.param ncnn_models/fox-person-cat-320-opt.bin 65536
```
Benchmarks (`bench_ncnn.py` pip wheel, `bench_ncnn_opts.py`, `bench_ncnn.cpp` against Fedora libncnn; 100 iters, idle GPU):
```
python bench_ncnn.py ncnn_models/fox-person-cat-320 $M/fox-person-cat-320.onnx 100
  CPU fp32 4thr   median  7.63 ms (131 fps)   vs ORT max|diff| 0.003
  CPU fp16 4thr   median  4.95 ms (202 fps)   vs ORT max|diff| 10.6 (fp16 arith; mean 0.17 on 0..326 px range)
  Vulkan fp32     median 30.71 ms ( 33 fps)   vs ORT max|diff| 0.0014   <- numerically exact on Honeykrisp
  Vulkan fp16     median 25.19 ms ( 40 fps)   vs ORT max|diff| 8.5
python bench_ncnn_opts.py ncnn_models/fox-person-cat-320-opt   -> vk fp32 23.8 / vk fp16 24.5 / fp16-storage-only 25.2 ms
g++ -O2 -std=c++17 bench_ncnn.cpp -o bench_ncnn_cpp -lncnn -fopenmp
./bench_ncnn_cpp ncnn_models/fox-person-cat-320 1 1 4 100  -> Vulkan fp16 median 24.0 ms (Fedora libncnn 20250916)
```
fp16/pack8/fp16-storage options change nothing (~22-25 ms) => dispatch-overhead bound (655 layers x ~35 us), not ALU bound.
CPU time per GPU inference ~5-9 ms (mostly driver `sys` time) vs ~20-25 thread-ms for the CPU path.
Bug: Fedora's libncnn 20250916 segfaults in `ncnn::VkWeightAllocator::clear()` during `ncnn::Net` destruction after a Vulkan session (inference itself is fine). The pip wheel `ncnn==1.0.20260526` does not have this problem. Use the pip wheel.

### 3.3 MNN (Vulkan + OpenCL) - WORKS, fastest GPU option
The pip wheel `mnn==3.6.1` is CPU-only on Linux (`Can't Find type=7/3 backend, use 0 instead`), but ships `mnnconvert`. Source build with GPU backends took ~4 min:
```
git clone --depth 1 https://github.com/alibaba/MNN.git MNN-src     # bef71b9, 2026-09-04
sed -i 's/-D__STRICT_ANSI__//g' MNN-src/CMakeLists.txt             # needed: GCC 16 libstdc++ breaks with __STRICT_ANSI__ (__int128 redefinition)
cd MNN-src/build && cmake -G Ninja .. -DCMAKE_BUILD_TYPE=Release -DMNN_VULKAN=ON -DMNN_OPENCL=ON -DMNN_ARM82=ON \
   -DMNN_BUILD_BENCHMARK=ON -DMNN_BUILD_TOOLS=ON -DMNN_BUILD_CONVERTER=OFF -DMNN_BUILD_TEST=OFF -DMNN_BUILD_DEMO=OFF -DMNN_SEP_BUILD=OFF
ninja -j6 MNN benchmark.out MNNV2Basic.out ModuleBasic.out
mnnconvert -f ONNX --modelFile $M/fox-person-cat-320.onnx --MNNModel mnn_models/fox-person-cat-320.mnn --bizCode frigate
```
Results (`benchmark.out models_dir loop warmup forwardType threads precision`; 0=CPU 3=OpenCL 7=Vulkan; precision 1=fp32 2=fp16; and `bench_mnn.cpp` for correctness):
```
LD_LIBRARY_PATH=MNN-src/build MNN-src/build/benchmark.out mnn_models 50 10 <type> 4 <prec>   (mnn_bench.txt)
  CPU    fp32  avg  6.65 ms      CPU    fp16  avg  4.70 ms
  OpenCL fp32  avg 38.1  ms      OpenCL fp16  avg 30.4 ms   (rusticl)
  Vulkan fp32  avg 21.3  ms      Vulkan fp16  avg 19.7 ms   (Honeykrisp)
g++ -O2 -std=c++17 bench_mnn.cpp -o bench_mnn_cpp -IMNN-src/include -LMNN-src/build -lMNN -Wl,-rpath,$PWD/MNN-src/build
./bench_mnn_cpp mnn_models/fox-person-cat-320.mnn 7 2 input_320.bin out.bin 100
  Vulkan fp32 median 21.1 ms  vs ORT max|diff| 0.0017 ; Vulkan fp16 median 19.3 ms  max|diff| 3.6 (better than CPU fp16's 8.9)
  OpenCL fp16 median 29.6 ms  max|diff| 35.1  <- rusticl result is numerically poor
  CPU time / inference: Vulkan ~7.7 ms (0.12 user + 0.73 sys per 110 runs), CPU fp16 ~19.5 thread-ms
```
Under synthetic CPU load (4 busy-loop threads, i.e. what ffmpeg decode does to this box):
```
  MNN Vulkan fp16: 19.6 ms idle -> 19.8 ms loaded (p95 36.7)
  MNN CPU    fp16:  4.7 ms idle -> 32.3 ms loaded (p95 154)
```

### 3.4 IREE (vulkan-spirv) - works but slowest, and default target gives NaNs
```
uv venv --python 3.12 venv_iree && source venv_iree/bin/activate && uv pip install "iree-base-compiler[onnx]" iree-base-runtime   # 3.11.0, aarch64 wheels exist
iree-import-onnx $M/fox-person-cat-320.onnx --opset-version 17 -o iree_models/fox.mlir          # 1 s
iree-compile iree_models/fox.mlir --iree-hal-target-backends=vulkan-spirv -o iree_models/fox_vulkan.vmfb                          # 25 s -> output is all NaN on Honeykrisp
iree-compile iree_models/fox.mlir --iree-hal-target-backends=vulkan-spirv --iree-vulkan-target=valhall -o iree_models/fox_vulkan_valhall.vmfb  # correct (max|diff| 0.0039)
iree-benchmark-module --module=iree_models/fox_vulkan_valhall.vmfb --device=vulkan --function=main_graph --input=1x3x320x320xf32
  default target: 33.6 ms (NaN output)     valhall target: 39.6 ms, ~0.9 ms CPU/iter     (iree_bench.txt)
```
There is no Apple/AGX Vulkan target profile in IREE (Apple is only a Metal target). Untuned llvm-cpu build was 198 ms (not a useful CPU baseline).

### 3.5 OpenCV DNN + OpenCL (rusticl) - NOT viable
`/usr/bin/python3 bench_opencv_dnn.py $M/fox-person-cat-320.onnx` (OpenCV 4.13, `cv2.ocl.haveOpenCL()==True`): CPU target 15.7 ms; OpenCL target 2922 ms (!) because rusticl fails to compile OpenCV's kernels (`CL_BUILD_PROGRAM_FAILURE: Internal compilation error: nir_shader not fully linked`) and OpenCV falls back per layer. OPENCL_FP16 is refused for non-Intel devices.

### 3.6 Others (not benchmarked)
* onnxruntime WebGPU EP: no linux-aarch64 wheel (`onnxruntime-webgpu` 1.27 only manylinux x86_64 / macOS arm64 / win). Source build needs Dawn; skipped (>30 min).
* TVM: `apache-tvm 0.26.0` cp314 aarch64 wheel installs, but `tvm.vulkan().exist == False` / `tvm.opencl().exist == False`; needs a source build plus auto-tuning. Skipped.
* Kompute (`kp 0.9.0` wheel available): a raw Vulkan compute framework, not an inference runtime; would require hand-writing the network. Not applicable.
* llama.cpp Vulkan: LLM-only, not applicable to CNN detection.

## 4. Comparison (fox-person-cat-320, batch 1, median unless noted; idle GPU, Frigate running on CPU)

| Runtime / device                     | fp32 ms | fp16 ms | fps (fp16) | correct vs ORT (max abs diff, fp32/fp16) | CPU ms per infer | build effort |
|--------------------------------------|--------:|--------:|-----------:|------------------------------------------|-----------------:|--------------|
| onnxruntime CPU EP (4 thr)           | 12.0    | n/a     | 83         | reference                                | ~48 thread-ms    | pip          |
| ncnn CPU (4 thr, pip wheel)          | 7.6     | 5.0     | 202        | 0.003 / 10.6                             | ~20-25           | pip + onnx2ncnn |
| MNN CPU (4 thr, source or pip)       | 6.6     | 4.7     | 213        | 0.03 / 8.9                               | ~19.5            | pip (CPU) |
| **MNN Vulkan (Honeykrisp)**          | 21.1    | **19.3**| **52**     | 0.0017 / 3.6                             | ~7.7             | 4-min source build + 1-line patch |
| ncnn Vulkan (Honeykrisp, pip wheel)  | 24-31   | 24-25   | 40-42      | 0.0014 / 8.5                             | ~5-9             | pip only |
| MNN OpenCL (rusticl)                 | 38.1    | 29.6    | 34         | - / 35 (bad)                             | ~34 (high sys)   | same build |
| IREE vulkan-spirv (valhall profile)  | 39.6    | n/a     | 25         | 0.0039 (default profile: NaN)            | ~0.9             | pip |
| OpenCV DNN OpenCL (rusticl)          | 2922    | refused | 0.3        | -                                        | -                | dnf |
| ORT WebGPU / TVM Vulkan / Kompute    | -       | -       | -          | no aarch64 wheel / no Vulkan in wheel / not a runtime | - | >30 min |

Bottom line: on this driver stack the GPU path is ~4x slower in latency than ARM fp16 NEON on the same chip for a 320x320 tiny model, because Honeykrisp's per-dispatch cost (~100 us) dominates ~650 small kernels. Its value is CPU offload: GPU latency is flat under CPU load (19.8 ms) while CPU inference collapses (4.7 -> 32 ms) when ffmpeg-like load is present. 50 fps of GPU detection is far more than Frigate needs for a handful of cameras.

## 5. Recommendation for the Frigate ZMQ detector adapter

Implement on **MNN, Vulkan backend, precision=low (fp16)**, using the source-built `libMNN.so` in `MNN-src/build` (Vulkan, image mode) with the model converted by `mnnconvert` from the fp32 ONNX. It is the fastest GPU route (19-21 ms, ~50 fps), numerically verified (fp32 max diff 0.0017 vs ORT), and its latency is unaffected by CPU contention. Keep `precision=high` (fp32, 21 ms) if you want bit-for-bit parity with the ONNX model; the fp16 gain is only ~10%.

Runner-up / lowest-effort: **ncnn Vulkan via `pip install ncnn`** (24-25 ms, ~40 fps, Python API, zero build, model via `onnx2ncnn`). Use it if the adapter must be pure Python and you don't want to build pymnn. Do not link against Fedora's `ncnn` package for Vulkan (teardown segfault).

Adapter shape (either runtime): one long-lived process holding the session; input = 320x320 RGB uint8 -> float /255 CHW; output `1x(4+ncls)x2100` -> transpose, filter by max class score, NMS (Frigate's ZMQ detector protocol expects the post-processed `[label, score, y1, x1, y2, x2]` rows, so decode + NMS happens in the adapter, like Frigate's built-in yolov9 handling). For MNN from Python, either build pymnn against the GPU-enabled tree (`cd MNN-src/pymnn/pip_package && python build_deps.py vulkan,opencl && python setup.py bdist_wheel`, untested here) or write the adapter in C++ (libzmq + libMNN, ~150 lines; `bench_mnn.cpp` is the session/tensor skeleton).

If CPU headroom turns out not to be a problem, the honest fastest option is simply ncnn/MNN CPU fp16 at ~5 ms; a hybrid adapter (GPU primary, CPU fallback) is cheap to add since both runtimes load the same converted model.

## 6. Open risks

1. Driver maturity: Honeykrisp/rusticl are new; the numbers here are from short (<1 min) bursts. Run a multi-hour soak of the adapter before trusting it; watch `dmesg` for AGX faults. No GPU devfreq/thermal node is exposed to userspace on this kernel.
2. Per-dispatch latency (~100 us Vulkan) is the bottleneck; a newer Mesa may change results (better or worse). Re-run `bench_mnn_cpp`/`bench_ncnn.py` after Mesa updates.
3. Mesa upgrade coupling: installing mesa-vulkan-drivers pulled mesa 26.1.4 -> 26.1.8 for all mesa packages; future `dnf update` will move the GPU stack under the adapter.
4. rusticl: OpenCL kernels compile-fail in OpenCV and MNN's OpenCL output is inaccurate (max diff 35); rusticl warns that Fedora's libclc is unpatched. Treat OpenCL as unusable here; use Vulkan.
5. `fox-person-cat-320-fp16.onnx` is malformed for onnxruntime (float32 input feeds fp16 Conv without a Cast: `Type parameter (T) of Optype (Conv) bound to different types`) and has a different class set (3 classes) than the fp32 model (4 classes). Use the fp32 ONNX as the source of truth and let ncnn/MNN do fp16 at load time.
6. fp16 arithmetic shifts box coordinates by up to ~4-10 px (max) / 0.1-0.2 px (mean) vs fp32 on random input; MNN Vulkan fp16 was the tightest (3.6). Acceptable for NVR detection but validate on real frames with your confidence threshold.
7. Fedora `ncnn` 20250916 segfaults in `VkWeightAllocator::clear()` at Net destruction with Vulkan; pip wheel 1.0.20260526 is fine.
8. IREE's default vulkan-spirv profile produces NaN on Honeykrisp; only the `valhall` profile was verified correct. Not recommended.
9. MNN needed a source patch (`-D__STRICT_ANSI__` removed) for GCC 16; upstream may fix or change this. The pymnn build path was not exercised.
10. Docker: Frigate runs in a container; the adapter process (host or container) needs `/dev/dri/renderD128` (world rw here) and the matching Mesa userspace inside the container if containerised. Simplest is running the ZMQ adapter on the host.

## 7. Files in this directory
`bench_ort_cpu.py` (ORT CPU baseline), `bench_ncnn.py` / `bench_ncnn_opts.py` / `bench_ncnn.cpp` (+`bench_ncnn_cpp`), `bench_mnn.py` (pip wheel, CPU only) / `bench_mnn.cpp` (+`bench_mnn_cpp`, GPU), `bench_opencv_dnn.py`, `ncnn_models/`, `mnn_models/`, `iree_models/`, `input_320.bin` + `ref_ort_output.bin` (seed-0 random input and ORT reference output for correctness checks), `MNN-src/` (patched source + `build/libMNN.so`), venvs `venv314` (ort, ncnn, mnn, tvm), `venv_iree` (py3.12), `venv312` (empty), raw logs `vulkaninfo_*.txt`, `clinfo_full.txt`, `clpeak*.txt`, `mnn_bench.txt`, `iree_bench.txt`.
