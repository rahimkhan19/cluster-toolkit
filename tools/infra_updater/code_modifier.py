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
Orchestrator (code modifier) for the Cluster Toolkit Infrastructure Updater.
Performs surgical, comment-preserving updates on target blueprints, synchronizes
coupled variables, validates YAML syntax, and opens a PR with integration tests.
All coupling rules and line signature keywords are loaded from the DataStore.
"""

import difflib
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from config import get_config
from datastore import BaseDataStore, get_datastore
from policy import policy_hold
from repo_manager import RepoManager
from statuses import APPLICABLE_CANDIDATE_STATUSES, CandidateStatus, PackageStatus, TestStatus

_VERSION_RE = r'v?[0-9]+(?:\.[0-9]+)+(?:-[a-zA-Z0-9._]+)?'


def _match_v_prefix(reference: str, value: str) -> str:
    """Adds or strips a leading 'v' on value so it matches the reference's style."""
    if re.match(r'^[vV][0-9]', reference) and re.match(r'^[0-9]', value):
        return f"v{value}"
    if re.match(r'^[0-9]', reference) and re.match(r'^[vV][0-9]', value):
        return value[1:]
    return value


def _is_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _format_like(old_val: str, new_val: str) -> str:
    """
    Shapes new_val after old_val:
    - URL with a /<version>/ path segment + bare version -> swap only that segment
    - image reference with a :<version> tag + bare version -> swap only the tag
    - otherwise keep the old value's v-prefix style
    """
    if _is_url(old_val) and not _is_url(new_val):
        m = re.search(rf'/({_VERSION_RE})/', old_val)
        if m:
            return old_val.replace(f"/{m.group(1)}/", f"/{_match_v_prefix(m.group(1), new_val)}/")
        return new_val
    if ":" in old_val and not _is_url(new_val) and not ("/" in new_val and ":" in new_val):
        m = re.search(rf':({_VERSION_RE})$', old_val)
        if m:
            return old_val[:m.start(1)] + _match_v_prefix(m.group(1), new_val)
        return new_val
    return _match_v_prefix(old_val, new_val)


