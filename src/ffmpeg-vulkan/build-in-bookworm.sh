#!/bin/sh
# Runs INSIDE the debian:bookworm build container `ffbuild-vk` (see research/GPU_RESCALE.md).
# Mounts: /src = ~/frigate/src/ffmpeg-vulkan (FFmpeg/, vk-headers/), /uapi = src/uapi-7.0.13,
#         /opt/ffmpeg-vk = install prefix.
# Container prep (once):
#   apt-get install -y --no-install-recommends build-essential nasm pkg-config ca-certificates libdrm-dev libudev-dev \
#     libssl-dev libx264-dev libopus-dev zlib1g-dev libvulkan-dev libshaderc-dev glslang-dev spirv-tools git
set -ex
git config --global --add safe.directory /src/FFmpeg
cd /src/FFmpeg
git rev-parse HEAD
# Force static x264/opus/shaderc (frigate:latest has none of those .so files).
rm -f /usr/lib/aarch64-linux-gnu/libx264.so /usr/lib/aarch64-linux-gnu/libopus.so /usr/lib/aarch64-linux-gnu/libshaderc.so*
ln -sf libshaderc_combined.a /usr/lib/aarch64-linux-gnu/libshaderc.a
# Debian "unbundles" glslang/SPIRV-Tools from libshaderc_combined.a, so give pkg-config the full static closure.
PC=/usr/lib/aarch64-linux-gnu/pkgconfig/shaderc.pc
grep -q start-group $PC || sed -i 's#^Libs: .*#Libs: -L${libdir} -Wl,--start-group -lshaderc -lglslang -lMachineIndependent -lGenericCodeGen -lOSDependent -lOGLCompiler -lSPIRV -lglslang-default-resource-limits -lSPIRV-Tools-opt -lSPIRV-Tools -Wl,--end-group -lstdc++ -lm -lpthread#' $PC
grep '^Libs' $PC
# Bookworm's Vulkan headers are 1.3.239; FFmpeg 8.1 needs >= 1.3.277 (1.4.317 for the vulkan_1_4 features),
# so the host's vulkan-headers 1.4.341 are overlaid via -I/src/vk-headers. FFmpeg dlopen()s libvulkan.so.1 at
# runtime (no link-time dependency), so the image's libvulkan1 1.3.239 is fine.
# Prefer the source-built shaderc (build-shaderc-in-bookworm.sh) over bookworm's 2023.2 when present.
[ -f /opt/shaderc-static/lib/pkgconfig/shaderc.pc ] && export PKG_CONFIG_PATH=/opt/shaderc-static/lib/pkgconfig:${PKG_CONFIG_PATH:-}
# glslang for the build-time (precompiled) shaders: the shaderc tree ships a current glslang binary.
[ -x /opt/shaderc-static/bin/glslang ] && export PATH=/opt/shaderc-static/bin:$PATH
./configure --prefix=/opt/ffmpeg-vk --enable-gpl --enable-version3 \
  --enable-static --disable-shared --disable-doc --disable-debug --enable-pic --enable-openssl --enable-libx264 \
  --enable-libopus --enable-libdrm --enable-libudev --enable-v4l2-request \
  --enable-vulkan --enable-libshaderc \
  --pkg-config-flags=--static \
  --extra-cflags="-I/uapi -I/src/vk-headers" --extra-libs="-lstdc++ -lm -lpthread" \
  --extra-version=vk-b57fbbe
grep -E '^(CONFIG_VULKAN|CONFIG_VULKAN_1_4|CONFIG_LIBSHADERC|CONFIG_SPIRV_LIBRARY|CONFIG_SPIRV_COMPILER|CONFIG_V4L2_REQUEST|CONFIG_LIBDRM|CONFIG_LIBUDEV|CONFIG_SCALE_VULKAN_FILTER|CONFIG_HWUPLOAD_FILTER|CONFIG_HWDOWNLOAD_FILTER|CONFIG_HWMAP_FILTER)=' ffbuild/config.mak || true
make -j8
make install
/opt/ffmpeg-vk/bin/ffmpeg -hide_banner -version | head -2
/opt/ffmpeg-vk/bin/ffmpeg -hide_banner -filters | grep -E 'vulkan|hwupload|hwdownload|hwmap' || true
/opt/ffmpeg-vk/bin/ffmpeg -hide_banner -hwaccels
ldd /opt/ffmpeg-vk/bin/ffmpeg
