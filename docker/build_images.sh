#!/bin/bash
# Build all MPC-Bench per-library evaluation container images.
# Works with either `docker` or `podman`; honour MPC_BENCH_CONTAINER_BIN.
set -e

CONTAINER_BIN="${MPC_BENCH_CONTAINER_BIN:-}"
if [ -z "$CONTAINER_BIN" ]; then
    if command -v podman >/dev/null 2>&1; then
        CONTAINER_BIN=podman
    elif command -v docker >/dev/null 2>&1; then
        CONTAINER_BIN=docker
    else
        echo "ERROR: neither 'podman' nor 'docker' on PATH; "                 \
             "set MPC_BENCH_CONTAINER_BIN to override." >&2
        exit 1
    fi
fi

if [ "$CONTAINER_BIN" = "podman" ] && [ -z "$XDG_RUNTIME_DIR" ]; then
    export XDG_RUNTIME_DIR=/tmp/podman_$UID
    mkdir -p "$XDG_RUNTIME_DIR"
fi

DIR="$(cd "$(dirname "$0")" && pwd)"

for lib in crypten tfe pysyft secretflow spdz; do
    echo "=== Building mpcbench-${lib} (using $CONTAINER_BIN) ==="
    "$CONTAINER_BIN" build -t mpcbench-${lib} -f "${DIR}/Dockerfile.${lib}" "${DIR}"
    echo ""
done

echo "=== Images ==="
"$CONTAINER_BIN" images | grep mpcbench
