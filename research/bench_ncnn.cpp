// C++ ncnn benchmark against Fedora's libncnn (NCNN_VULKAN=ON). 
// build: g++ -O2 -std=c++17 bench_ncnn.cpp -o bench_ncnn_cpp -lncnn -fopenmp
// usage: ./bench_ncnn_cpp ncnn_models/NAME <gpu 0|1> <fp16 0|1> [threads] [iters]
#include <ncnn/net.h>
#include <ncnn/gpu.h>
#include <chrono>
#include <cstdio>
#include <string>
#include <vector>
#include <algorithm>
int main(int argc, char** argv) {
    std::string base = argv[1]; int gpu = atoi(argv[2]); int fp16 = atoi(argv[3]);
    int threads = argc > 4 ? atoi(argv[4]) : 4; int iters = argc > 5 ? atoi(argv[5]) : 100;
    if (gpu) ncnn::create_gpu_instance();
    ncnn::Net net;
    net.opt.use_vulkan_compute = gpu; net.opt.num_threads = threads;
    net.opt.use_fp16_packed = fp16; net.opt.use_fp16_storage = fp16; net.opt.use_fp16_arithmetic = fp16;
    net.opt.use_bf16_storage = false;
    if (gpu) net.set_vulkan_device(0);
    net.load_param((base + ".param").c_str()); net.load_model((base + ".bin").c_str());
    ncnn::Mat in(320, 320, 3); in.fill(0.5f);
    ncnn::Mat out;
    auto infer = [&]() { ncnn::Extractor ex = net.create_extractor(); ex.input("images", in); ex.extract("output0", out); };
    for (int i = 0; i < 10; i++) infer();
    std::vector<double> t;
    for (int i = 0; i < iters; i++) { auto a = std::chrono::steady_clock::now(); infer(); auto b = std::chrono::steady_clock::now(); t.push_back(std::chrono::duration<double, std::milli>(b - a).count()); }
    std::sort(t.begin(), t.end()); double sum = 0; for (double v : t) sum += v;
    setvbuf(stdout, NULL, _IONBF, 0); printf("%s gpu=%d fp16=%d threads=%d: mean %.2f ms median %.2f p95 %.2f min %.2f -> %.1f fps (out %dx%dx%d)\n", base.c_str(), gpu, fp16, threads, sum / t.size(), t[t.size() / 2], t[t.size() * 95 / 100], t[0], 1000.0 / t[t.size() / 2], out.w, out.h, out.c);
    if (gpu) ncnn::destroy_gpu_instance();
    return 0;
}
