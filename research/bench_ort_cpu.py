#!/usr/bin/env python3
"""CPU baseline: onnxruntime CPU EP latency for a 320x320 YOLOv9-t ONNX model.
Usage: python bench_ort_cpu.py model.onnx [threads] [iters]"""
import sys, time, numpy as np, onnxruntime as ort
model = sys.argv[1]; threads = int(sys.argv[2]) if len(sys.argv) > 2 else 4; iters = int(sys.argv[3]) if len(sys.argv) > 3 else 100
so = ort.SessionOptions(); so.intra_op_num_threads = threads; so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
s = ort.InferenceSession(model, so, providers=["CPUExecutionProvider"])
inp = s.get_inputs()[0]; x = np.random.rand(1, 3, 320, 320).astype(np.float32)
for _ in range(10): s.run(None, {inp.name: x})
t = []
for _ in range(iters):
    t0 = time.perf_counter(); s.run(None, {inp.name: x}); t.append((time.perf_counter() - t0) * 1e3)
t = np.array(t)
print(f"{model.split('/')[-1]} threads={threads} ort={ort.__version__} CPU: mean {t.mean():.2f} ms  median {np.median(t):.2f}  p95 {np.percentile(t,95):.2f}  min {t.min():.2f}  -> {1000/np.median(t):.1f} fps")
