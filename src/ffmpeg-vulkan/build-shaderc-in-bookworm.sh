#!/bin/sh
# Runs INSIDE `ffbuild-vk`. Bookworm's libshaderc 2023.2 (glslang 12) cannot compile FFmpeg 8.1's runtime
# GLSL (needs GL_EXT_expect_assume), so build a current shaderc (with bundled glslang + SPIRV-Tools) as
# static PIC archives into /opt/shaderc-static and give it a pkg-config file FFmpeg can consume.
set -ex
apt-get install -y -qq --no-install-recommends cmake python3 git ca-certificates >/dev/null
mkdir -p /src/shaderc-build && cd /src/shaderc-build
if [ ! -d shaderc ]; then
  TAG=$(git ls-remote --tags https://github.com/google/shaderc 'v20*' | grep -v '\^{}' | sed 's|.*refs/tags/||' | sort -V | tail -1)
  echo "shaderc tag: $TAG"
  git clone -q --depth 1 -b "$TAG" https://github.com/google/shaderc shaderc
fi
cd shaderc && git log -1 --format='%H %d %s' && ./utils/git-sync-deps >/dev/null 2>&1 && \
  (cd third_party/glslang && git log -1 --format='glslang %H %s') && (cd third_party/spirv-tools && git log -1 --format='spirv-tools %H %s')
mkdir -p build && cd build
cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/shaderc-static -DBUILD_SHARED_LIBS=OFF \
  -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DSHADERC_SKIP_TESTS=ON -DSHADERC_SKIP_EXAMPLES=ON \
  -DSHADERC_SKIP_COPYRIGHT_CHECK=ON -DSHADERC_ENABLE_WERROR_COMPILE=OFF -DSPIRV_SKIP_TESTS=ON -DSPIRV_SKIP_EXECUTABLES=ON \
  -DENABLE_GLSLANG_BINARIES=ON .. >/dev/null
make -j8 >/dev/null 2>&1 || make -j8
make install >/dev/null
ls -la /opt/shaderc-static/lib/ | head -20
# pkg-config: libshaderc_combined.a really is combined here; add the C++ runtime.
mkdir -p /opt/shaderc-static/lib/pkgconfig
cat > /opt/shaderc-static/lib/pkgconfig/shaderc.pc <<PC
prefix=/opt/shaderc-static
libdir=\${prefix}/lib
includedir=\${prefix}/include
Name: shaderc
Description: static shaderc (combined) for ffmpeg-vk
Version: $(sed -n 's/.*shaderc_VERSION[^0-9]*\([0-9.]*\).*/\1/p' /src/shaderc-build/shaderc/CHANGES 2>/dev/null | head -1 | grep . || echo 2025.0)
Libs: -L\${libdir} -lshaderc_combined -lstdc++ -lm -lpthread
Cflags: -I\${includedir}
PC
cat /opt/shaderc-static/lib/pkgconfig/shaderc.pc
ls /opt/shaderc-static/bin 2>/dev/null || true
