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
workspace clone of the configured target repository.
Automates the end-to-end lifecycle:
  1. Fetch / Sync latest base branch
  2. Create atomic update branch
  3. Commit blueprint changes
  4. Push branch to remote
  5. Create GitHub Pull Request with automated update details
"""

import base64
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from config import get_config, UpdaterConfig
from http_client import GitHubClient, GitHubError
from statuses import (
    POLICY_STATUSES, PR_TRACKED_CANDIDATE_STATUSES, CandidateStatus, PackageStatus, derive_package_status,
)

# Workspaces already fetched by this process: every CLI run syncs once, however many RepoManagers it creates.
_SYNCED_WORKSPACES = set()

PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def parse_pr_url(url: Optional[str]) -> Optional[Tuple[str, str, int]]:
    """(owner, repo, number) of a GitHub PR URL. PR URLs are self-describing, so PRs
    opened against a previously configured repository are still tracked correctly."""
    m = PR_URL_RE.search(url or "")
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


class RepoManager:
    """Manages cloning, syncing, branching, committing, pushing, and PR creation for target repos."""

    def __init__(self, config: Optional[UpdaterConfig] = None):
        self.config = config or get_config()
        repo = self.config.repository
        self.workspace_dir = self.config.get_workspace_path()
        self.repo_url = repo.url
        self.base_branch = repo.base_branch
        self.owner = repo.owner
        self.repo_name = repo.name
        self.fork_url = repo.fork_url
        self.fork_owner = repo.fork_owner
        self.token = self.config.github_token
        self.author_name = self.config.git.author_name
        self.author_email = self.config.git.author_email
        self.github = GitHubClient(self.token)
        self.pulls_path = f"/repos/{self.owner}/{self.repo_name}/pulls"

    # ------------------------------------------------------------------ git

    def _git_env(self) -> Dict[str, str]:
        """
        Environment for git subprocesses. The token is passed as an HTTP auth header via
        GIT_CONFIG_* env vars, so it never lands in .git/config, remote URLs, or `ps` output.
        """
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = self.author_name
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = self.author_email
        if self.token:
            basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
        return env

    def _redact(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token and text else text

    def _run_git(self, args: List[str], check: bool = True, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        """Executes a git command inside the target workspace directory."""
        res = subprocess.run(
            ["git"] + args,
            cwd=cwd or self.workspace_dir,
            capture_output=True,
            text=True,
            check=False,
            env=self._git_env(),
        )
        if check and res.returncode != 0:
            err = self._redact((res.stderr or res.stdout).strip())
            raise RuntimeError(f"Git command failed ('git {' '.join(args)}'): {err}")
        return res

    def ensure_workspace(self, force_clean: bool = True) -> str:
        """
        Ensures the target repository is cloned and on the latest base_branch. Remotes are
        configured and fetched once per process; force_clean resets to origin/base_branch every call.
        """
        if not os.path.exists(os.path.join(self.workspace_dir, ".git")):
            parent = os.path.dirname(self.workspace_dir)
            os.makedirs(parent, exist_ok=True)
            print(f"[RepoManager] Cloning target repository '{self.repo_url}' (branch: {self.base_branch}) into {self.workspace_dir}...", flush=True)
            self._run_git(["clone", "--filter=blob:none", "--branch", self.base_branch, "--single-branch",
                           self.repo_url, self.workspace_dir], cwd=parent)
        elif self.workspace_dir not in _SYNCED_WORKSPACES:
            print(f"[RepoManager] Syncing workspace with latest origin/{self.base_branch}...", flush=True)
            self._run_git(["remote", "set-url", "origin", self.repo_url], check=False)
            self._run_git(["fetch", "origin", self.base_branch])

        if self.workspace_dir not in _SYNCED_WORKSPACES:
            # Configure the fork push remote (plain URL; auth comes from _git_env) when a fork is configured.
            if self.config.repository.is_fork:
                remotes = self._run_git(["remote"], check=False).stdout.split()
                verb = "set-url" if "fork" in remotes else "add"
                self._run_git(["remote", verb, "fork", self.fork_url], check=False)
            _SYNCED_WORKSPACES.add(self.workspace_dir)

        if force_clean:
            self._run_git(["checkout", "-f", "-B", self.base_branch, f"origin/{self.base_branch}"])
            self._run_git(["clean", "-fd"])
        return self.workspace_dir

    def prepare_update_branch(self, package_id: str, target_version: str) -> str:
        """
        Creates and checks out a clean update branch off the target repository's latest
        base branch (the same tree blueprints were discovered from), even when pushing to a
        fork whose base branch is behind. Note: if the upstream commits the fork lacks touch
        .github/workflows, pushing them requires a token with the `workflow` scope.
        """
        self.ensure_workspace(force_clean=True)
        clean_version = re.sub(r'[^a-zA-Z0-9_.\-]', '_', target_version)
        branch_name = f"{self.config.pull_request.branch_prefix}{package_id}-{clean_version}"
        base_ref = f"origin/{self.base_branch}"
        print(f"[RepoManager] Creating branch '{branch_name}' off {base_ref}...", flush=True)
        self._run_git(["checkout", "-B", branch_name, base_ref])
        return branch_name

    def get_current_branch(self) -> str:
        res = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        return res.stdout.strip() if res.returncode == 0 else "unknown"

    def get_head_sha(self) -> Optional[str]:
        res = self._run_git(["rev-parse", "HEAD"], check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    def get_diff(self, base_ref: Optional[str] = None) -> str:
        """Computes git diff against base branch (e.g. origin/develop) or working tree."""
        return self._run_git(["diff", base_ref or f"origin/{self.base_branch}"], check=False).stdout

    def get_diff_stat(self, base_ref: Optional[str] = None) -> str:
        return self._run_git(["diff", "--stat", base_ref or f"origin/{self.base_branch}"], check=False).stdout.strip()

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

        self._run_git(["add", "--"] + modified_files)

        commit_title = self.config.pull_request.commit_title.format(package_id=package_id, version=target_version)
        body = f"{(summary or '').strip()}\n\nAutomated update generated by Cluster Toolkit Infrastructure Updater."
        res = self._run_git(["commit", "-m", f"{commit_title}\n\n{body.strip()}"], check=False)
        if res.returncode != 0:
            return False, (res.stderr or res.stdout).strip()
        rev = self.get_head_sha() or ""
        print(f"[RepoManager] Committed changes: {rev[:8]} - {commit_title}", flush=True)
        return True, rev

    def push_branch(self, branch_name: str) -> bool:
        """Pushes the update branch to the fork remote if configured, else to origin."""
        repo = self.config.repository
        remote = "fork" if repo.is_fork else "origin"
        dest_desc = f"{repo.push_owner}/{repo.push_name}" + (" (fork)" if repo.is_fork else "")

        print(f"[RepoManager] Pushing branch '{branch_name}' to {dest_desc}...", flush=True)
        res = self._run_git(["push", "--force", remote, branch_name], check=False)
        if res.returncode == 0:
            print(f"[RepoManager] Branch '{branch_name}' pushed successfully to {dest_desc}.", flush=True)
            return True
        print(f"[RepoManager] [WARN] Push failed: {self._redact(res.stderr.strip())}", flush=True)
        return False

    # --------------------------------------------------------------- github

    @property
    def _head_owner(self) -> str:
        return (self.fork_owner if self.config.repository.is_fork else self.owner) or ""

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
        if not self.token:
            return {
                "status": "SKIPPED",
                "error": "GITHUB_TOKEN not available. Branch committed locally; PR creation skipped.",
                "pr_url": None
            }

        branch_name = self.get_current_branch()
        title = self.config.pull_request.pr_title.format(package_id=package_id, version=target_version)
        dl_url = candidate_info.get("download_url")
        summary = candidate_info.get("summary") or "Qualified production GA release."

        body_lines = [f"## Automated Infrastructure Update: `{package_id}`", ""]
        if modified_blueprints:
            body_lines += [
                "### Modified Blueprints & Coupled Variables",
                "| Blueprint File | Variable | Old Value | New Value |",
                "| :--- | :--- | :--- | :--- |",
            ]
            for m in modified_blueprints:
                body_lines.append(f"| `{m.get('file_path')}` | `{m.get('primary_variable')}` | `{m.get('old_value') or '-'}` | `{m.get('new_value')}` |")
            body_lines.append("")

        body_lines += ["### Summary", summary, ""]
        if dl_url:
            body_lines += [f"- **Artifact URL:** {dl_url}", ""]

        # Note any other open PRs for this package (both will remain open)
        other_prs = [p for p in self._find_open_prs_for_package(package_id) if p.get("head", {}).get("ref") != branch_name]
        if other_prs:
            body_lines.append("### Related Open Pull Requests")
            for op in other_prs:
                body_lines.append(f"> [!NOTE]\n> Pull request **#{op.get('number')}** (`{op.get('head', {}).get('ref', '')}`) is also currently open for this package.")
            body_lines.append("")

        body_lines += ["---", "*Generated automatically by the Cluster Toolkit Automated Infrastructure Updater.*"]

        head_param = f"{self.fork_owner}:{branch_name}" if self.config.repository.is_fork else branch_name
        payload = {
            "title": title,
            "head": head_param,
            "base": self.base_branch,
            "body": "\n".join(body_lines),
            "draft": False,
            "maintainer_can_modify": True
        }

        try:
            data = self.github.post(self.pulls_path, payload)
            print(f"[RepoManager] Created Pull Request #{data.get('number')}: {data.get('html_url')}", flush=True)
            return {"status": "CREATED", "pr_url": data.get("html_url"), "pr_number": data.get("number"), "branch": branch_name}
        except GitHubError as he:
            # If PR already exists for this branch, lookup existing PR (Idempotent)
            if he.status == 422 and "already exists" in he.body:
                print(f"[RepoManager] PR already exists for branch {branch_name}, looking up PR URL...", flush=True)
                existing = self._find_existing_pr(branch_name)
                if existing:
                    return {"status": "EXISTING", "pr_url": existing.get("html_url"), "pr_number": existing.get("number"), "branch": branch_name}
            print(f"[RepoManager] [WARN] GitHub PR creation failed: {he}", flush=True)
            return {"status": "ERROR", "error": str(he), "branch": branch_name, "pr_url": None}
        except (OSError, ValueError) as ex:
            print(f"[RepoManager] [WARN] PR creation exception: {ex}", flush=True)
            return {"status": "ERROR", "error": str(ex), "branch": branch_name, "pr_url": None}

    def _find_existing_pr(self, branch_name: str) -> Optional[Dict[str, Any]]:
        """Queries GitHub for an existing open PR matching head branch."""
        try:
            prs = self.github.get(self.pulls_path, params={"head": f"{self._head_owner}:{branch_name}", "state": "open"})
            return prs[0] if prs else None
        except (GitHubError, OSError, ValueError) as e:
            print(f"[RepoManager] [WARN] Existing PR lookup failed: {e}", flush=True)
            return None

    def _find_open_prs_for_package(self, package_id: str) -> List[Dict[str, Any]]:
        """Queries GitHub for any open PRs whose head branch belongs to this package."""
        try:
            prs = self.github.get_all(self.pulls_path, {"state": "open"})
        except (GitHubError, OSError, ValueError) as e:
            print(f"[RepoManager] [WARN] Open PR lookup failed: {e}", flush=True)
            return []
        prefix = f"{self.config.pull_request.branch_prefix}{package_id}-"
        owner = self._head_owner.lower()
        return [
            p for p in prs
            if p.get("head", {}).get("ref", "").startswith(prefix)
            and p.get("head", {}).get("user", {}).get("login", "").lower() == owner
        ]

    def sync_open_pr_statuses(self, store: Optional[Any] = None) -> Dict[str, Any]:
        """
        Synchronizes datastore status with remote GitHub Pull Request states.
        Each tracked candidate's PR is fetched by number (no pagination limits):
          - merged           -> candidate MERGED, package UP_TO_DATE at the candidate version
          - closed unmerged  -> candidate/package back to UPDATE_FOUND
          - open             -> candidate READY_FOR_REVIEW (unless TESTING / TEST_FAILED)
        SNOOZED / BLOCKED candidates are only touched when their PR was merged.
        """
        if store is None:
            from datastore import get_datastore
            store = get_datastore()

        candidates = store.list_candidates()
        changes = []
        errors = 0

        for cand in candidates:
            status = cand.get("status")
            ref = parse_pr_url(cand.get("pr_url"))
            if not ref or status == CandidateStatus.MERGED:
                continue
            pr_owner, pr_repo, pr_num = ref
            cand_id, pkg_id = cand["candidate_id"], cand.get("package_id")

            try:
                pr = self.github.get(f"/repos/{pr_owner}/{pr_repo}/pulls/{pr_num}")
            except GitHubError as he:
                if he.status != 404:
                    errors += 1
                    print(f"[RepoManager] [WARN] Failed to fetch PR #{pr_num} (HTTP {he.status})", flush=True)
                    continue
                pr = None
            except (OSError, ValueError) as e:
                errors += 1
                print(f"[RepoManager] [WARN] Failed to fetch PR #{pr_num}: {e}", flush=True)
                continue

            if pr and pr.get("merged"):
                print(f"[RepoManager] PR #{pr_num} was MERGED into {self.base_branch}. Updating status to UP_TO_DATE.", flush=True)
                store.update_candidate(cand_id, {"status": CandidateStatus.MERGED, "pr_url": pr.get("html_url")})
                store.update_package(pkg_id, {
                    "status": PackageStatus.UP_TO_DATE,
                    "current_version": cand.get("version"),
                    "snooze_until": None,
                    "snoozed_version": None,
                    "blocked_version": None
                })
                # Remove superseded SNOOZED and BLOCKED candidates for this package
                for other in candidates:
                    if other.get("package_id") == pkg_id and other.get("status") in POLICY_STATUSES and other["candidate_id"] != cand_id:
                        store.delete_candidate(other["candidate_id"])
                        other["status"] = "DELETED"
                cand["status"] = CandidateStatus.MERGED
                changes.append({"candidate_id": cand_id, "action": "MERGED", "pr_number": pr_num})
                continue

            if status not in PR_TRACKED_CANDIDATE_STATUSES:
                continue

            if pr is None or pr.get("state") == "closed":
                reason = "not found" if pr is None else "closed without merge"
                print(f"[RepoManager] PR #{pr_num} for package '{pkg_id}' {reason}. Moving back to UPDATE_FOUND.", flush=True)
                store.update_candidate(cand_id, {"status": CandidateStatus.UPDATE_FOUND, "pr_url": None})
                cand["status"] = CandidateStatus.UPDATE_FOUND
                changes.append({"candidate_id": cand_id, "action": "REVERTED_TO_UPDATE_FOUND", "pr_number": pr_num})
            elif pr.get("html_url") != cand.get("pr_url"):
                store.update_candidate(cand_id, {"pr_url": pr.get("html_url"), "branch": pr.get("head", {}).get("ref")})

        # Package status mirrors its most advanced active candidate (manual policy states win).
        if changes:
            by_pkg: Dict[str, List[str]] = {}
            for c in candidates:
                by_pkg.setdefault(c.get("package_id"), []).append(c.get("status"))
            for pkg in store.list_packages():
                pid, pstatus = pkg.get("package_id"), pkg.get("status")
                if pstatus in POLICY_STATUSES or pstatus == PackageStatus.UP_TO_DATE:
                    continue
                derived = derive_package_status(by_pkg.get(pid, ()))
                if derived and derived != pstatus:
                    store.update_package(pid, {"status": derived})

        return {"synced": errors == 0, "changes": changes, "error": f"{errors} PR lookup(s) failed" if errors else None}
