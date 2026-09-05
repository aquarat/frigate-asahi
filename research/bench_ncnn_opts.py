#!/usr/bin/env python3
"""ncnn Vulkan option sweep for one model. Usage: python bench_ncnn_opts.py ncnn_models/NAME [iters]"""
import sys, time, numpy as np, ncnn
base = sys.argv[1]; iters = int(sys.argv[2]) if len(sys.argv) > 2 else 60
x = np.random.rand(3, 320, 320).astype(np.float32)
def run(label, **kw):
    net = ncnn.Net(); net.opt.use_vulkan_compute = True; net.opt.num_threads = 1
    for k, v in kw.items(): setattr(net.opt, k, v)
    net.load_param(base + ".param"); net.load_model(base + ".bin")
    inp = net.input_names()[0]; outn = net.output_names()[0]; mat = ncnn.Mat(x)
    def infer():
        ex = net.create_extractor(); ex.input(inp, mat); _, out = ex.extract(outn); return out
    for _ in range(10): infer()
    t = []
    for _ in range(iters):
        t0 = time.perf_counter(); infer(); t.append((time.perf_counter() - t0) * 1e3)
    t = np.array(t); print(f"{label:<40} median {np.median(t):6.2f} ms  min {t.min():6.2f}  p95 {np.percentile(t,95):6.2f}")
    del net
run("vk fp32 default")
run("vk fp16 default", use_fp16_packed=True, use_fp16_storage=True, use_fp16_arithmetic=True)
run("vk fp16 storage only (fp32 arith)", use_fp16_packed=True, use_fp16_storage=True, use_fp16_arithmetic=False)
run("vk fp16 + image storage", use_fp16_packed=True, use_fp16_storage=True, use_fp16_arithmetic=True, use_image_storage=True)
run("vk fp16 no pack8", use_fp16_packed=True, use_fp16_storage=True, use_fp16_arithmetic=True, use_shader_pack8=False)
run("vk fp16 no packing at all", use_fp16_packed=False, use_fp16_storage=True, use_fp16_arithmetic=True, use_packing_layout=False)
ncnn.destroy_gpu_instance()
