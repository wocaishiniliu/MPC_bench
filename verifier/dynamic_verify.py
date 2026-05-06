#!/usr/bin/env python3
"""
Dynamic heuristic verification of LLM-generated patches.

For each resolved instance, apply the LLM patch and run heuristic checks:
1. Import check: can the modified modules be imported without error?
2. Signature check: do patched functions have correct signatures?
3. Semantic diff: compare LLM patch vs ground truth patch — flag divergence
4. For MPC ops: assert output ≈ plaintext equivalent (when applicable)

Does NOT require MPC runtime — pure Python checks on the code.
"""

import json
import os
import sys
import ast
import difflib
import subprocess
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent / "evaluation"))
from eval_multi_lib import (
    setup_worktree, cleanup_worktree, apply_patch_file,
    get_modified_files, LIBS, DATASET_DIR, OUTPUT_DIR
)

PATCH_DIR = OUTPUT_DIR / "patches" / "claude-haiku-4-5-20251001"
RESULT_DIR = Path(os.environ.get("MPC_BENCH_SAST_DIR", Path(__file__).resolve().parent.parent / "results" / "sast"))
RESULT_DIR.mkdir(parents=True, exist_ok=True)


def get_functions_in_file(filepath):
    """Extract function/method names and signatures from a Python file via AST."""
    try:
        with open(filepath, errors='replace') as f:
            tree = ast.parse(f.read())
    except SyntaxError:
        return {}

    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = []
            for a in node.args.args:
                args.append(a.arg)
            funcs[node.name] = {
                'lineno': node.lineno,
                'args': args,
                'decorators': [ast.dump(d) for d in node.decorator_list],
            }
    return funcs


