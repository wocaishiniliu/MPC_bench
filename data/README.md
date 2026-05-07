# MPC-Patch-Bench dataset

`mpc_bench.jsonl` (also accessible as the symlink alias `mpc_patch_bench.jsonl`) is the main MPC-Patch-Bench benchmark: **205 repository-level MPC bug-fix instances** distilled by the Data Curation Framework of the paper from 7 305 raw pull requests across five open-source MPC frameworks.

Licensed under [CC BY 4.0](LICENSE). Machine-readable metadata (Croissant 1.0 with the Responsible AI extension required by NeurIPS 2026 Datasets & Benchmarks) is provided alongside the data file in [`croissant.json`](croissant.json).

## File at a glance

- One JSON object per line (JSONL).
- Format is intentionally compatible with [SWE-bench](https://www.swebench.com/), with two MPC-specific extensions: every row carries a `library` field and the `instance_id` is prefixed by the library short name so rows from the five frameworks can be mixed safely.

## Per-library breakdown

| `library` | Repository | Instances |
|---|---|---:|
| `crypten`     | facebookresearch/CrypTen      |  61 |
| `tfe`         | tf-encrypted/tf-encrypted     |  43 |
| `spdz`        | data61/MP-SPDZ                |  41 |
| `secretflow`  | secretflow/secretflow         |  26 |
| `pysyft`      | OpenMined/PySyft              |  34 |
| **Total**     |                               | **205** |

## Schema

Each row contains the following **core fields** (present on all 205 instances):

| Field | Type | Description |
|---|---|---|
| `instance_id`       | str  | Unique row identifier, prefixed with the library short name (e.g. `crypten__facebookresearch__CrypTen-326`). |
| `library`           | str  | One of `crypten`, `tfe`, `spdz`, `secretflow`, `pysyft`. |
| `repo`              | str  | Upstream `owner/repo` slug on GitHub. |
| `base_commit`       | str  | Git SHA at which the LLM is asked to produce a fix. |
| `problem_statement` | str  | Natural-language bug report (mirrors the linked GitHub issue or, where the original PR shipped without one, a synthesised statement produced by the Human-AI Completion Engine). |
| `patch`             | str  | The gold patch — a unified diff that resolves the issue. |
| `test_patch`        | str  | The Fail-to-Pass / Pass-to-Pass test patch (also a unified diff). |
| `FAIL_TO_PASS`      | list | Test identifiers that must transition from FAIL → PASS for the instance to be resolved. |
| `PASS_TO_PASS`      | list | Test identifiers that must remain PASS → PASS. |

Each row may also carry the following **optional fields**, retained from the upstream PR for downstream analysis but not required by the harness:

`pr_number`, `test_framework`, `error_type`, `created_at`, `version`, `mpcdiff_type`, `mpcdiff_description`, `pull_number`, `hints_text`, `issue_numbers`.

## Quick load

```python
import json
from collections import defaultdict

rows = [json.loads(l) for l in open("data/mpc_bench.jsonl")]
print(f"Total rows: {len(rows)}")                        # 205
by_lib = defaultdict(list)
for r in rows:
    by_lib[r["library"]].append(r)
for lib, lst in sorted(by_lib.items()):
    print(f"  {lib:12} {len(lst)}")
```

## Loading just one library

The harness in [`../eval/run_eval.py`](../eval/run_eval.py) takes `--library {crypten,tfe,spdz,secretflow,pysyft}` and filters `mpc_bench.jsonl` automatically. From Python, the equivalent is one line:

```python
crypten_only = [r for r in rows if r["library"] == "crypten"]
```

## Provenance

- The 205 instances are drawn from the curation pipeline described in §3.2 of the paper. 596 candidates passed the high-confidence threshold of the curation agent and 579 passed at medium confidence; the Human-AI Completion Engine then produced executable instances for those that lacked developer-authored Fail-to-Pass / Pass-to-Pass tests.
- The strict, unmodified SWE-bench filter applied to the same 7 305 raw PRs retains only 42 instances; the 4.9× expansion to 205 is documented in Appendix A of the paper.

## What's not here

- **Synthetic SWE-smith data** generated via test-breaking mutations on the same five upstream codebases is not included in this commit. The synthetic supplement is non-essential for reproducing the paper's main results and may be released alongside the curation pipeline in a follow-up.
- **Per-model evaluation outputs** (the `eval_*.jsonl` files referenced in the paper) are reproducible from this dataset using `eval/run_eval.py`; we do not ship them as part of the dataset itself.

## Schema compatibility

The `instance_id, repo, base_commit, problem_statement, patch, test_patch, FAIL_TO_PASS, PASS_TO_PASS` field names follow the SWE-bench convention so that existing SWE-bench tooling can ingest MPC-Patch-Bench with only the addition of the `library` filter. Tools that key on `instance_id` should treat the `<library>__` prefix as part of the identifier; library-agnostic tools can simply ignore it.
