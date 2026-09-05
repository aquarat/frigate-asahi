"""
Inference backends.

This is the seam between the ZMQ protocol code (zmq_onnx_client.py) and the
engine that actually runs the network. The protocol code only ever calls:

    backend = create_backend("cpu", models_dir=...)
    backend.load("/path/to/model.onnx")
    outputs = backend.infer({"images": tensor})   # -> list of np.ndarray

Post-processing (YOLO / RF-DETR / D-FINE decoding + NMS) stays in the client,
so a backend only has to turn raw input tensor(s) into the model's raw output
tensor(s), in the same order ONNX Runtime would return them.

To add a new engine (e.g. an ncnn / Vulkan backend):

  1. Subclass InferenceBackend and implement load() and infer().
  2. Register it in BACKENDS below under a short name.
  3. Start the detector with --backend <name>.

Backends here: cpu / coreml / onnxruntime (ONNX Runtime), ncnn-vulkan (GPU via
Vulkan) and ncnn-cpu (ncnn NEON), plus the alias 'vulkan' -> best GPU backend.
A GPU backend that cannot initialise, or cannot load a model, falls back to cpu
with a 'BACKEND FALLBACK' log line.

Nothing in the ZMQ protocol code needs to change.
"""

import logging
import os
import platform
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)

ProviderSpec = Union[str, Tuple[str, dict]]


class InferenceBackend:
    """Minimal interface every backend implements."""

    name: str = "base"

    def load(self, model_path: str) -> None:
        """Load a model from disk. Raise on failure."""
        raise NotImplementedError

    def infer(self, feeds: Dict[str, np.ndarray]) -> List[np.ndarray]:
        """Run one inference. feeds maps input name -> array."""
        raise NotImplementedError

    @property
    def loaded(self) -> bool:
        return False

    @property
    def input_names(self) -> List[str]:
        """Names of the model inputs, in model order."""
        return []

    def describe(self) -> str:
        return self.name

    def close(self) -> None:
        """Release engine resources (called once at shutdown)."""
        return None


class OnnxRuntimeBackend(InferenceBackend):
    """ONNX Runtime with an arbitrary execution-provider list."""

    name = "onnxruntime"

    def __init__(
        self,
        providers: Sequence[ProviderSpec],
        intra_op_threads: Optional[int] = None,
    ):
        self.providers = list(providers)
        self.intra_op_threads = intra_op_threads
        self.session = None

    def load(self, model_path: str) -> None:
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if self.intra_op_threads:
            so.intra_op_num_threads = int(self.intra_op_threads)

        names = [p[0] if isinstance(p, tuple) else p for p in self.providers]
        logger.info(
            f"[{self.name}] loading {model_path} with providers={names}"
            + (f" intra_op_threads={self.intra_op_threads}" if self.intra_op_threads else "")
        )
        session = ort.InferenceSession(model_path, so, providers=self.providers)

        i = session.get_inputs()[0]
        o = session.get_outputs()[0]
        logger.info(f"Model input: {i.name}, shape: {i.shape}, type: {i.type}")
        logger.info(f"Model output: {o.name}, shape: {o.shape}, type: {o.type}")
        logger.info(f"Active providers: {session.get_providers()}")
        self.session = session

    def infer(self, feeds: Dict[str, np.ndarray]) -> List[np.ndarray]:
        return self.session.run(None, feeds)

    @property
    def loaded(self) -> bool:
        return self.session is not None

    @property
    def input_names(self) -> List[str]:
        return [i.name for i in self.session.get_inputs()] if self.session else []

    def describe(self) -> str:
        names = [p[0] if isinstance(p, tuple) else p for p in self.providers]
        return f"{self.name}({', '.join(names)})"


# --- ncnn (Vulkan GPU / CPU) ----------------------------------------------


def _find_tool(name: str, env_var: str) -> Optional[str]:
    """Locate a converter binary: explicit env var, then PATH."""
    import shutil

    p = os.environ.get(env_var)
    if p and os.path.isfile(p) and os.access(p, os.X_OK):
        return p
    return shutil.which(name)


