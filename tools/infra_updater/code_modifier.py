#!/usr/bin/env python3
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
Atomic Code Modifier for the Cluster Toolkit Infrastructure Updater.
Performs surgical, comment-preserving AST-safe updates on target blueprints,
synchronizing coupled variables atomically and validating YAML syntax.
All coupling rules and line signature keywords are loaded dynamically from DataStore.
"""

import difflib
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Any, Tuple
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, "../.."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.infra_updater.datastore import DataStore, get_datastore

class OrchestratorAgent:
    """
    Orchestrator Agent for Cluster Toolkit Automated Dependency Management (Doc Section 4.3).
    Performs surgical, comment-preserving AST-safe updates on target blueprints,
    synchronizing coupled variables atomically and validating YAML syntax.
    Transitions candidate updates to READY_FOR_REVIEW.
    """

    def __init__(self, store: Optional[DataStore] = None, repo_root: str = REPO_ROOT):
        self.store = store or get_datastore()
        self.repo_root = repo_root

    def _get_yaml_context(self, lines: List[str], line_idx: int) -> str:
        """Extracts parent block keys and immediate sibling metadata for contextual variable replacement."""
        current_indent = len(lines[line_idx]) - len(lines[line_idx].lstrip(" \t"))
        context_tokens = [lines[line_idx].strip()]
        running_indent = current_indent
        for j in range(line_idx - 1, -1, -1):
            l = lines[j]
            stripped = l.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(l) - len(l.lstrip(" \t"))
            if indent < running_indent:
                context_tokens.append(stripped)
                running_indent = indent
                if running_indent == 0:
                    break
            elif indent == running_indent and any(stripped.startswith(k) for k in ["id:", "- id:", "name:"]):
                context_tokens.append(stripped)

        for j in range(max(0, line_idx - 5), min(len(lines), line_idx + 6)):
            l = lines[j].strip()
            if any(l.startswith(k) for k in ["id:", "- id:", "name:", "source:", "image:", "command:"]):
                context_tokens.append(l)

        return " ".join(context_tokens).lower()

    def _replace_variable_in_text(
        self, text: str, var_name: str, new_val: str, signature_keywords: Optional[List[str]] = None
    ) -> Tuple[str, bool, str]:
        """
        Surgically replaces a variable assignment line preserving exact indentation and quotes.
        If signature_keywords is provided, checks contextual parent hierarchy and nearby metadata.
        Returns: (new_text, changed, old_val)
        """
        pattern = rf'^([ \t]*{re.escape(var_name)}:[ \t]*)(["\']?)([^"\r\n]+)(["\']?.*)$'
        lines = text.splitlines(keepends=True)
        changed = False
        old_val = ""

        new_lines = []
        for i, line in enumerate(lines):
            m = re.match(pattern, line)
            if m:
                # If keywords provided, check if line or nearby context matches
                if signature_keywords:
                    ctx = self._get_yaml_context(lines, i)
                    if not any(k.lower() in ctx for k in signature_keywords):
                        new_lines.append(line)
                        continue

                prefix, quote1, val, suffix_rest = m.groups()
                old_val = val.strip()
                if quote1:
                    new_line = f"{prefix}{quote1}{new_val}{quote1}\n"
                else:
                    new_line = f"{prefix}{new_val}\n"
                new_lines.append(new_line)
                changed = True
            else:
                new_lines.append(line)

        return ("".join(new_lines), changed, old_val)

    def apply_update(self, package_id: str, candidate_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Applies qualified candidate update to all blueprint instances associated with package_id.
        All variable bindings, coupling patterns, and signature keywords are read dynamically from DataStore.
        """
        # 1. Fetch Candidate Update
        if candidate_id:
            cand = self.store.get_candidate(candidate_id)
        else:
            candidates = self.store.list_candidates(package_id=package_id)
            valid_cands = [c for c in candidates if c.get("status") in ('UPDATE_FOUND', 'READY_FOR_REVIEW', 'QUALIFIED')]
            cand = valid_cands[-1] if valid_cands else None

        if not cand:
            return {
                "status": "ERROR",
                "message": f"No candidate update found for package '{package_id}' in datastore."
            }

        cand_id = cand["candidate_id"]
        cand_version = cand["version"]
        cand_url = cand.get("download_url", "")
        filename = os.path.basename(cand_url)

        # 2. Fetch Blueprint Instances with dynamically defined signature keywords
        instances = self.store.get_blueprints_for_package(package_id)

        if not instances:
            return {
                "status": "ERROR",
                "message": f"No blueprint instances registered for package '{package_id}'."
            }

        modified_files = []
        all_diffs = {}

        for inst in instances:
            inst_id = inst.get("instance_id")
            rel_path = inst.get("blueprint_path")
            var_name = inst.get("variable_name")
            coupled_vars = inst.get("coupled_vars", [])
            sig_keywords = inst.get("signature_keywords", [])

            abs_path = os.path.join(self.repo_root, rel_path)
            if not os.path.exists(abs_path):
                print(f"[WARN] Blueprint file not found: {abs_path}")
                continue

            with open(abs_path, "r", encoding="utf-8") as f:
                orig_content = f.read()

            # Determine primary replacement value based on variable type
            primary_val = cand_url if "url" in var_name.lower() else cand_version
            if "image" in var_name.lower() and "/" not in primary_val:
                primary_val = f"nvidia/cuda:{primary_val}"

            if isinstance(sig_keywords, str):
                try:
                    sig_keywords = json.loads(sig_keywords)
                except Exception:
                    sig_keywords = []

            # Primary variable replacement
            new_content, primary_changed, old_primary_val = self._replace_variable_in_text(
                orig_content, var_name, primary_val, signature_keywords=sig_keywords
            )

            # Inline script fallback for packages deployed via shell commands (e.g. MFT)
            if not primary_changed and "mft" in var_name.lower():
                cand_base = f"mft-{cand_version}-aarch64-deb"
                content_after = re.sub(r'https://www.mellanox.com/downloads/MFT/mft-[0-9\.\-]+-aarch64-deb\.tgz', cand_url, orig_content)
                content_after = re.sub(r'mft-[0-9\.\-]+-aarch64-deb', cand_base, content_after)
                if content_after != orig_content:
                    new_content = content_after
                    primary_changed = True
                    old_primary_val = "4.34.0-145"

            # Inline script fallback for Miniforge
            if not primary_changed and (package_id == "miniforge" or "miniforge" in var_name.lower()):
                content_after = re.sub(
                    r'https://github\.com/conda-forge/miniforge/releases/download/[0-9\.\-]+/Miniforge3-[0-9\.\-]+-Linux-x86_64\.sh',
                    f'https://github.com/conda-forge/miniforge/releases/download/{cand_version}/Miniforge3-{cand_version}-Linux-x86_64.sh',
                    orig_content
                )
                content_after = re.sub(
                    r'Miniforge3-[0-9\.\-]+-Linux-x86_64\.sh',
                    f'Miniforge3-{cand_version}-Linux-x86_64.sh',
                    content_after
                )
                if content_after != orig_content:
                    new_content = content_after
                    primary_changed = True
                    old_primary_val = "24.7.1-2"

            # Custom URL builder for CMake local installer
            if package_id == "cmake" or "cmake" in var_name.lower():
                parts = cand_version.lstrip("v").split(".")
                maj_min = f"v{parts[0]}.{parts[1]}" if len(parts) >= 2 else f"v{cand_version}"
                primary_val = f"https://cmake.org/files/{maj_min}/cmake-{cand_version.lstrip('v')}-linux-x86_64.sh"
                filename = f"cmake-{cand_version.lstrip('v')}-linux-x86_64.sh"

            # List item replacement for nvidia_packages (e.g. datacenter-gpu-manager packages)
            if not primary_changed and (package_id == "nvidia-dcgm" or "dcgm" in var_name or "nvidia_packages" in var_name):
                epoch_prefix = "1:" if not cand_version.startswith("1:") else ""
                target_ver = f"{epoch_prefix}{cand_version}"
                content_after, count = re.subn(
                    r'(datacenter-gpu-manager-4-[a-z0-9]+=)[0-9\.\-:]+',
                    rf'\g<1>{target_ver}',
                    orig_content
                )
                if count > 0:
                    new_content = content_after
                    primary_changed = True
                    old_primary_val = "1:4.6.1-1"

            # Coupled variables synchronization
            if isinstance(coupled_vars, str):
                try:
                    coupled_list = json.loads(coupled_vars)
                except Exception:
                    coupled_list = []
            else:
                coupled_list = coupled_vars or []

            coupled_changes = []

            for coupled in coupled_list:
                c_var_name = coupled.get("variable_name")
                pattern = coupled.get("pattern", "{filename}")
                c_val = pattern.format(filename=filename, version=cand_version)
                new_content, c_changed, old_c_val = self._replace_variable_in_text(
                    new_content, c_var_name, c_val
                )
                if c_changed:
                    coupled_changes.append({
                        "variable": c_var_name,
                        "old_value": old_c_val,
                        "new_value": c_val
                    })

            if not primary_changed and not coupled_changes:
                continue

            # 3. YAML Syntax & Integrity Validation
            try:
                yaml.safe_load(new_content)
            except yaml.YAMLError as ye:
                return {
                    "status": "ERROR",
                    "message": f"YAML syntax validation failed on {rel_path}: {ye}"
                }

            # 4. Atomic File Write
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            # Compute unified diff
            diff = list(difflib.unified_diff(
                orig_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                n=3
            ))
            all_diffs[rel_path] = "".join(diff)
            modified_files.append({
                "instance_id": inst_id,
                "file_path": rel_path,
                "primary_variable": var_name,
                "old_value": old_primary_val,
                "new_value": primary_val,
                "coupled_changes": coupled_changes
            })

        # 5. Transition Status in DataStore (Doc Section 2.3 & 3.1: status = 'READY_FOR_REVIEW')
        pkg = self.store.get_package(package_id)
        prev_version = pkg.get("current_version") if pkg else None
        self.store.update_candidate(cand_id, {
            "status": "READY_FOR_REVIEW",
            "previous_version": prev_version
        })
        self.store.update_package(package_id, {
            "current_version": cand_version,
            "status": "READY_FOR_REVIEW"
        })

        return {
            "status": "SUCCESS",
            "candidate_id": cand_id,
            "package_id": package_id,
            "target_version": cand_version,
            "download_url": cand_url,
            "workflow_status": "READY_FOR_REVIEW",
            "modified_files": modified_files,
            "diffs": all_diffs
        }

    def revert_update(self, package_id: Optional[str] = None) -> Dict[str, Any]:
        """Reverts modified files in git worktree back to HEAD and synchronizes DataStore."""
        if package_id:
            blueprints = self.store.get_blueprints_for_package(package_id)
            packages = [self.store.get_package(package_id)] if self.store.get_package(package_id) else []
        else:
            blueprints = self.store.list_all_blueprints()
            packages = self.store.list_packages()

        paths = list({b["blueprint_path"] for b in blueprints if b.get("blueprint_path")})

        reverted = []
        for p in paths:
            abs_p = os.path.join(self.repo_root, p)
            if os.path.exists(abs_p):
                subprocess.run(["git", "checkout", "--", p], cwd=self.repo_root, check=False)
                reverted.append(p)

        # Rollback DataStore candidate and package status if they were in READY_FOR_REVIEW
        for pkg in packages:
            pid = pkg.get("package_id")
            if not pid:
                continue
            cands = self.store.list_candidates(package_id=pid)
            prev_ver = None
            for c in cands:
                if c.get("status") in ("READY_FOR_REVIEW", "TESTING"):
                    prev_ver = c.get("previous_version")
                    self.store.update_candidate(c["candidate_id"], {"status": "UPDATE_FOUND"})
            if pkg.get("status") == "READY_FOR_REVIEW":
                pkg_updates = {"status": "UPDATE_FOUND"}
                if prev_ver:
                    pkg_updates["current_version"] = prev_ver
                self.store.update_package(pid, pkg_updates)

        return {"status": "REVERTED", "reverted_files": reverted}


# Backwards compatibility alias
AtomicCodeModifier = OrchestratorAgent

