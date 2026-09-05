#!/usr/bin/env python3
"""MNN benchmark (pip wheel): CPU / Vulkan / OpenCL backends for a converted .mnn model.
Usage: python bench_mnn.py mnn_models/NAME.mnn [iters]"""
import os, sys, time, numpy as np, MNN
path = sys.argv[1]; iters = int(sys.argv[2]) if len(sys.argv) > 2 else 60
x = np.random.rand(1, 3, 320, 320).astype(np.float32)
ref = None
try:
    import onnxruntime as ort
    s = ort.InferenceSession(os.path.expanduser("~/frigate/config/model_cache/fox-person-cat-320.onnx"), providers=["CPUExecutionProvider"])
    ref = s.run(None, {"images": x})[0]
except Exception as e: print("no ort ref", e)
def run(label, backend, precision=2, threads=4):
    try:
        interp = MNN.Interpreter(path)
        cfg = {"backend": backend, "numThread": threads, "precision": precision}  # precision: 0 normal,1 high,2 low(fp16)
        sess = interp.createSession(cfg)
        inp = interp.getSessionInput(sess); out = interp.getSessionOutput(sess)
        bi = interp.getSessionInfo(sess, 2) if hasattr(interp, "getSessionInfo") else None
        t_in = MNN.Tensor((1, 3, 320, 320), MNN.Halide_Type_Float, x, MNN.Tensor_DimensionType_Caffe)
        def infer():
            inp.copyFrom(t_in); interp.runSession(sess)
            t_out = MNN.Tensor(out.getShape(), MNN.Halide_Type_Float, np.zeros(out.getShape(), np.float32), MNN.Tensor_DimensionType_Caffe)
            out.copyToHostTensor(t_out); return np.array(t_out.getData()).reshape(out.getShape())
        for _ in range(10): o = infer()
        t = []
        for _ in range(iters):
            t0 = time.perf_counter(); o = infer(); t.append((time.perf_counter() - t0) * 1e3)
        t = np.array(t); err = ""
        if ref is not None and o.shape == ref.shape: err = f" | vs ORT max|d| {np.abs(o-ref).max():.3f} mean|d| {np.abs(o-ref).mean():.5f}"
        print(f"{label:<26} backend={backend} median {np.median(t):6.2f} ms  min {t.min():6.2f}  p95 {np.percentile(t,95):6.2f}  -> {1000/np.median(t):5.1f} fps{err}  (resolved backend info: {bi})")
    except Exception as e: print(f"{label:<26} FAILED: {e}")
print("MNN", MNN.version())
run("CPU fp32 4thr", "CPU", precision=1)
run("CPU fp16 4thr", "CPU", precision=2)
run("Vulkan fp32", "VULKAN", precision=1)
run("Vulkan fp16", "VULKAN", precision=2)
run("OpenCL fp32", "OPENCL", precision=1)
run("OpenCL fp16", "OPENCL", precision=2)
