# Per-library container images

The five MPC frameworks evaluated in MPC-Patch-Bench (CrypTen, tf-encrypted, SecretFlow, PySyft, MP-SPDZ) have non-overlapping Python / TensorFlow / PyTorch / system-package constraints, which makes a single host environment infeasible. We therefore ship one container image per framework; each image contains the framework's runtime dependencies and the test runner the eval harness invokes.

## Building the images

```bash
bash docker/build_images.sh
```

The script auto-detects `podman` or `docker` (set `MPC_BENCH_CONTAINER_BIN` to override). Resulting images:

| Image | Built from | Purpose |
|---|---|---|
| `mpcbench-crypten` | `Dockerfile.crypten` | CrypTen runtime + `pytest`. CrypTen needs CUDA at runtime, so this image is meant to be run on a GPU host. |
| `mpcbench-tfe` | `Dockerfile.tfe` | tf-encrypted (TensorFlow 1.15 era) + `pytest`. CPU only. |
| `mpcbench-pysyft` | `Dockerfile.pysyft` | PySyft + `pytest`. CPU only. |
| `mpcbench-secretflow` | `Dockerfile.secretflow` | SecretFlow + `pytest`. CPU only. |
| `mpcbench-spdz` | `Dockerfile.spdz` | MP-SPDZ compile/emulate toolchain. CPU only. |

## Using the images with the harness

The eval harness expects the upstream framework checkouts to be bind-mounted into the container at known paths and the per-library `MPC_BENCH_*_PYTHON` env var to point to the in-container interpreter. A typical CrypTen invocation looks like:

```bash
docker run --rm --gpus all \
    -v $(pwd):/work \
    -v /path/to/CrypTen:/external/CrypTen \
    -e ANTHROPIC_API_KEY \
    -e MPC_BENCH_CRYPTEN_REPO=/external/CrypTen \
    -e MPC_BENCH_CRYPTEN_PYTHON=python3 \
    mpcbench-crypten \
    python /work/eval/run_eval.py \
        --library crypten --model claude-sonnet-4-6 \
        --dataset /work/data/mpc_bench.jsonl \
        --output  /work/results/eval/eval_crypten_sonnet.jsonl
```

For the four CPU-only libraries the `--gpus all` flag is unnecessary.

## Notes

- The Dockerfiles deliberately install only the framework runtime, not the upstream source: bind-mount the repository at evaluation time so per-instance `git worktree`s and `git checkout`s remain on the host filesystem.
- `Dockerfile.spdz` is intentionally minimal; on first run it downloads MP-SPDZ binaries via the upstream installer.
