
## Step-by-Step Reproduction (30 minutes to first result)

### Prerequisites

- Linux system (tested on RHEL 8)
- Podman or Docker
- Python 3.10+
- Git
- 50GB disk space
- 8GB RAM minimum

### Step 1: Get the Repository (2 minutes)

```bash
# If you have access to the anonymous repo
wget https://anonymous.4open.science/r/MPC_bench-D496.zip
unzip MPC_bench-D496.zip
cd MPC_bench

### Step 2: Clone Framework Repositories (5 minutes)

The evaluation harness expects framework repositories in an `external/` directory:

```bash
# Create external directory
mkdir -p external
cd external

# Clone all 5 MPC frameworks
git clone https://github.com/facebookresearch/CrypTen.git
git clone https://github.com/data61/MP-SPDZ.git
git clone https://github.com/tf-encrypted/tf-encrypted.git
git clone https://github.com/OpenMined/PySyft.git
git clone https://github.com/secretflow/secretflow.git

cd ..
```

**Expected result**:
```
external/
├── CrypTen/
├── MP-SPDZ/
├── PySyft/
├── secretflow/
└── tf-encrypted/
```

### Step 3: Build Docker Images (10-15 minutes)

```bash
cd docker

# Set up Podman runtime directory
export XDG_RUNTIME_DIR=/tmp/podman_$UID
mkdir -p $XDG_RUNTIME_DIR

# Build all 5 framework images
bash build_images.sh

# Verify all images built
podman images | grep mpcbench
```

**Expected output**:
```
mpcbench-crypten       latest    ...    1.27GB
mpcbench-spdz          latest    ...    226MB
mpcbench-tfe           latest    ...    714MB
mpcbench-pysyft        latest    ...    1.5GB
mpcbench-secretflow    latest    ...    2.1GB
```

**Note**: If `mpcbench-pysyft` or `mpcbench-secretflow` fail with pip index errors, see Troubleshooting section below.

### Step 4: Set API Keys

```bash
# Set API key for your chosen LLM
export ANTHROPIC_API_KEY="sk-ant-..."  # For Claude
# OR
export OPENAI_API_KEY="sk-..."         # For GPT
# OR
export GEMINI_API_KEY="AIza..."        # For Gemini
```


### Step 5: Run One Task (5-10 minutes)

Run evaluation on a single CrypTen instance:

```bash
python eval/run_eval.py \
    --library crypten \
    --model claude-sonnet-4-6 \
    --dataset data/mpc_bench.jsonl \
    --output results/test_single.jsonl \
    --limit 1

# Check result
python3 << 'EOF'
import json
with open("results/test_single.jsonl") as f:
    for line in f:
        result = json.loads(line)
        print(f"Instance: {result['instance_id']}")
        print(f"Status: {result.get('resolved', 'N/A')}")
        print(f"F2P: {len(result.get('f2p', {}).get('passed', []))}/{len(result.get('FAIL_TO_PASS', []))}")
EOF
```

**Expected output**:
```
Instance: crypten__facebookresearch__CrypTen-326
Status: True/False (depending on LLM success)
F2P: X/2
```

---

## Full Benchmark Reproduction

### Run All 205 Instances

```bash
# Run all frameworks with one model
for lib in crypten spdz tfe pysyft secretflow; do
    python eval/run_eval.py \
        --library $lib \
        --model claude-sonnet-4-6 \
        --dataset data/mpc_bench.jsonl \
        --output results/eval_${lib}_sonnet.jsonl
done

# Results will be in:
# results/eval_crypten_sonnet.jsonl    (61 instances)
# results/eval_spdz_sonnet.jsonl       (41 instances)
# results/eval_tfe_sonnet.jsonl        (43 instances)
# results/eval_pysyft_sonnet.jsonl     (34 instances)
# results/eval_secretflow_sonnet.jsonl (26 instances)
```

### Run MPC Verifier

```bash
# Verify resolved patches
python verifier/scan_resolved.py \
    --patches results/eval_crypten_sonnet.jsonl \
    --rules verifier/rules/mpc_rules.yaml \
    --output results/verify_crypten_sonnet.jsonl
