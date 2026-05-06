#!/usr/bin/env python3
"""
MPCDiff verification on CrypTen resolved instances.
For each resolved instance, apply the LLM patch, then run MPC operations
and compare against plaintext oracle with tight tolerance.

Tests: add, mul, neg, relu, sigmoid, comparators, matmul
on the LLM-patched CrypTen code.
"""

import json
import os
import sys
import subprocess
import shutil
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent / "evaluation"))
from eval_multi_lib import (
    setup_worktree, cleanup_worktree, apply_patch_file,
    get_modified_files, setup_env, LIBS, DATASET_DIR, OUTPUT_DIR
)

PATCH_BASE = OUTPUT_DIR / "patches" / "claude-sonnet-4-6"
RESULT_DIR = Path(os.environ.get("MPC_BENCH_SAST_DIR", Path(__file__).resolve().parent.parent / "results" / "sast"))
CRYPTEN_PYTHON = os.environ.get("MPC_BENCH_CRYPTEN_PYTHON", "python3")

# MPCDiff test script to inject into worktree
MPCDIFF_TEST = '''#!/usr/bin/env python3
"""MPCDiff: encrypt→compute→decrypt vs plaintext, tight tolerance."""
import torch
import sys
import os

# Torch compat patch
import torch._utils_internal as _tui
if not hasattr(torch.storage, 'TypedStorage'):
    try: torch.storage.TypedStorage = torch.storage._TypedStorage
    except: pass
if not hasattr(_tui, 'get_source_lines_and_file'):
    _tui.get_source_lines_and_file = lambda *a, **k: (None, None)
for _attr in ['HalfStorageBase','QInt32StorageBase','QInt8StorageBase',
              'BFloat16StorageBase','QUInt8StorageBase','QUInt4x2StorageBase',
              'QUInt2x4StorageBase','ComplexFloatStorageBase','ComplexDoubleStorageBase']:
    if not hasattr(torch._C, _attr):
        setattr(torch._C, _attr, type(_attr, (), {}))

import crypten
import crypten.mpc as mpc
import crypten.communicator as comm

TIGHT_TOL = 0.01  # MPCDiff tolerance (much tighter than standard 0.05)
WIDE_TOL = 0.05   # Standard tolerance for reference

results = []

@mpc.run_multiprocess(world_size=2)
def run_mpcdiff():
    rank = comm.get().get_rank()

    # Test inputs: include negatives, zeros, large values, edge cases
    x_vals = torch.tensor([-5.0, -1.5, -0.001, 0.0, 0.001, 1.5, 5.0, 10.0])
    y_vals = torch.tensor([2.0, -0.5, 3.0, 1.0, -2.0, 0.5, -1.0, 4.0])

    x_enc = crypten.cryptensor(x_vals, src=0)
    y_enc = crypten.cryptensor(y_vals, src=0)

    tests = []

    # Test 1: Addition
    try:
        expected = x_vals + y_vals
        result = (x_enc + y_enc).get_plain_text()
        max_err = (result - expected).abs().max().item()
        tests.append(("add", max_err, max_err < TIGHT_TOL))
    except Exception as e:
        tests.append(("add", -1, False, str(e)[:80]))

    # Test 2: Multiplication
    try:
        expected = x_vals * y_vals
        result = (x_enc * y_enc).get_plain_text()
        max_err = (result - expected).abs().max().item()
        tests.append(("mul", max_err, max_err < TIGHT_TOL))
    except Exception as e:
        tests.append(("mul", -1, False, str(e)[:80]))

    # Test 3: Negation
    try:
        expected = -x_vals
        result = (-x_enc).get_plain_text()
        max_err = (result - expected).abs().max().item()
        tests.append(("neg", max_err, max_err < TIGHT_TOL))
    except Exception as e:
        tests.append(("neg", -1, False, str(e)[:80]))

    # Test 4: ReLU
    try:
        expected = torch.relu(x_vals)
        result = x_enc.relu().get_plain_text()
        max_err = (result - expected).abs().max().item()
        tests.append(("relu", max_err, max_err < TIGHT_TOL))
    except Exception as e:
        tests.append(("relu", -1, False, str(e)[:80]))

    # Test 5: Sigmoid (wider range test)
    try:
        sig_input = torch.tensor([-6.0, -3.0, -1.0, 0.0, 1.0, 3.0, 6.0])
        sig_enc = crypten.cryptensor(sig_input, src=0)
        expected = torch.sigmoid(sig_input)
        result = sig_enc.sigmoid().get_plain_text()
        max_err = (result - expected).abs().max().item()
        tests.append(("sigmoid", max_err, max_err < WIDE_TOL))  # sigmoid uses wider tol
    except Exception as e:
        tests.append(("sigmoid", -1, False, str(e)[:80]))

    # Test 6: Comparisons (gt, lt, eq)
    try:
        expected_gt = (x_vals > y_vals).float()
        result_gt = (x_enc > y_enc).get_plain_text()
        max_err = (result_gt - expected_gt).abs().max().item()
        tests.append(("gt", max_err, max_err < TIGHT_TOL))
    except Exception as e:
        tests.append(("gt", -1, False, str(e)[:80]))

    # Test 7: MatMul
    try:
        a = torch.tensor([[1.0, 2.0], [3.0, 4.0]])
        b = torch.tensor([[5.0, 6.0], [7.0, 8.0]])
        a_enc = crypten.cryptensor(a, src=0)
        b_enc = crypten.cryptensor(b, src=0)
        expected = torch.matmul(a, b)
        result = a_enc.matmul(b_enc).get_plain_text()
        max_err = (result - expected).abs().max().item()
        tests.append(("matmul", max_err, max_err < TIGHT_TOL))
    except Exception as e:
        tests.append(("matmul", -1, False, str(e)[:80]))

    # Test 8: Sum
    try:
        expected = x_vals.sum()
        result = x_enc.sum().get_plain_text()
        max_err = (result - expected).abs().item()
        tests.append(("sum", max_err, max_err < TIGHT_TOL))
    except Exception as e:
        tests.append(("sum", -1, False, str(e)[:80]))

    # Output results as JSON
    if rank == 0:
        import json
        output = []
        for t in tests:
            name = t[0]
            err = t[1]
            passed = t[2]
            error_msg = t[3] if len(t) > 3 else None
            output.append({"op": name, "max_err": err, "pass": passed, "error": error_msg})
        print("MPCDIFF_RESULT:" + json.dumps(output))

run_mpcdiff()
'''


