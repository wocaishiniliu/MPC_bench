#!/usr/bin/env python3
"""
Multi-library SWE-bench evaluation using Claude API.
Samples N instances per library, sends to Claude, applies patch, runs tests.

Usage:
  python3 scripts/eval_multi_lib.py --per-lib 3 --model claude-sonnet-4-6
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ─── Configuration ────────────────────────────────────────────────────────────

# ─── Path configuration ─────────────────────────────────────────────────────
# All paths are configurable via environment variables; the defaults assume the
# release-repo layout (data/ next to scripts/) and that each MPC framework's
# upstream source repository plus a clean Python interpreter is provided by
# the user.

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = Path(os.environ.get("MPC_BENCH_DATASET_DIR", REPO_ROOT / "data"))
OUTPUT_DIR = Path(os.environ.get("MPC_BENCH_OUTPUT_DIR", REPO_ROOT / "results" / "eval"))

# ─── LLM API keys ───────────────────────────────────────────────────────────
# All keys must be supplied via environment variables. Empty strings disable
# the corresponding backend.

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY",   "")
ANTHROPIC_API_URL   = "https://api.anthropic.com/v1/messages"
OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY",      "")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY",      "")
OPENROUTER_API_KEY  = os.environ.get("OPENROUTER_API_KEY",  "")

MAX_TOKENS = 16384
MAX_FILE_CHARS = 20000
TEST_TIMEOUT = 300  # 5min default; CrypTen multiprocess tests need more time

# ─── Per-library configuration ──────────────────────────────────────────────
# Per-library upstream repository checkouts and Python interpreters are
# configured via environment variables; the simplest setup is to use the
# Docker images in ../docker/ and bind-mount each upstream repo.

def _path_env(var, default):
    val = os.environ.get(var, "")
    return Path(val) if val else default

def _opt_path_env(var):
    val = os.environ.get(var, "")
    return Path(val) if val else None

EXTERNAL_DIR = _path_env("MPC_BENCH_EXTERNAL_DIR", REPO_ROOT / "external")

LIBS = {
    "crypten": {
        "file": "crypten_swebench.jsonl",
        "repo":     _path_env("MPC_BENCH_CRYPTEN_REPO",     EXTERNAL_DIR / "CrypTen"),
        "worktree": _opt_path_env("MPC_BENCH_CRYPTEN_WORKTREE"),
        "python":   os.environ.get("MPC_BENCH_CRYPTEN_PYTHON", "python3"),
        "test_cmd": "pytest",
        "framework": "CrypTen (Privacy-Preserving ML with MPC)",
        "setup": "crypten",
    },
    "tfe": {
        "file": "tfe_swebench.jsonl",
        "repo":     _path_env("MPC_BENCH_TFE_REPO",         EXTERNAL_DIR / "tf-encrypted"),
        "worktree": _opt_path_env("MPC_BENCH_TFE_WORKTREE"),
        "python":   os.environ.get("MPC_BENCH_TFE_PYTHON",  "python3"),
        "test_cmd": "pytest",
        "framework": "TF Encrypted (encrypted ML on TensorFlow)",
        "setup": "tfe",
    },
    "spdz": {
        "file": "spdz_swebench.jsonl",
        "repo":     _path_env("MPC_BENCH_SPDZ_REPO",        EXTERNAL_DIR / "MP-SPDZ"),
        "worktree": None,  # uses main repo
        "python":   None,
        "test_cmd": "mpc_emulate",  # special
        "framework": "MP-SPDZ (Multi-Protocol SPDZ framework)",
        "setup": "spdz",
    },
    "secretflow": {
        "file": "secretflow_swebench.jsonl",
        "repo":     _path_env("MPC_BENCH_SECRETFLOW_REPO",  EXTERNAL_DIR / "secretflow"),
        "worktree": None,
        "python":   os.environ.get("MPC_BENCH_SECRETFLOW_PYTHON", "python3"),
        "test_cmd": "pytest",
        "framework": "SecretFlow (privacy-preserving data analysis)",
        "setup": "secretflow",
    },
    "pysyft": {
        "file": "pysyft_swebench.jsonl",
        "repo":     _path_env("MPC_BENCH_PYSYFT_REPO",      EXTERNAL_DIR / "PySyft"),
        "worktree": _opt_path_env("MPC_BENCH_PYSYFT_WORKTREE"),
        "python":   os.environ.get("MPC_BENCH_PYSYFT_PYTHON", "python3"),
        "test_cmd": "pytest",
        "framework": "PySyft (private deep learning)",
        "setup": "pysyft",
    },
}


# ─── Per-library environment setup ──────────────────────────────────────────

# torch compatibility patch for old CrypTen commits (new PyTorch removed these)
TORCH_COMPAT_PATCH = """\
import torch
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
try:
    crypten.init()
