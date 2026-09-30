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
        """Resolves a unique list of tests for multiple modified blueprints."""
        seen_tests = set()
        results = []
        for bp in blueprint_paths:
            test_info = self.resolve_test_for_blueprint(bp)
            if test_info and test_info["test_name"] not in seen_tests:
                seen_tests.add(test_info["test_name"])
                results.append(test_info)
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

    def start_async_test_monitor(
        self,
        candidate_id: str,
        package_id: str,
        build_id: str,
        test_name: str,
        pr_number: Optional[int] = None
    ) -> threading.Thread:
        """
        Spawns a daemon thread to monitor the build in the background and update DataStore upon completion.
        """
        def _monitor():
            try:
                from datastore import get_datastore
                store = get_datastore()

                def _progress_cb(live_status: str, data: Dict[str, Any]):
                    # Keep DataStore updated with latest timestamp
                    pass

                result = self.wait_for_test_completion(
                    build_id=build_id,
                    poll_interval_sec=60,
                    max_wait_sec=86400,  # 24 hours
                    status_callback=_progress_cb
                )

                test_outcome = result.get("status")  # SUCCESS, FAILURE, TIMEOUT
                build_url = result.get("build_url")

                if test_outcome == "SUCCESS":
                    summary = f"Integration test '{test_name}' PASSED on PR #{pr_number or '-'}. Ready for human maintainer review."
                    store.update_candidate(candidate_id, {
                        "status": "READY_FOR_REVIEW",
                        "test_status": "SUCCESS",
                        "build_url": build_url
                    })
                    store.update_package(package_id, {
                        "status": "READY_FOR_REVIEW",
                        "test_status": "SUCCESS",
                        "qualification_summary": summary
                    })
                    print(f"[TestManager] [ASYNC COMPLETE] Package '{package_id}' candidate '{candidate_id}' test PASSED.", flush=True)
                else:
                    reason = result.get("failure_reason", "TEST_FAILED")
                    summary = f"Integration test '{test_name}' FAILED ({reason}) on PR #{pr_number or '-'}. Check Cloud Build logs."
                    store.update_candidate(candidate_id, {
                        "status": "TEST_FAILED",
                        "test_status": "FAILURE",
                        "build_url": build_url
                    })
                    store.update_package(package_id, {
                        "status": "TEST_FAILED",
                        "test_status": "FAILURE",
                        "qualification_summary": summary
                    })
                    print(f"[TestManager] [ASYNC COMPLETE] Package '{package_id}' candidate '{candidate_id}' test FAILED ({reason}).", flush=True)
            except Exception as e:
                print(f"[TestManager] [ASYNC ERROR] Exception in test monitor thread: {e}", flush=True)

        t = threading.Thread(target=_monitor, daemon=True, name=f"test-monitor-{package_id}")
        t.start()
        return t


_GLOBAL_TEST_MANAGER: Optional[TestManager] = None

def get_test_manager() -> TestManager:
    global _GLOBAL_TEST_MANAGER
    if _GLOBAL_TEST_MANAGER is None:
        _GLOBAL_TEST_MANAGER = TestManager()
    return _GLOBAL_TEST_MANAGER