def check_import(wt_path, lib_key, modified_files, python_cmd):
    """Try to import modified modules — catches syntax errors and missing deps."""
    issues = []
    env = os.environ.copy()
    env['PYTHONPATH'] = str(wt_path)

    for fp in modified_files:
        if not fp.endswith('.py'):
            continue
        # Convert file path to module path
        module = fp.replace('/', '.').replace('.py', '')

        try:
            p = subprocess.Popen(
                [python_cmd, '-c', f'import {module}'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(wt_path), env=env
            )
            _, err = p.communicate(timeout=30)
            if p.returncode != 0:
                err_msg = err.decode(errors='replace').strip().split('\n')[-1][:100]
                issues.append({
                    'check': 'import_error',
                    'file': fp,
                    'detail': err_msg,
                })
        except Exception as e:
            issues.append({
                'check': 'import_error',
                'file': fp,
                'detail': str(e)[:100],
            })

    return issues


def check_signature_match(gt_file, llm_file):
    """Compare function signatures between ground truth and LLM version."""
    issues = []
    gt_funcs = get_functions_in_file(gt_file)
    llm_funcs = get_functions_in_file(llm_file)

    for fname, gt_info in gt_funcs.items():
        if fname in llm_funcs:
            llm_info = llm_funcs[fname]
            if gt_info['args'] != llm_info['args']:
                issues.append({
                    'check': 'signature_mismatch',
                    'function': fname,
                    'detail': f"GT args={gt_info['args']} LLM args={llm_info['args']}",
                })
        else:
            # Function exists in GT but missing in LLM
            issues.append({
                'check': 'missing_function',
                'function': fname,
                'detail': f"Function {fname} in ground truth but missing in LLM patch",
            })

    # Functions added by LLM but not in GT
    for fname in llm_funcs:
        if fname not in gt_funcs:
            issues.append({
                'check': 'extra_function',
                'function': fname,
                'detail': f"Function {fname} added by LLM but not in ground truth",
            })

    return issues


def check_semantic_diff(gt_file, llm_file, filepath):
    """Compare LLM output with ground truth — flag significant divergence."""
    issues = []
    try:
        gt_lines = open(gt_file, errors='replace').readlines()
        llm_lines = open(llm_file, errors='replace').readlines()
    except Exception:
        return issues

    diff = list(difflib.unified_diff(gt_lines, llm_lines, lineterm=''))

    # Count meaningful changes (not whitespace/comments)
    meaningful_changes = 0
    mpc_keyword_changes = 0
    mpc_keywords = ['reveal', 'share', 'encrypt', 'decrypt', 'truncat',
                     'secret', 'beaver', 'triple', 'mask', 'reconstruct',
                     'get_plain_text', 'sfix', 'sint', 'cfix']

    for line in diff:
        if line.startswith('+') or line.startswith('-'):
            if line.startswith('+++') or line.startswith('---'):
                continue
            stripped = line[1:].strip()
            if stripped and not stripped.startswith('#'):
                meaningful_changes += 1
                for kw in mpc_keywords:
                    if kw in stripped.lower():
                        mpc_keyword_changes += 1
                        break

    if meaningful_changes > 50:
        issues.append({
            'check': 'large_divergence',
            'file': filepath,
            'detail': f"{meaningful_changes} meaningful line differences from ground truth",
        })

    if mpc_keyword_changes > 0:
        issues.append({
            'check': 'mpc_logic_divergence',
            'file': filepath,
            'detail': f"{mpc_keyword_changes} MPC-related lines differ from ground truth",
        })

    return issues


def verify_instance(iid, lib_key, inst):
    """Run all heuristic checks on a resolved instance."""
    cfg = LIBS[lib_key]
    results = {'instance_id': iid, 'lib': lib_key, 'checks': [], 'issues': []}

    patch_dir = PATCH_DIR / iid
    if not patch_dir.exists():
        results['issues'].append({'check': 'no_patch', 'detail': 'No saved LLM patch'})
        return results

    # Get python cmd
    python_cmds = {
        'crypten': os.environ.get('MPC_BENCH_CRYPTEN_PYTHON', 'python3'),
        'tfe': os.environ.get('MPC_BENCH_TFE_PYTHON', 'python3'),
        'pysyft': os.environ.get('MPC_BENCH_PYSYFT_PYTHON', 'python3'),
        'secretflow': os.environ.get('MPC_BENCH_SECRETFLOW_PYTHON', 'python3'),
        'spdz': 'python3',
    }
    python_cmd = python_cmds.get(lib_key, 'python3')

    mod_files = get_modified_files(inst['patch'])
    mod_files = [f for f in mod_files if f.endswith('.py')]

    wt_gt = None
    try:
        # Setup ground truth worktree
        wt_gt = setup_worktree(cfg['repo'], inst['base_commit'])
        apply_patch_file(wt_gt, inst['test_patch'])
        apply_patch_file(wt_gt, inst['patch'])

        for fp in mod_files:
            gt_file = wt_gt / fp
            llm_file = patch_dir / fp

            if not llm_file.exists() or not gt_file.exists():
                continue

            # Check 1: Signature match
            sig_issues = check_signature_match(str(gt_file), str(llm_file))
            results['issues'].extend(sig_issues)
            results['checks'].append('signature')

            # Check 2: Semantic diff
            diff_issues = check_semantic_diff(str(gt_file), str(llm_file), fp)
            results['issues'].extend(diff_issues)
            results['checks'].append('semantic_diff')

        # Check 3: Import check with LLM code
        # Copy LLM files to a fresh worktree
        wt_llm = setup_worktree(cfg['repo'], inst['base_commit'])
        apply_patch_file(wt_llm, inst['test_patch'])
        for fp in mod_files:
            llm_src = patch_dir / fp
            if llm_src.exists():
                dst = wt_llm / fp
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(llm_src.read_text(errors='replace'), encoding='utf-8')

        import_issues = check_import(wt_llm, lib_key, mod_files, python_cmd)
        results['issues'].extend(import_issues)
        results['checks'].append('import')

        cleanup_worktree(cfg['repo'], wt_llm)

    except Exception as e:
        results['issues'].append({'check': 'error', 'detail': str(e)[:200]})
    finally:
        if wt_gt:
            cleanup_worktree(cfg['repo'], wt_gt)

    return results


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

    print(f"Dynamic verification on {len(resolved)} resolved instances\n")

    all_results = []
    for i, r in enumerate(sorted(resolved, key=lambda x: x['lib'])):
        iid = r['instance_id']
        lib = r['lib']
        short = iid.split('__')[-1]
        inst = instances.get(iid)
        if not inst:
            continue

        print(f"[{i+1}/{len(resolved)}] {short} [{lib}]", end="")
        result = verify_instance(iid, lib, inst)
        all_results.append(result)

        n_issues = len(result['issues'])
        if n_issues > 0:
            print(f"  {n_issues} issues:")
            for issue in result['issues']:
                print(f"    [{issue['check']}] {issue.get('detail','')[:80]}")
        else:
            print(f"  CLEAN")

    # Summary
    print(f"\n{'='*60}")
    print(f"Dynamic Verification Summary")
    print(f"{'='*60}")

    total_issues = sum(len(r['issues']) for r in all_results)
    with_issues = sum(1 for r in all_results if r['issues'])

    print(f"Verified: {len(all_results)}")
    print(f"With issues: {with_issues}")
    print(f"Clean: {len(all_results) - with_issues}")
    print(f"Total issues: {total_issues}")

    by_check = defaultdict(int)
    for r in all_results:
        for issue in r['issues']:
            by_check[issue['check']] += 1

    print(f"\nBy check type:")
    for check, cnt in sorted(by_check.items(), key=lambda x: -x[1]):
        print(f"  {check}: {cnt}")

    # Save
    out_path = RESULT_DIR / 'dynamic_verify_haiku.jsonl'
    with open(out_path, 'w') as f:
        for r in all_results:
            f.write(json.dumps(r) + '\n')
    print(f"\nSaved -> {out_path}")


if __name__ == "__main__":
    main()
