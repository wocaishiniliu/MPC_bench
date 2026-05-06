# MPC-Bench

Repository-level benchmark for evaluating LLM code repair on **Secure Multi-Party Computation (MPC)** software.

This repository accompanies the paper *MPC-Bench: Security-Aware LLM Code Generation and Repair for Multi-Party Computation*.

## What's in here

| Path | Contents |
|---|---|
| [`verifier/`](verifier/) | **MPC Verifier**: dual-stream verification framework — Domain-Specific SAST (Semgrep rules + scanner) and dynamic differential testing (MPCDiff-style). |
| [`eval/`](eval/) | **Evaluation harness**: end-to-end pipeline that prompts an LLM, applies its patch to the target repository at the base commit, runs Fail-to-Pass / Pass-to-Pass tests via the appropriate library-specific runner, and (optionally) routes resolved patches through the MPC Verifier. |
| [`docker/`](docker/) | **Container images**: one Dockerfile per evaluated MPC framework (CrypTen, tf-encrypted, MP-SPDZ, SecretFlow, PySyft) for reproducible test execution. |
| [`data/`](data/) | **Dataset**: 205 verified MPC repository-level repair instances in [`data/mpc_bench.jsonl`](data/mpc_bench.jsonl). See [`data/README.md`](data/README.md) for schema and per-library breakdown. |

## Licensing

- **Code**: Apache License 2.0 (see [`LICENSE`](LICENSE)).
- **Dataset**: Creative Commons Attribution 4.0 International (CC BY 4.0; see [`data/LICENSE`](data/LICENSE)).

## Quick start

```bash
# 1. Clone
git clone https://github.com/wocaishiniliu/MPC_bench.git
cd MPC_bench

# 2. Set API keys for the LLMs you want to evaluate
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export GEMINI_API_KEY=AIza...

# 3. Build per-framework container images (requires podman or docker)
bash docker/build_images.sh

# 4. Run the evaluation on one library + one model
python eval/run_eval.py \
    --library crypten \
    --model claude-sonnet-4-6 \
    --dataset data/mpc_bench.jsonl \
    --output results/eval_crypten_sonnet.jsonl

# 5. Run the MPC Verifier on the resolved patches
python verifier/scan_resolved.py \
    --patches results/eval_crypten_sonnet.jsonl \
    --rules verifier/rules/mpc_rules.yaml \
    --output results/verify_crypten_sonnet.jsonl
```

See the per-component READMEs in [`verifier/README.md`](verifier/README.md), [`eval/README.md`](eval/README.md) and [`docker/README.md`](docker/README.md) for details.

## Citing

```
@inproceedings{mpc-bench-2026,
  title  = {MPC-Bench: Security-Aware LLM Code Generation and Repair for Multi-Party Computation},
  author = {Anonymous Authors},
  year   = {2026}
}
```