def convert_onnx_to_ncnn(onnx_path: str, param_path: str, bin_path: str) -> None:
    """
    ONNX -> ncnn .param/.bin using onnx2ncnn (+ ncnnoptimize when available).

    Writes to temp files in the target directory and renames at the end so a
    half-written cache is never picked up. Weights stay fp32 in the cache;
    fp16 is a runtime option (Net.opt.use_fp16_*), so one cache serves both.
    Override the tools with DETECTOR_ONNX2NCNN / DETECTOR_NCNNOPTIMIZE.
    """
    import subprocess
    import tempfile

    onnx2ncnn = _find_tool("onnx2ncnn", "DETECTOR_ONNX2NCNN")
    if not onnx2ncnn:
        raise RuntimeError(
            "onnx2ncnn not found (install the 'ncnn' distro package for the converter "
            "or set DETECTOR_ONNX2NCNN)"
        )
    ncnnoptimize = _find_tool("ncnnoptimize", "DETECTOR_NCNNOPTIMIZE")

    out_dir = os.path.dirname(os.path.abspath(param_path))
    tmpdir = tempfile.mkdtemp(prefix=".ncnn-convert-", dir=out_dir)
    try:
        raw_param = os.path.join(tmpdir, "raw.param")
        raw_bin = os.path.join(tmpdir, "raw.bin")
        logger.info(f"[ncnn] converting {onnx_path} with {onnx2ncnn}")
        r = subprocess.run(
            [onnx2ncnn, onnx_path, raw_param, raw_bin],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0 or not os.path.exists(raw_param) or not os.path.exists(raw_bin):
            raise RuntimeError(f"onnx2ncnn failed (rc={r.returncode}): {r.stderr.strip()[-2000:]}")
        # onnx2ncnn prints a PNNX advert to stderr; only surface real problems.
        unsupported = [l for l in r.stderr.splitlines() if "not supported" in l.lower()]
        if unsupported:
            raise RuntimeError("onnx2ncnn: unsupported ops: " + "; ".join(unsupported[:10]))

        final_param, final_bin = raw_param, raw_bin
        if ncnnoptimize:
            opt_param = os.path.join(tmpdir, "opt.param")
            opt_bin = os.path.join(tmpdir, "opt.bin")
            # last arg 0 = keep fp32 weights (65536 would store fp16 weights)
            r = subprocess.run(
                [ncnnoptimize, raw_param, raw_bin, opt_param, opt_bin, "0"],
                capture_output=True, text=True, timeout=600,
            )
            if r.returncode == 0 and os.path.exists(opt_param) and os.path.exists(opt_bin):
                final_param, final_bin = opt_param, opt_bin
            else:
                logger.warning(f"[ncnn] ncnnoptimize failed (rc={r.returncode}); using unoptimized graph")
        else:
            logger.warning("[ncnn] ncnnoptimize not found; using unoptimized graph")

        os.replace(final_param, param_path)
        os.replace(final_bin, bin_path)
        logger.info(f"[ncnn] cached converted model at {param_path} / {bin_path}")
    finally:
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)


def _onnx_io_meta(onnx_path: str) -> Tuple[List[Tuple[str, list]], List[Tuple[str, list]]]:
    """(inputs, outputs) as [(name, shape)] from the ONNX graph, via onnxruntime's
    metadata-only session (no inference is run; the session is discarded)."""
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    so.intra_op_num_threads = 1
    so.log_severity_level = 3
    s = ort.InferenceSession(onnx_path, so, providers=["CPUExecutionProvider"])
    ins = [(i.name, list(i.shape)) for i in s.get_inputs()]
    outs = [(o.name, list(o.shape)) for o in s.get_outputs()]
    del s
    return ins, outs