except Exception:
    pass
"""


def setup_env(lib_key, wt_path, instance):
    """Set up the environment for a library in the worktree.
    Returns (python_cmd, env_dict) to use for test execution.
    """
    cfg = LIBS[lib_key]
    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = "1"
    python_cmd = cfg.get("python") or sys.executable

    if lib_key == "crypten":
        # Inject torch compatibility patch as conftest.py
        conftest = wt_path / "conftest.py"
        if not conftest.exists():
            conftest.write_text(TORCH_COMPAT_PATCH, encoding="utf-8")
        else:
            # Prepend to existing conftest
            existing = conftest.read_text(encoding="utf-8")
            if "HalfStorageBase" not in existing:
                conftest.write_text(TORCH_COMPAT_PATCH + "\n" + existing, encoding="utf-8")

        # Also write to test/ directory if it exists
        test_conftest = wt_path / "test" / "conftest.py"
        if (wt_path / "test").is_dir():
            if not test_conftest.exists():
                test_conftest.write_text(TORCH_COMPAT_PATCH, encoding="utf-8")
            elif "HalfStorageBase" not in test_conftest.read_text(encoding="utf-8"):
                existing = test_conftest.read_text(encoding="utf-8")
                test_conftest.write_text(TORCH_COMPAT_PATCH + "\n" + existing, encoding="utf-8")

        env["PYTHONPATH"] = str(wt_path)
        python_cmd = str(BASE_DIR / "crypten_venv/bin/python3")
        pip_cmd = str(BASE_DIR / "crypten_venv/bin/pip3")
        # CrypTen needs pip install for C extensions
        try:
            p = subprocess.Popen(
                [pip_cmd, "install", "-e", ".", "--no-deps", "-q"],
                cwd=str(wt_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            p.communicate(timeout=120)
        except Exception:
            pass

    elif lib_key == "tfe":
        # tfe: use the tf-encrypted Python environment (Python 3.7, TF 1.15),
        # PYTHONPATH only — no pip install
        python_cmd = os.environ.get("MPC_BENCH_TFE_PYTHON", "python3")
        env["PYTHONPATH"] = str(wt_path)

    elif lib_key == "secretflow":
        # secretflow: Python 3.10, PYTHONPATH only.
        # Remove repo's conftest.py which imports spu, multiprocess etc.
        # Our test files are standalone and don't need these heavy dependencies.
        python_cmd = os.environ.get("MPC_BENCH_SECRETFLOW_PYTHON", "python3")
        env["PYTHONPATH"] = str(wt_path)
        for conftest in [wt_path / "tests" / "conftest.py",
                         wt_path / "tests" / "tuner" / "conftest.py",
                         wt_path / "conftest.py"]:
            if conftest.exists():
                conftest.rename(str(conftest) + ".bak")

    elif lib_key == "pysyft":
        # pysyft: Python 3.10, PYTHONPATH to worktree (+ packages/syft if exists)
        python_cmd = os.environ.get("MPC_BENCH_PYSYFT_PYTHON", "python3")
        paths = [str(wt_path)]
        # PySyft has packages/ subdirectory structure in some versions
        if (wt_path / "packages" / "syft").is_dir():
            paths.append(str(wt_path / "packages" / "syft"))
        if (wt_path / "packages" / "syft" / "src").is_dir():
            paths.append(str(wt_path / "packages" / "syft" / "src"))
        env["PYTHONPATH"] = ":".join(paths)
        # Remove repo conftest.py if it imports heavy deps
        for conftest_path in [wt_path / "tests" / "conftest.py",
                               wt_path / "conftest.py"]:
            if conftest_path.exists():
                content = conftest_path.read_text(errors="replace")
                if "syft" in content.lower() or "pytest_plugins" in content:
                    conftest_path.rename(str(conftest_path) + ".bak")

    elif lib_key == "spdz":
        # MP-SPDZ uses compile.py + emulate.sh, needs python 3.11
        env["PATH"] = "/opt/ohpc/pub/apps/python/python-3.11.4-gcc-12.2.0/bin:" + env.get("PATH", "")
        env["PYTHONPATH"] = str(wt_path)

    return python_cmd, env


# ─── Git helpers ──────────────────────────────────────────────────────────────

def git(repo, *args, timeout=30):
    p = subprocess.Popen(
        ["git", "-C", str(repo)] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    out, err = p.communicate(timeout=timeout)
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def setup_worktree(repo, base_commit):
    """Create temp worktree at base_commit, return path."""
    wt = tempfile.mkdtemp(prefix="mpc_eval_")
    rc, out, err = git(repo, "worktree", "add", "--detach", wt, base_commit, timeout=300)
    if rc != 0:
        raise RuntimeError(f"worktree failed: {err[:200]}")
    return Path(wt)


def cleanup_worktree(repo, wt_path):
    """Remove worktree."""
    try:
        git(repo, "worktree", "remove", "--force", str(wt_path), timeout=120)
    except Exception:
        pass
    import shutil
    if wt_path.exists():
        shutil.rmtree(str(wt_path), ignore_errors=True)


def apply_patch_file(wt_path, patch_text):
    """Apply a patch to worktree. Returns (success, message)."""
    if patch_text and not patch_text.endswith("\n"):
        patch_text += "\n"
    pf = os.path.join(str(wt_path), "_patch.diff")
    with open(pf, "w") as f:
        f.write(patch_text)
    rc, out, err = git(wt_path, "apply", "--3way", pf, timeout=30)
    os.unlink(pf)
    if rc == 0:
        return True, "ok"
    # Try with --ignore-whitespace
    pf2 = os.path.join(str(wt_path), "_patch2.diff")
    with open(pf2, "w") as f:
        f.write(patch_text)
    rc2, out2, err2 = git(wt_path, "apply", "--ignore-whitespace", pf2, timeout=30)
    os.unlink(pf2)
    if rc2 == 0:
        return True, "ok(ws)"
    # Fallback: extract new files directly from patch
    ok = _extract_new_files(wt_path, patch_text)
    if ok:
        return True, "ok(extract)"
    return False, err[:200]


def _extract_new_files(wt_path, patch_text):
    """Extract new files from a patch that creates files from /dev/null."""
    files = {}
    current_file = None
    lines = []
    for line in patch_text.split('\n'):
        if line.startswith('+++ b/'):
            if current_file and lines:
                files[current_file] = '\n'.join(lines)
            current_file = line[6:]
            lines = []
        elif line.startswith('--- /dev/null'):
            pass  # new file marker
        elif line.startswith('--- a/'):
            # Modification of existing file - can't handle with extraction
            if current_file and lines:
                files[current_file] = '\n'.join(lines)
            current_file = None
            lines = []
        elif current_file is not None:
            if line.startswith('+') and not line.startswith('+++'):
                lines.append(line[1:])
            elif line.startswith(' '):
                lines.append(line[1:])
            elif line.startswith('@@') or line.startswith('diff '):
                pass  # header lines
    if current_file and lines:
        files[current_file] = '\n'.join(lines)

    if not files:
        return False
    for fp, content in files.items():
        full = Path(str(wt_path)) / fp
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding='utf-8')
    return True


# ─── Claude API ───────────────────────────────────────────────────────────────

def build_system_prompt(lib_name, framework_desc):
    return (
        f"You are an expert developer working on {framework_desc}. "
        f"You will be given a bug report and the relevant source files. "
        f"Your task is to output the COMPLETE fixed content for each file that needs modification.\n\n"
        f"OUTPUT FORMAT (STRICTLY REQUIRED):\n"
        f"For each file that needs changes, output a block like:\n"
        f"<<<FILE: path/to/file.py>>>\n"
        f"<complete new file content here>\n"
        f"<<<END>>>\n\n"
        f"CRITICAL RULES:\n"
        f"- Output ONLY the file blocks. NO explanations, NO reasoning.\n"
        f"- Output the COMPLETE file content (not just changed parts).\n"
        f"- If a file needs to be newly created, still use the same format.\n"
        f"- Do NOT wrap content in markdown code fences."
    )


def build_user_prompt(instance, files_content, test_code):
    lines = ["## Bug Report\n", instance["problem_statement"], "\n"]

    file_list = list(files_content.keys())
    lines.append(f"## Files That Need Modification ({len(file_list)} files)\n")
    for fp in file_list:
        lines.append(f"- `{fp}`")
    lines.append("")

    lines.append("## Current File Contents\n")
    for fp, content in files_content.items():
        lines.append(f"### `{fp}`\n```\n{content}\n```\n")

    lines.append("## Test That Must Pass After Your Fix\n")
    lines.append(f"FAIL_TO_PASS: {instance['FAIL_TO_PASS']}\n")
    if test_code:
        lines.append(f"```\n{test_code}\n```\n")

    lines.append(
        f"## Task\nFix the bug so the above tests pass. "
        f"Output ONLY <<<FILE:...>>><<<END>>> blocks for ALL {len(file_list)} file(s). No other text."
    )
    return "\n".join(lines)


def call_claude(system_prompt, user_prompt, model="claude-sonnet-4-6"):
    """Call Claude API via requests."""
    import requests
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=300)
    if resp.status_code != 200:
        raise RuntimeError(f"API error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    return data["content"][0]["text"]


def call_openai(system_prompt, user_prompt, model="gpt-4.1"):
    """Call OpenAI API via requests with exponential backoff retry."""
    import requests
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    # GPT-5.x and o3/o4 use max_completion_tokens instead of max_tokens
    uses_new_api = model.startswith("gpt-5") or model.startswith("o3") or model.startswith("o4")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if uses_new_api:
        body["max_completion_tokens"] = MAX_TOKENS
    else:
        body["max_tokens"] = MAX_TOKENS
        body["temperature"] = 0
    max_retries = 8
    for attempt in range(max_retries):
        try:
            resp = requests.post("https://api.openai.com/v1/chat/completions",
                                 headers=headers, json=body, timeout=600)
        except requests.exceptions.Timeout:
            wait = min(2 ** attempt * 15, 120)
            print(f"    OpenAI timeout (attempt {attempt+1}/{max_retries}), waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code == 200:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if content is None:
                content = ""
            return content
        if resp.status_code == 429:
            # Rate limit - check if it's "request too large" (not retryable)
            err_text = resp.text[:500]
            if "too large" in err_text.lower():
                raise RuntimeError(f"OpenAI API error {resp.status_code}: {err_text[:300]}")
            # TPM rate limit - retry with backoff
            wait = min(2 ** attempt * 30, 300)  # 30s, 60s, 120s, 240s, 300s
            print(f"    Rate limited (attempt {attempt+1}/{max_retries}), waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code in (500, 502, 503):
            wait = min(2 ** attempt * 15, 120)
            print(f"    OpenAI server error {resp.status_code} (attempt {attempt+1}/{max_retries}), waiting {wait}s...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"OpenAI API error {resp.status_code}: {resp.text[:300]}")
    raise RuntimeError(f"OpenAI API failed after {max_retries} retries")


def call_gemini(system_prompt, user_prompt, model="gemini-2.5-pro"):
    """Call Gemini API via REST. Rate limit: 5 RPM free tier → 15s between calls."""
    import requests
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 65536,
            "temperature": 0,
        },
    }
    max_retries = 6
    for attempt in range(max_retries):
        resp = requests.post(url, json=body, timeout=600)
        if resp.status_code == 200:
            data = resp.json()
            cand = data.get("candidates", [{}])[0]
            finish = cand.get("finishReason", "")
            parts = cand.get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
            if not text and finish == "MAX_TOKENS":
                raise RuntimeError(f"Gemini output truncated (MAX_TOKENS), no text returned")
            usage = data.get("usageMetadata", {})
            think_tok = usage.get("thoughtsTokenCount", 0)
            out_tok = usage.get("candidatesTokenCount", 0)
            print(f"    Gemini usage: prompt={usage.get('promptTokenCount',0)} think={think_tok} out={out_tok} finish={finish}")
            return text
        if resp.status_code == 429:
            err_msg = resp.json().get("error", {}).get("message", "")
            wait = min(2 ** attempt * 15, 300)  # 15s, 30s, 60s, 120s, 240s, 300s
            print(f"    Gemini rate limit (attempt {attempt+1}/{max_retries}), waiting {wait}s... ({err_msg[:100]})")
            time.sleep(wait)
            continue
        raise RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")
    raise RuntimeError(f"Gemini rate limit after {max_retries} retries")


def call_openrouter(system_prompt, user_prompt, model):
    """Call OpenRouter API (for DeepSeek etc). Compatible with OpenAI chat format."""
    import requests
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    max_retries = 8
    for attempt in range(max_retries):
        try:
            resp = requests.post("https://openrouter.ai/api/v1/chat/completions",
                                 headers=headers, json=body, timeout=600)
        except requests.exceptions.Timeout:
            wait = min(2 ** attempt * 15, 120)
            print(f"    OpenRouter timeout (attempt {attempt+1}/{max_retries}), waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code == 200:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if content is None:
                content = ""
            usage = data.get("usage", {})
            reasoning = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
            print(f"    OpenRouter usage: prompt={usage.get('prompt_tokens',0)} completion={usage.get('completion_tokens',0)} reasoning={reasoning}")
            return content
        if resp.status_code == 429:
            err_text = resp.text[:500]
            wait = min(2 ** attempt * 30, 300)
            print(f"    OpenRouter rate limit (attempt {attempt+1}/{max_retries}), waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code in (500, 502, 503):
            wait = min(2 ** attempt * 15, 120)
            print(f"    OpenRouter server error {resp.status_code} (attempt {attempt+1}/{max_retries}), waiting {wait}s...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"OpenRouter API error {resp.status_code}: {resp.text[:300]}")
    raise RuntimeError(f"OpenRouter API failed after {max_retries} retries")


def call_llm(system_prompt, user_prompt, model):
    """Dispatch to correct API based on model name."""
    if model.startswith("gpt-") or model.startswith("o1") or model.startswith("o3") or model.startswith("o4"):
        return call_openai(system_prompt, user_prompt, model)
    elif model.startswith("gemini"):
        return call_gemini(system_prompt, user_prompt, model)
    elif model.startswith("deepseek/"):
        return call_openrouter(system_prompt, user_prompt, model)
    else:
        return call_claude(system_prompt, user_prompt, model)


def parse_file_blocks(response):
    """Parse <<<FILE: path>>>...<<<END>>> blocks."""
    # Try strict pattern first
    pattern = r"<<<FILE:\s*([^\n>]+?)>>>[ \t]*\n(.*?)<<<END>>>"
    blocks = {}
    for m in re.finditer(pattern, response, re.DOTALL):
        fp = m.group(1).strip()
        # Strip diff-style a/ or b/ prefix that LLMs copy from patch headers
        if fp.startswith("a/") or fp.startswith("b/"):
            fp = fp[2:]
        content = m.group(2)
        content = re.sub(r"^```\w*\n", "", content)
        content = re.sub(r"\n```\s*$", "", content)
        blocks[fp] = content

    # If none found, try without END marker (model may have been cut off)
    if not blocks:
        pattern2 = r"<<<FILE:\s*([^\n>]+?)>>>[ \t]*\n(.*?)(?=<<<FILE:|$)"
        for m in re.finditer(pattern2, response, re.DOTALL):
            fp = m.group(1).strip()
            if fp.startswith("a/") or fp.startswith("b/"):
                fp = fp[2:]
            content = m.group(2)
            content = re.sub(r"^```\w*\n", "", content)
            content = re.sub(r"\n```\s*$", "", content)
            # Strip trailing <<<END>>> if present
            content = re.sub(r"\n?<<<END>>>\s*$", "", content)
            blocks[fp] = content

    return blocks


# ─── File reading helpers ─────────────────────────────────────────────────────

def get_modified_files(patch):
    """Extract modified file paths from patch headers."""
    files = []
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:])
        elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            files.append(line[4:])
    return files


def read_file_at(wt_path, filepath):
    """Read file from worktree, truncate if too large."""
    full = wt_path / filepath
    if not full.is_file():
        return f"# [File not found: {filepath}]\n# This is a NEW file that needs to be created."
    content = full.read_text(errors="replace")
    if len(content) > MAX_FILE_CHARS:
        content = content[:MAX_FILE_CHARS] + f"\n# ... [truncated, total {len(content)} chars]"
    return content


def get_test_code_from_patch(test_patch):
    """Extract test code (+ lines) from test_patch."""
    lines = []
    for line in test_patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
    return "\n".join(lines) if lines else ""


# ─── Test runners ─────────────────────────────────────────────────────────────

def run_test_pytest(wt_path, python_cmd, f2p_tests, test_patch, env=None):
    """Run pytest-style tests. Returns (passed_count, total_count, details)."""
    # Extract test file paths from test_patch
    test_files = [l[6:] for l in test_patch.splitlines() if l.startswith("+++ b/")]
    if not test_files:
        return 0, len(f2p_tests), "no test files found"

    if env is None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(wt_path)
        env["OMP_NUM_THREADS"] = "1"

    # Try running each F2P test
    passed = 0
    details = []
    for test_id in f2p_tests:
        parts = test_id.split("::")

        # Determine pytest_id: need to find the actual test file
        if len(parts) >= 2:
            first = parts[0]
            rest = parts[1:]  # class::method or just method

            # Check if first part already looks like a file path (has / or .py)
            if "/" in first or first.endswith(".py"):
                test_file = first
                if not test_file.endswith(".py"):
                    test_file = first.replace(".", "/") + ".py"
                pytest_id = "::".join([test_file] + rest)
            else:
                # first is a class name like "TestContext" — find matching test file
                # from the test_patch files
                matched_file = None
                for tf in test_files:
                    if tf.endswith(".py"):
                        matched_file = tf
                        break
                if matched_file:
                    # Use test_file::ClassName::method
                    pytest_id = "::".join([matched_file] + parts)
                else:
                    pytest_id = test_id
        else:
            # Single part — might be a file, use as-is
            pytest_id = test_id

        try:
            cmd = [python_cmd or sys.executable, "-m", "pytest", "-xvs", pytest_id]
            p = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(wt_path), env=env
            )
            out, err = p.communicate(timeout=TEST_TIMEOUT)
            output = out.decode(errors="replace") + err.decode(errors="replace")
            if p.returncode == 0:
                passed += 1
                details.append(f"  PASS: {test_id}")
            else:
                # Check for specific pass patterns
                if "1 passed" in output:
                    passed += 1
                    details.append(f"  PASS: {test_id}")
                else:
                    err_line = ""
                    for line in output.splitlines()[-10:]:
                        if "FAILED" in line or "ERROR" in line or "Error" in line:
                            err_line = line[:100]
                            break
                    details.append(f"  FAIL: {test_id} ({err_line})")
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except Exception:
                pass
            details.append(f"  TIMEOUT: {test_id}")
        except Exception as e:
            details.append(f"  ERROR: {test_id} ({str(e)[:80]})")

    return passed, len(f2p_tests), "\n".join(details)


def run_test_mpc_emulate(wt_path, f2p_tests, test_patch):
    """Run MP-SPDZ compile+emulate tests."""
    passed = 0
    details = []

    # Extract test .mpc files from test_patch
    test_files = [l[6:] for l in test_patch.splitlines() if l.startswith("+++ b/") and l.endswith(".mpc")]

    python_cmd = "python3"
    # Try loading python 3.11 module
    env = os.environ.copy()
    env["PATH"] = "/opt/ohpc/pub/apps/python/python-3.11.4-gcc-12.2.0/bin:" + env.get("PATH", "")

    for test_id in f2p_tests:
        test_name = test_id.split("::")[0]
        try:
            # Compile
            compile_cmd = [python_cmd, str(wt_path / "compile.py"), test_name]
            p = subprocess.Popen(
                compile_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(wt_path), env=env
            )
            out, err = p.communicate(timeout=60)
            compile_out = out.decode(errors="replace") + err.decode(errors="replace")

            if p.returncode != 0 or "Error" in compile_out or "Traceback" in compile_out:
                if "compile" in test_id:
                    details.append(f"  FAIL(compile): {test_id} - {compile_out[-100:]}")
                    continue

            # Emulate (if test requires it)
            if "emulate" in test_id or "compile" not in test_id:
                emu_cmd = [str(wt_path / "Scripts" / "emulate.sh"), test_name]
                p2 = subprocess.Popen(
                    emu_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=str(wt_path), env=env
                )
                out2, err2 = p2.communicate(timeout=60)
                emu_out = out2.decode(errors="replace") + err2.decode(errors="replace")

                if "Crash requested" in emu_out or "Fatal error" in emu_out:
                    details.append(f"  FAIL(emulate): {test_id}")
                    continue

            passed += 1
            details.append(f"  PASS: {test_id}")
        except subprocess.TimeoutExpired:
            details.append(f"  TIMEOUT: {test_id}")
        except Exception as e:
            details.append(f"  ERROR: {test_id} ({str(e)[:80]})")

    return passed, len(f2p_tests), "\n".join(details)


def run_tests(lib_key, wt_path, f2p_tests, test_patch, instance=None):
    """Dispatch to appropriate test runner with proper env setup."""
    python_cmd, env = setup_env(lib_key, wt_path, instance)

    if lib_key == "spdz":
        return run_test_mpc_emulate(wt_path, f2p_tests, test_patch)
    else:
        return run_test_pytest(wt_path, python_cmd, f2p_tests, test_patch, env=env)


# ─── Main evaluation ─────────────────────────────────────────────────────────

def eval_instance(instance, lib_key, model):
    """Evaluate a single instance end-to-end."""
    cfg = LIBS[lib_key]
    repo = cfg["repo"]
    iid = instance["instance_id"]
    base = instance["base_commit"]
    patch = instance["patch"]
    test_patch = instance["test_patch"]
    f2p_raw = instance["FAIL_TO_PASS"]

    # Parse FAIL_TO_PASS
    if isinstance(f2p_raw, str):
        try:
            f2p = eval(f2p_raw)
        except Exception:
            f2p = [f2p_raw]
    else:
        f2p = f2p_raw

    if not f2p:
        return {
            "instance_id": iid, "lib": lib_key, "model": model,
            "status": "no_f2p_tests", "f2p_total": 0, "f2p_passed": 0,
            "error": "no FAIL_TO_PASS tests defined",
        }

    result = {
        "instance_id": iid,
        "lib": lib_key,
        "model": model,
        "status": "unknown",
        "f2p_total": len(f2p),
        "f2p_passed": 0,
        "error": None,
    }

    wt_path = None
    try:
        # 1. Setup worktree
        print(f"  [{iid}] Setting up worktree at {base[:12]}...")
        wt_path = setup_worktree(repo, base)

        # 2. Apply test_patch
        ok, msg = apply_patch_file(wt_path, test_patch)
        if not ok:
            result["status"] = "test_patch_failed"
            result["error"] = msg
            return result

        # 3. Read files that need fixing
        mod_files = get_modified_files(patch)
        # Filter to only code files (not docs/examples/binary)
        mod_files = [f for f in mod_files if not f.endswith((".rst", ".md", ".png", ".npy", ".tgz"))
                     and not f.startswith(("docs/", "doc/"))]

        if not mod_files:
            result["status"] = "no_files_to_fix"
            result["error"] = "no modifiable files in patch"
            return result

        files_content = {}
        for fp in mod_files:
            files_content[fp] = read_file_at(wt_path, fp)

        # 4. Get test code
        test_code = get_test_code_from_patch(test_patch)

        # 5. Call Claude API
        print(f"  [{iid}] Calling Claude ({model})...")
        sys_prompt = build_system_prompt(lib_key, cfg["framework"])
        user_prompt = build_user_prompt(instance, files_content, test_code)

        t0 = time.time()
        raw_response = call_llm(sys_prompt, user_prompt, model)
        api_time = time.time() - t0
        result["api_time"] = round(api_time, 1)
        result["response_len"] = len(raw_response)

        # 6. Parse and apply Claude's response
        file_blocks = parse_file_blocks(raw_response)
        if not file_blocks:
            result["status"] = "no_file_blocks"
            result["error"] = f"Claude response had no <<<FILE:>>><<<END>>> blocks. First 200 chars: {raw_response[:200]}"
            result["raw_response"] = raw_response[:2000]
            return result

        result["files_written"] = list(file_blocks.keys())

        # Save LLM-generated patches for reproducibility
        safe_model = model.replace("/", "-")
        patch_dir = OUTPUT_DIR / "patches" / safe_model / iid.replace("/", "__")
        patch_dir.mkdir(parents=True, exist_ok=True)
        for fp, content in file_blocks.items():
            save_path = patch_dir / fp
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(content, encoding="utf-8")
        # Also save raw response
        (patch_dir / "_raw_response.txt").write_text(raw_response, encoding="utf-8")

        for fp, content in file_blocks.items():
            full_path = wt_path / fp
            if full_path.is_dir():
                continue  # skip directory paths (e.g. git submodules)
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")

        # 7. Run tests
        print(f"  [{iid}] Running tests...")
        passed, total, details = run_tests(lib_key, wt_path, f2p, test_patch, instance)

        result["f2p_passed"] = passed
        result["test_details"] = details

        if passed == total:
            result["status"] = "resolved"
        elif passed > 0:
            result["status"] = "partial"
        else:
            result["status"] = "failed"

    except Exception as e:
        result["status"] = "exception"
        result["error"] = str(e)[:300]
    finally:
        if wt_path:
            cleanup_worktree(repo, wt_path)

    return result


def is_gpu_instance(instance):
    """Check if instance requires GPU/CUDA."""
    text = (instance.get("patch", "") + instance.get("test_patch", "") +
            str(instance.get("FAIL_TO_PASS", ""))).lower()
    return "cuda" in text or "gpu" in text


def sample_instances(per_lib, seed=42, lib_filter=None):
    """Sample N instances per library, excluding GPU-dependent ones."""
    random.seed(seed)
    sampled = []

    for lib_key, cfg in LIBS.items():
        if lib_filter and lib_key not in lib_filter:
            continue
        filepath = DATASET_DIR / cfg["file"]
        instances = []
        with open(filepath) as f:
            for line in f:
                inst = json.loads(line)
                # Exclude GPU instances
                if is_gpu_instance(inst):
                    continue
                instances.append(inst)

        # Sample
        n = min(per_lib, len(instances))
        selected = random.sample(instances, n)
        for inst in selected:
            sampled.append((lib_key, inst))

    return sampled


def main():
    parser = argparse.ArgumentParser(description="Multi-lib SWE-bench evaluation")
    parser.add_argument("--per-lib", type=int, default=3, help="instances per library")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default=None)
    parser.add_argument("--libs", default=None, help="comma-separated libs to eval (e.g. crypten,tfe)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = Path(args.output) if args.output else (
        OUTPUT_DIR / f"eval_pilot_{args.model.replace('/', '_')}_{args.per_lib}per.jsonl"
    )

    lib_filter = set(args.libs.split(",")) if args.libs else None
    sampled = sample_instances(args.per_lib, args.seed, lib_filter=lib_filter)

    print(f"Model:    {args.model}")
    print(f"Per-lib:  {args.per_lib}")
    print(f"Total:    {len(sampled)} instances")
    print(f"Output:   {output_file}")
    print("=" * 65)

    for lib_key, inst in sampled:
        print(f"  {lib_key}: {inst['instance_id']}")
    print("=" * 65)

    results = []
    t0 = time.time()

    for i, (lib_key, inst) in enumerate(sampled):
        iid = inst["instance_id"]
        print(f"\n[{i+1}/{len(sampled)}] {iid}")

        r = eval_instance(inst, lib_key, args.model)
        results.append(r)

        # Status display
        marker = {"resolved": "PASS", "partial": "PART", "failed": "FAIL"}.get(r["status"], r["status"])
        print(f"  => {marker}  F2P={r['f2p_passed']}/{r['f2p_total']}  "
              f"time={r.get('api_time', 0):.0f}s  status={r['status']}")
        if r.get("test_details"):
            for line in r["test_details"].split("\n")[:3]:
                print(f"     {line}")
        if r.get("error"):
            print(f"     error: {r['error'][:100]}")

        # Save incrementally
        with open(output_file, "w") as f:
            for res in results:
                f.write(json.dumps(res, ensure_ascii=False) + "\n")

    # Summary
    elapsed = time.time() - t0
    print("\n" + "=" * 65)
    print(f"SUMMARY  (elapsed: {elapsed:.0f}s)")
    print("=" * 65)

    resolved = sum(1 for r in results if r["status"] == "resolved")
    partial = sum(1 for r in results if r["status"] == "partial")
    failed = sum(1 for r in results if r["status"] == "failed")
    other = len(results) - resolved - partial - failed
    f2p_total = sum(r["f2p_total"] for r in results)
    f2p_passed = sum(r["f2p_passed"] for r in results)

    print(f"Resolved: {resolved}/{len(results)}")
    print(f"Partial:  {partial}/{len(results)}")
    print(f"Failed:   {failed}/{len(results)}")
    if other:
        print(f"Other:    {other}/{len(results)}")
    print(f"F2P pass: {f2p_passed}/{f2p_total} ({100*f2p_passed/f2p_total:.1f}% if f2p_total else 0)")

    # Per-library breakdown
    print("\nPer-library:")
    by_lib = {}
    for r in results:
        lib = r["lib"]
        if lib not in by_lib:
            by_lib[lib] = {"total": 0, "resolved": 0}
        by_lib[lib]["total"] += 1
        if r["status"] == "resolved":
            by_lib[lib]["resolved"] += 1
    for lib, stats in by_lib.items():
        print(f"  {lib}: {stats['resolved']}/{stats['total']} resolved")

    print(f"\nSaved -> {output_file}")


if __name__ == "__main__":
    main()
