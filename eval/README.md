# Evaluation harness

End-to-end pipeline that prompts an LLM, applies its patch to the target framework's upstream repository at the base commit, runs the appropriate per-library test runner, and emits a JSONL of per-instance results.

## Files

| File | Role |
|---|---|
| [`run_eval.py`](run_eval.py) | CLI entry point. One invocation = one `(model, library)` pair. |
| [`eval_multi_lib.py`](eval_multi_lib.py) | Shared core: LLM API wrappers, prompt builders, patch application, per-library test runners. |

## Usage

```bash
python eval/run_eval.py \
    --library crypten \
    --model claude-sonnet-4-6 \
    --dataset data/mpc_bench.jsonl \
    --output  results/eval/eval_crypten_sonnet.jsonl
```

`run_eval.py` is **resume-safe**: instances whose `instance_id` is already present in `--output` are skipped, so a partial run can be continued by re-issuing the same command.

Optional flags:

- `--limit N` — evaluate only the first N instances (smoke test).
- `--instance-ids ID1 ID2 ...` — evaluate only this explicit list.

## Required environment

### LLM API keys (set whichever backends you intend to use)

| Variable | Used by |
|---|---|
| `ANTHROPIC_API_KEY` | Claude Sonnet / Opus / Haiku |
| `OPENAI_API_KEY` | GPT-4.1 / GPT-4o-mini / GPT-5.x |
| `GEMINI_API_KEY` | Gemini 2.5 Pro / Gemini 2.5 Flash-Lite |
| `OPENROUTER_API_KEY` | DeepSeek-R1 and other OpenRouter-hosted models |

Models that do not have a key set will simply be unavailable; an explicit key is required at the command line via `--model <id>`.

### Per-library upstream repositories and Python interpreters

The harness needs a clean checkout of each MPC framework and a Python interpreter that can import that framework. The recommended setup is to use the Docker images in [`../docker/`](../docker/) and bind-mount the upstream repos at the standard locations; the harness reads these paths from environment variables:

| Variable | What it points at | Default |
|---|---|---|
| `MPC_BENCH_EXTERNAL_DIR` | parent dir for all upstream repos | `<repo>/external/` |
| `MPC_BENCH_CRYPTEN_REPO` | upstream CrypTen checkout | `$MPC_BENCH_EXTERNAL_DIR/CrypTen` |
| `MPC_BENCH_TFE_REPO` | upstream tf-encrypted checkout | `$MPC_BENCH_EXTERNAL_DIR/tf-encrypted` |
| `MPC_BENCH_SPDZ_REPO` | upstream MP-SPDZ checkout | `$MPC_BENCH_EXTERNAL_DIR/MP-SPDZ` |
| `MPC_BENCH_SECRETFLOW_REPO` | upstream SecretFlow checkout | `$MPC_BENCH_EXTERNAL_DIR/secretflow` |
| `MPC_BENCH_PYSYFT_REPO` | upstream PySyft checkout | `$MPC_BENCH_EXTERNAL_DIR/PySyft` |
| `MPC_BENCH_CRYPTEN_PYTHON` | Python interpreter with CrypTen + CUDA | `python3` |
| `MPC_BENCH_TFE_PYTHON` | Python interpreter with tf-encrypted | `python3` |
| `MPC_BENCH_SECRETFLOW_PYTHON` | Python interpreter with SecretFlow | `python3` |
| `MPC_BENCH_PYSYFT_PYTHON` | Python interpreter with PySyft | `python3` |
| `MPC_BENCH_CRYPTEN_WORKTREE`, `MPC_BENCH_TFE_WORKTREE`, `MPC_BENCH_PYSYFT_WORKTREE` | optional `git worktree` location for parallel runs | use main repo |
| `MPC_BENCH_DATASET_DIR` | dataset directory for backward-compat per-library jsonls | `<repo>/data/` |
| `MPC_BENCH_OUTPUT_DIR` | where harness writes intermediate output | `<repo>/results/eval/` |

## Output format

Each line of the output JSONL is a per-instance result of the form:

```json
{
  "instance_id": "crypten__37",
  "library": "crypten",
  "model": "claude-sonnet-4-6",
  "resolved": true,
  "f2p": {"passed": ["test_foo::test_bar"], "failed": []},
  "p2p": {"passed": [...], "failed": []},
  "patch": "<the model-emitted unified diff>",
  "duration_seconds": 87.3
}
```

Resolved patches can be fed into the [`../verifier/`](../verifier/) for security and numerical-fidelity checking.
