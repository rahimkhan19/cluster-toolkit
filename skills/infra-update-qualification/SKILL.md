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

name: infra-update-qualification
description: >
  Qualify upstream releases for discovered cloud infrastructure components.
  Query provider endpoints, enforce lifecycle stability rules (filter out RCs, betas, nightlies),
  and format candidate update payloads. Use when evaluating upstream updates for Cluster Toolkit.
compatibility: "Python 3.10+ and network access to public provider indices"
metadata:
  author: GoogleCloudPlatform
  status: experimental
  domain: infrastructure-automation
allowed-tools: Bash(curl:*)
allowed_read_only_commands:
  - "curl -I"
  - "curl -s"
---

# Source Qualification Skill

> [!WARNING]
> This skill is **experimental** and under active validation for Cluster Toolkit. Diagnostic queries are read-only, but remediation plans must be reviewed carefully by cluster operators before execution.

Use this skill to query upstream software and driver providers, verify release channels, and qualify candidate versions for Cluster Toolkit.

---

## 1. Upstream Verification Rules

1. **Target the Concrete Provider Source**:
   - Query the exact domain extracted by the Cartographer (e.g. `developer.download.nvidia.com`, `linux.mellanox.com`, `github.com`).
2. **Strict Stability Filtering**:
   - **Reject Unstable Tracks**: Never propose release candidates (`-rc*`), betas, alphas, developer previews, or nightly builds.
   - **General Availability (GA)**: Only qualify stable, production-ready releases.
3. **Verify Download Availability**:
   - Execute an HTTP HEAD check (`curl -I <url>`) to ensure the artifact is published and returns HTTP 200 before recommending it.

---

## 2. Candidate Update Payload

Format the qualified update as follows:

```json
{
  "component": "<component_name>",
  "current_version": "<current_version>",
  "target_version": "<new_stable_version>",
  "download_url": "<verified_direct_download_url>",
  "release_channel": "production-stable",
  "update_available": true,
  "changelog_url": "<url_to_official_release_notes>"
}
```
