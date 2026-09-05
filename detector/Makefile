VENV := venv
PYTHON := python3.11
PIP := $(VENV)/bin/pip3
PY := $(VENV)/bin/python3

.PHONY: help venv install reinstall clean run

help:
	@echo "Targets:"
	@echo "  venv       - Create local virtual environment in $(VENV)/"
	@echo "  install    - Create venv (if needed) and install dependencies"
	@echo "  run        - Run the ZMQ ONNX client"
	@echo ""
	@echo "Examples:"
	@echo "  make install"
	@echo "  make run MODEL=/path/to/model.onnx"
	@echo "  make run MODEL=/path/to/model.onnx ENDPOINT=ipc:///tmp/cache/zmq_detector"
	@echo "  make run MODEL=/path/to/model.onnx PROVIDERS=\"CoreMLExecutionProvider CPUExecutionProvider\" VERBOSE=1"
	@echo "  make run BACKEND=cpu THREADS=4 ENDPOINT=tcp://0.0.0.0:5555   # Linux/CPU (see README-linux.md)"

venv:
	$(PYTHON) -m venv $(VENV)

install: venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

run: venv
	$(PY) detector/zmq_onnx_client.py $(if $(MODEL),--model $(MODEL),) $(if $(ENDPOINT),--endpoint $(ENDPOINT),) $(if $(BACKEND),--backend $(BACKEND),) $(if $(THREADS),--threads $(THREADS),) $(if $(MODELS_DIR),--models-dir $(MODELS_DIR),) $(if $(PROVIDERS),--providers $(PROVIDERS),) $(if $(VERBOSE),-v,)

reinstall: clean install

clean:
	rm -rf $(VENV)


 
# --- Linux / Vulkan extras (see README-linux.md) ---------------------------------
MNN_SRC ?= $(HOME)/frigate/research/MNN-src

.PHONY: mnn-shim
# Build the ctypes shim for the mnn-vulkan backend against the source-built libMNN.so
mnn-shim:
	g++ -O2 -std=c++17 -shared -fPIC mnn_shim/mnn_shim.cpp -o mnn_shim/libmnn_shim.so \
	    -I$(MNN_SRC)/include -L$(MNN_SRC)/build -lMNN -Wl,-rpath,$(MNN_SRC)/build
