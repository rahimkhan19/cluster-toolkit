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
Shared HTTP helpers: one User-Agent, one default timeout, and one GitHub REST client.

This module has no dependency on config.py (config.py calls `configure()` at load time),
so it can be used while the configuration itself is being resolved.
"""

from typing import Any, Dict, Iterator, List, Optional

import requests

GITHUB_API = "https://api.github.com"

_settings = {"user_agent": "ClusterToolkitInfraUpdater/1.0", "timeout": 20}


def configure(user_agent: str, timeout: int) -> None:
    _settings.update(user_agent=user_agent, timeout=timeout)


def default_timeout() -> int:
    return _settings["timeout"]


def request(method: str, url: str, **kwargs) -> requests.Response:
    """requests.request with the updater's User-Agent and default timeout applied."""
    headers = {"User-Agent": _settings["user_agent"], **(kwargs.pop("headers", None) or {})}
    kwargs.setdefault("timeout", _settings["timeout"])
    return requests.request(method, url, headers=headers, **kwargs)


def get(url: str, **kwargs) -> requests.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    return request("POST", url, **kwargs)


def is_url_live(url: str) -> bool:
    """True if the artifact URL answers 200 (HEAD, falling back to GET for servers that reject HEAD)."""
    if not url:
        return False
    if url.startswith(("docker://", "docker.io/")):
        return True
    try:
        resp = request("HEAD", url, allow_redirects=True)
        if resp.status_code in (403, 405, 429):
            resp = get(url, stream=True, allow_redirects=True)
            resp.close()
        return resp.status_code == 200
    except requests.RequestException:
        return False


class GitHubError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"GitHub API HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class GitHubClient:
    """Minimal GitHub REST client (auth header, timeout, pagination)."""

    def __init__(self, token: Optional[str] = None):
        self.token = token

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _call(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"
        resp = request(method, url, headers=self._headers(), **kwargs)
        if resp.status_code >= 400:
            raise GitHubError(resp.status_code, resp.text)
        return resp

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self._call("GET", path, params=params).json()

    def post(self, path: str, payload: Dict[str, Any]) -> Any:
        return self._call("POST", path, json=payload).json()

    def paginate(self, path: str, params: Optional[Dict[str, Any]] = None, max_pages: int = 10) -> Iterator[Any]:
        """Yields items across pages by following the Link: rel="next" header."""
        url: Optional[str] = path
        query = {"per_page": 100, **(params or {})}
        for _ in range(max_pages):
            if not url:
                return
            resp = self._call("GET", url, params=query)
            yield from resp.json()
            url = resp.links.get("next", {}).get("url")
            query = None  # the next URL already carries the query string

    def get_all(self, path: str, params: Optional[Dict[str, Any]] = None, max_pages: int = 10) -> List[Any]:
        return list(self.paginate(path, params, max_pages))
