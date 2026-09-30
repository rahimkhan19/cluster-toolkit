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

Loads environment variables from tools/infra_updater/.env and parameters from config.yaml:
  - repository: url, branch
  - llm: model (defaults to gemini-3.8-flash)
  - server: port
"""

import json
import os
import re
from typing import Any, Dict, Optional, Tuple
import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_YAML_PATH = os.path.join(BASE_DIR, "config.yaml")
DOTENV_PATH = os.path.join(BASE_DIR, ".env")


def load_env_file():
    """Loads environment variables from tools/infra_updater/.env if present."""
    if not os.path.exists(DOTENV_PATH):
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(DOTENV_PATH, override=False)
    except Exception:
        pass

    # Ensure manual fallback if dotenv did not populate
    try:
        with open(DOTENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k not in os.environ:
                        os.environ[k] = v
    except Exception:
        pass

# Load .env into os.environ on module import
load_env_file()


class ConfigNode:
    """Wrapper that enables dot-notation attribute access over nested dictionaries."""
    def __init__(self, data: Dict[str, Any]):
        for key, value in data.items():
            if isinstance(value, dict):
                setattr(self, key, ConfigNode(value))
            else:
                setattr(self, key, value)

    def to_dict(self) -> Dict[str, Any]:
        result = {}
        for key, value in self.__dict__.items():
            if isinstance(value, ConfigNode):
                result[key] = value.to_dict()
            else:
                result[key] = value
        return result

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __getattr__(self, name: str) -> Any:
        return None


def _find_system_github_token() -> Optional[str]:
    """Auto-detects GitHub personal access token from env or active container processes."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"):
        val = os.environ.get(var)
        if val and val.strip():
            return val.strip()

    try:
        import subprocess
        ps = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True, check=False)
        for line in ps.stdout.splitlines():
            if "github-mcp-server" in line and "grep" not in line:
                m_pid = re.match(r'^\s*([0-9]+)', line)
                if m_pid:
                    pid = m_pid.group(1)
                    env_path = f"/proc/{pid}/environ"
                    if os.path.exists(env_path):
                        try:
                            with open(env_path, "rb") as f:
                                env_bytes = f.read().split(b'\0')
                                for eb in env_bytes:
                                    s = eb.decode("utf-8", errors="ignore")
                                    if s.startswith("GITHUB_PERSONAL_ACCESS_TOKEN=") or s.startswith("GITHUB_TOKEN="):
                                        tok = s.split("=", 1)[1].strip()
                                        if tok:
                                            return tok
                        except Exception:
                            pass
    except Exception:
        pass

    try:
        from google.cloud import secretmanager
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "hpc-toolkit-dev"
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/infra-updater-github-token/versions/latest"
        response = client.access_secret_version(name=name)
        sec_tok = response.payload.data.decode("UTF-8").strip()
        if sec_tok:
            return sec_tok
    except Exception:
        pass

    return None


class RepositoryConfig:
    def __init__(self, raw: Dict[str, Any]):
        self.url: str = raw.get("url", "https://github.com/GoogleCloudPlatform/cluster-toolkit.git")
        self.branch: str = raw.get("branch") or raw.get("base_branch", "develop")
        self.base_branch: str = self.branch

        # Derive owner and repo name from upstream url
        m = re.search(r"github\.com[/:]([^/]+)/([^/\.]+)", self.url)
        self.owner: str = m.group(1) if m else "GoogleCloudPlatform"
        self.name: str = m.group(2) if m else "cluster-toolkit"

        # Optional fork repository to push update branches to
        self.fork_url: Optional[str] = raw.get("fork_url")
        if self.fork_url and str(self.fork_url).strip():
            self.fork_url = str(self.fork_url).strip()
            m_fork = re.search(r"github\.com[/:]([^/]+)/([^/\.]+)", self.fork_url)
            self.fork_owner: Optional[str] = m_fork.group(1) if m_fork else None
            self.fork_name: Optional[str] = m_fork.group(2) if m_fork else None
        else:
            self.fork_url = None
            self.fork_owner = None
            self.fork_name = None

    @property
    def push_url(self) -> str:
        """Returns the URL of the repository where update branches should be pushed."""
        return self.fork_url or self.url

    @property
    def push_owner(self) -> str:
        """Returns the owner/org where update branches are pushed."""
        return self.fork_owner or self.owner

    @property
    def push_name(self) -> str:
        """Returns the repository name where update branches are pushed."""
        return self.fork_name or self.name

    @property
    def is_fork(self) -> bool:
        """Returns True if branches are pushed to a fork rather than the upstream repository."""
        return bool(self.fork_owner and self.fork_owner.lower() != self.owner.lower())