class NcnnBackend(InferenceBackend):
    """
    ncnn (pip wheel) backend. use_vulkan=True runs the graph on the GPU via
    Vulkan (Honeykrisp on Asahi); False runs ncnn's NEON CPU path.

    The service is handed ONNX files; they are converted on first load with
    onnx2ncnn/ncnnoptimize and cached next to the model as <name>.ncnn.param /
    <name>.ncnn.bin (regenerated when the ONNX is newer than the cache).

    One ncnn.Net (+ its Vulkan allocators) is created per load() and reused for
    every inference; only the lightweight Extractor is per-request, which is
    how ncnn is designed to be used. Outputs are reshaped to the ONNX output
    shapes (ncnn drops the batch axis) so post-processing is unchanged.
    """

    name = "ncnn-vulkan"

    def __init__(
        self,
        use_vulkan: bool = True,
        fp16: bool = True,
        threads: Optional[int] = None,
        device_index: Optional[int] = None,
    ):
        self.use_vulkan = use_vulkan
        self.fp16 = fp16
        self.threads = threads
        self.device_index = device_index
        self.net = None
        self._allocs: list = []
        self._in_names: List[str] = []
        self._in_shapes: List[list] = []
        self._out_names: List[str] = []
        self._out_shapes: List[list] = []
        self._model_path = None
        self._device_name = "cpu"
        self._closed = False
        if not use_vulkan:
            self.name = "ncnn-cpu"

        import ncnn  # noqa: F401  (raises ImportError -> caller falls back)

        self._ncnn = ncnn
        if self.use_vulkan:
            self.device_index = self._pick_vulkan_device(device_index)
            info = ncnn.get_gpu_info(self.device_index)
            self._device_name = info.device_name()
            logger.info(
                f"[{self.name}] Vulkan device {self.device_index}: {self._device_name} "
                f"(api {info.api_version():#x}, driver {info.driver_version():#x}), fp16={self.fp16}"
            )

    # -- setup -----------------------------------------------------------

    def _pick_vulkan_device(self, requested: Optional[int]) -> int:
        """Hardware GPU only: skip CPU-type Vulkan devices such as llvmpipe."""
        ncnn = self._ncnn
        n = ncnn.get_gpu_count()
        if n <= 0:
            raise RuntimeError("no Vulkan devices found")
        env = os.environ.get("DETECTOR_VULKAN_DEVICE")
        if requested is None and env not in (None, ""):
            requested = int(env)
        devs = [(i, ncnn.get_gpu_info(i)) for i in range(n)]
        names = ", ".join(f"{i}:{g.device_name()}(type {g.type()})" for i, g in devs)
        if requested is not None:
            if not (0 <= requested < n):
                raise RuntimeError(f"Vulkan device {requested} out of range; have {names}")
            return requested
        # ncnn type: 0 discrete, 1 integrated, 2 virtual, 3 cpu
        d = ncnn.get_default_gpu_index()
        if 0 <= d < n and devs[d][1].type() in (0, 1, 2):
            return d
        for i, g in devs:
            if g.type() in (0, 1, 2):
                return i
        raise RuntimeError(f"no hardware Vulkan device (only CPU emulation): {names}")

    @staticmethod
    def cache_paths(model_path: str) -> Tuple[str, str]:
        stem, _ = os.path.splitext(model_path)
        return stem + ".ncnn.param", stem + ".ncnn.bin"

    def _ensure_converted(self, model_path: str) -> Tuple[str, str]:
        param, binf = self.cache_paths(model_path)
        try:
            fresh = (
                os.path.getmtime(param) >= os.path.getmtime(model_path)
                and os.path.getmtime(binf) >= os.path.getmtime(model_path)
                and os.path.getsize(binf) > 0
            )
        except OSError:
            fresh = False
        if fresh:
            logger.info(f"[{self.name}] using cached ncnn model {param}")
        else:
            convert_onnx_to_ncnn(model_path, param, binf)
        return param, binf

    def _build_net(self, param: str, binf: str):
        ncnn = self._ncnn
        net = ncnn.Net()
        opt = net.opt
        opt.use_vulkan_compute = self.use_vulkan
        opt.use_fp16_packed = self.fp16
        opt.use_fp16_storage = self.fp16
        opt.use_fp16_arithmetic = self.fp16
        opt.use_bf16_storage = False
        allocs: list = []
        if self.use_vulkan:
            # The GPU does the work; ncnn's CPU-side helpers only need 1 thread and
            # must not spin in OpenMP afterwards (blocktime) next to ffmpeg.
            opt.num_threads = int(os.environ.get("DETECTOR_NCNN_THREADS", "1"))
            opt.openmp_blocktime = 0
            net.set_vulkan_device(self.device_index)
            vkdev = ncnn.get_gpu_device(self.device_index)
            blob = ncnn.VkBlobAllocator(vkdev)
            staging = ncnn.VkStagingAllocator(vkdev)
            opt.blob_vkallocator = blob
            opt.workspace_vkallocator = blob
            opt.staging_vkallocator = staging
            allocs = [blob, staging]  # must outlive the net
        else:
            opt.num_threads = int(self.threads or 4)
            opt.openmp_blocktime = 0
        if net.load_param(param) != 0:
            raise RuntimeError(f"ncnn load_param failed for {param}")
        if net.load_model(binf) != 0:
            raise RuntimeError(f"ncnn load_model failed for {binf}")
        return net, allocs

    def load(self, model_path: str) -> None:
        ins, outs = _onnx_io_meta(model_path)
        param, binf = self._ensure_converted(model_path)
        try:
            net, allocs = self._build_net(param, binf)
        except Exception as e:  # stale/corrupt cache: convert once more
            logger.warning(f"[{self.name}] cached model failed to load ({e}); reconverting")
            convert_onnx_to_ncnn(model_path, param, binf)
            net, allocs = self._build_net(param, binf)

        net_in, net_out = list(net.input_names()), list(net.output_names())
        in_names = [n for n, _ in ins]
        out_names = [n for n, _ in outs]
        if set(in_names) != set(net_in) or set(out_names) != set(net_out):
            raise RuntimeError(
                f"ncnn graph I/O {net_in}->{net_out} does not match ONNX {in_names}->{out_names}"
            )

        old_net, old_allocs = self.net, self._allocs
        self.net, self._allocs = net, allocs
        self._in_names, self._in_shapes = in_names, [s for _, s in ins]
        self._out_names, self._out_shapes = out_names, [s for _, s in outs]
        self._model_path = model_path
        logger.info(
            f"[{self.name}] loaded {os.path.basename(model_path)}: inputs {ins} outputs {outs} "
            f"({len(net.layers())} layers)"
        )

        # Warm-up: compiles/caches the Vulkan pipelines (first run is ~1 s) and
        # validates the output mapping before the model is declared ready.
        feeds = {}
        for name, shape in ins:
            shp = [d if isinstance(d, int) and d > 0 else (1 if i == 0 else 320) for i, d in enumerate(shape)]
            feeds[name] = np.zeros(shp, dtype=np.float32)
        y = self.infer(feeds)
        logger.info(f"[{self.name}] warm-up ok: outputs {[(a.shape, a.dtype.name) for a in y]}")

        if old_net is not None:
            del old_net
            del old_allocs

    # -- inference ---------------------------------------------------------

    def _to_mat(self, x: np.ndarray):
        x = np.asarray(x)
        if x.dtype != np.float32:
            x = x.astype(np.float32)
        if x.ndim == 4:
            if x.shape[0] != 1:
                raise ValueError(f"ncnn backend supports batch 1 only, got {x.shape}")
            x = x[0]
        x = np.ascontiguousarray(x)
        return self._ncnn.Mat(x)

    def _reshape_out(self, arr: np.ndarray, target: list) -> np.ndarray:
        if len(target) == arr.ndim + 1 and (target[0] in (1, None) or isinstance(target[0], str)):
            return arr[None]
        if all(isinstance(d, int) and d > 0 for d in target) and int(np.prod(target)) == arr.size:
            return arr.reshape(target)
        return arr

    def infer(self, feeds: Dict[str, np.ndarray]) -> List[np.ndarray]:
        net = self.net
        ex = net.create_extractor()
        for name in self._in_names:
            ex.input(name, self._to_mat(feeds[name]))
        outs = []
        for name, shape in zip(self._out_names, self._out_shapes):
            ret, m = ex.extract(name)
            if ret != 0:
                raise RuntimeError(f"ncnn extract({name}) failed with {ret}")
            outs.append(self._reshape_out(np.array(m, dtype=np.float32), shape))
        return outs

    @property
    def loaded(self) -> bool:
        return self.net is not None

    @property
    def input_names(self) -> List[str]:
        return list(self._in_names)

    def describe(self) -> str:
        if self.use_vulkan:
            return f"{self.name}({self._device_name}, {'fp16' if self.fp16 else 'fp32'})"
        return f"{self.name}({self.threads or 4} threads, {'fp16' if self.fp16 else 'fp32'})"

    def close(self) -> None:
        """Release the Net, then the allocators, then the Vulkan instance
        (in that order) so process exit never tears down a live GPU queue."""
        if self._closed:
            return
        self._closed = True
        try:
            self.net = None
            self._allocs = []
            import gc

            gc.collect()
            if self.use_vulkan:
                self._ncnn.destroy_gpu_instance()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[{self.name}] close: {e}")