class OrchestratorAgent:
    """
    Orchestrator Agent for Cluster Toolkit Automated Dependency Management.
    Applies a candidate to all selected blueprint instances, pushes a branch, opens a PR,
    and triggers the mapped integration tests.
    """

    def __init__(self, store: Optional[BaseDataStore] = None, repo_root: Optional[str] = None):
        self.config = get_config()
        self.store = store or get_datastore()
        self.repo_manager = RepoManager(self.config)
        self.repo_root = repo_root or self.repo_manager.workspace_dir

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
    ) -> Tuple[str, bool, str, str]:
        """
        Replaces the value of every `var_name: value` line (filtered by signature keywords when
        given). Preserves indentation, quote style, trailing `# comments`, and line endings.
        Returns: (new_text, changed, old_val, applied_val) for the first matching line.
        """
        pattern = re.compile(
            rf'^(?P<prefix>[ \t]*{re.escape(var_name)}:[ \t]*)'
            rf'(?:(?P<q>["\'])(?P<qval>.*?)(?P=q)|(?P<val>\S(?:.*?\S)?))'
            rf'(?P<suffix>(?:[ \t]+#.*)?[ \t]*)$'
        )
        lines = text.splitlines(keepends=True)
        changed = False
        old_val = ""
        applied_val = new_val

        for i, line in enumerate(lines):
            body = line.rstrip("\r\n")
            m = pattern.match(body)
            if not m:
                continue
            if signature_keywords:
                ctx = self._get_yaml_context(lines, i)
                if not any(k.lower() in ctx for k in signature_keywords):
                    continue

            quote = m.group("q") or ""
            current = m.group("qval") if quote else m.group("val")
            target = _format_like(current, new_val)
            if not old_val:
                old_val, applied_val = current, target
            new_line = f"{m.group('prefix')}{quote}{target}{quote}{m.group('suffix')}{line[len(body):]}"
            if new_line != line:
                lines[i] = new_line
                changed = True

        return ("".join(lines), changed, old_val, applied_val)

    def _select_candidate(self, package_id: str, candidate_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if candidate_id:
            return self.store.get_candidate(candidate_id)
        # list_candidates is newest-first
        return next((c for c in self.store.list_candidates(package_id=package_id)
                     if c.get("status") in APPLICABLE_CANDIDATE_STATUSES), None)

    @staticmethod
    def _script_fallbacks(package_id: str, var_name: str, content: str, version: str, url: str) -> Optional[Tuple[str, str, str]]:
        """
        Package-specific in-script replacements for artifacts referenced inside shell
        commands rather than a dedicated YAML variable. Returns (new_content, old_val, new_val).
        (Phase 4 replaces these with declarative registry rules.)
        """
        vn = var_name.lower()
        if "mft" in vn:
            m_old = re.search(r'mft-([0-9.\-]+)-aarch64-deb', content)
            new_base = f"mft-{version}-aarch64-deb"
            out = re.sub(r'https://www\.mellanox\.com/downloads/MFT/mft-[0-9.\-]+-aarch64-deb\.tgz', url, content)
            out = re.sub(r'mft-[0-9.\-]+-aarch64-deb', new_base, out)
            return (out, m_old.group(1) if m_old else "", version) if out != content else None

        if package_id == "miniforge" or "miniforge" in vn:
            m_old = re.search(r'Miniforge3-([0-9.\-]+)-Linux-x86_64\.sh', content)
            out = re.sub(
                r'https://github\.com/conda-forge/miniforge/releases/download/[0-9.\-]+/Miniforge3-[0-9.\-]+-Linux-x86_64\.sh',
                f'https://github.com/conda-forge/miniforge/releases/download/{version}/Miniforge3-{version}-Linux-x86_64.sh',
                content
            )
            out = re.sub(r'Miniforge3-[0-9.\-]+-Linux-x86_64\.sh', f'Miniforge3-{version}-Linux-x86_64.sh', out)
            return (out, m_old.group(1) if m_old else "", version) if out != content else None

        if package_id == "nvidia-dcgm" or "dcgm" in vn or "nvidia_packages" in vn:
            target = version if version.startswith("1:") else f"1:{version}"
            m_old = re.search(r'datacenter-gpu-manager-4-[a-z0-9]+=([0-9.\-:]+)', content)
            out = re.sub(r'(datacenter-gpu-manager-4-[a-z0-9]+=)[0-9.\-:]+', rf'\g<1>{target}', content)
            return (out, m_old.group(1) if m_old else "", target) if out != content else None

        if package_id == "openmpi" or "openmpi" in vn:
            clean = version.lstrip("v")
            parts = clean.split(".")
            maj_min = f"v{parts[0]}.{parts[1]}" if len(parts) >= 2 else f"v{clean}"
            m_old = re.search(r'openmpi-([0-9.]+)\.tar\.bz2', content)
            out = re.sub(
                r'https://download\.open-mpi\.org/release/open-mpi/v[0-9.]+/openmpi-[0-9.]+\.tar\.bz2',
                f'https://download.open-mpi.org/release/open-mpi/{maj_min}/openmpi-{clean}.tar.bz2',
                content
            )
            out = re.sub(r'(openmpi-)[0-9.]+(\.tar\.bz2)', rf'\g<1>{clean}\g<2>', out)
            out = re.sub(r'(cd openmpi-)[0-9.]+', rf'\g<1>{clean}', out)
            out = re.sub(r'(\s+openmpi-)[0-9.]+(\s*)$', rf'\g<1>{clean}\g<2>', out, flags=re.MULTILINE)
            return (out, m_old.group(1) if m_old else "", clean) if out != content else None

        return None

    def apply_update(
        self,
        package_id: str,
        candidate_id: Optional[str] = None,
        create_pr: bool = True,
        run_test: bool = True,
        wait_for_test: bool = False
    ) -> Dict[str, Any]:
        """
        Applies the newest applicable candidate to all selected blueprint instances of package_id.
        Branches off the latest base branch, modifies blueprints, commits, pushes, opens a PR,
        and triggers the mapped integration tests. Test results are recorded by
        wait_for_test (blocking) or by the dashboard server's background poller.
        """
        cand = self._select_candidate(package_id, candidate_id)
        if not cand:
            return {"status": "ERROR", "message": f"No applicable candidate update found for package '{package_id}'."}

        cand_id = cand["candidate_id"]
        cand_version = cand["version"]
        cand_url = cand.get("download_url", "")
        filename = os.path.basename(cand_url)

        hold = policy_hold(self.store.get_package(package_id) or {}, cand_version)
        if hold:
            return {"status": hold[0], "message": f"'{package_id}': {hold[1]} PR creation skipped."}

        instances = self.store.get_blueprints_for_package(package_id)
        if not instances:
            return {"status": "ERROR", "message": f"No blueprint instances registered for package '{package_id}'."}
        selected_instances = [inst for inst in instances if inst.get("enabled", True)]
        if not selected_instances:
            msg = f"All {len(instances)} blueprint instance(s) for package '{package_id}' are deselected. Update skipped."
            print(f"[Orchestrator] {msg}")
            return {"status": "SKIPPED", "message": msg}
        if len(selected_instances) < len(instances):
            print(f"[Orchestrator] Selected blueprints for '{package_id}': {len(selected_instances)}/{len(instances)} (deselected instances skipped).")

        branch_name = self.repo_manager.prepare_update_branch(package_id, cand_version)

        # Compute all edits in memory first (several instances may share a file).
        file_contents: Dict[str, Dict[str, str]] = {}  # rel_path -> {"orig", "new"}
        modified_files = []

        for inst in selected_instances:
            rel_path = inst.get("blueprint_path")
            var_name = inst.get("variable_name")
            sig_keywords = inst.get("signature_keywords") or []
            if not rel_path or not var_name:
                continue

            if rel_path not in file_contents:
                if self.config.repository.is_fork:
                    # Fork branches start from fork/base; take the blueprint from upstream base.
                    self.repo_manager._run_git(["checkout", f"origin/{self.repo_manager.base_branch}", "--", rel_path], check=False)
                abs_path = os.path.join(self.repo_root, rel_path)
                if not os.path.exists(abs_path):
                    print(f"[WARN] Blueprint file not found: {abs_path}")
                    continue
                with open(abs_path, "r", encoding="utf-8", newline="") as f:
                    orig = f.read()
                file_contents[rel_path] = {"orig": orig, "new": orig}
            before = file_contents[rel_path]["new"]

            # Primary replacement value based on variable type
            primary_val = cand_url if "url" in var_name.lower() else cand_version
            if "image" in var_name.lower() and "/" not in primary_val:
                primary_val = f"nvidia/cuda:{primary_val}"
            if package_id == "cmake" or "cmake" in var_name.lower():
                parts = cand_version.lstrip("v").split(".")
                maj_min = f"v{parts[0]}.{parts[1]}" if len(parts) >= 2 else f"v{cand_version}"
                filename = f"cmake-{cand_version.lstrip('v')}-linux-x86_64.sh"
                primary_val = f"https://cmake.org/files/{maj_min}/{filename}"

            new_content, primary_changed, old_primary_val, actual_primary_val = self._replace_variable_in_text(
                before, var_name, primary_val, signature_keywords=sig_keywords
            )
            if not primary_changed:
                fb = self._script_fallbacks(package_id, var_name, before, cand_version, cand_url)
                if fb:
                    new_content, old_primary_val, actual_primary_val = fb
                    primary_changed = True

            coupled_changes = []
            for coupled in inst.get("coupled_vars") or []:
                c_var_name = coupled.get("variable_name")
                c_val = coupled.get("pattern", "{filename}").format(filename=filename, version=cand_version)
                new_content, c_changed, old_c_val, actual_c_val = self._replace_variable_in_text(
                    new_content, c_var_name, c_val, signature_keywords=sig_keywords
                )
                if c_changed:
                    coupled_changes.append({"variable": c_var_name, "old_value": old_c_val, "new_value": actual_c_val})

            if new_content == before:
                continue
            file_contents[rel_path]["new"] = new_content
            modified_files.append({
                "instance_id": inst.get("instance_id"),
                "file_path": rel_path,
                "primary_variable": var_name,
                "old_value": old_primary_val,
                "new_value": actual_primary_val if primary_changed else primary_val,
                "coupled_changes": coupled_changes
            })

        changed_files = {p: c for p, c in file_contents.items() if c["new"] != c["orig"]}
        if not changed_files:
            return {"status": "ERROR", "message": f"No blueprint files were modified for package '{package_id}'."}

        # Validate every file before writing any (all-or-nothing).
        for rel_path, c in changed_files.items():
            try:
                yaml.safe_load(c["new"])
            except yaml.YAMLError as ye:
                return {"status": "ERROR", "message": f"YAML syntax validation failed on {rel_path}: {ye}"}

        all_diffs = {}
        for rel_path, c in changed_files.items():
            with open(os.path.join(self.repo_root, rel_path), "w", encoding="utf-8", newline="") as f:
                f.write(c["new"])
            all_diffs[rel_path] = "".join(difflib.unified_diff(
                c["orig"].splitlines(keepends=True), c["new"].splitlines(keepends=True),
                fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}", n=3
            ))

        commit_ok, commit_info = self.repo_manager.commit_changes(
            package_id=package_id,
            target_version=cand_version,
            summary=cand.get("summary", ""),
            modified_files=list(changed_files)
        )
        if not commit_ok:
            return {"status": "ERROR", "message": f"Git commit failed for package '{package_id}': {commit_info}"}
        head_sha = commit_info

        pushed = self.repo_manager.push_branch(branch_name)
        if not pushed:
            return {"status": "ERROR", "message": f"Git push failed for branch '{branch_name}' on package '{package_id}'."}

        pkg = self.store.get_package(package_id) or {}
        current_blueprint_ver = pkg.get("current_version")

        pr_url = pr_number = None
        if create_pr:
            pr_res = self.repo_manager.create_pull_request(
                package_id=package_id,
                target_version=cand_version,
                candidate_info=cand,
                modified_blueprints=modified_files
            )
            pr_url, pr_number = pr_res.get("pr_url"), pr_res.get("pr_number")
            if not pr_url:
                return {"status": "ERROR", "message": f"Pull request creation failed for package '{package_id}': {pr_res.get('error')}"}

        self.store.update_candidate(cand_id, {
            "status": CandidateStatus.READY_FOR_REVIEW,
            "previous_version": current_blueprint_ver,
            "current_version": current_blueprint_ver,
            "pr_url": pr_url,
            "branch": branch_name,
            "head_sha": head_sha,
            "tests": [],
            "tests_summary": None,
            "test_status": None,
        })
        self.store.update_package(package_id, {"status": PackageStatus.READY_FOR_REVIEW, "test_status": None, "tests_summary": None})

        test_res: Dict[str, Any] = {}
        if create_pr and pr_number and run_test:
            bp_paths = [m["file_path"] for m in modified_files]
            test_res = self._run_tests(cand_id, package_id, pr_number, bp_paths, head_sha=head_sha, wait=wait_for_test)

        return {
            "status": "SUCCESS",
            "candidate_id": cand_id,
            "package_id": package_id,
            "target_version": cand_version,
            "download_url": cand_url,
            "workflow_status": test_res.get("workflow_status", CandidateStatus.READY_FOR_REVIEW),
            "branch": branch_name,
            "pr_url": pr_url,
            "pr_number": pr_number,
            "pushed": pushed,
            "test_status": test_res.get("test_status"),
            "tests": test_res.get("tests", []),
            "tests_summary": test_res.get("tests_summary"),
            "modified_files": modified_files,
            "diffs": all_diffs
        }

    def _run_tests(
        self,
        cand_id: str,
        package_id: str,
        pr_number: int,
        bp_paths: List[str],
        head_sha: Optional[str] = None,
        rerun_finished: bool = False,
        wait: bool = False,
    ) -> Dict[str, Any]:
        """Resolves, approves/re-runs and records the integration tests for a candidate's PR."""
        from test_manager import get_test_manager, record_test_results
        tm = get_test_manager()
        resolved = tm.resolve_tests_for_blueprints(bp_paths)
        if not resolved:
            print(f"[Orchestrator] No integration test mapped for modified blueprints: {bp_paths}", flush=True)
            return {"status": "NO_TESTS", "tests": []}

        print(f"[Orchestrator] Triggering {len(resolved)} integration test(s) for PR #{pr_number}...", flush=True)
        tests = tm.trigger_all_tests_for_pr(pr_number, resolved, head_sha=head_sha, rerun_finished=rerun_finished)
        summary, test_status, workflow = record_test_results(self.store, cand_id, package_id, tests, pr_number)

        if wait and test_status == TestStatus.RUNNING:
            print(f"[Orchestrator] Waiting for {len(tests)} blueprint test(s) to complete...", flush=True)
            tm.wait_for_all_tests_completion(tests)
            summary, test_status, workflow = record_test_results(self.store, cand_id, package_id, tests, pr_number)
        elif test_status == TestStatus.RUNNING:
            print("[Orchestrator] Tests are running; results will be recorded by the dashboard's background poller.", flush=True)

        return {"status": "SUCCESS", "tests": tests, "tests_summary": summary,
                "test_status": test_status, "workflow_status": workflow}

    def trigger_candidate_test(self, candidate_id: str, wait_for_test: bool = False) -> Dict[str, Any]:
        """(Re-)runs the integration tests for an existing candidate with an open PR."""
        cand = self.store.get_candidate(candidate_id)
        if not cand:
            return {"status": "ERROR", "message": f"Candidate '{candidate_id}' not found."}
        m = re.search(r"/pull/(\d+)", cand.get("pr_url") or "")
        if not m:
            return {"status": "ERROR", "message": "Candidate does not have an active PR."}

        package_id = cand["package_id"]
        bp_paths = [i["blueprint_path"] for i in self.store.get_blueprints_for_package(package_id)
                    if i.get("enabled", True) and i.get("blueprint_path")]
        if not bp_paths:
            return {"status": "ERROR", "message": f"No active/selected blueprints found for package '{package_id}'."}

        res = self._run_tests(candidate_id, package_id, int(m.group(1)), bp_paths,
                              head_sha=cand.get("head_sha"), rerun_finished=True, wait=wait_for_test)
        if res["status"] == "NO_TESTS":
            return {"status": "ERROR", "message": f"No integration tests mapped for package '{package_id}'."}
        return res
