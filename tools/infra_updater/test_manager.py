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
Test Manager for Cluster Toolkit Automated Infrastructure Updater.

Maps modified blueprints to corresponding Cloud Build integration test triggers,
approves/triggers tests on pull request creation, waits for execution to complete,
and records test results (SUCCESS / FAILURE) into DataStore for dashboard telemetry.
"""

import glob
import json
import os
import re
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import yaml

from config import get_config, BASE_DIR

# Known special case mappings for blueprints to test names
SPECIAL_BLUEPRINT_TESTS = {
    "examples/storage-slurm.yaml": "slurm-storage",
    "storage-slurm.yaml": "slurm-storage",
    "examples/batch-mpi.yaml": "batch-mpi",
    "batch-mpi.yaml": "batch-mpi",
    "examples/ml-slurm.yaml": "ml-slurm",
    "ml-slurm.yaml": "ml-slurm",
    "examples/ml-slurm-g4.yaml": "ml-g4-onspot-slurm",
    "ml-slurm-g4.yaml": "ml-g4-onspot-slurm",
    "examples/machine-learning/a4x-highgpu-4g/a4x-vm.yaml": "ml-a4x-highgpu-slurm",
    "a4x-vm.yaml": "ml-a4x-highgpu-slurm",
    "examples/machine-learning/a4x-maxgpu-4g-metal/a4xmax-bm-slurm-blueprint.yaml": "gke-a4x-max-bm",
    "a4xmax-bm-slurm-blueprint.yaml": "gke-a4x-max-bm",
    "examples/machine-learning/a3-megagpu-8g/a3mega-slurm-blueprint.yaml": "ml-a3-megagpu-onspot-slurm-ubuntu",
    "a3mega-slurm-blueprint.yaml": "ml-a3-megagpu-onspot-slurm-ubuntu",
    "examples/machine-learning/a3-megagpu-8g/a3mega-slurm-gcsfuse-lssd-blueprint.yaml": "ml-a3-megagpu-onspot-slurm-ubuntu",
    "a3mega-slurm-gcsfuse-lssd-blueprint.yaml": "ml-a3-megagpu-onspot-slurm-ubuntu",
    "examples/machine-learning/build-service-images/a3m/blueprint.yaml": "ml-a3-megagpu-onspot-slurm-ubuntu",
    "examples/machine-learning/build-service-images/shared.yaml": "hpc-build-slurm-image",
    "shared.yaml": "hpc-build-slurm-image",
    "examples/gke-consumption-options/dws-flex-start-compact-placement/gke-h4d/gke-h4d.yaml": "gke-h4d-onspot",
    "tools/cloud-build/daily-tests/blueprints/e2e.yaml": "e2e",
    "e2e.yaml": "e2e",
    "tools/cloud-build/daily-tests/blueprints/crd-default.yaml": "chrome-remote-desktop",
    "crd-default.yaml": "chrome-remote-desktop",
    "tools/cloud-build/daily-tests/blueprints/crd-ubuntu.yaml": "chrome-remote-desktop-ubuntu",
    "crd-ubuntu.yaml": "chrome-remote-desktop-ubuntu",
}


def compute_tests_summary(tests: List[Dict[str, Any]]) -> Tuple[str, Optional[str], str]:
    """
    Computes human-readable summary, overall test status, and overall candidate workflow status.
    Returns: (summary_str, test_status, workflow_status)
    e.g. ("5/6 passed (1 failed)", "FAILURE", "TEST_FAILED")
         ("6/6 tests passed", "SUCCESS", "READY_FOR_REVIEW")
         ("2/6 passed (4 running)", "RUNNING", "TESTING")
    """
    if not tests:
        return ("No tests mapped", None, "READY_FOR_REVIEW")

    total = len(tests)
    passed = sum(1 for t in tests if t.get("status") == "SUCCESS")
    failed = sum(1 for t in tests if t.get("status") in ("FAILURE", "ERROR", "TIMEOUT"))
    running = sum(1 for t in tests if t.get("status") in ("RUNNING", "TRIGGERED", "PENDING", "QUEUED"))

    if total == 1:
        if passed == 1:
            return ("1/1 passed", "SUCCESS", "READY_FOR_REVIEW")
        elif failed == 1:
            return ("0/1 passed (1 failed)", "FAILURE", "TEST_FAILED")
        elif running == 1:
            return ("1 running", "RUNNING", "TESTING")
        else:
            status = tests[0].get("status", "PENDING")
            return (f"1 test ({status})", "RUNNING", "TESTING")

    # Multiple tests
    if passed == total:
        return (f"{passed}/{total} tests passed", "SUCCESS", "READY_FOR_REVIEW")
    elif running > 0:
        if failed > 0:
            summary = f"{passed}/{total} passed ({failed} failed, {running} running)"
        else:
            summary = f"{passed}/{total} passed ({running} running)" if passed > 0 else f"{running}/{total} running"
        return (summary, "RUNNING", "TESTING")
    elif failed > 0:
        return (f"{passed}/{total} passed ({failed} failed)", "FAILURE", "TEST_FAILED")
    else:
        return (f"{passed}/{total} passed", "SUCCESS" if passed == total else "RUNNING", "READY_FOR_REVIEW" if passed == total else "TESTING")


class TestManager:
    """Manages integration test discovery, Cloud Build approval/triggering, and lifecycle monitoring."""

    def __init__(self, config=None, project_id: Optional[str] = None):
        self.config = config or get_config()
        self.project_id = project_id or self.config.database.project_id or "hpc-toolkit-dev"
        self._session = None
        self._test_mapping_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self.repo_root = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))

    def _get_session(self):
        """Returns an authenticated HTTP session for Cloud Build API."""
        if self._session is None:
            try:
                import google.auth
                from google.auth.transport.requests import AuthorizedSession
                credentials, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
                self._session = AuthorizedSession(credentials)
            except Exception as e:
                print(f"[TestManager] [WARN] Could not initialize google.auth session: {e}", flush=True)
                self._session = None
        return self._session

    def get_blueprint_mapping(self) -> Dict[str, Dict[str, Any]]:
        """
        Scans tools/cloud-build/daily-tests/tests/*.yml across repository root and target workspace
        to build a map of blueprint path -> test metadata.
        """
        if self._test_mapping_cache is not None:
            return self._test_mapping_cache

        mapping: Dict[str, Dict[str, Any]] = {}

        # Search candidates in repo_root and target workspace
        search_dirs = [
            os.path.join(self.repo_root, "tools", "cloud-build", "daily-tests"),
            os.path.join(self.config.get_workspace_path(), "tools", "cloud-build", "daily-tests")
        ]

        test_files = []
        for sdir in search_dirs:
            pattern = os.path.join(sdir, "tests", "*.yml")
            found = glob.glob(pattern)
            if found:
                test_files.extend(found)

        for tf in test_files:
            test_name = os.path.basename(tf)[:-4]  # strip .yml
            trigger_name = f"PR-test-{test_name}"
            build_file = os.path.join("tools", "cloud-build", "daily-tests", "builds", f"{test_name}.yaml")
            try:
                with open(tf, "r", encoding="utf-8") as f:
                    y = yaml.safe_load(f)
                    if isinstance(y, dict):
                        bp = y.get("blueprint_yaml")
                        if bp:
                            cleaned = bp.replace("{{ workspace }}/", "").lstrip("./")
                            meta = {
                                "test_name": test_name,
                                "trigger_name": trigger_name,
                                "test_file": tf,
                                "build_file": build_file,
                                "blueprint_yaml": cleaned
                            }
                            mapping[cleaned] = meta
                            mapping[os.path.basename(cleaned)] = meta
            except Exception:
                pass

        # Apply special case overrides / supplements
        for bp_path, test_name in SPECIAL_BLUEPRINT_TESTS.items():
            meta = {
                "test_name": test_name,
                "trigger_name": f"PR-test-{test_name}",
                "build_file": os.path.join("tools", "cloud-build", "daily-tests", "builds", f"{test_name}.yaml"),
                "blueprint_yaml": bp_path
            }
            if bp_path not in mapping:
                mapping[bp_path] = meta
            if os.path.basename(bp_path) not in mapping:
                mapping[os.path.basename(bp_path)] = meta

        self._test_mapping_cache = mapping
        return mapping

    def resolve_test_for_blueprint(self, blueprint_path: str) -> Optional[Dict[str, Any]]:
        """Resolves test metadata for a given blueprint path or filename."""
        mapping = self.get_blueprint_mapping()
        cleaned = blueprint_path.replace("{{ workspace }}/", "").lstrip("./")
        if cleaned in mapping:
            return mapping[cleaned]
        base = os.path.basename(cleaned)
        if base in mapping:
            return mapping[base]

        # Fuzzy lookup: match by end of path
        for k, v in mapping.items():
            if cleaned.endswith(k) or k.endswith(cleaned):
                return v

        return None

    def resolve_tests_for_blueprints(self, blueprint_paths: List[str]) -> List[Dict[str, Any]]:
        """Resolves a unique list of tests for multiple modified blueprints, linking each blueprint."""
        seen_tests = set()
        results = []
        for bp in blueprint_paths:
            test_info = self.resolve_test_for_blueprint(bp)
            if test_info:
                t_name = test_info["test_name"]
                if t_name not in seen_tests:
                    seen_tests.add(t_name)
                    t_copy = dict(test_info)
                    t_copy["blueprint_path"] = bp
                    t_copy["blueprint_paths"] = [bp]
                    results.append(t_copy)
                else:
                    for r in results:
                        if r["test_name"] == t_name:
                            if bp not in r.get("blueprint_paths", []):
                                r.setdefault("blueprint_paths", []).append(bp)
                            break
        return results

    def trigger_test_for_pr(
        self,
        pr_number: int,
        branch_name: str,
        test_info: Dict[str, Any],
        poll_timeout_sec: int = 90,
        poll_interval_sec: int = 5
    ) -> Dict[str, Any]:
        """
        Finds the pending Cloud Build for this PR number and approves it, or triggers directly.
        Returns a dict with build_id, build_url, and trigger status.
        """
        test_name = test_info["test_name"]
        trigger_name = test_info.get("trigger_name", f"PR-test-{test_name}")
        session = self._get_session()

        print(f"[TestManager] Locating Cloud Build for PR #{pr_number} (trigger: {trigger_name})...", flush=True)

        # 1. Search for pending Cloud Build created by GitHub PR webhook
        start_time = time.time()
        matching_build = None

        if session:
            while time.time() - start_time < poll_timeout_sec:
                try:
                    # Query builds by PR number using indexed single-field filter for fast lookup (~0.5s)
                    url = f"https://cloudbuild.googleapis.com/v1/projects/{self.project_id}/builds"
                    params = {"filter": f'substitutions._PR_NUMBER="{pr_number}"', "pageSize": "100"}
                    resp = session.get(url, params=params, timeout=30)
                    if resp.status_code == 200:
                        builds = resp.json().get("builds", [])
                        for b in builds:
                            b_trig = b.get("substitutions", {}).get("TRIGGER_NAME")
                            if b_trig == trigger_name:
                                matching_build = b
                                break
                        if matching_build:
                            break
                except Exception as e:
                    print(f"[TestManager] [DEBUG] Build query attempt failed: {e}", flush=True)
                time.sleep(poll_interval_sec)

        # 2. If matching build found in PENDING status, approve it!
        if matching_build:
            build_id = matching_build["id"]
            log_url = matching_build.get("logUrl") or f"https://console.cloud.google.com/cloud-build/builds/{build_id}?project={self.project_id}"
            curr_status = matching_build.get("status")
            approval = matching_build.get("approval", {})
            approval_state = approval.get("state")

            print(f"[TestManager] Found build {build_id} (Status: {curr_status}, Approval: {approval_state})", flush=True)

            if approval_state == "PENDING" or curr_status == "PENDING":
                print(f"[TestManager] Approving build {build_id} for test '{test_name}'...", flush=True)
                approved = False
                if session:
                    try:
                        approve_url = f"https://cloudbuild.googleapis.com/v1/projects/{self.project_id}/builds/{build_id}:approve"
                        payload = {
                            "approvalResult": {
                                "decision": "APPROVED",
                                "comment": f"Approved test '{test_name}' on PR #{pr_number} by Automated Infra Updater"
                            }
                        }
                        res = session.post(approve_url, json=payload, timeout=20)
                        if res.status_code in (200, 201):
                            approved = True
                            print(f"[TestManager] Build {build_id} APPROVED via REST API.", flush=True)
                        else:
                            print(f"[TestManager] [WARN] Approve REST API returned HTTP {res.status_code}: {res.text}", flush=True)
                    except Exception as ex:
                        print(f"[TestManager] [WARN] REST API approval call failed: {ex}", flush=True)

                if not approved:
                    # Fallback to gcloud beta builds approve
                    try:
                        cmd = ["gcloud", "beta", "builds", "approve", build_id, f"--project={self.project_id}"]
                        sub_res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                        if sub_res.returncode == 0:
                            approved = True
                            print(f"[TestManager] Build {build_id} APPROVED via gcloud CLI.", flush=True)
                        else:
                            print(f"[TestManager] [WARN] gcloud approve returned {sub_res.returncode}: {sub_res.stderr.strip()}", flush=True)
                    except Exception as gex:
                        print(f"[TestManager] [WARN] gcloud approve execution failed: {gex}", flush=True)

            return {
                "status": "TRIGGERED",
                "action": "APPROVED",
                "build_id": build_id,
                "build_url": log_url,
                "test_name": test_name,
                "trigger_name": trigger_name
            }

        # 3. If no pending build found after timeout, fallback to submit
        print(f"[TestManager] No auto-pending build detected for trigger '{trigger_name}' after {poll_timeout_sec}s; attempting fallback submit...", flush=True)
        build_file_rel = test_info.get("build_file", "")
        build_file_abs = os.path.join(self.repo_root, build_file_rel) if build_file_rel else ""
        if not os.path.exists(build_file_abs):
            build_file_abs = os.path.join(self.config.get_workspace_path(), build_file_rel)

        triggered_build_id = None
        triggered_log_url = None

        if os.path.exists(build_file_abs):
            try:
                cmd = [
                    "gcloud", "builds", "submit",
                    f"--config={build_file_abs}",
                    f"--substitutions=_PR_NUMBER={pr_number},BRANCH_NAME={branch_name}",
                    f"--project={self.project_id}",
                    "--async",
                    "--format=json"
                ]
                sub_res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if sub_res.returncode == 0:
                    out_json = json.loads(sub_res.stdout)
                    triggered_build_id = out_json.get("id") or out_json.get("metadata", {}).get("build", {}).get("id")
                    triggered_log_url = out_json.get("logUrl")
                    print(f"[TestManager] Build submitted directly via config {build_file_abs}: {triggered_build_id}", flush=True)
            except Exception as gex:
                print(f"[TestManager] [WARN] gcloud builds submit fallback failed: {gex}", flush=True)

        if not triggered_build_id:
            return {
                "status": "ERROR",
                "message": f"Could not find or approve Cloud Build test for PR #{pr_number} (trigger '{trigger_name}') within {poll_timeout_sec}s.",
                "test_name": test_name
            }

        final_log_url = triggered_log_url or f"https://console.cloud.google.com/cloud-build/builds/{triggered_build_id}?project={self.project_id}"
        return {
            "status": "TRIGGERED",
            "action": "SUBMITTED",
            "build_id": triggered_build_id,
            "build_url": final_log_url,
            "test_name": test_name,
            "trigger_name": trigger_name
        }

    def trigger_all_tests_for_pr(
        self,
        pr_number: int,
        branch_name: str,
        test_infos: List[Dict[str, Any]],
        poll_timeout_sec: int = 90,
        poll_interval_sec: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Triggers or approves all integration tests associated with a PR.
        Returns a list of test status records.
        """
        results = []
        for t_info in test_infos:
            t_name = t_info["test_name"]
            print(f"[TestManager] Triggering test '{t_name}' for PR #{pr_number}...", flush=True)
            res = self.trigger_test_for_pr(
                pr_number=pr_number,
                branch_name=branch_name,
                test_info=t_info,
                poll_timeout_sec=poll_timeout_sec,
                poll_interval_sec=poll_interval_sec
            )
            rec = {
                "test_name": t_name,
                "trigger_name": t_info.get("trigger_name", f"PR-test-{t_name}"),
                "blueprint_path": t_info.get("blueprint_path"),
                "blueprint_paths": t_info.get("blueprint_paths", []),
                "build_id": res.get("build_id"),
                "build_url": res.get("build_url"),
                "status": "RUNNING" if res.get("status") == "TRIGGERED" else ("ERROR" if res.get("status") == "ERROR" else "PENDING"),
                "action": res.get("action"),
                "message": res.get("message")
            }
            results.append(rec)
        return results

    def wait_for_test_completion(
        self,
        build_id: str,
        poll_interval_sec: int = 60,
        max_wait_sec: int = 86400,  # 24 hours
        status_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None
    ) -> Dict[str, Any]:
        """
        Polls Cloud Build status until terminal state (SUCCESS, FAILURE, TIMEOUT, CANCELLED).
        Invokes status_callback('RUNNING' | 'SUCCESS' | 'FAILURE', build_data) periodically.
        """
        session = self._get_session()
        start_time = time.time()
        print(f"[TestManager] Monitoring build {build_id} (timeout: {max_wait_sec}s, poll: {poll_interval_sec}s)...", flush=True)

        while time.time() - start_time < max_wait_sec:
            build_data = None
            if session:
                try:
                    url = f"https://cloudbuild.googleapis.com/v1/projects/{self.project_id}/builds/{build_id}"
                    resp = session.get(url, timeout=10)
                    if resp.status_code == 200:
                        build_data = resp.json()
                except Exception as e:
                    print(f"[TestManager] [DEBUG] Poll error: {e}", flush=True)

            if not build_data:
                # Fallback to gcloud builds describe
                try:
                    cmd = ["gcloud", "builds", "describe", build_id, f"--project={self.project_id}", "--format=json"]
                    sub_res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                    if sub_res.returncode == 0:
                        build_data = json.loads(sub_res.stdout)
                except Exception:
                    pass

            if build_data:
                curr_status = build_data.get("status")
                log_url = build_data.get("logUrl") or f"https://console.cloud.google.com/cloud-build/builds/{build_id}?project={self.project_id}"

                if curr_status == "SUCCESS":
                    print(f"[TestManager] Build {build_id} finished: SUCCESS! Log: {log_url}", flush=True)
                    if status_callback:
                        status_callback("SUCCESS", build_data)
                    return {
                        "status": "SUCCESS",
                        "build_id": build_id,
                        "build_url": log_url,
                        "build_data": build_data
                    }
                elif curr_status in ("FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"):
                    print(f"[TestManager] Build {build_id} finished: {curr_status}. Log: {log_url}", flush=True)
                    if status_callback:
                        status_callback("FAILURE", build_data)
                    return {
                        "status": "FAILURE",
                        "failure_reason": curr_status,
                        "build_id": build_id,
                        "build_url": log_url,
                        "build_data": build_data
                    }
                else:
                    # Still running (PENDING, QUEUED, WORKING)
                    if status_callback:
                        status_callback("RUNNING", build_data)

            time.sleep(poll_interval_sec)

        return {
            "status": "TIMEOUT",
            "failure_reason": "MAX_WAIT_EXCEEDED",
            "build_id": build_id,
            "build_url": f"https://console.cloud.google.com/cloud-build/builds/{build_id}?project={self.project_id}"
        }

    def wait_for_all_tests_completion(
        self,
        tests: List[Dict[str, Any]],
        poll_interval_sec: int = 60,
        max_wait_sec: int = 86400,  # 24 hours
        status_callback: Optional[Callable[[List[Dict[str, Any]]], None]] = None
    ) -> List[Dict[str, Any]]:
        """
        Monitors all running tests until each reaches a terminal state (SUCCESS, FAILURE, ERROR, TIMEOUT).
        Invokes status_callback(tests) periodically upon state transitions.
        """
        session = self._get_session()
        start_time = time.time()
        print(f"[TestManager] Monitoring {len(tests)} test(s) (timeout: {max_wait_sec}s, poll: {poll_interval_sec}s)...", flush=True)

        while time.time() - start_time < max_wait_sec:
            all_done = True
            changed = False

            for t in tests:
                if t.get("status") in ("SUCCESS", "FAILURE", "ERROR", "TIMEOUT"):
                    continue

                build_id = t.get("build_id")
                if not build_id:
                    t["status"] = "ERROR"
                    changed = True
                    continue

                all_done = False
                build_data = None
                if session:
                    try:
                        url = f"https://cloudbuild.googleapis.com/v1/projects/{self.project_id}/builds/{build_id}"
                        resp = session.get(url, timeout=10)
                        if resp.status_code == 200:
                            build_data = resp.json()
                    except Exception as e:
                        print(f"[TestManager] [DEBUG] Poll error for build {build_id}: {e}", flush=True)

                if not build_data:
                    try:
                        cmd = ["gcloud", "builds", "describe", build_id, f"--project={self.project_id}", "--format=json"]
                        sub_res = subprocess.run(cmd, capture_output=True, text=True, check=False)
                        if sub_res.returncode == 0:
                            build_data = json.loads(sub_res.stdout)
                    except Exception:
                        pass

                if build_data:
                    curr_status = build_data.get("status")
                    if build_data.get("logUrl"):
                        t["build_url"] = build_data.get("logUrl")

                    if curr_status == "SUCCESS":
                        print(f"[TestManager] Build {build_id} ({t.get('test_name')}) finished: SUCCESS! Log: {t.get('build_url')}", flush=True)
                        t["status"] = "SUCCESS"
                        changed = True
                    elif curr_status in ("FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED"):
                        print(f"[TestManager] Build {build_id} ({t.get('test_name')}) finished: {curr_status}. Log: {t.get('build_url')}", flush=True)
                        t["status"] = "FAILURE"
                        t["failure_reason"] = curr_status
                        changed = True

            if changed and status_callback:
                status_callback(tests)

            if all_done:
                break

            time.sleep(poll_interval_sec)

        # Mark any remaining running tests as TIMEOUT if max_wait_sec exceeded
        for t in tests:
            if t.get("status") in ("RUNNING", "TRIGGERED", "PENDING", "QUEUED"):
                t["status"] = "TIMEOUT"
                t["failure_reason"] = "MAX_WAIT_EXCEEDED"

        return tests

    def start_async_tests_monitor(
        self,
        candidate_id: str,
        package_id: str,
        tests: List[Dict[str, Any]],
        pr_number: Optional[int] = None
    ) -> threading.Thread:
        """
        Spawns a daemon thread to monitor all tests in the background and update DataStore.
        """
        def _monitor():
            try:
                from datastore import get_datastore
                store = get_datastore()

                def _progress_cb(live_tests: List[Dict[str, Any]]):
                    summary_text, overall_test, overall_workflow = compute_tests_summary(live_tests)
                    updates = {
                        "tests": live_tests,
                        "tests_summary": summary_text,
                        "test_status": overall_test,
                        "status": overall_workflow
                    }
                    store.update_candidate(candidate_id, updates)
                    store.update_package(package_id, {
                        "status": overall_workflow,
                        "test_status": overall_test,
                        "tests_summary": summary_text,
                        "qualification_summary": f"Tests progress for PR #{pr_number or '-'}: {summary_text}"
                    })

                self.wait_for_all_tests_completion(
                    tests=tests,
                    poll_interval_sec=60,
                    max_wait_sec=86400,
                    status_callback=_progress_cb
                )

                summary_text, overall_test, overall_workflow = compute_tests_summary(tests)
                total = len(tests)

                if overall_test == "SUCCESS":
                    qual_summary = f"All {total} integration test(s) PASSED on PR #{pr_number or '-'}. Ready for human review."
                elif overall_test == "FAILURE":
                    failed = sum(1 for t in tests if t.get("status") in ("FAILURE", "ERROR", "TIMEOUT"))
                    qual_summary = f"{failed}/{total} integration test(s) FAILED on PR #{pr_number or '-'}. See Cloud Build logs."
                else:
                    qual_summary = f"Tests completed: {summary_text} on PR #{pr_number or '-'}."

                primary_build_url = None
                for t in tests:
                    if t.get("build_url"):
                        primary_build_url = t.get("build_url")
                        break

                cand_updates = {
                    "tests": tests,
                    "tests_summary": summary_text,
                    "test_status": overall_test,
                    "status": overall_workflow,
                }
                if primary_build_url:
                    cand_updates["build_url"] = primary_build_url

                store.update_candidate(candidate_id, cand_updates)
                store.update_package(package_id, {
                    "status": overall_workflow,
                    "test_status": overall_test,
                    "tests_summary": summary_text,
                    "qualification_summary": qual_summary
                })
                print(f"[TestManager] [ASYNC COMPLETE] Package '{package_id}' candidate '{candidate_id}' finished tests: {summary_text}", flush=True)
            except Exception as e:
                print(f"[TestManager] [ASYNC ERROR] Exception in async tests monitor thread: {e}", flush=True)

        t = threading.Thread(target=_monitor, daemon=True, name=f"tests-monitor-{package_id}")
        t.start()
        return t

    def start_async_test_monitor(
        self,
        candidate_id: str,
        package_id: str,
        build_id: str,
        test_name: str,
        pr_number: Optional[int] = None
    ) -> threading.Thread:
        """Backwards compatibility for single-test monitoring."""
        test_rec = {
            "test_name": test_name,
            "trigger_name": f"PR-test-{test_name}",
            "build_id": build_id,
            "build_url": f"https://console.cloud.google.com/cloud-build/builds/{build_id}?project={self.project_id}",
            "status": "RUNNING"
        }
        return self.start_async_tests_monitor(
            candidate_id=candidate_id,
            package_id=package_id,
            tests=[test_rec],
            pr_number=pr_number
        )


_GLOBAL_TEST_MANAGER: Optional[TestManager] = None

def get_test_manager() -> TestManager:
    global _GLOBAL_TEST_MANAGER
    if _GLOBAL_TEST_MANAGER is None:
        _GLOBAL_TEST_MANAGER = TestManager()
    return _GLOBAL_TEST_MANAGER