class FallbackBackend(InferenceBackend):
    """Try `primary`; if load() fails, log loudly and serve from `fallback`."""

    def __init__(self, primary: InferenceBackend, fallback: InferenceBackend):
        self.primary = primary
        self.fallback = fallback
        self.active: Optional[InferenceBackend] = None
        self.reason: Optional[str] = None
        self.name = primary.name

    def load(self, model_path: str) -> None:
        try:
            self.primary.load(model_path)
            self.active = self.primary
            self.reason = None
            return
        except Exception as e:  # noqa: BLE001
            self.reason = str(e)
            logger.error(
                f"BACKEND FALLBACK: {self.primary.describe()} failed to load "
                f"{os.path.basename(model_path)} ({e}); using {self.fallback.describe()} instead"
            )
            try:
                self.primary.close()
            except Exception:  # noqa: BLE001
                pass
        self.fallback.load(model_path)
        self.active = self.fallback

    def infer(self, feeds):
        return self.active.infer(feeds)

    @property
    def loaded(self) -> bool:
        return self.active is not None and self.active.loaded

    @property
    def input_names(self) -> List[str]:
        return self.active.input_names if self.active else []

    def describe(self) -> str:
        if self.active is self.fallback:
            return f"{self.fallback.describe()} [fallback from {self.primary.name}]"
        return self.primary.describe()

    def close(self) -> None:
        for b in (self.primary, self.fallback):
            c = getattr(b, "close", None)
            if c:
                try:
                    c()
                except Exception:  # noqa: BLE001
                    pass


