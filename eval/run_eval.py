#!/usr/bin/env python3
"""Unified MPC-Bench evaluation CLI.

Evaluates a single LLM on a single MPC framework's slice of MPC-Bench by:
  1. loading the dataset jsonl,
  2. for each instance: prompting the model, parsing the returned files,
     applying the patch to the framework's upstream repo at the base
     commit, running the per-library test runner,
  3. emitting one JSONL row per instance to ``--output``.

Resume-safe: instances whose ``instance_id`` already appears in the
output file are skipped.

Examples
--------

    # Evaluate Sonnet 4.6 on the CrypTen slice
    python eval/run_eval.py \\
        --library crypten \\
        --model claude-sonnet-4-6 \\
        --dataset data/mpc_bench.jsonl \\
        --output  results/eval/eval_crypten_sonnet.jsonl

    # Evaluate every library with a single model
    for lib in crypten tfe spdz secretflow pysyft; do
        python eval/run_eval.py --library $lib --model gpt-5.4 \\
            --dataset data/mpc_bench.jsonl \\
            --output  results/eval/eval_${lib}_gpt54.jsonl
    done
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval_multi_lib import LIBS, eval_instance


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run MPC-Bench evaluation for one (model, library) pair.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--library", required=True, choices=sorted(LIBS.keys()),
                   help="Which MPC library slice of the benchmark to evaluate.")
    p.add_argument("--model", required=True,
                   help="Model identifier passed to call_llm() "
                        "(e.g. claude-sonnet-4-6, gpt-5.4, gemini-2.5-pro).")
    p.add_argument("--dataset", required=True, type=Path,
                   help="Path to the merged mpc_bench.jsonl dataset file.")
    p.add_argument("--output", required=True, type=Path,
                   help="JSONL file to append per-instance results to.")
    p.add_argument("--limit", type=int, default=None,
                   help="Optional: evaluate only the first N instances.")
    p.add_argument("--instance-ids", nargs="+", default=None,
                   help="Optional: evaluate only this list of instance_ids.")
    return p.parse_args()


def load_instances(dataset_path: Path, library: str) -> list[dict]:
    """Load instances for the requested library from the merged jsonl.

    The merged dataset uses a ``library`` field on each row (or the
    ``instance_id`` is prefixed with ``<library>__``); both forms are
    accepted.
    """
    out: list[dict] = []
    with dataset_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            lib = d.get("library")
            if lib is None:
                lib = d.get("instance_id", "").split("__", 1)[0]
            if lib == library:
                out.append(d)
    return out


def already_evaluated(output_path: Path) -> set[str]:
    if not output_path.exists():
        return set()
    seen = set()
    with output_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if "instance_id" in d:
                    seen.add(d["instance_id"])
            except json.JSONDecodeError:
                pass
    return seen


def main() -> int:
    args = parse_args()

    instances = load_instances(args.dataset, args.library)
    if args.instance_ids is not None:
        wanted = set(args.instance_ids)
        instances = [i for i in instances if i.get("instance_id") in wanted]
    if args.limit is not None:
        instances = instances[: args.limit]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    seen = already_evaluated(args.output)
    todo = [i for i in instances if i.get("instance_id") not in seen]

    print(
        f"[run_eval] library={args.library} model={args.model} "
        f"dataset={args.dataset} -> output={args.output}"
    )
    print(f"[run_eval] {len(instances)} candidate instances, "
          f"{len(seen)} already in output, {len(todo)} to evaluate.")

    t0 = time.time()
    for i, inst in enumerate(todo, 1):
        iid = inst.get("instance_id", "<unknown>")
        print(f"[run_eval] [{i}/{len(todo)}] {iid} ...", flush=True)
        try:
            result = eval_instance(inst, args.library, args.model)
        except Exception as e:                           # pragma: no cover
            result = {
                "instance_id": iid,
                "library": args.library,
                "model": args.model,
                "exception": repr(e),
                "resolved": False,
            }
        with args.output.open("a") as f:
            f.write(json.dumps(result) + "\n")

    print(f"[run_eval] done in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
