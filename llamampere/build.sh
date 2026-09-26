#!/usr/bin/env bash
# Reproducible build of llamAmpere's llama-server for the dual-3090 box (sm_86, CUDA 12.9, NCCL).
# Output: ./bin (llama-server + its ggml/llama shared libs). Runs inside llama-swap-unified, which already
# provides CUDA 12.9 runtime, cuBLAS, NCCL and libgomp.
#   usage: ./build.sh            (builds the pinned commit + patches/*.patch in a throwaway container)
set -euo pipefail
cd "$(dirname "$0")"
COMMIT=2cb16936b5d081a92f1d0369954561efb6d1e2c7   # JakeATX/llamAmpere main, 2026-09-19 (PR #10 merge; includes v0.3.1)
IMAGE=nvidia/cuda:12.9.1-devel-ubuntu24.04
rm -rf bin.new && mkdir -p bin.new
docker run --rm --cpuset-cpus 5-31 -u 0 -v "$PWD":/work "$IMAGE" bash -c "
  set -euo pipefail
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null && apt-get install -y -qq git cmake build-essential libssl-dev >/dev/null
  git init -q /src && cd /src && git remote add origin https://github.com/JakeATX/llamAmpere
  git fetch -q --depth 1 origin $COMMIT && git checkout -q FETCH_HEAD
  for p in /work/patches/*.patch; do echo \"applying \$(basename \$p)\"; git apply \"\$p\"; done
  cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=86 -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=OFF \
        -DLLAMA_CURL=OFF -DCMAKE_EXE_LINKER_FLAGS=-Wl,--allow-shlib-undefined >/work/bin.new/cmake.log 2>&1
  cmake --build build --target llama-server -j 27 >/work/bin.new/build.log 2>&1
  cp build/bin/llama-server /work/bin.new/ && find build -name '*.so*' -exec cp -a {} /work/bin.new/ \;
  echo \"$COMMIT + \$(ls /work/patches | tr '\n' ' ')\" > /work/bin.new/VERSION
  chown -R $(id -u):$(id -g) /work/bin.new
"
rm -rf bin.old && { [ -d bin ] && mv bin bin.old || true; } && mv bin.new bin
echo "built: $(cat bin/VERSION)"
