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

"""
LLM prompt templates for the Cluster Toolkit Automated Infrastructure Updater.

  select_release            one generic prompt: pick the newest GA release from fetched candidates
  extract_manifest_version  exception: read a single version out of a Kubernetes manifest
  release_summary           exception: plain-text release-notes summary
"""

import os
from functools import lru_cache

PROMPTS_DIR = os.path.dirname(os.path.abspath(__file__))


@lru_cache(maxsize=None)
def _load(name: str) -> str:
    with open(os.path.join(PROMPTS_DIR, f"{name}.txt"), "r", encoding="utf-8") as f:
        return f.read().strip()


def render_prompt(name: str, **kwargs) -> str:
    """Renders prompts/<name>.txt. `extra_rules` (optional) is appended as additional numbered guidance."""
    kwargs["extra_rules"] = kwargs.get("extra_rules") or ""
    return _load(name).format(**kwargs).strip()
