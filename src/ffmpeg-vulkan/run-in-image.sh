#!/bin/bash
# Run /opt/ffmpeg-vk/bin/ffmpeg INSIDE a throwaway `frigate:latest` container with the host's Honeykrisp
# Vulkan ICD made available. Never touches the container named `frigate`.
#
#   run-in-image.sh [ffmpeg args...]               -> ffmpeg (Vulkan-capable) inside frigate:latest
#   RUN_IMAGE=debian:bookworm run-in-image.sh ...  -> same in a plain bookworm image
#   RUN_ENTRY=bash run-in-image.sh -c '...'        -> a shell inside the same environment (ffmpeg is $FFMPEG_VK)
#
# How it works (see research/GPU_RESCALE.md, "Container ICD recipe"):
#   * the host's /usr/lib64 is bind-mounted read-only at /host-lib64 and the host's dynamic loader at
#     /host-lib64-ld.so. libvulkan_asahi.so needs GLIBC_2.38 and Fedora's libstdc++, which bookworm (glibc 2.36)
#     does not have, so ffmpeg is started through Fedora's ld.so with --library-path /host-lib64. Only that one
#     process uses the Fedora userspace; the rest of the container is untouched.
#   * VK_DRIVER_FILES points the (Fedora) Vulkan loader at an ICD json whose library_path is /host-lib64/libvulkan_asahi.so.
#   * /dev/dri/renderD128 (world rw on this host) is passed through.
#   * /opt/ffmpeg-vk is the static bookworm build (Kwiboo v4l2-request-n8.1 b57fbbe + --enable-vulkan --enable-libshaderc).
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
IMAGE=${RUN_IMAGE:-frigate:latest}
ENTRY=${RUN_ENTRY:-}
RESEARCH=${RESEARCH:-${FRIGATE_HOME:-$HOME/frigate}/research}
EXTRA_DOCKER_ARGS=${EXTRA_DOCKER_ARGS:-}   # e.g. "--device /dev/video0 --device /dev/media0" for the AVD step

FFMPEG_VK='/host-lib64-ld.so --library-path /host-lib64 /opt/ffmpeg-vk/bin/ffmpeg'
if [ -z "$ENTRY" ]; then
  ENTRY=/host-lib64-ld.so
  set -- --library-path /host-lib64 /opt/ffmpeg-vk/bin/ffmpeg "$@"
fi

exec docker run --rm -i \
  --device /dev/dri/renderD128 \
  -v /usr/lib64:/host-lib64:ro \
  -v /usr/lib/ld-linux-aarch64.so.1:/host-lib64-ld.so:ro \
  -v "$HERE/icd:/etc/vulkan-asahi:ro,z" \
  -v /opt/ffmpeg-vk:/opt/ffmpeg-vk:ro,z \
  -v "$RESEARCH:/research:z" \
  -e VK_DRIVER_FILES=/etc/vulkan-asahi/asahi_icd.container.json \
  -e VK_ICD_FILENAMES=/etc/vulkan-asahi/asahi_icd.container.json \
  -e FFMPEG_VK="$FFMPEG_VK" \
  $EXTRA_DOCKER_ARGS \
  --entrypoint "$ENTRY" "$IMAGE" "$@"