```

---

## Troubleshooting

### Issue 1: Docker Build Fails for PySyft/SecretFlow

**Error**: `ERROR: Could not find a version that satisfies the requirement pytest`

**Cause**: Using `--index-url` for PyTorch blocks access to PyPI

**Fix**:

```bash
# Fix PySyft Dockerfile
cat > docker/Dockerfile.pysyft << 'EOF'
FROM docker.io/library/python:3.10-slim

RUN pip install --no-cache-dir torch==2.1.0 --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir pytest numpy pydantic==1.10.18 pynacl packaging requests jinja2 PyYAML

WORKDIR /workspace
ENV PYTHONPATH=/workspace
ENV OMP_NUM_THREADS=1
EOF

# Fix SecretFlow Dockerfile
cat > docker/Dockerfile.secretflow << 'EOF'
FROM docker.io/library/python:3.10-slim

RUN pip install --no-cache-dir torch==2.1.0 --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir pytest numpy pandas scikit-learn jax jaxlib grpcio protobuf cloudpickle multiprocess psutil

WORKDIR /workspace
ENV PYTHONPATH=/workspace
ENV OMP_NUM_THREADS=1
EOF

# Rebuild
podman build -t mpcbench-pysyft -f docker/Dockerfile.pysyft docker/
podman build -t mpcbench-secretflow -f docker/Dockerfile.secretflow docker/
```

### Issue 2: Framework Repositories Not Found

**Error**: `MPC_BENCH_CRYPTEN_REPO environment variable not set`

**Fix**: Set environment variables pointing to framework repos:

```bash
export MPC_BENCH_EXTERNAL_DIR=$(pwd)/external
export MPC_BENCH_CRYPTEN_REPO=$MPC_BENCH_EXTERNAL_DIR/CrypTen
export MPC_BENCH_SPDZ_REPO=$MPC_BENCH_EXTERNAL_DIR/MP-SPDZ
export MPC_BENCH_TFE_REPO=$MPC_BENCH_EXTERNAL_DIR/tf-encrypted
export MPC_BENCH_PYSYFT_REPO=$MPC_BENCH_EXTERNAL_DIR/PySyft
export MPC_BENCH_SECRETFLOW_REPO=$MPC_BENCH_EXTERNAL_DIR/secretflow
```

### Issue 3: Podman Permission Errors

**Error**: `error creating temporary file: No such file or directory`

**Fix**:

```bash
# Set up Podman runtime directory
export XDG_RUNTIME_DIR=/tmp/podman_$UID
mkdir -p $XDG_RUNTIME_DIR

# Or use Docker instead
alias podman=docker
```

### Issue 4: Test Timeouts

**Error**: Tests hang or timeout after 5 minutes

**Solution**: CrypTen multi-process tests may need longer timeout. Edit `eval/eval_multi_lib.py`:

```python
TEST_TIMEOUT = 600  # Increase to 10 minutes
```

---


## Validation Checklist

Before claiming successful reproduction:

- [ ] All 5 Docker images built successfully
- [ ] All 5 framework repos cloned in `external/`
- [ ] Single task completes and produces output
- [ ] Output JSONL contains required fields: `instance_id`, `library`, `resolved`, `f2p`, `p2p`
- [ ] At least one task reaches `resolved: true` (if LLM is working)

---

## Expected Output Format

### Evaluation Result (per task)

```json
{
  "instance_id": "crypten__facebookresearch__CrypTen-326",
  "library": "crypten",
  "resolved": true,
  "f2p": {
    "passed": ["TestTFP::test_tuple_cache", "TestTTP::test_tuple_cache"],
    "failed": []
  },
  "p2p": {
    "passed": ["Test3PC::test_tuple_cache", ...],
    "failed": []
  },
  "patch": "<LLM-generated diff>",
  "duration_seconds": 87.3
}
```

### Verification Result

```json
{
  "instance_id": "crypten__facebookresearch__CrypTen-326",
  "static_check": "pass",
  "static_findings": [],
  "dynamic_check": "pass",
  "overall": "verified"
}
```
