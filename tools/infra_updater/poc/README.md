# Automated Driver & Infrastructure Updater - Proof of Concept (POC)
**Bug Tracking:** [b/562019559](https://b.corp.google.com/issues/562019559)  
**Location:** `tools/infra_updater/poc/`

This directory contains the runnable Proof of Concept (POC) for automated driver and infrastructure updates in Cluster Toolkit, combining **sub-millisecond deterministic checks** with **Gemini LLM semantic triage**.

---

## 1. Architecture: Hybrid Deterministic + LLM Pipeline

```
                +------------------------------------------------+
                |          Canonical Package Registry            |
                |               (packages table)                 |
                +-----------------------+------------------------+
                                        |
                        [Source Qualification Agent]
                                        |
            +---------------------------+---------------------------+
            | Upfront Learned Rule Check| Gemini LLM GA Stability   |
            | (< 1ms SemVer gatekeeper) | Channel Reasoning         |
            +---------------------------+---------------------------+
                                        |
            +---------------------------+---------------------------+
            | HTTP HEAD Liveness Check  | Gemini LLM Semantic       |
            | (Verifies HTTP 200 OK)    | Changelog & Deprecation   |
            +---------------------------+---------------------------+
                                        |
                                        v
                        +-------------------------------+
                        |   Qualified Candidate Queue   |
                        |   (candidate_updates table)   |
                        +---------------+---------------+
                                        |
                                        v
                          [Atomic Code Modifier]
                                        |
            +---------------------------+---------------------------+
            | Comment-Preserving AST    | Synchronized Coupled Vars |
            | Surgical YAML Replacement | (e.g. installer URL+file) |
            +---------------------------+---------------------------+
                                        |
                                        v
                    [Local YAML Validation & git diff]
```

---

## 2. Key Modules

1. **`source_agent.py`**:
   - **Gemini LLM GA Stability Filter**: Evaluates release tags and channel metadata to confirm production GA status.
   - **Gemini LLM Changelog Analysis**: Reads raw upstream markdown release notes to detect breaking syntax, dropped kernel versions (Linux 6.1/6.6 LTS), or deprecated CLI flags in the context of Debian 12 / Rocky 9.
   - **Sub-Millisecond Upfront Rule Checker**: Evaluates SQLite `learned_rules` in `< 1ms` (~0.2ms) before making expensive network calls.
   - **HTTP HEAD Liveness Check**: Validates that artifacts return HTTP 200.
2. **`code_modifier.py`**:
   - Surgical, comment-preserving YAML modifier.
   - Coupled variable synchronization (e.g. `cuda_installer_url` + `cuda_installer_file`).
   - Signature keyword filtering to protect generic variable names (`package_url`).
   - Local YAML AST validation (`yaml.safe_load`).
3. **`init_db.py`**:
   - Initializes SQLite database (`poc_state.db`) with 4 normalized tables.
4. **`run_poc.py`**:
   - Master CLI runner with rich colored terminal outputs.

---

## 3. Quickstart CLI Commands

From the repository root (`/usr/local/google/home/rahimkh/Desktop/Projects/cluster-toolkit`):

### A. Run Source Qualification (with Gemini LLM Analysis)
```bash
python3 tools/infra_updater/poc/run_poc.py --check-all
```
*Queries upstream release archives, runs the LLM GA filter, checks learned rules (<1ms), analyzes changelogs with Gemini, verifies HTTP 200, and records qualified candidates in SQLite.*

### B. Demonstrate Gemini LLM Changelog & Deprecation Triage
```bash
python3 tools/infra_updater/poc/run_poc.py --test-llm-triage
```
*Demonstrates Gemini reading upstream release notes to catch breaking changes (e.g., dropped kernel support, deprecated CLI flags, removed symlinks) vs compatible bugfix releases.*

### C. Demonstrate < 1ms Upfront Rule Blocking
```bash
python3 tools/infra_updater/poc/run_poc.py --test-rule-blocking
```
*Demonstrates sub-millisecond SQLite rule evaluation blocking known faulty versions (e.g. CUDA 13.1.0 or GVE 1.5.0) in ~0.2ms before any network or file operations.*

### D. Apply Atomic Blueprint Updates & View Git Diff
```bash
# Update NVIDIA CUDA Toolkit (x86_64) across a3ultra and a4high blueprints:
python3 tools/infra_updater/poc/run_poc.py --apply nvidia-cuda-x86

# Update Google Virtual Ethernet driver across image builder and a3mega blueprints:
python3 tools/infra_updater/poc/run_poc.py --apply gve-dkms
```

### E. Preview State Tables or Reset
```bash
# Preview all 4 SQLite state tables:
python3 tools/infra_updater/poc/run_poc.py --show-tables

# Reset git files and database state:
python3 tools/infra_updater/poc/run_poc.py --reset
```