# --- MNN (Vulkan GPU) via the C shim ------------------------------------------


def convert_onnx_to_mnn(onnx_path: str, mnn_path: str) -> None:
    """
    ONNX -> .mnn. Default route: the `mnn` pip wheel's converter extension
    (`_tools.mnnconvert`) run in a subprocess, deliberately bypassing the wheel's
    `mnnconvert` CLI wrapper, which reports usage (machine-id, model uuid, args)
    to Alibaba Cloud and tries to pip-install an SDK for that. Set
    DETECTOR_MNNCONVERT to a standalone MNNConvert binary to use that instead.
    """
    import subprocess
    import sys

    tool = _find_tool("MNNConvert", "DETECTOR_MNNCONVERT")
    if tool:
        cmd = [tool]
    else:
        cmd = [sys.executable, "-c", "import sys, _tools; _tools.mnnconvert(sys.argv)", "mnnconvert"]
    tmp = mnn_path + ".tmp"
    cmd += ["-f", "ONNX", "--modelFile", onnx_path, "--MNNModel", tmp, "--bizCode", "frigate"]
    logger.info(f"[mnn] converting {onnx_path} with {cmd[0]}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        raise RuntimeError(
            f"mnn conversion failed (rc={r.returncode}; is the `mnn` wheel installed?): "
            f"{(r.stdout + r.stderr).strip()[-2000:]}"
        )
    os.replace(tmp, mnn_path)
    logger.info(f"[mnn] cached converted model at {mnn_path}")


