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
Rewrites the package's pinned versions in the selected blueprints (pins from packages.yaml),
validates YAML syntax, and opens a PR with integration tests.
"""

import difflib
import os
import sys
from typing import Any, Dict, List, Optional
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from config import get_config
from datastore import BaseDataStore, get_datastore
from policy import policy_hold
from registry import load_registry, render_values, rewrite
from repo_manager import RepoManager, parse_pr_url
from statuses import APPLICABLE_CANDIDATE_STATUSES, CandidateStatus, PackageStatus, TestStatus

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

    def _select_candidate(self, package_id: str, candidate_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if candidate_id:
            return self.store.get_candidate(candidate_id)
        # list_candidates is newest-first
        return next((c for c in self.store.list_candidates(package_id=package_id)
                     if c.get("status") in APPLICABLE_CANDIDATE_STATUSES), None)

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

        pkg = self.store.get_package(package_id) or {}
        hold = policy_hold(pkg, cand_version)
        if hold:
            return {"status": hold[0], "message": f"'{package_id}': {hold[1]} PR creation skipped."}

        instances = pkg.get("blueprints", [])
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

        # Rewrite every pin of the package in each selected blueprint (packages.yaml / registry.rewrite).
        pkg_def = load_registry().packages.get(package_id)
        if not pkg_def:
            return {"status": "ERROR", "message": f"Package '{package_id}' is not defined in packages.yaml."}
        values = render_values(cand_version, cand_url)
        file_contents: Dict[str, Dict[str, str]] = {}  # rel_path -> {"orig", "new"}
        modified_files = []

        for rel_path in dict.fromkeys(inst["blueprint_path"] for inst in selected_instances if inst.get("blueprint_path")):
            if self.config.repository.is_fork:
                # Fork branches start from fork/base; take the blueprint from upstream base.
                self.repo_manager._run_git(["checkout", f"origin/{self.repo_manager.base_branch}", "--", rel_path], check=False)
            abs_path = os.path.join(self.repo_root, rel_path)
            if not os.path.exists(abs_path):
                print(f"[WARN] Blueprint file not found: {abs_path}")
                continue
            with open(abs_path, "r", encoding="utf-8", newline="") as f:
                orig = f.read()
            new, changes = rewrite(orig, pkg_def, values)
            file_contents[rel_path] = {"orig": orig, "new": new}
            if new != orig:
                modified_files.append({
                    "instance_id": rel_path,
                    "file_path": rel_path,
                    "primary_variable": changes[0]["variable"],
                    "old_value": changes[0]["old_value"],
                    "new_value": changes[0]["new_value"],
                    "changes": changes,
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
        ref = parse_pr_url(cand.get("pr_url"))
        if not ref:
            return {"status": "ERROR", "message": "Candidate does not have an active PR."}
        repo = self.repo_manager.config.repository
        if (ref[0].lower(), ref[1].lower()) != (repo.owner.lower(), repo.name.lower()):
            return {"status": "ERROR", "message": f"PR {cand['pr_url']} is not on the target repository "
                                                  f"{repo.owner}/{repo.name}; its Cloud Build triggers do not apply."}

        package_id = cand["package_id"]
        pkg = self.store.get_package(package_id) or {}
        bp_paths = [i["blueprint_path"] for i in pkg.get("blueprints", []) if i.get("enabled", True) and i.get("blueprint_path")]
        if not bp_paths:
            return {"status": "ERROR", "message": f"No active/selected blueprints found for package '{package_id}'."}

        res = self._run_tests(candidate_id, package_id, ref[2], bp_paths,
                              head_sha=cand.get("head_sha"), rerun_finished=True, wait=wait_for_test)
        if res["status"] == "NO_TESTS":
            return {"status": "ERROR", "message": f"No integration tests mapped for package '{package_id}'."}
        return res
