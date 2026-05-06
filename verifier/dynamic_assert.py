#!/usr/bin/env python3
"""
Dynamic assertion: run the same test on ground truth AND LLM patch,
compare outputs. If both produce identical results → LLM fix is safe.

For each resolved instance:
1. Worktree A: base_commit + test_patch + ground_truth_patch → run test → capture output
2. Worktree B: base_commit + test_patch + LLM_patch → run test → capture output
3. Assert outputs match
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

PATCH_DIR = OUTPUT_DIR / "patches" / "claude-haiku-4-5-20251001"
RESULT_DIR = Path(os.environ.get("MPC_BENCH_SAST_DIR", Path(__file__).resolve().parent.parent / "results" / "sast"))
RESULT_DIR.mkdir(parents=True, exist_ok=True)

TEST_TIMEOUT = 300


def run_test_capture(lib_key, wt_path, f2p_tests, test_patch, instance):
    """Run tests and capture full stdout/stderr output."""
    python_cmd, env = setup_env(lib_key, wt_path, instance)

    # Extract test files from test_patch
    test_files = [l[6:] for l in test_patch.splitlines() if l.startswith("+++ b/")]

    outputs = {}
    for test_id in f2p_tests:
        parts = test_id.split("::")
        if len(parts) >= 2:
            first = parts[0]
            rest = parts[1:]
            if "/" in first or first.endswith(".py"):
                pytest_id = "::".join([first] + rest)
            else:
                matched_file = None
                for tf in test_files:
                    if tf.endswith(".py"):
                        matched_file = tf
                        break
                pytest_id = "::".join([matched_file] + parts) if matched_file else test_id
        else:
            pytest_id = test_id

        try:
            cmd = [python_cmd or sys.executable, "-m", "pytest", "-xvs", pytest_id]
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(wt_path), env=env
            )
            out, err = p.communicate(timeout=TEST_TIMEOUT)
            output = out.decode(errors="replace") + err.decode(errors="replace")
            passed = p.returncode == 0 or "1 passed" in output
            outputs[test_id] = {
                "passed": passed,
                "returncode": p.returncode,
                "output": output,
            }
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except:
                pass
            outputs[test_id] = {"passed": False, "returncode": -1, "output": "TIMEOUT"}
        except Exception as e:
            outputs[test_id] = {"passed": False, "returncode": -1, "output": str(e)}

    return outputs


def extract_test_results(output_text):
    """Extract meaningful test output lines (assertions, values, errors)."""
    meaningful = []
    for line in output_text.split('\n'):
        line = line.strip()
        # Keep assertion results, printed values, PASSED/FAILED
        if any(kw in line for kw in ['PASSED', 'FAILED', 'assert', 'Assert',
                                       'Error', 'error', '= ', 'result',
                                       'Expected', 'expected', 'Actual', 'actual']):
            meaningful.append(line)
    return meaningful


def main():
    # Load resolved instances
    eval_path = OUTPUT_DIR / "eval_valid99_claude-haiku-4-5-20251001.jsonl"
    resolved = []
    with open(eval_path) as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                if d['status'] == 'resolved':
                    resolved.append(d)

    # Load dataset
    instances = {}
    for lib_key, cfg in LIBS.items():
        with open(DATASET_DIR / cfg['file']) as f:
            for line in f:
                if line.strip():
                    d = json.loads(line)
                    instances[d['instance_id']] = d

    print(f"Dynamic Assert: comparing GT vs LLM on {len(resolved)} resolved instances\n")

    all_results = []

    for i, r in enumerate(sorted(resolved, key=lambda x: x['lib'])):
        iid = r['instance_id']
        lib = r['lib']
        short = iid.split('__')[-1]
        inst = instances.get(iid)
        if not inst:
            continue

        patch_dir = PATCH_DIR / iid
        if not patch_dir.exists():
            print(f"[{i+1}/{len(resolved)}] {short:<25} SKIP (no patch saved)")
            continue

        f2p = inst.get('FAIL_TO_PASS', [])
        if isinstance(f2p, str):
            try:
                f2p = json.loads(f2p)
            except:
                f2p = [f2p]

        print(f"[{i+1}/{len(resolved)}] {short:<25} [{lib}]", end="", flush=True)

        cfg = LIBS[lib]
        mod_files = get_modified_files(inst['patch'])
        mod_files = [f for f in mod_files if f.endswith('.py')]

        wt_gt = None
        wt_llm = None
        result = {'instance_id': iid, 'lib': lib, 'verdict': 'unknown', 'details': {}}

        try:
            # === Worktree A: Ground Truth ===
            wt_gt = setup_worktree(cfg['repo'], inst['base_commit'])
            apply_patch_file(wt_gt, inst['test_patch'])
            apply_patch_file(wt_gt, inst['patch'])
            gt_outputs = run_test_capture(lib, wt_gt, f2p, inst['test_patch'], inst)

            # === Worktree B: LLM Patch ===
            wt_llm = setup_worktree(cfg['repo'], inst['base_commit'])
            apply_patch_file(wt_llm, inst['test_patch'])
            # Apply LLM-generated files
            for fp in mod_files:
                llm_src = patch_dir / fp
                if llm_src.exists():
                    dst = wt_llm / fp
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_text(llm_src.read_text(errors='replace'), encoding='utf-8')
            llm_outputs = run_test_capture(lib, wt_llm, f2p, inst['test_patch'], inst)

            # === Compare ===
            all_match = True
            test_details = {}
            for test_id in f2p:
                gt = gt_outputs.get(test_id, {})
                llm = llm_outputs.get(test_id, {})

                gt_passed = gt.get('passed', False)
                llm_passed = llm.get('passed', False)
                gt_rc = gt.get('returncode', -1)
                llm_rc = llm.get('returncode', -1)

                # Compare return codes
                same_result = (gt_passed == llm_passed) and (gt_rc == llm_rc)

                # Also compare meaningful output lines
                gt_lines = extract_test_results(gt.get('output', ''))
                llm_lines = extract_test_results(llm.get('output', ''))
                output_match = (gt_lines == llm_lines)

                if not same_result:
                    all_match = False

                test_details[test_id] = {
                    'gt_passed': gt_passed,
                    'llm_passed': llm_passed,
                    'result_match': same_result,
                    'output_match': output_match,
                }

            if all_match:
                result['verdict'] = 'SAFE'
                print(f"  ✓ SAFE (GT=LLM, all tests match)")
            else:
                result['verdict'] = 'DIVERGENT'
                for tid, td in test_details.items():
                    if not td['result_match']:
                        print(f"  ✗ DIVERGENT: {tid} GT={'PASS' if td['gt_passed'] else 'FAIL'} LLM={'PASS' if td['llm_passed'] else 'FAIL'}")

            result['details'] = test_details

        except Exception as e:
            result['verdict'] = 'ERROR'
            result['error'] = str(e)[:200]
            print(f"  ERROR: {str(e)[:80]}")
        finally:
            if wt_gt:
                cleanup_worktree(cfg['repo'], wt_gt)
            if wt_llm:
                cleanup_worktree(cfg['repo'], wt_llm)

        all_results.append(result)

    # Summary
    print(f"\n{'='*60}")
    print(f"Dynamic Assert Summary")
    print(f"{'='*60}")

    verdicts = defaultdict(int)
    for r in all_results:
        verdicts[r['verdict']] += 1

    print(f"Total: {len(all_results)}")
    print(f"  SAFE:      {verdicts['SAFE']:>3} (GT output == LLM output)")
    print(f"  DIVERGENT: {verdicts['DIVERGENT']:>3} (GT output != LLM output)")
    print(f"  ERROR:     {verdicts['ERROR']:>3}")

    # Save
    out_path = RESULT_DIR / 'dynamic_assert_haiku.jsonl'
    with open(out_path, 'w') as f:
        for r in all_results:
            f.write(json.dumps(r, default=str) + '\n')
    print(f"\nSaved -> {out_path}")


if __name__ == "__main__":
    main()
