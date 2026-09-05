#!/usr/bin/env python3
"""ncnn benchmark (pip wheel): CPU vs Vulkan GPU for a converted YOLOv9-t 320 model.
Also checks numerical agreement vs onnxruntime CPU on the same random input.
Usage: python bench_ncnn.py ncnn_models/NAME [onnx_for_check.onnx] [iters]"""
import sys, time, numpy as np, ncnn
base = sys.argv[1]; onnx_path = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].endswith('.onnx') else None
iters = int(sys.argv[-1]) if sys.argv[-1].isdigit() else 100
x = np.random.rand(1, 3, 320, 320).astype(np.float32)
ref = None
if onnx_path:
    import onnxruntime as ort
    s = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    ref = s.run(None, {s.get_inputs()[0].name: x})[0]

def run(use_gpu, fp16, threads=4, label=""):
    net = ncnn.Net()
    net.opt.use_vulkan_compute = use_gpu
    net.opt.num_threads = threads
    net.opt.use_fp16_packed = fp16; net.opt.use_fp16_storage = fp16; net.opt.use_fp16_arithmetic = fp16
    net.opt.use_bf16_storage = False
    net.load_param(base + ".param"); net.load_model(base + ".bin")
    inp = net.input_names()[0]; outn = net.output_names()[0]
    mat = ncnn.Mat(x[0])  # CHW
    def infer():
        ex = net.create_extractor(); ex.input(inp, mat); _, out = ex.extract(outn); return np.array(out)
    for _ in range(10): infer()
    t = []
    for _ in range(iters):
        t0 = time.perf_counter(); o = infer(); t.append((time.perf_counter() - t0) * 1e3)
    t = np.array(t)
    err = ""
    if ref is not None:
        o = o.reshape(ref.shape) if o.size == ref.size else o
        if o.shape == ref.shape:
            err = f" | vs ORT: max|diff| {np.abs(o-ref).max():.4f}  mean|diff| {np.abs(o-ref).mean():.5f} (out range {ref.min():.1f}..{ref.max():.1f})"
        else:
            err = f" | shape mismatch ncnn {o.shape} vs ort {ref.shape}"
    print(f"{label:<28} mean {t.mean():6.2f} ms  median {np.median(t):6.2f}  p95 {np.percentile(t,95):6.2f}  min {t.min():6.2f} -> {1000/np.median(t):5.1f} fps{err}")
    del net

print("ncnn", ncnn.__version__, "gpus:", ncnn.get_gpu_count(), "device0:", ncnn.get_gpu_info(0).device_name() if ncnn.get_gpu_count() else None)
run(False, False, 4, "CPU fp32 4thr")
run(False, True, 4, "CPU fp16 4thr")
run(True, False, 1, "Vulkan fp32")
run(True, True, 1, "Vulkan fp16")
ncnn.destroy_gpu_instance()
