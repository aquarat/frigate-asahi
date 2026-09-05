#!/usr/bin/env python3
"""
Stand-alone test client that mimics Frigate's zmq_ipc.py detector plugin.

Protocol (identical to frigate/detectors/plugins/zmq_ipc.py):
  1. REQ socket connects to the endpoint.
  2. Handshake: send [ {"model_request": true, "model_name": NAME} ]
     -> reply [ {"model_available": bool, "model_loaded": bool, ...} ]
     If not available, send [ {"model_data": true, "model_name": NAME}, model_bytes ].
  3. Inference: send [ {"shape": [1,3,H,W], "dtype": "float32", "model_type": "yologeneric"}, bytes ]
     -> reply [ header_json, float32 bytes ] with shape [20, 6].

Frigate sends model_type as ModelTypeEnum.<member>.name, so yolo-generic
arrives as the string "yologeneric".

Runs with only pyzmq + numpy, so it also works inside the frigate image:

  docker run --rm --add-host host.docker.internal:host-gateway \
      -v ~/frigate/detector/test_client.py:/test_client.py:ro \
      --entrypoint python3 frigate:latest /test_client.py \
      --endpoint tcp://host.docker.internal:5555
"""

import argparse
import json
import os
import sys
import time

import numpy as np
import zmq

ZERO = np.zeros((20, 6), np.float32)


def decode_response(frames):
    """Copy of ZmqIpcDetector._decode_response."""
    if len(frames) == 1:
        buf = frames[0]
        if len(buf) != 20 * 6 * 4:
            print(f"unexpected payload size: {len(buf)}")
            return ZERO
        return np.frombuffer(buf, dtype=np.float32).reshape((20, 6))
    if len(frames) >= 2:
        header = json.loads(frames[0].decode("utf-8"))
        if "error" in header:
            print(f"detector reported error: {header['error']}")
        shape = tuple(header.get("shape", []))
        dtype = np.dtype(header.get("dtype", "float32"))
        return np.frombuffer(frames[1], dtype=dtype).reshape(shape)
    print("empty reply")
    return ZERO


def handshake(sock, model_name, model_path):
    """Copy of ZmqIpcDetector._check_and_transfer_model / _send_model_data."""
    sock.send_multipart([json.dumps({"model_request": True, "model_name": model_name}).encode()])
    orig = sock.getsockopt(zmq.RCVTIMEO)
    sock.setsockopt(zmq.RCVTIMEO, 30000)
    try:
        frames = sock.recv_multipart()
    finally:
        sock.setsockopt(zmq.RCVTIMEO, orig)
    resp = json.loads(frames[0].decode())
    print(f"model_request reply: {resp}")
    if resp.get("model_available") and resp.get("model_loaded"):
        return True
    if resp.get("model_available") and not resp.get("model_loaded"):
        print("Model exists on detector but failed to load")
        return False
    if not model_path or not os.path.exists(model_path):
        print(f"Detector does not have the model and no local --model-path to push ({model_path})")
        return False
    print(f"Transferring model {model_path} to detector ...")
    with open(model_path, "rb") as f:
        data = f.read()
    sock.send_multipart([json.dumps({"model_data": True, "model_name": model_name}).encode(), data])
    sock.setsockopt(zmq.RCVTIMEO, 30000)
    try:
        frames = sock.recv_multipart()
    finally:
        sock.setsockopt(zmq.RCVTIMEO, orig)
    resp = json.loads(frames[0].decode())
    print(f"model_data reply: {resp}")
    return bool(resp.get("model_saved") and resp.get("model_loaded"))


