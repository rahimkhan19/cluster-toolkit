# Automated Driver & Infrastructure Updater
**Bug Tracking:** [b/562019559](https://b.corp.google.com/issues/562019559)  
**Location:** `tools/infra_updater/`

An automated driver and infrastructure update management system for Cluster Toolkit, combining **sub-millisecond deterministic checks** with **Gemini LLM semantic triage**.

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
            | Upfront Learned Rule Check| Gemini LLM Candidate      |
            | (< 1ms SemVer gatekeeper) | Extraction & GA Filtering |
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
   - **Gemini LLM Candidate Extraction & GA Stability Filter**: Discovers upstream releases and evaluates tags/channel metadata to confirm production GA status.
   - **Sub-Millisecond Upfront Rule Checker**: Evaluates policy `learned_rules` in `< 1ms` (~0.2ms) before making expensive network calls.
   - **HTTP HEAD Liveness Check**: Validates that candidate artifacts exist and return HTTP 200.
2. **`code_modifier.py`**:
   - Surgical, comment-preserving YAML modifier.
   - Coupled variable synchronization (e.g. `cuda_installer_url` + `cuda_installer_file`).
   - Signature keyword filtering to protect generic variable names (`package_url`).
   - Local YAML AST validation (`yaml.safe_load`).
3. **`init_db.py`**:
   - Initializes canonical JSON state store (`updater_state.json`) with seeded package registry, blueprint instances, and learned rules.
4. **`datastore.py`**:
   - Thread-safe, atomic transactional JSON datastore managing live package states, candidates, learned rules, and audit logs.
4. **`run_updater.py`** (alias: `main.py`):
   - Master CLI runner with rich colored terminal output and comprehensive qualification flags.
5. **`server.py`**:
   - Lightweight web server hosting an interactive dashboard (on port 8080 by default) for live qualification, atomic application, diff review, and log streaming.

---

## 3. Quickstart CLI Commands

From the repository root (`/usr/local/google/home/rahimkh/Desktop/Projects/cluster-toolkit`):

### A. Run Source Qualification
```bash
python3 tools/infra_updater/run_updater.py --check-all
```
*Queries upstream release archives, runs LLM candidate extraction and GA stability filtering, checks learned rules (<1ms), verifies HTTP 200, and records qualified candidates in updater_state.json.*

### B. Launch Web UI Dashboard
```bash
python3 tools/infra_updater/server.py --port 8080
```
*Opens an interactive visual dashboard at `http://localhost:8080` showing package registries, blueprint instances, learned rules, qualified candidates, and real-time execution logs.*

### C. Demonstrate < 1ms Upfront Rule Blocking
```bash
python3 tools/infra_updater/run_updater.py --test-rule-blocking
```
*Demonstrates sub-millisecond rule evaluation blocking known faulty versions (e.g. CUDA 13.1.0 or GVE 1.5.0) in ~0.2ms before any network or file operations.*

### D. Apply Atomic Blueprint Updates & View Git Diff
```bash
# Update NVIDIA CUDA Toolkit (x86_64) across a3ultra and a4high blueprints:
python3 tools/infra_updater/run_updater.py --apply nvidia-cuda-x86

# Update Google Virtual Ethernet driver across image builder and a3mega blueprints:
python3 tools/infra_updater/run_updater.py --apply gve-dkms
```

### E. Preview State Entities or Reset
```bash
# Preview all DataStore entities:
python3 tools/infra_updater/run_updater.py --show-tables

# Reset git files and state store:
python3 tools/infra_updater/run_updater.py --reset
```