class MnnBackend(InferenceBackend):
    """
    MNN through detector/mnn_shim/libmnn_shim.so (ctypes), which links the
    source-built libMNN.so with the Vulkan backend (see README-linux.md).
    forward_type 7 = Vulkan, 0 = CPU; precision 2 = fp16 ("low"), 1 = fp32.
    One Interpreter/Session is created per load() and reused; batch 1 only.
    """

    name = "mnn-vulkan"
    DEFAULT_SHIM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mnn_shim", "libmnn_shim.so")

    def __init__(self, forward_type: int = 7, fp16: bool = True, threads: Optional[int] = None):
        import ctypes

        self.forward_type = forward_type
        self.fp16 = fp16
        self.threads = threads
        self._h = None
        self._in_names: List[str] = []
        self._in_shapes: List[list] = []
        self._out_names: List[str] = []
        self._out_shapes: List[list] = []
        self._closed = False
        if forward_type == 0:
            self.name = "mnn-cpu"
        shim = os.environ.get("DETECTOR_MNN_SHIM") or self.DEFAULT_SHIM
        if not os.path.isfile(shim):
            raise RuntimeError(f"MNN shim not found at {shim} (build it with `make mnn-shim`)")
        mnn_lib = os.environ.get("DETECTOR_MNN_LIB")
        if mnn_lib:
            ctypes.CDLL(mnn_lib, mode=ctypes.RTLD_GLOBAL)  # override the shim's rpath libMNN
        lib = ctypes.CDLL(shim)
        c = ctypes
        lib.mnn_shim_create.restype = c.c_void_p
        lib.mnn_shim_create.argtypes = [c.c_char_p, c.c_int, c.c_int, c.c_int, c.c_char_p, c.c_int]
        lib.mnn_shim_num_inputs.restype = c.c_int
        lib.mnn_shim_num_inputs.argtypes = [c.c_void_p]
        lib.mnn_shim_num_outputs.restype = c.c_int
        lib.mnn_shim_num_outputs.argtypes = [c.c_void_p]
        lib.mnn_shim_io_info.restype = c.c_int
        lib.mnn_shim_io_info.argtypes = [c.c_void_p, c.c_int, c.c_int, c.c_char_p, c.c_int, c.POINTER(c.c_int), c.c_int]
        lib.mnn_shim_infer.restype = c.c_int
        lib.mnn_shim_infer.argtypes = [c.c_void_p, c.POINTER(c.c_void_p), c.POINTER(c.c_size_t), c.POINTER(c.c_void_p), c.POINTER(c.c_size_t)]
        lib.mnn_shim_destroy.restype = None
        lib.mnn_shim_destroy.argtypes = [c.c_void_p]
        self._lib = lib
        self._ct = c
        logger.info(f"[{self.name}] shim {shim}, forward_type={forward_type}, fp16={fp16}")

    @staticmethod
    def cache_path(model_path: str) -> str:
        return os.path.splitext(model_path)[0] + ".mnn"

    def _ensure_converted(self, model_path: str) -> str:
        mnn = self.cache_path(model_path)
        try:
            fresh = os.path.getmtime(mnn) >= os.path.getmtime(model_path) and os.path.getsize(mnn) > 0
        except OSError:
            fresh = False
        if fresh:
            logger.info(f"[{self.name}] using cached MNN model {mnn}")
        else:
            convert_onnx_to_mnn(model_path, mnn)
        return mnn

    def _io(self, h, which: int, n: int):
        c = self._ct
        names, shapes = [], []
        for i in range(n):
            name = c.create_string_buffer(512)
            dims = (c.c_int * 8)()
            nd = self._lib.mnn_shim_io_info(h, which, i, name, 512, dims, 8)
            if nd < 0:
                raise RuntimeError("mnn_shim_io_info failed")
            names.append(name.value.decode())
            shapes.append([int(dims[k]) for k in range(nd)])
        return names, shapes

    def load(self, model_path: str) -> None:
        c = self._ct
        ins, outs = _onnx_io_meta(model_path)
        mnn = self._ensure_converted(model_path)
        err = c.create_string_buffer(1024)
        h = self._lib.mnn_shim_create(mnn.encode(), self.forward_type, 2 if self.fp16 else 1,
                                      int(self.threads or 4) if self.forward_type == 0 else 1, err, 1024)
        if not h:
            raise RuntimeError(f"MNN create failed: {err.value.decode(errors='replace')}")
        in_names, in_shapes = self._io(h, 0, self._lib.mnn_shim_num_inputs(h))
        out_names, out_shapes = self._io(h, 1, self._lib.mnn_shim_num_outputs(h))
        onnx_out = [n for n, _ in outs]
        if set(out_names) != set(onnx_out) or set(in_names) != set(n for n, _ in ins):
            self._lib.mnn_shim_destroy(h)
            raise RuntimeError(f"MNN graph I/O {in_names}->{out_names} does not match ONNX")
        # keep ONNX order for outputs (post-processing relies on it)
        order = [out_names.index(n) for n in onnx_out]
        out_names = [out_names[i] for i in order]
        out_shapes = [out_shapes[i] for i in order]
        old = self._h
        self._h, self._in_names, self._in_shapes, self._out_names, self._out_shapes = h, in_names, in_shapes, out_names, out_shapes
        self._out_order = order
        logger.info(f"[{self.name}] loaded {os.path.basename(model_path)}: inputs {list(zip(in_names, in_shapes))} outputs {list(zip(out_names, out_shapes))}")
        y = self.infer({n: np.zeros(s, np.float32) for n, s in zip(in_names, in_shapes)})
        logger.info(f"[{self.name}] warm-up ok: outputs {[(a.shape, a.dtype.name) for a in y]}")
        if old:
            self._lib.mnn_shim_destroy(old)

    def infer(self, feeds: Dict[str, np.ndarray]) -> List[np.ndarray]:
        c = self._ct
        n_in, n_out = len(self._in_names), len(self._out_names)
        in_arrs = [np.ascontiguousarray(np.asarray(feeds[n]), dtype=np.float32) for n in self._in_names]
        # shim wants outputs in its own (map) order; we return ONNX order
        shim_out = [None] * n_out
        for k, i in enumerate(self._out_order):
            shim_out[i] = np.empty(self._out_shapes[k], np.float32)
        in_ptrs = (c.c_void_p * n_in)(*[a.ctypes.data for a in in_arrs])
        in_sz = (c.c_size_t * n_in)(*[a.size for a in in_arrs])
        out_ptrs = (c.c_void_p * n_out)(*[a.ctypes.data for a in shim_out])
        out_sz = (c.c_size_t * n_out)(*[a.size for a in shim_out])
        rc = self._lib.mnn_shim_infer(self._h, in_ptrs, in_sz, out_ptrs, out_sz)
        if rc != 0:
            raise RuntimeError(f"mnn_shim_infer failed rc={rc}")
        return [shim_out[i] for i in self._out_order]

    @property
    def loaded(self) -> bool:
        return self._h is not None

    @property
    def input_names(self) -> List[str]:
        return list(self._in_names)

    def describe(self) -> str:
        dev = "Vulkan" if self.forward_type == 7 else ("OpenCL" if self.forward_type == 3 else "CPU")
        return f"{self.name}({dev}, {'fp16' if self.fp16 else 'fp32'})"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        h, self._h = self._h, None
        if h:
            try:
                self._lib.mnn_shim_destroy(h)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{self.name}] close: {e}")