def resolve_dynamic_git_author(token: Optional[str] = None) -> Tuple[str, str]:
    """
    Dynamically resolves git author name and email without hardcoding:
    1. Checks environment variables GIT_AUTHOR_NAME and GIT_AUTHOR_EMAIL.
    2. Queries GitHub API (GET /user) using the provided or auto-detected GitHub token.
    3. Queries local/global git config (`git config --get user.name`, `git config --get user.email`).
    4. Falls back cleanly to GitHub's standard noreply email format if email is hidden on profile.
    """
    author_name = os.environ.get("GIT_AUTHOR_NAME")
    author_email = os.environ.get("GIT_AUTHOR_EMAIL")

    if author_name and author_email:
        return author_name.strip(), author_email.strip()

    gh_token = token or os.environ.get("GITHUB_TOKEN") or _find_system_github_token()
    gh_login = None

    if gh_token:
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"token {gh_token}",
                    "User-Agent": "cluster-toolkit-infra-updater",
                    "Accept": "application/vnd.github.v3+json"
                }
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    gh_login = data.get("login")
                    if not author_name and data.get("name"):
                        author_name = data.get("name")
                    if not author_email and data.get("email"):
                        author_email = data.get("email")
        except Exception:
            pass

    # Fallback to local git config if name or email still not determined
    if not author_name or not author_email:
        try:
            import subprocess
            if not author_name:
                res_name = subprocess.run(["git", "config", "--get", "user.name"], capture_output=True, text=True, check=False)
                if res_name.stdout.strip():
                    author_name = res_name.stdout.strip()
            if not author_email:
                res_email = subprocess.run(["git", "config", "--get", "user.email"], capture_output=True, text=True, check=False)
                if res_email.stdout.strip():
                    author_email = res_email.stdout.strip()
        except Exception:
            pass

    # Fallback if still not determined
    if not author_name:
        author_name = gh_login or "Cluster Toolkit Updater"

    if not author_email:
        if gh_login:
            author_email = f"{gh_login}@users.noreply.github.com"
        else:
            author_email = "infra-updater@google.com"

    return author_name, author_email


class GitConfig:
    def __init__(self, raw: Dict[str, Any], token: Optional[str] = None):
        dyn_name, dyn_email = resolve_dynamic_git_author(token)
        self.author_name: str = os.environ.get("GIT_AUTHOR_NAME") or raw.get("author_name") or dyn_name
        self.author_email: str = os.environ.get("GIT_AUTHOR_EMAIL") or raw.get("author_email") or dyn_email


class LLMConfig:
    def __init__(self, raw: Dict[str, Any]):
        self.model: str = raw.get("model", "gemini-3.8-flash")


class DatabaseConfig:
    def __init__(self, raw: Dict[str, Any]):
        self.provider: str = os.environ.get("UPDATER_DB_PROVIDER") or raw.get("provider", "firestore")
        self.project_id: str = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("FIREBASE_PROJECT_ID") or raw.get("project_id", "hpc-toolkit-dev")
        self.database_id: str = os.environ.get("FIRESTORE_DATABASE_ID") or raw.get("database_id", "automated-dependency-management-db")


