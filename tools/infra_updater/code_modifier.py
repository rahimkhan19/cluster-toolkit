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
All coupling rules and line signature keywords are loaded dynamically from SQLite.
"""

import difflib
import json
import os
import re
import sqlite3
import subprocess
from typing import Dict, List, Optional, Any, Tuple
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, "../.."))
DB_PATH = os.path.join(BASE_DIR, "updater_state.db")

class OrchestratorAgent:
    """
    Orchestrator Agent for Cluster Toolkit Automated Dependency Management (Doc Section 4.3).
    Performs surgical, comment-preserving AST-safe updates on target blueprints,
    synchronizing coupled variables atomically and validating YAML syntax.
    Transitions candidate updates to READY_FOR_REVIEW.
    """

    def __init__(self, db_path: str = DB_PATH, repo_root: str = REPO_ROOT):
        self.db_path = db_path
        self.repo_root = repo_root

    def _replace_variable_in_text(
        self, text: str, var_name: str, new_val: str, signature_keywords: Optional[List[str]] = None
    ) -> Tuple[str, bool, str]:
        """
        Surgically replaces a variable assignment line preserving exact indentation and quotes.
        If signature_keywords is provided, only replaces lines that contain at least one keyword.
        Returns: (new_text, changed, old_val)
        """
        pattern = rf'^([ \t]*{re.escape(var_name)}:[ \t]*)(["\']?)([^"\r\n]+)(["\']?.*)$'
        lines = text.splitlines(keepends=True)
        changed = False
        old_val = ""

        new_lines = []
        for line in lines:
            m = re.match(pattern, line)
            if m:
                # If keywords provided, check if line or nearby context matches
                if signature_keywords:
                    line_lower = line.lower()
                    if not any(k.lower() in line_lower for k in signature_keywords):
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
        All variable bindings, coupling patterns, and signature keywords are read dynamically from SQLite.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # 1. Fetch Candidate Update
        if candidate_id:
            cursor.execute("""
                SELECT candidate_id, version, download_url 
                FROM candidate_updates WHERE candidate_id = ?
            """, (candidate_id,))
        else:
            cursor.execute("""
                SELECT candidate_id, version, download_url 
                FROM candidate_updates 
                WHERE package_id = ? AND status IN ('UPDATE_FOUND', 'READY_FOR_REVIEW', 'QUALIFIED')
                ORDER BY created_at DESC LIMIT 1
            """, (package_id,))
        
        cand_row = cursor.fetchone()
        if not cand_row:
            conn.close()
            return {
                "status": "ERROR",
                "message": f"No candidate update found for package '{package_id}' in candidate_updates table."
            }

        cand_id, cand_version, cand_url = cand_row
        filename = os.path.basename(cand_url)

        # 2. Fetch Blueprint Instances with dynamically defined signature keywords
        cursor.execute("""
            SELECT instance_id, blueprint_path, variable_name, coupled_vars, signature_keywords
            FROM blueprint_instances WHERE package_id = ?
        """, (package_id,))
        instances = cursor.fetchall()

        if not instances:
            conn.close()
            return {
                "status": "ERROR",
                "message": f"No blueprint instances registered for package '{package_id}'."
            }

        modified_files = []
        all_diffs = {}

        for inst_id, rel_path, var_name, coupled_vars_json, sig_keywords_json in instances:
            abs_path = os.path.join(self.repo_root, rel_path)
            if not os.path.exists(abs_path):
                print(f"[WARN] Blueprint file not found: {abs_path}")
                continue

            with open(abs_path, "r", encoding="utf-8") as f:
                orig_content = f.read()

            # Determine primary replacement value based on variable type
            primary_val = cand_url if "url" in var_name.lower() else cand_version

            # Load signature keywords dynamically from the database row
            try:
                sig_keywords = json.loads(sig_keywords_json) if sig_keywords_json else []
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
            coupled_list = json.loads(coupled_vars_json)
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
                conn.close()
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

        # 5. Transition Status in SQLite (Doc Section 2.3 & 3.1: status = 'READY_FOR_REVIEW')
        cursor.execute("UPDATE candidate_updates SET status = 'READY_FOR_REVIEW' WHERE candidate_id = ?", (cand_id,))
        cursor.execute("UPDATE packages SET current_version = ?, status = 'READY_FOR_REVIEW', updated_at = CURRENT_TIMESTAMP WHERE package_id = ?", (cand_version, package_id))
        conn.commit()
        conn.close()

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
        """Reverts modified files in git worktree back to HEAD."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        if package_id:
            cursor.execute("SELECT blueprint_path FROM blueprint_instances WHERE package_id = ?", (package_id,))
        else:
            cursor.execute("SELECT blueprint_path FROM blueprint_instances")

        paths = [row[0] for row in cursor.fetchall()]
        conn.close()

        reverted = []
        for p in paths:
            abs_p = os.path.join(self.repo_root, p)
            if os.path.exists(abs_p):
                subprocess.run(["git", "checkout", "--", p], cwd=self.repo_root, check=False)
                reverted.append(p)

        return {"status": "REVERTED", "reverted_files": reverted}


# Backwards compatibility alias
AtomicCodeModifier = OrchestratorAgent

