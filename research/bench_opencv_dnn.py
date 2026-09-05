#!/usr/bin/env python3
"""OpenCV DNN benchmark: CPU vs OpenCL (rusticl) targets. Uses system python3-opencv.
Usage: /usr/bin/python3 bench_opencv_dnn.py model.onnx [iters]"""
import sys, time, numpy as np, cv2
path = sys.argv[1]; iters = int(sys.argv[2]) if len(sys.argv) > 2 else 60
print("OpenCV", cv2.__version__, "haveOpenCL", cv2.ocl.haveOpenCL(), "useOpenCL", cv2.ocl.useOpenCL())
x = np.random.rand(1, 3, 320, 320).astype(np.float32)
def run(label, backend, target):
    try:
        net = cv2.dnn.readNetFromONNX(path); net.setPreferableBackend(backend); net.setPreferableTarget(target)
        net.setInput(x)
        for _ in range(5): o = net.forward()
        t = []
        for _ in range(iters):
            t0 = time.perf_counter(); o = net.forward(); t.append((time.perf_counter() - t0) * 1e3)
        t = np.array(t); print(f"{label:<24} median {np.median(t):7.2f} ms  min {t.min():7.2f}  p95 {np.percentile(t,95):7.2f} -> {1000/np.median(t):5.1f} fps  out {o.shape}")
    except Exception as e: print(f"{label:<24} FAILED: {str(e).splitlines()[-1][:200]}")
cv2.setNumThreads(4)
run("CPU (default)", cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)
run("OpenCL fp32", cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_OPENCL)
run("OpenCL fp16", cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_OPENCL_FP16)
