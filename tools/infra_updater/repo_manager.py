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
Independent Target Repository Manager for Cluster Toolkit Automated Infrastructure Updater.

Decouples the updater execution from the local codebase by maintaining an isolated
workspace clone of the target repository (https://github.com/rahimkhan19/cluster-toolkit, branch develop).
Automates the end-to-end lifecycle:
  1. Fetch / Sync latest develop branch
  2. Create atomic update branch
  3. Commit AST changes
  4. Push branch to remote
  5. Create GitHub Pull Request with automated update details
"""

import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple
import urllib.request
import urllib.error

from tools.infra_updater.config import get_config, UpdaterConfig

class RepoManager:
    """Manages cloning, syncing, branching, committing, pushing, and PR creation for target repos."""

    def __init__(self, config: Optional[UpdaterConfig] = None):
        self.config = config or get_config()
        self.workspace_dir = self.config.get_workspace_path()
        self.repo_url = self.config.repository.url
        self.base_branch = self.config.repository.base_branch
        self.owner = self.config.repository.owner
        self.repo_name = self.config.repository.name
        self.token = self.config.get_github_token()
        self.author_name = self.config.git.author_name
        self.author_email = self.config.git.author_email

    def _run_git(self, args: List[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        """Executes a git command inside the target workspace directory."""
        env = os.environ.copy()
        if self.token:
            env["GITHUB_TOKEN"] = self.token
            env["GH_TOKEN"] = self.token

        res = subprocess.run(
            ["git"] + args,
            cwd=self.workspace_dir,
            capture_output=capture,
            text=True,
            check=False,
            env=env
        )
        if check and res.returncode != 0:
            err = res.stderr.strip() if res.stderr else res.stdout.strip()
            # Redact token from error message if present
            if self.token and self.token in err:
                err = err.replace(self.token, "***")
            raise RuntimeError(f"Git command failed ('git {' '.join(args)}'): {err}")
        return res

    def ensure_workspace(self, force_clean: bool = True) -> str:
        """
        Ensures the target repository is cloned and synced to the latest base_branch.
        Creates workspace_dir if needed and fetches/resets to origin/base_branch.
        """
        os.makedirs(os.path.dirname(self.workspace_dir), exist_ok=True)
        git_dir = os.path.join(self.workspace_dir, ".git")

        if not os.path.exists(git_dir):
            print(f"[RepoManager] Cloning target repository '{self.repo_url}' (branch: {self.base_branch}) into {self.workspace_dir}...", flush=True)
            clone_cmd = [
                "git", "clone",
                "--branch", self.base_branch,
                "--single-branch",
                self.repo_url,
                self.workspace_dir
            ]
            res = subprocess.run(clone_cmd, capture_output=True, text=True, check=False)
            if res.returncode != 0:
                raise RuntimeError(f"Failed to clone target repo {self.repo_url}: {res.stderr.strip()}")
        else:
            print(f"[RepoManager] Syncing workspace with latest origin/{self.base_branch}...", flush=True)
            self._run_git(["fetch", "origin", self.base_branch])
            if force_clean:
                self._run_git(["checkout", "-f", self.base_branch])
                self._run_git(["reset", "--hard", f"origin/{self.base_branch}"])
                self._run_git(["clean", "-fd"])

        # Configure local git author from config
        self._run_git(["config", "user.name", self.author_name], check=False)
        self._run_git(["config", "user.email", self.author_email], check=False)

        return self.workspace_dir

    def prepare_update_branch(self, package_id: str, target_version: str) -> str:
        """
        Creates and checks out a clean atomic update branch off the latest develop branch.
        """
        self.ensure_workspace(force_clean=True)
        clean_version = re.sub(r'[^a-zA-Z0-9_\.\-]', '_', target_version)
        branch_name = f"infra-update/{package_id}-{clean_version}"

        print(f"[RepoManager] Creating branch '{branch_name}' off origin/{self.base_branch}...", flush=True)
        self._run_git(["checkout", "-B", branch_name, f"origin/{self.base_branch}"])
        return branch_name

    def get_current_branch(self) -> str:
        res = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        return res.stdout.strip() if res.returncode == 0 else "unknown"

    def get_diff(self, base_ref: Optional[str] = None) -> str:
        """Computes git diff against base branch (e.g. origin/develop) or working tree."""
        ref = base_ref or f"origin/{self.base_branch}"
        res = self._run_git(["diff", ref], check=False)
        return res.stdout

    def get_diff_stat(self, base_ref: Optional[str] = None) -> str:
        ref = base_ref or f"origin/{self.base_branch}"
        res = self._run_git(["diff", "--stat", ref], check=False)
        return res.stdout.strip()

    def commit_changes(
        self,
        package_id: str,
        target_version: str,
        summary: str,
        modified_files: List[str]
    ) -> Tuple[bool, str]:
        """Stages modified files and creates a git commit."""
        if not modified_files:
            return False, "No files modified to commit"

        for f in modified_files:
            self._run_git(["add", f])

        commit_title = f"[infra-update] Upgrade {package_id} to {target_version}"
        commit_body = f"{summary.strip()}\n\nAutomated update generated by Cluster Toolkit Infrastructure Updater."
        full_msg = f"{commit_title}\n\n{commit_body}"

        res = self._run_git(["commit", "-m", full_msg], check=False)
        if res.returncode == 0:
            rev = self._run_git(["rev-parse", "HEAD"]).stdout.strip()
            print(f"[RepoManager] Committed changes: {rev[:8]} - {commit_title}", flush=True)
            return True, rev
        else:
            return False, res.stderr.strip() or res.stdout.strip()

    def push_branch(self, branch_name: str) -> bool:
        """Pushes the update branch to the remote repository."""
        token = self.config.get_github_token()
        if token:
            remote_url = f"https://x-access-token:{token}@github.com/{self.owner}/{self.repo_name}.git"
        else:
            remote_url = "origin"

        print(f"[RepoManager] Pushing branch '{branch_name}' to remote...", flush=True)
        res = self._run_git(["push", remote_url, branch_name, "--force"], check=False)
        if res.returncode == 0:
            print(f"[RepoManager] Branch '{branch_name}' pushed successfully to {self.owner}/{self.repo_name}.", flush=True)
            return True
        else:
            err = res.stderr.strip()
            if token and token in err:
                err = err.replace(token, "***")
            print(f"[RepoManager] [WARN] Push failed: {err}", flush=True)
            return False

    def create_pull_request(
        self,
        package_id: str,
        target_version: str,
        candidate_info: Dict[str, Any],
        modified_blueprints: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        """
        Creates a GitHub Pull Request using GitHub REST API.
        Formats full PR description including release overview and qualification verification.
        """
        token = self.config.get_github_token()
        if not token:
            return {
                "status": "SKIPPED",
                "message": "GITHUB_TOKEN not available. Branch committed locally; PR creation skipped.",
                "pr_url": None
            }

        branch_name = self.get_current_branch()
        title = f"[Infra Update] Upgrade {package_id} to {target_version}"

        # Markdown Body Construction
        dl_url = candidate_info.get("download_url", "-")
        curr_ver = candidate_info.get("current_version") or candidate_info.get("previous_version")
        if not curr_ver or curr_ver == "-":
            try:
                from tools.infra_updater.datastore import get_datastore
                pkg = get_datastore().get_package(package_id)
                if pkg and pkg.get("current_version"):
                    curr_ver = pkg.get("current_version")
            except Exception:
                pass
        if not curr_ver or curr_ver == "-":
            if modified_blueprints:
                for mb in modified_blueprints:
                    old_v = mb.get("old_value")
                    if old_v and old_v != "-":
                        curr_ver = old_v
                        break
        if not curr_ver:
            curr_ver = "-"

        summary = candidate_info.get("summary") or candidate_info.get("changelog_summary", "Qualified production GA release.")

        body_lines = [
            f"## Automated Infrastructure Update: `{package_id}`",
            "",
            "### Release Overview",
            f"- **Component ID:** `{package_id}`",
            f"- **Current Blueprint Version:** `{curr_ver}`",
            f"- **Target Qualified Version:** `{target_version}`",
            f"- **Artifact URL:** {dl_url}",
            "",
            "### Summary",
            f"{summary}",
            ""
        ]

        if modified_blueprints:
            body_lines.append("### Modified Blueprints & Coupled Variables")
            body_lines.append("| Blueprint File | Variable | Old Value | New Value |")
            body_lines.append("| :--- | :--- | :--- | :--- |")
            for m in modified_blueprints:
                body_lines.append(f"| `{m.get('file_path')}` | `{m.get('primary_variable')}` | `{m.get('old_value')}` | `{m.get('new_value')}` |")
            body_lines.append("")

        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "ClusterToolkitInfraUpdater/1.0"
        }

        # Note any other open PRs for this package (both will remain open)
        open_pkg_prs = self._find_open_prs_for_package(package_id, headers)
        other_prs = [p for p in open_pkg_prs if p.get("head", {}).get("ref") != branch_name]
        if other_prs:
            body_lines.append("### Related Open Pull Requests")
            for op in other_prs:
                op_num = op.get("number")
                op_branch = op.get("head", {}).get("ref", "")
                body_lines.append(f"> [!NOTE]\n> Pull request **#{op_num}** (`{op_branch}`) is also currently open for this package.")
            body_lines.append("")

        body_lines.append("---")
        body_lines.append("*Generated automatically by the Cluster Toolkit Automated Infrastructure Updater.*")

        pr_body = "\n".join(body_lines)

        url = f"https://api.github.com/repos/{self.owner}/{self.repo_name}/pulls"
        payload = {
            "title": title,
            "head": branch_name,
            "base": self.base_branch,
            "body": pr_body,
            "draft": False,
            "maintainer_can_modify": True
        }

        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                pr_url = data.get("html_url")
                pr_number = data.get("number")
                print(f"[RepoManager] Created Pull Request #{pr_number}: {pr_url}", flush=True)

                # Keep any existing older PRs open for human maintainer review

                return {
                    "status": "CREATED",
                    "pr_url": pr_url,
                    "pr_number": pr_number,
                    "branch": branch_name
                }
        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="ignore")
            # If PR already exists for this branch, lookup existing PR (Idempotent)
            if he.code == 422 and "already exists" in err_body:
                print(f"[RepoManager] PR already exists for branch {branch_name}, looking up PR URL...", flush=True)
                existing = self._find_existing_pr(branch_name, headers)
                if existing:
                    return {
                        "status": "EXISTING",
                        "pr_url": existing.get("html_url"),
                        "pr_number": existing.get("number"),
                        "branch": branch_name
                    }
            print(f"[RepoManager] [WARN] GitHub PR creation returned HTTP {he.code}: {err_body}", flush=True)
            return {
                "status": "ERROR",
                "error": f"HTTP {he.code}: {err_body}",
                "branch": branch_name,
                "pr_url": None
            }
        except Exception as ex:
            print(f"[RepoManager] [WARN] PR creation exception: {ex}", flush=True)
            return {
                "status": "ERROR",
                "error": str(ex),
                "branch": branch_name,
                "pr_url": None
            }

    def _find_existing_pr(self, branch_name: str, headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Queries GitHub for existing open PR matching head branch."""
        try:
            head_param = f"{self.owner}:{branch_name}"
            url = f"https://api.github.com/repos/{self.owner}/{self.repo_name}/pulls?head={urllib.parse.quote(head_param)}&state=open"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req) as resp:
                prs = json.loads(resp.read().decode("utf-8"))
                if prs and len(prs) > 0:
                    return prs[0]
        except Exception:
            pass
        return None

    def _find_open_prs_for_package(self, package_id: str, headers: Dict[str, str]) -> List[Dict[str, Any]]:
        """Queries GitHub for any open PRs associated with this package."""
        try:
            url = f"https://api.github.com/repos/{self.owner}/{self.repo_name}/pulls?state=open&per_page=50"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req) as resp:
                prs = json.loads(resp.read().decode("utf-8"))
                matching = []
                for p in prs:
                    head_ref = p.get("head", {}).get("ref", "")
                    if head_ref.startswith(f"infra-update/{package_id}-"):
                        matching.append(p)
                return matching
        except Exception:
            return []

    def _add_labels_to_pr(self, pr_number: int, labels: List[str], headers: Dict[str, str]):
        """Attaches labels to the created pull request."""
        try:
            url = f"https://api.github.com/repos/{self.owner}/{self.repo_name}/issues/{pr_number}/labels"
            payload = {"labels": labels}
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req):
                pass
        except Exception as e:
            print(f"[RepoManager] [WARN] Failed to attach labels to PR #{pr_number}: {e}", flush=True)

    def sync_open_pr_statuses(self, store: Optional[Any] = None) -> Dict[str, Any]:
        """
        Synchronizes datastore status with remote GitHub Pull Request states.
        If a developer closes a PR manually without merging it, candidate and package
        move back to 'UPDATE_FOUND' (update available).
        If a PR was merged into the base branch, status transitions to 'MERGED' / 'UP_TO_DATE'.
        """
        if store is None:
            from tools.infra_updater.datastore import get_datastore
            store = get_datastore()

        token = self.config.get_github_token()
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "ClusterToolkitInfraUpdater/1.0"
        }
        if token:
            headers["Authorization"] = f"token {token}"

        # Fetch recent pull requests from GitHub (both open and closed)
        url = f"https://api.github.com/repos/{self.owner}/{self.repo_name}/pulls?state=all&per_page=100"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                pulls_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            print(f"[RepoManager] [WARN] Failed to fetch PRs from GitHub (HTTP {he.code}): {he.reason}", flush=True)
            return {"synced": False, "error": f"HTTP {he.code}"}
        except Exception as e:
            print(f"[RepoManager] [WARN] PR sync exception: {e}", flush=True)
            return {"synced": False, "error": str(e)}

        prs_by_number = {p.get("number"): p for p in pulls_data if p.get("number")}
        prs_by_branch = {p.get("head", {}).get("ref"): p for p in pulls_data if p.get("head", {}).get("ref")}

        changes = []
        candidates = store.list_candidates()
        for cand in candidates:
            cand_id = cand.get("candidate_id")
            pkg_id = cand.get("package_id")
            status = cand.get("status")
            pr_url = cand.get("pr_url")
            branch = cand.get("branch")

            # Look up corresponding GitHub PR
            pr_data = None
            if pr_url:
                m = re.search(r"/pull/(\d+)", pr_url)
                if m:
                    pr_num = int(m.group(1))
                    pr_data = prs_by_number.get(pr_num)
            if not pr_data and branch:
                pr_data = prs_by_branch.get(branch)

            if not pr_data:
                # If candidate was in READY_FOR_REVIEW but PR is not on GitHub
                if status == "READY_FOR_REVIEW" and pr_url:
                    print(f"[RepoManager] PR for candidate '{cand_id}' not found on GitHub. Moving back to UPDATE_FOUND.", flush=True)
                    prev_ver = cand.get("previous_version")
                    store.update_candidate(cand_id, {"status": "UPDATE_FOUND", "pr_url": None})
                    pkg_update = {"status": "UPDATE_FOUND"}
                    if prev_ver:
                        pkg_update["current_version"] = prev_ver
                    store.update_package(pkg_id, pkg_update)
                    changes.append({"candidate_id": cand_id, "action": "REVERTED_TO_UPDATE_FOUND", "reason": "PR not found"})
                continue

            pr_num = pr_data.get("number")
            pr_state = pr_data.get("state") # "open" or "closed"
            merged_at = pr_data.get("merged_at")
            is_merged = bool(merged_at or pr_data.get("merged", False))

            if pr_state == "closed":
                if is_merged:
                    # Successfully merged into base branch
                    if status != "MERGED":
                        print(f"[RepoManager] PR #{pr_num} was MERGED into {self.base_branch}. Updating status to UP_TO_DATE.", flush=True)
                        store.update_candidate(cand_id, {
                            "status": "MERGED",
                            "pr_url": pr_data.get("html_url")
                        })
                        store.update_package(pkg_id, {
                            "status": "UP_TO_DATE",
                            "current_version": cand.get("target_version")
                        })
                        changes.append({"candidate_id": cand_id, "action": "MERGED", "pr_number": pr_num})
                else:
                    # Closed manually without merging! Move back to update available!
                    if status != "UPDATE_FOUND":
                        print(f"[RepoManager] PR #{pr_num} for package '{pkg_id}' was closed manually without merge. Moving back to UPDATE_FOUND (update available).", flush=True)
                        prev_ver = cand.get("previous_version")
                        store.update_candidate(cand_id, {
                            "status": "UPDATE_FOUND",
                            "pr_url": None
                        })
                        pkg_update = {"status": "UPDATE_FOUND"}
                        if prev_ver:
                            pkg_update["current_version"] = prev_ver
                        store.update_package(pkg_id, pkg_update)
                        changes.append({"candidate_id": cand_id, "action": "REVERTED_TO_UPDATE_FOUND", "pr_number": pr_num})
            elif pr_state == "open":
                # Ensure status is READY_FOR_REVIEW
                if status != "READY_FOR_REVIEW" or cand.get("pr_url") != pr_data.get("html_url"):
                    store.update_candidate(cand_id, {
                        "status": "READY_FOR_REVIEW",
                        "pr_url": pr_data.get("html_url"),
                        "branch": pr_data.get("head", {}).get("ref")
                    })
                    store.update_package(pkg_id, {
                        "status": "READY_FOR_REVIEW"
                    })
                    changes.append({"candidate_id": cand_id, "action": "SYNCED_READY_FOR_REVIEW", "pr_number": pr_num})

        # Ensure package statuses reflect their active candidates
        for pkg in store.list_packages():
            pid = pkg.get("package_id")
            pkg_cands = store.list_candidates(package_id=pid)
            if not pkg_cands:
                continue
            has_ready = any(c.get("status") == "READY_FOR_REVIEW" for c in pkg_cands)
            has_update = any(c.get("status") in ("UPDATE_FOUND", "QUALIFIED") for c in pkg_cands)
            if has_ready and pkg.get("status") != "READY_FOR_REVIEW":
                store.update_package(pid, {"status": "READY_FOR_REVIEW"})
            elif not has_ready and has_update and pkg.get("status") != "UPDATE_FOUND":
                store.update_package(pid, {"status": "UPDATE_FOUND"})

        return {"synced": True, "changes": changes}
