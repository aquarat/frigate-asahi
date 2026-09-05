// Tiny C ABI around MNN::Interpreter so backends.py can drive the source-built
// libMNN.so (Vulkan) through ctypes. Single-session, batch-1, float32 I/O.
// Build: see Makefile target `mnn-shim` (g++ -shared -fPIC, links libMNN.so).
#include <MNN/Interpreter.hpp>
#include <MNN/Tensor.hpp>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

namespace {
struct Handle {
    std::shared_ptr<MNN::Interpreter> net;
    MNN::Session* session = nullptr;
    std::vector<std::pair<std::string, MNN::Tensor*>> inputs, outputs;
    std::vector<std::unique_ptr<MNN::Tensor>> host_in, host_out;
    std::string err;
};
void set_err(char* err, int errlen, const std::string& msg) {
    if (err && errlen > 0) { std::strncpy(err, msg.c_str(), errlen - 1); err[errlen - 1] = 0; }
}
}  // namespace

extern "C" {

// forward_type: MNNForwardType (0 CPU, 7 Vulkan, 3 OpenCL). precision: 0 normal, 1 high(fp32), 2 low(fp16).
void* mnn_shim_create(const char* model_path, int forward_type, int precision, int threads, char* err, int errlen) {
    auto h = new Handle();
    h->net.reset(MNN::Interpreter::createFromFile(model_path), [](MNN::Interpreter* p) { if (p) MNN::Interpreter::destroy(p); });
    if (!h->net) { set_err(err, errlen, std::string("createFromFile failed: ") + model_path); delete h; return nullptr; }
    MNN::ScheduleConfig cfg;
    cfg.type = (MNNForwardType)forward_type;
    cfg.numThread = threads > 0 ? threads : 1;
    MNN::BackendConfig bc;
    bc.precision = (MNN::BackendConfig::PrecisionMode)precision;
    cfg.backendConfig = &bc;
    h->session = h->net->createSession(cfg);
    if (!h->session) { set_err(err, errlen, "createSession failed"); delete h; return nullptr; }
    for (auto& kv : h->net->getSessionInputAll(h->session)) {
        h->inputs.emplace_back(kv.first, kv.second);
        h->host_in.emplace_back(new MNN::Tensor(kv.second, MNN::Tensor::CAFFE));
    }
    for (auto& kv : h->net->getSessionOutputAll(h->session)) {
        h->outputs.emplace_back(kv.first, kv.second);
        h->host_out.emplace_back(new MNN::Tensor(kv.second, MNN::Tensor::CAFFE));
    }
    return h;
}

int mnn_shim_num_inputs(void* p) { return (int)((Handle*)p)->inputs.size(); }
int mnn_shim_num_outputs(void* p) { return (int)((Handle*)p)->outputs.size(); }

// name -> buf; dims -> dims[0..ndim); returns ndim (or -1). which: 0 input, 1 output.
int mnn_shim_io_info(void* p, int which, int idx, char* name, int namelen, int* dims, int maxdims) {
    auto h = (Handle*)p;
    auto& v = which == 0 ? h->inputs : h->outputs;
    if (idx < 0 || idx >= (int)v.size()) return -1;
    set_err(name, namelen, v[idx].first);
    auto shape = v[idx].second->shape();
    int n = (int)shape.size();
    for (int i = 0; i < n && i < maxdims; i++) dims[i] = shape[i];
    return n;
}

// in[i] / out[i] are float32 buffers sized to the tensors' elementSize().
int mnn_shim_infer(void* p, const float* const* in, const size_t* in_elems, float* const* out, const size_t* out_elems) {
    auto h = (Handle*)p;
    for (size_t i = 0; i < h->inputs.size(); i++) {
        auto* ht = h->host_in[i].get();
        if ((size_t)ht->elementSize() != in_elems[i]) return -2;
        std::memcpy(ht->host<float>(), in[i], in_elems[i] * sizeof(float));
        h->inputs[i].second->copyFromHostTensor(ht);
    }
    if (h->net->runSession(h->session) != MNN::NO_ERROR) return -3;
    for (size_t i = 0; i < h->outputs.size(); i++) {
        auto* ht = h->host_out[i].get();
        if ((size_t)ht->elementSize() != out_elems[i]) return -4;
        h->outputs[i].second->copyToHostTensor(ht);
        std::memcpy(out[i], ht->host<float>(), out_elems[i] * sizeof(float));
    }
    return 0;
}

void mnn_shim_destroy(void* p) {
    auto h = (Handle*)p;
    if (!h) return;
    h->host_in.clear(); h->host_out.clear();
    if (h->session) h->net->releaseSession(h->session);
    h->net.reset();
    delete h;
}

}  // extern "C"
