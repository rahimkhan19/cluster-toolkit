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

name: infra-update-cartographer
description: >
  Discover updateable cloud infrastructure components, hardcoded versions, and
  accompanying download URLs across Cluster Toolkit blueprints, Terraform modules,
  Packer templates, and shell scripts. Use when scanning the repository for dependency updates.
compatibility: "Python 3.10+ and git"
metadata:
  author: GoogleCloudPlatform
  status: experimental
  domain: infrastructure-automation
allowed-tools: Bash(git:*) Bash(grep:*)
allowed_read_only_commands:
  - "git diff"
  - "grep -rn"
---

# Infrastructure Update Cartographer Skill

> [!WARNING]
> This skill is **experimental** and under active validation for Cluster Toolkit. Diagnostic queries are read-only, but remediation plans must be reviewed carefully by cluster operators before execution.

Use this skill to scan Cluster Toolkit repository files (blueprints, Terraform modules, build scripts, and Packer manifests) to discover updateable infrastructure components and extract their concrete download paths, package repository URLs, or image families.

---

## 1. Incremental Discovery Workflow

Instead of sweeping the entire repository on every execution, the Cartographer operates in an incremental registry model:

1. **Check Git Diff**: Inspect changes between the last evaluated commit and HEAD:
   ```bash
   git diff <last_scanned_commit>..HEAD --name-only
   ```
2. **Filter Target File Types**: Only analyze:
   - YAML Blueprints (`examples/**/*.yaml`, `examples/**/*.yml`)
   - Terraform Configuration (`modules/**/*.tf`, `community/**/*.tf`)
   - Shell Installation Scripts (`*.sh`, `scripts/**/*.sh`)
   - Packer Image Manifests (`*.pkr.hcl`)

3. **Identify Component Patterns**:
   Look for hardcoded cloud infrastructure declarations:
   - **NVIDIA CUDA / GPU Drivers**: URLs matching `https://developer.download.nvidia.com/compute/cuda/<version>/local_installers/...` or `cuda-keyring` packages.
   - **Mellanox OFED / DOCA**: URLs matching `https://linux.mellanox.com/public/repo/doca/<version>/...`.
   - **GCE Image Families**: Configuration lines containing `family: <image-family-name>` and `project: <image-project>`.
   - **Helm Charts**: Declarations referencing OCI registries or Helm repositories.

---

## 2. Extraction & Inventory Output

For each detected component, extract the complete metadata payload:

```json
{
  "component": "<component_name>",
  "current_version": "<version_string>",
  "upstream_url": "<concrete_download_or_repo_url>",
  "file_path": "<relative_path_to_file>",
  "variable_name": "<name_of_variable_or_setting>",
  "context_lines": "<surrounding_block>"
}
```

Never invent or hallucinate download URLs. Only extract URLs explicitly declared within the codebase.
