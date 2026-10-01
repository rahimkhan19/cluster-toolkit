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
Central Configuration Loader for Cluster Toolkit Automated Infrastructure Updater.

Loads environment variables from tools/infra_updater/.env and parameters from config.yaml.
Environment variables always take precedence over config.yaml values.
Environment-specific values (project, database, repository, model) live only in config.yaml;
the defaults below are behavioral defaults that are the same for every deployment.
"""

import os
import re
import subprocess
from typing import Any, Dict, Optional, Tuple

import yaml
from dotenv import load_dotenv

import http_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_YAML_PATH = os.path.join(BASE_DIR, "config.yaml")
DOTENV_PATH = os.path.join(BASE_DIR, ".env")

# Load .env into os.environ on module import (existing env vars win).
load_dotenv(DOTENV_PATH, override=False)

GITHUB_REPO_RE = re.compile(r"github\.com[/:]([^/]+)/([^/.]+)")
TOKEN_SECRET_NAME = "infra-updater-github-token"


class ConfigError(ValueError):
    pass


def _required(raw: Dict[str, Any], section: str, key: str, env: Optional[str] = None) -> str:
    val = (os.environ.get(env) if env else None) or raw.get(key)
    if not val:
        hint = f" (or env {env})" if env else ""
        raise ConfigError(f"Missing required setting '{section}.{key}' in {CONFIG_YAML_PATH}{hint}.")
    return str(val)


def _resolve_github_token(project_id: str) -> Optional[str]:
    """Returns the GitHub token from the environment, falling back to Secret Manager."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val

    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{TOKEN_SECRET_NAME}/versions/latest"
        return client.access_secret_version(name=name).payload.data.decode("utf-8").strip() or None
    except Exception as e:  # Secret missing / no ADC: run unauthenticated.
        print(f"[Config] [WARN] No GitHub token in env and Secret Manager lookup failed: {e}", flush=True)
        return None


def _resolve_git_author(raw: Dict[str, Any], token: Optional[str]) -> Tuple[str, str]:
    """
    Resolves the git author once, in order:
    env (GIT_AUTHOR_NAME/EMAIL) -> config.yaml git.* -> GitHub /user -> local git config -> defaults.
    """
    name = os.environ.get("GIT_AUTHOR_NAME") or raw.get("author_name")
    email = os.environ.get("GIT_AUTHOR_EMAIL") or raw.get("author_email")
    if name and email:
        return name.strip(), email.strip()

    login = None
    if token:
        try:
            data = http_client.GitHubClient(token).get("/user")
            login = data.get("login")
            name = name or data.get("name")
            email = email or data.get("email")
        except (http_client.GitHubError, OSError, ValueError) as e:
            print(f"[Config] [WARN] Could not resolve git author from GitHub: {e}", flush=True)

    def _git_cfg(key: str) -> Optional[str]:
        res = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True, check=False)
        return res.stdout.strip() or None

    name = name or _git_cfg("user.name") or login or "Cluster Toolkit Updater"
    email = email or _git_cfg("user.email") or (f"{login}@users.noreply.github.com" if login else "infra-updater@google.com")
    return name, email


class DatabaseConfig:
    def __init__(self, raw: Dict[str, Any]):
        self.provider: str = (os.environ.get("UPDATER_DB_PROVIDER") or raw.get("provider") or "firestore").lower()
        self.project_id: str = _required(raw, "database", "project_id", "GOOGLE_CLOUD_PROJECT")
        self.database_id: Optional[str] = (
            _required(raw, "database", "database_id", "FIRESTORE_DATABASE_ID") if self.provider == "firestore" else None
        )


class RepositoryConfig:
    def __init__(self, raw: Dict[str, Any]):
        self.url: str = _required(raw, "repository", "url", "UPDATER_TARGET_REPO")
        self.branch: str = os.environ.get("UPDATER_BASE_BRANCH") or raw.get("branch", "develop")
        self.base_branch: str = self.branch

        m = GITHUB_REPO_RE.search(self.url)
        if not m:
            raise ConfigError(f"repository.url must be a GitHub repository URL, got '{self.url}'.")
        self.owner: str = m.group(1)
        self.name: str = m.group(2)

        # Optional fork repository to push update branches to
        fork = (os.environ.get("UPDATER_FORK_REPO") or raw.get("fork_url") or "").strip()
        m_fork = GITHUB_REPO_RE.search(fork) if fork else None
        self.fork_url: Optional[str] = fork or None
        self.fork_owner: Optional[str] = m_fork.group(1) if m_fork else None
        self.fork_name: Optional[str] = m_fork.group(2) if m_fork else None

    @property
    def push_url(self) -> str:
        """URL of the repository where update branches are pushed."""
        return self.fork_url or self.url

    @property
    def push_owner(self) -> str:
        return self.fork_owner or self.owner

    @property
    def push_name(self) -> str:
        return self.fork_name or self.name

    @property
    def is_fork(self) -> bool:
        """True if branches are pushed to a fork rather than the upstream repository."""
        return bool(self.fork_owner and self.fork_owner.lower() != self.owner.lower())