# --- factories -------------------------------------------------------------


def _cpu_backend(models_dir: str, threads: Optional[int], providers) -> InferenceBackend:
    b = OnnxRuntimeBackend(["CPUExecutionProvider"], intra_op_threads=threads)
    b.name = "cpu"
    return b


def _coreml_backend(models_dir: str, threads: Optional[int], providers) -> InferenceBackend:
    cache_dir = os.path.join(models_dir, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    coreml_options = {
        "ModelFormat": "MLProgram",  # MLProgram format for better performance
        "MLComputeUnits": "ALL",  # CPU + GPU + Neural Engine
        "ModelCacheDirectory": cache_dir,
    }
    b = OnnxRuntimeBackend(
        [("CoreMLExecutionProvider", coreml_options), "CPUExecutionProvider"],
        intra_op_threads=threads,
    )
    b.name = "coreml"
    return b


def _onnxruntime_backend(models_dir: str, threads: Optional[int], providers) -> InferenceBackend:
    """Explicit provider list, e.g. --providers CUDAExecutionProvider CPUExecutionProvider."""
    if not providers:
        raise ValueError("backend 'onnxruntime' requires --providers")
    specs: List[ProviderSpec] = []
    for p in providers:
        if p == "CoreMLExecutionProvider":
            specs.append(_coreml_backend(models_dir, threads, None).providers[0])
        else:
            specs.append(p)
    return OnnxRuntimeBackend(specs, intra_op_threads=threads)


def _env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")


def _ncnn_vulkan_backend(models_dir: str, threads: Optional[int], providers) -> InferenceBackend:
    """ncnn on the GPU (Vulkan), fp16 unless DETECTOR_NCNN_FP16=0. Raises if there
    is no usable Vulkan device; if a model later fails to load on it, the wrapper
    falls back to the cpu backend for that model."""
    primary = NcnnBackend(use_vulkan=True, fp16=_env_flag("DETECTOR_NCNN_FP16", True), threads=threads)
    return FallbackBackend(primary, _cpu_backend(models_dir, threads, providers))


def _ncnn_cpu_backend(models_dir: str, threads: Optional[int], providers) -> InferenceBackend:
    """ncnn NEON CPU path (fp16 arithmetic by default: ~5 ms on the M1 vs 12 ms ORT)."""
    primary = NcnnBackend(use_vulkan=False, fp16=_env_flag("DETECTOR_NCNN_FP16", True), threads=threads)
    return FallbackBackend(primary, _cpu_backend(models_dir, threads, providers))


def _mnn_vulkan_backend(models_dir: str, threads: Optional[int], providers) -> InferenceBackend:
    """MNN on the GPU (Vulkan) through the C shim; fp16 unless DETECTOR_MNN_FP16=0."""
    primary = MnnBackend(forward_type=7, fp16=_env_flag("DETECTOR_MNN_FP16", True), threads=threads)
    return FallbackBackend(primary, _cpu_backend(models_dir, threads, providers))


# GPU backends in order of preference; 'vulkan' resolves to the first one that
# initialises. Override with DETECTOR_VULKAN_PREFERENCE="mnn-vulkan,ncnn-vulkan".
VULKAN_PREFERENCE: List[str] = [
    n.strip() for n in os.environ.get("DETECTOR_VULKAN_PREFERENCE", "ncnn-vulkan,mnn-vulkan").split(",") if n.strip()
]


def _vulkan_backend(models_dir: str, threads: Optional[int], providers) -> InferenceBackend:
    errors = []
    for name in VULKAN_PREFERENCE:
        try:
            return BACKENDS[name](models_dir, threads, providers)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            logger.warning(f"[vulkan] {name} unavailable: {e}")
    raise RuntimeError("no Vulkan backend available: " + "; ".join(errors))


# name -> factory(models_dir, threads, providers)
BACKENDS: Dict[str, Callable[..., InferenceBackend]] = {
    "cpu": _cpu_backend,
    "coreml": _coreml_backend,
    "onnxruntime": _onnxruntime_backend,
    "ncnn-vulkan": _ncnn_vulkan_backend,
    "ncnn-cpu": _ncnn_cpu_backend,
    "mnn-vulkan": _mnn_vulkan_backend,
    "vulkan": _vulkan_backend,  # alias -> best available GPU backend
}


def available_backends() -> List[str]:
    return ["auto"] + sorted(BACKENDS)


def default_backend_name() -> str:
    """'coreml' on Apple Silicon macOS when the EP is present, else 'cpu'."""
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            import onnxruntime as ort

            if "CoreMLExecutionProvider" in ort.get_available_providers():
                return "coreml"
        except Exception:  # noqa: BLE001
            pass
    return "cpu"


def create_backend(
    name: str = "auto",
    *,
    models_dir: str,
    threads: Optional[int] = None,
    providers: Optional[Sequence[str]] = None,
) -> InferenceBackend:
    """Build a backend by name. 'auto' picks default_backend_name()."""
    if providers and name in ("auto", "onnxruntime"):
        name = "onnxruntime"
    elif name == "auto":
        name = default_backend_name()
    try:
        factory = BACKENDS[name]
    except KeyError:
        raise ValueError(f"Unknown backend '{name}'. Available: {available_backends()}")
    try:
        backend = factory(models_dir, threads, providers)
    except Exception as e:  # noqa: BLE001
        if name == "cpu":
            raise
        logger.error(
            f"BACKEND FALLBACK: could not initialise backend '{name}' ({e}); "
            f"using 'cpu' instead"
        )
        backend = _cpu_backend(models_dir, threads, providers)
    logger.info(f"Using inference backend: {backend.describe()}")
    return backend
