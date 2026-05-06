#!/usr/bin/env python3
"""
SAST scan of LLM-generated fixes for resolved instances.
For each resolved instance, re-call the LLM API, save generated files,
then run Semgrep with MPC-specific rules.
"""

import json
import os
import sys
import time
import subprocess
import shutil
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
from eval_multi_lib import (
    call_llm, build_system_prompt, build_user_prompt,
    parse_file_blocks, get_modified_files, read_file_at,
    get_test_code_from_patch, setup_worktree, cleanup_worktree,
    LIBS, DATASET_DIR,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAST_DIR = Path(os.environ.get("MPC_BENCH_SAST_DIR", REPO_ROOT / "results" / "sast"))
RULES_FILE = Path(__file__).resolve().parent / "rules" / "mpc_rules.yaml"
EVAL_DIR = Path(os.environ.get("MPC_BENCH_EVAL_DIR", REPO_ROOT / "results" / "eval"))
SEMGREP = os.environ.get("SEMGREP_BIN") or shutil.which("semgrep") or "semgrep"


def load_resolved_instances(model_fname, rerun_fname=None):
    """Load resolved instance IDs from eval results."""
    results = {}
    for fname in [model_fname] + ([rerun_fname] if rerun_fname else []):
        try:
            with open(EVAL_DIR / fname) as f:
                for line in f:
                    if line.strip():
                        d = json.loads(line)
                        results[d["instance_id"]] = d
        except FileNotFoundError:
            pass
    return {iid: r for iid, r in results.items() if r["status"] == "resolved"}


def load_dataset():
    """Load all dataset instances."""
    instances = {}
    lib_map = {}
    for lib_key, cfg in LIBS.items():
        ds_path = DATASET_DIR / cfg["file"]
        with open(ds_path) as f:
            for line in f:
                if line.strip():
                    inst = json.loads(line)
                    instances[inst["instance_id"]] = inst
                    lib_map[inst["instance_id"]] = lib_key
    return instances, lib_map


def run_semgrep(target_dir):
    """Run semgrep on target directory, return findings."""
    try:
        result = subprocess.run(
            [SEMGREP, "--config", str(RULES_FILE), "--json", str(target_dir)],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode in (0, 1):  # 0=no findings, 1=findings found
            data = json.loads(result.stdout)
            return data.get("results", [])
    except Exception as e:
        print(f"    Semgrep error: {e}")
    return []


def scan_model(model_name, model_id, eval_fname, rerun_fname=None):
    """Scan all resolved instances for a model."""
    print(f"\n{'='*70}")
    print(f"  Scanning: {model_name} ({model_id})")
    print(f"{'='*70}")

    resolved = load_resolved_instances(eval_fname, rerun_fname)
    if not resolved:
        print(f"  No resolved instances found")
        return []

    print(f"  Resolved instances: {len(resolved)}")

    instances, lib_map = load_dataset()
    output_dir = SAST_DIR / model_name.replace(" ", "_").replace(".", "")
    output_dir.mkdir(parents=True, exist_ok=True)

    all_findings = []

    for i, (iid, eval_result) in enumerate(sorted(resolved.items())):
        if iid not in instances:
            continue

        inst = instances[iid]
        lib_key = lib_map[iid]
        cfg = LIBS[lib_key]
        short = iid.split("__")[-1]

        print(f"\n  [{i+1}/{len(resolved)}] {short}")

        # Setup worktree to read source files
        wt_path = None
        try:
            wt_path = setup_worktree(cfg["repo"], inst["base_commit"])

            # Apply test_patch first (same as eval)
            from eval_multi_lib import apply_patch_file
            apply_patch_file(wt_path, inst["test_patch"])

            # Read source files
            mod_files = get_modified_files(inst["patch"])
            mod_files = [f for f in mod_files if not f.endswith((".rst", ".md", ".png", ".npy", ".tgz"))
                         and not f.startswith(("docs/", "doc/"))]

            files_content = {}
            for fp in mod_files:
                files_content[fp] = read_file_at(wt_path, fp)

            test_code = get_test_code_from_patch(inst["test_patch"])

            # Call LLM to get fix
            sys_prompt = build_system_prompt(lib_key, cfg["framework"])
            user_prompt = build_user_prompt(inst, files_content, test_code)

            print(f"    Calling {model_id}...")
            raw_response = call_llm(sys_prompt, user_prompt, model_id)

            # Parse file blocks
            file_blocks = parse_file_blocks(raw_response)
            if not file_blocks:
                print(f"    No file blocks in response, skipping")
                continue

            # Save LLM output files to scan directory
            scan_dir = output_dir / short
            if scan_dir.exists():
                shutil.rmtree(str(scan_dir))
            scan_dir.mkdir(parents=True)

            for fp, content in file_blocks.items():
                out_path = scan_dir / fp
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(content, encoding="utf-8")

            # Run Semgrep
            findings = run_semgrep(scan_dir)
            if findings:
                print(f"    SAST findings: {len(findings)}")
                for f in findings:
                    rule = f.get("check_id", "").split(".")[-1]
                    line = f.get("start", {}).get("line", "?")
                    msg = f.get("extra", {}).get("message", "")[:80]
                    print(f"      [{rule}] line {line}: {msg}")

                all_findings.append({
                    "instance_id": iid,
                    "lib": lib_key,
                    "model": model_name,
                    "n_findings": len(findings),
                    "findings": [{
                        "rule": f.get("check_id", ""),
                        "severity": f.get("extra", {}).get("severity", ""),
                        "file": f.get("path", ""),
                        "line": f.get("start", {}).get("line", 0),
                        "message": f.get("extra", {}).get("message", ""),
                    } for f in findings],
                })
            else:
                print(f"    Clean (0 findings)")

            time.sleep(1)  # Rate limit

        except Exception as e:
            print(f"    Error: {e}")
        finally:
            if wt_path:
                cleanup_worktree(cfg["repo"], wt_path)

    return all_findings


def main():
    SAST_DIR.mkdir(parents=True, exist_ok=True)

    # Models to scan - only scan models with resolved instances
    models = [
        ("Opus 4.6", "claude-opus-4-6", "eval_all_claude-opus-4-6.jsonl", None),
        ("GPT-5.4", "gpt-5.4", "eval_all_gpt-5.4.jsonl", "eval_rerun_gpt-5.4.jsonl"),
        ("Haiku 4.5", "claude-haiku-4-5-20251001", "eval_all_claude-haiku-4-5-20251001.jsonl", None),
        ("GPT-4o-mini", "gpt-4o-mini", "eval_all_gpt-4o-mini.jsonl", "eval_rerun_gpt-4o-mini.jsonl"),
    ]

    all_results = []
    for model_name, model_id, eval_fname, rerun_fname in models:
        findings = scan_model(model_name, model_id, eval_fname, rerun_fname)
        all_results.extend(findings)

    # Save results
    out_path = SAST_DIR / "sast_scan_results.jsonl"
    with open(out_path, "w") as f:
        for r in all_results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Summary
    print(f"\n{'='*70}")
    print(f"SAST SCAN SUMMARY")
    print(f"{'='*70}")

    by_model = defaultdict(lambda: {"scanned": 0, "with_findings": 0, "total_findings": 0})
    by_rule = defaultdict(int)
    for r in all_results:
        by_model[r["model"]]["scanned"] += 1
        by_model[r["model"]]["with_findings"] += 1
        by_model[r["model"]]["total_findings"] += r["n_findings"]
        for f in r["findings"]:
            by_rule[f["rule"].split(".")[-1]] += 1

    for model, stats in sorted(by_model.items()):
        print(f"  {model}: {stats['with_findings']} instances with findings, {stats['total_findings']} total")

    print(f"\nBy rule:")
    for rule, cnt in sorted(by_rule.items(), key=lambda x: -x[1]):
        print(f"  {rule}: {cnt}")

    print(f"\nSaved -> {out_path}")


if __name__ == "__main__":
    main()
