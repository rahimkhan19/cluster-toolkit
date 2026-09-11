---
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

name: infra-update-code-changer
description: >
  Apply atomic code changes for qualified infrastructure updates in Cluster Toolkit.
  Identify co-dependent variables across blueprints and modules, perform atomic replacements,
  and validate YAML/HCL syntax before finalizing changes. Use when modifying files for dependency updates.
compatibility: "Python 3.10+ and git"
metadata:
  author: GoogleCloudPlatform
  status: experimental
  domain: infrastructure-automation
allowed-tools: Bash(git:*)
allowed_read_only_commands:
  - "git diff"
  - "git status"
---

# Code Changer Agent Skill

> [!WARNING]
> This skill is **experimental** and under active validation for Cluster Toolkit. Diagnostic queries are read-only, but remediation plans must be reviewed carefully by cluster operators before execution.

Use this skill to evaluate cross-layer compatibility, detect coupled sibling variables, and atomically apply file modifications for infrastructure updates.

---

## 1. Co-Dependency & Atomic Update Rules

When applying an update to a Cluster Toolkit blueprint or module, never update an isolated URL without checking for dependent variables:

1. **Coupled File Paths & URLs**:
   - In Ansible blocks, check if `cuda_installer_url` is paired with a local destination path (e.g. `cuda_installer_file`). Both must be updated simultaneously to prevent runtime execution failures.
2. **Co-Dependent Package Suites**:
   - If updating an NVIDIA driver package, ensure coupled packages (`nvidia-dkms`, `libnvidia-nscq`) are bumped to matching versions.
3. **Syntax Validation**:
   - Always run syntax validation (e.g., YAML parser) before recording the change as successful.
4. **Git Isolation**:
   - Isolate modifications on a dedicated branch (e.g. `deps/<component>-update`).
   - Run `git diff` to verify only the targeted variables were touched.
