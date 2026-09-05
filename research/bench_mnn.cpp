// MNN C++ bench + correctness dump. build: see GPU_INFERENCE.md
// usage: ./bench_mnn_cpp model.mnn <forwardType 0=CPU 3=OpenCL 7=Vulkan> <precision 1=fp32 2=fp16> input.bin output.bin [iters]
#include <MNN/Interpreter.hpp>
#include <MNN/Tensor.hpp>
#include <chrono>
#include <cstdio>
#include <vector>
#include <algorithm>
#include <fstream>
int main(int argc, char** argv) {
    auto net = MNN::Interpreter::createFromFile(argv[1]);
    int ft = atoi(argv[2]), prec = atoi(argv[3]); int iters = argc > 6 ? atoi(argv[6]) : 100;
    MNN::ScheduleConfig cfg; cfg.type = (MNNForwardType)ft; cfg.numThread = 4;
    MNN::BackendConfig bc; bc.precision = (MNN::BackendConfig::PrecisionMode)prec; cfg.backendConfig = &bc;
    auto sess = net->createSession(cfg);
    auto in = net->getSessionInput(sess, nullptr); auto out = net->getSessionOutput(sess, nullptr);
    std::vector<float> x(3 * 320 * 320); std::ifstream(argv[4], std::ios::binary).read((char*)x.data(), x.size() * 4);
    MNN::Tensor hin(in, MNN::Tensor::CAFFE); std::copy(x.begin(), x.end(), hin.host<float>());
    MNN::Tensor hout(out, MNN::Tensor::CAFFE);
    auto infer = [&]() { in->copyFromHostTensor(&hin); net->runSession(sess); out->copyToHostTensor(&hout); };
    for (int i = 0; i < 10; i++) infer();
    std::vector<double> t;
    for (int i = 0; i < iters; i++) { auto a = std::chrono::steady_clock::now(); infer(); auto b = std::chrono::steady_clock::now(); t.push_back(std::chrono::duration<double, std::milli>(b - a).count()); }
    std::sort(t.begin(), t.end());
    printf("MNN type=%d prec=%d: median %.2f ms p95 %.2f min %.2f -> %.1f fps (out %d elems)\n", ft, prec, t[t.size()/2], t[t.size()*95/100], t[0], 1000.0/t[t.size()/2], hout.elementSize()); fflush(stdout);
    std::ofstream(argv[5], std::ios::binary).write((char*)hout.host<float>(), hout.elementSize() * 4);
    return 0;
}