def run_mpcdiff_on_instance(iid, inst, lib_key):
    """Apply LLM patch and run MPCDiff verification."""
    cfg = LIBS[lib_key]
    patch_dir = PATCH_BASE / iid
    mod_files = get_modified_files(inst['patch'])
    mod_files = [f for f in mod_files if f.endswith('.py')]

    wt = None
    try:
        wt = setup_worktree(cfg['repo'], inst['base_commit'])

        # Apply test_patch
        apply_patch_file(wt, inst['test_patch'])

        # Apply LLM patch (from saved patches)
        for fp in mod_files:
            llm_src = patch_dir / fp
            if llm_src.exists():
                dst = wt / fp
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(llm_src.read_text(errors='replace'), encoding='utf-8')

        # Write MPCDiff test
        mpcdiff_file = wt / "test_mpcdiff_verify.py"
        mpcdiff_file.write_text(MPCDIFF_TEST, encoding='utf-8')

        # Run
        env = os.environ.copy()
        env['PYTHONPATH'] = str(wt)
        env['OMP_NUM_THREADS'] = '1'

        p = subprocess.Popen(
            [CRYPTEN_PYTHON, str(mpcdiff_file)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(wt), env=env
        )
        out, err = p.communicate(timeout=300)
        output = out.decode(errors='replace')

        # Parse results
        for line in output.split('\n'):
            if 'MPCDIFF_RESULT:' in line:
                json_str = line.split('MPCDIFF_RESULT:')[1].strip()
                return json.loads(json_str)

        return [{"op": "all", "max_err": -1, "pass": False, "error": f"No output. stderr: {err.decode()[-200:]}"}]

    except subprocess.TimeoutExpired:
        try: p.kill()
        except: pass
        return [{"op": "all", "max_err": -1, "pass": False, "error": "TIMEOUT"}]
    except Exception as e:
        return [{"op": "all", "max_err": -1, "pass": False, "error": str(e)[:200]}]
    finally:
        if wt:
            cleanup_worktree(cfg['repo'], wt)


def main():
    # Load Sonnet CrypTen resolved
    resolved = []
    with open(OUTPUT_DIR / "eval_valid_crypten_claude-sonnet-4-6.jsonl") as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                if d['status'] == 'resolved':
                    resolved.append(d)

    # Load dataset
    instances = {}
    with open(DATASET_DIR / LIBS['crypten']['file']) as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                instances[d['instance_id']] = d

    print(f"MPCDiff Verification on {len(resolved)} CrypTen resolved instances\n")

    all_results = []
    n_safe = 0
    n_unsafe = 0

    for i, r in enumerate(resolved):
        iid = r['instance_id']
        short = iid.split('__')[-1]
        inst = instances.get(iid)
        if not inst:
            continue

        print(f"[{i+1}/{len(resolved)}] {short}", end="", flush=True)

        mpcdiff_results = run_mpcdiff_on_instance(iid, inst, 'crypten')

        failed_ops = [t for t in mpcdiff_results if not t['pass']]
        all_pass = len(failed_ops) == 0

        result = {
            'instance_id': iid,
            'verdict': 'SAFE' if all_pass else 'UNSAFE',
            'tests': mpcdiff_results,
            'n_pass': sum(1 for t in mpcdiff_results if t['pass']),
            'n_total': len(mpcdiff_results),
        }
        all_results.append(result)

        if all_pass:
            n_safe += 1
            max_err = max(t['max_err'] for t in mpcdiff_results)
            print(f"  ✓ SAFE (max_err={max_err:.6f})")
        else:
            n_unsafe += 1
            for t in failed_ops:
                err_info = f"err={t['max_err']:.4f}" if t['max_err'] >= 0 else t.get('error', '')[:60]
                print(f"\n    ✗ {t['op']}: {err_info}", end="")
            print()

    # Summary
    print(f"\n{'='*60}")
    print(f"MPCDiff Verification Summary")
    print(f"{'='*60}")
    print(f"Total: {len(all_results)}")
    print(f"  SAFE:   {n_safe}")
    print(f"  UNSAFE: {n_unsafe}")

    if n_unsafe > 0:
        print(f"\nUnsafe instances:")
        for r in all_results:
            if r['verdict'] == 'UNSAFE':
                short = r['instance_id'].split('__')[-1]
                failed = [t for t in r['tests'] if not t['pass']]
                print(f"  {short}:")
                for t in failed:
                    print(f"    {t['op']}: max_err={t['max_err']:.4f}" if t['max_err'] >= 0 else f"    {t['op']}: {t.get('error','')[:80]}")

    # Save
    out_path = RESULT_DIR / 'mpcdiff_verify_crypten.jsonl'
    with open(out_path, 'w') as f:
        for r in all_results:
            f.write(json.dumps(r) + '\n')
    print(f"\nSaved -> {out_path}")


if __name__ == "__main__":
    main()