def make_tensor(args):
    shape = (1, 3, args.size, args.size)
    if args.input == "zeros":
        return np.zeros(shape, np.float32)
    if args.input == "image":
        import cv2  # only needed for --input image

        img = cv2.imread(args.image)
        if img is None:
            sys.exit(f"could not read image {args.image}")
        img = cv2.resize(img, (args.size, args.size))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        return np.ascontiguousarray(img.transpose(2, 0, 1)[None])
    rng = np.random.default_rng(0)
    return rng.random(shape, dtype=np.float32)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--endpoint", default="tcp://127.0.0.1:5555")
    p.add_argument("--model-name", default="fox-person-cat-320.onnx",
                   help="basename Frigate would send (from model.path)")
    p.add_argument("--model-path", default=None,
                   help="local file to push if the detector lacks the model (like Frigate does)")
    p.add_argument("--model-type", default="yologeneric",
                   help="ModelTypeEnum.name as Frigate sends it (yologeneric, dfine, rfdetr)")
    p.add_argument("-n", "--num", type=int, default=50, help="inference requests to send")
    p.add_argument("--duration", type=float, default=None,
                   help="run for this many seconds instead of -n requests (soak testing)")
    p.add_argument("--rate", type=float, default=None,
                   help="pace requests at this many per second (default: as fast as possible)")
    p.add_argument("--report-every", type=float, default=30.0,
                   help="with --duration: print a running latency summary every N seconds")
    p.add_argument("--size", type=int, default=320)
    p.add_argument("--input", choices=["random", "zeros", "image"], default="random")
    p.add_argument("--image", help="image file for --input image")
    p.add_argument("--timeout-ms", type=int, default=200,
                   help="per-request timeout; Frigate's default request_timeout_ms is 200")
    p.add_argument("--labels", default=None, help="optional labelmap file to name class ids")
    args = p.parse_args()

    labels = {}
    if args.labels and os.path.exists(args.labels):
        with open(args.labels) as f:
            for i, line in enumerate(l.strip() for l in f if l.strip()):
                labels[i] = line

    ctx = zmq.Context()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, args.timeout_ms)
    sock.setsockopt(zmq.SNDTIMEO, args.timeout_ms)
    sock.setsockopt(zmq.LINGER, 0)
    print(f"connecting to {args.endpoint}")
    sock.connect(args.endpoint)

    if not handshake(sock, args.model_name, args.model_path):
        sys.exit("handshake failed")

    tensor = make_tensor(args)
    header = json.dumps({
        "shape": list(tensor.shape),
        "dtype": str(tensor.dtype.name),
        "model_type": args.model_type,
    }).encode("utf-8")
    payload = memoryview(tensor.tobytes(order="C"))

    lat = []
    timeouts = 0
    errors = 0
    result = ZERO
    t_start = time.perf_counter()
    t_report = t_start
    i = -1
    while True:
        i += 1
        if args.duration is not None:
            if time.perf_counter() - t_start >= args.duration:
                break
        elif i >= args.num:
            break
        if args.rate:
            due = t_start + i / args.rate
            wait = due - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
        if args.duration is not None and time.perf_counter() - t_report >= args.report_every:
            t_report = time.perf_counter()
            a = np.array(lat[-int(args.report_every * (args.rate or 100)):] or [0.0])
            print(f"[{t_report - t_start:6.0f}s] n={len(lat)} timeouts={timeouts} errors={errors} "
                  f"recent avg={a.mean():.2f} p95={np.percentile(a, 95):.2f} max={a.max():.2f} ms", flush=True)
        t0 = time.perf_counter()
        try:
            sock.send_multipart([header, payload])
            frames = sock.recv_multipart()
            if len(frames) >= 2 and b'"error"' in frames[0]:
                errors += 1
            result = decode_response(frames)
        except zmq.Again:
            timeouts += 1
            print(f"request {i}: timeout after {args.timeout_ms} ms (Frigate would reset its socket here)")
            sock.close(linger=0)
            sock = ctx.socket(zmq.REQ)
            sock.setsockopt(zmq.RCVTIMEO, args.timeout_ms)
            sock.setsockopt(zmq.SNDTIMEO, args.timeout_ms)
            sock.setsockopt(zmq.LINGER, 0)
            sock.connect(args.endpoint)
            continue
        lat.append((time.perf_counter() - t0) * 1000.0)

    print(f"\nlast result shape={result.shape} dtype={result.dtype}")
    print("rows: [class_id, score, y_min, x_min, y_max, x_max]")
    nonzero = result[result[:, 1] > 0]
    for row in nonzero:
        cid = int(row[0])
        name = labels.get(cid, "")
        print(f"  {cid:2d} {name:8s} score={row[1]:.3f} box(y0,x0,y1,x1)=({row[2]:.3f},{row[3]:.3f},{row[4]:.3f},{row[5]:.3f})")
    if len(nonzero) == 0:
        print("  (no detections above threshold - expected for random/zero input)")

    if lat:
        a = np.array(lat)
        elapsed = time.perf_counter() - t_start
        print(f"\n{len(lat)} requests ok, {timeouts} timeouts, {errors} error replies, "
              f"{elapsed:.1f} s, {len(lat) / elapsed:.1f} req/s")
        print(f"round-trip latency: avg={a.mean():.2f} ms  min={a.min():.2f}  p50={np.percentile(a,50):.2f}  "
              f"p95={np.percentile(a,95):.2f}  p99={np.percentile(a,99):.2f}  max={a.max():.2f}")
    else:
        sys.exit("no successful requests")
    sys.exit(1 if (timeouts or errors) else 0)


if __name__ == "__main__":
    main()
