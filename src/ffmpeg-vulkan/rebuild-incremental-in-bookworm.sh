#!/bin/sh
# Incremental rebuild of /opt/ffmpeg-vk INSIDE a debian:bookworm container after source changes
# (e.g. the read-back patches on branch avd-readback-vk). The tree must already be configured by
# build-in-bookworm.sh (ffbuild/config.mak present). Mounts as build-in-bookworm.sh, plus
# /opt/shaderc-static = src/ffmpeg-vulkan/shaderc-static (the static shaderc from build-shaderc-in-bookworm.sh).
#
#   docker run -d --name ffbuild-vk --platform linux/arm64 -v $PWD:/src:z -v $PWD/../uapi-7.0.13:/uapi:z \
#     -v $PWD/shaderc-static:/opt/shaderc-static:z -v /opt/ffmpeg-vk:/opt/ffmpeg-vk:z debian:bookworm sleep infinity
#   docker exec ffbuild-vk sh /src/rebuild-incremental-in-bookworm.sh
set -ex
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends build-essential nasm pkg-config ca-certificates git \
  libdrm-dev libudev-dev libssl-dev libx264-dev libopus-dev zlib1g-dev libvulkan-dev >/dev/null
# same as build-in-bookworm.sh: force static x264/opus (the Frigate image has no .so for them)
rm -f /usr/lib/aarch64-linux-gnu/libx264.so /usr/lib/aarch64-linux-gnu/libopus.so /usr/lib/aarch64-linux-gnu/libshaderc.so*
export PKG_CONFIG_PATH=/opt/shaderc-static/lib/pkgconfig:${PKG_CONFIG_PATH:-}
export PATH=/opt/shaderc-static/bin:$PATH
git config --global --add safe.directory /src/FFmpeg
cd /src/FFmpeg
git log --oneline -3
grep -E '^(prefix|CONFIG_VULKAN|CONFIG_V4L2_REQUEST|CONFIG_LIBSHADERC|CONFIG_SCALE_VULKAN_FILTER)=' ffbuild/config.mak
# make sure the final link is redone (the ffmpeg link rule does not depend on config.mak)
rm -f ffmpeg ffmpeg_g ffprobe ffprobe_g
make -j8
make install
/opt/ffmpeg-vk/bin/ffmpeg -hide_banner -version | head -1
/opt/ffmpeg-vk/bin/ffmpeg -hide_banner -hwaccels
ldd /opt/ffmpeg-vk/bin/ffmpeg