class PullRequestConfig:
    """Branch naming and commit / PR title templates. Placeholders: {package_id}, {version}."""

    def __init__(self, raw: Dict[str, Any]):
        self.branch_prefix: str = raw.get("branch_prefix", "infra-update/")
        self.commit_title: str = raw.get("commit_title", "[infra-update] Upgrade {package_id} to {version}")
        self.pr_title: str = raw.get("pr_title", "[Infra Update] Upgrade {package_id} to {version}")


class CloudBuildConfig:
    def __init__(self, raw: Dict[str, Any], default_project: str):
        self.project_id: str = os.environ.get("CLOUD_BUILD_PROJECT") or raw.get("project_id") or default_project
        self.trigger_prefix: str = raw.get("trigger_prefix", "PR-test-")
        self.tests_dir: str = raw.get("tests_dir", "tools/cloud-build/daily-tests/tests")
        self.builds_dir: str = raw.get("builds_dir", "tools/cloud-build/daily-tests/builds")
        self.console_url: str = raw.get(
            "console_url", "https://console.cloud.google.com/cloud-build/builds/{build_id}?project={project_id}")
        # Integration tests can run for about a day; after this a running test is marked TIMEOUT.
        self.max_test_duration_seconds: int = int(float(raw.get("max_test_duration_hours", 24)) * 3600)
        # How long to wait for the PR webhook to create a build before reporting ERROR.
        self.build_lookup_timeout_seconds: int = int(raw.get("build_lookup_timeout_seconds", 90))


class GitConfig:
    def __init__(self, raw: Dict[str, Any], token: Optional[str]):
        self.author_name, self.author_email = _resolve_git_author(raw, token)


class LLMConfig:
    def __init__(self, raw: Dict[str, Any], default_project: str):
        self.model: str = _required(raw, "llm", "model", "GEMINI_MODEL")
        self.project_id: str = raw.get("project_id") or default_project
        self.location: str = os.environ.get("GOOGLE_CLOUD_REGION") or raw.get("location", "global")


class HttpConfig:
    def __init__(self, raw: Dict[str, Any]):
        self.timeout_seconds: int = int(raw.get("timeout_seconds", 20))
        self.user_agent: str = raw.get("user_agent", "ClusterToolkitInfraUpdater/1.0")


class PolicyConfig:
    def __init__(self, raw: Dict[str, Any]):
        self.default_snooze_days: int = int(raw.get("default_snooze_days", 30))


class QualificationConfig:
    def __init__(self, raw: Dict[str, Any]):
        self.max_workers: int = max(1, int(raw.get("max_workers", 6)))
        self.max_candidates: int = max(1, int(raw.get("max_candidates", 25)))


class ServerConfig:
    def __init__(self, raw: Dict[str, Any]):
        self.port: int = int(os.environ.get("PORT") or os.environ.get("UPDATER_SERVER_PORT") or raw.get("port", 8080))
        self.host: str = raw.get("host", "0.0.0.0")
        self.pr_sync_interval_seconds: int = int(raw.get("pr_sync_interval_seconds", 60))
        self.test_poll_interval_seconds: int = int(raw.get("test_poll_interval_seconds", 60))


class UpdaterConfig:
    def __init__(self, config_path: str = CONFIG_YAML_PATH):
        raw: Dict[str, Any] = {}
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}

        self.http = HttpConfig(raw.get("http", {}))
        http_client.configure(self.http.user_agent, self.http.timeout_seconds)

        self.database = DatabaseConfig(raw.get("database", {}))
        self.repository = RepositoryConfig(raw.get("repository", {}))
        self.pull_request = PullRequestConfig(raw.get("pull_request", {}))
        self.cloud_build = CloudBuildConfig(raw.get("cloud_build", {}), self.database.project_id)
        self.llm = LLMConfig(raw.get("llm", {}), self.database.project_id)
        self.policy = PolicyConfig(raw.get("policy", {}))
        self.qualification = QualificationConfig(raw.get("qualification", {}))
        self.server = ServerConfig(raw.get("server", {}))

        self.github_token: Optional[str] = _resolve_github_token(self.database.project_id)
        self.git = GitConfig(raw.get("git", {}), self.github_token)

        # Export resolved values so git and child CLI processes reuse them
        # without repeating Secret Manager / GitHub lookups.
        if self.github_token:
            os.environ["GITHUB_TOKEN"] = self.github_token
        os.environ["GIT_AUTHOR_NAME"] = self.git.author_name
        os.environ["GIT_AUTHOR_EMAIL"] = self.git.author_email

    def get_workspace_path(self) -> str:
        """Absolute path of the target repository workspace (UPDATER_WORKSPACE_DIR overrides)."""
        return os.path.abspath(os.environ.get("UPDATER_WORKSPACE_DIR") or os.path.join(BASE_DIR, "target_repo"))


_GLOBAL_CONFIG: Optional[UpdaterConfig] = None

def get_config(reload: bool = False) -> UpdaterConfig:
    global _GLOBAL_CONFIG
    if _GLOBAL_CONFIG is None or reload:
        _GLOBAL_CONFIG = UpdaterConfig()
    return _GLOBAL_CONFIG
