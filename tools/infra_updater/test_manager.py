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
approves (or re-runs) the PR's builds, and records test results (SUCCESS / FAILURE)
into the DataStore. Long-running monitoring is done by polling (`refresh_tests`), either
in a blocking loop from the CLI or by the dashboard server's background poller.
"""

import datetime
import glob
from concurrent.futures import ThreadPoolExecutor
import os
import time
from typing import Any, Dict, List, Optional, Tuple
import yaml

from config import get_config, BASE_DIR
from registry import load_registry
from statuses import TEST_FAILED_STATUSES, TEST_TERMINAL_STATUSES, CandidateStatus, TestStatus

CLOUD_BUILD_API = "https://cloudbuild.googleapis.com/v1"

# Cloud Build API build states (external vocabulary, mapped onto TestStatus).
BUILD_SUCCESS = ("SUCCESS",)
BUILD_FAILED = ("FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED", "EXPIRED")

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _clean_bp_path(path: str) -> str:
    return path.replace("{{ workspace }}/", "").removeprefix("./")


def compute_tests_summary(tests: List[Dict[str, Any]]) -> Tuple[str, Optional[str], str]:
    """
    Computes human-readable summary, overall test status, and overall candidate workflow status.
    Returns: (summary_str, test_status, workflow_status)
    e.g. ("5/6 passed (1 failed)", "FAILURE", "TEST_FAILED")
         ("6/6 tests passed", "SUCCESS", "READY_FOR_REVIEW")
         ("2/6 passed (4 running)", "RUNNING", "TESTING")
    """
    if not tests:
        return ("No tests mapped", None, CandidateStatus.READY_FOR_REVIEW)

    total = len(tests)
    passed = sum(1 for t in tests if t.get("status") == TestStatus.SUCCESS)
    failed = sum(1 for t in tests if t.get("status") in TEST_FAILED_STATUSES)
    running = total - passed - failed

    if passed == total:
        return (f"{passed}/{total} passed" if total == 1 else f"{passed}/{total} tests passed",
                TestStatus.SUCCESS, CandidateStatus.READY_FOR_REVIEW)
    if running > 0:
        if total == 1:
            summary = "1 running"
        elif failed:
            summary = f"{passed}/{total} passed ({failed} failed, {running} running)"
        elif passed:
            summary = f"{passed}/{total} passed ({running} running)"
        else:
            summary = f"{running}/{total} running"
        return (summary, TestStatus.RUNNING, CandidateStatus.TESTING)
    return (f"{passed}/{total} passed ({failed} failed)", TestStatus.FAILURE, CandidateStatus.TEST_FAILED)


def record_test_results(store, candidate_id: str, package_id: str, tests: List[Dict[str, Any]],
                        pr_number: Optional[int] = None) -> Tuple[str, Optional[str], str]:
    """Persists test progress / final results onto the candidate and its package."""
    summary_text, overall_test, overall_workflow = compute_tests_summary(tests)
    total = len(tests)
    pr_ref = f"PR #{pr_number}" if pr_number else "the PR"
    if overall_test == TestStatus.SUCCESS:
        qual_summary = f"All {total} integration test(s) PASSED on {pr_ref}. Ready for review."
    elif overall_test == TestStatus.FAILURE:
        failed = sum(1 for t in tests if t.get("status") in TEST_FAILED_STATUSES)
        qual_summary = f"{failed}/{total} integration test(s) FAILED on {pr_ref}. See Cloud Build logs."
    else:
        qual_summary = f"Tests in progress on {pr_ref}: {summary_text}"

    store.update_candidate(candidate_id, {
        "status": overall_workflow,
        "test_status": overall_test,
        "tests": tests,
        "tests_summary": summary_text,
    })
    store.update_package(package_id, {
        "status": overall_workflow,
        "test_status": overall_test,
        "tests_summary": summary_text,
        "qualification_summary": qual_summary,
    })
    return summary_text, overall_test, overall_workflow


class TestManager:
    """Manages integration test discovery, Cloud Build approval/triggering, and status polling."""

    def __init__(self, config=None, project_id: Optional[str] = None):
        self.config = config or get_config()
        self.cb = self.config.cloud_build
        self.project_id = project_id or self.cb.project_id
        self._session = None
        self._test_mapping_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self.repo_root = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))
        self.repo_full_name = f"{self.config.repository.owner}/{self.config.repository.name}".lower()

    def _get_session(self):
        """Returns an authenticated HTTP session for the Cloud Build API."""
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            self._session = AuthorizedSession(credentials)
        return self._session

    def _builds_url(self, suffix: str = "") -> str:
        return f"{CLOUD_BUILD_API}/projects/{self.project_id}/builds{suffix}"

    def _console_url(self, build_id: str) -> str:
        return self.cb.console_url.format(build_id=build_id, project_id=self.project_id)

    def _trigger_name(self, test_info: Dict[str, Any]) -> str:
        return test_info.get("trigger_name") or f"{self.cb.trigger_prefix}{test_info['test_name']}"

    # ------------------------------------------------------------ discovery

    def get_blueprint_mapping(self) -> Dict[str, Dict[str, Any]]:
        """
        Scans <cloud_build.tests_dir>/*.yml across repository root and target workspace
        to build a map of blueprint path -> test metadata.
        """
        if self._test_mapping_cache is not None:
            return self._test_mapping_cache

        def _meta(test_name: str, bp: str, test_file: Optional[str] = None) -> Dict[str, Any]:
            return {
                "test_name": test_name,
                "trigger_name": f"{self.cb.trigger_prefix}{test_name}",
                "test_file": test_file,
                "build_file": os.path.join(self.cb.builds_dir, f"{test_name}.yaml"),
                "blueprint_yaml": bp,
            }

        mapping: Dict[str, Dict[str, Any]] = {}
        for root in (self.repo_root, self.config.get_workspace_path()):
            for tf in glob.glob(os.path.join(root, self.cb.tests_dir, "*.yml")):
                try:
                    with open(tf, "r", encoding="utf-8") as f:
                        y = yaml.safe_load(f)
                except (OSError, yaml.YAMLError) as e:
                    print(f"[TestManager] [WARN] Skipping unreadable test file {tf}: {e}", flush=True)
                    continue
                bp = y.get("blueprint_yaml") if isinstance(y, dict) else None
                if bp:
                    cleaned = _clean_bp_path(bp)
                    mapping[cleaned] = _meta(os.path.basename(tf)[:-4], cleaned, tf)

        # packages.yaml test_overrides cover blueprints no test file references (never override discovered ones).
        for bp_path, test_name in load_registry().test_overrides.items():
            mapping.setdefault(bp_path, _meta(test_name, bp_path))

        self._test_mapping_cache = mapping
        return mapping

    def resolve_test_for_blueprint(self, blueprint_path: str) -> Optional[Dict[str, Any]]:
        """Resolves test metadata for a given blueprint path or filename."""
        return self.get_blueprint_mapping().get(_clean_bp_path(blueprint_path))

    def resolve_tests_for_blueprints(self, blueprint_paths: List[str]) -> List[Dict[str, Any]]:
        """Resolves a unique list of tests for multiple modified blueprints, linking each blueprint."""
        by_name: Dict[str, Dict[str, Any]] = {}
        for bp in blueprint_paths:
            info = self.resolve_test_for_blueprint(bp)
            if not info:
                continue
            rec = by_name.get(info["test_name"])
            if rec is None:
                rec = dict(info, blueprint_path=bp, blueprint_paths=[])
                by_name[info["test_name"]] = rec
            if bp not in rec["blueprint_paths"]:
                rec["blueprint_paths"].append(bp)
        return list(by_name.values())

    # ------------------------------------------------------------- trigger

    def _find_pr_build(self, pr_number: int, trigger_name: str, head_sha: Optional[str]) -> Optional[Dict[str, Any]]:
        """Newest build for this repo's PR + trigger (+ commit SHA when known)."""
        resp = self._get_session().get(
            self._builds_url(),
            params={"filter": f'substitutions._PR_NUMBER="{pr_number}"', "pageSize": "100"},
            timeout=30,
        )
        resp.raise_for_status()
        for b in resp.json().get("builds", []):  # API returns newest first
            sub = b.get("substitutions", {})
            if sub.get("TRIGGER_NAME") != trigger_name:
                continue
            if sub.get("REPO_FULL_NAME") and sub["REPO_FULL_NAME"].lower() != self.repo_full_name:
                continue
            if head_sha and sub.get("COMMIT_SHA") != head_sha:
                continue
            return b
        return None

    def _approve(self, build_id: str, test_name: str, pr_number: int) -> None:
        payload = {"approvalResult": {"decision": "APPROVED",
                                      "comment": f"Approved test '{test_name}' on PR #{pr_number} by Automated Infra Updater"}}
        res = self._get_session().post(self._builds_url(f"/{build_id}:approve"), json=payload, timeout=20)
        if res.status_code in (200, 201):
            print(f"[TestManager] Build {build_id} APPROVED.", flush=True)
        else:
            print(f"[TestManager] [WARN] Approve returned HTTP {res.status_code}: {res.text}", flush=True)

    def _retry(self, build_id: str) -> Optional[Dict[str, Any]]:
        """Re-runs a finished build. Returns the new build resource."""
        res = self._get_session().post(self._builds_url(f"/{build_id}:retry"), json={}, timeout=30)
        if res.status_code not in (200, 201):
            print(f"[TestManager] [WARN] Retry of build {build_id} returned HTTP {res.status_code}: {res.text}", flush=True)
            return None
        return res.json().get("metadata", {}).get("build")

    def trigger_test_for_pr(
        self,
        pr_number: int,
        test_info: Dict[str, Any],
        head_sha: Optional[str] = None,
        rerun_finished: bool = False,
        poll_timeout_sec: Optional[int] = None,
        poll_interval_sec: int = 5
    ) -> Dict[str, Any]:
        """
        Finds the Cloud Build created by the PR webhook for this trigger and approves it.
        With rerun_finished=True, a build that already finished is re-run (used by "Run tests").
        """
        test_name = test_info["test_name"]
        trigger_name = self._trigger_name(test_info)
        poll_timeout_sec = poll_timeout_sec or self.cb.build_lookup_timeout_seconds
        print(f"[TestManager] Locating Cloud Build for PR #{pr_number} (trigger: {trigger_name})...", flush=True)

        build = None
        deadline = time.time() + poll_timeout_sec
        while time.time() < deadline:
            try:
                build = self._find_pr_build(pr_number, trigger_name, head_sha)
            except Exception as e:
                print(f"[TestManager] [WARN] Build query failed: {e}", flush=True)
            if build:
                break
            time.sleep(poll_interval_sec)

        if not build:
            return {"status": TestStatus.ERROR, "test_name": test_name,
                    "message": f"No Cloud Build found for PR #{pr_number} (trigger '{trigger_name}') within {poll_timeout_sec}s."}

        action = "FOUND"
        if rerun_finished and build.get("status") in BUILD_SUCCESS + BUILD_FAILED:
            print(f"[TestManager] Build {build['id']} already finished ({build.get('status')}); re-running...", flush=True)
            build = self._retry(build["id"]) or build
            action = "RETRIED"

        build_id = build["id"]
        approval_state = build.get("approval", {}).get("state")
        print(f"[TestManager] Build {build_id} (Status: {build.get('status')}, Approval: {approval_state})", flush=True)
        if approval_state == "PENDING":
            self._approve(build_id, test_name, pr_number)
            action = "APPROVED"

        return {
            "status": "TRIGGERED",
            "action": action,
            "build_id": build_id,
            "build_url": build.get("logUrl") or self._console_url(build_id),
            "test_name": test_name,
            "trigger_name": trigger_name
        }

    def trigger_all_tests_for_pr(
        self,
        pr_number: int,
        test_infos: List[Dict[str, Any]],
        head_sha: Optional[str] = None,
        rerun_finished: bool = False,
    ) -> List[Dict[str, Any]]:
        """Approves (or re-runs) all integration tests of a PR concurrently. Returns test records."""
        self._get_session()  # create the shared session before the worker threads use it
        with ThreadPoolExecutor(max_workers=min(8, len(test_infos) or 1)) as pool:
            responses = list(pool.map(
                lambda t: self.trigger_test_for_pr(pr_number, t, head_sha=head_sha, rerun_finished=rerun_finished),
                test_infos))
        results = []
        for t_info, res in zip(test_infos, responses):
            results.append({
                "test_name": t_info["test_name"],
                "trigger_name": self._trigger_name(t_info),
                "blueprint_path": t_info.get("blueprint_path"),
                "blueprint_paths": t_info.get("blueprint_paths", []),
                "build_id": res.get("build_id"),
                "build_url": res.get("build_url"),
                "status": TestStatus.RUNNING if res.get("status") == "TRIGGERED" else TestStatus.ERROR,
                "action": res.get("action"),
                "message": res.get("message"),
                "triggered_at": _now_iso(),
            })
        return results

    # ------------------------------------------------------------- polling

    def refresh_tests(self, tests: List[Dict[str, Any]], max_wait_sec: Optional[int] = None) -> bool:
        """
        One polling pass: updates each non-terminal test from Cloud Build in place.
        Tests running longer than max_wait_sec (default: cloud_build.max_test_duration_hours)
        are marked TIMEOUT. Returns True if anything changed.
        """
        max_wait_sec = max_wait_sec or self.cb.max_test_duration_seconds
        changed = False
        now = datetime.datetime.now(datetime.timezone.utc)
        for t in tests:
            if t.get("status") in TEST_TERMINAL_STATUSES:
                continue
            build_id = t.get("build_id")
            if not build_id:
                t["status"] = TestStatus.ERROR
                changed = True
                continue
            try:
                resp = self._get_session().get(self._builds_url(f"/{build_id}"), timeout=15)
                resp.raise_for_status()
                build = resp.json()
            except Exception as e:
                print(f"[TestManager] [WARN] Poll error for build {build_id}: {e}", flush=True)
                continue

            if build.get("logUrl") and t.get("build_url") != build["logUrl"]:
                t["build_url"] = build["logUrl"]
                changed = True
            status = build.get("status")
            if status in BUILD_SUCCESS:
                t["status"] = TestStatus.SUCCESS
            elif status in BUILD_FAILED:
                t["status"] = TestStatus.FAILURE
                t["failure_reason"] = status
            else:
                started = t.get("triggered_at")
                if started and (now - datetime.datetime.fromisoformat(started)).total_seconds() > max_wait_sec:
                    t["status"] = TestStatus.TIMEOUT
                    t["failure_reason"] = "MAX_WAIT_EXCEEDED"
                else:
                    continue
            changed = True
            print(f"[TestManager] Build {build_id} ({t.get('test_name')}) finished: {t['status']}. Log: {t.get('build_url')}", flush=True)
        return changed

    def wait_for_all_tests_completion(
        self,
        tests: List[Dict[str, Any]],
        poll_interval_sec: Optional[int] = None,
        max_wait_sec: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Blocking CLI helper: polls until every test reaches a terminal state (or times out)."""
        poll_interval_sec = poll_interval_sec or self.config.server.test_poll_interval_seconds
        max_wait_sec = max_wait_sec or self.cb.max_test_duration_seconds
        print(f"[TestManager] Monitoring {len(tests)} test(s) (timeout: {max_wait_sec}s, poll: {poll_interval_sec}s)...", flush=True)
        while True:
            self.refresh_tests(tests, max_wait_sec=max_wait_sec)
            if all(t.get("status") in TEST_TERMINAL_STATUSES for t in tests):
                return tests
            time.sleep(poll_interval_sec)

    def poll_testing_candidates(self, store) -> int:
        """
        One background pass over every TESTING candidate: refreshes its tests and persists
        any change. Returns the number of candidates updated. State lives in the DataStore,
        so polling naturally resumes after a server restart.
        """
        updated = 0
        for cand in store.list_candidates(status=CandidateStatus.TESTING):
            tests = cand.get("tests") or []
            if not tests or not self.refresh_tests(tests):
                continue
            pr_num = None
            pr_url = cand.get("pr_url") or ""
            if "/pull/" in pr_url:
                pr_num = pr_url.rstrip("/").rsplit("/", 1)[-1]
            summary, _, _ = record_test_results(store, cand["candidate_id"], cand["package_id"], tests, pr_num)
            print(f"[TestManager] Candidate {cand['candidate_id']} ({cand['package_id']}): {summary}", flush=True)
            updated += 1
        return updated


_GLOBAL_TEST_MANAGER: Optional[TestManager] = None

def get_test_manager() -> TestManager:
    global _GLOBAL_TEST_MANAGER
    if _GLOBAL_TEST_MANAGER is None:
        _GLOBAL_TEST_MANAGER = TestManager()
    return _GLOBAL_TEST_MANAGER
