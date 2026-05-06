# MPC Verifier

The MPC Verifier is the dual-stream security/numerical-fidelity verifier described in §3.3 of the paper. It is invoked on the patches produced by `eval/run_eval.py` for every functionally-resolved instance and emits a verified-resolution decision.

## Two streams

### 1. Domain-Specific SAST (static, source-only)

A Semgrep ruleset tailored to MPC anti-patterns. Categories:

- **Unsafe reveals** — e.g., `reveal()` or `get_plain_text()` inside an iterative loop, or a revealed value sent to `print` / logging.
- **Insecure arithmetic** — raw floating-point coercion on secret-shared types, or non-cryptographic randomness such as `np.random.rand()`.
- **Illegal type casting** — unauthorised conversions between public and private primitives that silently break data-oblivious boundaries.
- **Context-aware crypto-rule violations** — missing synchronisation steps or breaches of context-dependent security policies.

The rule file lives at [`rules/mpc_rules.yaml`](rules/mpc_rules.yaml). The scanner is [`scan_resolved.py`](scan_resolved.py), which:
1. loads the resolved instances from an `eval_*.jsonl` file,
2. re-prompts the LLM and saves the generated source files to disk,
3. runs `semgrep --config rules/mpc_rules.yaml` over those files,
4. emits per-instance flag counts to a SAST result jsonl.

### 2. Dynamic Differential Testing (run-time, MPCDiff-style)

The dynamic stream actually executes the patched code under MPC and compares the secure outputs against a plaintext oracle, using ultra-tight tolerance bounds to surface fixed-point or finite-ring drift.

- [`dynamic_verify.py`](dynamic_verify.py) — main differential-testing harness.
- [`dynamic_assert.py`](dynamic_assert.py) — assertion helpers used by the harness.
- [`heuristic_mpcdiff.py`](heuristic_mpcdiff.py) — MPCDiff-style differential generator.
- [`mpcdiff_verify_crypten.py`](mpcdiff_verify_crypten.py) — CrypTen-specific runner (CrypTen requires CUDA; see top-level README).

## Running the verifier

```bash
# Static stream
python verifier/scan_resolved.py \
    --patches results/eval/eval_crypten_sonnet.jsonl \
    --output  results/sast/sast_crypten_sonnet.jsonl

# Dynamic stream (CrypTen example, requires GPU)
python verifier/dynamic_verify.py \
    --patches results/eval/eval_crypten_sonnet.jsonl \
    --library crypten \
    --output  results/sast/dynamic_crypten_sonnet.jsonl
```

## Required environment variables

The verifier scripts read several environment variables to locate per-library Python interpreters and the result directory. All are optional; the defaults assume a single `python3` on `$PATH` and a `results/sast/` directory next to this README.

| Variable | Purpose | Default |
|---|---|---|
| `MPC_BENCH_SAST_DIR` | Where to write SAST / dynamic output JSONLs | `<repo>/results/sast/` |
| `MPC_BENCH_EVAL_DIR` | Where to read evaluation result JSONLs | `<repo>/results/eval/` |
| `SEMGREP_BIN` | Path to the `semgrep` executable | first `semgrep` on `$PATH` |
| `MPC_BENCH_CRYPTEN_PYTHON` | Python interpreter with CrypTen installed (CUDA-enabled) | `python3` |
| `MPC_BENCH_TFE_PYTHON` | Python interpreter with tf-encrypted installed | `python3` |
| `MPC_BENCH_PYSYFT_PYTHON` | Python interpreter with PySyft installed | `python3` |
| `MPC_BENCH_SECRETFLOW_PYTHON` | Python interpreter with SecretFlow installed | `python3` |

The simplest setup is to use the per-library Docker images in [`../docker/`](../docker/) and set each variable to `python3` inside the container; the per-library installation conflicts otherwise make a single host environment infeasible.

## Output format

Each scanner emits one JSON object per processed instance:

```json
{
  "instance_id": "crypten__37",
  "model": "claude-sonnet-4-6",
  "sast_flags": [
    {"rule_id": "mpc-reveal-in-loop", "file": "...", "line": 42, "severity": "WARNING"}
  ],
  "verified": false
}
```

The `eval/run_eval.py` harness combines the SAST and dynamic streams into a single verified-resolution boolean per instance.