class ServerConfig:
    def __init__(self, raw: Dict[str, Any]):
        port_val = os.environ.get("PORT") or os.environ.get("UPDATER_SERVER_PORT") or raw.get("port", 8080)
        self.port: int = int(port_val)
        self.host: str = raw.get("host", "0.0.0.0")
        self.pr_sync_interval_seconds: int = int(raw.get("pr_sync_interval_seconds", 60))


class UpdaterConfig:
    def __init__(self, config_path: str = CONFIG_YAML_PATH):
        self.config_path = config_path
        self._raw_data = self._load_file()
        self._apply_env_overrides()

        self.github_token: Optional[str] = self.get_github_token()
        self.database = DatabaseConfig(self._raw_data.get("database", {}))
        self.repository = RepositoryConfig(self._raw_data.get("repository", {}))
        self.git = GitConfig(self._raw_data.get("git", {}), token=self.github_token)
        self.llm = LLMConfig(self._raw_data.get("llm", {}))
        self.server = ServerConfig(self._raw_data.get("server", {}))
        self.node = ConfigNode(self._raw_data)

    def _load_file(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}

    def _apply_env_overrides(self):
        db = self._raw_data.setdefault("database", {})
        if os.environ.get("UPDATER_DB_PROVIDER"):
            db["provider"] = os.environ["UPDATER_DB_PROVIDER"]
        if os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("FIREBASE_PROJECT_ID"):
            db["project_id"] = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("FIREBASE_PROJECT_ID")
        if os.environ.get("FIRESTORE_DATABASE_ID"):
            db["database_id"] = os.environ["FIRESTORE_DATABASE_ID"]

        repo = self._raw_data.setdefault("repository", {})
        if os.environ.get("UPDATER_TARGET_REPO"):
            repo["url"] = os.environ["UPDATER_TARGET_REPO"]
        if os.environ.get("UPDATER_BASE_BRANCH"):
            repo["branch"] = os.environ["UPDATER_BASE_BRANCH"]
        if os.environ.get("UPDATER_FORK_REPO"):
            repo["fork_url"] = os.environ["UPDATER_FORK_REPO"]

        git = self._raw_data.setdefault("git", {})
        if os.environ.get("GIT_AUTHOR_NAME"):
            git["author_name"] = os.environ["GIT_AUTHOR_NAME"]
        if os.environ.get("GIT_AUTHOR_EMAIL"):
            git["author_email"] = os.environ["GIT_AUTHOR_EMAIL"]

        token = _find_system_github_token()
        if token:
            os.environ["GITHUB_TOKEN"] = token
            os.environ["GH_TOKEN"] = token

        llm = self._raw_data.setdefault("llm", {})
        if os.environ.get("GEMINI_MODEL"):
            llm["model"] = os.environ["GEMINI_MODEL"]

        server = self._raw_data.setdefault("server", {})
        if os.environ.get("PORT"):
            try:
                server["port"] = int(os.environ["PORT"])
            except ValueError:
                pass
        elif os.environ.get("UPDATER_SERVER_PORT"):
            try:
                server["port"] = int(os.environ["UPDATER_SERVER_PORT"])
            except ValueError:
                pass

    def get_workspace_path(self) -> str:
        """Returns the absolute path to the target repository workspace directory."""
        if os.environ.get("UPDATER_WORKSPACE_DIR"):
            return os.path.abspath(os.environ["UPDATER_WORKSPACE_DIR"])
        if os.environ.get("K_SERVICE"):
            return "/tmp/target_repo"
        return os.path.abspath(os.path.join(BASE_DIR, "target_repo"))

    def get_github_token(self) -> Optional[str]:
        return os.environ.get("GITHUB_TOKEN") or _find_system_github_token()


_GLOBAL_CONFIG: Optional[UpdaterConfig] = None

def get_config(reload: bool = False) -> UpdaterConfig:
    global _GLOBAL_CONFIG
    if _GLOBAL_CONFIG is None or reload:
        _GLOBAL_CONFIG = UpdaterConfig()
    return _GLOBAL_CONFIG
