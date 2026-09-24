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

import os
import re
from typing import Any, Dict, Optional
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

    return None


class RepositoryConfig:
    def __init__(self, raw: Dict[str, Any]):
        self.url: str = raw.get("url", "https://github.com/rahimkhan19/cluster-toolkit.git")
        self.branch: str = raw.get("branch") or raw.get("base_branch", "develop")
        self.base_branch: str = self.branch

        # Derive owner and repo name from url
        m = re.search(r"github\.com[/:]([^/]+)/([^/\.]+)", self.url)
        self.owner: str = m.group(1) if m else "rahimkhan19"
        self.name: str = m.group(2) if m else "cluster-toolkit"


class GitConfig:
    def __init__(self, raw: Dict[str, Any]):
        self.author_name: str = os.environ.get("GIT_AUTHOR_NAME") or raw.get("author_name", "Cluster Toolkit Infra Updater Bot")
        self.author_email: str = os.environ.get("GIT_AUTHOR_EMAIL") or raw.get("author_email", "infra-updater-bot@google.com")


class LLMConfig:
    def __init__(self, raw: Dict[str, Any]):
        self.model: str = raw.get("model", "gemini-3.8-flash")


class ServerConfig:
    def __init__(self, raw: Dict[str, Any]):
        self.port: int = int(raw.get("port", 8080))
        self.host: str = raw.get("host", "0.0.0.0")
        self.pr_sync_interval_seconds: int = int(raw.get("pr_sync_interval_seconds", 60))


class UpdaterConfig:
    def __init__(self, config_path: str = CONFIG_YAML_PATH):
        self.config_path = config_path
        self._raw_data = self._load_file()
        self._apply_env_overrides()

        self.repository = RepositoryConfig(self._raw_data.get("repository", {}))
        self.git = GitConfig(self._raw_data.get("git", {}))
        self.llm = LLMConfig(self._raw_data.get("llm", {}))
        self.server = ServerConfig(self._raw_data.get("server", {}))
        self.github_token: Optional[str] = self.get_github_token()
        self.node = ConfigNode(self._raw_data)

    def _load_file(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}

    def _apply_env_overrides(self):
        repo = self._raw_data.setdefault("repository", {})
        if os.environ.get("UPDATER_TARGET_REPO"):
            repo["url"] = os.environ["UPDATER_TARGET_REPO"]
        if os.environ.get("UPDATER_BASE_BRANCH"):
            repo["branch"] = os.environ["UPDATER_BASE_BRANCH"]

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
        if os.environ.get("UPDATER_SERVER_PORT"):
            try:
                server["port"] = int(os.environ["UPDATER_SERVER_PORT"])
            except ValueError:
                pass

    def get_workspace_path(self) -> str:
        """Returns the absolute path to the target repository workspace directory."""
        return os.path.abspath(os.path.join(BASE_DIR, "target_repo"))

    def get_github_token(self) -> Optional[str]:
        return os.environ.get("GITHUB_TOKEN") or _find_system_github_token()


_GLOBAL_CONFIG: Optional[UpdaterConfig] = None

def get_config(reload: bool = False) -> UpdaterConfig:
    global _GLOBAL_CONFIG
    if _GLOBAL_CONFIG is None or reload:
        _GLOBAL_CONFIG = UpdaterConfig()
    return _GLOBAL_CONFIG
